import contextlib
import logging
import time
import uuid
from datetime import UTC
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..models import (
    Gpu,
    GpuHost,
    Lab,
    OperationStatus,
    OperationType,
    Template,
    TemplateVersion,
    User,
    Workspace,
    WorkspaceOperation,
    WorkspaceStatus,
)
from ..utils import utcnow
from .billing import BillingPolicy
from .image_ref import pinned_ref
from .ledger import CreditLedgerService
from .providers.base import ResourceReservation, RuntimeState, WorkspaceProvider
from .scheduler import GpuScheduler, recover_stuck_gpu_allocations
from .streaming import StreamingSessionService
from .worker import OperationWorker, enqueue_operation

logger = logging.getLogger("embodiedcloud.orchestrator")


def _failure_is_terminal(operation: "WorkspaceOperation | None") -> bool:
    """这一次失败是不是最后一次：判据从 worker 那一侧来，不在这里重抄一遍。

    `operation is None` = 同步路径（`WorkspaceOrchestrator._start`）：没有 worker 接管，
    这一次就是最后一次，维持既有的 FAILED 语义（ADR 0002 修订）。
    """
    return operation is None or not OperationWorker.will_retry(operation)


def _observed_runtime_state(provider: WorkspaceProvider, workspace: Workspace) -> str:
    """给「释放准入没放行」那一档的 `error_message` 用的诊断读数。

    它**不参与判决**：放不放卡只由 `_release_admitted` 一处决定。这里多问一次只为把
    "provider 当时到底说了什么"落进库里那条原因；观测本身抛错也不许把补偿路径带崩，
    所以兜成字符串。（与 `app/services/warmpool.py` 的同名辅助同一立场。）
    """
    try:
        return str(provider.reconcile(workspace))
    except Exception as exc:  # pragma: no cover - 诊断面，不影响判决
        return f"unobservable ({exc})"


