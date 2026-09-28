"""发布矩阵判据：合法 skip 的闭集 + 提交报告的可复现面。

动机（N-31）：`docs/VALIDATION.json` 是提交物，CI 用
`make validate && git diff --exit-code docs/VALIDATION.*` 判它新鲜 —— 这只在
"报告内容是仓库代码的函数"时成立。上一轮实测到它不成立：同一棵树跑两次，
一次 `skipped=1`、一次 `skipped=2`（外网 registry 通道抖动让 docker 档一条
自判 PENDING），于是 `test_run.passed/skipped` 与 `integration_docker.status`
连 note 里的 "执行 25/26" 一起漂。漂的不是代码，但提交物跟着抖，
每次都要人手改文档面，而且文档里"唯一 skip 是 X"会变成假话。

本文件的判据把两件事分开：
- 提交面只留可复现门禁（`collected` / `failed` / lint / type / migration / build …），
  环境读数（各集成档状态、跳过几支）另落 `dist/VALIDATION_RUN.*`（gitignored）。
- skip 不再是"数字"，而是"闭集"：只有集成档模块里、文案带该模块哨兵的 skip 合法，
  其余一律判红 —— 抖动只会让合法集合少一支，多出一支就是真信号。
"""

import importlib.util
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_validator():
    path = ROOT / "scripts" / "validate_release.py"
    spec = importlib.util.spec_from_file_location("validate_release", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- 全量 test run 的预算：越界必须是具名判决，不能是 traceback，也不能吃到旧报告 ---


def _mini_suite(tmp_path: Path, slow_seconds: int = 40) -> tuple[list[str], Path]:
    """造一份"一条立刻过、第二条睡死"的真 pytest 套件，返回 cmd 与 junit 路径。

    为什么用真子进程真套件而不是替身：这条判据要证的正是
    "被 SIGINT 的那跑会不会产 JUnit、里面有没有已跑完的用例名"，
    那是 pytest 会话收尾的行为（本机 `_pytest/junitxml.py:647` 只在 sessionfinish 写文件），
    替身证不了。
    """
    suite = tmp_path / "test_mini_budget.py"
    suite.write_text(
        "import time\n\n\n"
        "def test_alpha_fast() -> None:\n    assert True\n\n\n"
        f"def test_beta_sleeps() -> None:\n    time.sleep({slow_seconds})\n",
        encoding="utf-8",
    )
    junit = tmp_path / "mini-junit.xml"
    cmd = [
        sys.executable, "-m", "pytest", str(suite),
        "--junitxml", str(junit), "-q", "-p", "no:cacheprovider",
    ]
    return cmd, junit


def test_bounded_run_turns_a_budget_breach_into_a_named_verdict(tmp_path: Path) -> None:
    """必开火：预算之内跑不完时，不抛 traceback、留下部分 JUnit、判决是 FAIL 而非放行。"""
    v = _load_validator()
    cmd, junit = _mini_suite(tmp_path)
    result = v.run_pytest_bounded(cmd, junit, budget=8, grace=25)
    assert result["timed_out"] is True, result
    assert isinstance(result["returncode"], int) and result["returncode"] != 0, result
    assert junit.exists(), "SIGINT 没让 pytest 收尾出 JUnit：部分报告这条依据不成立"
    names = {tc.get("name") for tc in ET.parse(junit).getroot().iter("testcase")}  # noqa: S314 本机 pytest 产物
    assert "test_alpha_fast" in names, f"已跑完的用例名没进部分报告：{names}"
    assert "test_beta_sleeps" not in names, f"睡死那条竟被算成跑完了：{names}"
    verdict = v.suite_timeout_verdict(True, junit.exists(), 8)
    assert verdict is not None and verdict["status"] == "FAIL", verdict
    assert "无法计数" in verdict["note"] and "部分用例名可用" in verdict["note"], verdict


def test_bounded_run_leaves_a_finished_run_alone(tmp_path: Path) -> None:
    """必不开火：预算够时这条判据不得凭空造出越界（否则超时判决成了免检通道）。"""
    v = _load_validator()
    suite = tmp_path / "test_mini_ok.py"
    suite.write_text("def test_only() -> None:\n    assert 1 == 1\n", encoding="utf-8")
    junit = tmp_path / "mini-ok.xml"
    result = v.run_pytest_bounded(
        [sys.executable, "-m", "pytest", str(suite), "--junitxml", str(junit), "-q", "-p", "no:cacheprovider"],
        junit, budget=120, grace=10,
    )
    assert result["timed_out"] is False and result["returncode"] == 0, result
    assert v.suite_timeout_verdict(False, junit.exists(), 120) is None
    assert {tc.get("name") for tc in ET.parse(junit).getroot().iter("testcase")} == {"test_only"}  # noqa: S314


def test_previous_runs_junit_is_never_consumed_as_this_one(tmp_path: Path) -> None:
    """陈旧报告 hazard：上一跑的 junit 必须在起跑前被删掉。

    `pytest_sessionfinish` 才写文件，而被 SIGKILL 的那跑根本不写——
    于是"这一跑没跑完"与"磁盘上有一份昨天的"同时成立，
    消费方那句 `if junit.exists()` 会把昨天的 647 读成今天的。
    """
    v = _load_validator()
    cmd, junit = _mini_suite(tmp_path)
    junit.write_text(
        '<testsuite name="pytest" tests="999" errors="0" failures="0" skipped="0" time="1.0">'
        '<testcase classname="昨天的" name="test_stale_marker" time="0.1"/></testsuite>',
        encoding="utf-8",
    )
    result = v.run_pytest_bounded(cmd, junit, budget=8, grace=25)
    assert result["timed_out"] is True, result
    names = {tc.get("name") for tc in ET.parse(junit).getroot().iter("testcase")}  # noqa: S314 本机 pytest 产物
    assert "test_stale_marker" not in names, "旧报告被当成本轮读数用了"
    assert "test_alpha_fast" in names, names


def test_timeout_verdict_polarities_are_pure() -> None:
    """判决纯函数三态：没越界 None；越界有报告／没报告两条 note 不同；都不带秒数环境读数。"""
    v = _load_validator()
    assert v.suite_timeout_verdict(False, True, 1500) is None
    with_report = v.suite_timeout_verdict(True, True, 1500)
    without = v.suite_timeout_verdict(True, False, 1500)
    assert with_report["status"] == without["status"] == "FAIL"
    assert "部分用例名可用" in with_report["note"] and "没换来报告" in without["note"]
    for note in (with_report["note"], without["note"]):
        assert "1500" in note, note
        assert not re.search(r"\b已等 [\d.]+s", note), f"提交面那句话里不许有本次跑的秒数：{note}"
    # 预算这个数字只有一处定义，判据不自己再写一遍
    src = (ROOT / "scripts" / "validate_release.py").read_text(encoding="utf-8")
    assert len(re.findall(r"^SUITE_BUDGET_SECONDS\s*=", src, flags=re.M)) == 1
    # test run 那一步必须走有界跑批。查的是**调用形状**而不是 `process.kill()` 这种词——
    # 本文件的注释里本来就要提它，按词查会被自己的正文命中（本轮已在这条上翻过一次）。
    assert "run_pytest_bounded([PYTHON" in src, "test run 又退回裸 subprocess.run 了"
    assert 'code, _ = run([PYTHON, "-m", "pytest"' not in src, "同一处出现第二种起跑形状"


def test_run_maps_a_timeout_onto_124_instead_of_raising() -> None:
    """`run()` 自己也一样：超时返回 124 + 是哪条命令，不许再把整个认证台打死。"""
    v = _load_validator()
    code, text = v.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
    assert code == 124, (code, text[:200])
    assert "命令超时（1s）" in text and "-c" in text, text[:200]

GATES = {
    "tests.test_docker_provider_integration": "DOCKER_VALIDATION_PENDING",
    "tests.test_k8s_integration": "K8S_PHYSICAL_VALIDATION_PENDING",
}


def _junit(tmp_path, body: str) -> dict:
    report = tmp_path / "junit.xml"
    report.write_text(body, encoding="utf-8")
    return _load_validator().count_tests_junit(report)


def test_count_reports_the_names_of_skipped_cases(tmp_path) -> None:
    """skip 必须可按用例名归因，跟 failed_names 同理：只给总数就无法判断"是哪一支在抖"。"""
    counts = _junit(
        tmp_path,
        '<testsuites><testsuite name="pytest" tests="4" skipped="2" failures="0" errors="0">'
        '<testcase classname="tests.a" name="ok"/>'
        '<testcase classname="tests.test_k8s_integration" name="lifecycle">'
        '<skipped type="pytest.skip" message="K8S_PHYSICAL_VALIDATION_PENDING: 需要真集群"/></testcase>'
        '<testcase classname="tests.test_docker_provider_integration" name="fetchable">'
        '<skipped type="pytest.skip" message="DOCKER_VALIDATION_PENDING: 通道不可达"/></testcase>'
        "</testsuite></testsuites>",
    )
    assert counts["skipped"] == 2, counts
    assert counts["skipped_names"] == [
        "tests.test_k8s_integration::lifecycle",
        "tests.test_docker_provider_integration::fetchable",
    ], counts
    # 全量身份清单是"声明未落地"判据的分母：没收集到的用例不该继续被当作合法跳过
    assert "tests.a::ok" in counts["test_names"], counts


def test_admissible_skips_are_gate_module_plus_that_modules_sentinel() -> None:
    """闭集判据：正例不开火、三类反例必须各自开火。"""
    validator = _load_validator()
    fn = validator.skip_admission_offenders

    # 正例：两个集成档模块各按自己的哨兵跳过 → 无越界
    assert fn(GATES, [
        {"id": "tests.test_k8s_integration::lifecycle", "message": "K8S_PHYSICAL_VALIDATION_PENDING: 需要真集群"},
        {"id": "tests.test_docker_provider_integration::fetchable", "message": "DOCKER_VALIDATION_PENDING: 通道抖动"},
    ]) == []
    assert fn(GATES, []) == [], "零 skip 必须合法（判据不得把没跳过读成越界）"

    # 反例 1：同模块但文案没带哨兵 —— 这是普通 skip，不是"整档缺件"
    bad = fn(GATES, [{"id": "tests.test_k8s_integration::lifecycle", "message": "临时跳过"}])
    assert bad and "test_k8s_integration::lifecycle" in bad[0], bad

    # 反例 2：非集成档模块，哪怕文案里带了哨兵（借别人的旗号）
    bad = fn(GATES, [{"id": "tests.test_gpu_pool_guard::reclaim", "message": "DOCKER_VALIDATION_PENDING: 冒名"}])
    assert bad and "test_gpu_pool_guard::reclaim" in bad[0], bad

    # 反例 3：带了对应模块的哨兵，但该模块不在闸清单里（新增档忘了登记）
    bad = fn({}, [{"id": "tests.test_browser_console::x", "message": "BROWSER_VALIDATION_PENDING: 缺浏览器"}])
    assert bad and "test_browser_console::x" in bad[0], bad


def test_gate_sentinels_are_read_live_and_cover_every_integration_module() -> None:
    """闸清单本身不许是手抄表：模块与哨兵都从用例源码现取，且六档齐全。

    这一条钉住的是判据的入域 —— 漏一个模块，那个模块的合法跳过就会被判红，
    而漏的那一档恰恰是"环境本来就可能缺"的那一档。
    """
    validator = _load_validator()
    gates = validator._integration_gates()
    assert len(gates) == 6, sorted(gates)
    for name, spec in gates.items():
        assert spec["module"].startswith("tests."), (name, spec)
        assert len(spec["sentinel"]) > 8, (name, spec)
    assert gates["docker"]["sentinel"] == "DOCKER_VALIDATION_PENDING"
    assert gates["k8s"]["sentinel"] == "K8S_PHYSICAL_VALIDATION_PENDING"

    # 闭集 = 六档 + 非档位但按条件跳过的模块，且哨兵同样由 AST 现取
    universe = validator.conditional_skip_universe()
    assert len(universe) == 6 + len(validator.EXTRA_SKIP_UNIVERSE), sorted(universe)
    assert all(m.startswith("tests.") for m in universe), universe
    # N-33 收口：`test_gpu_pool_guard` 不再有条件跳过，因此它必须离开闭集。
    # 留在闭集里就等于"允许它静默不跑"，而它已经没有任何跳过的分支了。
    assert "tests.test_gpu_pool_guard" not in universe, (
        "池守卫模块还在闭集里：它已经不跳了，留着就是给一个不存在的例外发通行证"
    )
    assert validator.EXTRA_SKIP_UNIVERSE == (), validator.EXTRA_SKIP_UNIVERSE


def test_the_pool_guard_file_has_no_conditional_skip_left() -> None:
    """N-33 的判据化：`tests/test_gpu_pool_guard.py` 里不许再有 `pytest.skip`。

    那两支原先按"共用池的余量"跳过（缺余量=合法），意味着"回收守卫到底还被验过没有"
    取决于跑序。现在 rig 显式把前置达成（回收后断言余量），跳过分支改成断言：
    真达不成就是缺陷，该红，不该被记成"这一跑没跑到"。
    判据走 AST（文本搜 `pytest.skip` 会被注释里的同名说法挡/骗）。
    """
    import ast

    src = (ROOT / "tests" / "test_gpu_pool_guard.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    skips = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "skip"
    ]
    assert not skips, f"还剩 {len(skips)} 处条件跳过：{[n.lineno for n in skips]}"
    assert "GATE_SENTINEL" not in {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}, (
        "哨兵常量还在：它存在的唯一理由就是那两处 skip"
    )


def _checks(**over) -> dict:
    """一份"形状与 validate 产出一致"的最小报告夹具。"""
    checks = {
        "test_collected": {"status": "PASS", "count": 559},
        "test_run": {"status": "PASS", "passed": 557, "skipped": 2, "failed": 0,
                     "failed_names": [], "skipped_names": ["a::b", "c::d"],
                     # 与 validate 的产出保持同形：这三个是本次跑那一侧的环境字段
                     "elapsed_seconds": 632.3, "host_load": [12.9, 8.5, 7.1], "host_cpus": 10},
        "unexpected_skips": {"status": "PASS", "note": "全部 skip 在闭集内"},
        "integration_docker": {"status": "PARTIAL", "note": "执行 25/26，跳过 1"},
        "integration_k8s": {"status": "PENDING", "note": "整档 1 用例未执行"},
        "lint": {"status": "PASS"},
        "typecheck": {"status": "PASS"},
        "migration": {"status": "PASS"},
        "build": {"status": "PASS"},
        "docs_test_counts": {"status": "PASS", "note": "一致"},
        "docs_row_order": {"status": "PASS", "note": "一致"},
    }
    checks.update(over)
    return checks


def test_committed_face_drops_environment_readings_but_keeps_the_facts() -> None:
    """可复现面：环境读数换掉它不变，代码事实换掉它必须变。

    两档缺一不可 —— 只验"抖不动"可以靠把所有字段都遮掉达成（那等于没报告），
    所以同一把尺子要反过来验 `collected`/`failed` 真的还在面上。
    """
    validator = _load_validator()
    base = validator.reproducible_checks(_checks())
    jittered = validator.reproducible_checks(_checks(
        test_run={"status": "PASS", "passed": 558, "skipped": 1, "failed": 0,
                  "failed_names": [], "skipped_names": ["a::b"]},
        integration_docker={"status": "PASS", "note": "执行 26/26 用例在真实后端上执行"},
        integration_k8s={"status": "PENDING", "note": "整档 1 用例未执行，原因换了措辞"},
        unexpected_skips={"status": "PASS", "note": "全部 skip 在闭集内（本跑 1 支）"},
    ))
    assert base == jittered, "环境读数一抖，提交面就跟着抖 —— 这正是 N-31 的缺陷"

    # 反向对照 1：failed 变了必须看得见
    assert validator.reproducible_checks(_checks(
        test_run={"status": "FAIL", "passed": 556, "skipped": 2, "failed": 1,
                  "failed_names": ["tests.a::boom"], "skipped_names": ["a::b", "c::d"]}
    )) != base, "failed 被遮掉了：可复现面成了空壳"
    # 反向对照 2：collected 变了必须看得见
    assert validator.reproducible_checks(_checks(
        test_collected={"status": "PASS", "count": 560}
    )) != base, "collected 被遮掉了：加用例不再需要改文档面"
    # 反向对照 3：lint 红了必须看得见
    assert validator.reproducible_checks(_checks(lint={"status": "FAIL"})) != base
    # 反向对照 4：越界 skip 属环境读数，不进提交面
    assert "unexpected_skips" not in base
    assert "integration_docker" not in base and "integration_k8s" not in base


def test_environment_mask_is_tied_to_fields_that_actually_exist() -> None:
    """遮罩清单要自证：声明的每个环境字段必须真在报告里，且不得把整份报告遮没。"""
    validator = _load_validator()
    assert validator.mask_offenders(_checks()) == []
    bogus = _checks()
    bogus["test_run"].pop("skipped_names")
    offenders = validator.mask_offenders(bogus)
    assert len(offenders) == 1 and "skipped_names" in offenders[0], offenders
    # 反向对照：整份报告只剩环境读数（test_run 合法存在，其他全是 integration_*/闭集判决）
    swallowed = {
        "test_run": bogus["test_run"],
        "integration_docker": {"status": "PASS"},
        "integration_k8s": {"status": "PENDING"},
        "unexpected_skips": {"status": "PASS"},
    }
    offenders = validator.mask_offenders(swallowed)
    assert any("遮没" in line for line in offenders), offenders


def test_run_report_lives_in_the_ignored_directory() -> None:
    """环境读数的落点必须在 gitignored 目录，否则它又变回提交物。"""
    validator = _load_validator()
    assert validator.RUN_REPORT_JSON.parent.name == "dist", validator.RUN_REPORT_JSON
    assert validator.RUN_REPORT_MD.parent.name == "dist", validator.RUN_REPORT_MD
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert any(line.strip().rstrip("/") == "dist" for line in ignore.splitlines()), (
        ".gitignore 没忽略 dist/ → 环境读数会重新进入版本库"
    )
    names = {line.strip().rstrip("/") for line in ignore.splitlines() if line.strip()}
    assert "tmp" in names, (
        ".gitignore 没忽略 tmp/ ⇒ 一次性取证与记账备份会脏工作树，"
        "并把 `_doc_roots()` 的文档根面撑大（N-118 实测：多出 tmp 就产出 2 条假悬空指针）"
    )


def test_scratch_directories_are_not_documentation_roots() -> None:
    """仓内 scratch 目录不得撑大「文档根」这一面（N-118）。

    一手读数：记账脚本把 `.bak` 落进仓内 `tmp/anchor-patch-n116/` 之后，`_doc_roots()`
    （按 `ROOT.iterdir()` 现取顶层目录）多出 `"tmp"`，CHANGELOG:328 与 CURRENT_STATE 里
    那两句**设计上不作为指针**的示例路径 `tmp/probe.py` 立刻被读成悬空引用，主线门
    `doc_references` 当场翻红。机制隔离复算（只换 roots、其余入参取现值）：

        现行 8 个根 → 干净；roots 里加一个 "tmp" → 恰好 2 条同名 offender，差集 2。

    所以这条判据钉的不是 `tmp` 这一 spelled 名字，而是「被 git 忽略的目录不属于文档面」
    这条划界：`.gitignore` 里的 `tmp/` 行与 `_git_ignored_topdirs()` 的过滤任缺其一都红
    （前者由上面那条文本面判据钉，本条钉行为面）。
    """
    validator = _load_validator()
    scratch = ROOT / "tmp"
    made = not scratch.exists()
    scratch.mkdir(exist_ok=True)
    try:
        assert any(p.name == "tmp" for p in ROOT.iterdir() if p.is_dir()), (
            "夹具没能把 scratch 目录造出来，本条就成了恒真断言"
        )
        roots = validator._doc_roots()
        assert "tmp" not in roots, f"scratch 目录进了文档根面 {roots} ⇒ 示例路径会被读成悬空指针"
        offenders = validator.dangling_doc_reference_offenders()
        assert offenders == [], f"doc_references 被工具自己的落点顶红：{offenders}"
        # 分母不许被收坏：收敛 roots 之后，路径指针仍要有实打实的量（与既有那支同一下界）。
        assert validator.doc_reference_stats()["paths"] >= 200, validator.doc_reference_stats()
    finally:
        if made and not any(scratch.iterdir()):
            scratch.rmdir()


def test_pointer_spellings_are_fully_qualified_today() -> None:
    """账面里的路径指针必须写成仓内全路径（N-121 的棘轮，今日基线 0）。

    改前读数（一手）：`CHANGELOG.md`／`docs/CURRENT_STATE.md`／`docs/ARCHITECTURE.md`／
    `docs/ACCEPTANCE_GATES.md`／`docs/SECURITY.md`／两份 review 里共 **114 处**缩写指针
    （`scheduler.py:257-267`、`providers/k8s.py:328-329`、`billing.py:191-198` 这一类），
    而 `doc_references` 的划界只认「以仓内根目录开头」⇒ 这 114 处**没有任何存在性读者**。
    分两步改完：第一轮按反引号形状改 102 处，第二轮调**判据自己的读数**改剩下 12 处
    （那 12 处是不带反引号写的，第一轮的正则看不见——两轮读数都以 `doc_pointer_spelling_readings()`
    为准，不以我自己再数一遍为准）。
    """
    validator = _load_validator()
    readings = validator.doc_pointer_spelling_readings()
    assert readings["unqualified"] == 0, readings
    assert readings["ambiguous"] == 0, readings
    # 非恒真：语料确实很大，把判据收空也会红（这条下界与 :763 那支同源）
    assert readings["qualified"] >= 150, readings
    # 外部写法单独计数，不当违规也不当通过
    assert readings["external"] >= 1, readings
    assert validator.dangling_doc_reference_offenders() == [], validator.dangling_doc_reference_offenders()


def test_the_spelling_clause_fires_on_each_pointer_shape() -> None:
    """四极对照（合成语料＋合成索引，不碰仓）：缩写／歧义／外部／已限定各判各的。

    这一支是上一条的牙齿：没有它，`unqualified == 0` 可能只是因为分母空或形状判据写歪。
    """
    validator = _load_validator()
    index = frozenset({
        "app/services/orchestrator.py",
        "app/routers/usage.py",
        "app/gate.py",
        "scripts/gate.py",
        "docs/CURRENT_STATE.md",
    })
    roots = ("app", "docs", "scripts")

    shortened, counts = validator.unqualified_pointer_readings(
        {"CHANGELOG.md": "结算逻辑见 `orchestrator.py:597`。"}, index, roots)
    assert counts == {"qualified": 0, "unqualified": 1, "ambiguous": 0, "external": 0}, counts
    assert len(shortened) == 1 and "app/services/orchestrator.py:597" in shortened[0], shortened

    ambiguous, counts = validator.unqualified_pointer_readings(
        {"CHANGELOG.md": "门禁装配见 gate.py:9。"}, index, roots)
    assert counts["ambiguous"] == 1 and counts["unqualified"] == 0, counts
    assert len(ambiguous) == 1 and "候选" in ambiguous[0], ambiguous

    qualified, counts = validator.unqualified_pointer_readings(
        {"CHANGELOG.md": "见 `app/services/orchestrator.py:597` 与 docs/CURRENT_STATE.md:9。"}, index, roots)
    assert qualified == [] and counts["qualified"] == 2, (qualified, counts)

    external, counts = validator.unqualified_pointer_readings(
        {"CHANGELOG.md": "moby `api/swagger.yaml:8984-8995` 与 `venv/.../kube_config.py:450`。"}, index, roots)
    assert external == [] and counts["external"] == 2, (external, counts)

    # 边界自证：把缩写写进一个更长行号旁边，不得被当成同一处
    both, counts = validator.unqualified_pointer_readings(
        {"CHANGELOG.md": "`app/routers/usage.py:36` 与 usage.py:365 两种写法。"}, index, roots)
    assert counts["qualified"] == 1 and counts["unqualified"] == 1, counts
    assert len(both) == 1 and "usage.py:365" in both[0], both


def test_the_gate_forwards_the_spelling_clause(monkeypatch) -> None:
    """接线判据：门禁本体必须真的把新判据的读数并进 offenders（电池 C1 逼出来的）。

    实测形状：把 `dangling_doc_reference_offenders()` 里那句 `extra` 摘掉之后，上面两支判据
    照绿（它们各自直接调读数函数），也就是说**报告面不再核这条**而测试面全绿——
    判据退化成死码。这一支换的是语料提供者（不换被测函数），让门禁在合成语料上走一遍真接线。
    """
    validator = _load_validator()
    monkeypatch.setattr(
        validator, "_doc_pointer_texts", lambda: {"CHANGELOG.md": "结算逻辑见 orchestrator.py:597。"}
    )
    offenders = validator.dangling_doc_reference_offenders()
    assert any("少写仓内前缀" in o for o in offenders), offenders
    assert any("app/services/orchestrator.py:597" in o for o in offenders), offenders


def test_committed_docs_json_is_written_from_the_reproducible_view() -> None:
    """接线判据：提交面确实经过投影，运行面确实另写一份。

    恒真写法（"源码里出现过 reproducible_checks 字样"）挡不住"投影算了但没传给
    写盘"，所以这里取的是 docs/VALIDATION.json 那条 write_text 的实参形状。
    """
    text = (ROOT / "scripts" / "validate_release.py").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if '"VALIDATION.json"' in line]
    assert len(lines) == 1, f"docs/VALIDATION.json 的写盘点应恰好 1 处，实读 {len(lines)}"
    assert '"checks": reproducible_checks(checks),' in text, (
        "docs/VALIDATION.json 又直接用全量 checks —— 环境读数会重新进入提交面"
    )
    assert "RUN_REPORT_JSON" in text and "RUN_REPORT_MD" in text, "环境读数没有第二条落点"
    assert '"checks": checks,' in text, "运行面应当原样落全量读数（否则两份报告互相缺项）"

    # 反证：把投影摘掉，同一把尺子必须翻红
    dropped = text.replace('"checks": reproducible_checks(checks),', '"checks": checks,', 1)
    assert dropped != text and '"checks": reproducible_checks(checks),' not in dropped


