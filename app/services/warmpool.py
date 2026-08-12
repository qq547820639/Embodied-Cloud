"""Warm Pool: 为启用模板预置预热 workspace, 降低冷启动延迟。

注意: 这是 warm pool 占位实现——真实预启动容器在 GPU 环境验证。
mock provider 下 start 很快, 本模块主要用于验证 maintain / benchmark 的正确性。
"""

import math
import statistics
import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..models import Template, Workspace, WorkspaceStatus
from .orchestrator import WorkspaceOrchestrator

_LAUNCH_TIMEOUT_S = 30.0
# "准备就绪" 的 warm workspace 状态（尚未开始 provisioning，可被抢占）
_WARM_POOL_STATES = {WorkspaceStatus.CREATED.value, WorkspaceStatus.QUEUED.value}


def record_warm_pool_stats(
    *,
    template_id: str | None = None,
    warm_count: int | None = None,
    ready_count: int | None = None,
) -> None:
    """指标预留骨架: 后续接入 Prometheus warm pool gauge/counter。"""
    return None


class WarmPoolManager:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        orchestrator: WorkspaceOrchestrator,
        settings: Settings,
    ):
        self.session_factory = session_factory
        self.orchestrator = orchestrator
        self.settings = settings

    # ------------------------------------------------------------------
    def maintain(self, db: Session) -> None:
        """为每个 enabled 模板补齐 warm workspace (status 属于 {CREATED, QUEUED})。"""
        if not self.settings.warm_pool_enabled:
            return
        for template in db.scalars(select(Template).where(Template.enabled.is_(True))):
            ready = self._count_ready(db, template.id)
            missing = self.settings.warm_pool_size - ready
            if missing <= 0:
                continue
            # 防止无限预热：每个模板至多创建 warm_pool_size 个
            for _ in range(min(missing, self.settings.warm_pool_size)):
                workspace = self.orchestrator.create(
                    db,
                    template,
                    name=f"warm-{template.id}",
                    user_id=None,
                    organization_id=None,
                )
                self.orchestrator._start(workspace.id)
                record_warm_pool_stats(template_id=template.id, warm_count=1)

    def metrics(self, db: Session) -> dict[str, dict[str, int]]:
        """每个 enabled 模板的 warm (RUNNING) 数与 ready (CREATED/QUEUED) 数。"""
        result: dict[str, dict[str, int]] = {}
        for template in db.scalars(select(Template).where(Template.enabled.is_(True))):
            counts: dict[str, int] = {}
            rows = db.execute(
                select(Workspace.status, func.count(Workspace.id))
                .where(Workspace.template_id == template.id)
                .group_by(Workspace.status)
            )
            for status, count in rows:
                counts[str(status)] = int(count)
            result[template.id] = {
                "warm": counts.get(WorkspaceStatus.RUNNING.value, 0),
                "ready": sum(counts.get(status, 0) for status in _WARM_POOL_STATES),
            }
        return result

    def benchmark_launch(self, db: Session, template_id: str, iterations: int = 3) -> dict:
        """依次 create+start workspace, 轮询到 RUNNING/FAILED, 统计 p50/p95。"""
        template = db.get(Template, template_id)
        if template is None:
            raise ValueError("模板不存在")
        samples: list[float] = []
        for _ in range(max(1, iterations)):
            workspace = self.orchestrator.create(
                db,
                template,
                name=f"bench-{template_id}",
                user_id=None,
                organization_id=None,
            )
            started = time.monotonic()
            self.orchestrator._start(workspace.id)
            samples.append(self._wait_launch(workspace.id, started))
        sorted_samples = sorted(samples)
        return {
            "p50_s": statistics.median(sorted_samples),
            "p95_s": _percentile(sorted_samples, 95),
            "samples": samples,
        }

    # ------------------------------------------------------------------
    def _count_ready(self, db: Session, template_id: str) -> int:
        count = db.scalar(
            select(func.count(Workspace.id)).where(
                Workspace.template_id == template_id,
                Workspace.status.in_(_WARM_POOL_STATES),
            )
        )
        return int(count or 0)

    def _wait_launch(
        self, workspace_id: str, start_time: float, timeout: float = _LAUNCH_TIMEOUT_S
    ) -> float:
        """轮询 DB 直到 workspace 进入 RUNNING/FAILED (最多 timeout 秒), 返回耗时。"""
        deadline = start_time + timeout
        while time.monotonic() < deadline:
            with self.session_factory() as poll_db:
                workspace = poll_db.get(Workspace, workspace_id)
                if workspace is not None and workspace.status in {
                    WorkspaceStatus.RUNNING.value,
                    WorkspaceStatus.FAILED.value,
                }:
                    return time.monotonic() - start_time
            time.sleep(0.05)
        return time.monotonic() - start_time  # 超时兜底


def _percentile(sorted_samples: list[float], percentile: float) -> float:
    """sorted 列表的近似分位 (nearest-rank)。"""
    if not sorted_samples:
        return 0.0
    idx = math.ceil(percentile / 100.0 * len(sorted_samples)) - 1
    idx = max(0, min(idx, len(sorted_samples) - 1))
    return sorted_samples[idx]
