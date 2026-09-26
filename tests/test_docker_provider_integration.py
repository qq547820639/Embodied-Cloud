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


def _pull_verdict_action(daemon_reading: str, verdict: str, detail: str) -> tuple[str, str]:
    """把"守护进程取不到 + 第二条通道的读数"映射成 (动作, 给读者看的一句话)。

    纯函数是为了能被常驻用例逐档打靶（这一支判据今晚真被镜像站的一次 `not found` 打过，
    但下一轮多半是绿的——不把它抽出来，分流逻辑就只能等下一次故障才第一次运行）。
    动作只有三种：`skip`（通道故障，主张未获证）、`red_pin`（两条传输都说没有＝钉错了）、
    `red_undecided`（两条通道一条说没有、另一条自己也不通＝无法定案，留着别洗）。
    """
    if verdict == "present":
        return (
            "skip",
            f"{GATE_SENTINEL}: 守护进程这条传输答 not found，第二条传输确认该摘要存在（{detail}）"
            f"⇒ 判为通道故障而非配方错误，主张本轮未获证。daemon 读数：{daemon_reading}",
        )
    if verdict == "absent":
        return "red_pin", f"两条独立传输都说这份摘要不存在 ⇒ 钉进配方的引用是错的：{daemon_reading}；{detail}"
    return "red_undecided", f"守护进程取不到、独立通道也无法定案（{detail}）：{daemon_reading}"


def test_independent_digest_read_discriminates_present_from_absent() -> None:
    """定案用的那条第二通道必须自己会分正反：真摘要＝present、翻一位＝absent。

    它若对坏摘要也回 present，上面那档 `skip` 就成了免检通道——任何钉错的 digest 都能靠
    "镜像站说不认识＋这条通道说存在"变成永久跳过。三档各有出口，`unknown` 走前提缺失。
    """
    from tests.test_supply_chain import REPO_ROOT, _base_refs, _is_pinned

    pinned = [r for _, r in _base_refs([REPO_ROOT / "runtime" / "Dockerfile.control-plane"]) if _is_pinned(r)]
    assert pinned, "没有钉 digest 的 FROM，无法取真摘要做对照"
    ref = pinned[0]
    digest = ref.rsplit("@sha256:", 1)[1]

    good_verdict, good_detail = _independent_digest_read(ref, "sha256:" + digest)
    if good_verdict == "unknown":
        pytest.skip(f"{GATE_SENTINEL}: 第二通道本身不可达，无法给这支对照定档：{good_detail}")
    assert good_verdict == "present", f"配方里钉着的真摘要在第二通道不是 present：{good_verdict} / {good_detail}"

    flipped = ("0" if digest[0] != "0" else "1") + digest[1:]
    bad_verdict, bad_detail = _independent_digest_read(ref, "sha256:" + flipped)
    assert bad_verdict == "absent", f"翻掉一位的摘要竟然不是 absent（{bad_verdict} / {bad_detail}）⇒ 分流逻辑不可信"

    # 映射不出的仓库必须落到 unknown，不能靠猜把别家的名字拼到官方库前缀上
    other = "registry.example.com/team/app:1.0.0@sha256:" + digest
    assert _independent_digest_read(other, "sha256:" + digest)[0] == "unknown"


def test_pull_verdict_action_has_three_distinct_exits() -> None:
    """三种读数组合各自映射到不同动作，且都不是一句空话（消息里必须带两条读数）。"""
    skip_msg = _pull_verdict_action("daemon: not found", "present", "HTTP 200 / 10373 B")
    red_pin = _pull_verdict_action("daemon: not found", "absent", "独立通道 404 Not Found")
    undecided = _pull_verdict_action("daemon: not found", "unknown", "独立通道不可用 URLError")
    assert skip_msg[0] == "skip" and GATE_SENTINEL in skip_msg[1], skip_msg
    assert red_pin[0] == "red_pin" and "钉进配方的引用是错的" in red_pin[1], red_pin
    assert undecided[0] == "red_undecided" and "无法定案" in undecided[1], undecided
    assert len({skip_msg[0], red_pin[0], undecided[0]}) == 3


