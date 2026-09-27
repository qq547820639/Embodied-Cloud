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
  warm_pool_claim_seconds（只给成功交付的 claim 记时；orchestrator 的
  workspace_launch_seconds 覆盖的是**冷启动**那条路，两条路各有一把尺）
"""

import logging
import time
from typing import TYPE_CHECKING

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..metrics import (
    WARM_POOL_CLAIM_FAILED,
    WARM_POOL_CLAIM_TOTAL,
    WARM_POOL_READY,
    record_warm_pool_claim_duration,
)
from ..models import (
    Gpu,
    GpuStatus,
    OperationType,
    Template,
    User,
    WarmPoolState,
    Workspace,
    WorkspaceStatus,
)
from ..utils import utcnow

if TYPE_CHECKING:
    from .orchestrator import WorkspaceOrchestrator

logger = logging.getLogger("embodiedcloud.warmpool")

_WARM_STATES = {state.value for state in WarmPoolState}

# 基准端点的迭代上限：**只有一份定义**（路由用它算 Query 的 le，服务用它夹紧），
# 否则"路由挡 21、服务却照跑 10000"这种两数各写一遍的漂移迟早出现。
BENCHMARK_MAX_ITERATIONS = 20
BENCHMARK_DEFAULT_ITERATIONS = 3

# PREWARMING 占位在「收割」阶段被收敛为 FAILED 的终态/错误态集合：
# 这些状态的 workspace 既不可能再被 claim，也不应继续占用池位。
_TERMINAL_STATUSES = {
    WorkspaceStatus.FAILED.value,
    WorkspaceStatus.STOPPED.value,
    WorkspaceStatus.DELETED.value,
}


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
        # 补位冷却退避：本轮刚 reap 为 FAILED 的池位，本轮补位跳过（下一轮恢复），
        # 避免容量不足时「reap FAILED → 立即重建 → PROVISION 再失败」的无限 churn。
        self._fail_cooldown: dict[str, int] = {}

    # ------------------------------------------------------------------
    # maintain：两步异步补齐 warm runtime（不阻塞 worker 循环）
    # ------------------------------------------------------------------
    def maintain(self, db: Session) -> dict[str, int]:
        """为每个 enabled 模板补齐 warm runtime（PREWARMING → READY）。

        三步异步模式：
        1) 收割（先做）：把每个模板的 PREWARMING workspace 收敛——
           RUNNING → READY；终态/错误态 → FAILED（不残留 PREWARMING 占位）；
        2) 清理：为已 FAILED 且无 active operation 的 warm workspace 补入队
           DESTROY（异步释放占位与资源，不残留 tombstone 累积）；
        3) 补位：missing = warm_pool_size - (ready + prewarming + legacy)，
           每个缺失位只 create + 置 PREWARMING + start_async（入队 durable
           PROVISION operation，由 worker 异步执行）；本轮刚 reap 失败的池位
           因冷却退避跳过补位（容量恢复后下一轮收敛回目标 size）。

        不再同步调用 orchestrator._start（其内含 wait_ready 120s 门禁，
        同步预热会长时间阻塞 worker 循环，期间 STOP/DESTROY/PROVISION 无法处理）。
        """
        if not self.settings.warm_pool_enabled:
            return {"created": 0, "ready": 0, "destroyed": 0, "skipped_no_capacity": 0}
        stats = {"created": 0, "ready": 0, "destroyed": 0, "skipped_no_capacity": 0}
        templates = list(db.scalars(select(Template).where(Template.enabled.is_(True))))
        # 1) 收割（先做）：PREWARMING → READY/FAILED，保证池位不残留占位
        for template in templates:
            self._reap_prewarming(db, template.id, stats)
        # 2) 清理：FAILED warm workspace 入队 DESTROY（有 active op 则下轮再试）
        for template in templates:
            self._cleanup_failed(db, template.id, stats)
        # 3) 补位：只创建 + 入队异步 PROVISION，带冷却退避与容量闸门
        reserve = int(self.settings.warm_pool_reserve_slots)
        # 一份**共享**的空闲卡多重集：闸门必须跨模板算账，按模板各算各的会重复许诺同一张卡
        # （实测：8 张卡、5 个模板、size=2 时逐模板判"有没有够用的卡"全部通过，仍然开出 10 格）。
        free_gib = sorted(self._free_capacities_gib(db))
        warned_over_reserve = False
        for template in templates:
            ready = self._count_state(db, template.id, WarmPoolState.READY)
            prewarming = self._count_state(db, template.id, WarmPoolState.PREWARMING)
            # 兼容旧语义：CREATED/QUEUED 且未标记 warm_pool_state 的 workspace 视为池内
            legacy = self._count_legacy_pool(db, template.id)
            missing = self.settings.warm_pool_size - (ready + prewarming + legacy)
            # 冷却仅作用一轮：本轮 reap 失败的池位跳过补位，下一轮恢复
            cooldown = self._fail_cooldown.pop(template.id, 0)
            fillable = max(0, missing - cooldown)
            # 容量闸门（两道，理由都在 tests/test_warmpool.py 的注释里）：
            # - 装不下的格**不开**：现在 AVAILABLE 卡里没有任何一张够这个模板的 VRAM，
            #   创建了也只会是"注定失败的行 → 收割 FAILED → DESTROY"的空转；
            # - `warm_pool_reserve_slots` 给交互请求留卡：池子不许把舰队吃干，
            #   否则 size × 模板数 逼近容量时用户请求会全数失败（实测 8 卡／5 模板／size=2
            #   → 池占满 8 张，交互 0/5 起得来）。默认 0＝不预留，行为与改造前一致。
            # 补位是异步入队的（PROVISION 之后才真占卡），所以这里要自己把「本轮已开的格」
            # 和「上一轮还在 PREWARMING 的格」一起从可用量里扣掉，否则同一轮会把同一张卡许诺两次。
            required = int(template.recommended_vram_gb)
            for _slot in range(fillable):
                pick = next((g for g in free_gib if g >= required), None)
                if pick is None:
                    # 规则一：没有够用的卡就**不开格**（开出来注定失败 → 收割 → DESTROY 的空转）
                    stats["skipped_no_capacity"] += fillable - _slot
                    break
                if len(free_gib) - 1 < reserve:
                    # 规则二：这一格拿走就凑不满预留 ⇒ 留给交互请求
                    stats["skipped_no_capacity"] += fillable - _slot
                    if reserve >= len(free_gib) and not warned_over_reserve:
                        # 借 K8s 的方向：预留大于容量不该"静默归零"（它是直接判错）。
                        # 这里不抛（会把 worker 循环打死），但必须留下一条 WARNING，
                        # 否则配置写错的人只看得见"池子怎么老是空的"。
                        warned_over_reserve = True
                        logger.warning(
                            "warm pool reserve=%d leaves no room in %d free card(s): "
                            "pool will not fill this round",
                            reserve,
                            len(free_gib),
                        )
                    break
                free_gib.remove(pick)
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
                # 异步预热：入队 durable PROVISION operation（worker 异步执行），
                # 不再同步 _start（避免 wait_ready 120s 阻塞 worker 循环）。
                self.orchestrator.start_async(workspace.id)
        self._refresh_gauge(db)
        return stats

    def _free_capacities_gib(self, db: Session) -> list[int]:
        """AVAILABLE 卡的显存（GiB）列表；只读，不改状态。

        挑"最小的够用那张"来记账，与生产的 best_fit 分配同向（`scheduler.allocate` 的 ORDER BY），
        所以闸门模拟出来的余量与 worker 真占卡时的余量是同一个方向。
        """
        return [
            int(memory_mib // 1024)
            for memory_mib in db.scalars(
                select(Gpu.memory_total).where(Gpu.status == GpuStatus.AVAILABLE.value)
            )
        ]

    def _reap_prewarming(self, db: Session, template_id: str, stats: dict[str, int]) -> None:
        """收割 PREWARMING 占位：RUNNING → READY；终态/错误态 → FAILED。

        PREWARMING 是「已创建但尚未确认预热完成」的中间态。worker 异步 provision
        完成后 workspace 变 RUNNING（但仍标 PREWARMING），下一次 maintain 收割成
        READY；provision 失败/被停/删除则收割成 FAILED，释放池位供补位重建。
        收割为 FAILED 的池位计入冷却，本轮补位跳过（防止容量不足时无限 churn）。
        """
        prewarming = db.scalars(
            select(Workspace).where(
                Workspace.template_id == template_id,
                Workspace.warm_pool_state == WarmPoolState.PREWARMING.value,
                Workspace.deleted_at.is_(None),
            )
        ).all()
        failed_this_round = 0
        for workspace in prewarming:
            if workspace.status == WorkspaceStatus.RUNNING.value:
                workspace.warm_pool_state = WarmPoolState.READY.value
                stats["ready"] += 1
            elif workspace.status in _TERMINAL_STATUSES:
                workspace.warm_pool_state = WarmPoolState.FAILED.value
                failed_this_round += 1
        if prewarming:
            db.commit()
        if failed_this_round:
            self._fail_cooldown[template_id] = (
                self._fail_cooldown.get(template_id, 0) + failed_this_round
            )

    def _cleanup_failed(self, db: Session, template_id: str, stats: dict[str, int]) -> None:
        """为已 FAILED 且无 active operation 的 warm workspace 补入队 DESTROY。

        收割把 PREWARMING 收敛为 FAILED 后，这里入队 durable DESTROY operation
        异步清理（释放占位/资源、tombstone）。若 PROVISION operation 仍 active
        （RETRYING 未到终态），DB 唯一约束会拒绝 DESTROY 入队——本轮先跳过，
        下轮重试，保证 FAILED 池位最终被清理、不残留累积。
        """
        from .worker import enqueue_operation

        failed = db.scalars(
            select(Workspace).where(
                Workspace.template_id == template_id,
                Workspace.warm_pool_state == WarmPoolState.FAILED.value,
                Workspace.deleted_at.is_(None),
            )
        ).all()
        for workspace in failed:
            if self.orchestrator._has_active_operation(db, workspace.id):
                continue
            op = enqueue_operation(self.session_factory, workspace.id, OperationType.DESTROY)
            if op is not None:
                stats["destroyed"] += 1

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
        # §7 的兜底：入口先看 provider 能不能轮换。不能轮换时走到下面只会是
        # "占一格 READY → rotate 失败 → 拆 runtime → 退额度 → 返回 None"，
        # 白烧一个真 runtime 与一轮账（compose root 那句禁用是同一规则的第一道，
        # 但它是 import 期副作用，不该是唯一的一道）。
        if not self.orchestrator.provider.supports_credential_rotation:
            WARM_POOL_CLAIM_FAILED.inc()
            logger.warning(
                "warm pool claim refused: provider %r cannot rotate credentials",
                self.orchestrator.provider.name,
            )
            return None
        claim_started = time.monotonic()
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
        # §18：claim 直接置 RUNNING（不走 provision），所以预授权必须在这里做；
        # 额度不足 → BillingError，本 workspace 走下面的补偿归还，不交付给用户
        billing = getattr(self.orchestrator, "billing", None)
        if billing is not None:
            try:
                billing.reserve_launch(db, user, workspace.id)
            except Exception as exc:
                db.rollback()
                logger.info("warm pool claim aborted for %s: %s", claimed_id[:8], exc)
                WARM_POOL_CLAIM_FAILED.inc()
                return None
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
            # §7（P0）完整补偿，保证 0 orphan：
            #   streaming terminate → provider.destroy(runtime) → GPU release →
            #   清凭据/owner/端口 → DRAINING/FAILED。调用方 fallback 正常 provision。
            WARM_POOL_CLAIM_FAILED.inc()
            try:
                self.orchestrator.streaming.terminate_for_workspace(db, workspace.id)
            except Exception:
                db.rollback()
            try:
                self.orchestrator.provider.destroy(workspace)  # runtime 容器/Pod（幂等）
            except Exception as exc:
                logger.error("warm pool claim cleanup: runtime destroy failed: %s", exc)
            try:
                self.orchestrator.scheduler.release(db, workspace.id)  # GPU（幂等）
            except Exception:
                db.rollback()
            workspace.warm_pool_state = WarmPoolState.DRAINING.value
            workspace.user_id = None
            workspace.organization_id = None
            workspace.password = None
            workspace.ide_url = None
            workspace.container_name = None
            workspace.ide_port = None
            workspace.signal_port = None
            workspace.media_port = None
            workspace.status = WorkspaceStatus.FAILED.value
            workspace.error_message = "warm pool claim failed: credential rotation not supported"
            db.commit()
            # §18：交付失败 → 刚才圈住的额度退回（此处已 commit，release 自带提交）
            if billing is not None:
                billing.release_hold(db, workspace.id, reason="warm pool claim aborted")
            logger.warning(
                "warm pool claim aborted for %s: credential rotation failed (runtime+GPU released)",
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
        # 只给**成功交付**的 claim 记时：池空/竞争失败/轮换失败都不是一次"快的 claim"，
        # 把它们计进来会让 P95 越差越好看。
        record_warm_pool_claim_duration(template_id, time.monotonic() - claim_started)
        logger.info("warm pool claim: workspace %s → user %s", claimed_id[:8], user.id)
        return workspace

    # ------------------------------------------------------------------
    def pool_metrics(self, db: Session) -> dict[str, dict[str, int]]:
        """warm pool 真实数量：按 template × state 聚合（COUNT(*)，非存在性标志）。"""
        from sqlalchemy import func

        result: dict[str, dict[str, int]] = {}
        templates = list(db.scalars(select(Template).where(Template.enabled.is_(True))))
        for template in templates:
            rows = db.execute(
                select(Workspace.warm_pool_state, func.count(Workspace.id))
                .where(
                    Workspace.template_id == template.id,
                    Workspace.deleted_at.is_(None),
                )
                .group_by(Workspace.warm_pool_state)
            )
            counts = dict.fromkeys(_WARM_STATES, 0)
            for state, count in rows:
                if state is not None and state in counts:
                    counts[state] = int(count)
            result[template.id] = counts
        return result

    def benchmark_launch(
        self,
        db: Session,
        template_id: str,
        iterations: int = BENCHMARK_DEFAULT_ITERATIONS,
    ) -> dict:
        """依次 create+start（测完即 destroy），统计 p50/p95。

        三处是被自己的反例逼出来的，不是装饰：
        - **夹紧 iterations**：路由那侧有 422，但这一句是给直接调用者（CLI/脚本）兜底的；
          改造前这里只有 `max(1, iterations)`，一个 `?iterations=10000` 就会真建一万个 workspace。
        - **每轮测完立刻 destroy**：这些行 `user_id=None`（无主），不清理就是"用 GET 泄漏活体
          并占住 GPU"——实测改造前一次 iterations=2 的调用留下 4 个活体（含同批其他用例）。
        - **返回 iterations**：小样本下 p95 就是 `max()`，读数必须自带样本数才不会被当成百分位。
        """
        import math
        import statistics

        template = db.get(Template, template_id)
        if template is None:
            raise ValueError("模板不存在")
        n = min(max(1, int(iterations)), BENCHMARK_MAX_ITERATIONS)
        samples: list[float] = []
        for _ in range(n):
            workspace = self.orchestrator.create(
                db,
                template,
                name=f"bench-{template_id}",
                user_id=None,
                organization_id=None,
            )
            started = time.monotonic()
            try:
                self.orchestrator._start(workspace.id)
                samples.append(self._wait_launch(workspace.id, started))
            finally:
                self._destroy_quietly(db, workspace)
        sorted_samples = sorted(samples)
        idx95 = max(0, min(math.ceil(0.95 * len(sorted_samples)) - 1, len(sorted_samples) - 1))
        return {
            "p50_s": statistics.median(sorted_samples),
            "p95_s": sorted_samples[idx95] if sorted_samples else 0.0,
            "samples": samples,
            "iterations": n,
        }

    def _destroy_quietly(self, db: Session, workspace: Workspace) -> None:
        """基准用量的 workspace 测完就回收；回收失败不得吃掉读数。"""
        try:
            self.orchestrator.destroy(db, workspace)
        except Exception as exc:  # provider 清理失败等：留下痕迹，不抛出
            db.rollback()
            logger.error("benchmark cleanup failed for %s: %s", str(getattr(workspace, "id", "?"))[:8], exc)

    def _wait_launch(
        self, workspace_id: str, start_time: float, timeout: float = 30.0
    ) -> float:
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
        """旧语义：CREATED/QUEUED 且未标记 warm_pool_state 的 workspace 计入池。

        仅统计无归属（user_id IS NULL）的 workspace —— warm pool workspace 无 user_id，
        普通用户创建的 QUEUED workspace 有归属，不得误计入池。
        """
        from sqlalchemy import func

        n = db.scalar(
            select(func.count(Workspace.id)).where(
                Workspace.template_id == template_id,
                Workspace.warm_pool_state.is_(None),
                Workspace.status.in_(
                    [WorkspaceStatus.CREATED.value, WorkspaceStatus.QUEUED.value]
                ),
                Workspace.deleted_at.is_(None),
                Workspace.user_id.is_(None),
            )
        )
        return int(n or 0)

    def _refresh_gauge(self, db: Session) -> None:
        ready = 0
        for template in db.scalars(select(Template).where(Template.enabled.is_(True))):
            ready += self._count_state(db, template.id, WarmPoolState.READY)
        WARM_POOL_READY.set(ready)
