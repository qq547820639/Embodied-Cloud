"""自持有的 PostgreSQL 测试服务器（docker 容器，loopback-only，用完即删）。

为什么自己写而不用 testcontainers / pytest-postgresql：
- testcontainers 默认拉起 Ryuk 回收容器并挂 `/var/run/docker.sock`（rw），且 ryuk
  镜像不在本地缓存 → 离线环境首跑必挂；
- pytest-postgresql 的 `postgresql_proc` 需要宿主机 `initdb`/`pg_ctl` 二进制（本机
  没有），其 `noproc` 模式又要求你自己准备容器 —— 等于白加一层 LGPL 依赖。
这里零新依赖（除驱动 psycopg，已在 dev extra），且缺件时干净跳过。
"""

import atexit
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from sqlalchemy import engine_from_config

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_IMAGE = "postgres:16-alpine"
GATE_SENTINEL = "POSTGRES_VALIDATION_PENDING"
_READY_TIMEOUT_SECONDS = 90

# 本模块启动过、但尚未显式 stop 的容器（pytest 崩溃/被 kill 时的兜底清理）
_live: set[str] = set()


def image_name() -> str:
    return os.environ.get("EMBODIEDCLOUD_PG_IMAGE", DEFAULT_IMAGE)


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
        import psycopg  # noqa: F401
    except ImportError:
        return '缺驱动：pip install -e ".[postgres]"'
    return None


class PgServer:
    """一个临时 postgres 容器。端口只绑 127.0.0.1，测试结束立即删除。"""

    def __init__(self) -> None:
        self.name = f"ec-pg-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.port: int | None = None

    # -- lifecycle -----------------------------------------------------
    def start(self) -> "PgServer":
        proc = _docker(
            "run",
            "-d",
            "--rm",
            "--name",
            self.name,
            "-p",
            "127.0.0.1::5432",
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=trust",  # 一次性容器 + 仅回环暴露，无真实数据
            "-e",
            "POSTGRES_DB=postgres",
            image_name(),
        )
        if proc.returncode != 0:
            raise RuntimeError(f"docker run postgres failed: {proc.stdout}{proc.stderr}")
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

    def _wait_ready(self) -> int:
        """就绪判据走测试真正使用的那条路：对发布端口建 TCP 连接并 SELECT 1。

        不能用容器内 `pg_isready`：官方镜像的 entrypoint 在 initdb 期间会先起一个
        只监听 unix socket 的**临时**服务器，`pg_isready` 会对它返回"accepting
        connections"，随后 entrypoint 把它关掉再拉正式服务器 —— 那个瞬间端口转发
        是死的（实测报 "server closed the connection unexpectedly"）。
        """
        import psycopg

        deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
        last_error = ""
        while time.monotonic() < deadline:
            if _docker("inspect", "-f", "{{.State.Running}}", self.name, timeout=20).stdout.strip() != "true":
                logs = _docker("logs", "--tail", "50", self.name, timeout=20)
                raise RuntimeError(f"postgres 容器未运行：{logs.stdout}{logs.stderr}")
            port = self._try_published_port()
            if port is not None:
                try:
                    with psycopg.connect(
                        f"postgresql://postgres@127.0.0.1:{port}/postgres",
                        autocommit=True,
                        connect_timeout=3,
                    ) as conn:
                        conn.execute("SELECT 1")
                    return port
                except psycopg.OperationalError as exc:  # 转发未就绪/临时服务器切换中 → 继续等
                    last_error = str(exc).strip().replace("\n", " ")
            time.sleep(0.5)
        raise RuntimeError(f"postgres 容器 {self.name} 在 {_READY_TIMEOUT_SECONDS}s 内未就绪：{last_error}")

    def _try_published_port(self) -> int | None:
        out = _docker("port", self.name, "5432/tcp", timeout=20).stdout
        for line in out.splitlines():
            host, _, port = line.strip().rpartition(":")
            if host == "127.0.0.1" and port.isdigit():
                return int(port)
        return None

    # -- connection urls -----------------------------------------------
    def server_url(self) -> str:
        assert self.port is not None
        return f"postgresql+psycopg://postgres@127.0.0.1:{self.port}/postgres"

    def database_url(self, dbname: str) -> str:
        assert self.port is not None
        return f"postgresql+psycopg://postgres@127.0.0.1:{self.port}/{dbname}"


def _cleanup_leaked() -> None:
    for name in list(_live):
        _docker("rm", "-f", name, timeout=30)
    _live.clear()


atexit.register(_cleanup_leaked)


def create_database(server_url: str, dbname: str) -> None:
    """在临时服务器上开一个空库（CREATE/DROP DATABASE 需要 autocommit）。"""
    import psycopg

    with psycopg.connect(_libpq(server_url), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{dbname}"')


def drop_database(server_url: str, dbname: str) -> None:
    import psycopg

    with psycopg.connect(_libpq(server_url), autocommit=True) as conn:
        # FORCE（PG 13+）：先断开残留连接再删，避免测试线程未收尾时卡住
        conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


def _libpq(url: str) -> str:
    """sqlalchemy URL → psycopg 原生 DSN（去掉 +psycopg 方言后缀）。"""
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def migrate(url: str, revision: str = "head") -> None:
    """对给定 URL 跑 alembic 链（env.py 优先读 EMBODIEDCLOUD_DATABASE_URL）。

    临时改写该环境变量并在 finally 还原 —— conftest 在 import 期就把它指向
    sqlite 测试库，不还原会污染同会话后续用例。
    """
    from alembic.config import Config

    from alembic import command

    previous = os.environ.get("EMBODIEDCLOUD_DATABASE_URL")
    os.environ["EMBODIEDCLOUD_DATABASE_URL"] = url
    try:
        # 故意不传 alembic.ini：env.py 里的 fileConfig() 默认
        # disable_existing_loggers=True，在同进程跑一次迁移就会把 app 的 logger
        # 静音，后续用例（如 caplog 断言）看不见任何日志记录（实测过）。
        cfg = Config()
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        if revision == "base":
            command.downgrade(cfg, "base")
        else:
            command.upgrade(cfg, revision)
    finally:
        if previous is None:
            del os.environ["EMBODIEDCLOUD_DATABASE_URL"]
        else:
            os.environ["EMBODIEDCLOUD_DATABASE_URL"] = previous


def make_factory(url: str, pool_size: int = 8):
    """绑定 URL 的 sessionmaker（并发测试用；连接池要大于线程数）。"""
    from sqlalchemy.orm import Session, sessionmaker

    engine = engine_from_config(
        {"sqlalchemy.url": url},
        prefix="sqlalchemy.",
        pool_size=pool_size,
        max_overflow=pool_size,
        future=True,
    )
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def server_version(url: str) -> str:
    """实测服务器版本（证明这条用例真跑在 PostgreSQL 上，而不是被静默降级）。"""
    from sqlalchemy import create_engine, text

    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            return str(conn.execute(text("SHOW server_version")).scalar())
    finally:
        engine.dispose()
