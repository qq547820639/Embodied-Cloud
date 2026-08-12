"""Warm Pool：真实 claim 模型（§18）。

状态机：
    PREWARMING → READY（预热完成，无最终用户归属、无用户数据）
    READY → CLAIMING（原子抢占）→ CLAIMED（attach user → RUNNING）
    READY/其他 → DRAINING（退出池）→ FAILED

- READY 的 warm runtime：没有 user ownership、没有 project data
- claim 是原子操作（UPDATE ... WHERE warm_pool_state='ready'，SQLite/PostgreSQL
  单写者语义），并发下至多一个用户 claim 成功
- 没有 READY runtime 时 claim 返回 None → 调用方 fallback 正常 provision
- 指标：warm_pool_ready / warm_pool_claim_total / warm_pool_claim_failed /
  workspace_launch_seconds（orchestrator 已接入）
"""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..metrics import (
    WARM_POOL_CLAIM_FAILED,
    WARM_POOL_CLAIM_TOTAL,
    WARM_POOL_READY,
)
from ..models import Template, User, WarmPoolState, Workspace, WorkspaceStatus

if TYPE_CHECKING:
    from .orchestrator import WorkspaceOrchestrator

logger = logging.getLogger("embodiedcloud.warmpool")

_WARM_STATES = {state.value for state in WarmPoolState}


def utcnow() -> datetime:
    return datetime.now(UTC)


