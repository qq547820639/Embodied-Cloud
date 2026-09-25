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

import os
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


def _daemon_arch() -> str:
    raw = _docker("info", "--format", "{{.Architecture}}").stdout.strip()
    return {"aarch64": "arm64", "x86_64": "amd64"}.get(raw, raw)


def _image_arch(image: str) -> str:
    return _docker("image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", image).stdout.strip()


def _usable_image() -> tuple[str | None, str]:
    """返回 (可用的测试镜像, 不可用原因)。"""
    arch = _daemon_arch()
    candidates = (IMAGE,) if IMAGE else IMAGE_CANDIDATES
    for cand in candidates:
        if not cand:
            continue
        if _docker("image", "inspect", cand, timeout=20).returncode != 0:
            continue
        image_arch = _image_arch(cand)
        if image_arch.endswith(arch):
            return cand, ""
    return None, f"没有本地缓存且架构匹配（daemon={arch}）的测试镜像，试过：{', '.join(candidates)}"


def gate_reason() -> str | None:
    if shutil.which("docker") is None:
        return "docker CLI 不可用"
    if _docker("version", timeout=20).returncode != 0:
        return "docker daemon 不可达"
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
    assert provider.wait_ready(workspace, Template(), timeout_seconds=5) is False
    assert provider.wait_ready(ws(None), Template(), timeout_seconds=1) is False


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
