"""自持有的 S3 兼容对象存储测试服务器（docker 容器，loopback-only，用完即删）。

为什么是 VersityGW 而不是 MinIO / moto / LocalStack（2026-09-25 实测检索）：
- **MinIO**：语义最主流，但 `minio/minio` 仓库已归档（GitHub API `archived=true`，
  末次 release RELEASE.2025-10-15），把常驻门禁钉在一个不再出补丁的镜像上不合适；
  且 AGPL-3.0。本机实测 `minio/minio:latest` 仍可拉取（镜像 built 2025-09-07），
  所以它保留作**一次性交叉核对**用（见 docs/adr/0008），不做常驻依赖。
- **LocalStack**：仓库同样已归档，license 为 NOASSERTION（非 OSI），直接排除。
- **moto**：Apache-2.0 且活跃，但它是 AWS 语义的**二次实现**（内存 mock）。用它
  验证"我们如何区分 NoSuchKey / NoSuchBucket"属于循环自证——它想返回什么就返回什么。
- **VersityGW**：Apache-2.0、当日仍有提交、真实 Go 服务端（HTTP + SigV4 + XML 错误体），
  posix 后端单容器即可跑，镜像 29 MB。选它。

零新生产依赖：boto3 只在 `[s3]` 生产 extra 与 dev extra 里，缺件时整档干净跳过。
"""

import atexit
import os
import shutil
import subprocess
import tempfile
import time
import uuid

DEFAULT_IMAGE = "versity/versitygw:v1.8.0"
GATE_SENTINEL = "S3_VALIDATION_PENDING"
_READY_TIMEOUT_SECONDS = 90

# 一次性容器 + 仅回环暴露，凭据是随机测试值，不是任何真实账号
ACCESS_KEY = "ec-s3-test-ak"
SECRET_KEY = "ec-s3-test-sk-" + uuid.uuid4().hex[:12]

_live: set[str] = set()


def image_name() -> str:
    return os.environ.get("EMBODIEDCLOUD_S3_IMAGE", DEFAULT_IMAGE)


def _docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    # S603/S607: 参数全部为本模块受控常量（容器名/端口/镜像由 uuid 或固定值生成），
    # 无任何用户输入拼接；docker 可执行文件由 shutil.which 解析。
    return subprocess.run(  # noqa: S603
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def gate_reason() -> str | None:
    """可跑返回 None，否则返回不可跑的原因（供 skip 文案与 release gate 分类）。"""
    if shutil.which("docker") is None:
        return "docker CLI 不可用"
    if _docker("version", timeout=20).returncode != 0:
        return "docker daemon 不可达"
    if _docker("image", "inspect", image_name(), timeout=20).returncode != 0:
        return f"镜像 {image_name()} 未缓存（离线无法拉取）"
    try:
        import boto3  # noqa: F401
    except ImportError:
        return '缺 SDK：pip install -e ".[s3]"'
    return None


class S3Server:
    """一个临时 S3 兼容服务端。端口只绑 127.0.0.1，测试结束立即删除。

    三个目录互不嵌套是**服务端要求**：posix 后端会拒绝 versioning-dir 落在
    root 之内（实测报 "the root directory /data contains the directory /data/.vers"）。
    """

    def __init__(self) -> None:
        self.name = f"ec-s3-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.port: int | None = None
        self._dirs: list[tempfile.TemporaryDirectory[str]] = []

    # -- lifecycle -----------------------------------------------------
    def start(self) -> "S3Server":
        mounts = []
        for target in ("/data", "/vers", "/iam"):
            tmp = tempfile.TemporaryDirectory(prefix="ec-s3-")
            self._dirs.append(tmp)
            mounts += ["-v", f"{tmp.name}:{target}"]
        proc = _docker(
            "run",
            "-d",
            "--rm",
            "--name",
            self.name,
            "-p",
            "127.0.0.1::10000",
            "-e",
            f"ROOT_ACCESS_KEY={ACCESS_KEY}",
            "-e",
            f"ROOT_SECRET_KEY={SECRET_KEY}",
            *mounts,
            image_name(),
            "--port",
            ":10000",
            "--iam-dir",
            "/iam",
            "posix",
            "--versioning-dir",
            "/vers",
            "/data",
        )
        if proc.returncode != 0:
            self._close_dirs()
            raise RuntimeError(f"docker run {image_name()} failed: {proc.stdout}{proc.stderr}")
        _live.add(self.name)
        try:
            self.port = self._wait_ready()
        except Exception:
            self.stop()
            raise
        return self

    def stop(self) -> None:
        _live.discard(self.name)
        _docker("rm", "-f", self.name, timeout=60)
        self._close_dirs()

    def _close_dirs(self) -> None:
        for tmp in self._dirs:
            tmp.cleanup()
        self._dirs.clear()

    def _wait_ready(self) -> int:
        """就绪判据 = 「带签名的 ListBuckets 真的返回 200」。

        不用 TCP 探通：服务端在启动横幅之前就会 accept，早到的请求会以连接重置
        结束；也不用匿名 HTTP：匿名请求返回 403 只能证明"有人在监听"，不能证明
        凭据可用。ListBuckets 同时证明监听、SigV4 验签与鉴权三段都通。
        """
        deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
        last_error = ""
        while time.monotonic() < deadline:
            state = _docker("inspect", "-f", "{{.State.Running}}", self.name, timeout=20).stdout.strip()
            if state == "false":
                logs = _docker("logs", "--tail", "50", self.name, timeout=20)
                raise RuntimeError(f"S3 容器已退出：{logs.stdout}{logs.stderr}")
            port = self._try_published_port()
            if port is not None:
                try:
                    self.client(port).list_buckets()
                    return port
                except Exception as exc:
                    last_error = str(exc).strip().replace("\n", " ")[:200]
            time.sleep(0.5)
        raise RuntimeError(f"S3 容器 {self.name} 在 {_READY_TIMEOUT_SECONDS}s 内未就绪：{last_error}")

    def _try_published_port(self) -> int | None:
        out = _docker("port", self.name, "10000/tcp", timeout=20).stdout
        for line in out.splitlines():
            host, _, port = line.strip().rpartition(":")
            if host == "127.0.0.1" and port.isdigit():
                return int(port)
        return None

    # -- client --------------------------------------------------------
    def endpoint_url(self, port: int | None = None) -> str:
        return f"http://127.0.0.1:{port if port is not None else self.port}"

    def client(self, port: int | None = None, secret_key: str | None = None):
        import boto3

        return boto3.client(
            "s3",
            endpoint_url=self.endpoint_url(port),
            aws_access_key_id=ACCESS_KEY,
            aws_secret_access_key=secret_key if secret_key is not None else SECRET_KEY,
        )

    def running_image(self) -> str:
        """容器实际使用的镜像 ID（用于证明请求真的出到了进程外的那台服务端）。"""
        return _docker("inspect", "-f", "{{.Image}}", self.name, timeout=20).stdout.strip()

    def container_state(self) -> str:
        return _docker("inspect", "-f", "{{.State.Running}}", self.name, timeout=20).stdout.strip()


def unique_bucket(prefix: str = "ec-artifacts") -> str:
    """桶名要 DNS 安全且全局唯一（并发进程/重跑都不撞）。"""
    return f"{prefix}-{os.getpid()}-{uuid.uuid4().hex[:10]}".lower()


def _cleanup_leaked() -> None:
    for name in list(_live):
        _docker("rm", "-f", name, timeout=30)
    _live.clear()


atexit.register(_cleanup_leaked)
