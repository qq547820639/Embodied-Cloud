"""版本单一来源（§15）：pyproject.toml 是事实源，其余动态派生/发布替换。"""

import re
from pathlib import Path


def _pyproject_version() -> str:
    text = Path("pyproject.toml").read_text()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert m, "pyproject.toml 缺少 version"
    return m.group(1)


def test_pyproject_is_version_source_of_truth():
    assert _pyproject_version() == "0.6.0"


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
