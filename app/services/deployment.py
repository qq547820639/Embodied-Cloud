"""DeploymentService: artifact 登记 + 部署状态机.

状态机: pending → downloading → verified → running → success/failed.
- checksum 由控制面记录、edge 端实际校验; verify_checksum 提供控制面侧幂等校验.
- owner 隔离与越权 404 语义与 workspaces router 一致 (SECURITY.md T1).
"""

import hashlib
import shutil
import uuid
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..models import (
    Artifact,
    DeploymentRecord,
    DeploymentStatus,
    Role,
    Template,
    TemplateVersion,
    Workspace,
)
from .artifact_store import ArtifactStore, LocalArtifactStore
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
    ) -> DeploymentRecord:
        """创建部署记录 (status=pending).

        幂等: 同 (workspace, artifact, robot_type) 已存在 PENDING/RUNNING 则返回现有记录.
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
            except Exception as exc:
                return self._fail(db, deployment, f"artifact object missing: {exc}")
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
        """verified → running; 绑定执行 agent (可选)."""
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
