"""公共工具函数（零依赖：不 import 任何 app 内模块）。

models.py 被全应用导入，此处若反向依赖任何服务/模型模块会引入循环导入。
故本模块只允许依赖标准库。
"""

from datetime import UTC, datetime


def utcnow() -> datetime:
    """返回带 UTC 时区的当前时间（SQLAlchemy default 与业务时间戳统一口径）。"""
    return datetime.now(UTC)
