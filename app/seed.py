from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Template

DEPRECATED_BUILTIN_TEMPLATE_IDS = {"franka-reach-play"}


SEED_TEMPLATES = [
    {
        "id": "newton-cartpole-smoke",
        "name": "Newton Cartpole · 5轮冒烟训练",
        "description": "最小验证模板：验证 Isaac Lab 3.0 与 Newton/MuJoCo-Warp 后端是否可用。",
        "category": "入门 / RL",
        "runtime": "isaaclab",
        "launch_command": (
            "isaaclab train --rl_library rsl_rl --task Isaac-Cartpole-Direct "
            "--num_envs 16 presets=newton_mjwarp --max_iterations 5"
        ),
        "requires_streaming": False,
        "recommended_vram_gb": 8,
        "estimated_hourly_cost_cny": 3.0,
    },
    {
        "id": "franka-lift-cube",
        "name": "Franka Lift Cube · 机械臂抓取",
        "description": "第一个付费产品场景：Franka 抓取方块，可直接修改奖励与训练参数。",
        "category": "机械臂 / RL",
        "runtime": "isaaclab",
        "launch_command": "isaaclab train --rl_library rsl_rl --task Isaac-Lift-Cube-Franka-v0 --num_envs 128",
        "requires_streaming": False,
        "recommended_vram_gb": 16,
        "estimated_hourly_cost_cny": 6.0,
    },
    {
        "id": "webrtc-streaming-smoke",
        "name": "Isaac Sim · WebRTC 可视化冒烟",
        "description": "启动官方 Isaac Lab 最小仿真教程，并验证 Isaac Sim 6 WebRTC 远程可视化链路。",
        "category": "可视化 / Streaming",
        "runtime": "isaaclab",
        "launch_command": "uv run python scripts/tutorials/00_sim/launch_app.py --size 0.5",
        "requires_streaming": True,
        "recommended_vram_gb": 16,
        "estimated_hourly_cost_cny": 8.0,
    },
]


def seed_templates(db: Session) -> None:
    """Idempotently seed and update built-in templates.

    Built-in template commands are product code, not immutable user data, so an
    upgrade deliberately refreshes their fields while preserving the same IDs.
    """
    for template_id in DEPRECATED_BUILTIN_TEMPLATE_IDS:
        old_template = db.scalar(select(Template).where(Template.id == template_id))
        if old_template is not None:
            old_template.enabled = False

    for spec in SEED_TEMPLATES:
        existing = db.scalar(select(Template).where(Template.id == spec["id"]))
        if existing is None:
            db.add(Template(**spec))
        else:
            for key, value in spec.items():
                setattr(existing, key, value)
            existing.enabled = True
    db.commit()
