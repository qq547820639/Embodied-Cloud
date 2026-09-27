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


def health_reason(url: str, *, client=None, timeout: float = 5.0) -> str:
    """一次健康探测：返回""表示就绪，否则返回**为什么还没就绪**。

    关键是"什么异常都不外抛"。就绪循环原来只 `contextlib.suppress(OSError)`，
    而 httpx 的连接/读超时属于 `httpx.TransportError`（不是 OSError）——健康检查一卡住，
    异常就从循环里穿出去，绕开"未就绪 ⇒ 带服务日志的失败"这条有诊断价值的出路，
    把消费它的两档（浏览器、边缘 agent）变成 setup 错误。
    """
    try:
        import httpx

        target = client or httpx
        response = target.get(url, timeout=timeout)
        status = int(getattr(response, "status_code", 0) or 0)
        return "" if status == 200 else f"HTTP {status}"
    except ImportError as exc:  # 没装 httpx：这档本来就该跳，不该在这里炸
        return f"缺 httpx：{exc}"
    except Exception as exc:  # 有意宽捕：探测的答案是"没就绪"，不是异常类型
        return f"{type(exc).__name__}: {exc}"


def wait_until_ready(
    *,
    probe,
    alive,
    deadline_seconds: float,
    sleep=time.sleep,
    log_tail=lambda: "<无日志>",
    exit_code=None,
) -> tuple[bool, str]:
    """轮询到就绪；任何失败都收敛成 (False, 带日志的原因)。

    `probe()` 约定不抛（`health_reason` 就是照这个约定写的），但这里仍然兜住异常：
    判据要护的是"循环绝不外抛"，不是"调用方一定守约"。
    """
    deadline = time.monotonic() + deadline_seconds
    last = "未探测"
    while True:
        if not alive():
            rc = exit_code() if exit_code is not None else None
            return False, f"服务进程已退出 rc={rc}：\n{log_tail()}"
        try:
            last = probe()
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        if not last:
            return True, ""
        if time.monotonic() >= deadline:
            return False, f"未在 {deadline_seconds:.0f}s 内就绪（最后一次读数：{last}）：\n{log_tail()}"
        sleep(0.3)



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
        def _probe() -> str:
            # 端口没通时不算"未就绪"的原因里也不刺眼：socket 探测失败就回一句短语
            with contextlib.closing(socket.socket()) as sock:
                sock.settimeout(1)
                if sock.connect_ex(("127.0.0.1", port)) != 0:
                    return "端口未监听"
            return health_reason(f"{base}/api/health", client=httpx)

        ok, why = wait_until_ready(
            probe=_probe,
            alive=lambda: proc.poll() is None,
            deadline_seconds=_READY_TIMEOUT_SECONDS,
            exit_code=lambda: proc.returncode,
            log_tail=server.log_tail,
        )
        if not ok:
            raise RuntimeError(f"uvicorn 起不来：{base}\n{why}")
        try:
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
