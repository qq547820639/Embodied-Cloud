"""应用级单例与 FastAPI 依赖（避免 router 之间的循环导入）。

所有 router 从本模块取：settings / engine / session / 认证依赖 / 服务对象。
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .db import make_engine, make_session_factory, session_dependency
from .models import User
from .security import make_session_dependency
from .seed import seed_templates
from .services.ledger import CreditLedgerService
from .services.orchestrator import WorkspaceOrchestrator
from .services.providers.base import WorkspaceProvider
from .services.providers.docker import DockerProvider
from .services.providers.k8s import KubernetesProvider
from .services.providers.mock import MockProvider
from .services.scheduler import GpuScheduler
from .services.worker import OperationWorker

settings = Settings()
settings.ensure_dirs()
engine = make_engine(settings)
SessionFactory: sessionmaker[Session] = make_session_factory(engine)
get_db = session_dependency(SessionFactory)
DB = Annotated[Session, Depends(get_db)]

get_current_user: Callable = make_session_dependency(SessionFactory, settings)
CurrentUser = Annotated[User, Depends(get_current_user)]

scheduler = GpuScheduler(SessionFactory)
ledger = CreditLedgerService(SessionFactory)


def make_provider() -> WorkspaceProvider:
    if settings.provider.lower() == "docker":
        return DockerProvider(settings)
    if settings.provider.lower() == "k8s" or settings.provider.lower() == "kubernetes":
        return KubernetesProvider(settings)
    return MockProvider(settings.public_base_url)


provider = make_provider()
orchestrator = WorkspaceOrchestrator(SessionFactory, provider, settings.workspace_root, scheduler, ledger)
worker = OperationWorker(SessionFactory, orchestrator)


def bootstrap_db() -> None:
    """启动时建表（开发便利）并 seed；生产应关闭 auto_create_tables 走 alembic。"""
    from .db import Base

    if settings.auto_create_tables:
        Base.metadata.create_all(engine)
    with SessionFactory() as db:
        seed_templates(db)
        bootstrap_gpu_inventory(db)


def bootstrap_gpu_inventory(db: Session) -> None:
    """按 provider 同步 GPU inventory；mock 提供 2 张虚拟 GPU 便于全链路演示。"""
    from .services.scheduler import GpuInfo

    provider_name = provider.name
    if provider_name == "mock":
        gpus = [
            GpuInfo(gpu_uuid="mock-gpu-0001", model="Mock RTX 4090", memory_total=24564, index=0),
            GpuInfo(gpu_uuid="mock-gpu-0002", model="Mock RTX 6000 Ada", memory_total=49152, index=1),
        ]
        scheduler.sync_host(
            db,
            host_id="mock-host-0001",
            name="mock-host",
            address="127.0.0.1",
            provider="mock",
            gpus=gpus,
        )
    elif provider_name == "docker":
        _sync_docker_gpus(db)


def _sync_docker_gpus(db: Session) -> None:
    import shutil
    import subprocess

    from .services.scheduler import GpuInfo

    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi is None:
        return
    result = subprocess.run(  # noqa: S603 仅受控调用 nvidia-smi（绝对路径）
        [
            nvidia_smi,
            "--query-gpu=index,uuid,name,memory.total",
            "--format=csv,noheader,nounits",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return
    gpus: list[GpuInfo] = []
    for line in result.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 4:
            try:
                gpus.append(
                    GpuInfo(
                        gpu_uuid=parts[1],
                        model=parts[2],
                        memory_total=int(parts[3]),
                        index=int(parts[0]),
                    )
                )
            except ValueError:
                continue
    if gpus:
        scheduler.sync_host(
            db,
            host_id="docker-host-0001",
            name="docker-host",
            address=settings.host_public_ip,
            provider="docker",
            gpus=gpus,
        )


def run_crash_recovery() -> None:
    orchestrator.crash_recovery()
