"""DeploymentService: artifact 登记 + 部署状态机.

状态机: pending → downloading → verified → running → success/failed.
- checksum 由控制面记录、edge 端实际校验; verify_checksum 提供控制面侧幂等校验.
- owner 隔离与越权 404 语义与 workspaces router 一致 (SECURITY.md T1).
"""

import hashlib
import uuid
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..models import Artifact, DeploymentRecord, DeploymentStatus, Role, Template, Workspace


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DeploymentService:
    def __init__(self, session_factory: sessionmaker[Session], workspace_root: Path) -> None:
        self.session_factory = session_factory
        self.workspace_root = workspace_root

    # ------------------------------------------------------------------
    # 资源访问（owner 隔离；越权一律 404）
    # ------------------------------------------------------------------
    @staticmethod
    def _ensure_owner(db: Session, user, workspace: Workspace) -> None:
        if user.role != Role.ADMIN.value and workspace.user_id != user.id:
            raise HTTPException(404, "workspace not found")

    @staticmethod
    def _template_version(db: Session, workspace: Workspace) -> str:
        template = db.get(Template, workspace.template_id)
        return template.version if template is not None else "0.1.0"

    # ------------------------------------------------------------------
    def create_artifact(self, db: Session, user, workspace: Workspace, path: str) -> Artifact:
        """把 workspace 目录下的文件登记为 Artifact (sha256 + size_bytes).

        path 相对于 workspace_root/{workspace.id}/; 文件必须存在, 且不得逃逸出该目录.
        """
        self._ensure_owner(db, user, workspace)
        base = (self.workspace_root / workspace.id).resolve()
        full = (base / path).resolve()
        if not full.is_relative_to(base):
            raise HTTPException(400, "artifact path escapes workspace directory")
        if not full.is_file():
            raise HTTPException(404, f"artifact file not found: {path}")
        artifact = Artifact(
            id=str(uuid.uuid4()),
            workspace_id=workspace.id,
            name=Path(path).name,
            path=path,
            checksum=_sha256_file(full),
            size_bytes=full.stat().st_size,
            model_version=self._template_version(db, workspace),
        )
        db.add(artifact)
        db.commit()
        db.refresh(artifact)
        return artifact

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

    def verify(self, db: Session, deployment: DeploymentRecord) -> DeploymentRecord:
        """downloading → verified (checksum 由 edge 端校验, 控制面只记录状态)."""
        if deployment.status == DeploymentStatus.VERIFIED.value:
            return deployment
        if deployment.status != DeploymentStatus.DOWNLOADING.value:
            raise self._bad_transition(deployment, "verified")
        deployment.status = DeploymentStatus.VERIFIED.value
        db.commit()
        return deployment

    def verify_checksum(self, db: Session, deployment: DeploymentRecord) -> DeploymentRecord:
        """幂等校验: artifact 当前 checksum 与记录一致 → verified, 否则 failed+error."""
        if deployment.status in {
            DeploymentStatus.VERIFIED.value,
            DeploymentStatus.RUNNING.value,
            DeploymentStatus.SUCCESS.value,
            DeploymentStatus.FAILED.value,
        }:
            return deployment
        artifact = db.get(Artifact, deployment.artifact_id) if deployment.artifact_id else None
        if artifact is None:
            return self._fail(db, deployment, "artifact missing")
        full = self.workspace_root / deployment.workspace_id / artifact.path
        if not full.is_file():
            return self._fail(db, deployment, f"artifact file missing: {artifact.path}")
        current = _sha256_file(full)
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