def test_environment_tier_readers_point_at_the_run_report() -> None:
    """消费面对齐：读 integration_* 的地方必须读环境读数那份文件。

    CI 的 "Docker-backed tiers really ran" 与 release.sh 的档位表都按 integration_*
    取数；提交面一旦不含这些键，它们会静默读到空集合然后"没有档不 PASS"判绿。
    """
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    release = (ROOT / "scripts" / "release.sh").read_text(encoding="utf-8")
    for label, text in (("ci.yml", ci), ("release.sh", release)):
        assert "VALIDATION_RUN.json" in text, f"{label} 还在从提交面读集成档读数"
        assert "integration_" in text, f"{label} 里没有档位判据（本判据会恒真）"
    # 反向对照：把 run 文件路径改掉，判据必须翻红
    assert "VALIDATION_RUN.json" not in ci.replace("VALIDATION_RUN.json", "VALIDATION.json")


def _build_requires(pyproject_text: str) -> list[str]:
    """`[build-system].requires` 的原始条目（一条正则太脆，按节解析）。"""
    import tomllib

    return tomllib.loads(pyproject_text)["build-system"]["requires"]


def test_build_gate_does_not_resolve_its_backend_from_the_network() -> None:
    """`build=PASS` 写进提交面，那构建后端的来源也必须是锁，不能是 PyPI 当日最新。

    实测缺口：`[build-system].requires = ["setuptools>=75"]`，而 `uv.lock` 里
    **根本没有 setuptools**（`importlib.util.find_spec("setuptools")` 在锁造的 venv 里
    读回 ABSENT）⇒ `python -m build` 的默认隔离环境每次都要联网解析 `>=75` 的上界，
    装到哪个版本由 PyPI 当天决定。这一格与 lint/typecheck 同性质：读数不是代码的函数。
    """
    validator = _load_validator()
    src = (ROOT / "scripts" / "validate_release.py").read_text(encoding="utf-8")
    assert '"-m", "build", "--no-isolation"' in src, (
        "validate 仍在隔离环境里现取构建后端：build=PASS 不是仓库代码的函数"
    )
    # 反证：摘掉旗标，同一把尺子必须翻红（而不是"看着像没变"）
    reverted = src.replace('"-m", "build", "--no-isolation"', '"-m", "build"')
    assert reverted != src and '"-m", "build", "--no-isolation"' not in reverted

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    requires = _build_requires(pyproject)
    assert requires, "build-system.requires 解析出来是空的，本判据会恒真"
    dev = validator.optional_extra(pyproject, "dev")
    for spec in requires:
        name = validator.require_name(spec)
        assert any(name == validator.require_name(entry) for entry in dev), (
            f"构建后端 {spec} 不在 dev extra 里：它没被 uv.lock 覆盖，`--no-isolation` 会当场 ImportError"
        )
    # 锁里必须真的有这个包，且锁住的版本满足 build-system 的区间
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    for spec in requires:
        name = validator.require_name(spec)
        version = validator.locked_version(lock, name)
        assert version, f"uv.lock 里没有 {name}：构建后端不在锁上"
        assert validator.spec_satisfied(spec, version), f"{name} 锁在 {version}，不满足 {spec}"


