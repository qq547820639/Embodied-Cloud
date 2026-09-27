import os
import secrets
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

from ...config import Settings
from ...models import Template, Workspace
from ..ports import allocate_tcp_port, is_port_free, release_tcp_port
from .base import ProvisionResult, ResourceReservation, RuntimeState


class DockerProvider:
    """Single-host NVIDIA GPU runtime for the first sellable EmbodiedCloud release.

    GPU authority: GpuScheduler 是唯一决策入口，provision 必须收到
    ResourceReservation 并严格按其 gpu_index 绑定（--gpus device=N）。
    本类不包含任何 GPU allocator（禁止自行选择 GPU）。

    Policy decisions are deliberately conservative:
    - one running workspace per physical GPU;
    - code-server gets an independent TCP port;
    - Isaac Lab public WebRTC uses NVIDIA's current default ports (49100/TCP,
      47998/UDP), so this provider permits at most one streaming workspace per
      host until a validated multi-instance streaming gateway is introduced.
    """

    name = "docker"
    WEBRTC_SIGNAL_PORT = 49100
    WEBRTC_MEDIA_PORT = 47998

    def __init__(self, settings: Settings):
        self.settings = settings
        # 进程内 IDE 端口分配登记（workspace_id → ide_port）：destroy 时释放回
        # ports 池，消除异步 docker run 期间的串行 TOCTOU 重复分配。
        self._allocated_ide_ports: dict[str, int] = {}

    def _run(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        # S603: 仅执行受控常量参数（docker CLI），不包含用户输入。
        return subprocess.run(args, text=True, capture_output=True, check=check)  # noqa: S603

    def health(self) -> tuple[bool, str]:
        if shutil.which("docker") is None:
            return False, "docker CLI not found"
        try:
            docker = self._run(["docker", "info", "--format", "{{json .ServerVersion}}"])
        except Exception as exc:
            return False, f"docker daemon unavailable: {exc}"
        if shutil.which("nvidia-smi") is None:
            return False, "docker ready but nvidia-smi not found"
        try:
            gpu = self._run(["nvidia-smi", "--query-gpu=index,name,memory.total", "--format=csv,noheader,nounits"])
            gpu_line = gpu.stdout.strip().splitlines()[0] if gpu.stdout.strip() else "no GPU"
        except Exception as exc:
            return False, f"docker ready but NVIDIA GPU unavailable: {exc}"
        return True, f"docker {docker.stdout.strip()} / GPU {gpu_line}"

    def _name(self, workspace: Workspace) -> str:
        """容器名的唯一口径：DB 列优先，列没落上就按命名约定 `ec-{id[:12]}` 推导。

        provision 是"先 `docker run --name ec-…` 建容器、后把名字持久化"，中间崩了就留下
        一个有名字但列里为空的活容器。改前 inspect/reconcile/start/stop 直接读那一列并把空列
        当成"不存在"或干脆空转，而 destroy/provision/pull_artifact 按约定推导 ⇒ 同一个容器
        在两套逻辑下既是活的又是没的（N-70）。缺席与否只能由引擎回答。
        """
        return workspace.container_name or f"ec-{workspace.id[:12]}"

    # 引擎"答不上来"分两种，本机 docker CLI 实测同一 rc=1、只靠 stderr 分：
    #   不存在          → "error: no such object: <name>"
    #   守护进程连不上  → "Cannot connect to the Docker daemon at <host>. Is the docker daemon running?"
    # 借 docker-py 的类型分层（`docker/errors.py:93` 把 404 单独收成 NotFound(APIError)，
    # 而连接失败根本不是 APIError）：把两者混成"absent"会让守护进程一断线就把所有
    # workspace 判成"runtime 已不在"，于是连 `_release_admitted(command_succeeded=False)`
    # 那档"只认 MISSING"也照样放行放卡。
    _ABSENT_NEEDLES = ("no such object", "no such container")

    def _absent_or_unknown(self, stderr: str | None, name: str) -> dict:
        """只有引擎亲口说不存在才算不存在；问不到／答不上是 unknown。"""
        text = (stderr or "").lower()
        if any(needle in text for needle in self._ABSENT_NEEDLES):
            return {"state": "absent", "container_name": name}
        return {"state": "unknown", "container_name": name}

    def _container_exists(self, workspace: Workspace) -> bool:
        name = self._name(workspace)
        result = self._run(
            ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
            check=False,
        )
        return name in result.stdout.splitlines()

    def _run_checked(self, args: list[str], *, container_name: str) -> None:
        """执行容器生命周期命令并校验 returncode。

        容器不存在（幂等成功）时静默返回；其他失败（如 docker daemon 不可用）
        上抛 RuntimeError —— 调用方据此阻断「置 DELETED + 释放 GPU」，由 reconcile
        重试，避免孤儿容器仍持有 --gpus device=N 造成一卡双跑。

        "不存在"的判法两档共用 `_absent_or_unknown` 那一份名字表（N-70）：先看命令自己的
        文案，再用 inspect 二次确认；inspect 也问不到（daemon 不可达）就判真实失败上抛。
        """
        result = self._run(args, check=False)
        if result.returncode == 0:
            return
        output = f"{result.stdout or ''}\n{result.stderr or ''}"
        if self._absent_or_unknown(output, container_name)["state"] == "absent":
            return  # 容器已不存在：幂等成功
        # 兜底：文案随 CLI 版本而变，用 inspect 二次确认容器是否确实不存在。
        # inspect 自身失败（如 daemon 不可用）时无法判定 absent → 视为真实失败上抛。
        inspect = self._run(
            ["docker", "inspect", "--format", "{{.Id}}", container_name], check=False
        )
        if inspect.returncode == 0:
            # 容器仍存在 → 命令确实失败
            raise RuntimeError(
                f"docker command failed: {' '.join(args)} "
                f"(rc={result.returncode}): {output.strip()[:400]}"
            )
        if self._absent_or_unknown(inspect.stderr, container_name)["state"] == "absent":
            return  # 容器确实不存在：幂等成功
        raise RuntimeError(
            f"docker command failed: {' '.join(args)} "
            f"(rc={result.returncode}): {output.strip()[:400]}"
        )

    def _streaming_workspace_running(self) -> bool:
        if shutil.which("docker") is None:
            return False
        result = self._run(
            ["docker", "ps", "--filter", "label=embodiedcloud.workspace=1", "--format", "{{.Labels}}"],
            check=False,
        )
        return any("embodiedcloud.streaming=1" in labels for labels in result.stdout.splitlines())

    def _assert_streaming_slot_available(self) -> None:
        if self._streaming_workspace_running():
            raise RuntimeError(
                "A streaming workspace is already running on this host. "
                "v0.1 intentionally allows one Isaac Lab WebRTC stream per host."
            )
        if not is_port_free(self.WEBRTC_SIGNAL_PORT):
            raise RuntimeError(f"WebRTC signaling port {self.WEBRTC_SIGNAL_PORT}/TCP is already in use.")
        if not is_port_free(self.WEBRTC_MEDIA_PORT, sock_type=socket.SOCK_DGRAM):
            raise RuntimeError(f"WebRTC media port {self.WEBRTC_MEDIA_PORT}/UDP is already in use.")

    def run_argv(
        self,
        *,
        workspace: Workspace,
        template: Template,
        reservation: ResourceReservation,
        container_name: str,
        workspace_dir: Path,
        ide_port: int,
        password: str,
        image: str,
    ) -> list[str]:
        """`docker run` 的完整 argv（provision 与 docker 真实档共用这一份）。

        单独成方法只为让常驻档能拿**生产同款** argv 交给守护进程验收：测试里重抄
        一遍命令行，只能证明两份抄得一样，证明不了生产那条 argv 守护进程认得。
        """
        gpu_index = reservation.gpu_index
        env = [
            "-e", "ACCEPT_EULA=Y",
            "-e", f"PRIVACY_CONSENT={'Y' if self.settings.privacy_consent else 'N'}",
            "-e", f"WORKSPACE_PASSWORD={password}",
            "-e", f"IDE_PORT={ide_port}",
            "-e", f"LIVESTREAM={'1' if template.requires_streaming else '0'}",
            "-e", f"PUBLIC_IP={self.settings.host_public_ip}",
        ]
        return [
            "docker", "run", "-d", "--rm",
            "--name", container_name,
            "--network", "host",
            # GPU 绑定完全来自 reservation（scheduler 是唯一决策者），provider 不自行选卡
            "--gpus", f"device={gpu_index}",
            "--label", "embodiedcloud.workspace=1",
            "--label", f"embodiedcloud.workspace_id={workspace.id}",
            "--label", f"embodiedcloud.gpu={gpu_index}",
            "--label", f"embodiedcloud.gpu_id={reservation.gpu_id}",
            "--label", f"embodiedcloud.streaming={1 if template.requires_streaming else 0}",
            "--label", f"embodiedcloud.operation={reservation.metadata.get('operation_id', '')}",
            "--label", f"embodiedcloud.fencing={reservation.metadata.get('fencing_token', '')}",
            *env,
            "-v", f"{workspace_dir}:/workspace/project:rw",
            image,
        ]

    def provision(
        self,
        workspace: Workspace,
        template: Template,
        workspace_dir: Path,
        reservation: ResourceReservation | None = None,
    ) -> ProvisionResult:
        # 单一 GPU 权威：没有 reservation 就不允许启动真实容器
        if reservation is None:
            raise RuntimeError("DockerProvider requires a ResourceReservation from GpuScheduler")
        if not self.settings.eula_accepted:
            raise RuntimeError("Set EMBODIEDCLOUD_EULA_ACCEPTED=true before launching NVIDIA Isaac containers.")

        ok, detail = self.health()
        if not ok:
            raise RuntimeError(detail)

        if template.requires_streaming:
            self._assert_streaming_slot_available()

        workspace_dir.mkdir(parents=True, exist_ok=True)
        # Isaac Sim 6 containers use uid/gid 1234 by default. A bind-mounted
        # project directory created by the control-plane user may otherwise be
        # read-only to the workspace. v0.1 is explicitly trusted single-host
        # mode, so grant workspace-local write access here; Kubernetes/PVC mode
        # replaces this with fsGroup/volume ownership policy.
        # Isaac Sim 6 容器默认 uid/gid 1234。单机可信模式（ADR 0003）在此授予
        # workspace 本地写权限；K8s/PVC 模式用 fsGroup 策略替代，不适用此处。
        os.chmod(workspace_dir, 0o777)  # noqa: S103
        streaming_note = ""
        if template.requires_streaming:
            streaming_note = (
                "\nThis template starts its own Isaac Lab/Isaac Sim process when you run the command.\n"
                "The container exports LIVESTREAM=1 and PUBLIC_IP, so do not launch a second background simulator.\n"
            )
        readme_path = workspace_dir / "README.md"
        readme_path.write_text(
            "# EmbodiedCloud Workspace\n\n"
            f"Template: {template.name}\n\n"
            "Run this inside the browser terminal:\n\n"
            f"```bash\ncd /workspace/IsaacLab\n{template.launch_command}\n```\n"
            f"{streaming_note}",
            encoding="utf-8",
        )
        os.chmod(readme_path, 0o666)  # noqa: S103

        # §11 provision 幂等：同 workspace 容器已存在（重试/adopt）→ 复用，不创建第二份
        existing = self._container_exists(workspace)
        if existing:
            return ProvisionResult(
                ide_port=workspace.ide_port,
                signal_port=workspace.signal_port,
                media_port=workspace.media_port,
                ide_url=workspace.ide_url,
                stream_hint=workspace.stream_hint,
                password=None,
                container_name=self._name(workspace),
            )

        ide_port = allocate_tcp_port(self.settings.ide_port_start, self.settings.ide_port_end)
        # 记录分配，destroy 时释放（含 provision 中途失败的补偿销毁路径）
        self._allocated_ide_ports[workspace.id] = ide_port
        signal_port = self.WEBRTC_SIGNAL_PORT if template.requires_streaming else None
        media_port = self.WEBRTC_MEDIA_PORT if template.requires_streaming else None

        password = secrets.token_urlsafe(16)
        container_name = self._name(workspace)

        # runtime 镜像：workspace.image（TemplateVersion 快照）→ template.image →
        # settings.workspace_image 显式 fallback。禁止 mutable latest。
        image = workspace.image or template.image or self.settings.workspace_image

        args = self.run_argv(
            workspace=workspace,
            template=template,
            reservation=reservation,
            container_name=container_name,
            workspace_dir=workspace_dir,
            ide_port=ide_port,
            password=password,
            image=image,
        )
        result = self._run(args, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())

        stream_hint = None
        if template.requires_streaming:
            stream_hint = (
                f"Run the template command first. Isaac Lab public WebRTC will use "
                f"host={self.settings.host_public_ip}, signaling TCP {signal_port}, media UDP {media_port}. "
                "One client per simulator instance. Keep these endpoints on a trusted/private network "
                "or place an authenticated/TLS gateway in front of any public access."
            )
        return ProvisionResult(
            ide_port=ide_port,
            signal_port=signal_port,
            media_port=media_port,
            ide_url=f"http://{self.settings.host_public_ip}:{ide_port}/",
            stream_hint=stream_hint,
            password=password,
            container_name=container_name,
        )

    def start(self, workspace: Workspace) -> None:
        """从 STOPPED 恢复：docker start 已有容器（容器由 scheduler reservation 绑定）。

        名字走 `_name`：改前的 `if workspace.container_name` 让"列没落上"变成静默空转，
        调用返回成功而什么都没跑 ⇒ 上层的释放准入就被"命令成功"骗过去了。
        """
        name = self._name(workspace)
        self._run_checked(["docker", "start", name], container_name=name)

    def stop(self, workspace: Workspace) -> None:
        name = self._name(workspace)
        self._run_checked(["docker", "stop", "-t", "20", name], container_name=name)

    def destroy(self, workspace: Workspace) -> None:
        """删除容器（幂等）。container_name 未持久化（如 provision 中途失败）时
        由 `_name` 按命名约定推导，保证补偿清理可达。"""
        container_name = self._name(workspace)
        try:
            self._run_checked(["docker", "rm", "-f", container_name], container_name=container_name)
        finally:
            # 释放 IDE 端口回进程内端口池。放在 finally：即使 docker daemon 不可用
            # 导致 _run_checked 上抛（并被上层 suppress 吞掉），也释放登记，避免
            # _allocated_tcp 残留长期占用进程内端口池。释放后若 OS 端口仍被容器占用，
            # allocate_tcp_port 的 is_port_free 探测会继续跳过该端口（双保险）。
            # 优先取 provision 时登记的端口（覆盖中途失败未持久化到 workspace.ide_port 的
            # 场景）；否则回退到已持久化的 workspace.ide_port（跨进程/重启后的 destroy）。
            port = self._allocated_ide_ports.pop(workspace.id, None)
            if port is None and workspace.ide_port is not None:
                port = workspace.ide_port
            if port is not None:
                release_tcp_port(port)

    def pull_artifact(self, workspace: Workspace, source_path: str) -> Path:
        """docker cp 把容器内文件拉到控制面本地临时路径并返回。

        - container_name 缺失时按 ec-{id[:12]} 推导（与 destroy 一致）
        - 失败（非零 returncode / 目标文件未生成）上抛，并清理临时目录
        - 返回的临时文件由调用方负责清理（try/finally）

        单元测试覆盖命令构造；真实环境（docker daemon + 运行中容器）待物理验证。
        """
        container_name = self._name(workspace)
        tmp_dir = Path(tempfile.mkdtemp(prefix="embodiedcloud-artifact-"))
        dest = tmp_dir / Path(source_path).name
        result = self._run(
            ["docker", "cp", f"{container_name}:{source_path}", str(dest)], check=False
        )
        if result.returncode != 0:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            output = f"{result.stdout or ''}\n{result.stderr or ''}"
            raise RuntimeError(
                f"docker cp failed: {container_name}:{source_path} "
                f"(rc={result.returncode}): {output.strip()[:400]}"
            )
        if not dest.is_file():
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise FileNotFoundError(f"artifact file not found in container: {source_path}")
        return dest

    def inspect(self, workspace: Workspace) -> dict:
        """读取容器实况：running/restarting/exited/absent；问不到引擎时是 unknown，不是 absent。"""
        name = self._name(workspace)
        result = self._run(
            ["docker", "inspect", "--format", "{{json .State}}", name],
            check=False,
        )
        if result.returncode != 0:
            return self._absent_or_unknown(result.stderr, name)
        import json

        try:
            state = json.loads(result.stdout.strip())
        except ValueError:
            return {"state": "unknown", "container_name": name}
        return {
            "state": state.get("Status", "unknown"),
            "running": bool(state.get("Running")),
            "exit_code": state.get("ExitCode"),
            "container_name": name,
        }

    def logs(self, workspace: Workspace, tail: int = 200) -> str:
        name = self._name(workspace)
        result = self._run(
            ["docker", "logs", "--tail", str(tail), name],
            check=False,
        )
        if result.returncode != 0:
            return ""
        return result.stdout

    def wait_ready(self, workspace, template, timeout_seconds: int = 120) -> bool:
        """§9 readiness：容器 running + IDE 端口 TCP/HTTP 可达 +
        TemplateVersion.healthcheck 真实 exec（如配置）。"""
        import time as _time

        if self.inspect(workspace)["state"] == "absent":
            # 引擎说不存在就不必等满 timeout；但"问不到"（unknown）不能当不存在——
            # 那种情况下继续等、由 readiness 超时上抛，比把活容器判成没起来安全。
            return False
        deadline = _time.monotonic() + max(1, timeout_seconds)
        while _time.monotonic() < deadline:
            try:
                state = self.inspect(workspace)
            except Exception:
                state = {}
            if not state.get("running"):
                _time.sleep(2)
                continue
            ide_port = workspace.ide_port
            # IDE TCP + HTTP 探测
            if ide_port is not None:
                import socket

                tcp_ok = False
                http_ok = False
                try:
                    with socket.create_connection(("127.0.0.1", ide_port), timeout=2):
                        tcp_ok = True
                except OSError:
                    pass
                if tcp_ok:
                    try:
                        import urllib.request

                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{ide_port}/", timeout=2
                        ) as resp:
                            http_ok = resp.status < 500
                    except Exception:
                        http_ok = False
                if not (tcp_ok and http_ok):
                    _time.sleep(2)
                    continue
            # TemplateVersion.healthcheck 真实执行（容器内 exec）
            hc = getattr(template, "healthcheck", None) or {}
            command = hc.get("command") if isinstance(hc, dict) else None
            if command:
                result = self._run(
                    ["docker", "exec", self._name(workspace), "sh", "-c", command],
                    check=False,
                )
                if result.returncode != 0:
                    _time.sleep(2)
                    continue
            return True
        return False

    @property
    def supports_credential_rotation(self) -> bool:
        # 运行中容器 env 不可变 → 不支持运行时轮换 → Docker Warm Pool 默认禁用
        return False

    def rotate_credentials(self, workspace: Workspace, credentials: dict) -> bool:
        """运行中容器的 WORKSPACE_PASSWORD env 不可变；不支持运行时轮换。

        返回 False → warm pool claim 不得交付（fallback 正常 provision）。
        未来支持路径：镜像改为从文件读取密码（如 /workspace/project/.code-server-passwd）
        后，此处改为写入文件并返回 True。
        """
        return False

    def reconcile(self, workspace: Workspace) -> RuntimeState:
        """判定 runtime 存活：引擎说在跑 → ALIVE；引擎说不存在 → MISSING；问不到 → UNKNOWN。

        改前有两处把"读不到"当成"不在了"：① `container_name` 列为空直接 return MISSING；
        ② 兜底把任何非 running 的 state（含 inspect 自己给的 unknown）落到 MISSING。
        释放准入 `_release_admitted(command_succeeded=False)` 只认 MISSING —— 于是守护进程
        断线那一段时间里，每个 workspace 都"看起来"没了，卡会被放回池子（N-70）。
        """
        if shutil.which("docker") is None:
            return RuntimeState.UNKNOWN
        try:
            inspect = self.inspect(workspace)
        except Exception:
            return RuntimeState.UNKNOWN
        if inspect.get("state") == "absent":
            return RuntimeState.MISSING
        if inspect.get("running"):
            return RuntimeState.ALIVE
        if inspect.get("state") in {"unknown", None}:
            return RuntimeState.UNKNOWN
        return RuntimeState.MISSING
