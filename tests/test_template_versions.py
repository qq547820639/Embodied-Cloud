"""Immutable TemplateVersion（§15）：identity 与版本分离。

约束：
- UNIQUE(template_id, version)
- 已发布版本禁止 mutable update（seed 不允许覆盖已存在 released version）
- Workspace 引用具体 TemplateVersion；启动使用版本镜像（Template A → image A）
- 版本声明了 image_digest 时，快照与启动引用都必须带上它（钉不住就等于没钉）
"""

import ast
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Template, TemplateVersion, Workspace, WorkspaceStatus
from app.seed import SEED_TEMPLATES, seed_templates
from app.services.image_ref import ImageDigestError, pinned_ref
from app.services.orchestrator import WorkspaceOrchestrator
from app.services.providers.docker import DockerProvider
from app.services.providers.mock import MockProvider
from app.services.scheduler import GpuInfo, GpuScheduler
from app.utils import utcnow
from tests.dbfiles import db_url

ENGINE = create_engine(db_url("template-version"), connect_args={"check_same_thread": False})
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
    spec_image = SEED_TEMPLATES[0]["image"]
    try:
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
    finally:
        # 还原 spec（测试隔离；异常时也必须恢复，避免污染其它用例）
        SEED_TEMPLATES[0]["image"] = spec_image


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

    def wait_ready(self, workspace, template, timeout_seconds: int = 120) -> bool:
        return True

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
    from datetime import timedelta

    published_at = utcnow()
    v1 = TemplateVersion(
        id=f"tv-{template_id}-v1",
        template_id=template_id,
        version="0.1.0",
        image=image_a,
        entrypoint="",
        released=True,
        created_at=published_at,
    )
    v2 = TemplateVersion(
        id=f"tv-{template_id}-v2",
        template_id=template_id,
        version="0.2.0",
        image=image_b,
        entrypoint="",
        released=True,
        created_at=published_at + timedelta(minutes=5),
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


def test_seed_sets_current_version_id():
    """§11：seed 设置确定性 current_version_id（发布策略，非 created_at 猜测）。"""
    with Factory() as db:
        _seed(db)
        for spec in SEED_TEMPLATES:
            template = db.get(Template, spec["id"])
            assert template.current_version_id is not None
            version = db.get(TemplateVersion, template.current_version_id)
            assert version is not None
            assert version.template_id == spec["id"]
            assert version.released is True


def test_workspace_binds_current_version_not_created_at():
    """§11：Workspace 绑定 current_version_id 指向的版本，即使它不是最新创建行。"""
    with Factory() as db:
        _seed(db)
        template = db.get(Template, "cartpole")
        # 手动创建一个更晚的 released 版本，但 current_version_id 仍指向发布版本
        later = TemplateVersion(
            id="tv-cartpole-later",
            template_id="cartpole",
            version="9.9.9",
            image="registry/cartpole:9.9.9",
            entrypoint="",
            released=True,
        )
        db.add(later)
        db.commit()

        orchestrator = WorkspaceOrchestrator(Factory, MockProvider("http://127.0.0.1:8000"), Path("/tmp/tv-test3"))  # noqa: S108
        ws = orchestrator.create(db, template, user_id="u1")
        # 绑定发布指针版本（0.1.0），而不是 created_at 最新的 9.9.9
        assert ws.template_version_id == template.current_version_id
        version = db.get(TemplateVersion, ws.template_version_id)
        assert version.version == "0.1.0"
        assert ws.image == "embodiedcloud/isaaclab-workspace:0.1.0"


def test_deployment_uses_workspace_template_version():
    """§11：Artifact/Deployment 的 model_version 来自 Workspace.template_version_id。"""
    from app.models import User
    from app.services.deployment import DeploymentService

    with Factory() as db:
        _seed(db)
        user = User(id="u1", email="u1@example.com", username="u1", password_hash="x")  # noqa: S106
        db.add(user)
        db.commit()
        template = db.get(Template, "cartpole")
        # 旧版本指针：指向 0.1.0，但 Template.version 被改（模拟模板升级）
        template.version = "5.0.0"
        db.commit()
        orchestrator = WorkspaceOrchestrator(Factory, MockProvider("http://127.0.0.1:8000"), Path("/tmp/tv-test4"))  # noqa: S108
        ws = orchestrator.create(db, template, user_id="u1")
        assert db.get(TemplateVersion, ws.template_version_id).version == "0.1.0"

        root = Path("/tmp/tv-test4-root")  # noqa: S108
        (root / ws.id).mkdir(parents=True, exist_ok=True)
        (root / ws.id / "out.pt").write_bytes(b"v1")
        svc = DeploymentService(Factory, root)
        artifact = svc.create_artifact(db, user, ws, "out.pt")
        # 版本来自 Workspace 引用的 TemplateVersion（0.1.0），不是 Template.version(5.0.0)
        assert artifact.model_version == "0.1.0"


def test_version_fallback_is_total_ordered_on_identical_created_at():
    """同秒发布的两个 released 版本：兜底选择必须是全序规则的结果，且可重复。

    只按 created_at 排时这一档会随扫描顺序漂移（实测过：整库并发跑时同一份数据
    两次解析出不同镜像），即"新工作区用哪个镜像"不确定。
    """
    with Factory() as db:
        _seed(db)
        same = utcnow()
        template = Template(
            id="template-tie",
            slug="template-tie",
            name="tie",
            version="0.1.0",
            description="test",
            category="test",
            runtime="isaaclab",
            image="registry/tie:0.1.0",
            launch_command="echo ok",
            enabled=True,
            recommended_vram_gb=16,
            estimated_hourly_cost_cny=1.0,
        )
        db.add(template)
        db.flush()
        for suffix, image in (("aaa", "registry/tie:0.1.0"), ("zzz", "registry/tie:0.2.0")):
            db.add(
                TemplateVersion(
                    id=f"tv-tie-{suffix}",
                    template_id="template-tie",
                    version=image.split(":")[-1],
                    image=image,
                    entrypoint="",
                    released=True,
                    created_at=same,
                )
            )
        db.commit()

        orchestrator = WorkspaceOrchestrator(Factory, MockProvider("http://127.0.0.1:8000"), Path("/tmp/tv-tie"))  # noqa: S108
        first = orchestrator.create(db, db.get(Template, "template-tie"), user_id="u1")
        second = orchestrator.create(db, db.get(Template, "template-tie"), user_id="u1")

        # 规则本身（created_at desc, id desc）可预测：id 更大的 zzz 胜出
        assert first.image == "registry/tie:0.2.0", f"未按全序规则选版：{first.image}"
        assert first.image == second.image, f"同一份数据两次解析出不同镜像：{first.image} vs {second.image}"
        assert first.template_version_id == "tv-tie-zzz"


# ---------------------------------------------------------------------------
# image_digest 真的有读者（§15 记录、SUPPLY_CHAIN §8 第 1 条）
# ---------------------------------------------------------------------------

DIG_A = "sha256:" + "ab" * 32
DIG_B = "sha256:" + "cd" * 32
APP_DIR = Path(__file__).resolve().parents[1] / "app"


def test_pinned_ref_arms():
    """合规／不合规两侧都要覆盖：钉不上时必须拒绝，而不是退回可变 tag。"""
    assert pinned_ref(None, DIG_A) is None, "image 为空不该越权造值（provider 还有下一档 fallback）"
    assert pinned_ref("reg/img:1.0", None) == "reg/img:1.0"
    assert pinned_ref("reg/img:1.0", "") == "reg/img:1.0"
    assert pinned_ref("reg/img:1.0", DIG_A) == f"reg/img:1.0@{DIG_A}"
    # 已钉过的引用再钉一次必须幂等（重复发布/回填不该改语义）
    assert pinned_ref(f"reg/img:1.0@{DIG_A}", DIG_A) == f"reg/img:1.0@{DIG_A}"
    with pytest.raises(ImageDigestError, match="不一致"):
        pinned_ref(f"reg/img:1.0@{DIG_A}", DIG_B)
    with pytest.raises(ImageDigestError, match="形制不对"):
        pinned_ref("reg/img:1.0", "sha256:deadbeef")
    with pytest.raises(ImageDigestError, match="形制不对"):
        pinned_ref("reg/img:1.0", "md5:" + "0" * 32)
    with pytest.raises(ImageDigestError, match="列宽"):
        pinned_ref("r/" + "x" * 250, DIG_A)


def test_version_with_digest_launches_the_pinned_ref(monkeypatch, tmp_path):
    """整条链：版本声明 digest → workspace 快照带 digest → docker argv 里就是那个钉死的 token。

    只测快照那一环不够：provider 读的是 workspace.image，链上任何一环把 digest 丢掉，
    启动就悄悄回到可变 tag，而列里仍然"看起来有 digest"。
    """
    from app.config import Settings

    settings = Settings(
        eula_accepted=True, workspace_root=tmp_path, ide_port_start=38200, ide_port_end=38220
    )
    monkeypatch.setattr("app.services.providers.docker.is_port_free", lambda *a, **kw: True)
    monkeypatch.setattr("app.services.providers.docker.allocate_tcp_port", lambda *a, **kw: 38201)
    provider = CapturingDockerProvider(settings)

    pinned = f"registry/pinned-image:1.2.3@{DIG_A}"
    with Factory() as db:
        _seed(db)
        db.add(
            Template(
                id="t2",
                slug="t2",
                name="t2",
                description="d",
                category="c",
                runtime="isaaclab",
                image="registry/stale:0.0.1",
                launch_command="echo ok",
                enabled=True,
                recommended_vram_gb=16,
                estimated_hourly_cost_cny=1.0,
            )
        )
        db.flush()
        db.add(
            TemplateVersion(
                id="tv-t2-001",
                template_id="t2",
                version="1.2.3",
                image="registry/pinned-image:1.2.3",
                image_digest=DIG_A,
                entrypoint="",
                released=True,
            )
        )
        db.commit()
        orchestrator = WorkspaceOrchestrator(Factory, provider, tmp_path)
        ws = orchestrator.create(db, db.get(Template, "t2"), user_id="u1")
        assert ws.image == pinned, f"版本声明了 digest，快照却没钉住：{ws.image}"
        wid = ws.id

    orchestrator._start(wid)

    with Factory() as db:
        assert db.get(Workspace, wid).status == WorkspaceStatus.RUNNING.value
    argv = provider.runs[0]
    assert pinned in argv, f"docker argv 里没有钉死的引用：{argv}"
    assert "registry/pinned-image:1.2.3" not in argv, "argv 里还留着一个可变 tag 形态的同一镜像"


def _attr_reads(app_dir: Path, attr: str) -> int:
    """`app/`（除 models.py）里对某列名的属性读取次数——模型里的声明不算读者。"""
    hits = 0
    for path in sorted(app_dir.rglob("*.py")):
        if "__pycache__" in path.parts or path.name == "models.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits += sum(
            1 for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == attr
        )
    return hits