def test_make_build_and_the_release_gate_use_the_same_invocation() -> None:
    """`make build` 与 validate 的 build 步必须是同一种装法。

    真工件由 `make build` 产出（release.sh 第 4/5 步调它），validate 只是量一次；
    两处各写一份参数，漂移的永远是"被发布的那一份"——与 `make lint`/`make typecheck`
    那条既有判据同一个道理。
    """
    make = (ROOT / "Makefile").read_text(encoding="utf-8")
    src = (ROOT / "scripts" / "validate_release.py").read_text(encoding="utf-8")
    mk = re.search(r"^build:\n\t\$\(PYTHON\) -m build(.*)$", make, re.MULTILINE)
    assert mk, "Makefile 里 build 目标的形状变了，本判据读不到参数"
    call = re.search(r'\[PYTHON, "-m", "build"([^\]]*)\]', src)
    assert call, "validate 里的 build 调用形状变了"
    make_flags = {t for t in mk.group(1).split() if t.startswith("--")}
    gate_flags = set(re.findall(r'"(--[^"]+)"', call.group(1)))
    assert make_flags, "Makefile 的 build 参数集合为空（判据会恒真）"
    assert make_flags == gate_flags, f"make={sorted(make_flags)} gate={sorted(gate_flags)}"


def _build_wheel(outdir: Path, epoch: str) -> str:
    """真跑一次 `python -m build --no-isolation`，返回 wheel 的 sha256。"""
    import hashlib
    import subprocess
    import sys

    env = dict(os.environ, SOURCE_DATE_EPOCH=epoch)
    res = subprocess.run(  # noqa: S603 受控常量参数（本机 venv 的 build 模块）
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(outdir)],
        cwd=ROOT, text=True, capture_output=True, env=env, timeout=300,
    )
    assert res.returncode == 0, res.stdout[-400:] + res.stderr[-400:]
    wheels = sorted(outdir.glob("*.whl"))
    assert len(wheels) == 1, wheels
    return hashlib.sha256(wheels[0].read_bytes()).hexdigest()


