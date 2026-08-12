from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Template, TemplateVersion

DEPRECATED_BUILTIN_TEMPLATE_IDS = {"franka-reach-play"}

TEMPLATE_IMAGE = "embodiedcloud/isaaclab-workspace:0.1.0"

# 首批 5 个 Golden Template：全部 version locked、镜像版本化、可验收。
# 禁止引用 mutable latest。
SEED_TEMPLATES = [
    {
        "id": "cartpole",
        "slug": "cartpole",
        "name": "Cartpole · RL 入门训练",
        "version": "0.1.0",
        "description": "最小验证模板：验证 Isaac Lab 3.0 与 Newton/MuJoCo-Warp 后端是否可用，5 轮迭代冒烟。",
        "category": "入门 / RL",
        "runtime": "isaaclab",
        "image": TEMPLATE_IMAGE,
        "gpu_requirement_gb": 8,
        "repo_asset": "IsaacLab/scripts/tutorials",
        "entrypoint": (
            "isaaclab train --rl_library rsl_rl --task Isaac-Cartpole-Direct "
            "--num_envs 16 presets=newton_mjwarp --max_iterations 5"
        ),
        "outputs": ["logs", "checkpoints"],
        "requires_streaming": False,
        "healthcheck": {"command": "isaaclab --help", "interval_s": 60},
        "metadata_json": {"acceptance": "5 iterations complete, no NaN loss"},
        "launch_command": (
            "isaaclab train --rl_library rsl_rl --task Isaac-Cartpole-Direct "
            "--num_envs 16 presets=newton_mjwarp --max_iterations 5"
        ),
        "recommended_vram_gb": 8,
        "estimated_hourly_cost_cny": 3.0,
    },
    {
        "id": "franka-lift",
        "slug": "franka-lift",
        "name": "Franka Lift Cube · 机械臂抓取",
        "version": "0.1.0",
        "description": "第一个付费产品场景：Franka 抓取方块，可直接修改奖励与训练参数。",
        "category": "机械臂 / RL",
        "runtime": "isaaclab",
        "image": TEMPLATE_IMAGE,
        "gpu_requirement_gb": 16,
        "repo_asset": "IsaacLab/scripts",
        "entrypoint": (
            "isaaclab train --rl_library rsl_rl --task Isaac-Lift-Cube-Franka-v0 --num_envs 128"
        ),
        "outputs": ["logs", "checkpoints", "metrics"],
        "requires_streaming": False,
        "healthcheck": {"command": "isaaclab --help", "interval_s": 60},
        "metadata_json": {"acceptance": "success rate > 0 after training"},
        "launch_command": (
            "isaaclab train --rl_library rsl_rl --task Isaac-Lift-Cube-Franka-v0 --num_envs 128"
        ),
        "recommended_vram_gb": 16,
        "estimated_hourly_cost_cny": 6.0,
    },
    {
        "id": "franka-pick-place",
        "slug": "franka-pick-place",
        "name": "Franka Pick & Place · 抓取放置",
        "version": "0.1.0",
        "description": "Franka 抓取-搬运-放置复合任务，面向 Deploy 场景的进阶模板。",
        "category": "机械臂 / RL",
        "runtime": "isaaclab",
        "image": TEMPLATE_IMAGE,
        "gpu_requirement_gb": 24,
        "repo_asset": "IsaacLab/scripts",
        "entrypoint": (
            "isaaclab train --rl_library rsl_rl --task Isaac-Pick-Place-Franka-v0 --num_envs 128"
        ),
        "outputs": ["logs", "checkpoints", "metrics"],
        "requires_streaming": True,
        "healthcheck": {"command": "isaaclab --help", "interval_s": 60},
        "metadata_json": {"acceptance": "pick+place success rate > 0"},
        "launch_command": (
            "isaaclab train --rl_library rsl_rl --task Isaac-Pick-Place-Franka-v0 --num_envs 128"
        ),
        "recommended_vram_gb": 24,
        "estimated_hourly_cost_cny": 10.0,
    },
    {
        "id": "domain-randomization",
        "slug": "domain-randomization",
        "name": "Domain Randomization · 域随机化",
        "version": "0.1.0",
        "description": "展示域随机化训练策略：Sim2Real 转移关键能力。",
        "category": "进阶 / Sim2Real",
        "runtime": "isaaclab",
        "image": TEMPLATE_IMAGE,
        "gpu_requirement_gb": 24,
        "repo_asset": "IsaacLab/scripts",
        "entrypoint": (
            "isaaclab train --rl_library rsl_rl --task Isaac-Velocity-Rough-Franka-v0 "
            "--num_envs 256 --max_iterations 20"
        ),
        "outputs": ["logs", "checkpoints"],
        "requires_streaming": True,
        "healthcheck": {"command": "isaaclab --help", "interval_s": 60},
        "metadata_json": {"acceptance": "randomized env trains without crash"},
        "launch_command": (
            "isaaclab train --rl_library rsl_rl --task Isaac-Velocity-Rough-Franka-v0 "
            "--num_envs 256 --max_iterations 20"
        ),
        "recommended_vram_gb": 24,
        "estimated_hourly_cost_cny": 12.0,
    },
    {
        "id": "rgbd-perception",
        "slug": "rgbd-perception",
        "name": "RGB-D Perception · 感知",
        "version": "0.1.0",
        "description": "RGB-D 感知实验：验证仿真相机输出、点云与可视化的完整链路。",
        "category": "感知 / Vision",
        "runtime": "isaaclab",
        "image": TEMPLATE_IMAGE,
        "gpu_requirement_gb": 16,
        "repo_asset": "IsaacLab/scripts",
        "entrypoint": (
            "uv run python scripts/tutorials/04_sensors/launch_app.py --num_envs 2"
        ),
        "outputs": ["rgb", "depth", "pointcloud"],
        "requires_streaming": True,
        "healthcheck": {"command": "isaaclab --help", "interval_s": 60},
        "metadata_json": {"acceptance": "camera frame renders without crash"},
        "launch_command": "uv run python scripts/tutorials/04_sensors/launch_app.py --num_envs 2",
        "recommended_vram_gb": 16,
        "estimated_hourly_cost_cny": 8.0,
    },
]


