"""Immutable TemplateVersion（§15）：identity 与版本分离。

约束：
- UNIQUE(template_id, version)
- 已发布版本禁止 mutable update（seed 不允许覆盖已存在 released version）
- Workspace 引用具体 TemplateVersion；启动使用版本镜像（Template A → image A）
"""

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Template, TemplateVersion, Workspace, WorkspaceStatus
from app.seed import SEED_TEMPLATES, seed_templates
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.docker import DockerProvider
from app.services.providers.mock import MockProvider
from app.services.scheduler import GpuInfo, GpuScheduler

ENGINE = create_engine("sqlite:///./test-template-version.db", connect_args={"check_same_thread": False})
Factory = sessionmaker(bind=ENGINE, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    yield
    Base.metadata.drop_all(ENGINE)


def _seed(db) -> None:
    seed_templates(db)
    GpuScheduler(Factory).sync_host(
        db,
        host_id="host-1",
        name="h1",
        address="127.0.0.1",
        provider="mock",
        gpus=[GpuInfo(gpu_uuid="gpu-1", model="RTX", memory_total=24564, index=0)],
    )


def test_seed_creates_released_versions_for_all_templates():
    with Factory() as db:
        _seed(db)
        for spec in SEED_TEMPLATES:
            version = db.scalar(
                select(TemplateVersion).where(
                    TemplateVersion.template_id == spec["id"], TemplateVersion.version == spec["version"]
                )
            )
            assert version is not None
            assert version.released is True
            assert version.image == spec["image"]
            assert version.gpu_requirement_gb == spec["gpu_requirement_gb"]


def test_seed_never_overwrites_released_version():
    """seed 不允许偷偷覆盖已存在的 released version（§15 核心约束）。"""
    with Factory() as db:
        _seed(db)
        version = db.scalar(
            select(TemplateVersion).where(
                TemplateVersion.template_id == "cartpole", TemplateVersion.version == "0.1.0"
            )
        )
        original_image = version.image
        # 修改 seed spec 模拟"模板更新"（指向新镜像）
        SEED_TEMPLATES[0]["image"] = "registry/cartpole:0.2.0"

    with Factory() as db:
        _seed(db)  # 再次 seed

    with Factory() as db:
        version = db.scalar(
            select(TemplateVersion).where(
                TemplateVersion.template_id == "cartpole", TemplateVersion.version == "0.1.0"
            )
        )
        # 已发布版本内容未被覆盖（不可变）
        assert version.image == original_image
        assert version.image != "registry/cartpole:0.2.0"

    # 还原 spec（测试隔离）
    SEED_TEMPLATES[0]["image"] = original_image


def test_unique_template_version_constraint():
    with Factory() as db:
        _seed(db)
        dup = TemplateVersion(
            id="tv-dup",
            template_id="cartpole",
            version="0.1.0",  # 与 seed 冲突
            image="x",
            entrypoint="",
        )
        db.add(dup)
        with pytest.raises(IntegrityError):
            db.commit()


def test_workspace_binds_template_version_and_snapshots_image():
    """Workspace 必须引用具体 TemplateVersion；image 快照来自版本。"""
    with Factory() as db:
        _seed(db)
        orchestrator = WorkspaceOrchestrator(Factory, MockProvider("http://127.0.0.1:8000"), Path("/tmp/tv-test"))  # noqa: S108
        template = db.get(Template, "cartpole")
        version = db.scalar(
            select(TemplateVersion).where(
                TemplateVersion.template_id == "cartpole", TemplateVersion.version == "0.1.0"
            )
        )
        ws = orchestrator.create(db, template, user_id="u1")
        assert ws.template_version_id == version.id
        assert ws.image == version.image == "embodiedcloud/isaaclab-workspace:0.1.0"


class CapturingDockerProvider(DockerProvider):
    def __init__(self, settings):
        super().__init__(settings)
        self.runs: list[list[str]] = []

    def health(self):
        return True, "fake"

    def _streaming_workspace_running(self):
        return False

    def _run(self, args, *, check=True):
        if args[1] == "run":
            self.runs.append(args)
            return type("R", (), {"returncode": 0, "stdout": "c\n", "stderr": ""})()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()


def _template_with_versions(db, template_id: str, image_a: str, image_b: str):
    """模板 identity + 两个 released 版本（不同镜像）。"""
    template = Template(
        id=template_id,
        slug=template_id,
        name=template_id,
        version="0.1.0",
        description="test",
        category="test",
        runtime="isaaclab",
        image=image_a,
        launch_command="echo ok",
        enabled=True,
        recommended_vram_gb=16,
        estimated_hourly_cost_cny=1.0,
    )
    db.add(template)
    db.flush()
    v1 = TemplateVersion(
        id=f"tv-{template_id}-v1",
        template_id=template_id,
        version="0.1.0",
        image=image_a,
        entrypoint="",
        released=True,
    )
    v2 = TemplateVersion(
        id=f"tv-{template_id}-v2",
        template_id=template_id,
        version="0.2.0",
        image=image_b,
        entrypoint="",
        released=True,
    )
    db.add_all([v1, v2])
    db.commit()
    return template


def test_workspace_uses_latest_released_version_image():
    """启动实际 runtime image 必须是 TemplateVersion 的镜像（不同模板不同镜像）。"""
    with Factory() as db:
        _seed(db)
        _template_with_versions(db, "template-a", "registry/template-a:0.1.0", "registry/template-a:0.2.0")
        _template_with_versions(db, "template-b", "registry/template-b:0.1.0", "registry/template-b:0.2.0")
        orchestrator = WorkspaceOrchestrator(Factory, MockProvider("http://127.0.0.1:8000"), Path("/tmp/tv-test2"))  # noqa: S108
        ws_a = orchestrator.create(db, db.get(Template, "template-a"), user_id="u1")
        ws_b = orchestrator.create(db, db.get(Template, "template-b"), user_id="u1")
        # 最新 released 版本（0.2.0）镜像
        assert ws_a.image == "registry/template-a:0.2.0"
        assert ws_b.image == "registry/template-b:0.2.0"
        # 不同模板镜像必须不同且正确
        assert ws_a.image != ws_b.image


def test_docker_run_uses_workspace_version_snapshot(monkeypatch, tmp_path):
    """Docker launch 使用 workspace.image（版本快照）而非 template.image。"""
    from app.config import Settings

    settings = Settings(
        eula_accepted=True,
        workspace_root=tmp_path,
        ide_port_start=38100,
        ide_port_end=38120,
    )
    monkeypatch.setattr("app.services.providers.docker.is_port_free", lambda *a, **kw: True)
    monkeypatch.setattr("app.services.providers.docker.allocate_tcp_port", lambda *a, **kw: 38101)
    provider = CapturingDockerProvider(settings)

    with Factory() as db:
        _seed(db)
        template = Template(
            id="t1",
            slug="t1",
            name="t1",
            description="d",
            category="c",
            runtime="isaaclab",
            image="registry/old-template-image:0.1.0",  # 模板 identity 上的旧镜像
            launch_command="echo ok",
            enabled=True,
            recommended_vram_gb=16,
            estimated_hourly_cost_cny=1.0,
        )
        db.add(template)
        db.flush()
        db.add(
            TemplateVersion(
                id="tv-t1-001",
                template_id="t1",
                version="0.1.0",
                image="registry/version-image:0.1.0",  # 版本决定的镜像
                entrypoint="",
                released=True,
            )
        )
        db.commit()
        orchestrator = WorkspaceOrchestrator(Factory, provider, tmp_path)
        ws = orchestrator.create(db, db.get(Template, "t1"), user_id="u1")
        assert ws.image == "registry/version-image:0.1.0"
        wid = ws.id

    orchestrator._start(wid)

    with Factory() as db:
        ws = db.get(Workspace, wid)
        assert ws.status == WorkspaceStatus.RUNNING.value, ws.error_message
    joined = " ".join(provider.runs[0])
    assert "registry/version-image:0.1.0" in joined
    assert "registry/old-template-image:0.1.0" not in joined