def test_pinned_source_date_epoch_makes_the_wheel_recomputable(tmp_path) -> None:
    """同一份树、同一个 SOURCE_DATE_EPOCH ⇒ wheel 必须逐字节相同；不钉就得不同。

    动机是实测：`make build` 两次产出的 wheel sha256 不同（zip 条目带打包时刻），
    于是 `dist/checksums.txt` 里那行 sha 只能当"某一次构建的记录"，不能被第三方
    复算。钉住 `SOURCE_DATE_EPOCH` 之后 wheel 可复算（sdist 仍不可，见 CURRENT_STATE
    的 N-34：setuptools 的 sdist 不把生成条目 PKG-INFO/目录的 mtime 夹到该值）。
    """
    a = _build_wheel(tmp_path / "a", "1700000000")
    b = _build_wheel(tmp_path / "b", "1700000000")
    assert a == b, "钉了 epoch 两次构建还是不同 ⇒ 另有未夹住的时间源，本判据的前提不成立"

    # 必开对照：换一个 epoch，sha 必须变（否则上面那条相等是恒真）
    c = _build_wheel(tmp_path / "c", "1600000000")
    assert c != a, "换 epoch 后 wheel 没变 ⇒ 时间戳根本没进产物，上一条相等不证明任何事"


def test_build_call_sites_pin_the_epoch_in_makefile_and_the_gate() -> None:
    """两个构建调用点都得钉 epoch，且取值口径一致（与 lint/typecheck 目标那条同一手法）。"""
    make = (ROOT / "Makefile").read_text(encoding="utf-8")
    src = (ROOT / "scripts" / "validate_release.py").read_text(encoding="utf-8")
    assert "SOURCE_DATE_EPOCH ?= " in make, "Makefile 不再提供 SOURCE_DATE_EPOCH 缺省值"
    assert re.search(r"^export SOURCE_DATE_EPOCH$", make, re.MULTILINE), (
        "Makefile 定义了却没 export：build 那一步拿不到，产物仍带打包时刻"
    )
    assert make.count("%ct") == 1, f"Makefile 里 git 时间口径出现 {make.count('%ct')} 次，取值口径不唯一"
    assert make.index("SOURCE_DATE_EPOCH ?= ") < make.index("build:"), "export 必须早于 build 目标"
    # Python 侧的口径搬进了 scripts/build_env.py（validate 与探针共用一份实现）；
    # Makefile 那一条是另一种语言，只能按"两条命令逐字相同"对账——否则一边改成 %cI
    # （带时区的 ISO 时间）就再也对不上了，而两边都"看起来在钉时间"。
    assert "from build_env import epoch_env" in src and "epoch_env(" in src, (
        "validate 不再走共享时间口径：两条路径会各算各的秒"
    )
    helper = (ROOT / "scripts" / "build_env.py").read_text(encoding="utf-8")
    mk_cmd = re.search(r"\$\(shell git log (\S+) (\S+)", make)
    py_cmd = re.search(r'\["git", "log", "([^"]+)", "([^"]+)"\]', helper)
    assert mk_cmd and py_cmd, f"读不到两侧的命令：make={mk_cmd} python={py_cmd}"
    assert mk_cmd.groups() == py_cmd.groups(), (
        f"两侧口径不同：make {mk_cmd.groups()} vs python {py_cmd.groups()}"
    )
    # 反证：摘掉 export，判据必须翻红
    assert not re.search(r"^export SOURCE_DATE_EPOCH$", make.replace("export SOURCE_DATE_EPOCH\n", ""), re.MULTILINE)


