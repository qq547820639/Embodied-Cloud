"""真起一个 uvicorn 子进程的共享夹具核心。

两处消费：`test_browser_console.py`（浏览器必须有真 HTTP 端口）与
`test_edge_agent_e2e.py`（agent 是**另一个进程**，TestClient 那种进程内 ASGI
调用对它不存在）。此前只有浏览器档自带这套，第二份若照抄，就绪判据、回收顺序、
日志取法就会各自漂移——而"服务在测试中途自己退掉"这类问题恰好只在其中一份里
看得见（参见 child-process 归因的做法：子进程日志必须能落到可读的地方）。
"""

import contextlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_READY_TIMEOUT_SECONDS = 60.0


@dataclass
class Server:
    url: str
    db_path: Path
    log_path: Path
    workspace_root: Path
    proc: subprocess.Popen[str] = field(repr=False)

    def log_tail(self, limit: int = 4000) -> str:
        if not self.log_path.exists():
            return "<无日志>"
        return self.log_path.read_text(encoding="utf-8", errors="replace")[-limit:]


def free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.contextmanager
def live_server(root: Path | None = None, *, provider: str = "mock") -> Iterator[Server]:
    """一次性控制面：mock provider + 独立 sqlite + 独立 workspace 目录。

    进程由本上下文自己回收（terminate → 超时 kill），临时目录随之删除；
    就绪判据走测试真正使用的 TCP + `GET /api/health` 路径。
    """
    import httpx

    own_root = False
    if root is None:
        root = Path(tempfile.mkdtemp(prefix="ec-live-"))
        own_root = True
    root.mkdir(parents=True, exist_ok=True)
    db = root / "app.db"
    workspaces = root / "workspaces"
    env = dict(
        os.environ,
        EMBODIEDCLOUD_PROVIDER=provider,
        EMBODIEDCLOUD_DATABASE_URL=f"sqlite:///{db}",
        EMBODIEDCLOUD_WORKSPACE_ROOT=str(workspaces),
        EMBODIEDCLOUD_AUTO_CREATE_TABLES="true",
        PYTHONPATH=str(REPO_ROOT),
    )
    port = free_port()
    log_path = root / "srv.log"
    base = f"http://127.0.0.1:{port}"
    with log_path.open("w", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(  # noqa: S603 受控常量参数（本仓 uvicorn），无用户输入
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            text=True,
        )
        server = Server(url=base, db_path=db, log_path=log_path, workspace_root=workspaces, proc=proc)
        ready = False
        deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
        try:
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError(f"uvicorn 提前退出 rc={proc.returncode}:\n{server.log_tail()}")
                with contextlib.suppress(OSError):
                    with contextlib.closing(socket.socket()) as probe:
                        probe.settimeout(1)
                        probe.connect(("127.0.0.1", port))
                    if httpx.get(f"{base}/api/health", timeout=5).status_code == 200:
                        ready = True
                        break
                time.sleep(0.3)
            if not ready:
                raise RuntimeError(f"uvicorn 未在 {_READY_TIMEOUT_SECONDS}s 内就绪：{base}\n{server.log_tail()}")
            yield server
        finally:
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=10)
            if proc.poll() is None:  # pragma: no cover
                proc.kill()
                proc.wait(timeout=10)
            if own_root:
                shutil.rmtree(root, ignore_errors=True)
