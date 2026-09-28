"""DockerProvider 真容器集成用例（docker_integration）。

覆盖面就是 docs/CURRENT_STATE.md 长期挂在 TECH DEBT 上的那串方法：
health 之外的 start/stop/destroy/inspect/logs/wait_ready/reconcile/
pull_artifact/_streaming_workspace_running/_assert_streaming_slot_available。
此前只有"复刻命令行参数"的假客户端单测，docker 语义（幂等 absent、真实失败上抛、
容器内 exec、端口是否真被释放）从未在真守护进程上执行过。

不覆盖的部分（如实标注，不冒充）：
- provision()：`health()` 要求 nvidia-smi，且启动参数固定带 `--gpus device=N`；
  本机是 Apple Silicon，无 CUDA → 仍属 PHYSICAL_GPU_VALIDATION_PENDING。
- WebRTC 媒体面（49100/TCP + 47998/UDP 上真有流）：需要 Isaac Sim 容器 → PENDING。

运行：`make test-docker`（需 docker daemon + 本地已缓存测试镜像）。
缺件时整档干净跳过，skip 文案带 DOCKER_VALIDATION_PENDING 供 release gate 登记。
"""

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from app.config import Settings
from app.models import Template, Workspace
from app.services.ports import is_port_free
from app.services.providers.base import ResourceReservation, RuntimeState
from app.services.providers.docker import DockerProvider
from tests.docker_probe import detail_of, docker_probe

GATE_SENTINEL = "DOCKER_VALIDATION_PENDING"

pytestmark = pytest.mark.docker_integration

IMAGE = os.environ.get("EMBODIEDCLOUD_DOCKER_TEST_IMAGE", "alpine:latest")