def _independent_digest_read(ref: str, digest: str) -> tuple[str, str]:
    """绕开守护进程那条传输，问另一个注册表："这份内容到底在不在"。

    返回 `present` / `absent` / `unknown` 三档之一 + 一句读数。判据不是"HTTP 200 就算在"：
    内容地址存储里"同一份东西"的定义是**逐字节重算的 sha256 与钉住的摘要相等**，
    所以这里取回 manifest body 自己算一遍（2026-09-26 实测：`python` 官方库那份
    10373 B、重算相符；把首位十六进制翻掉后同一通道回 404 —— 两个方向都验过才敢用它定案）。
    """
    import hashlib
    import json as _json
    import urllib.error
    import urllib.request

    repo = ref.split("@", 1)[0].split(":", 1)[0]
    if "/" in repo:
        return "unknown", f"没有为 {repo} 建立第二通道映射（只覆盖 Docker Hub 官方库）"
    base = "https://public.ecr.aws"
    scope = f"repository:docker/library/{repo}:pull"
    accept = "application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json"

    def _get(url: str, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
        # S310: URL 由本函数外的常量拼出（public.ecr.aws + 仓库名 + 摘要），无用户输入
        req = urllib.request.Request(url, headers=headers or {})  # noqa: S310
        with urllib.request.urlopen(req, timeout=25) as resp:  # noqa: S310
            return resp.status, resp.read()

    try:
        token = _json.loads(_get(f"{base}/token/?service=public.ecr.aws&scope={scope}")[1])["token"]
        status, body = _get(
            f"{base}/v2/docker/library/{repo}/manifests/{digest}",
            {"Authorization": f"Bearer {token}", "Accept": accept},
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return "absent", "独立通道 404 Not Found"
        return "unknown", f"独立通道 HTTP {exc.code}"
    except Exception as exc:  # 网络/超时/JSON 解析：这一档就是"无法定案"，不假装成任何一个结论
        return "unknown", f"独立通道不可用 {type(exc).__name__}: {str(exc)[:120]}"
    if status != 200:
        return "unknown", f"独立通道 HTTP {status}"
    same = hashlib.sha256(body).hexdigest() == digest.split(":", 1)[1]
    detail = f"独立通道 HTTP {status} / {len(body)} B / 重算 sha256 {'与钉住的值相等' if same else '不相等'}"
    return ("present" if same else "absent"), detail


def test_pinned_base_of_the_control_plane_recipe_is_fetchable():
    """配方里钉死的基础镜像必须能被"构建用的那条传输"取到：把一次性实测变常驻判据。

    本轮之前没有任何常驻门禁构建过控制面镜像——`make validate` 的 build 检查量的是
    `python -m build` 出的 wheel，`make control-image` 只在人手上跑过。于是"钉进去的
    digest 其实取不到"这类错误（上一轮就差点犯：第三方镜像站的读数与本机缓存不吻合时，
    钉错方向是让所有人的构建当场失败）只会在别人 build 的那一刻暴露。
    本轮实测过一次完整构建（`Step 1/10 : FROM python:3.12-slim@sha256:f77ac9e4…`，
    `Successfully built`，产物 `python -V` = 3.12.14，约 160s），这一支把其中"基础镜像取得到"
    这一半变成每轮都算的判据；完整构建因 pip 层要重装（实测约 2.5 分钟）不常驻，如实记下。

    刻意**不**断言"tag 现在仍指向这个 digest"：钉住的内容本来就该在 tag 移动后保持不变，
    那样断言等于把配方钉成一个每漂移必红的项。tag 是否已移动只作为读数打印。
    """
    # 判据只有一份实现：解析 FROM 与"有没有钉"都复用供应链档的那对纯函数
    from tests.test_supply_chain import REPO_ROOT, _base_refs, _is_pinned

    refs = [r for _, r in _base_refs([REPO_ROOT / "runtime" / "Dockerfile.control-plane"])]
    pinned = [r for r in refs if _is_pinned(r)]
    assert pinned, f"控制面配方里没有钉 digest 的 FROM，逐字相等判据会失去权威侧：{refs}"
    ref = pinned[0]

    pulled = _docker("pull", ref, timeout=300)
    if pulled.returncode != 0:
        low = (pulled.stderr or "").lower()
        if any(k in low for k in ("timeout", "dial tcp", "no such host", "connection refused", "unreachable")):
            pytest.skip(f"{GATE_SENTINEL}: 基础镜像引用无法核验（registry 不可达）：{pulled.stderr[-200:]}")
        # 守护进程的 docker.io 传输今天走的是它配置里的第三方镜像站（错误串里能看见
        # `docker.1panel.live`），而镜像站对**有效**的摘要也会回 `not found`（2026-09-26 实测：
        # 同一份 `python:3.12-slim@sha256:f77ac9e4…` 上一轮经同一条传输 pull 成功、本轮回 not found）。
        # 所以这里不能"取不到就红"，也不能"取不到就跳"——用第二条独立传输定案，
        # 三个方向各有出口：两边都说没有＝钉错了，必须红；镜像站说没有而独立通道逐字节确认有
        # ＝通道故障，报"前提未达成 + 两条读数"；独立通道自己也不通＝无法定案，如实留红。
        digest = ref.rsplit("@sha256:", 1)[1]
        verdict, detail = _independent_digest_read(ref, "sha256:" + digest)
        daemon_reading = (pulled.stderr or "").strip().splitlines()[-1][:180]
        action, message = _pull_verdict_action(daemon_reading, verdict, detail)
        if action == "skip":
            pytest.skip(message)
        raise AssertionError(message)


    info = _docker("image", "inspect", "--format", "{{.Architecture}}|{{.Os}}|{{.Id}}", ref, timeout=60)
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
        control = f"开火（pull 失败：{neg.stderr.strip().splitlines()[-1][:120]}）"
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
        _base_refs,
        _is_pinned,
        _load_image_sbom_validator,
        _tool_image_refs,
    )

    refs = _tool_image_refs(IMAGE_SBOM_SCRIPT.read_text(encoding="utf-8"))
    assert refs, f"{IMAGE_SBOM_SCRIPT.name} 里解析不到工具镜像赋值，这支判据会无事可做"
    tool = sorted(refs)[0]

    subjects = [r for _, r in _base_refs([REPO_ROOT / "runtime" / "Dockerfile.control-plane"]) if _is_pinned(r)]
    assert subjects, "控制面配方没有钉 digest 的 FROM，被审对象无从选取"
    subject = subjects[0]

    def _transport_skippable(stderr: str) -> bool:
        low = (stderr or "").lower()
        return any(k in low for k in ("timeout", "dial tcp", "no such host", "connection refused", "unreachable"))

    have = _docker("image", "inspect", tool, timeout=60)
    if have.returncode != 0:
        pulled = _docker("pull", tool, timeout=600)
        if pulled.returncode != 0:
            if _transport_skippable(pulled.stderr):
                pytest.skip(f"{GATE_SENTINEL}: SBOM 工具镜像取不到（registry 通道不可达）：{pulled.stderr[-200:]}")
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
        if _transport_skippable(run.stderr):
            pytest.skip(f"{GATE_SENTINEL}: trivy 运行期通道不可达：{run.stderr[-200:]}")
        raise AssertionError(f"trivy 出清单失败：{run.stderr[-400:]}")

    # 落盘只为本数一遍；判据读的是这份 stdout（脚本侧同样走 stdout，理由见 image_sbom.sh 注释）
    out = tmp_path / "image.cdx.json"
    out.write_text(run.stdout, encoding="utf-8")
    validator = _load_image_sbom_validator()
    doc = json.loads(out.read_text(encoding="utf-8"))
    offenders = validator.sbom_offenders(doc, subject)
    assert not offenders, "工具产出的镜像层 SBOM 过不了形状判据：" + " | ".join(offenders)

    purls = [str(c.get("purl", "")) for c in doc.get("components", []) if isinstance(c, dict)]
    print(
        f"\n[image-sbom] {tool.split('@')[0]}… 对 {subject.split(':')[0]} 产出 components={len(purls)} "
        f"deb={sum(1 for p in purls if p.startswith('pkg:deb/'))} "
        f"pypi={sum(1 for p in purls if p.startswith('pkg:pypi/'))} "
        f"spec={doc.get('specVersion')}；自报对象="
        f"{doc.get('metadata', {}).get('component', {}).get('type')}"
    )

