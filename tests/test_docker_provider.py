from pathlib import Path
from subprocess import CompletedProcess

import pytest

from app.config import Settings
from app.models import Template, Workspace
from app.services.providers.docker import DockerProvider


class FakeDockerProvider(DockerProvider):
    def __init__(self, settings):
        super().__init__(settings)
        self.commands: list[list[str]] = []

    def health(self):
        return True, "fake docker + gpu"

    def _choose_gpu(self):
        return 0, "Fake RTX"

    def _streaming_workspace_running(self):
        return False

    def _run(self, args, *, check=True):
        self.commands.append(args)
        return CompletedProcess(args=args, returncode=0, stdout="container-id\n", stderr="")


def make_template(streaming: bool) -> Template:
    return Template(
        id="test-template",
        name="Test",
        description="test",
        category="test",
        runtime="isaaclab",
        launch_command="echo ok",
        requires_streaming=streaming,
        recommended_vram_gb=16,
        estimated_hourly_cost_cny=1.0,
        enabled=True,
    )


def make_workspace() -> Workspace:
    return Workspace(
        id="11111111-2222-3333-4444-555555555555",
        name="Test Workspace",
        template_id="test-template",
        provider="docker",
        status="queued",
    )


def test_streaming_provider_uses_isaac_lab_env_and_fixed_ports(tmp_path, monkeypatch):
    settings = Settings(
        eula_accepted=True,
        host_public_ip="10.0.0.8",
        workspace_root=tmp_path,
        ide_port_start=38100,
        ide_port_end=38120,
    )
    provider = FakeDockerProvider(settings)
    monkeypatch.setattr("app.services.providers.docker.is_port_free", lambda *a, **kw: True)
    monkeypatch.setattr("app.services.providers.docker.allocate_tcp_port", lambda *a, **kw: 38101)

    result = provider.provision(make_workspace(), make_template(True), tmp_path / "ws")

    assert result.signal_port == 49100
    assert result.media_port == 47998
    assert result.ide_port == 38101
    run = provider.commands[-1]
    joined = " ".join(run)
    assert "LIVESTREAM=1" in joined
    assert "PUBLIC_IP=10.0.0.8" in joined
    assert "embodiedcloud.streaming=1" in joined
    assert "ISAACSIM_SIGNAL_PORT" not in joined
    assert "ISAACSIM_STREAM_PORT" not in joined


def test_eula_is_required(tmp_path):
    settings = Settings(eula_accepted=False, workspace_root=tmp_path)
    provider = FakeDockerProvider(settings)
    with pytest.raises(RuntimeError, match="EULA"):
        provider.provision(make_workspace(), make_template(False), tmp_path / "ws")
