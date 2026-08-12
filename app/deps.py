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
from .security import WorkspaceCredentialCipher, make_session_dependency, validate_credential_configuration
from .seed import seed_templates
from .services.billing import BillingPolicy
from .services.edge import EdgeService
from .services.ledger import CreditLedgerService
from .services.orchestrator import WorkspaceOrchestrator
from .services.providers.base import WorkspaceProvider
from .services.providers.docker import DockerProvider
from .services.providers.k8s import KubernetesProvider
from .services.providers.mock import MockProvider
from .services.scheduler import GpuScheduler
from .services.warmpool import WarmPoolManager
from .services.worker import OperationWorker

settings = Settings()
settings.ensure_dirs()
# §13：生产（非 mock provider）必须显式配置凭据密钥，否则拒绝启动
validate_credential_configuration(settings.provider, settings.workspace_credential_key)
engine = make_engine(settings)
SessionFactory: sessionmaker[Session] = make_session_factory(engine)
get_db = session_dependency(SessionFactory)
DB = Annotated[Session, Depends(get_db)]

get_current_user: Callable = make_session_dependency(SessionFactory, settings)
CurrentUser = Annotated[User, Depends(get_current_user)]

scheduler = GpuScheduler(SessionFactory)
ledger = CreditLedgerService(SessionFactory)
# §8：真实注入 Settings（禁止配置字段存在但对象用 constructor default）
billing = BillingPolicy(
    SessionFactory,
    ledger,
    minimum_launch_minutes=settings.billing_minimum_launch_minutes,
    enforce_preauthorization=settings.billing_enforce_preauthorization,
)
credential_cipher = WorkspaceCredentialCipher(settings.workspace_credential_key)


def make_provider() -> WorkspaceProvider:
    if settings.provider.lower() == "docker":
        return DockerProvider(settings)
    if settings.provider.lower() == "k8s" or settings.provider.lower() == "kubernetes":
        return KubernetesProvider(settings)
    return MockProvider(settings.public_base_url)


provider = make_provider()
orchestrator = WorkspaceOrchestrator(
    SessionFactory,
    provider,
    settings.workspace_root,
    scheduler,
    ledger,
    billing=billing,
    credential_cipher=credential_cipher,
    ready_timeout_seconds=settings.provision_ready_timeout_seconds,
)
worker = OperationWorker(SessionFactory, orchestrator)
# §7（P0）：provider 不支持运行时凭据轮换（如 Docker）→ warm pool 默认禁用
if settings.warm_pool_enabled and not provider.supports_credential_rotation:
    import logging

    logging.getLogger("embodiedcloud").warning(
        "warm pool disabled: provider %r does not support credential rotation "
        "(would leak old credentials on claim)", provider.name,
    )
    settings.warm_pool_enabled = False
warm_pool = WarmPoolManager(SessionFactory, orchestrator, settings)
edge_service = EdgeService(SessionFactory)


def bootstrap_db() -> None:
    """启动时建表（开发便利）并 seed；生产应关闭 auto_create_tables 走 alembic。"""
    from .db import Base

    if settings.auto_create_tables:
        Base.metadata.create_all(engine)
    with SessionFactory() as db:
        seed_templates(db)
        bootstrap_gpu_inventory(db)


def bootstrap_gpu_inventory(db: Session) -> None:
    """按 provider 同步 GPU inventory；mock 提供 8 张虚拟 GPU 便于全链路演示与并发测试。"""
    from .services.scheduler import GpuInfo

    provider_name = provider.name
    if provider_name == "mock":
        gpus = [
            GpuInfo(
                gpu_uuid=f"mock-gpu-{i:04d}",
                model="Mock RTX 4090" if i % 2 == 0 else "Mock RTX 6000 Ada",
                memory_total=24564 if i % 2 == 0 else 49152,
                index=i,
            )
            for i in range(8)
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
    elif provider_name in {"k8s", "kubernetes"}:
        _sync_k8s_gpus(db)


def _sync_k8s_gpus(db: Session) -> None:
    """K8s inventory：按 node 的 nvidia.com/gpu capacity 同步 GpuHost/Gpu。

    平台 Scheduler v1 只做 cluster + node + capacity reservation；
    Pod 最终 device assignment 由 NVIDIA Device Plugin 负责
    （不虚构通过普通 resource request 指定 GPU UUID）。
    node 可用（集群可达）时同步；不可达静默跳过（health() 汇报）。
    """
    from .services.providers.k8s import KubernetesProvider
    from .services.scheduler import GpuInfo

    try:
        k8s = KubernetesProvider(settings)
    except Exception:
        return
    ok, _ = k8s.health()
    if not ok:
        return
    try:
        api = k8s._require_client()
        nodes = api.CoreV1Api().list_node()
    except Exception:
        return
    for node in nodes.items:
        metadata = getattr(node, "metadata", None)
        status = getattr(node, "status", None)
        if metadata is None or status is None:
            continue
        node_name = getattr(metadata, "name", "node") or "node"
        capacity = dict(getattr(status, "allocatable", None) or getattr(status, "capacity", None) or {})
        gpu_count = 0
        for key, value in capacity.items():
            if getattr(key, "key", key) == "nvidia.com/gpu":
                gpu_count = int(getattr(value, "value", value) or 0)
        if gpu_count <= 0:
            continue
        address = ""
        for addr in getattr(status, "addresses", None) or []:
            if getattr(addr, "type", "") in ("InternalIP", "Hostname"):
                address = getattr(addr, "address", "") or address
        gpus = [
            GpuInfo(
                gpu_uuid=f"{node_name}:gpu-{i}",
                model=f"K8s GPU ({node_name})",
                memory_total=settings.k8s_gpu_memory_mb,  # capacity reservation 粒度；精确显存由 device plugin 上报
                index=i,
            )
            for i in range(gpu_count)
        ]
        scheduler.sync_host(
            db,
            host_id=f"k8s-node-{node_name}",
            name=node_name,
            address=address,
            provider="k8s",
            gpus=gpus,
        )


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