STATE_FIXTURE = """| Gate | 结果 |
|---|---|
| Lint / Type | PASS（ruff 0 / mypy 46 files：`app` + `edge_agent`） |
| Migration | PASS（clean DB empty→head **14 文件链** + 模型↔迁移对账） |
| Integration PostgreSQL | **PASS**（自建一次性容器，真行锁语义） |
| Integration K8s（GPU 全流程） | PENDING（原因：需要节点带 nvidia.com/gpu 容量） |
"""


def test_state_page_gate_keeps_reproducible_numbers_and_bans_jittering_ones() -> None:
    """状态页那一节：可复现的数要**对上**，随环境抖的数要**不许手抄**。

    起因是实测：N-31 把环境读数从报告里逐出去了，但 `docs/CURRENT_STATE.md` 的 §1
    还在手抄 `Integration Docker **PASS 25/25**`，而本轮真实档位是 26/26（docker 档
    加过一次用例，没人去改那行）；`mypy 45 files` 同样过期（实测 46）。
    两类数要走两条路：代码决定的（mypy 文件数、迁移链长）纳进对账，
    环境决定的（各档执行了几支）根本不该出现在面上。
    """
    validator = _load_validator()
    fn = validator.state_row_offenders
    assert fn(STATE_FIXTURE, typecheck_files=46, migration_chain=14) == []
    # 反例 1：mypy 文件数过期
    bad = fn(STATE_FIXTURE.replace("mypy 46 files", "mypy 45 files"), typecheck_files=46, migration_chain=14)
    assert len(bad) == 1 and "mypy" in bad[0], bad
    # 反例 2：迁移链长过期
    bad = fn(STATE_FIXTURE.replace("**14 文件链**", "**13 文件链**"), typecheck_files=46, migration_chain=14)
    assert len(bad) == 1 and "Migration" in bad[0] and "13" in bad[0] and "14" in bad[0], bad
    # 反例 3：手抄环境读数
    bad = fn(
        STATE_FIXTURE.replace("**PASS**（自建一次性容器", "**PASS 21/21**（自建一次性容器"),
        typecheck_files=46, migration_chain=14,
    )
    assert bad and "环境读数" in bad[0] and "PostgreSQL" in bad[0], bad
    # 反例 4：被盯的行整行消失（覆盖面缩小）
    trimmed = "\n".join(ln for ln in STATE_FIXTURE.splitlines() if not ln.startswith("| Migration"))
    bad = fn(trimmed, typecheck_files=46, migration_chain=14)
    assert bad and "Migration" in bad[0], bad
    # 反例 5：一行都没有 ⇒ 判据与恒真同形
    assert fn("没有表\n", typecheck_files=46, migration_chain=14)


