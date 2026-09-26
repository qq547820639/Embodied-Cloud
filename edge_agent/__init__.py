"""EmbodiedCloud 边缘代理（edge agent，§25）。

跑在机器人那侧的独立包：**只依赖标准库**，不 import `app`——控制面代码不会
装到设备上，设备侧也不需要数据库、ORM 或 provider。它与控制面的全部契约就是
`X-Agent-Token` 鉴权的几个 HTTP 端点（ADR 0007 的 C 方案）。

一轮工作（`EdgeAgentRuntime.run_once`）：
    心跳 → 发现绑定给自己的部署 → begin（设备侧承认开始取件）
    → 流式取件 + 边写边算 sha256 → 与期望摘要核对 → 上报 checksum
    → 交给驱动加载并跑一次 → 遥测回报观测值
"""

from .agent import EdgeAgentRuntime, RoundOutcome
from .client import AgentClient, AgentClientError, ArtifactFetch, ArtifactIntegrityError
from .drivers import MockRobotDriver, RobotDriver, build_driver

__all__ = [
    "AgentClient",
    "AgentClientError",
    "ArtifactFetch",
    "ArtifactIntegrityError",
    "EdgeAgentRuntime",
    "MockRobotDriver",
    "RobotDriver",
    "RoundOutcome",
    "build_driver",
]