class WorkspaceOrchestrator:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: WorkspaceProvider,
        workspace_root: Path,
        scheduler: GpuScheduler | None = None,
        ledger: CreditLedgerService | None = None,
        streaming: StreamingSessionService | None = None,
        billing: "BillingPolicy | None" = None,
        credential_cipher=None,
        ready_timeout_seconds: int = 120,
    ):
        self.session_factory = session_factory
        self.provider = provider
        self.workspace_root = workspace_root
        self.scheduler = scheduler or GpuScheduler(session_factory)
        self.ledger = ledger or CreditLedgerService(session_factory)
        self.streaming = streaming or StreamingSessionService(session_factory)
        self.billing = billing
        # §20：控制面 DB 不保存明文密码（None 时=测试/无加密环境，直接存明文）
        self.credential_cipher = credential_cipher
        self.ready_timeout_seconds = ready_timeout_seconds

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------
    def create(
        self,
        db: Session,
        template: Template,
        name: str | None = None,
        user_id: str | None = None,
        organization_id: str | None = None,
    ) -> Workspace:
        # §11：绑定具体不可变 TemplateVersion（runtime image 的单一事实来源）。
        # 确定性 active-version 指针（template.current_version_id，发布策略决定）；
        # 旧数据无指针时按 released + created_at 兜底（迁移期）。
        template_version = None
        if template.current_version_id is not None:
            template_version = db.get(TemplateVersion, template.current_version_id)
        if template_version is None:
            template_version = db.scalar(
                select(TemplateVersion)
                .where(
                    TemplateVersion.template_id == template.id,
                    TemplateVersion.released.is_(True),
                )
                # 必须全序：同一次提交里发布的两个版本 created_at 可能完全相同，
                # 只按 created_at 排会让"新工作区用哪个镜像"变成随机结果。
                # 唯一确定的 active 指针仍是 template.current_version_id；
                # 这里的次级键只为给无指针的历史数据一个稳定答案。
                .order_by(TemplateVersion.created_at.desc(), TemplateVersion.id.desc())
            )
        workspace_id = str(uuid.uuid4())
        workspace = Workspace(
            id=workspace_id,
            name=name or f"{template.name} · {workspace_id[:6]}",
            template_id=template.id,
            template_version_id=template_version.id if template_version else None,
            # 快照版本镜像：启动时 provider 使用该镜像（禁止 mutable latest）。
            # 版本若声明了 image_digest，快照那一刻就钉成不可变引用——provider
            # 两条路径都只读 workspace.image，不需要各自再判一次"要不要钉"。
            image=pinned_ref(
                (template_version.image if template_version else template.image),
                template_version.image_digest if template_version else None,
            ),
            user_id=user_id,
            organization_id=organization_id,
            provider=self.provider.name,
            status=WorkspaceStatus.CREATED.value,
        )
        db.add(workspace)
        db.commit()
        db.refresh(workspace)
        return workspace

    # ------------------------------------------------------------------
    # durable operations（替换 threading.Thread 裸线程）
    # ------------------------------------------------------------------
    def start_async(self, workspace_id: str) -> WorkspaceOperation | None:
        """把 workspace 的 PROVISION 任务入队为 durable operation（PENDING）。"""
        return enqueue_operation(self.session_factory, workspace_id, OperationType.PROVISION)

    def execute_operation(self, db: Session, op: WorkspaceOperation) -> None:
        """worker 回调：执行一个 operation；抛错由 worker 按 attempts 决定重试/失败。"""
        workspace = db.get(Workspace, op.workspace_id)
        if workspace is None:
            raise RuntimeError(f"workspace {op.workspace_id[:8]} not found for operation")
        if op.operation_type in {OperationType.PROVISION.value, OperationType.START.value}:
            self._execute_provision(db, workspace, operation=op)
        elif op.operation_type == OperationType.STOP.value:
            # §12：durable STOP —— 幂等（重复 stop 不重复结算/释放）
            self.stop(db, workspace)
            if workspace.status != WorkspaceStatus.STOPPED.value:
                # 这一轮没把 runtime 停下就抛给 worker，按 attempts 决定重试/失败（ADR 0002）。
                # 不抛的话 STOP operation 记 SUCCEEDED，而卡还被那个活着的容器吃着 ——
                # 退码与"目标达成"是两件事，这里只认后者。
                raise RuntimeError(
                    f"STOP attempt did not retire the runtime: "
                    f"{workspace.error_message or workspace.status}"
                )
        elif op.operation_type == OperationType.DESTROY.value:
            # §12：durable DESTROY —— 幂等 tombstone；控制面重启不丢清理
            self.destroy(db, workspace)
        elif op.operation_type == OperationType.RECONCILE.value:
            self.reconcile_all()
        else:
            raise RuntimeError(f"unsupported operation type: {op.operation_type}")

    def _start(self, workspace_id: str) -> None:
        """同步执行 provision（测试与内部同步路径；生产异步路径走 operation worker）。

        失败不向上抛：workspace 状态已由 _execute_provision 置为 FAILED（ADR 0002）。
        """
        with self.session_factory() as db:
            workspace = db.get(Workspace, workspace_id)
            if workspace is None:
                return
            # 失败不向上抛：workspace 状态已由 _execute_provision 置为 FAILED（ADR 0002）
            with contextlib.suppress(Exception):
                self._execute_provision(db, workspace)
            db.commit()

    # ------------------------------------------------------------------
    # provision（同步核心；由 operation 驱动）
    # ------------------------------------------------------------------
    def _execute_provision(
        self, db: Session, workspace: Workspace, operation: "WorkspaceOperation | None" = None
    ) -> None:
        """将 workspace 带到 RUNNING；幂等（已 RUNNING/PROVISIONING 直接返回）。"""
        if workspace.status in {WorkspaceStatus.RUNNING.value, WorkspaceStatus.PROVISIONING.value}:
            return
        template = db.get(Template, workspace.template_id)
        if template is None or not template.enabled:
            raise RuntimeError("Template not found or disabled")
        # BillingPolicy 兜底门禁（API 层已检查；直接调用/重试路径再确认一次）
        if self.billing is not None and workspace.user_id is not None:
            user = db.get(User, workspace.user_id)
            if user is not None:
                self.billing.check_launch_eligible(db, user, template)
                # §S-3：provision 重试路径补 course quota 门禁（无 lab 上下文，
                # 按 template 匹配所有 lab 检查；首次执行后配额耗尽 → 重试被拦截）
                self.billing.check_course_quota(db, user, template)
                # §18：预授权 —— 真把这笔额度圈住（幂等：重试路径命中同一 pending hold）
                self.billing.reserve_launch(db, user, workspace.id)
        # 失败重试前将状态置回 QUEUED（上次失败已置 FAILED）
        workspace.status = WorkspaceStatus.QUEUED.value
        workspace.error_message = None
        db.commit()
        # §25：launch 指标（含时长观测）
        from ..metrics import record_launch_duration, record_launch_failure, record_launch_start

        # 分族：池内补位不是用户按下的启动（N-39，理由见 app/metrics.py 的注释）
        record_launch_start(workspace, template.id, workspace.provider)
        launch_started = time.monotonic()
        # N-67：补偿回滚**结果**是释放准入的输入，不是可以吞掉的东西。
        #   cleanup_attempted —— 补偿域到底进没进（只有进去了才有一次"我把它退役了"的动作）；
        #   cleanup_error     —— 补偿 destroy 抛出来的那一句（只进诊断文案，不参与判决）。
        cleanup_attempted = False
        cleanup_error: str | None = None
        try:
            # 1) 原子分配 GPU（GpuScheduler 是唯一 GPU reservation 决策入口）
            gpu = self.scheduler.allocate(
                db, workspace.id, gpu_requirement_gb=template.gpu_requirement_gb
            )
            workspace.gpu_id = gpu.id
            workspace.gpu_index = gpu.gpu_index
            workspace.gpu_name = gpu.model
            workspace.status = WorkspaceStatus.PROVISIONING.value
            db.commit()
            # §11：operation/fencing 上下文注入 reservation →
            # provider 外部资源打 label/annotation（worker 外部副作用可审计/可 adopt）
            op_meta: dict = {}
            if operation is not None:
                op_meta = {
                    "operation_id": operation.id,
                    "fencing_token": operation.fencing_token or "",
                }
            # §10：node identity 明确字段（K8s nodeSelector/affinity 依据）
            host = db.get(GpuHost, gpu.host_id)
            reservation = ResourceReservation(
                host_id=gpu.host_id,
                gpu_id=gpu.id,
                gpu_uuid=gpu.gpu_uuid,
                gpu_index=gpu.gpu_index or 0,
                memory_mb=gpu.memory_total,
                node_name=host.name if host is not None else gpu.host_id,
                metadata=op_meta,
            )
            # 2) provider 严格按 reservation 绑定资源，禁止二次决策；
            #    §9 readiness gate 也在同一补偿域内（未就绪 → destroy + release）
            try:
                result = self.provider.provision(
                    workspace,
                    template,
                    self.workspace_root / workspace.id,
                    reservation,
                )
                ready = self.provider.wait_ready(
                    workspace, template, timeout_seconds=self.ready_timeout_seconds
                )
                if not ready:
                    raise RuntimeError(
                        f"runtime readiness timeout after {self.ready_timeout_seconds}s"
                    )
            except Exception:
                # 补偿回滚：清理 provider 已创建的下游资源（容器/Pod/PVC/Service），
                # 幂等（container_name 未落库时按 workspace.id 推导）。
                # 改前这里是 `with contextlib.suppress(Exception)`：destroy 成没成被原地丢掉，
                # `_fail` 于是只能盲放卡（N-67）。现在把结果记下来，再原样上抛**原来那个**
                # 异常 —— 补偿的错误不许换掉原始异常的类型（ADR 0002：provider/scheduler
                # 边界不得给调用方引入新异常类型），也不许把它包成第二个异常往外抛。
                cleanup_attempted = True
                try:
                    self.provider.destroy(workspace)
                except Exception as destroy_exc:
                    cleanup_error = str(destroy_exc)
                    logger.warning(
                        "workspace %s compensating destroy failed: %s",
                        workspace.id[:8], destroy_exc,
                    )
                raise
            workspace.ide_port = result.ide_port
            workspace.signal_port = result.signal_port
            workspace.media_port = result.media_port
            workspace.ide_url = result.ide_url
            workspace.stream_hint = result.stream_hint
            # §20：DB 只保存加密凭据（runtime 侧仍有一份必要副本）
            if result.password is not None:
                workspace.password = (
                    self.credential_cipher.encrypt(result.password)
                    if self.credential_cipher is not None
                    else result.password
                )
            workspace.container_name = result.container_name
            workspace.status = WorkspaceStatus.RUNNING.value
            workspace.started_at = utcnow()
            workspace.stopped_at = None
            record_launch_duration(workspace, template.id, time.monotonic() - launch_started)
        except Exception as exc:
            # 盲捕获是有意设计：provider/scheduler 边界任意异常不得泄漏到 API（ADR 0002）
            record_launch_failure(workspace, template.id, workspace.provider)
            self._fail(
                db,
                workspace,
                str(exc),
                terminal=_failure_is_terminal(operation),
                # 释放准入的输入（N-63 的同一条判据，判据本体见 `_release_admitted`）：
                # 进了补偿域，就以那次补偿 destroy 的真实结果为准；没进（典型是 allocate
                # 自己失败，provider 侧从未创建过东西）就没有"我把它退役了"的动作可背书，
                # 走宽松档 —— 与改前一致，但 provider 亲口说 ALIVE 时照样不放卡。
                command_succeeded=(not cleanup_attempted) or cleanup_error is None,
                cleanup_error=cleanup_error,
            )
            raise
        db.commit()

    def _fail(
        self,
        db: Session,
        workspace: Workspace,
        message: str,
        *,
        terminal: bool = True,
        command_succeeded: bool,
        cleanup_error: str | None = None,
    ) -> None:
        """记录这一次尝试的失败原因；GPU 只放回 provider 认账"runtime 已退役"的那一格。

        释放准入（N-67，同一判据在仓库里的第四处消费位，前三处是 `_stop_cleanup` 的成功档
        与报错档、`claim` 的撤销档）：卡能不能回池，只由 `_release_admitted` 一处决定，
        不由补偿 destroy 的返回码决定。`command_succeeded` 由调用方（`_execute_provision`
        的补偿域）传入，语义与 `_release_admitted` 的同名参数一致。

        - 放行 ⇒ 今天的全部行为：release + 清 GPU 三列 + 下面那一档状态。
        - 不放行 ⇒ **不放卡、不清 GPU 三列**，状态写 **STOPPING**：
          两个候选里只有它同时满足"被保护"与"有人重试"。
          ① 保护：`recover_stuck_gpu_allocations`（scheduler.py:274-281）按状态保护
            {PROVISIONING, RUNNING, STOPPING}；QUEUED/FAILED 都在保护集外，留在 QUEUED 会被
            它把还有人吃的卡强制放掉，本函数的准入判据等于被绕过。
          ② 重试：`reconcile_all` 的 STOPPING+ALIVE 档已经接了 `_stop_cleanup`
            （:692），会真再叫一次 provider，认账了才结算＋放卡。
          反过来 PROVISIONING 两样都不满足：`_execute_provision` 的幂等前置
          （status ∈ {RUNNING, PROVISIONING} 直接 return）会把 durable 重试变成一次
          空转并被 worker 记成 SUCCEEDED；而不放行时 provider 恰恰说 ALIVE，
          `reconcile_all` 的 PROVISIONING+ALIVE 档会把它 adopt 成 RUNNING —— 一个没有
          端口、没有凭据、provision 明确失败的 runtime 被对外的 status 宣布在跑。

        `terminal=False`（worker 还会按 attempts 重试）时，放行档的 status 停在 QUEUED：
        把"还要再试"的失败写成 FAILED，等于对读者宣布一个还没下的结论——
        `workspace_operations` 在 API 层零读者，status 是"还在重试"这件事的唯一出口。
        实测形状：共享卡池被借走的那 1s 里第 1 次尝试报 `No GPU available` → 旧写法
        当场 FAILED → 第 2 次尝试成功 → 同一个 workspace 又回到 RUNNING（FAILED→RUNNING
        的复活），期间 GET /api/workspaces/{id} 读到的是假死。常驻对照见
        tests/test_worker.py::test_retryable_provision_failure_is_not_published_as_terminal。
        不放行档不分 terminal：STOPPING 不是终态，因而它同样没有替 worker 下结论；
        两档在这里优先的是同一件事 —— 卡不得在 runtime 还活着时回池。

        终态那一路的持久化要求不变：release 异常时回滚只撤销 release 的局部修改，
        随后重新置位 + error_message + 清 GPU 字段；GPU 残留由 reconcile 补做。
        """
        # §18：启动失败 → 圈住的额度原样退回（不产生任何账本条目；重试那一轮会重新圈）。
        # 这一步在准入判据**之外**：额度轴与资源轴是两件事，卡放不放不该决定退款做不做。
        if self.billing is not None:
            try:
                self.billing.release_hold(db, workspace.id, reason="provision failed")
            except Exception as exc:  # 释放失败不得掩盖状态置位（盲捕获有意，见 ADR 0002）
                logger.warning("hold release failed for %s: %s", workspace.id[:8], exc)
        if self._release_admitted(workspace, command_succeeded=command_succeeded):
            # 放行 = provider 认账 runtime 已退役 ⇒ 今天的全部行为保持（放卡 + 清列 + 该档状态）
            settled = WorkspaceStatus.FAILED.value if terminal else WorkspaceStatus.QUEUED.value
            workspace.status = settled
            workspace.error_message = message
            # 先 flush：release 成功时其内部 commit 会连同状态置位一并落库
            db.flush()
            try:
                self.scheduler.release(db, workspace.id)
            except Exception as exc:
                db.rollback()
                # 不得因 release 失败回滚状态置位：重新置位（release 失败可被 reconcile 补做）
                logger.warning(
                    "workspace %s GPU release failed during fail: %s (will be reconciled)",
                    workspace.id[:8], exc,
                )
                workspace.status = settled
                workspace.error_message = message
            # 放行即"这一次尝试没有留下运行时"：不得继续声称占有 GPU（字段一并清除）
            workspace.gpu_id = None
            workspace.gpu_index = None
            workspace.gpu_name = None
            db.flush()
            return
        observed = _observed_runtime_state(self.provider, workspace)
        workspace.status = WorkspaceStatus.STOPPING.value
        workspace.error_message = (
            f"{message}; GPU not released: provider reports runtime {observed} "
            f"and the runtime was not retired "
            f"({cleanup_error or 'compensating destroy reported success'})"
        )
        # 不放行时没有 `scheduler.release` 内部的那次 commit，状态与原因必须自己落库：
        # "这张卡还被活着的 runtime 吃着"是不能只待在内存里等调用方顺手 commit 的事实。
        db.commit()
        logger.warning(
            "workspace %s provision failed but GPU release is not admitted (runtime %s)",
            workspace.id[:8], observed,
        )

    # ------------------------------------------------------------------
    # stop / destroy（同步 API 语义）
    # ------------------------------------------------------------------
    def stop(self, db: Session, workspace: Workspace) -> Workspace:
        """STOP 生命周期：streaming.stop → runtime.stop →（确认 runtime 已不在）→ settle → release。

        全部步骤幂等；重试（再次 stop）会**重新尝试**停止 runtime，而不是只做结算与释放
        （改前形状：FAILED 直接进 `_finalize_stop`，容器还在吃卡却被判 STOPPED 且 GPU 已释放
        —— 与 `destroy()` 里"provider 清理失败不得释放 GPU / 置 DELETED"守的是同一条不变量）。

        没达成目标时状态**留在 STOPPING**，不写 FAILED：ADR 0002 的修订（"可重试的失败不得
        写成终态"）同样适用于 STOP，而且 `recover_stuck_gpu_allocations` 只保护非终态
        {PROVISIONING, RUNNING, STOPPING}——写成 FAILED 会被它按"终态孤儿"把卡放掉，
        等于绕开本函数的全部准入判据。收敛交给 operation 重试与 reconcile 那条 level-triggered 路。
        """
        if workspace.status == WorkspaceStatus.STOPPED.value:
            # 幂等补做：STOPPED 只可能来自一次成功的 release（release 失败会留在 STOPPING），
            # 所以这一档不需要再叫 provider，只把结算/释放按幂等补做一遍。
            self._finalize_stop(db, workspace)
            db.commit()
            db.refresh(workspace)
            return workspace
        if workspace.status != WorkspaceStatus.STOPPING.value:
            workspace.status = WorkspaceStatus.STOPPING.value
            db.commit()
        error = self._stop_cleanup(db, workspace)
        if error is not None:
            workspace.error_message = error
        db.commit()
        db.refresh(workspace)
        return workspace

    def _stop_cleanup(self, db: Session, workspace: Workspace) -> str | None:
        """跑完 STOP 的清理序列。返回 None = 目标达成；返回字符串 = 没达成的原因。

        ADR 0002：provider/scheduler 边界的任意异常都转成"原因字符串"，不得泄漏给调用方
        （`stop()` 的调用面是 durable worker 与配额 monitor，不是 HTTP 请求处理器）。
        释放 GPU 只发生在 `_release_admitted` 认账之后 —— 命令的成败不替 runtime 事实背书。
        """
        stop_error: str | None = None
        try:
            # 1) 先终结流媒体会话并释放流端口
            self.streaming.terminate_for_workspace(db, workspace.id)
            # 2) runtime stop（重试也真叫一次：引擎对已停/已无容器回 304/204，不是错误）
            self.provider.stop(workspace)
            # 3) 释放 GPU 的准入：以 provider 观测到的 runtime 事实为准
            admitted = self._release_admitted(workspace, command_succeeded=True)
        except Exception as exc:
            stop_error = str(exc)
            # 命令报错也不等于 runtime 还在：provider 亲口说没了就照样收尾
            admitted = self._release_admitted(workspace, command_succeeded=False)
        if not admitted:
            # 原因里写"provider 亲口说了什么"，不写死某一种子情形：没放行有两种原因
            # （自述还活着／压根问不到），只说其中一种的那条消息在另一种场合就是假话。
            # `_observed_runtime_state` 只做诊断复读，判决仍由上面那一次判据决定。
            return stop_error or (
                f"release not admitted: provider reports runtime "
                f"{_observed_runtime_state(self.provider, workspace)}"
            )
        if stop_error is not None:
            logger.warning(
                "workspace %s stop command errored (%s) but runtime is confirmed gone",
                workspace.id[:8], stop_error,
            )
        # 4) 结算 + 释放 GPU + STOPPED
        try:
            self._finalize_stop(db, workspace)
        except Exception as exc:  # scheduler.release 失败：留在 STOPPING，由 reconcile/重试补做
            return f"stop finalize failed: {exc}"
        return None

    def _release_admitted(self, workspace: Workspace, *, command_succeeded: bool) -> bool:
        """释放 GPU 的唯一准入判据：以 provider 观测到的 runtime 事实为准，而不是调用返回码。

        - 清理命令成功：只要 provider 不再自述 ALIVE 就放行。UNKNOWN 是"没有可观测
          runtime"那一档（mock，见 providers/base.py:12-15 与 providers/mock.py:72-74），
          演示路径必须停得下来。
        - 清理命令失败：只认 MISSING —— provider 亲口说 runtime 不在了（K8s 对已删
          Deployment 的 404 走这一极，providers/k8s.py:328-329 会抛错），此时不放行就是
          把 GPU 永久钉死在一张已经没有使用者的卡上。
        其余一律不释放：把 GPU 从还在吃卡的容器手里放掉就是「一卡双跑」。

        取径见本轮登记行（Kubernetes finalizer 的"在用资源不得判为已删除"＋
        moby Engine API 把 304/404 都算停止成功，即"停没停"由引擎自述）。
        `command_succeeded` 语义对 destroy 同样成立，供 provision 失败那一路复用 ——
        N-67 起 `_fail` 是它的第四个消费位（前三位：本函数上下两档、warm pool 的 claim 撤销档）。
        第三种结果：问不到。provider 连"还在不在"都答不上来（`reconcile` 自己抛错）时
        判不出，一律按"不放行"处理 —— ADR 0008 同一口径（存储问不到不等于对象不存在，
        这里：runtime 问不到不等于卡没人吃）。这一档也必须留在这里而不是各消费位自己
        try：`_stop_cleanup` 的报错档是在 except 处理器里第二次问的，异常从那里冒出去
        就会穿出 ADR 0002 划的 provider/scheduler 边界。
        """
        try:
            state = self.provider.reconcile(workspace)
        except Exception as exc:
            logger.warning(
                "workspace %s release admission undecidable (provider raised: %s)"
                " — treated as not admitted",
                workspace.id[:8], exc,
            )
            return False
        if command_succeeded:
            return state != RuntimeState.ALIVE
        return state == RuntimeState.MISSING

    def _settle_run(self, db: Session, workspace: Workspace) -> int:
        """结算当前运行段，并把累计秒数重算为账本投影（幂等）；返回账本实际入账的秒数。

        契约（N-74 的 C1 立的极，本轮不动）：返回的是**账本认下的那一段的秒数**，
        不是重放时重算出来的 elapsed。要"账本这一次净新增了多少"的是指标那一侧，
        它走 `_settle_run_delta`（N-75），不要拿本方法的返回值当它。
        """
        booked, _delta = self._settle_run_delta(db, workspace)
        return booked

    def _settle_run_delta(self, db: Session, workspace: Workspace) -> tuple[int, int]:
        """结算当前运行段，返回 `(账本认下的这一段秒数, 账本本次净新增秒数)`。

        idempotency_key 由 `ledger.usage_idempotency_key(workspace.id, started_at.isoformat())`
        给出（全仓唯一一处键构造），与 settle_workspace_run 内部一致：同一运行段重复
        结算不会重复扣款。传的是**原始列值** `workspace.started_at.isoformat()`，
        不做时区归一 —— 读侧 `segment_booked` 用同一把键认这一段。
        累计列不靠 `+=` 维护（N-64）：幂等键只保证不重复扣款，重放时 `run_seconds`
        会按已经流逝的时间变得更大，`+=` 会让展示用量与配额门禁比账本多算一截。
        取自账本的投影既能自愈这类历史膨胀值，也让返回值与扣款同源——hold 转正
        据此行动（`capture_hold(usage_seconds=booked)`），不再拿账本没认过的数当事实。

        第二个读数是为 N-75 加的：`GPU_SECONDS` 是 **Counter**，而"这一段值多少秒"在
        重放时照样是 30（幂等键命中已有行，`entry.gpu_seconds` 读的就是那一行），
        拿它去 `inc` 就把同一段计两次——实测 stop 重试与 reconcile_all 两路都是 60 vs 账本 30。
        Counter 只能单调增，所以它该加的是**账本的增量**：结算前后各读一次
        `settled_gpu_seconds`，`delta = after - before`。首结算 `delta == booked`；
        重放时行已存在 ⇒ `after == before` ⇒ `delta == 0`，不新增也不重复新增。
        账本是 append-only（本模块与 ledger 的教义：永不 UPDATE/DELETE 已入账行），
        两次读数之间只有 `settle_workspace_run` 写，所以 `delta` 不会为负；
        真为负就是账本被人动过，`Counter.inc` 会抛 ValueError 而不是被静默夹平（宁可红）。
        无 started_at（尚未开始运行）时返回 `(0, 0)`，且不产生账本/累计副作用。

        抬 `gpu_seconds_total` 也放在这里（N-89），不放在 `_finalize_stop`：账本入一次秒数、
        指标就动一次，而入账点不止停止那一条——`destroy()` 与 `reconcile_all` 的
        RUNNING→MISSING→FAILED 档各自都经 `_settle_running_segment` 入账，实测两路都给 counter
        加 0 而账本入了 30 秒（`docs/ARCHITECTURE.md` 的指标表把这一项写成"已计费的 GPU 秒"，
        少计就是对外报小）。写在这一层，"哪条收尾路径记账"就不再是一个需要每条臂各自记住的问题。
        """
        if not workspace.started_at:
            return 0, 0
        before = self.ledger.settled_gpu_seconds(db, workspace.id)
        started = workspace.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        run_seconds = max(0, int((utcnow() - started).total_seconds()))
        # 幂等结算该运行段 GPU 秒数（同一运行段重复结算不会重复扣款）
        entry = self.ledger.settle_workspace_run(
            db, workspace, run_seconds, workspace.started_at.isoformat()
        )
        # record() 内部提交，所以这里读到的 SUM 已含刚写入的那一行
        after = self.ledger.settled_gpu_seconds(db, workspace.id)
        workspace.accumulated_seconds = after
        booked = (entry.gpu_seconds or 0) if entry is not None else 0
        # §18：hold 随结算转正。**无条件**收口：不足 1 秒的运行段不产生 usage 条目
        # （settle_workspace_run 对 0 秒返回 None），若只在有账本条目时 capture，
        # 这种段会把 pending hold 一直留到超时扫描才回收。
        if self.billing is not None:
            self.billing.capture_hold(
                db,
                workspace.id,
                usage_seconds=booked,
                ledger_usage_key=entry.idempotency_key if entry is not None else None,
            )
        # §25 的实际计费 GPU 秒指标：记的是**账本本次净新增**（N-75），且只在存在运行段时到得了
        # 这一行（无 started_at 已在上面返回）。重放时 delta 为 0，counter 不动。
        from ..metrics import record_gpu_seconds

        record_gpu_seconds(after - before)
        return booked, after - before

    def _finalize_stop(self, db: Session, workspace: Workspace) -> None:
        """终结串流会话 + 结算运行段 + 释放 GPU + STOPPED（幂等；reconcile 与 stop 共用）。"""
        now = utcnow()
        # 只结算：抬 `gpu_seconds_total` 由 `_settle_run_delta` 自己负责（N-89），
        # 这样 destroy／reconcile→FAILED 那两条同样入账的路径不会漏计。
        self._settle_run_delta(db, workspace)
        # 释放 GPU（stop 后释放；幂等）
        self.scheduler.release(db, workspace.id)
        # 写 STOPPED 之前把这一格的串流会话与端口收掉（N-117）。本函数原先是全仓唯一
        # 「写终态却不自己终结」的位点：配对证据在调用链上（`_stop_cleanup:448`、reconcile
        # 的 STOPPING 档 `:830` 都先终结过），而 `stop():421` 的幂等补做档只重跑本函数——
        # 一旦某趟 `_stop_cleanup` 的终结先抛错（它与 provider.stop 同在 `:446-452` 那个 try，
        # except 不 rollback），STOPPED 就与仍为 connected 的会话一起提交，之后每次重试都补不回来。
        # 该调用只动库、无活动会话时返回 0、重复调用同结果，所以与上面那两处并存不是重复劳动。
        self.streaming.terminate_for_workspace(db, workspace.id)
        workspace.status = WorkspaceStatus.STOPPED.value
        workspace.stopped_at = now
        workspace.started_at = None

    def _settle_running_segment(self, db: Session, workspace: Workspace) -> None:
        """结算当前 RUNNING 段并累计秒数（供 destroy/delete 复用）。"""
        if workspace.status == WorkspaceStatus.RUNNING.value and workspace.started_at:
            self._settle_run(db, workspace)
            db.commit()

    def destroy(self, db: Session, workspace: Workspace) -> None:
        """DESTROY 生命周期（soft delete / tombstone 语义）：
        streaming.stop → runtime.destroy → billing settle → GPU release → DELETED。

        workspace 行保留（deleted_at tombstone）用于 billing/audit/deployment
        history/安全调查；API 默认不返回 deleted workspace。全部步骤幂等。
        """
        if workspace.deleted_at is not None:
            return  # 已 tombstone，幂等
        try:
            # 1) 先终结流媒体会话（幂等）
            self.streaming.terminate_for_workspace(db, workspace.id)
        except Exception:
            db.rollback()
        try:
            self._settle_running_segment(db, workspace)
        except Exception as exc:
            db.rollback()
            # 结算失败不得静默丢账：留下可审计痕迹（error_message + ERROR 日志），
            # 但不阻断资源释放与 DELETED 置位（不能让一次账本故障把容器和卡钉住）。
            # 这一段的账**就是不入账，也没有补做者**（原判据写「可补偿」是未兑现的主张，
            # N-105 查清后改口）：`reconcile_all` 跳过 tombstone、quota monitor 只选
            # `deleted_at IS NULL`、回收器只管卡不管账。故意不去"补"的理由是时钟：
            # `_settle_run` 取 `utcnow() - started_at`，对墓碑格那就是把等待时长算成运行时长。
            # 判据 `tests/test_streaming_lifecycle.py` 的 N-105 两档（零条目 + started_at 已清；
            # 对照档证明"零"不是"destroy 从不结算"）。
            workspace.error_message = f"destroy settle failed: {exc}"
            logger.error("workspace %s destroy settle failed: %s", workspace.id[:8], exc)
        try:
            self.provider.destroy(workspace)
        except Exception:
            # provider 清理失败（如 docker daemon 不可用）→ 不得释放 GPU / 置 DELETED：
            # 否则孤儿容器仍持有 --gpus device=N，却被释放 GPU → 一卡双跑。
            # 上抛让 DESTROY operation 重试（ADR 0002 边界），重试时再补做清理。
            db.rollback()
            raise
        try:
            self.scheduler.release(db, workspace.id)
        except Exception:
            db.rollback()
        # tombstone：状态 DELETED + deleted_at，行保留（仅 provider 清理成功后置位）
        workspace.status = WorkspaceStatus.DELETED.value
        workspace.deleted_at = utcnow()
        workspace.started_at = None
        db.commit()

    # ------------------------------------------------------------------
    # reconciliation（替代 crash_recovery 的"停掉所有 RUNNING"）
    # ------------------------------------------------------------------
    def reconcile_all(
        self,
        *,
        limit: int | None = None,
        older_than_seconds: int | None = None,
    ) -> dict[str, int]:
        """把 DB 状态收敛到 runtime 事实。幂等：连续执行多次结果一致。

        规则：
        - DB RUNNING + runtime ALIVE  → 保持（adopt）
        - DB RUNNING + K8s 节点不一致 → 先走 `_stop_cleanup`（provider.stop + 准入），
          认账了才 FAILED；没认账保持 RUNNING，下一轮接着试（N-68）
        - DB RUNNING + runtime MISSING → 结算确认的 usage → 释放 GPU → FAILED
        - DB PROVISIONING + runtime ALIVE → RUNNING（adopt）
        - DB PROVISIONING + runtime MISSING → 无 active operation 则重新入队（retry）
        - DB STOPPING + runtime ALIVE → 再试停止；确认不在才结算 → 释放 → STOPPED，
          仍未确认则保持 STOPPING 等下一轮（不释放 GPU）。provision 失败且释放准入没放行
          的那一格（N-67，`_fail` 写 STOPPING）就靠这一档被接着退役。
        - DB STOPPING + runtime 停止/缺失 → 结算 → 释放 → STOPPED
        - QUEUED/CREATED + runtime MISSING → 重新入队 PROVISION
        - UNKNOWN（mock 无真实 runtime）→ 不动，避免误杀
        - 任意一格本轮抛错 → 记 `errors` 并继续看其余格（N-90：一格的故障
          不得把整趟收敛 abort 掉，否则后面的 runtime 这轮没人看）
        - `older_than_seconds` / `limit` 都给定时走**定向档**（周期驱动者用，N-98 的代价那一条）：
          只碰「比阈值老」且**队列没有在做**的格，且一趟最多看 `limit` 格。不抢队列的格既是
          正确性要求（同一格两个判决源会互相退让），也是成本要求：每格一次 provider 往返
          （本机实测 `docker inspect` 中位 93.6 ms／p95 217 ms），而这趟是在 worker 线程里跑的。
          两个参数都不给＝今天的全量扫描，`run_crash_recovery` 与既有判据走的就是这一档。

        `scanned` 是本轮真正看过的格数、`skipped` 是因定向档被让过的格数；全量档下
        `skipped` 恒为 0（两格都在返回值里，形状不随档位变）。
        """
        stats = {
            "adopted": 0,
            "failed": 0,
            "stopped": 0,
            "requeued": 0,
            "kept": 0,
            "errors": 0,
            "scanned": 0,
            "skipped": 0,
        }
        with self.session_factory() as db:
            for w in list(db.scalars(select(Workspace).order_by(Workspace.created_at))):
                if w.deleted_at is not None:
                    continue  # tombstone：不参与 reconcile
                if w.status in {WorkspaceStatus.STOPPED.value, WorkspaceStatus.FAILED.value}:
                    continue
                if not self._reconcile_cell_admitted(db, w, older_than_seconds):
                    stats["skipped"] += 1
                    continue
                if limit is not None and stats["scanned"] >= limit:
                    break  # 有界：一趟最多看 limit 格，其余留给下一趟（按 created_at 轮转）
                stats["scanned"] += 1
                try:
                    self._reconcile_one(db, w, stats)
                    # 当场落库：下一格炸掉时的 `db.rollback()` 不能把**已经收敛完的格子**
                    # 一起退回（各臂内部的 release/enqueue 自带提交，status/error_message
                    # 这两笔在这里补上，异常边界才是可用的而不是名义上的）。
                    db.commit()
                except Exception as exc:  # 一格失败不得中止整趟（N-90）
                    db.rollback()
                    stats["errors"] = stats.get("errors", 0) + 1
                    logger.warning(
                        "reconcile: workspace %s 这一格本轮无法收敛（其余格照看）: %s",
                        w.id[:8], exc,
                    )
            db.commit()
            recover_stuck_gpu_allocations(db)
        return stats

    def _reconcile_cell_admitted(
        self, db: Session, w: Workspace, older_than_seconds: int | None
    ) -> bool:
        """定向档的准入判定：队列没在这格上做事、且这格比阈值老。

        `older_than_seconds=None`（全量档）一律准入，`reconcile_all` 的默认行为因此一位未变。
        「队列没在做」既是正确性也是礼貌：一个 STOP operation 还在 pending/retrying 时，
        它的格子由 worker 的 claim/lease/attempts 那条路负责；这里再插一脚就是两个判决源
        互相回退。等 attempts 打满 `MAX_ATTEMPTS` 变成 FAILED 之后，活跃谓词不再为真，
        这一档才接手——那正是 N-98 里「被拒的格没人再试」的那一站。
        """
        if older_than_seconds is None:
            return True
        if self._has_active_operation(db, w.id):
            return False
        # 时间基准取"最后一次真实状态变化"：STOPPING 格看 stopped_at（若有）否则 started_at，
        # 再兜到 created_at（该列非空，所以这里不需要再兜 None）。
        reference = w.stopped_at or w.started_at or w.created_at
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=UTC)
        return (utcnow() - reference).total_seconds() >= older_than_seconds

    def _reconcile_one(self, db: Session, w: Workspace, stats: dict[str, int]) -> None:
        """一格 workspace 的收敛判决——`reconcile_all` 的循环体逐字搬进来。

        单独成函数只为给**一格**一个异常边界（N-90）：`provider.reconcile` 或
        `scheduler.release` 抛错时只本格不收敛，后面的格子照样被看过。
        ADR 0002 给 provider/scheduler 边界立的规矩是「异常转成原因、不外泄给调用面」，
        这里的调用面是 worker 线程与启动恢复——泄漏出去会连累其余所有 runtime。
        """
        state = self.provider.reconcile(w)
        # §10：K8s 路径校验 Pod 实际 nodeName == reservation.node_name
        node_mismatch = False
        if (
            w.status == WorkspaceStatus.RUNNING.value
            and w.provider in {"k8s", "kubernetes"}
            and w.gpu_id is not None
        ):
            gpu = db.get(Gpu, w.gpu_id)
            host = db.get(GpuHost, gpu.host_id) if gpu is not None else None
            reserved_node = host.name if host is not None else None
            if reserved_node:
                try:
                    runtime = self.provider.inspect(w)
                except Exception:
                    runtime = {}
                actual_node = (
                    runtime.get("node_name") or runtime.get("node")
                )
                if actual_node and actual_node != reserved_node:
                    node_mismatch = True
                    logger.warning(
                        "workspace %s: pod on node %r but reserved %r",
                        w.id[:8], actual_node, reserved_node,
                    )
        if w.status == WorkspaceStatus.RUNNING.value:
            if node_mismatch:
                # 分配节点与实际运行节点不一致 → 不得保持 RUNNING。但"卡回池"的前提
                # 仍然是 runtime 事实不在（N-63 的同一条准入）：改前这里是
                # settle + release + FAILED 三连，一次都不叫 provider.stop ⇒ pod 还在
                # 错的节点上吃卡，卡却已回池（同类第三实例）。现在先走 `_stop_cleanup`
                # 完整清理，认账了才置 FAILED；没认账就保持 RUNNING 并把原因留在
                # error_message 上，下一轮这条分支接着重试（RUNNING 在
                # recover_stuck_gpu_allocations 的保护集内，卡不会被它放掉）。
                w.error_message = (
                    f"reconciled: pod node mismatch (actual={actual_node}, reserved={reserved_node})"
                )
                error = self._stop_cleanup(db, w)
                if error is None:
                    w.status = WorkspaceStatus.FAILED.value
                    stats["failed"] += 1
                else:
                    logger.warning(
                        "reconcile: node-mismatch stop not admitted for %s: %s",
                        w.id[:8], error,
                    )
            elif state == RuntimeState.ALIVE:
                stats["kept"] += 1  # adopt：继续运行
            elif state == RuntimeState.MISSING:
                # runtime 自己没了 ⇒ 这一格的串流会话与端口也必须一起终结。STOP 与
                # DESTROY 两档本来就调它，只有这条"没人叫过 stop"的路劲漏了：结果是
                # workspaces 说 FAILED、streaming_sessions 还说 connected，
                # `GET /api/streaming/workspace/{id}` 与前端那行端口成了假活（N-116）。
                # 该函数只动库（不叫 provider）、无活动会话时返回 0，重复调用同结果。
                self.streaming.terminate_for_workspace(db, w.id)
                self._settle_running_segment(db, w)
                self.scheduler.release(db, w.id)
                w.status = WorkspaceStatus.FAILED.value
                w.error_message = "reconciled: runtime missing"
                w.stopped_at = utcnow()
                w.started_at = None
                stats["failed"] += 1
            # UNKNOWN：无真实 runtime 可判定，保守不动
        elif w.status == WorkspaceStatus.PROVISIONING.value:
            if state == RuntimeState.ALIVE:
                # 实际已就绪（如容器先于 DB 提交）：adopt
                w.status = WorkspaceStatus.RUNNING.value
                w.started_at = w.started_at or utcnow()
                w.stopped_at = None
                stats["adopted"] += 1
            elif state == RuntimeState.MISSING:
                if not self._has_active_operation(db, w.id):
                    enqueue_operation(db, w.id, OperationType.PROVISION)
                    stats["requeued"] += 1
        elif w.status == WorkspaceStatus.STOPPING.value:
            if state == RuntimeState.ALIVE:
                # runtime 仍存活：再走一轮完整清理（含 provider.stop）。改前是
                # `contextlib.suppress` 吞掉停止失败后无条件 `_finalize_stop` ——
                # 与 stop() 的重试路径是同一个缺陷的第二处；两路现在共用
                # `_stop_cleanup`，准入判据只有 `_release_admitted` 一份。
                error = self._stop_cleanup(db, w)
                if error is None:
                    stats["stopped"] += 1
                else:
                    # 仍未确认：保持 STOPPING（recover_stuck_gpu_allocations 因此
                    # 不会把这张还有人吃的卡放掉），下一轮接着试
                    logger.warning("reconcile: stop incomplete for %s: %s", w.id[:8], error)
            elif state == RuntimeState.MISSING:
                # runtime 已停（或从未起来）：结算 + 释放
                self._finalize_stop(db, w)
                stats["stopped"] += 1
        elif w.status in {
            WorkspaceStatus.QUEUED.value,
            WorkspaceStatus.CREATED.value,
        } and not self._has_active_operation(db, w.id):
            enqueue_operation(db, w.id, OperationType.PROVISION)
            stats["requeued"] += 1


    def _has_active_operation(self, db: Session, workspace_id: str) -> bool:
        return (
            db.scalar(
                select(WorkspaceOperation.id).where(
                    WorkspaceOperation.workspace_id == workspace_id,
                    WorkspaceOperation.status.in_(
                        [
                            OperationStatus.PENDING.value,
                            OperationStatus.RUNNING.value,
                            OperationStatus.RETRYING.value,
                        ]
                    ),
                )
            )
            is not None
        )

    def release_expired_holds(self) -> int:
        """周期回收超时 pending hold（§18 崩溃残留兜底；RUNNING 的段不回收）。"""
        with self.session_factory() as db:
            if self.billing is None:
                return 0
            return self.billing.release_expired_holds(db)

    def monitor_runtime_quotas(self) -> dict[str, int]:
        """§12 active-runtime quota monitor：防止 workspace 无限跑成大额负数。

        对每个 RUNNING workspace：
        - 投影余额 = 当前余额 - 本运行段 live 消耗（预估）
          投影余额 < 0（即将透支）→ 优雅停止（settle + release + STOPPED）
        - course quota：模板对应 lab 配额用尽 → 同样停止
        幂等：停止经 `stop()` → `_stop_cleanup`（provider.stop ＋ 释放准入 ＋ 幂等结算/释放），
        没被认账的停止不把 workspace 写成终态，也不计入已停数；重复 monitor 不重复扣费。
        """
        stats = {"stopped": 0, "scanned": 0, "notified": 0}
        with self.session_factory() as db:
            # 复用注入的 BillingPolicy（构造属性），无 policy 时整段跳过（如测试/无计费环境）
            if self.billing is None:
                logger.debug("quota monitor skipped: no billing policy configured")
                return stats
            running = db.scalars(
                select(Workspace).where(
                    Workspace.status == WorkspaceStatus.RUNNING.value,
                    Workspace.deleted_at.is_(None),
                )
            ).all()
            for w in running:
                if w.user_id is None:
                    continue  # warm pool（无归属）不监控计费
                stats["scanned"] += 1
                user = db.get(User, w.user_id)
                if user is None:
                    continue
                # 投影余额：当前余额 - 本运行段已消耗
                live = 0
                if w.started_at:
                    started = w.started_at
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=UTC)
                    live = max(0, int((utcnow() - started).total_seconds()))
                # 口径与 `available_credits` 同源：这份加法全仓只写在 `gross_credits` 里
                # （原先这里再抄一遍 `balance + organization_balance`，三处口径会分叉）
                projected = self.billing.gross_credits(db, user) - live
                # course quota 检查
                labs = db.scalars(
                    select(Lab).where(Lab.template_id == w.template_id)
                ).all()
                quota_exceeded = any(
                    self.billing.course_usage_seconds(db, user.id, lab) >= lab.quota_seconds
                    for lab in labs
                )
                if projected < 0 or quota_exceeded:
                    reason = (
                        "quota monitor: credits exhausted" if projected < 0
                        else "quota monitor: course quota reached"
                    )
                    self.stop(db, w)  # 幂等：streaming→runtime→确认不在→settle→release→STOPPED
                    if w.status == WorkspaceStatus.STOPPED.value:
                        w.error_message = reason
                        db.commit()
                        stats["stopped"] += 1
                    else:
                        # 没达成就别报"已停"：这一轮释放准入没放行（runtime 仍被 provider
                        # 自述存活，或 release 失败）。error_message 留 stop() 写下的原因——
                        # 它比配额原因更该被读到；计数不 +1，workspace 仍在Running集合里
                        # 说明它还会被下一轮 monitor 再试。
                        logger.warning(
                            "quota monitor: stop not admitted for %s (%s): %s",
                            w.id[:8], reason, w.error_message,
                        )
        return stats

    def crash_recovery(self) -> dict[str, int]:
        """启动恢复：基于 runtime 事实的 reconciliation（幂等、不误杀存活 runtime）。"""
        return self.reconcile_all()