def test_the_status_page_agrees_with_the_committed_report() -> None:
    """真面对账：CURRENT_STATE 的 §1 必须与**提交面**里那两个可复现数一致。

    提交面只放代码决定的数，所以这条不会随环境抖（与 docs_test_counts 同一口径）；
    它防的是"报告改了、状态页没跟着改"。
    注意一条次序：pytest 看到的是**上一次**写出的报告，所以给提交面新增字段之后，
    第一次 `make validate` 必然红在这条上（字段还没进文件），重跑一次即收敛 —— 与
    `docs_test_counts` 把值对账放进 validate 而不是 pytest 是同一个道理。
    """
    validator = _load_validator()
    report = json.loads((ROOT / "docs" / "VALIDATION.json").read_text(encoding="utf-8"))
    checks = report["checks"]
    assert "files" in checks["typecheck"], "提交面没有 mypy 文件数：这条对账没有事实源"
    assert "chain" in checks["migration"], "提交面没有迁移链长：这条对账没有事实源"
    text = (ROOT / "docs" / "CURRENT_STATE.md").read_text(encoding="utf-8")
    offenders = validator.state_row_offenders(
        text, typecheck_files=checks["typecheck"]["files"], migration_chain=checks["migration"]["chain"]
    )
    assert offenders == [], offenders


def test_mypy_summary_parser_handles_both_forms() -> None:
    """从 mypy 的收口行取文件数：成功形与失败形都要能读，读不到要返回 None 而不是 0。"""
    validator = _load_validator()
    assert validator.mypy_source_files("Success: no issues found in 46 source files") == 46
    assert validator.mypy_source_files("Found 3 errors in 2 files (checked 46 source files)") == 46
    assert validator.mypy_source_files("everything else") is None


def test_validate_registers_the_state_gate_before_the_summary() -> None:
    """接线：`docs_state_rows` 必须在组装提交面之前算出来，否则它的 FAIL 进不了 overall。"""
    src = (ROOT / "scripts" / "validate_release.py").read_text(encoding="utf-8")
    assert "state_row_offenders(" in src and '"docs_state_rows"' in src
    assert src.index("state_row_offenders(") < src.index('"checks": reproducible_checks(checks)'), (
        "对账发生在组装提交面之后 → 它的红进不了这一轮的面"
    )


def test_pending_reasons_must_point_at_an_actionable_gap() -> None:
    """PENDING 的说明要落到"缺什么"，不许退化成把哨兵当原因、或一句没有指向的话。

    档位干净跳过是 N-31 定下的形状，但"跳过"本身不是终点：读的人要能照着原因去补环境。
    原因文本都是代码里的常量（skip 文案），所以这条判据的结论不随环境变——
    健康时没有 PENDING 档 = 无可核 = PASS；跳了档就必须说清缺项。
    """
    validator = _load_validator()
    fn = validator.pending_reason_offenders
    good = {
        "integration_postgres": {
            "status": "PENDING",
            "note": "整档 19 用例未执行，原因：缺驱动：pip install -e .[postgres]"
                    "（哨兵 POSTGRES_VALIDATION_PENDING）",
        },
        "integration_docker": {"status": "PASS", "note": "26/26 用例在真实后端上执行"},
    }
    assert fn(good) == []
    # 只有哨兵、没有原因
    sentinel_only = {
        "integration_x": {
            "status": "PENDING",
            "note": "整档 3 用例未执行，原因：X_PENDING（哨兵 X_PENDING）",
        }
    }
    assert len(fn(sentinel_only)) == 1, fn(sentinel_only)
    # 有原因但不指向任何缺项
    vague = {
        "integration_x": {
            "status": "PENDING",
            "note": "整档 3 用例未执行，原因：今天不太方便（哨兵 X_PENDING）",
        }
    }
    assert len(fn(vague)) == 1, fn(vague)
    # 空 note 不算"通过"
    assert fn({"integration_x": {"status": "PENDING", "note": ""}})
    # 反向对照：全部 PASS 时无可核 = 不报错
    assert fn({"integration_x": {"status": "PASS", "note": "3/3 用例在真实后端上执行"}}) == []
    # "可行动"允许两种成立方式：说出缺什么，或给一条能跑的动作（后者由 N-49 半挂剧本逼出来）
    action_only = {
        "integration_x": {
            "status": "PENDING",
            "note": "整档 3 用例未执行，原因：先 docker pull 任一候选镜像（哨兵 X_PENDING）",
        }
    }
    assert fn(action_only) == [], fn(action_only)
    neither = {
        "integration_x": {
            "status": "PENDING",
            "note": "整档 3 用例未执行，原因：镜像不在，环境问题（哨兵 X_PENDING）",
        }
    }
    assert len(fn(neither)) == 1, fn(neither)


