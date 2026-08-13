from pathlib import Path
from subprocess import CompletedProcess

import pytest

from app.config import Settings
from app.models import Template, Workspace
from app.services.providers.base import ResourceReservation
from app.services.providers.docker import DockerProvider


class FakeDockerProvider(DockerProvider):
    def __init__(self, settings):
        super().__init__(settings)
        self.commands: list[list[str]] = []

    def health(self):
        return True, "fake docker + gpu"

    def _streaming_workspace_running(self):
        return False

    def _run(self, args, *, check=True):
        self.commands.append(args)
        return CompletedProcess(args=args, returncode=0, stdout="container-id\n", stderr="")


def make_template(streaming: bool, image: str | None = None) -> Template:
    return Template(
        id="test-template",
        name="Test",
        description="test",
        category="test",
        runtime="isaaclab",
        image=image,
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


def make_reservation(gpu_index: int = 3, gpu_id: str = "gpu-abc") -> ResourceReservation:
    return ResourceReservation(
        host_id="docker-host-0001",
        gpu_id=gpu_id,
        gpu_uuid=f"GPU-{gpu_id}-uuid",
        gpu_index=gpu_index,
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

    result = provider.provision(make_workspace(), make_template(True), tmp_path / "ws", make_reservation())

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
        provider.provision(make_workspace(), make_template(False), tmp_path / "ws", make_reservation())


def test_provision_requires_reservation(tmp_path):
    """DockerProvider 必须拒绝没有 reservation 的 provision —— 不允许自行选择 GPU。"""
    settings = Settings(eula_accepted=True, workspace_root=tmp_path)
    provider = FakeDockerProvider(settings)
    with pytest.raises(RuntimeError, match="ResourceReservation"):
        provider.provision(make_workspace(), make_template(False), tmp_path / "ws", None)


def test_docker_run_gpu_device_matches_reservation(tmp_path, monkeypatch):
    """--gpus device=N 与 scheduler reservation.gpu_index 完全一致。"""
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

    for index in (0, 2, 5):
        result = provider.provision(
            make_workspace(), make_template(False), tmp_path / "ws", make_reservation(gpu_index=index)
        )
        assert result is not None
        run = provider.commands[-1]
        joined = " ".join(run)
        assert f"--gpus device={index}" in joined
        assert f"embodiedcloud.gpu={index}" in joined


def test_docker_run_uses_template_image(tmp_path, monkeypatch):
    """runtime 镜像由 template.image 决定，而不是 settings.workspace_image。"""
    settings = Settings(
        eula_accepted=True,
        workspace_root=tmp_path,
        ide_port_start=38100,
        ide_port_end=38120,
        workspace_image="embodiedcloud/default:fallback",
    )
    provider = FakeDockerProvider(settings)
    monkeypatch.setattr("app.services.providers.docker.is_port_free", lambda *a, **kw: True)
    monkeypatch.setattr("app.services.providers.docker.allocate_tcp_port", lambda *a, **kw: 38101)

    # Template A 与 Template B 使用不同镜像
    provider.provision(
        make_workspace(), make_template(False, image="registry/template-a@sha256:aaaa"), tmp_path / "ws",
        make_reservation(gpu_index=0),
    )
    run_a = provider.commands[-1]
    assert "registry/template-a@sha256:aaaa" in " ".join(run_a)
    assert "embodiedcloud/default:fallback" not in " ".join(run_a)

    provider.provision(
        make_workspace(), make_template(False, image="registry/template-b@sha256:bbbb"), tmp_path / "ws",
        make_reservation(gpu_index=0),
    )
    run_b = provider.commands[-1]
    assert "registry/template-b@sha256:bbbb" in " ".join(run_b)

    # 两个模板镜像必须不同且正确
    joined_a, joined_b = " ".join(run_a), " ".join(run_b)
    assert "template-a" in joined_a and "template-b" in joined_b
    assert joined_a != joined_b


def test_docker_run_fallback_image_when_template_has_none(tmp_path, monkeypatch):
    """模板未配置镜像时允许 fallback 到平台默认镜像（显式配置的 fallback）。"""
    settings = Settings(
        eula_accepted=True,
        workspace_root=tmp_path,
        ide_port_start=38100,
        ide_port_end=38120,
        workspace_image="embodiedcloud/default-image:0.5.0",
    )
    provider = FakeDockerProvider(settings)
    monkeypatch.setattr("app.services.providers.docker.is_port_free", lambda *a, **kw: True)
    monkeypatch.setattr("app.services.providers.docker.allocate_tcp_port", lambda *a, **kw: 38101)

    provider.provision(
        make_workspace(), make_template(False, image=None), tmp_path / "ws", make_reservation(gpu_index=0)
    )
    joined = " ".join(provider.commands[-1])
    assert "embodiedcloud/default-image:0.5.0" in joined


def test_destroy_idempotent_when_no_such_container(monkeypatch, tmp_path):
    """docker rm 返回「No such container」→ 幂等成功，不抛错。"""
    settings = Settings(eula_accepted=True, workspace_root=tmp_path)
    provider = DockerProvider(settings)
    ws = make_workspace()
    ws.container_name = "ec-test"

    def fake_run(args, *, check=True):
        if args[:2] == ["docker", "rm"]:
            return CompletedProcess(
                args=args, returncode=1, stdout="", stderr="Error: No such container: ec-test"
            )
        return CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(provider, "_run", fake_run)
    provider.destroy(ws)  # 不应抛错


def test_destroy_idempotent_when_container_absent_by_inspection(monkeypatch, tmp_path):
    """docker rm 报错文案不含「No such container」，但 inspect 判定容器不存在 → 幂等成功。"""
    settings = Settings(eula_accepted=True, workspace_root=tmp_path)
    provider = DockerProvider(settings)
    ws = make_workspace()
    ws.container_name = "ec-test"

    def fake_run(args, *, check=True):
        if args[:2] == ["docker", "rm"]:
            return CompletedProcess(
                args=args, returncode=1, stdout="", stderr="Error: container already gone"
            )
        if args[:2] == ["docker", "inspect"]:
            return CompletedProcess(
                args=args, returncode=1, stdout="", stderr="Error: No such object: ec-test"
            )
        return CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(provider, "_run", fake_run)
    provider.destroy(ws)  # 不应抛错


def test_destroy_raises_when_container_still_exists(monkeypatch, tmp_path):
    """docker rm 失败且 inspect 判定容器仍存在 → 上抛 RuntimeError，不吞错。"""
    settings = Settings(eula_accepted=True, workspace_root=tmp_path)
    provider = DockerProvider(settings)
    ws = make_workspace()
    ws.container_name = "ec-test"

    def fake_run(args, *, check=True):
        if args[:2] == ["docker", "rm"]:
            return CompletedProcess(
                args=args, returncode=1, stdout="", stderr="Error: removal in progress"
            )
        if args[:2] == ["docker", "inspect"]:
            # 容器仍存在 → 非幂等，必须上抛
            return CompletedProcess(args=args, returncode=0, stdout="container-id\n", stderr="")
        return CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(provider, "_run", fake_run)
    with pytest.raises(RuntimeError, match="docker command failed"):
        provider.destroy(ws)


def test_destroy_raises_when_daemon_unavailable(monkeypatch, tmp_path):
    """docker rm 失败且 inspect 也失败（daemon 不可用，无法确认 absent）→ 上抛，不吞错。"""
    settings = Settings(eula_accepted=True, workspace_root=tmp_path)
    provider = DockerProvider(settings)
    ws = make_workspace()
    ws.container_name = "ec-test"

    def fake_run(args, *, check=True):
        if args[:2] in (["docker", "rm"], ["docker", "inspect"]):
            return CompletedProcess(
                args=args, returncode=1, stdout="", stderr="Cannot connect to the Docker daemon"
            )
        return CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(provider, "_run", fake_run)
    with pytest.raises(RuntimeError, match="docker command failed"):
        provider.destroy(ws)


def test_destroy_releases_port_even_when_run_raises(monkeypatch, tmp_path):
    """docker daemon 不可用导致 destroy 上抛时，finally 仍释放登记的 IDE 端口。"""
    settings = Settings(eula_accepted=True, workspace_root=tmp_path)
    provider = DockerProvider(settings)
    ws = make_workspace()
    ws.container_name = "ec-test"
    ws.ide_port = 38101
    provider._allocated_ide_ports[ws.id] = 38101

    released: list[int] = []
    monkeypatch.setattr(
        "app.services.providers.docker.release_tcp_port", lambda port: released.append(port)
    )

    def fake_run(args, *, check=True):
        if args[:2] in (["docker", "rm"], ["docker", "inspect"]):
            return CompletedProcess(
                args=args, returncode=1, stdout="", stderr="Cannot connect to the Docker daemon"
            )
        return CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(provider, "_run", fake_run)
    with pytest.raises(RuntimeError, match="docker command failed"):
        provider.destroy(ws)
    assert released == [38101]
    assert ws.id not in provider._allocated_ide_ports


def test_stop_idempotent_when_no_such_container(monkeypatch, tmp_path):
    """docker stop 对不存在的容器也应幂等成功（不因静默失败遗留错误状态）。"""
    settings = Settings(eula_accepted=True, workspace_root=tmp_path)
    provider = DockerProvider(settings)
    ws = make_workspace()
    ws.container_name = "ec-test"

    def fake_run(args, *, check=True):
        if args[:2] == ["docker", "stop"]:
            return CompletedProcess(
                args=args, returncode=1, stdout="", stderr="Error: No such container: ec-test"
            )
        return CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(provider, "_run", fake_run)
    provider.stop(ws)  # 不应抛错


# ---------------------------------------------------------------------------
# pull_artifact：命令构造（真实 docker daemon 路径待物理验证）
# ---------------------------------------------------------------------------


def _fake_cp_run(commands: list):
    """模拟 docker cp 成功：把内容写到目标路径（目标文件由 docker cp 产生）。"""

    def fake_run(args, *, check=True):
        commands.append(args)
        if args[:2] == ["docker", "cp"]:
            dest = Path(args[-1])
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"artifact-bytes")
        return CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    return fake_run


def test_pull_artifact_constructs_docker_cp(monkeypatch, tmp_path):
    """docker cp {container}:{path} {tmp} 命令参数正确，返回临时文件。"""
    settings = Settings(eula_accepted=True, workspace_root=tmp_path)
    provider = DockerProvider(settings)
    ws = make_workspace()
    ws.container_name = "ec-11111111-222"
    commands: list[list[str]] = []

    monkeypatch.setattr(provider, "_run", _fake_cp_run(commands))
    result = provider.pull_artifact(ws, "outputs/checkpoint.pt")

    assert commands[0][:2] == ["docker", "cp"]
    assert commands[0][2] == "ec-11111111-222:outputs/checkpoint.pt"
    assert commands[0][3] == str(result)
    assert result.read_bytes() == b"artifact-bytes"


def test_pull_artifact_derives_container_name_when_missing(monkeypatch, tmp_path):
    """container_name 缺失时按 ec-{id[:12]} 推导（与 destroy 一致）。"""
    settings = Settings(eula_accepted=True, workspace_root=tmp_path)
    provider = DockerProvider(settings)
    ws = make_workspace()  # container_name 为 None
    commands: list[list[str]] = []

    monkeypatch.setattr(provider, "_run", _fake_cp_run(commands))
    provider.pull_artifact(ws, "checkpoint.pt")

    assert commands[0][2] == "ec-11111111-222:checkpoint.pt"


def test_pull_artifact_raises_on_docker_cp_failure(monkeypatch, tmp_path):
    """docker cp 非零 returncode → 上抛 RuntimeError。"""
    settings = Settings(eula_accepted=True, workspace_root=tmp_path)
    provider = DockerProvider(settings)
    ws = make_workspace()
    ws.container_name = "ec-test"

    def fake_run(args, *, check=True):
        return CompletedProcess(
            args=args, returncode=1, stdout="", stderr="Error: No such container: ec-test"
        )

    monkeypatch.setattr(provider, "_run", fake_run)
    with pytest.raises(RuntimeError, match="docker cp failed"):
        provider.pull_artifact(ws, "checkpoint.pt")
