import contextlib
import os
import secrets
import shutil
import socket
import subprocess
from pathlib import Path

from ...config import Settings
from ...models import Template, Workspace
from ..ports import allocate_tcp_port, is_port_free
from .base import ProvisionResult


class DockerProvider:
    """Single-host NVIDIA GPU runtime for the first sellable EmbodiedCloud release.

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

    def _list_gpus(self) -> list[tuple[int, str]]:
        out = self._run(["nvidia-smi", "--query-gpu=index,name", "--format=csv,noheader,nounits"]).stdout
        result: list[tuple[int, str]] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            idx, name = line.split(",", 1)
            result.append((int(idx.strip()), name.strip()))
        return result

    def _running_workspace_labels(self) -> list[str]:
        if shutil.which("docker") is None:
            return []
        result = self._run(
            ["docker", "ps", "--filter", "label=embodiedcloud.workspace=1", "--format", "{{.Labels}}"],
            check=False,
        )
        return [line for line in result.stdout.splitlines() if line.strip()]

    def _occupied_gpus(self) -> set[int]:
        occupied: set[int] = set()
        for labels in self._running_workspace_labels():
            for part in labels.split(","):
                if part.startswith("embodiedcloud.gpu="):
                    with contextlib.suppress(ValueError):
                        occupied.add(int(part.split("=", 1)[1]))
        return occupied

    def _streaming_workspace_running(self) -> bool:
        return any("embodiedcloud.streaming=1" in labels for labels in self._running_workspace_labels())

    def _choose_gpu(self) -> tuple[int, str]:
        occupied = self._occupied_gpus()
        for idx, name in self._list_gpus():
            if idx not in occupied:
                return idx, name
        raise RuntimeError("No free GPU. v0.1 policy is one running workspace per physical GPU.")

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

    def provision(self, workspace: Workspace, template: Template, workspace_dir: Path) -> ProvisionResult:
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

        gpu_index, gpu_name = self._choose_gpu()
        ide_port = allocate_tcp_port(self.settings.ide_port_start, self.settings.ide_port_end)
        signal_port = self.WEBRTC_SIGNAL_PORT if template.requires_streaming else None
        media_port = self.WEBRTC_MEDIA_PORT if template.requires_streaming else None

        password = secrets.token_urlsafe(16)
        container_name = f"ec-{workspace.id[:12]}"
        env = [
            "-e", "ACCEPT_EULA=Y",
            "-e", f"PRIVACY_CONSENT={'Y' if self.settings.privacy_consent else 'N'}",
            "-e", f"WORKSPACE_PASSWORD={password}",
            "-e", f"IDE_PORT={ide_port}",
            "-e", f"LIVESTREAM={'1' if template.requires_streaming else '0'}",
            "-e", f"PUBLIC_IP={self.settings.host_public_ip}",
        ]

        args = [
            "docker", "run", "-d", "--rm",
            "--name", container_name,
            "--network", "host",
            "--gpus", f"device={gpu_index}",
            "--label", "embodiedcloud.workspace=1",
            "--label", f"embodiedcloud.workspace_id={workspace.id}",
            "--label", f"embodiedcloud.gpu={gpu_index}",
            "--label", f"embodiedcloud.streaming={1 if template.requires_streaming else 0}",
            *env,
            "-v", f"{workspace_dir}:/workspace/project:rw",
            self.settings.workspace_image,
        ]
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
            gpu_index=gpu_index,
            gpu_name=gpu_name,
            ide_port=ide_port,
            signal_port=signal_port,
            media_port=media_port,
            ide_url=f"http://{self.settings.host_public_ip}:{ide_port}/",
            stream_hint=stream_hint,
            password=password,
            container_name=container_name,
        )

    def stop(self, workspace: Workspace) -> None:
        if workspace.container_name:
            self._run(["docker", "stop", "-t", "20", workspace.container_name], check=False)

    def destroy(self, workspace: Workspace) -> None:
        if workspace.container_name:
            self._run(["docker", "rm", "-f", workspace.container_name], check=False)