def test_image_digest_column_has_a_production_reader():
    """这一列自 v0.4 就在，本轮普查读数是**0 处读者**（列存在≠镜像被钉住）。

    现在它有了唯一消费点（orchestrator 快照）。这条判据盯着它别退回去：
    删掉 `pinned_ref(...)` 那处调用，本条即红。
    """
    assert _attr_reads(APP_DIR, "current_version_id") > 0, "探针本身读不到已知有读者的列，本条判据恒真"
    assert _attr_reads(APP_DIR, "image_digest") >= 1, (
        "TemplateVersion.image_digest 又变回装饰性字段：没有任何生产代码读它"
    )


def test_record_digest_then_create_pins_it(monkeypatch, tmp_path):
    """回填 → 新建 workspace 的快照 → docker argv，三步在同一份真库上连起来跑。

    CLI 单测用的是 session 替身（那只能证明分支逻辑）；这一支证明写入真的落地、
    并且下一个 workspace 真的因此被钉住——也就是 SUPPLY_CHAIN §8 第 1 条要的闭环。
    """
    import app.cli as cli
    from app.config import Settings

    settings = Settings(
        eula_accepted=True, workspace_root=tmp_path, ide_port_start=38300, ide_port_end=38320
    )
    monkeypatch.setattr("app.services.providers.docker.is_port_free", lambda *a, **kw: True)
    monkeypatch.setattr("app.services.providers.docker.allocate_tcp_port", lambda *a, **kw: 38301)
    provider = CapturingDockerProvider(settings)
    monkeypatch.setattr(cli, "SessionFactory", Factory)

    with Factory() as db:
        _seed(db)
        version = db.scalar(
            select(TemplateVersion).where(TemplateVersion.template_id == "cartpole")
        )
        assert version.image_digest is None, "seed 不该自带 digest（本机没有可核实的 registry digest）"
        base_image = version.image
        # 回填**之前**建好的工作区：它的快照就该停在可变 tag 上（快照语义），
        # 回填不改历史行——这条不是猜测，下面有断言。
        early = WorkspaceOrchestrator(Factory, provider, tmp_path).create(
            db, db.get(Template, "cartpole"), user_id="u1"
        )
        early_id = early.id
        assert early.image == base_image

    assert cli.record_image_digest("cartpole", version.version, DIG_A) == 0
    with Factory() as db:
        assert db.get(TemplateVersion, version.id).image_digest == DIG_A
        assert db.get(Workspace, early_id).image == base_image, "回填把历史工作区的快照也改了"

    with Factory() as db:
        orchestrator = WorkspaceOrchestrator(Factory, provider, tmp_path)
        ws = orchestrator.create(db, db.get(Template, "cartpole"), user_id="u1")
        assert ws.image == f"{base_image}@{DIG_A}", f"回填之后新建的工作区没被钉住：{ws.image}"
        wid = ws.id

    orchestrator._start(wid)
    assert DIG_A in " ".join(provider.runs[0]), f"docker argv 里没有 digest：{provider.runs[0]}"
