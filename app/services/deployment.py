"""DeploymentService: artifact 登记 + 部署状态机.

状态机: pending → downloading → verified → running → success/failed.
- checksum 由控制面记录、edge 端实际校验; verify_checksum 提供控制面侧幂等校验.
- owner 隔离与越权 404 语义与 workspaces router 一致 (SECURITY.md T1).
"""

import hashlib
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, sessionmaker

from ..models import (
    Artifact,
    DeploymentRecord,
    DeploymentStatus,
    EdgeAgent,
    Role,
    Template,
    TemplateVersion,
    Workspace,
)
from ..utils import utcnow
from .artifact_store import (
    ArtifactNotFoundError,
    ArtifactStore,
    LocalArtifactStore,
)
from .providers.base import WorkspaceProvider


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DeploymentService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        workspace_root: Path,
        store: "ArtifactStore | None" = None,
        provider: "WorkspaceProvider | None" = None,
    ) -> None:
        self.session_factory = session_factory
        self.workspace_root = workspace_root
        # §9：对象存储协议。默认 LocalArtifactStore(workspace_root)（开发/本地，
        # object_key 即 workspace 目录文件，零行为变化）；生产注入
        # S3CompatibleArtifactStore —— 控制面不直接 mount Workspace PVC。
        self.store = store or LocalArtifactStore(workspace_root)
        # WorkspaceProvider（可选）：控制面本地读不到 workspace 产出文件时，
        # 经 provider.pull_artifact 从 runtime 拉取（K8s 下数据在 workspace Pod
        # 的 PVC，控制面 Pod 读不到）。None = 仅走本地文件系统（现状行为，
        # mock/docker 单机 workspace_root 共享时可用）。
        self.provider = provider

    # ------------------------------------------------------------------
    # 资源访问（owner 隔离；越权一律 404）
    # ------------------------------------------------------------------
    @staticmethod
    def _ensure_owner(db: Session, user, workspace: Workspace) -> None:
        if user.role != Role.ADMIN.value and workspace.user_id != user.id:
            raise HTTPException(404, "workspace not found")

    @staticmethod
    def _template_version(db: Session, workspace: Workspace) -> str:
        """§11：从 Workspace 引用的 TemplateVersion 取版本号（不用 Template.version 猜）。

        旧数据（template_version_id 为空）→ 按 Template.version 兜底。
        """
        if workspace.template_version_id is not None:
            version = db.get(TemplateVersion, workspace.template_version_id)
            if version is not None:
                return version.version
        template = db.get(Template, workspace.template_id)
        return template.version if template is not None else "0.1.0"

    @staticmethod
    def _object_key(workspace: Workspace, path: str) -> str:
        return f"{workspace.id}/{path}"

    # ------------------------------------------------------------------
    def create_artifact(self, db: Session, user, workspace: Workspace, path: str) -> Artifact:
        """把 Workspace 产出登记为 Artifact：读文件 → ArtifactStore.put → DB 记录。

        path 相对于 workspace_root/{workspace.id}/; 文件必须存在, 且不得逃逸出该目录.

        读取来源二选一：
        1) 本地文件存在（mock/docker 单机，workspace_root 共享）→ 直接读；
        2) 本地不存在 → provider.pull_artifact 从 workspace runtime 拉取临时文件
           （K8s 下数据在 workspace Pod PVC，控制面本地读不到）→ 校验后入 store，
           finally 清理临时文件。
        """
        self._ensure_owner(db, user, workspace)
        base = (self.workspace_root / workspace.id).resolve()
        full = (base / path).resolve()
        if not full.is_relative_to(base):
            raise HTTPException(400, "artifact path escapes workspace directory")
        tmp_file: Path | None = None
        if full.is_file():
            source: Path = full
        elif self.provider is not None:
            try:
                tmp_file = self.provider.pull_artifact(workspace, path)
            except FileNotFoundError as exc:
                raise HTTPException(404, f"artifact file not found: {path}") from exc
            except Exception as exc:
                raise HTTPException(500, f"failed to pull artifact from workspace: {exc}") from exc
            if not tmp_file.is_file() or tmp_file.stat().st_size <= 0:
                shutil.rmtree(tmp_file.parent, ignore_errors=True)
                tmp_file = None
                raise HTTPException(404, f"artifact file not found: {path}")
            source = tmp_file
        else:
            raise HTTPException(404, f"artifact file not found: {path}")
        try:
            object_key = self._object_key(workspace, path)
            # 写入对象存储（生产 S3；开发 Local）。Local 下 object_key 即原文件，
            # 写入内容与原文件一致（幂等覆盖）。
            self.store.put(object_key, source.read_bytes())
            artifact = Artifact(
                id=str(uuid.uuid4()),
                workspace_id=workspace.id,
                name=Path(path).name,
                path=path,
                object_key=object_key,
                content_type="application/octet-stream",
                store_name=self.store.name,
                checksum=_sha256_file(source),
                size_bytes=source.stat().st_size,
                model_version=self._template_version(db, workspace),
            )
            db.add(artifact)
            db.commit()
            db.refresh(artifact)
            return artifact
        finally:
            # 拉取的临时文件由调用方负责清理（Provider 契约）：
            # pull_artifact 用 mkdtemp 建目录，清理时连父目录一并移除，避免空目录泄漏。
            if tmp_file is not None:
                shutil.rmtree(tmp_file.parent, ignore_errors=True)

    def deploy(
        self,
        db: Session,
        user,
        workspace: Workspace,
        artifact: Artifact,
        robot_type: str,
        edge_agent=None,
        run_deadline_seconds: int | None = None,
    ) -> DeploymentRecord:
        """创建部署记录 (status=pending).

        幂等: 同 (workspace, artifact, robot_type) 已存在 PENDING/RUNNING 则返回现有记录.
        `run_deadline_seconds` 是这次运行自带的时长预算（N-134）；只在**新建**那一行写，
        幂等复用的旧行不接新预算——改一条已经在飞的运行的时钟，等于让第二次 POST
        悄悄推翻第一次的判决前提。
        """
        self._ensure_owner(db, user, workspace)
        existing = db.scalar(
            select(DeploymentRecord).where(
                DeploymentRecord.workspace_id == workspace.id,
                DeploymentRecord.artifact_id == artifact.id,
                DeploymentRecord.robot_type == robot_type,
                DeploymentRecord.status.in_(
                    [DeploymentStatus.PENDING.value, DeploymentStatus.RUNNING.value]
                ),
            )
        )
        if existing is not None:
            # 幂等复用的那条若还没人认领，本次指定的 agent 就是它的执行设备；
            # 已绑别的设备时不改——复用不抢别人名下的任务。
            if (
                edge_agent is not None
                and existing.status == DeploymentStatus.PENDING.value
                and existing.edge_agent_id is None
            ):
                existing.edge_agent_id = edge_agent.id
                db.commit()
                db.refresh(existing)
            return existing
        deployment = DeploymentRecord(
            id=str(uuid.uuid4()),
            workspace_id=workspace.id,
            artifact_id=artifact.id,
            model_version=artifact.model_version,
            template_version=self._template_version(db, workspace),
            robot_type=robot_type,
            checksum=artifact.checksum,
            status=DeploymentStatus.PENDING.value,
            edge_agent_id=edge_agent.id if edge_agent is not None else None,
            run_deadline_seconds=run_deadline_seconds,
        )
        db.add(deployment)
        db.commit()
        db.refresh(deployment)
        return deployment

    # ------------------------------------------------------------------
    # 状态机
    # ------------------------------------------------------------------
    def download(self, db: Session, deployment: DeploymentRecord) -> DeploymentRecord:
        """pending/downloading → downloading (模拟 edge 拉取)."""
        if deployment.status == DeploymentStatus.DOWNLOADING.value:
            return deployment
        if deployment.status != DeploymentStatus.PENDING.value:
            raise self._bad_transition(deployment, "downloading")
        deployment.status = DeploymentStatus.DOWNLOADING.value
        db.commit()
        return deployment

    # ------------------------------------------------------------------
    # Edge agent 侧通路（§25 / ADR 0007）：发现 → 开始取件 → 取件
    # ------------------------------------------------------------------
    def list_assigned(self, db: Session, agent: EdgeAgent) -> list[DeploymentRecord]:
        """agent token 可见面：只有**绑定给自己**的部署，别的租户/别的设备都读不到。"""
        return list(
            db.scalars(
                select(DeploymentRecord)
                .where(DeploymentRecord.edge_agent_id == agent.id)
                .order_by(DeploymentRecord.created_at)
            )
        )

    def begin_agent_download(
        self, db: Session, agent: EdgeAgent, deployment: DeploymentRecord
    ) -> DeploymentRecord:
        """设备侧发起 pending → downloading（对应 AWS Jobs 的 StartNextPendingJobExecution）。

        为什么必须由 agent 来说这句话：`report_checksum` 只接受 DOWNLOADING
        （§23 防绕过——没下载就上报等于凭空判 VERIFIED），而"已绑定给自己"这一条
        已经在路由层 `_get_deployment_for_agent` 校过。
        写成条件 UPDATE（id + 读到的 status + edge_agent_id）而不是先改对象再 commit：
        并发重复 begin 至多一个赢家，其余的 rowcount=0，读回真值幂等返回。
        """
        if deployment.status == DeploymentStatus.DOWNLOADING.value:
            return deployment
        if deployment.status != DeploymentStatus.PENDING.value:
            raise self._bad_transition(deployment, "downloading")
        result = db.execute(
            update(DeploymentRecord)
            .where(
                DeploymentRecord.id == deployment.id,
                DeploymentRecord.status == DeploymentStatus.PENDING.value,
                DeploymentRecord.edge_agent_id == agent.id,
            )
            .values(status=DeploymentStatus.DOWNLOADING.value)
            # SQL 层比较，避开 ORM 的 in-Python evaluator（同 worker._try_claim）
            .execution_options(synchronize_session=False)
        )
        db.commit()
        db.refresh(deployment)
        winner = int(cast("CursorResult[Any]", result).rowcount or 0) == 1
        if not winner and deployment.status != DeploymentStatus.DOWNLOADING.value:
            # 不是自己写的、又没落到 downloading = 有人抢先推进了一步（verify/run），
            # 对 agent 来说这就是"这条不用取了"，按状态机语义拒绝。
            raise self._bad_transition(deployment, "downloading")
        return deployment

    def read_artifact(
        self, db: Session, deployment: DeploymentRecord
    ) -> tuple[bytes, Artifact]:
        """按记录里的 object_key 从 ArtifactStore 取字节（§9：不碰 workspace 文件系统）。

        key **不来自请求参数**（请求只有 deployment id），所以没有用户可控路径。
        仍做一次前缀复核：一条被手工改写过的 Artifact 行（object_key 指向别的
        workspace 目录）不能借这个端点被读出来——越权一律 404，不泄露存在性。
        """
        artifact = db.get(Artifact, deployment.artifact_id) if deployment.artifact_id else None
        if artifact is None or not artifact.object_key:
            raise HTTPException(404, "artifact not found")
        if not artifact.object_key.startswith(f"{deployment.workspace_id}/"):
            raise HTTPException(404, "artifact not found")
        return self.store.get(artifact.object_key), artifact

    def verify_checksum(self, db: Session, deployment: DeploymentRecord) -> DeploymentRecord:
        """幂等校验: ArtifactStore 中的对象 checksum 与记录一致 → verified, 否则 failed+error.

        防绕过（§23）：必须先 download（PENDING 直接调校验视为绕过，拒绝）；
        终态（verified/running/success/failed）幂等返回。
        """
        if deployment.status in {
            DeploymentStatus.VERIFIED.value,
            DeploymentStatus.RUNNING.value,
            DeploymentStatus.SUCCESS.value,
            DeploymentStatus.FAILED.value,
        }:
            return deployment
        if deployment.status != DeploymentStatus.DOWNLOADING.value:
            raise self._bad_transition(deployment, "verified")
        artifact = db.get(Artifact, deployment.artifact_id) if deployment.artifact_id else None
        if artifact is None:
            return self._fail(db, deployment, "artifact missing")
        # §9：从对象存储获取（不直接读 Workspace PVC / 控制面文件系统）；
        # object_key 为 None = 旧数据（登记于 store 引入前）→ 兼容读原路径
        object_key = artifact.object_key
        if object_key is None:
            legacy = self.workspace_root / deployment.workspace_id / artifact.path
            if not legacy.is_file():
                return self._fail(db, deployment, f"artifact file missing: {artifact.path}")
            current = _sha256_file(legacy)
        else:
            try:
                data = self.store.get(object_key)
            except (ArtifactNotFoundError, FileNotFoundError) as exc:
                return self._fail(db, deployment, f"artifact object missing: {exc}")
            # 其余异常（鉴权失败 / 桶不存在 / 网络故障 / store 自身缺陷）**上抛**：
            # verified|failed 都是终态（见本方法开头的提前返回），在这里吞掉并把
            # 部署写成 FAILED 等于用一次存储不可用永久作废这次部署，且错误信息
            # 会说谎（"object missing"）。让请求以 5xx 结束，记录停在 downloading，
            # 调用方可重验。
            current = hashlib.sha256(data).hexdigest()
        if current == deployment.checksum:
            deployment.status = DeploymentStatus.VERIFIED.value
            deployment.error_message = None
        else:
            deployment.status = DeploymentStatus.FAILED.value
            deployment.error_message = (
                f"checksum mismatch: expected {deployment.checksum}, got {current}"
            )
        db.commit()
        return deployment

    def report_checksum(
        self, db: Session, deployment: DeploymentRecord, actual_sha256: str
    ) -> DeploymentRecord:
        """§10：Edge 上报本地计算的 sha256 → server 比较 → VERIFIED / FAILED。

        - 必须先 download（DOWNLOADING 才能上报；PENDING 直接上报 = 绕过，拒绝）
        - actual == expected(deployment.checksum) → VERIFIED
        - actual != expected → FAILED（tampered download / wrong checksum / 跨部署）
        - 终态幂等：VERIFIED/RUNNING/SUCCESS 重复上报不再改变；
          FAILED 后再上报正确值**不得复活**（防 replay）
        """
        if deployment.status in {
            DeploymentStatus.VERIFIED.value,
            DeploymentStatus.RUNNING.value,
            DeploymentStatus.SUCCESS.value,
        }:
            return deployment
        if deployment.status == DeploymentStatus.FAILED.value:
            return deployment  # 终态防复活（replay 不改变结果）
        if deployment.status != DeploymentStatus.DOWNLOADING.value:
            raise self._bad_transition(deployment, "verified")
        if actual_sha256 == deployment.checksum:
            deployment.status = DeploymentStatus.VERIFIED.value
            deployment.error_message = None
        else:
            deployment.status = DeploymentStatus.FAILED.value
            deployment.error_message = (
                f"checksum mismatch (edge reported): expected {deployment.checksum}, got {actual_sha256}"
            )
        db.commit()
        return deployment

    def run_policy(self, db: Session, deployment: DeploymentRecord, agent=None) -> DeploymentRecord:
        """verified → running; 绑定执行 agent (可选).

        进 running 的那一刻给 `run_started_at` 盖章（N-134）：这条部署自己的预算
        `run_deadline_seconds` 只能相对它量，而 `updated_at` 带 `onupdate`，任何一次与运行
        无关的写都会把它顶新——那等于让每个写者都能重置这条运行的时钟。
        已在 running 的重复调用不重盖章：重跑 `/run` 悄悄把预算窗口拉回起点，
        等于用一个幂等 POST 续命一条永远不会结束的 running。
        """
        if deployment.status == DeploymentStatus.RUNNING.value:
            if agent is not None:
                deployment.edge_agent_id = agent.id
                db.commit()
            return deployment
        if deployment.status != DeploymentStatus.VERIFIED.value:
            raise self._bad_transition(deployment, "running")
        if agent is not None:
            deployment.edge_agent_id = agent.id
        deployment.status = DeploymentStatus.RUNNING.value
        deployment.run_started_at = utcnow()
        db.commit()
        return deployment

    def complete(
        self, db: Session, deployment: DeploymentRecord, success: bool, error: str = ""
    ) -> DeploymentRecord:
        """running → success/failed. 终态幂等: 重复 complete 直接返回."""
        if deployment.status in {DeploymentStatus.SUCCESS.value, DeploymentStatus.FAILED.value}:
            return deployment
        if deployment.status != DeploymentStatus.RUNNING.value:
            raise self._bad_transition(deployment, "success/failed")
        deployment.status = DeploymentStatus.SUCCESS.value if success else DeploymentStatus.FAILED.value
        deployment.error_message = error or None
        db.commit()
        return deployment

    def complete_from_agent_report(
        self, db: Session, agent: EdgeAgent, report: dict | None
    ) -> DeploymentRecord | None:
        """ADR 0007 承诺的那一半：控制面按设备回传的 `edge-run` 收口 running → 终态。

        此前这条链只走了一半——设备把运行结果写成遥测，控制面写进 `telemetry_events`
        之后就没有然后了：`RUNNING` 的唯一出口是用户手工 `POST /deployments/{id}/complete`。
        也就是说一条真跑完的部署会一直挂着，而 ADR 里那句"控制面据此收口"没有实现者。

        授权看**行**，不看 payload：条件 UPDATE 的 WHERE 同时钉着
        `id == report["deployment_id"]`、`status == running`、`edge_agent_id == agent.id`。
        - 越权（这条部署不是你的）：rowcount 0，那一行原样不动；
        - 重复上报：第二条同样 rowcount 0 —— 终态一旦写下，后到的读数改不动它
          （遥测事件本身仍然全部留档，`GET /api/edge/agents/{id}/telemetry` 看得见）；
        - 状态不对（还没 run 就报结果）：不动，不许凭空判成功。

        信任边界如实写明：`ok` 与 `detail` 是设备自报的，控制面无法在本机之外复核
        机器人是否真的动了——这是 ADR 0007 选择"由设备回报收口"时就已经接受的代价，
        设备此时能做的最坏事情是把**自己名下**的部署判成 success/failed，
        碰不到别人的行（WHERE 里的归属列），也改不了已成的终态。
        """
        deployment_id = str((report or {}).get("deployment_id") or "")
        if not deployment_id:
            return None
        success = bool((report or {}).get("ok"))
        detail = str((report or {}).get("detail") or "")
        result = db.execute(
            update(DeploymentRecord)
            .where(
                DeploymentRecord.id == deployment_id,
                DeploymentRecord.status == DeploymentStatus.RUNNING.value,
                DeploymentRecord.edge_agent_id == agent.id,
            )
            .values(
                status=DeploymentStatus.SUCCESS.value if success else DeploymentStatus.FAILED.value,
                error_message=None if success else (detail or "reported failed"),
            )
            # SQL 层比较，避开 ORM 的 in-Python evaluator（同 begin_agent_download）
            .execution_options(synchronize_session=False)
        )
        db.commit()
        if int(cast("CursorResult[Any]", result).rowcount or 0) != 1:
            return None
        return db.get(DeploymentRecord, deployment_id)

    def fail_overdue_runs(self, db: Session, now: datetime | None = None) -> int:
        """running 且过了**这条部署自己**的预算 → failed（N-134）。

        为什么不沿用 N-133 那条全局判活窗口：`edge_agent_offline_after_seconds`（默认 90 s）
        量的是"这台设备最后一次心跳距今多久"，运行预算量的是"这条运行已经开了多久"。
        两个不同的事实取同一个数，结果是双向错：一次合法要跑 20 分钟的巡检会被 90 s
        的静默判死，而一次 5 秒的推理拿到 90 s 等于几乎不设防。借的是 K8s Job 那一格的
        形状——每个 Job 自带 `activeDeadlineSeconds`，相对它自己的 `.status.startTime`
        量（本机装的 client 31.0.0 的字段注释原文：`duration in seconds relative to the
        startTime that the job may be continuously active before the system tries to
        terminate it; value must be positive integer`，
        `.venv/lib/python3.12/site-packages/kubernetes/client/models/v1_job_spec.py:132`）。
        本仓的 `run_started_at` 就是那个 startTime：`updated_at` 带 `onupdate`，任何一次
        与运行无关的写都会把它顶新，不能拿来量预算。

        判据三条都在 SQL 侧选，终态仍由条件 UPDATE 守：
        - `status == running`：设备回报（`complete_from_agent_report`）与掉线收口
          （`expire_stale_agents`）和本条 sweep 是三个写者竞争同一行，第一个赢家定案，
          后到的 rowcount 0——超时的运行是一次**已结束的失败**，不由后到的读数复活；
        - `run_deadline_seconds IS NOT NULL`：没人定过预算就不判。给一个缺省值就是凭空
          替所有部署做决定（ADR 0008 的"未知不等于违规"这条同样适用）；
        - `run_started_at IS NOT NULL`：迁移之前进过 running 的存量行没有起跑时刻，
          不回填、不拿 `updated_at` 顶。

        这一条同时收掉 N-133 故意跳过的那一档：`edge_agent_id IS NULL` 的 running 拿不出
        "最后一次被看见"的证据，但它有自己的 `run_started_at`——预算量的是时间，不需要
        设备在场。于是"人工在库外把部署推到 running 又没人负责"那一格第一次有了归属者。

        逐行的预算换算放在 Python 侧（先按上面三条选出候选，再算谁真的超时，最后用
        `id IN (…)` 加 `status == running` 写）：`run_started_at + N 秒` 这种逐行算术在
        SQLite 与 PostgreSQL 两侧写法不同（`datetime(col,'+N seconds')` 对
        `col + make_interval(secs => N)`），而 ADR 0005 要求这条通路两侧语义对等。
        候选集与写入之间 `run_started_at` 不会被改动——只有"新进入 running"才盖章，
        而候选已经是 running，`run_policy` 对已在 running 的调用不重盖章。
        数字不进 `error_message`：那一列给人读一句话，秒数留在 `run_deadline_seconds`
        与 `run_started_at` 两列里由 `DeploymentOut` 透出去，避免同一事实在两处各自过期。
        """
        anchor = now or utcnow()
        candidates = list(
            db.execute(
                select(
                    DeploymentRecord.id,
                    DeploymentRecord.run_started_at,
                    DeploymentRecord.run_deadline_seconds,
                ).where(
                    DeploymentRecord.status == DeploymentStatus.RUNNING.value,
                    DeploymentRecord.run_deadline_seconds.is_not(None),
                    DeploymentRecord.run_started_at.is_not(None),
                )
            )
        )
        overdue: list[str] = []
        for dep_id, started, seconds in candidates:
            if started is None or seconds is None:  # 列可空：SQL 已筛过，这里只满足类型
                continue
            # SQLite 会把 DateTime(timezone=True) 读成无时区值，与 ledger/orchestrator
            # 同一处口径（`app/services/ledger.py:114`）：无 tz 就当 UTC。
            moment = started if started.tzinfo else started.replace(tzinfo=UTC)
            if anchor - moment >= timedelta(seconds=int(seconds)):
                overdue.append(str(dep_id))
        if not overdue:
            return 0
        result = db.execute(
            update(DeploymentRecord)
            .where(
                DeploymentRecord.id.in_(overdue),
                DeploymentRecord.status == DeploymentStatus.RUNNING.value,
            )
            .values(
                status=DeploymentStatus.FAILED.value,
                error_message="run exceeded its own deadline before reporting",
            )
            # SQL 层比较，避开 ORM 的 in-Python evaluator（同 complete_from_agent_report）
            .execution_options(synchronize_session=False)
        )
        db.commit()
        return int(cast("CursorResult[Any]", result).rowcount or 0)

    @staticmethod
    def _fail(db: Session, deployment: DeploymentRecord, message: str) -> DeploymentRecord:
        deployment.status = DeploymentStatus.FAILED.value
        deployment.error_message = message
        db.commit()
        return deployment

    @staticmethod
    def _bad_transition(deployment: DeploymentRecord, target: str) -> HTTPException:
        return HTTPException(409, f"invalid deployment status transition: {deployment.status} → {target}")

    # ------------------------------------------------------------------
    def list(self, db: Session, user, workspace_id: str | None = None) -> list[DeploymentRecord]:
        """owner 过滤 (admin 可见全部)."""
        stmt = select(DeploymentRecord).join(Workspace, DeploymentRecord.workspace_id == Workspace.id)
        if user.role != Role.ADMIN.value:
            stmt = stmt.where(Workspace.user_id == user.id)
        if workspace_id:
            stmt = stmt.where(DeploymentRecord.workspace_id == workspace_id)
        return list(db.scalars(stmt.order_by(DeploymentRecord.created_at.desc())))