def seed_templates(db: Session) -> None:
    """Idempotently seed built-in templates + 每个模板的不可变 TemplateVersion。

    版本不可变约束（§15）：
    - UNIQUE(template_id, version) 由数据库兜底
    - 已发布（released）TemplateVersion 禁止覆盖：seed 只创建缺失版本，
      绝不修改已存在的 released 版本（修改模板 = 创建新版本）
    - 模板 identity 字段（名称/价格等展示字段）可刷新，但版本内容不可变
    """
    for template_id in DEPRECATED_BUILTIN_TEMPLATE_IDS:
        old_template = db.scalar(select(Template).where(Template.id == template_id))
        if old_template is not None:
            old_template.enabled = False

    for spec in SEED_TEMPLATES:
        existing = db.scalar(select(Template).where(Template.id == spec["id"]))
        if existing is None:
            existing = Template(**spec)
            db.add(existing)
            db.flush()
        else:
            # identity/展示字段可刷新；版本相关内容不可变
            for key in ("name", "description", "category", "estimated_hourly_cost_cny", "enabled"):
                if key in spec:
                    setattr(existing, key, spec[key])
            existing.enabled = True
        # 已发布版本不可覆盖：仅创建缺失版本
        version = str(spec.get("version", "0.1.0"))
        existing_version = db.scalar(
            select(TemplateVersion).where(
                TemplateVersion.template_id == spec["id"], TemplateVersion.version == version
            )
        )
        if existing_version is None:
            db.add(
                TemplateVersion(
                    id=f"tv-{spec['id']}-{version.replace('.', '-')}",
                    template_id=spec["id"],
                    version=version,
                    image=spec.get("image"),
                    entrypoint=spec.get("entrypoint", ""),
                    gpu_requirement_gb=spec.get("gpu_requirement_gb", 16),
                    requires_streaming=spec.get("requires_streaming", False),
                    outputs=spec.get("outputs", []),
                    healthcheck=spec.get("healthcheck"),
                    metadata_json=spec.get("metadata_json", {}),
                    source_revision=None,
                    released=True,
                )
            )
    db.commit()