def test_pending_reason_gate_is_wired_into_the_summary() -> None:
    """接线：`pending_reasons` 必须在组装提交面之前算出来，否则它的红进不了这一轮的面。"""
    src = (ROOT / "scripts" / "validate_release.py").read_text(encoding="utf-8")
    assert "pending_reason_offenders(" in src and '"pending_reasons"' in src
    assert src.index("pending_reason_offenders(") < src.index('"checks": reproducible_checks(checks)'), (
        "对账发生在组装提交面之后 → 它的 FAIL 改不了 overall"
    )


def test_dangling_reference_check_catches_all_three_kinds() -> None:
    """纯函数层：三种失效引用都要点名，且"没引用任何东西"不能算通过。

    动因是一条元事实：我在写这条判据之前"记得"的 k8s 开关名比仓里真正被读的那个多了一个字母，
    而全仓根本没有这个名字（`grep` 零命中）——人（和我）就是会记错这类指针，
    所以"可行动的原因"要核到引用的东西真的存在，而不只是含一个"缺"字。
    """
    validator = _load_validator()
    catalog = {
        "env": {"EMBODIEDCLOUD_PG_IMAGE", "EMBODIEDCLOUD_KIND_BIN"},
        "make": {"test-pg", "control-image"},
        "extra": {"postgres", "s3"},
    }
    texts = {
        "tests/pg_server.py": 'return "缺驱动：pip install -e \".[postgres]\"；或镜像未缓存 EMBODIEDCLOUD_PG_IMAGE"',
        "docs/OPERATIONS.md": "先 `make test-pg`；kind 二进制用 EMBODIEDCLOUD_KIND_BIN 指",
    }
    assert validator.dangling_reference_offenders(texts, catalog) == []
    bad = dict(texts)
    bad["docs/OPERATIONS.md"] = (
        "先 `make test-pgg`；export EMBODIEDCLOUD_NOT_A_REAL_SWITCH=1；pip install -e .[postgre]"
    )
    offenders = validator.dangling_reference_offenders(bad, catalog)
    kinds = " ".join(offenders)
    assert len(offenders) == 3, offenders
    assert "make test-pgg" in kinds and "EMBODIEDCLOUD_NOT_A_REAL_SWITCH" in kinds and ".[postgre]" in kinds, offenders
    # 空目录 / 零引用都不能被读成"通过"
    assert validator.dangling_reference_offenders(texts, {"env": set(), "make": set(), "extra": set()})
    assert validator.dangling_reference_offenders({"x.md": "这里什么指针都没写"}, catalog)


def test_the_repo_has_no_dangling_operational_pointers() -> None:
    """真面：文档与档位前置文案里的 env / make 目标 / extra，全部要能在仓内找到出处。

    目录来自代码本身（Settings 字段 + `env_prefix`、`os.environ` 读点名、app/edge_agent 里的字面量、
    Makefile 目标、pyproject 的 optional extras），所以这条不依赖环境。
    """
    validator = _load_validator()
    catalog = validator.reference_catalog()
    for kind in ("env", "make", "extra"):
        assert catalog[kind], f"目录的 {kind} 一侧为空：判据会恒真"
    offenders = validator.dangling_reference_offenders(validator.pointer_bearing_texts(), catalog)
    assert offenders == [], offenders
    scanned = validator.pointer_bearing_texts()
    cited = sum(1 for text in scanned.values() if "EMBODIEDCLOUD_" in text or "make " in text)
    assert cited >= 5, f"只扫到 {cited} 份含操作指针的文本，覆盖面可疑"


def test_reference_gate_is_wired_into_the_summary() -> None:
    src = (ROOT / "scripts" / "validate_release.py").read_text(encoding="utf-8")
    assert "dangling_reference_offenders(" in src and '"reason_references"' in src
    assert src.index("dangling_reference_offenders(") < src.index('"checks": reproducible_checks(checks)'), (
        "对账发生在组装提交面之后 → 它的 FAIL 改不了 overall"
    )

def test_generated_reports_are_not_scanned_for_pointers() -> None:
    """生成物不能当输入：报告会把上一轮的 FAIL 原因原样抄回，扫它就是让判据自我喂养、收敛不了。

    这条真实发生过：门禁行里的占位写法被点名后，失效原因被写进 `docs/VALIDATION.md`，
    下一次扫描又在报告里「发现」同一批假指针 —— 红一次不算错，但这样收敛不了。
    """
    validator = _load_validator()
    scanned = validator.pointer_bearing_texts()
    assert "docs/VALIDATION.md" not in scanned, "生成报告被扫了 ⇒ 上一条偏离会被自我复述喂养"
    assert validator.GENERATED_DOCS, "排除表为空：排除逻辑没有事实依据"
    assert "docs/OPERATIONS.md" in scanned and any(k.startswith("tests/") for k in scanned), scanned.keys()


def test_the_env_catalog_counts_reads_not_mentions() -> None:
    """收紧目录：`EMBODIEDCLOUD_*` 只有**被读**才算出处；注释里提一句不算，常量间接读要算。

    动因是这条判据上线后抓到的第一个真错，就是我自己的错：仓里真正读的 k8s 开关是单数形式，
    而我写进注释与测试文档里的是复数形式——差一个字母的指针正是这类判据要抓的形状。
    """
    validator = _load_validator()
    sources = {
        "a.py": (
            "import os\n"
            "# 注释里提一句 EMBODIEDCLOUD_ONLY_MENTIONED 不算出处\n"
            'VIA_CONST = "EMBODIEDCLOUD_READ_VIA_CONST"\n'
            'VALUE = os.environ.get("EMBODIEDCLOUD_READ_DIRECT")\n'
            "OTHER = os.environ[VIA_CONST]\n"
        ),
        "b.py": 'import os\nX = os.getenv("EMBODIEDCLOUD_READ_GETENV")\n',
    }
    read = validator.env_names_read_by(sources)
    assert read == {
        "EMBODIEDCLOUD_READ_VIA_CONST",
        "EMBODIEDCLOUD_READ_DIRECT",
        "EMBODIEDCLOUD_READ_GETENV",
    }, read
    assert "EMBODIEDCLOUD_ONLY_MENTIONED" not in read, "注释里的名字被当成出处：目录太宽"
    # 收紧之后仍要覆盖仓内真实被读的开关，否则是调瞎不是调准
    catalog = validator.reference_catalog()["env"]
    for name in ("EMBODIEDCLOUD_PG_IMAGE", "EMBODIEDCLOUD_KIND_BIN", "EMBODIEDCLOUD_EDGE_TOKEN"):
        assert name in catalog, f"真被读的开关掉出目录：{name}"