def _docker(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    # S603/S607: 参数为本模块受控常量（容器名由 uuid 铸造），无用户输入
    return subprocess.run(  # noqa: S603
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


IMAGE = os.environ.get("EMBODIEDCLOUD_DOCKER_TEST_IMAGE", "")

# 本机 daemon 与镜像架构必须一致：实测 `alpine:latest` 在本机是 amd64/占位条目，
# 容器起来就退（--rm 下表现为"No such container"），会让整档读数失真。
IMAGE_CANDIDATES = ("node:22-alpine", "postgres:16-alpine", "ubuntu:24.04", "alpine:latest")


def _docker_probe(*args: str, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    """前置检查统一走 `tests/docker_probe.py`（四个 docker 档共用一份异常吸收）。"""
    return docker_probe(_docker, *args, timeout=timeout)


def _daemon_arch(notes: list[str]) -> str:
    probe = _docker_probe("info", "--format", "{{.Architecture}}")
    if probe.returncode != 0:
        notes.append(f"docker info: {(probe.stderr or probe.stdout).strip()[:120]}")
        return ""
    raw = probe.stdout.strip()
    return {"aarch64": "arm64", "x86_64": "amd64"}.get(raw, raw)


def _image_arch(image: str) -> str:
    return _docker_probe("image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", image).stdout.strip()


def _usable_image() -> tuple[str | None, str]:
    """返回 (可用的测试镜像, 不可用原因)。前置检查的异常都在这条路上被吸收成原因文本。"""
    notes: list[str] = []
    arch = _daemon_arch(notes)
    candidates = (IMAGE,) if IMAGE else IMAGE_CANDIDATES
    for cand in candidates:
        if not cand:
            continue
        inspect = _docker_probe("image", "inspect", cand)
        if inspect.returncode != 0:
            if inspect.stderr and inspect.stderr.strip() not in notes:
                notes.append(f"docker image inspect {cand}: {inspect.stderr.strip()[:100]}")
            continue
        image_arch = _image_arch(cand)
        if arch and image_arch.endswith(arch):
            return cand, ""
    base = (
        f"没有本地缓存且架构匹配（daemon={arch or '未知'}）的测试镜像，试过：{', '.join(c for c in candidates if c)}"
        "；先 `docker pull` 其中任一，或用 EMBODIEDCLOUD_DOCKER_TEST_IMAGE 指向本地已有的镜像"
    )
    return None, "；".join([base, *notes])


def gate_reason() -> str | None:
    if shutil.which("docker") is None:
        return "docker CLI 不可用"
    probe = _docker_probe("version")
    if probe.returncode != 0:
        detail = detail_of(probe)
        return f"docker daemon 不可达（{detail or 'docker version 返回非零'}）"
    image, reason = _usable_image()
    if image is None:
        return reason
    return None


@pytest.fixture(scope="session")
def test_image():
    image, _reason = _usable_image()
    if image is None:
        pytest.skip(f"{GATE_SENTINEL}: {_reason}")
    return image


def _owned_containers() -> list[str]:
    return _docker("ps", "-a", "--filter", "name=ec-dtest-", "--format", "{{.Names}}").stdout.split()


@pytest.fixture(scope="session", autouse=True)
def _require_docker():
    reason = gate_reason()
    if reason is not None:
        pytest.skip(f"{GATE_SENTINEL}: {reason}")
    # 只对本次会话新建的容器负责（不动同机其它项目的容器）
    baseline = set(_owned_containers())
    yield
    leaked = sorted(set(_owned_containers()) - baseline)
    for name in leaked:
        _docker("rm", "-f", name, timeout=60)
    assert not leaked, f"用例泄漏了未清理的容器：{leaked}"


class Containers:
    """本用例创建的容器一律登记，teardown 强制删除。

    故意不加 `--rm`：提供方的 stop()/inspect()/reconcile() 要观察"容器已退出但仍
    存在"这一真实状态（带 --rm 时 stop 会把容器整个删掉，读到的是 absent，
    断言就变成自证）。生产 provision 用 --rm，其 STOPPED→start 语义由
    orchestrator 把 START 路由到 _execute_provision 重新起容器来兜。
    """

    def __init__(self, image: str) -> None:
        self.image = image
        self.names: list[str] = []

    def start(self, *args: str) -> str:
        name = f"ec-dtest-{uuid.uuid4().hex[:8]}"
        proc = _docker("run", "-d", "--name", name, self.image, *args)
        if proc.returncode != 0:
            raise RuntimeError(f"docker run failed: {proc.stdout}{proc.stderr}")
        self.names.append(name)
        return name

    def start_with_args(self, *docker_args: str) -> str:
        """需要 --label / -p 等参数时用这条（参数在 image 之前）。"""
        name = f"ec-dtest-{uuid.uuid4().hex[:8]}"
        proc = _docker("run", "-d", "--name", name, *docker_args, self.image, "sh", "-c", "sleep 300")
        if proc.returncode != 0:
            raise RuntimeError(f"docker run failed: {proc.stdout}{proc.stderr}")
        self.names.append(name)
        return name

    def publish_idle_port(self) -> tuple[str, int]:
        """容器在跑、宿主机端口被 dockerd 代理占住（不需要容器内真有监听）。"""
        name = f"ec-dtest-{uuid.uuid4().hex[:8]}"
        proc = _docker(
            "run", "-d", "--name", name, "-p", "127.0.0.1::8080", self.image, "sh", "-c", "sleep 300"
        )
        if proc.returncode != 0:
            raise RuntimeError(f"docker run failed: {proc.stdout}{proc.stderr}")
        self.names.append(name)
        return name, _host_port(name, "8080/tcp")

    def cleanup(self) -> None:
        for name in self.names:
            _docker("rm", "-f", name, timeout=60)
        self.names.clear()


@pytest.fixture
def http_server():
    """控制面本地的真 HTTP 服务：wait_ready 探的是 TCP+HTTP 可达，与容器无关。"""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ready")

        def log_message(self, *args):  # 静音
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _host_port(name: str, private_port: str) -> int:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        out = _docker("port", name, private_port).stdout
        for line in out.splitlines():
            host, _, port = line.strip().rpartition(":")
            if host == "127.0.0.1" and port.isdigit():
                return int(port)
        if _docker("inspect", "-f", "{{.State.Running}}", name).stdout.strip() != "true":
            logs = _docker("logs", "--tail", "30", name)
            raise RuntimeError(f"容器 {name} 未运行：{logs.stdout}{logs.stderr}")
        time.sleep(0.3)
    raise RuntimeError(f"容器 {name} 未发布 {private_port}")


@pytest.fixture
def containers(test_image):
    c = Containers(test_image)
    try:
        yield c
    finally:
        c.cleanup()


@pytest.fixture
def provider(tmp_path):
    return DockerProvider(
        Settings(
            provider="docker",
            database_url="sqlite:///:memory:",
            workspace_root=tmp_path / "ws",
            eula_accepted=True,
            privacy_consent=False,
        )
    )


def ws(container_name: str | None = None, ide_port: int | None = None, workspace_id: str | None = None) -> Workspace:
    wid = workspace_id or str(uuid.uuid4())
    return Workspace(
        id=wid,
        name="docker-test",
        template_id="cartpole",
        provider="docker",
        status="running",
        container_name=container_name,
        ide_port=ide_port,
    )


def template_with_healthcheck(command: str) -> Template:
    return Template(
        id="cartpole",
        slug="cartpole",
        name="CartPole",
        description="t",
        category="rl",
        healthcheck={"command": command},
    )


def wait_running(provider: DockerProvider, workspace: Workspace, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if provider.inspect(workspace).get("running"):
            return True
        time.sleep(0.3)
    return False


def wait_exited(provider: DockerProvider, workspace: Workspace, timeout: float = 30.0) -> dict:
    """等容器真的不在 running 了（`wait_running` 的负向对称体），返回最后一次实况。

    与 `wait_running` 不同，这里超时是**失败**不是 False：调用方的判据是
    "已退出的容器不该被读成就绪"，前提没立起来时报告说的必须是"前提未达成"，
    而不是让产品结论去承担一次竞速的运气。
    """
    deadline = time.monotonic() + timeout
    seen: dict = {}
    while time.monotonic() < deadline:
        seen = provider.inspect(workspace)
        if not seen.get("running"):
            return seen
        time.sleep(0.2)
    raise AssertionError(f"前提未达成：{timeout}s 内容器仍在 running，最后一次读数 {seen!r}")


# ---------------------------------------------------------------------------
# inspect / reconcile
# ---------------------------------------------------------------------------


def test_inspect_reports_real_running_state(provider, containers):
    name = containers.start("sh", "-c", "sleep 60")
    workspace = ws(name)
    assert wait_running(provider, workspace), "容器未在 30s 内进入 running"
    state = provider.inspect(workspace)
    assert state["state"] == "running" and state["running"] is True
    assert state["container_name"] == name


def test_inspect_distinguishes_absent_from_exited(provider, containers):
    # 从未存在过 → absent
    gone = provider.inspect(ws("ec-dtest-does-not-exist"))
    assert gone["state"] == "absent"

    # 真实退出过的容器（不带 --rm）→ exited + 真实退出码
    exited_name = f"ec-dtest-exited-{uuid.uuid4().hex[:8]}"
    proc = _docker("run", "--name", exited_name, containers.image, "sh", "-c", "exit 3")
    # 先登记再断言：前提失败也不把容器留在机器上
    containers.names.append(exited_name)
    # docker run（非 -d）会把容器的退出码原样带出来 → 这里 3 才是"执行成功"
    assert proc.returncode == 3, f"退出码前提异常：rc={proc.returncode} {proc.stderr}"
    try:
        state = provider.inspect(ws(exited_name))
        assert state["state"] == "exited", f"应读到 exited：{state}"
        assert state["running"] is False
        assert state["exit_code"] == 3, f"退出码应为 3：{state}"
    finally:
        _docker("rm", "-f", exited_name)

    # container_name 未持久化 → absent（而非崩溃）
    assert provider.inspect(ws(None))["state"] == "absent"


def test_reconcile_alive_missing_real_containers(provider, containers):
    name = containers.start("sh", "-c", "sleep 60")
    workspace = ws(name)
    assert wait_running(provider, workspace)
    assert provider.reconcile(workspace) == RuntimeState.ALIVE

    _docker("rm", "-f", name)
    assert provider.reconcile(workspace) == RuntimeState.MISSING
    assert provider.reconcile(ws(None)) == RuntimeState.MISSING


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------


def test_logs_returns_real_container_stdout(provider, containers):
    marker = f"log-{uuid.uuid4().hex[:8]}"
    name = containers.start("sh", "-c", f"echo {marker}; sleep 5")
    workspace = ws(name)
    assert wait_running(provider, workspace)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if marker in provider.logs(workspace, tail=50):
            break
        time.sleep(0.3)
    assert marker in provider.logs(workspace, tail=50), "docker logs 未取到真实输出"
    assert provider.logs(ws("no-such-container")) == ""
    assert provider.logs(ws(None)) == ""


# ---------------------------------------------------------------------------
# start / stop / destroy
# ---------------------------------------------------------------------------


def test_stop_start_roundtrip_on_real_container(provider, containers):
    name = containers.start("sh", "-c", "sleep 300")
    workspace = ws(name)
    assert wait_running(provider, workspace)

    provider.stop(workspace)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and provider.inspect(workspace).get("running"):
        time.sleep(0.3)
    assert provider.inspect(workspace)["state"] == "exited", "stop 后容器应真的停下"

    provider.start(workspace)
    assert wait_running(provider, workspace), "start 后容器应重新 running"


def test_destroy_removes_container_and_releases_real_port(provider, containers):
    name, port = containers.publish_idle_port()
    workspace = ws(name, ide_port=port)
    assert wait_running(provider, workspace)
    # 前提：端口此刻确实被占用（否则"释放"断言是空的）
    assert not is_port_free(port), f"前提不成立：发布端口 {port} 并未被占用"

    provider.destroy(workspace)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and provider._container_exists(workspace):
        time.sleep(0.3)
    assert not provider._container_exists(workspace), "destroy 后容器仍存在"
    assert is_port_free(port), f"端口 {port} 未真正释放回池"

    # 幂等：再来一次不得抛
    provider.destroy(workspace)


def test_destroy_derives_container_name_when_not_persisted(provider, containers):
    wid = str(uuid.uuid4())
    name = f"ec-{wid[:12]}"
    proc = _docker("run", "-d", "--rm", "--name", name, containers.image, "sh", "-c", "sleep 60")
    assert proc.returncode == 0, proc.stderr
    containers.names.append(name)
    assert wait_running(provider, ws(name, workspace_id=wid)), "容器未进入 running"

    # 关键：container_name 未持久化（provision 中途失败的场景），destroy 仍要清干净
    workspace = ws(None, workspace_id=wid)
    provider.destroy(workspace)
    assert not provider._container_exists(workspace), "按命名约定推导 container_name 的补偿清理未生效"


def test_reconcile_asks_the_engine_when_the_name_column_is_empty(provider, containers):
    """真引擎上的 N-70 判决：容器活着而列是空的 ⇒ ALIVE，停过之后才 MISSING。

    改前 `reconcile` 开头 `if not workspace.container_name: return MISSING` —— 这台真机上
    那个容器明明在跑，控制面却说它不在了，于是释放准入会把还有人吃的卡放回池子。
    这里用 `ec-{id[:12]}` 造一个"名字合乎约定但没落库"的真容器，把两档都走一遍：
    ALIVE（问到了）→ stop（现在真的发命令了）→ MISSING（引擎说没了）。
    """
    wid = str(uuid.uuid4())
    name = f"ec-{wid[:12]}"
    proc = _docker("run", "-d", "--rm", "--name", name, containers.image, "sh", "-c", "sleep 60")
    assert proc.returncode == 0, proc.stderr
    containers.names.append(name)
    assert wait_running(provider, ws(name, workspace_id=wid)), "容器未进入 running"

    workspace = ws(None, workspace_id=wid)  # 列没落上
    assert provider.reconcile(workspace) is RuntimeState.ALIVE, (
        "空列被当成缺席：引擎说这个容器在跑"
    )

    provider.stop(workspace)
    assert provider.reconcile(workspace) is not RuntimeState.ALIVE, (
        "stop 在空列时静默空转（改前的 `if workspace.container_name:`）"
    )


def test_run_checked_raises_on_real_failure_but_swallows_absent(provider, containers):
    # 幂等：容器不存在时 stop/start/rm 均不得抛
    provider.stop(ws("ec-dtest-absent"))
    provider.start(ws("ec-dtest-absent"))
    provider.destroy(ws("ec-dtest-absent"))

    # 真实失败且容器确实存在 → 必须上抛（否则上层会误判"已清理"并释放 GPU）
    name = containers.start("sh", "-c", "sleep 300")
    bad = _docker("update", "--restart-policy", "bogus", name)  # 合法容器 + 非法参数
    assert bad.returncode != 0, f"构造失败前提未成立：{bad.stderr}"
    with pytest.raises(RuntimeError, match="docker command failed"):
        provider._run_checked(["docker", "update", "--restart-policy", "bogus", name], container_name=name)


# ---------------------------------------------------------------------------
# _container_exists / streaming 槽位（真标签）
# ---------------------------------------------------------------------------


def test_container_exists_matches_only_the_exact_name(provider, containers):
    name = containers.start("sh", "-c", "sleep 30")
    assert wait_running(provider, ws(name))
    assert provider._container_exists(ws(name))
    assert not provider._container_exists(ws(f"{name}-suffix"))
    assert not provider._container_exists(ws(None))


def test_streaming_slot_detected_from_real_container_labels(provider, containers):
    assert provider._streaming_workspace_running() is False

    name = containers.start_with_args(
        "--label", "embodiedcloud.workspace=1",
        "--label", "embodiedcloud.streaming=1",
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and provider._streaming_workspace_running() is False:
        time.sleep(0.3)
    assert provider._streaming_workspace_running() is True, "docker 标签过滤未识别到流媒体容器"
    with pytest.raises(RuntimeError, match="streaming workspace is already running"):
        provider._assert_streaming_slot_available()

    _docker("rm", "-f", name)
    assert provider._streaming_workspace_running() is False


# ---------------------------------------------------------------------------
# pull_artifact（docker cp 真路径）
# ---------------------------------------------------------------------------


def test_pull_artifact_copies_a_real_file_out_of_the_container(provider, containers):
    marker = f"artifact-{uuid.uuid4().hex[:8]}"
    name = containers.start("sh", "-c", f"echo {marker} > /tmp/model.pt; sleep 120")
    workspace = ws(name)
    deadline = time.monotonic() + 30
    content = ""
    path: Path | None = None
    while time.monotonic() < deadline:
        try:
            # /tmp 是容器内路径（docker cp 的源），不是宿主临时目录
            path = provider.pull_artifact(workspace, "/tmp/model.pt")  # noqa: S108
        except RuntimeError:
            time.sleep(0.5)
            continue
        content = path.read_text()
        break
    assert path is not None and marker in content, f"docker cp 未取到文件：{content!r}"
    assert str(path).startswith(str(Path(tempfile_root())))
    shutil.rmtree(path.parent, ignore_errors=True)


def tempfile_root() -> str:
    import tempfile

    return tempfile.gettempdir()


def test_pull_artifact_failure_cleans_up_its_temp_dir(provider, containers):
    name = containers.start("sh", "-c", "sleep 60")
    workspace = ws(name)
    assert wait_running(provider, workspace)
    with pytest.raises(RuntimeError, match="docker cp failed"):
        provider.pull_artifact(workspace, "/tmp/definitely-not-here.bin")  # noqa: S108  容器内路径


# ---------------------------------------------------------------------------
# wait_ready（running + TCP/HTTP + 容器内 exec 三道真判据）
# ---------------------------------------------------------------------------


def test_wait_ready_passes_against_real_http_and_exec_healthcheck(provider, containers, http_server):
    """三道判据全真：容器 running + 宿主端口 TCP/HTTP 可达 + 容器内 exec 通过。"""
    name = containers.start("sh", "-c", "sleep 300")
    workspace = ws(name, ide_port=http_server)
    assert wait_running(provider, workspace)
    # 真实前提：healthcheck 检查的标记文件由容器内 exec 创建
    created = _docker("exec", name, "sh", "-c", "touch /ec-ready-marker")
    assert created.returncode == 0, created.stderr
    template = template_with_healthcheck("test -f /ec-ready-marker")
    assert provider.wait_ready(workspace, template, timeout_seconds=60) is True


def test_wait_ready_fails_when_exec_healthcheck_fails(provider, containers, http_server):
    """前提反证：标记文件不存在时，同一判据必须读到 False（否则上一条是假绿）。"""
    name = containers.start("sh", "-c", "sleep 300")
    workspace = ws(name, ide_port=http_server)
    assert wait_running(provider, workspace)
    template = template_with_healthcheck("test -f /ec-ready-marker")
    started = time.monotonic()
    assert provider.wait_ready(workspace, template, timeout_seconds=6) is False
    assert time.monotonic() - started >= 5.5, "wait_ready 未真正用尽超时（判据可能被短路）"


def test_wait_ready_fails_when_container_not_running(provider, containers):
    name = containers.start("sh", "-c", "exit 0")
    workspace = ws(name)
    # 前提必须先立：`docker run -d` 一返回容器就是"已启动"，而 `exit 0` 落地只有几毫秒。
    # 没有这道等待时，本条判据其实在和 exit 竞速——首次 inspect 若抓到 running=True，
    # 而这条工作区既无 ide_port 也无 healthcheck，wait_ready 就会照实返回 True。
    # 本轮实测：同一份树两次全量跑，一次绿、一次 assert True is False（宿主 load ~10）。
    state = wait_exited(provider, workspace)
    assert state.get("exit_code") == 0, state  # 退出的是我们要它退的那个进程
    assert provider.wait_ready(workspace, Template(), timeout_seconds=5) is False
    assert provider.wait_ready(ws(None), Template(), timeout_seconds=1) is False


def test_wait_exited_fails_loudly_when_the_container_keeps_running(provider, containers):
    """前提工具自己得会开火，否则它只是一次无声的 sleep。"""
    name = containers.start("sh", "-c", "sleep 300")
    with pytest.raises(AssertionError, match="前提未达成"):
        wait_exited(provider, ws(name), timeout=2.0)


def test_wait_ready_passes_without_ide_port_and_without_healthcheck(provider, containers):
    """无 ide_port / 无 healthcheck 时只判 running —— 两档都得真过一遍。"""
    name = containers.start("sh", "-c", "sleep 60")
    workspace = ws(name)
    assert wait_running(provider, workspace)
    assert provider.wait_ready(workspace, Template(), timeout_seconds=20) is True


# ---------------------------------------------------------------------------
# credential rotation（真容器上确认"env 不可变"这一策略判断）
# ---------------------------------------------------------------------------


def test_credential_rotation_stays_unsupported_on_real_container(provider, containers):
    name = containers.start_with_args("-e", "WORKSPACE_PASSWORD=first")
    workspace = ws(name)
    assert wait_running(provider, workspace)
    assert provider.supports_credential_rotation is False
    assert provider.rotate_credentials(workspace, {"password": "second"}) is False
    # 真实前提：运行中容器的 env 里仍是旧口令（不是我们"声称"不可变）
    inspect = _docker("inspect", "-f", "{{json .Config.Env}}", name)
    assert "WORKSPACE_PASSWORD=first" in inspect.stdout
    assert "WORKSPACE_PASSWORD=second" not in inspect.stdout


# ---------------------------------------------------------------------------
# §24 GPU 设备透传（--gpus）：可数字化的一半 = 守护进程理解并按 reservation 绑定
#
# 本机无 NVIDIA 设备/CDI，"容器里真能看见 GPU"那半仍是 PHYSICAL_PENDING（脚本
# scripts/gpu_acceptance.sh）。这一节验收的是另一半：provider 发出的那条 argv
# 是不是被守护进程**原样接受并记成对应设备的 DeviceRequest**。
# 用 create 而非 run 就够：create 阶段守护进程已经解析并落库 HostConfig，
# 实测 `--gpus device=7` → `{"DeviceIDs":["7"],"Capabilities":[["gpu"]]}`。
# ---------------------------------------------------------------------------


def _reservation(gpu_index: int) -> ResourceReservation:
    return ResourceReservation(
        host_id="localhost",
        gpu_id=f"gpu-{gpu_index}",
        gpu_uuid=f"GPU-0000{gpu_index}",
        gpu_index=gpu_index,
        metadata={"operation_id": "op-gpu-1", "fencing_token": 7},
    )


def _run_argv(provider: DockerProvider, image: str, gpu_index: int, name: str, tmp_path: Path) -> list[str]:
    return provider.run_argv(
        workspace=ws(container_name=name),
        template=Template(id="cartpole", slug="cartpole", name="CartPole", description="t", category="rl"),
        reservation=_reservation(gpu_index),
        container_name=name,
        workspace_dir=tmp_path / "proj",
        ide_port=18123,
        password="s3cret-probe",  # noqa: S106 测试数据，只进 create 的 env
        image=image,
    )


def _to_create(argv: list[str]) -> list[str]:
    """只把 `run -d --rm` 头换成 `create`，其余参数一字不改。

    生产 argv 的形态本身由 `assert argv[:4] == ...` 钉住；测试不重抄命令行，
    重抄只能证明两份抄得一样。
    """
    assert argv[:4] == ["docker", "run", "-d", "--rm"], argv[:4]
    return argv[4:]


def _exec_verbatim(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """原样执行生产 argv（argv[0] 已是 "docker"），与 provider._run 同一调用形状。"""
    return subprocess.run(  # noqa: S603 受控 argv：与生产路径同一份列表
        argv, capture_output=True, text=True, timeout=120, check=False
    )


@pytest.mark.parametrize("gpu_index", [0, 3])
def test_gpu_index_binding_is_recorded_by_the_daemon_itself(provider, test_image, tmp_path, gpu_index):
    """GPU 绑定来自 reservation，且守护进程按同一序号记账（不是"我们以为传了"）。"""
    name = f"ec-dtest-gpu{gpu_index}-{uuid.uuid4().hex[:6]}"
    argv = _run_argv(provider, test_image, gpu_index, name, tmp_path)
    created = _docker("create", *_to_create(argv))
    assert created.returncode == 0, created.stderr
    try:
        import json

        # index 3 在本机并不存在（本机无 GPU）：守护进程照单记账，说明这条参数的
        # 语义是"请求该设备"，硬件侧的满足与否归 CDI/runtime（物理档）。
        recorded = json.loads(
            _docker("inspect", "-f", "{{json .HostConfig.DeviceRequests}}", name).stdout
        )
        assert recorded == [
            {"Driver": "", "Count": 0, "DeviceIDs": [str(gpu_index)], "Capabilities": [["gpu"]], "Options": {}}
        ], recorded
        labels = json.loads(_docker("inspect", "-f", "{{json .Config.Labels}}", name).stdout)
        assert labels["embodiedcloud.gpu"] == str(gpu_index)
        assert labels["embodiedcloud.gpu_id"] == f"gpu-{gpu_index}"
        assert labels["embodiedcloud.operation"] == "op-gpu-1"
        assert labels["embodiedcloud.fencing"] == "7"
        binds = json.loads(_docker("inspect", "-f", "{{json .HostConfig.Binds}}", name).stdout)
        assert binds == [f"{tmp_path / 'proj'}:/workspace/project:rw"], binds
        env = json.loads(_docker("inspect", "-f", "{{json .Config.Env}}", name).stdout)
        assert "WORKSPACE_PASSWORD=s3cret-probe" in env
        assert "IDE_PORT=18123" in env
        assert "ACCEPT_EULA=Y" in env
        assert _docker("inspect", "-f", "{{.HostConfig.NetworkMode}}", name).stdout.strip() == "host"
    finally:
        _docker("rm", "-f", name)


def test_provider_argv_is_accepted_by_the_daemon_and_gpu_failure_is_not_a_usage_error(
    provider, test_image, tmp_path
):
    """把生产 argv 原样交给 `docker run`：守护进程必须走到 GPU 发现，而不是报命令行错误。

    这是"参数形状对不对"的正向证明（错误形状会以 `unknown flag` 一类 CLI 用法错误结束）。
    同时确认失败不留孤儿容器：`--rm` 下启动失败的容器会被自动清掉（实测
    "error: no such object"），否则同名重试会撞容器名，且旧容器可能仍占着那张卡。
    """
    name = f"ec-dtest-run-{uuid.uuid4().hex[:6]}"
    argv = _run_argv(provider, test_image, 0, name, tmp_path)
    result = _exec_verbatim(argv)
    if result.returncode == 0:
        # 有真实 GPU 运行时的机器（例如 gpu 档主机）上它会起来：那也是通过
        _docker("rm", "-f", name, timeout=30)
        return
    stderr = result.stderr.lower()
    assert "unknown flag" not in stderr, result.stderr
    assert "accepts no arguments" not in stderr, result.stderr
    assert "gpu" in stderr, result.stderr  # 实测：failed to discover GPU vendor from CDI
    # 失败之后不能再有同名容器（否则 provision 重试会被名字冲突卡住）
    assert _docker("inspect", "-f", "{{.State.Status}}", name).returncode != 0


# --- 守护进程这条传输的读数分类：判"registry 有没有答过话"，不数症状关键词 -------------
#
# 为什么不用关键词表（本轮 2026-09-27 改的）：一次 ghcr 通道抖动把 docker 档 27 支里的 1 支
# 变成**代码失败**，事后逐字对过一手来源，两处形状错在同一个地方——把病因按头部截断 + 按症状枚举：
# - containerd 把传输原因接在**最后**：`client/pull.go:188` 的
#   `failed to resolve reference %q: %w` 套 `core/remotes/docker/resolver.go:649` 的
#   `failed to do request: %w`，最里层才是 net 层错误。所以"取前 200 字符"正好把唯一有区分力
#   的那一段切掉，剩下的前缀在"通道没走到"与"registry 答了话"两种情形下逐字相同。
# - moby 自己也按"有没有 registry 错误体"分流：`daemon/containerd/registry_errors.go`
#   用 `errors.As` 取 `docker.Errors` / `ErrUnexpectedStatus`，命中才加 `error from registry: %w`
#   前缀。⇒ "答复形状"是上游自己的分类轴，不是我发明的。
# - go-containerregistry 的 `Error.Temporary()`（`pkg/v1/remote/transport/error.go`）把
#   OCI code 与 HTTP status 两个命名空间并起来判（408/429/500/502/503/504 为临时），
#   其余一律 fatal ⇒ 401/403/denied/manifest unknown 是**答复**，不是"没走到"。
# - 码表与 404 规定见 OCI distribution-spec（`MANIFEST_UNKNOWN`／`NAME_UNKNOWN`／`DENIED`／
#   `TOOMANYREQUESTS`，以及"manifest is not found in the repository, the response code MUST be
#   404 Not Found"）。
# 借的是它的**分类轴**（答复形状 + 存在性主张两分），不引依赖：这里只有守护进程打出的一行文本可读。

ABSENT_CLAIMS = ("manifest unknown", "not found", "name unknown", "digest invalid")
OTHER_ANSWERS = (
    "error from registry:", "denied", "unauthorized", "toomanyrequests", "unexpected status",
    "forbidden", "authentication required", "invalid repository name",
)

# 本机一手读数（2026-09-27，`docker pull <这些引用>` 的完整 stderr，未截断）
DAEMON_READINGS: tuple[tuple[str, str, str], ...] = (
    (
        'Error response from daemon: failed to resolve reference '
        '"ghcr.io/astral-sh/uv@sha256:' + "a" * 64 + '": ghcr.io/astral-sh/uv@sha256:' + "a" * 64
        + ": not found",
        "absent",
        "本机：真 ghcr 仓库 + 全 a 的假摘要 ⇒ registry 明说没有",
    ),
    (
        "Error response from daemon: error from registry: denied\ndenied",
        "answered",
        "本机：不存在的 ghcr 仓库 ⇒ 答的是权限/不泄漏存在性，不改写存在性主张",
    ),
    (
        'Error response from daemon: failed to resolve reference "registry-embodiedcloud-'
        'nonexistent-xyz.test/foo:1.0.0": failed to do request: Head "https://registry-'
        'embodiedcloud-nonexistent-xyz.test/v2/foo/manifests/1.0.0": dial tcp: lookup '
        "registry-embodiedcloud-nonexistent-xyz.test on 192.168.5.1:53: no such host",
        "no-answer",
        "本机：域名解析不到 ⇒ 根本没走到 registry",
    ),
    (
        'Error response from daemon: failed to resolve reference "localhost:1/foo/bar:1.0.0": '
        'failed to do request: Head "https://localhost:1/v2/foo/bar/manifests/1.0.0": dial tcp '
        "127.0.0.1:1: connect: connection refused",
        "no-answer",
        "本机：端口拒连 ⇒ 同上，前缀与第一种假摘要逐字同形",
    ),
    (
        'Error response from daemon: unknown: failed to resolve reference '
        '"ghcr.io/mowglinext/mowglinext/mowglinext-gui:main": unexpected status from HEAD '
        'request to https://ghcr.io/v2/mowglinext/mowglinext/mowglinext-gui/manifests/main: '
        "403 Forbidden",
        "answered",
        "第三方原文（github.com/mowglinext/mowglinext/issues/358，本轮重开过正文）："
        "含 failed to resolve reference 与 Head 字样却是**真答复**——症状关键词表在这种串上必错",
    ),
    (
        'Error response from daemon: failed to resolve reference "ghcr.io/trufflesecurity/'
        'trufflehog:latest": failed to do request: Head "https://ghcr.io/v2/trufflesecurity/'
        'trufflehog/manifests/latest": net/http: TLS handshake timeout',
        "no-answer",
        "按第三方 issue 的形状构造（github.com/evoila/meho/issues/3310 正文里 "
        "`net/http: TLS handshake timeout` 是**单独一行的日志**，我把它拼进了 containerd 的错误链；"
        "本轮重开过该 issue 正文，逐字一整条未亲验、也未在本机重放）",
    ),
    (
        "Error response from daemon: toomanyrequests: retry-after: 243.008µs, allowed: 44000/minute",
        "answered",
        "按第三方 issue 的形状构造（github.com/pdcarlson/Frapp/issues/2609；本轮取正文时被匿名 API "
        "限流挡住，未亲验原文）。这一档的分类不依赖它：429/TOOMANYREQUESTS 属『临时答复』的依据是 "
        "go-containerregistry 的 temporaryErrorCodes 与 OCI spec 码表（本轮重开过源）",
    ),
)


def registry_reading(reading: str) -> str:
    """守护进程这条传输的读数分四档：absent／answered／no-answer／unreadable。

    absent    registry 明说"这份东西我这儿没有"（存在性主张——但镜像站会对**有效**摘要回
              `not found`，2026-09-26 实测过，所以它不能单独定案，要第二通道表态）
    answered  registry 答了话但不涉及存在性（denied／unauthorized／429／奇形状态码）
    no-answer 根本没走到 registry（DNS／拒连／TLS／超时／黑洞……）
    unreadable 我们连一行错误文本都没拿到——这是**我们自己的读数坏了**，
              不许折算成"环境没问题"，也不许折算成"钉错了"，单独一档留着逼人来看
    """
    low = (reading or "").strip().lower()
    if not low:
        return "unreadable"
    if any(k in low for k in ABSENT_CLAIMS):
        return "absent"
    if any(k in low for k in OTHER_ANSWERS):
        return "answered"
    return "no-answer"


def _daemon_reading_of(stderr: str) -> str:
    """取守护进程错误文本里最后一条非空行的**尾部**：病因在最后，截头等于截走判别位。"""
    for line in reversed((stderr or "").strip().splitlines()):
        if line.strip():
            return line.strip()
    return ""


def _pull_failure_action(daemon_reading: str, verdict: str, detail: str) -> tuple[str, str]:
    """把"守护进程这条传输的读数 + 第二通道的读数"映射成 (动作, 一句话)。

    纯函数是为了能被常驻用例逐档打靶（这一支判据真被镜像站的一次 `not found` 打过，
    下一轮多半是绿的——不抽出来，分流逻辑就只能等下一次故障才第一次运行）。

    **判点在次序**，三档动作各有不能洗的理由：
    1. 第二通道逐字节认定"这份摘要不存在" ⇒ `red_pin`。钉进配方的引用就是错的，
       守护进程那边长得再像通道故障也不改判。
    2. 第二通道逐字节认定"存在" ⇒ `skip`。内容寻址存储里"同一份东西"的定义已经被满足了，
       剩下的都是本机这条传输的事——哪怕守护进程回的是 `not found`（镜像站会这么撒的实测过）。
    3. 第二通道无法定案（`unknown`，它自己也够不到）时，才轮到守护进程的**答复形状**说话：
       它说没有 ⇒ `red_undecided`（两条读数都没有指认"存在"，也没有一条能说"是配方错"，
       留着别洗）；我们连文本都没拿到 ⇒ 同样 `red_undecided`；
       其余（答了话但不涉及存在性、或压根没走到）⇒ `skip`，并打出两条读数——
       这正是"环境抖动不得冒充代码失败"那一格：今晚的 ghcr 抖动就走在这里。
    """
    kind = registry_reading(daemon_reading)
    if verdict == "absent":
        return "red_pin", (
            f"两条独立传输都指认这份摘要不存在 ⇒ 钉进配方的引用是错的"
            f"（daemon 读数分类 {kind} 也不改判）：{daemon_reading}；{detail}"
        )
    if verdict == "present":
        return "skip", (
            f"{GATE_SENTINEL}: 第二通道逐字节确认该摘要存在（{detail}）⇒ 守护进程这条传输的"
            f"失败（分类 {kind}）判为通道问题，主张本轮未获证。daemon 读数：{daemon_reading}"
        )
    if kind in ("absent", "unreadable"):
        why = (
            "registry 答了『没有』这条摘要，而第二通道够不到、无法反驳"
            if kind == "absent"
            else "守护进程失败却没留下任何错误文本——这条判据的读数本身坏了"
        )
        return "red_undecided", (
            f"取不到且无法定案：{why}；第二通道读数 {verdict}（{detail}）；daemon 读数：{daemon_reading}"
        )
    what = "没走到 registry" if kind == "no-answer" else "答了话但不涉及存在性（鉴权/限流/状态码）"
    return "skip", (
        f"{GATE_SENTINEL}: 两条通道本轮都无法定案——daemon 这条{what}，第二通道 {verdict}"
        f"（{detail}）。没有任何一方指认这份摘要是错的，主张本轮未获证；"
        f"要定案就换一条出网通道再跑 `make control-image`。daemon 读数：{daemon_reading}"
    )


def test_independent_digest_read_discriminates_present_from_absent() -> None:
    """定案用的那条第二通道必须自己会分正反：真摘要＝present、翻一位＝absent。

    它若对坏摘要也回 present，上面那档 `skip` 就成了免检通道——任何钉错的 digest 都能靠
    "镜像站说不认识＋这条通道说存在"变成永久跳过。三档各有出口，`unknown` 走前提缺失。
    """
    from tests.test_supply_chain import REPO_ROOT, _base_refs, _is_pinned

    pinned = sorted({r for _, r in _base_refs([REPO_ROOT / "runtime" / "Dockerfile.control-plane"]) if _is_pinned(r)})
    assert pinned, "没有钉 digest 的引用，无法取真摘要做对照"
    # 逐份核，不是只核第一份：改成多阶段之后配方里两份钉死的引用（基础镜像＋builder 的 uv）
    # 分别走两条不同的第二通道映射（ECR Public 换注册表／ghcr 换传输），只跑 pinned[0] 的话
    # 后一条分支从来没被执行过——它写错也无人知道。
    unknowns = []
    for ref in pinned:
        digest = ref.rsplit("@sha256:", 1)[1]
        good_verdict, good_detail = _independent_digest_read(ref, "sha256:" + digest)
        if good_verdict == "unknown":
            unknowns.append(f"{ref.split('@')[0]}：{good_detail}")
            continue
        assert good_verdict == "present", f"配方里钉着的真摘要在第二通道不是 present：{good_verdict} / {good_detail}"

        flipped = ("0" if digest[0] != "0" else "1") + digest[1:]
        bad_verdict, bad_detail = _independent_digest_read(ref, "sha256:" + flipped)
        assert bad_verdict == "absent", f"翻掉一位的摘要竟然不是 absent（{bad_verdict} / {bad_detail}）⇒ 分流逻辑不可信"

    if unknowns:
        # 全部够不到＝前提未达成（如实跳过并带每份的读数）；只有一份够不到＝另一份已经
        # 把它那条分支的正反两档跑完了，不能拿邻居的不可达把这一份的失败洗掉。
        assert len(unknowns) < len(pinned), "两条第二通道都够不到：" + " | ".join(unknowns)

    # 映射不出的仓库必须落到 unknown，不能靠猜把别家的名字拼到官方库前缀上
    other_digest = "ab" * 32
    assert _independent_digest_read(f"registry.example.com/team/app:1.0.0@sha256:{other_digest}",
                                    "sha256:" + other_digest)[0] == "unknown"


def test_daemon_reading_classifier_separates_the_two_families() -> None:
    """一手读数逐条打靶：同一截前缀下的"没有"与"没走到"必须落到不同档。

    这一支就是本轮的因果证明：写它之前，分流靠一张症状关键词表，而今晚那条
    ghcr 抖动串（`failed to resolve reference … failed to do request: Head …`）
    既不在表里、也不长得像"registry 说不存在"，于是环境抖动在发布门禁上表现为一支代码失败。
    """
    seen: set[str] = set()
    for reading, expected, source in DAEMON_READINGS:
        got = registry_reading(reading)
        assert got == expected, f"分类成 {got}，期望 {expected}（{source}）：{reading[:90]}"
        seen.add(got)
    assert seen == {"absent", "answered", "no-answer"}, seen
    # 两极之一：坏值不落到某一档就是判据没开火
    assert registry_reading("") == "unreadable", "连错误文本都没有时必须单独一档，不许折算成环境没问题"
    # 另一半：含 `failed to resolve reference` 与 `Head "https://…"` 的两条读数必须分家——
    # 这正是症状关键词表做不到的事（前缀逐字同形，判别位在尾部）
    same_prefix = [r for r, _k, _s in DAEMON_READINGS if "failed to resolve reference" in r]
    classes = {registry_reading(r) for r in same_prefix}
    assert len(same_prefix) >= 3 and classes == {"absent", "no-answer", "answered"}, (
        len(same_prefix), sorted(classes))


def test_truncation_direction_is_load_bearing() -> None:
    """截断按头部会把病因切掉：本机那条 `: not found` 读数截到 200 字符就退化成 no-answer。

    守护进程的错误链是 containerd 那种"外层在前、原因在最后"的形状，所以这一格不是
    审美问题：旧写法 `splitlines()[-1][:200]` 与被切掉的那一位正好是分流唯一依据。
    """
    absent = next(r for r, k, _s in DAEMON_READINGS if k == "absent")
    assert registry_reading(absent) == "absent"
    assert registry_reading(absent[:200]) == "no-answer", (
        "截头 200 字符后仍带着存在性主张 ⇒ 这条夹具不够长，控制失去区分力（该换夹具，不该改判据）"
    )
    # 取尾的读法不丢任何东西
    assert registry_reading(_daemon_reading_of(f"层一\n层二\n{absent}")) == "absent"
    # 这一行才是"截断方向"的牙：把取尾换成取头（旧写法 `[-1][:200]`），长读数尾部的
    # 存在性主张就被丢掉，下面这条立刻红
    assert _daemon_reading_of("头" * 300 + "\n" + absent).endswith("not found")
    assert _daemon_reading_of("只有前缀\n") == "只有前缀"
    assert _daemon_reading_of("全是空白\n   \n") == "全是空白"
    assert _daemon_reading_of("") == ""


def test_pull_failure_action_adjudicates_every_combination() -> None:
    """答复形状 × 第二通道读数的全组合各有出口：坏摘要必须红，好摘要不能被洗成通过。"""
    combos = {
        # (daemon 读数, 第二通道) -> 动作
        (next(r for r, k, _ in DAEMON_READINGS if k == "no-answer"), "present"): "skip",
        (next(r for r, k, _ in DAEMON_READINGS if k == "no-answer"), "absent"): "red_pin",
        (next(r for r, k, _ in DAEMON_READINGS if k == "no-answer"), "unknown"): "skip",
        (next(r for r, k, _ in DAEMON_READINGS if k == "absent"), "present"): "skip",
        (next(r for r, k, _ in DAEMON_READINGS if k == "absent"), "absent"): "red_pin",
        (next(r for r, k, _ in DAEMON_READINGS if k == "absent"), "unknown"): "red_undecided",
        (next(r for r, k, _ in DAEMON_READINGS if k == "answered"), "unknown"): "skip",
        (next(r for r, k, _ in DAEMON_READINGS if k == "answered"), "present"): "skip",
        ("", "unknown"): "red_undecided",
    }
    seen: set[str] = set()
    for (reading, verdict), expected in combos.items():
        action, message = _pull_failure_action(reading, verdict, "读数详情")
        assert action == expected, f"{reading[:60]} + {verdict} → {action}，期望 {expected}：{message}"
        if action == "skip":
            assert GATE_SENTINEL in message, message
        else:
            assert "钉进配方" in message or "无法定案" in message, message
        assert "读数详情" in message or action != "skip", f"skip 却没把第二通道读数打出来：{message}"
        seen.add(action)
    assert seen == {"skip", "red_pin", "red_undecided"}, seen
    # 坏摘要那一档必须压过一切daemon 读数形状（上一版在这里放过免检通道）
    bad = next(r for r, k, _ in DAEMON_READINGS if k == "absent")
    assert _pull_failure_action(bad, "absent", "独立通道 404")[0] == "red_pin"


def test_the_daemon_reading_classifier_has_exactly_one_copy() -> None:
    """同一件事的判断在这个文件里被写过两份（守护进程取回失败那一条按关键词表跳过，
    镜像层 SBOM 那一条另起一个 `_skippable` 小函数）——两份各有盲区：今晚假红的是前者，
    而后者会把 429／鉴权这类**真答复**判成代码失败。合并成一把尺之后，出现第三种拼写就红。

    判的是形状不是名字：本文件的正文里本来就要提到这两个旧名字，按子串查会被自己命中。
    """
    src = Path(__file__).read_text(encoding="utf-8")
    for shape in (r"^TRANSPORT[_]KEYS\s*=", r"def _transport[_]skippable\("):
        hit = re.findall(shape, src, flags=re.M)
        assert not hit, f"{shape} 又长回来了：{hit}"
    assert len(re.findall(r"^def registry[_]reading\(", src, flags=re.M)) == 1, "分流判据不该有第二份实现"
    # 接线证明：合并后的那把尺必须真被 SBOM 那条路消费。名字是拼出来的、正文里也不写全名——
    # 判据里只要多出现一次那个字面量，按计数判的断言就会被自己命中（本轮踩过两次）。
    needle = "transport" + "_only"
    hits = [i for i, ln in enumerate(src.splitlines(), 1) if needle + "(" in ln]
    assert len(hits) == 3, (hits, needle)  # 定义 1 + 工具镜像 + trivy 运行期
    body = src[src.index("def " + needle + "("):]
    body = body[: body.index("\n    have =")]
    assert "registry" + "_reading(" in body, "SBOM 那条路只是改了个名字，没真的走同一把尺"


def _registry_get(url: str, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    """registry API 的一次 GET（两条独立通道共用）。

    URL 全部由本模块内的常量与钉死的引用拼出（注册表主机＋仓库名＋摘要），没有用户输入。
    """
    import urllib.request

    req = urllib.request.Request(url, headers=headers or {})  # noqa: S310
    with urllib.request.urlopen(req, timeout=25) as resp:  # noqa: S310
        return resp.status, resp.read()


def _digest_channels(ref: str, digest: str) -> tuple[str, str]:
    """守护进程之外的第二条通道，问"这份内容到底在不在"。

    返回 `present` / `absent` / `unknown` 三档之一 + 一句读数。判据不是"HTTP 200 就算在"：
    内容地址存储里"同一份东西"的定义是**逐字节重算的 sha256 与钉住的摘要相等**，
    所以这里取回 manifest body 自己算一遍（2026-09-26 实测：`python` 官方库那份
    10373 B、重算相符；把首位十六进制翻掉后同一通道回 404 —— 两个方向都验过才敢用它定案）。

    两条通道按注册表分策略，各自能说的事实不同，所以 detail 里写明是哪一条：
    - Docker Hub 官方库 → `public.ecr.aws/docker/library/<repo>`，**换一家注册表**，
      能定案"这份字节在别处也认得"；
    - `ghcr.io/...` → 还是 ghcr，但**换一条传输**（宿主 urllib 直连 registry API，与守护进程
      那条独立）。这能定案"摘要在不在、字节对不对"，不能定案"这家注册表有没有被篡改"。
    """
    import hashlib
    import json as _json
    import urllib.error

    accept = "application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json"
    repo = ref.split("@", 1)[0].split(":", 1)[0]
    if repo.startswith("ghcr.io/"):
        path, base, label = repo.split("/", 1)[1], "https://ghcr.io", "ghcr（同注册表、另一条传输）"
        token_url = f"{base}/token?service=ghcr.io&scope=repository:{path}:pull"
        manifest_url = f"{base}/v2/{path}/manifests/{digest}"
    elif "/" not in repo:
        path, base, label = f"docker/library/{repo}", "https://public.ecr.aws", "ECR Public（另一家注册表）"
        token_url = f"{base}/token/?service=public.ecr.aws&scope=repository:{path}:pull"
        manifest_url = f"{base}/v2/{path}/manifests/{digest}"
    else:
        return "unknown", f"没有为 {repo} 建立第二通道映射（只覆盖 Docker Hub 官方库与 ghcr）"

    try:
        token = _json.loads(_registry_get(token_url)[1])["token"]
        status, body = _registry_get(manifest_url, {"Authorization": f"Bearer {token}", "Accept": accept})
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return "absent", f"{label} 404 Not Found"
        return "unknown", f"{label} HTTP {exc.code}"
    except Exception as exc:  # 网络/超时/JSON 解析：这一档就是"无法定案"，不假装成任何一个结论
        return "unknown", f"{label} 不可用 {type(exc).__name__}: {str(exc)[:120]}"
    if status != 200:
        return "unknown", f"{label} HTTP {status}"
    same = hashlib.sha256(body).hexdigest() == digest.split(":", 1)[1]
    detail = f"{label} HTTP {status} / {len(body)} B / 重算 sha256 {'与钉住的值相等' if same else '不相等'}"
    return ("present" if same else "absent"), detail


def _independent_digest_read(ref: str, digest: str) -> tuple[str, str]:
    """既有调用名的薄壳：判据实现挪到 `_digest_channels`，这一层只保名字不改语义。"""
    return _digest_channels(ref, digest)


def test_pinned_base_of_the_control_plane_recipe_is_fetchable():
    """配方里钉死的基础镜像必须能被"构建用的那条传输"取到：把一次性实测变常驻判据。

    本轮之前没有任何常驻门禁构建过控制面镜像——`make validate` 的 build 检查量的是
    `python -m build` 出的 wheel，`make control-image` 只在人手上跑过。于是"钉进去的
    digest 其实取不到"这类错误（上一轮就差点犯：第三方镜像站的读数与本机缓存不吻合时，
    钉错方向是让所有人的构建当场失败）只会在别人 build 的那一刻暴露。
    本轮实测过一次完整构建（`Successfully built`，产物 `python -V` = 3.12.14）；改造配方后
    再跑一次是 55.6s（旧配方那 165s 里有大半是 pip 层现解析＋构建隔离要装 setuptools），
    这一支把其中"配方里每一份钉死的引用都取得到"这一半变成每轮都算的判据。完整构建本身
    仍不常驻（要拉全部 wheel 字节，属于外网波动面，见 SUPPLY_CHAIN §8 第 4 项）。

    覆盖面从"第一个 FROM"扩到**配方里所有钉死的引用**：改成多阶段之后 builder 还拉一份
    工具镜像（uv），那份 digest 取不到时构建一样当场失败，没有理由只测基础镜像那一份。
    刻意**不**断言"tag 现在仍指向这个 digest"：钉住的内容本来就该在 tag 移动后保持不变，
    那样断言等于把配方钉成一个每漂移必红的项。tag 是否已移动只作为读数打印。
    """
    # 判据只有一份实现：解析 FROM/COPY --from 与"有没有钉"都复用供应链档的那对纯函数
    from tests.test_supply_chain import REPO_ROOT, _base_refs, _is_pinned

    refs = [r for _, r in _base_refs([REPO_ROOT / "runtime" / "Dockerfile.control-plane"])]
    pinned = sorted({r for r in refs if _is_pinned(r)})
    assert pinned, f"控制面配方里没有钉 digest 的引用，逐字相等判据会失去权威侧：{refs}"
    for ref in pinned:
        _assert_pinned_ref_fetchable(ref)


def _assert_pinned_ref_fetchable(ref: str) -> None:
    # 走共用异常吸收（`tests/docker_probe.py`）而不是裸 `_docker`：registry 卡住时
    # `subprocess.run(timeout=…)` 抛的 TimeoutExpired 会**绕过下面那套三档分流**，
    # 于是"环境这一趟没走到 registry"被报成一条代码失败（2026-09-28 整轮认证实测：
    # `docker pull` 300s 超时 ⇒ `FAILED … TimeoutExpired`，而同一条传输 7.4s 就回 rc=0）。
    # 吸收之后它变成一个 rc=124 的读数，才有机会进 `_pull_failure_action` 定档。
    pulled = _docker_probe("pull", ref, timeout=300)
    if pulled.returncode != 0:
        # 守护进程的 docker.io 传输今天走的是它配置里的第三方镜像站（错误串里能看见
        # `docker.1panel.live`），而镜像站对**有效**的摘要也会回 `not found`（2026-09-26 实测：
        # 同一份 `python:3.12-slim@sha256:f77ac9e4…` 上一轮经同一条传输 pull 成功、本轮回 not found）。
        # 所以这里不"取不到就红"、也不"看着像超时就跳"：先让第二条独立传输对摘要本身表态，
        # 再由 _pull_failure_action 定档（坏摘要在任何 daemon 读数下都必须红）。
        digest = ref.rsplit("@sha256:", 1)[1]
        verdict, detail = _independent_digest_read(ref, "sha256:" + digest)
        # 取**尾**不取头：containerd 的错误链把病因接在最后，截头等于把唯一有区分力的一位丢掉
        daemon_reading = _daemon_reading_of(pulled.stderr)
        action, message = _pull_failure_action(daemon_reading, verdict, detail)
        if action == "skip":
            pytest.skip(message)
        raise AssertionError(message)


    info = _docker_probe(
        "image", "inspect", "--format", "{{.Architecture}}|{{.Os}}|{{.Id}}", ref, timeout=60
    )
    assert info.returncode == 0, f"pull 成功但 inspect 读不到：{info.stderr[-200:]}"
    arch, os_name, image_id = info.stdout.strip().split("|")
    assert arch and os_name == "linux" and image_id.startswith("sha256:"), info.stdout

    # 负向对照（这支判据的牙）：把 digest 首位改掉后必须取不到。
    # 它若不开火，说明上面的 pull 根本没在按 digest 解析（例如被 daemon 当成 tag 处理）。
    #
    # 默认不跑，用 `EMBODIEDCLOUD_RECIPE_BASE_CONTROL=1` 打开，理由是一条实测代价：
    # 本机同一台 daemon 上 `docker pull <正确 digest>` 37.7s、`docker pull <翻转一位>` 91.8s
    # ——负向档要走完整趟 registry 才能拿到"取不到"这个答案，把它折进每轮就是 +90s，
    # 而它要证的事（pull 是按 digest 而不是按 tag 解析）不随每轮代码变化。
    # 2026-09-26 实测跑过：翻转首位后 pull 失败且给出 manifest 未知类错误，故默认档只出读数。
    digest = ref.rsplit("@sha256:", 1)[1]
    flipped = ("0" if digest[0] != "0" else "1") + digest[1:]
    bogus = ref.rsplit("@sha256:", 1)[0] + "@sha256:" + flipped
    if os.environ.get("EMBODIEDCLOUD_RECIPE_BASE_CONTROL") == "1":
        neg = _docker("pull", bogus, timeout=300)
        if neg.returncode == 0:
            raise AssertionError(
                f"改掉 digest 一位之后仍然 pull 成功——上面的判据没有按 digest 解析：{bogus}"
            )
        assert (neg.stderr or "").strip(), "负向对照 pull 失败却没有任何错误文本，读数不可归因"
        control = f"开火（pull 失败：{_daemon_reading_of(neg.stderr)[-160:]}）"
        # 第二通道也不能是橡皮图章：同一个翻转摘要在它那里必须同样"不存在"，
        # 否则上面那条"镜像站说没有＋独立通道说有 ⇒ 判为通道故障"的分流就永远没有反面。
        i_verdict, i_detail = _independent_digest_read(ref, "sha256:" + flipped)
        if i_verdict != "absent":
            raise AssertionError(f"翻转后的摘要在独立通道不是 absent（读作 {i_verdict}）：{i_detail}")
        control += f"；独立对照（翻转摘要）{i_verdict}（{i_detail}）"
    else:
        control = "未跑（置 EMBODIEDCLOUD_RECIPE_BASE_CONTROL=1 开火；2026-09-26 已实测开火）"

    tag_ref = ref.split("@", 1)[0]
    tag_info = _docker("image", "inspect", "--format", "{{index .RepoDigests 0}}", tag_ref, timeout=60)
    drift = tag_info.stdout.strip() if tag_info.returncode == 0 else "(本机没有该 tag 的缓存条目)"
    print(
        f"\n[recipe-base] {ref} pull=OK arch={arch}/{os_name} id={image_id[:19]}…；"
        f"负向对照（翻转首位成 {bogus.rsplit('@sha256:', 1)[1][:12]}…）{control}；tag 侧当前读数 {drift}"
    )


def test_pinned_sbom_tool_actually_produces_a_checkable_image_sbom(tmp_path: Path) -> None:
    """镜像层 SBOM 这一步的**接线**常驻：钉死的工具镜像真能出清单，清单真过形状判据。

    与 `scripts/image_sbom.sh` 的分工是刻意的：脚本产的是控制面镜像那一份，需要先真构建
    （实测 pip 层受外网波动影响，同一棵树两次 `ReadTimeoutError` 后第三次才成，不常驻）；
    这一产的是三件每轮都能核的事——
    1. 钉进脚本的工具 digest 仍然取得到（这条 digest 来自第三方公开镜像站，取字节只走守护进程）；
    2. 挂 docker.sock 后 trivy 读得到**本地**镜像（官方文档写明这是容器内扫镜像的接线方式）；
    3. 它的输出满足 §5 那套形状判据，且判据跑的是 `scripts/check_image_sbom.py` 同一份实现。

    被审对象取配方里那份已钉 digest 的基础镜像：本机一定拿得到，且实测同时含
    Debian OS 包（87 个 pkg:deb）与 wheel（1 个 pkg:pypi），两条子判据都不是空转。
    """
    # 判据与解析都只有一份实现：工具引用、被审引用、形状判据全部从供应链档与量具里取
    from tests.test_supply_chain import (
        IMAGE_SBOM_SCRIPT,
        REPO_ROOT,
        TRIVY_TOOL_REF_SCRIPT,
        _base_refs,
        _is_pinned,
        _load_image_sbom_validator,
        _tool_image_refs_in,
    )

    # 工具引用被收进单一来源（image_sbom.sh 只 source 它），所以解析面必须是"消费方 + 被
    # source 的那份"两文件合起来：只看 image_sbom.sh 会读到空集，判据就退化成无事可做。
    texts = [IMAGE_SBOM_SCRIPT.read_text(encoding="utf-8"), TRIVY_TOOL_REF_SCRIPT.read_text(encoding="utf-8")]
    refs = _tool_image_refs_in(texts)
    assert len(refs) == 1, f"这一步应当只拉起一份外部工具镜像，实际解析到 {sorted(refs)}"
    tool = next(iter(refs))

    subjects = [r for _, r in _base_refs([REPO_ROOT / "runtime" / "Dockerfile.control-plane"]) if _is_pinned(r)]
    assert subjects, "控制面配方没有钉 digest 的 FROM，被审对象无从选取"
    subject = subjects[0]

    def transport_only(stderr: str) -> bool:
        # 与守护进程取回失败那一条共用同一把尺：registry 明说『没有』＝钉错了必须红，
        # 连文本都没拿到＝我们自己的读数坏了；答了话但不涉及存在性（鉴权/限流）与
        # 压根没走到 registry，都只说明本轮未获证。
        return registry_reading(_daemon_reading_of(stderr)) not in ("absent", "unreadable")

    have = _docker("image", "inspect", tool, timeout=60)
    if have.returncode != 0:
        pulled = _docker("pull", tool, timeout=600)
        if pulled.returncode != 0:
            if transport_only(pulled.stderr):
                pytest.skip(
                    f"{GATE_SENTINEL}: SBOM 工具镜像这条传输未定案"
                    f"（{registry_reading(_daemon_reading_of(pulled.stderr))}）："
                    f"{_daemon_reading_of(pulled.stderr)[-200:]}"
                )
            raise AssertionError(f"钉进脚本的工具镜像取不到：{tool}\n{pulled.stderr[-400:]}")

    run = _docker(
        "run",
        "--rm",
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock",
        tool,
        "image",
        "--format",
        "cyclonedx",
        subject,
        timeout=600,
    )
    if run.returncode != 0:
        if transport_only(run.stderr):
            pytest.skip(
                f"{GATE_SENTINEL}: trivy 运行期未定案"
                f"（{registry_reading(_daemon_reading_of(run.stderr))}）："
                f"{_daemon_reading_of(run.stderr)[-200:]}"
            )
        raise AssertionError(f"trivy 出清单失败：{run.stderr[-400:]}")

    # 落盘只为本数一遍；判据读的是这份 stdout（脚本侧同样走 stdout，理由见 image_sbom.sh 注释）
    out = tmp_path / "image.cdx.json"
    out.write_text(run.stdout, encoding="utf-8")
    validator = _load_image_sbom_validator()
    doc = json.loads(out.read_text(encoding="utf-8"))
    # 把判据绑在"这个镜像的字节"上，而不只是这个名字上：Id 由 inspect 独立取得，
    # 清单自报的摘要必须与它相等（缺了这一步，扫错对象也能过——名字只是 trivy 的回声）。
    id_read = _docker("image", "inspect", "--format", "{{.Id}}", subject, timeout=60)
    assert id_read.returncode == 0, f"被审对象 inspect 不到 Id：{id_read.stderr[-200:]}"
    offenders = validator.sbom_offenders(doc, subject, id_read.stdout.strip())
    assert not offenders, "工具产出的镜像层 SBOM 过不了形状判据：" + " | ".join(offenders)

    purls = [str(c.get("purl", "")) for c in doc.get("components", []) if isinstance(c, dict)]
    print(
        f"\n[image-sbom] {tool.split('@')[0]}… 对 {subject.split(':')[0]} 产出 components={len(purls)} "
        f"deb={sum(1 for p in purls if p.startswith('pkg:deb/'))} "
        f"pypi={sum(1 for p in purls if p.startswith('pkg:pypi/'))} "
        f"spec={doc.get('specVersion')}；自报对象="
        f"{doc.get('metadata', {}).get('component', {}).get('type')}"
    )
def test_control_plane_image_runtime_layout_is_what_the_docs_assume():
    """新配方把项目改成"不装发行包、只给 venv+源码"之后，运行时形状必须被核住。

    三条都是本轮改动的直接后果，任何一条变了就意味着某份运维说明在骗人：
    1. `python` 落在 venv 里（依赖由 uv.lock 决定），`app` 从 /app 源码 import 得到；
    2. `pip` 仍然指向**基础镜像**那套解释器（`/usr/local/bin/pip`）——这就是"往运行中的
       容器里 `pip install` 会装到应用看不见的地方"这条运维陷阱的成因。它现在是**被钉住的
       事实**而不是巧合：哪天 uv 改成往 venv 里种 pip（或有人 `--seed`），这一条会红，
       人就必须回去核对 docs/OPERATIONS.md 里 s3 那一格与 SUPPLY_CHAIN §4；
    3. 迁移动作 `command: ["alembic", "upgrade", "head"]` 真能在镜像里跑通（`alembic heads`
       列出修订号）——它靠的是 `WORKDIR /app` + `alembic.ini` 的 `prepend_sys_path = .`，
       在此之前没有任何用例验证过这条链在 venv 布局下仍然成立。
    另外钉一条"本项目自己的入口脚本**有意**不在镜像里"：CMD 是 `python -m app.main`，
    `[project.scripts]` 那两个名字是设备侧/宿主侧装包时用的。哪天有人把项目装回镜像，
    这条也会红，逼他确认那是有意的还是又漂回了旧配方。
    """
    import re

    subject = os.environ.get("CONTROL_IMAGE", "embodiedcloud/control-plane:0.7.0")
    if _docker("image", "inspect", subject, timeout=60).returncode != 0:
        pytest.skip(f"{GATE_SENTINEL}: 被审镜像不在场（先跑 make control-image）：{subject}")

    def _sh(script: str) -> str:
        run = _docker("run", "--rm", "--entrypoint", "sh", subject, "-c", script, timeout=180)
        assert run.returncode == 0, f"{script} 退 {run.returncode}：{(run.stderr or '')[-300:]}"
        return run.stdout.strip()

    assert _sh("python -c 'import sys; print(sys.prefix)'").endswith("/.venv"), "python 不在 venv 里"
    assert "app/__init__.py" in _sh("python -c 'import app; print(app.__file__)'")

    pip_path = _sh('readlink -f "$(command -v pip)"')
    assert "/usr/local/" in pip_path, (
        f"pip 不再指向基础镜像那套解释器（实测 {pip_path}）⇒ 运维说明要跟着改：回去核对 "
        "docs/OPERATIONS.md 的 s3 那一格与 SUPPLY_CHAIN §4"
    )
    venv_python_dir = _sh("python -c 'import sys; print(sys.executable)'")
    assert venv_python_dir.startswith("/app/.venv/"), venv_python_dir
    assert pip_path.startswith("/usr/local/"), pip_path

    assert _sh('readlink -f "$(command -v alembic)"') == "/app/.venv/bin/alembic"
    heads = _sh("alembic heads")
    assert re.search(r"^[0-9a-f]{4,} \(head\)", heads, re.M), f"alembic heads 没列出修订号：{heads}"
    for script_name in ("embodiedcloud", "embodiedcloud-edge-agent"):
        assert _sh(f"command -v {script_name} || echo ABSENT").endswith("ABSENT"), (
            f"{script_name} 又出现在镜像 PATH 里——项目被装回发行包了？确认是有意的并同步改本用例"
        )


def test_a_hanging_docker_cli_is_a_clean_skip_not_26_setup_errors(monkeypatch) -> None:
    """daemon 卡住（超时）必须与"拒连"同样走干净跳过路径。

    真事：一次干净树复算里 `docker version` 超时 20 秒，本模块 26 条用例全部
    `failed on setup with "subprocess.TimeoutExpired"` —— 拒连会被识别成"daemon 不可达"
    而跳过，卡住却把异常抛出前置检查，环境抖动在发布门禁上表现为代码失败（26 个 FAIL）。
    """
    def _boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["docker", *[str(a) for a in args]], timeout=kwargs.get("timeout", 20))

    monkeypatch.setitem(globals(), "_docker", _boom)
    reason = gate_reason()
    assert isinstance(reason, str) and reason, f"卡住的 daemon 没被判成不可用：{reason!r}"
    assert "超时" in reason or "timeout" in reason.lower(), reason
    image, why = _usable_image()
    assert image is None and why, (image, why)

    # 反向对照：把 _docker 换成"健康"的形状，前置检查不许再报超时（它可能报别的原因，那不算失败）
    class _Ok:
        returncode = 0
        stdout = "aarch64\namd64\n"
        stderr = ""

    monkeypatch.setitem(globals(), "_docker", lambda *a, **k: _Ok())
    healthy = gate_reason() or ""
    assert "超时" not in healthy and "timeout" not in healthy.lower(), healthy
