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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_validator():
    path = ROOT / "scripts" / "validate_release.py"
    spec = importlib.util.spec_from_file_location("validate_release", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    assert universe["tests.test_gpu_pool_guard"] == "GPU_POOL_VALIDATION_PENDING"
    assert all(m.startswith("tests.") for m in universe), universe


def _checks(**over) -> dict:
    """一份"形状与 validate 产出一致"的最小报告夹具。"""
    checks = {
        "test_collected": {"status": "PASS", "count": 559},
        "test_run": {"status": "PASS", "passed": 557, "skipped": 2, "failed": 0,
                     "failed_names": [], "skipped_names": ["a::b", "c::d"]},
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
