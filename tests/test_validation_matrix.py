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