class WarmPoolManager:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        orchestrator: "WorkspaceOrchestrator",
        settings: Settings,
    ):
        self.session_factory = session_factory
        self.orchestrator = orchestrator
        self.settings = settings

    # ------------------------------------------------------------------
    # maintain：补齐并预热 READY runtime
    # ------------------------------------------------------------------
    def maintain(self, db: Session) -> dict[str, int]:
        """为每个 enabled 模板补齐 warm runtime（PREWARMING → READY）。"""
        if not self.settings.warm_pool_enabled:
            return {"created": 0, "ready": 0}
        stats = {"created": 0, "ready": 0}
        for template in db.scalars(select(Template).where(Template.enabled.is_(True))):
            ready = self._count_state(db, template.id, WarmPoolState.READY)
            prewarming = self._count_state(db, template.id, WarmPoolState.PREWARMING)
            # 兼容旧语义：CREATED/QUEUED 且未标记 warm_pool_state 的 workspace 视为池内
            legacy = self._count_legacy_pool(db, template.id)
            missing = self.settings.warm_pool_size - (ready + prewarming + legacy)
            if missing <= 0:
                continue
            for _ in range(missing):
                workspace = self.orchestrator.create(
                    db,
                    template,
                    name=f"warm-{template.id}",
                    user_id=None,
                    organization_id=None,
                )
                workspace.warm_pool_state = WarmPoolState.PREWARMING.value
                db.commit()
                stats["created"] += 1
                # 预热（同步）：成功后 READY
                self.orchestrator._start(workspace.id)
                with self.session_factory() as fresh:
                    w = fresh.get(Workspace, workspace.id)
                    if w is not None and w.status == WorkspaceStatus.RUNNING.value:
                        w.warm_pool_state = WarmPoolState.READY.value
                        fresh.commit()
                        stats["ready"] += 1
                    elif w is not None:
                        w.warm_pool_state = WarmPoolState.FAILED.value
                        fresh.commit()
        self._refresh_gauge(db)
        return stats

    # ------------------------------------------------------------------
    # claim：原子抢占 READY runtime
    # ------------------------------------------------------------------
    def claim(
        self,
        db: Session,
        template_id: str,
        user: User,
        credential_cipher=None,
    ) -> Workspace | None:
        """原子 claim 一个 READY warm runtime 并绑定用户；无 READY 返回 None。

        claim 成功后：attach user → 轮换凭据（新 password 加密落库，runtime 侧
        生效依赖 runtime rotate 能力，见 §20 说明）→ CLAIMED → RUNNING
        （计费从 claim 时刻开始）。
        """
        if not self.settings.warm_pool_enabled:
            return None
        # 原子抢占：UPDATE 后检查受影响行数；并发下至多一个事务成功
        claimed_id = db.scalar(
            select(Workspace.id)
            .where(
                Workspace.template_id == template_id,
                Workspace.warm_pool_state == WarmPoolState.READY.value,
                Workspace.deleted_at.is_(None),
            )
            .limit(1)
        )
        if claimed_id is None:
            return None
        WARM_POOL_CLAIM_TOTAL.inc()
        result = db.execute(
            update(Workspace)
            .where(
                Workspace.id == claimed_id,
                Workspace.warm_pool_state == WarmPoolState.READY.value,
            )
            .values(warm_pool_state=WarmPoolState.CLAIMING.value)
        )
        db.commit()
        from typing import Any, cast

        from sqlalchemy.engine import CursorResult

        rowcount = cast("CursorResult[Any]", result).rowcount
        if rowcount is None or int(rowcount) != 1:
            # 并发竞争：已被其他用户 claim → 通知调用方 fallback
            WARM_POOL_CLAIM_FAILED.inc()
            return None
        workspace = db.get(Workspace, claimed_id)
        assert workspace is not None
        # attach user
        workspace.user_id = user.id
        workspace.organization_id = user.organization_id
        # 轮换凭据（§7）：warm runtime 的旧密码不属于任何用户，claim 后必须更换。
        # 1) 生成新密码并加密落库；2) provider.rotate_credentials 让 runtime 生效。
        new_password = None
        if credential_cipher is not None:
            import secrets

            new_password = secrets.token_urlsafe(16)
            workspace.password = credential_cipher.encrypt(new_password)
        db.commit()
        rotated = False
        if new_password is not None:
            rotated = self.orchestrator.provider.rotate_credentials(
                workspace, {"password": new_password}
            )
        if not rotated:
            # 轮换失败：不得把 workspace 交给用户（旧密码仍可登录 = 不能发布）。
            # → DRAINING + 释放，调用方 fallback 正常 provision。
            WARM_POOL_CLAIM_FAILED.inc()
            workspace.warm_pool_state = WarmPoolState.DRAINING.value
            workspace.user_id = None
            workspace.organization_id = None
            workspace.password = None
            workspace.status = WorkspaceStatus.FAILED.value
            workspace.error_message = "warm pool claim failed: credential rotation not supported"
            db.commit()
            logger.warning(
                "warm pool claim aborted for %s: credential rotation failed",
                claimed_id[:8],
            )
            return None
        workspace.warm_pool_state = WarmPoolState.CLAIMED.value
        workspace.status = WorkspaceStatus.RUNNING.value
        workspace.started_at = utcnow()
        workspace.stopped_at = None
        workspace.error_message = None
        db.commit()
        db.refresh(workspace)
        self._refresh_gauge(db)
        logger.info("warm pool claim: workspace %s → user %s", claimed_id[:8], user.id)
        return workspace

    # ------------------------------------------------------------------
    def drain(self, db: Session, workspace_id: str) -> None:
        """把 warm workspace 移出池（DRAINING → 停用/释放）。"""
        workspace = db.get(Workspace, workspace_id)
        if workspace is None or workspace.warm_pool_state is None:
            return
        workspace.warm_pool_state = WarmPoolState.DRAINING.value
        db.commit()

    def mark_failed(self, db: Session, workspace_id: str) -> None:
        workspace = db.get(Workspace, workspace_id)
        if workspace is None:
            return
        workspace.warm_pool_state = WarmPoolState.FAILED.value
        db.commit()

    def pool_metrics(self, db: Session) -> dict[str, int]:
        """warm_pool_ready：READY 数量（按模板聚合）。"""
        counts: dict[str, int] = {}
        for state in _WARM_STATES:
            n = db.scalar(
                select(Workspace.id)
                .where(Workspace.warm_pool_state == state, Workspace.deleted_at.is_(None))
            )
            counts[state] = 1 if n is not None else 0
        return counts

    # ------------------------------------------------------------------
    # 兼容接口（早期版本）
    # ------------------------------------------------------------------
    def metrics(self, db: Session) -> dict[str, dict[str, int]]:
        """每个 enabled 模板的 warm（READY）与 ready（PREWARMING/CLAIMING/CLAIMED）数。"""
        result: dict[str, dict[str, int]] = {}
        for template in db.scalars(select(Template).where(Template.enabled.is_(True))):
            result[template.id] = {
                "warm": self._count_state(db, template.id, WarmPoolState.READY),
                "ready": self._count_state(db, template.id, WarmPoolState.PREWARMING)
                + self._count_state(db, template.id, WarmPoolState.CLAIMING)
                + self._count_state(db, template.id, WarmPoolState.CLAIMED),
            }
        return result

    def benchmark_launch(self, db: Session, template_id: str, iterations: int = 3) -> dict:
        """依次 create+start workspace，轮询到 RUNNING/FAILED，统计 p50/p95。"""
        import math
        import statistics
        import time

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
        idx95 = max(0, min(math.ceil(0.95 * len(sorted_samples)) - 1, len(sorted_samples) - 1))
        return {
            "p50_s": statistics.median(sorted_samples),
            "p95_s": sorted_samples[idx95] if sorted_samples else 0.0,
            "samples": samples,
        }

    def _wait_launch(
        self, workspace_id: str, start_time: float, timeout: float = 30.0
    ) -> float:
        import time

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

    # ------------------------------------------------------------------
    def _count_state(self, db: Session, template_id: str, state: WarmPoolState) -> int:
        from sqlalchemy import func

        n = db.scalar(
            select(func.count(Workspace.id)).where(
                Workspace.template_id == template_id,
                Workspace.warm_pool_state == state.value,
                Workspace.deleted_at.is_(None),
            )
        )
        return int(n or 0)

    def _count_legacy_pool(self, db: Session, template_id: str) -> int:
        """旧语义：CREATED/QUEUED 且未标记 warm_pool_state 的 workspace 计入池。"""
        from sqlalchemy import func

        n = db.scalar(
            select(func.count(Workspace.id)).where(
                Workspace.template_id == template_id,
                Workspace.warm_pool_state.is_(None),
                Workspace.status.in_(
                    [WorkspaceStatus.CREATED.value, WorkspaceStatus.QUEUED.value]
                ),
                Workspace.deleted_at.is_(None),
            )
        )
        return int(n or 0)

    def _refresh_gauge(self, db: Session) -> None:
        ready = 0
        for template in db.scalars(select(Template).where(Template.enabled.is_(True))):
            ready += self._count_state(db, template.id, WarmPoolState.READY)
        WARM_POOL_READY.set(ready)