def test_env_read_resolution_is_transitive_but_not_fooled_by_cycles() -> None:
    """常量间接读要能走多跳；两个常量互相指对方时不许死循环、也不许凭空造出处。

    上一版只解一跳（`CONST = "名字"`），我把这条边界写进了未证实。
    现在按传递闭包解，并显式验证：多跳能认、环不会卡、指不到字面量的名字不算出处。
    """
    validator = _load_validator()
    sources = {
        "two_hop.py": (
            "import os\n"
            'HOP1 = "EMBODIEDCLOUD_TWO_HOP"\n'
            "HOP2 = HOP1\n"
            "VALUE = os.environ.get(HOP2)\n"
        ),
        "cycle.py": (
            "import os\n"
            "LOOP_A = LOOP_B\n"
            "LOOP_B = LOOP_A\n"
            'os.environ.get(LOOP_A)\n'
            'os.environ.get("EMBODIEDCLOUD_AFTER_CYCLE")\n'
        ),
    }
    read = validator.env_names_read_by(sources)
    assert "EMBODIEDCLOUD_TWO_HOP" in read, "两跳间接读没认出来"
    assert "EMBODIEDCLOUD_AFTER_CYCLE" in read, "环旁边的正常读点被带没了"
    assert len([n for n in read if n.startswith("EMBODIEDCLOUD_LOOP")]) == 0, read


def test_rooted_path_pointers_are_checked_against_the_same_roots() -> None:
    """限定在仓内根目录下的路径引用要核存在性；而且索引必须与 roots 同源。

    这条判据的第一版把文件索引写死成一份目录清单，漏了两个真实存在的顶层目录，
    第一次跑就把 11 条**存在**的路径报成悬空 —— 分母与划界不同源时，门禁只会自造假阳性。
    """
    from pathlib import Path as _P

    validator = _load_validator()
    texts = {
        "docs/A.md": (
            "入口脚本见 runtime/real-entry.sh，配置见 deploy/kubernetes/exists.yaml；"
            "示例路径 tmp/probe.py 与通配 docs/adr/*.md 不算指针；写歪的是 scripts/gone.py"
        )
    }
    offenders = validator.doc_reference_offenders(
        texts,
        {"docs/A.md": 3},
        {},
        roots=("deploy", "runtime", "scripts"),
        repo_files=frozenset({"runtime/real-entry.sh", "deploy/kubernetes/exists.yaml", "docs/A.md"}),
    )
    assert offenders == ["docs/A.md: 引用了仓里不存在的路径 scripts/gone.py"], offenders

    index = validator._repo_file_index()
    for root in validator._doc_roots():
        sample = next(
            (f for f in sorted((_P(".") / root).rglob("*")) if f.is_file() and "__pycache__" not in str(f)),
            None,
        )
        if sample is not None:
            rel = sample.as_posix().removeprefix("./")
            assert rel in index, f"根目录 {root} 里的 {rel} 不在索引里：分母比划界窄"
    stats = validator.doc_reference_stats()
    assert stats["paths"] >= 200, f"路径指针分母太小，这条核起来近乎空转：{stats}"


def test_rooted_globs_must_match_at_least_one_file() -> None:
    """通配写法也算指针——但它声称"这类文件在这里"，所以一个都匹配不到就是空目录声称。

    两个极性都要有：匹配到时不许误报；字符类只认 ASCII 路径字符，
    否则中文会被 `\\w` 这类字符类吞进 glob（普查时就造出过一条假 glob）。
    """
    validator = _load_validator()
    files = frozenset({"docs/adr/0001-a.md", "docs/adr/0002-b.md", "docs/A.md"})
    ok = {"docs/X.md": "决策记录在 docs/adr/*.md；这一句后面跟着中文，runtime/账五项全不动 不是路径"}
    offenders = validator.doc_reference_offenders(
        ok, {"docs/X.md": 3}, {}, roots=("docs", "runtime"), repo_files=files
    )
    assert offenders == [], offenders
    bad = {"docs/X.md": "清单在 deploy/manifests/*.yaml"}
    offenders = validator.doc_reference_offenders(bad, {"docs/X.md": 3}, {}, roots=("deploy",), repo_files=files)
    assert offenders == ["docs/X.md: 通配 deploy/manifests/*.yaml 在仓里一个文件都匹配不到（空目录声称）"], offenders


def test_the_repo_docs_have_no_dangling_pointers() -> None:
    """真面：仓内文档现在 0 条悬空指针（先普查确认形状可信，才立的门）。"""
    validator = _load_validator()
    assert validator.dangling_doc_reference_offenders() == []
    stats = validator.doc_reference_stats()
    assert stats["fileline"] >= 50 and stats["anchors"] >= 3 and stats["paths"] >= 400, stats


def test_document_pointers_resolve_to_real_lines_and_sections() -> None:
    """`文件:行号` 与 `宿主.md §节` 两类指针逐条核，两类恒真形状也算偏离。"""
    validator = _load_validator()
    existing = {"app/x.py": 2, "docs/B.md": 9, "scripts/tool.py": 40}
    sections = {"B.md": {"1", "2", "2.1"}, "ARCHITECTURE.md": {"7", "8"}}
    good = {"docs/A.md": "见 scripts/tool.py:12 与 app/x.py:2；另见 B.md §2.1、ARCHITECTURE.md §8"}
    assert validator.doc_reference_offenders(good, existing, sections) == []
    bad = {"docs/A.md": "见 app/x.py:3；再看 docs/B.md §7；还有 C.md §1；最后是 nope.py:4"}
    offenders = validator.doc_reference_offenders(bad, existing, sections)
    joined = "\n".join(offenders)
    assert len(offenders) == 4, offenders
    assert "app/x.py:3 超出该文件长度 2 行" in joined, offenders
    assert "docs/B.md §7" in joined and "C.md" in joined and "nope.py" in joined, offenders
    assert validator.doc_reference_offenders({}, existing, sections)
    assert validator.doc_reference_offenders({"docs/A.md": "没有任何指针"}, existing, sections)


def test_doc_reference_gate_is_wired_into_the_summary() -> None:
    src = (ROOT / "scripts" / "validate_release.py").read_text(encoding="utf-8")
    assert "dangling_doc_reference_offenders(" in src and '"doc_references"' in src
    assert src.index("dangling_doc_reference_offenders(") < src.index('"checks": reproducible_checks(checks)')
    body = src[src.index("def doc_reference_offenders") : src.index("def dangling_doc_reference_offenders")]
    assert "不做" in body, "判据的边界（哪些路径有意不核）必须写在函数里，防止将来悄悄扩面"
