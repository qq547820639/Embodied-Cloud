"""版本单一来源（§15）：pyproject.toml 是事实源，其余动态派生/发布替换。"""

import re
from pathlib import Path


def _pyproject_version() -> str:
    text = Path("pyproject.toml").read_text()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert m, "pyproject.toml 缺少 version"
    return m.group(1)


def test_pyproject_is_version_source_of_truth():
    assert _pyproject_version() == "0.7.0"


def _load_validator():
    """按路径加载 scripts/validate_release.py：计数串的解析只有那一份实现。"""
    import importlib.util

    path = Path("scripts/validate_release.py").resolve()
    spec = importlib.util.spec_from_file_location("validate_release", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_docs_carry_exactly_one_test_count_token() -> None:
    """形状判据：CHANGELOG 当前版本节与 CURRENT_STATE 各写且**只写一次**计数串。

    数值对账不在这里，而在 scripts/validate_release.py：pytest 阶段读到的
    docs/VALIDATION.json 必然是**上一次**产物，把值比较放这儿会造出不收敛的自引用
    ——本轮真实踩过：新加一条判据用例后，报告说 failed=1 而文档说 failed=0，
    而那个 failed 正是本判据自己报的，连跑两次 validate 都不收敛。
    """
    validator = _load_validator()
    version = _pyproject_version()
    blocks = {
        "CHANGELOG 当前版本节": validator.changelog_section(
            Path("CHANGELOG.md").read_text(encoding="utf-8"), version
        ),
        "docs/CURRENT_STATE.md": Path("docs/CURRENT_STATE.md").read_text(encoding="utf-8"),
    }
    for name, block in blocks.items():
        found = validator.doc_count_tokens(block)
        assert found, f"{name} 里没有 'collected N / passed N / skipped N / failed N' 计数串"
        assert len(found) == 1, f"{name} 出现 {len(found)} 处计数串，只允许 1 处（多处必有一处会过期）"


def test_doc_count_reconciliation_can_fire() -> None:
    """开火对照：数字不符时值对账必须逐处点名，而不是安静地返回空。"""
    validator = _load_validator()
    checks = {
        "test_collected": {"count": 999999},
        "test_run": {"passed": 888888, "skipped": 777777, "failed": 666666},
    }
    offenders = validator.docs_counts_discrepancies(_pyproject_version(), checks)
    assert len(offenders) == 2, offenders
    assert all("实测" in line for line in offenders), offenders


def test_lint_and_type_targets_are_the_same_in_makefile_and_the_release_gate() -> None:
    """`make lint`/`make typecheck` 与 `make validate` 必须量同一批目标。

    本轮新增 `edge_agent/` 包时要同时改两处，漏一处就会出现"开发者跑的门禁比
    发布门禁宽"——而且新代码恰好是没人量的那份。判据按目标集合比对，
    并钉住 edge_agent 在册（否则两遍空集合也能互相相等）。
    """
    make = Path("Makefile").read_text(encoding="utf-8")
    src = Path("scripts/validate_release.py").read_text(encoding="utf-8")
    for tool in ("ruff", "mypy"):
        mk = re.search(rf"^\t\$\(PYTHON\) -m {tool} (.+)$", make, re.MULTILINE)
        gate = re.search(rf'"-m", "{tool}"((?:, "[a-z_]+")+)', src)
        assert mk and gate, f"{tool}: 两处之一找不到调用行"
        make_targets = set(mk.group(1).split()) - {"check"}
        gate_targets = set(re.findall(r'"([a-z_]+)"', gate.group(1))) - {"check"}
        assert make_targets, f"{tool}: Makefile 目标集合为空（判据会恒真）"
        assert "edge_agent" in make_targets, make_targets
        assert make_targets == gate_targets, f"{tool}: make={sorted(make_targets)} gate={sorted(gate_targets)}"


def test_report_carries_the_names_of_failing_cases(tmp_path) -> None:
    """一次偶发失败必须可归因：计数之外还要带出**是哪条**。

    本轮真实教训：`make validate` 把 pytest 的输出丢弃（`code, _ = run(...)`），
    报告只剩 "failed: 1"，那条用例从此无法追查——重跑两次都不再红。名字取自
    JUnit 的结构化属性，不是日志文本（日志里没有稳定的用例名可解析）。
    """
    validator = _load_validator()

    def counts_of(body: str) -> dict:
        report = tmp_path / "junit.xml"
        report.write_text(body, encoding="utf-8")
        return validator.count_tests_junit(report)

    fired = counts_of(
        '<testsuites><testsuite name="pytest" tests="4" skipped="1" failures="1" errors="1">'
        '<testcase classname="tests.a" name="ok"/>'
        '<testcase classname="tests.a" name="boom"><failure message="x">trace</failure></testcase>'
        '<testcase classname="tests.b" name="crashed"><error message="e">trace</error></testcase>'
        '<testcase classname="tests.c" name="pending"><skipped message="why"/></testcase>'
        "</testsuite></testsuites>"
    )
    assert fired["failed"] == 2, fired
    assert fired["failed_names"] == ["tests.a::boom", "tests.b::crashed"], fired

    # 反向对照：全绿报告不得凭空造出名字，否则这条判据只是"字段存在"而非"有牙"。
    clean = counts_of(
        '<testsuites><testsuite name="pytest" tests="2" skipped="1" failures="0" errors="0">'
        '<testcase classname="tests.a" name="ok"/>'
        '<testcase classname="tests.c" name="pending"><skipped message="why"/></testcase>'
        "</testsuite></testsuites>"
    )
    assert clean["failed"] == 0 and clean["failed_names"] == [], clean


def test_release_script_owns_the_value_reconciliation() -> None:
    """判据不许在"搬家"中丢失：值对账确实接在 validate 汇总之前。"""
    source = Path("scripts/validate_release.py").read_text(encoding="utf-8")
    assert "docs_test_counts" in source, "docs 计数串 ↔ 实测的对账判据被删了"
    assert source.index("docs_counts_discrepancies(") < source.index("software_failed = any("), (
        "对账必须发生在汇总之前，否则它的 FAIL 进不了 overall"
    )


def test_app_init_version_matches_pyproject():
    import app

    assert app.__version__ == _pyproject_version()


def test_makefile_version_matches_pyproject():
    text = Path("Makefile").read_text()
    assert f"VERSION ?= {_pyproject_version()}" in text


def test_static_ui_version_dynamic():
    """静态 UI 不再硬编码版本：由 /api/health 动态更新（pill id 存在）。"""
    html = Path("app/static/index.html").read_text()
    assert 'id="version-pill"' in html
    js = Path("app/static/app.js").read_text()
    assert "h.version" in js  # loadHealth 更新版本 pill


def test_k8s_manifest_version_matches():
    yaml_text = Path("deploy/kubernetes/control-plane.yaml").read_text()
    assert f"control-plane:{_pyproject_version()}" in yaml_text


def test_openapi_version_matches():
    import json

    spec = json.loads(Path("docs/openapi.json").read_text())
    assert spec["info"]["version"] == _pyproject_version()


def _locked_project_version(lock_text: str) -> str | None:
    """从 uv.lock 里取本项目自己的版本（不能按行找：每个包都有 version 行）。"""
    for block in lock_text.split("[[package]]"):
        if 'name = "embodiedcloud"' not in block:
            continue
        for line in block.splitlines():
            if line.startswith("version = "):
                return line.split("=", 1)[1].strip().strip('"')
    return None


def test_lockfile_version_matches_pyproject():
    """uv.lock 也是版本面的一部分。

    只 bump pyproject 不重跑 `uv lock` 时，`uv lock --check` 会在 release 第 4.1 步
    才红 —— 那已经是发布链后半段；这条把它提前到 `make test`。
    """
    version = _pyproject_version()
    assert _locked_project_version(Path("uv.lock").read_text()) == version


def test_pod_labels_hit_network_policy_selector():
    """§14：provider 创建的 Pod labels 必须命中 NetworkPolicy selector
    （embodiedcloud.workspace: true），禁止假安全配置。"""
    import re

    from app.config import Settings
    from app.services.providers.k8s import KubernetesProvider
    from tests.k8s_fakes import make_fake_models

    calls: list = []

    class Recorder:
        def CoreV1Api(self):
            return self

        def AppsV1Api(self):
            return self

        def create_namespaced_deployment(self, namespace, body, **kwargs):
            calls.append(body)
            return body

        def create_namespaced_persistent_volume_claim(self, namespace, body, **kwargs):
            return body

        def create_namespaced_service(self, namespace, body, **kwargs):
            return body

    from app.models import Template, Workspace
    from app.services.providers.base import ResourceReservation

    provider = KubernetesProvider(
        Settings(eula_accepted=True, k8s_namespace="embodiedcloud"),
        _client=Recorder(),
        model_factory=make_fake_models,
    )
    ws = Workspace(id="w1", name="w", template_id="t", provider="k8s", status="queued")
    template = Template(
        id="t", slug="t", name="t", description="d", category="c", runtime="isaaclab",
        launch_command="", enabled=True, recommended_vram_gb=16, estimated_hourly_cost_cny=1.0,
    )
    reservation = ResourceReservation(
        host_id="k8s-node-n1", gpu_id="g1", gpu_uuid="u1", gpu_index=0, node_name="n1"
    )
    provider.provision(ws, template, Path("/tmp/test-labels"), reservation)  # noqa: S108
    pod_labels = calls[0].spec.template.metadata.labels
    assert pod_labels.get("embodiedcloud.workspace") == "true", "Pod 缺少 NetworkPolicy 命中 label"

    # NetworkPolicy selector 使用同一 label 值
    np_text = Path("deploy/kubernetes/workspace-network-policy.yaml").read_text()
    assert re.search(r'embodiedcloud\.workspace:\s*"true"', np_text)
