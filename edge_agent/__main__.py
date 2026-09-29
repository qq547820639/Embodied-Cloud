"""`python -m edge_agent` / `embodiedcloud-edge-agent` 命令行。

    # 1) 设备入网（在有人值守的那台机器上跑一次，用用户的 Bearer token）
    embodiedcloud-edge-agent register --server http://127.0.0.1:8000 \\
        --owner-token "$USER_TOKEN" --name arm-01
    # → {"agent_id": "...", "token": "..."}  token 只出现这一次，存进设备密钥库

    # 2) 设备常驻
    embodiedcloud-edge-agent run --server http://127.0.0.1:8000 \\
        --agent-id "$AGENT_ID" --token "$AGENT_TOKEN" --workdir /var/lib/edge-agent
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .agent import RUN_HEARTBEAT_SECONDS, EdgeAgentRuntime, RoundOutcome
from .client import AgentClientError
from .drivers import build_driver

ENV_SERVER = "EMBODIEDCLOUD_EDGE_SERVER"
ENV_TOKEN = "EMBODIEDCLOUD_EDGE_TOKEN"  # noqa: S105 环境变量名，不是凭据
ENV_AGENT_ID = "EMBODIEDCLOUD_EDGE_AGENT_ID"
ENV_WORKDIR = "EMBODIEDCLOUD_EDGE_WORKDIR"


def _exit_code(outcomes: list[RoundOutcome]) -> int:
    """非零退出的三种形状，一种比一种难看见。

    1. `action == "error"`：一条坏记录（原有）。
    2. `reported is False`：机器人真动过而控制面不知道（N-109）。
    3. `run_heartbeat_misses > 0`（N-137）：这次运行期间**存活证据没送达**。
       它不等于运行失败——驱动可能跑完、结果也可能报成功了；它说的是这段时间里
       控制面收不到心跳，于是那条 `running` 有可能已被判活 sweep 收成 `failed`，
       而设备自己不知道。冒烟跑的绿灯不该盖住这种"两边的账可能对不上"。
    """
    for outcome in outcomes:
        if (
            outcome.action == "error"
            or not outcome.reported
            or not outcome.start_reported
            or outcome.run_heartbeat_misses
        ):  # 开跑声明没送达＝这一格在控制面上是"看不见在跑"的（N-139）
            return 1
    return 0


def _parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="embodiedcloud-edge-agent", description=__doc__)
    root.add_argument("--server", default=os.environ.get(ENV_SERVER, ""), help="控制面 base URL")
    sub = root.add_subparsers(dest="command", required=True)

    reg = sub.add_parser("register", help="用用户 Bearer token 注册设备（token 只返回一次）")
    reg.add_argument("--owner-token", required=True, help="控制面用户 access token")
    reg.add_argument("--name", required=True, help="设备名")

    run = sub.add_parser("run", help="常驻：心跳 + 发现 + 取件 + 校验 + 驱动")
    run.add_argument("--agent-id", default=os.environ.get(ENV_AGENT_ID, ""))
    run.add_argument("--token", default=os.environ.get(ENV_TOKEN, ""))
    run.add_argument(
        "--workdir",
        default=os.environ.get(ENV_WORKDIR, str(Path.home() / ".embodiedcloud" / "edge-agent")),
        help="模型落盘目录",
    )
    run.add_argument("--driver", default="mock", help="设备驱动名（当前：mock）")
    run.add_argument("--interval", type=float, default=5.0, help="轮询间隔秒")
    run.add_argument(
        "--run-heartbeat",
        dest="run_heartbeat",
        type=float,
        default=RUN_HEARTBEAT_SECONDS,
        help="一次物理运行期间的心跳间隔秒；必须显著小于控制面的判活阈值"
        "（`edge_agent_offline_after_seconds`，默认 90 s），否则这段阻塞里设备会被判成离线",
    )
    run.add_argument("--iterations", type=int, default=None, help="跑满 N 轮后退出（默认常驻）")
    run.add_argument("--json", action="store_true", help="把每轮处置结果按 JSON 打印")
    return root


def _register(server: str, owner_token: str, name: str) -> dict:
    req = urllib.request.Request(  # noqa: S310 base_url 由运维显式给出
        f"{server.rstrip('/')}/api/edge/agents/register",
        data=json.dumps({"name": name, "device_info": {}}).encode("utf-8"),
        headers={"Authorization": f"Bearer {owner_token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise AgentClientError(exc.code, req.full_url, exc.read().decode("utf-8", "replace")[:200]) from exc


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.server:
        print(f"缺 --server（或 {ENV_SERVER}）", file=sys.stderr)
        return 2

    if args.command == "register":
        body = _register(args.server, args.owner_token, args.name)
        agent = body["agent"]
        print(json.dumps({"agent_id": agent["id"], "token": body["token"]}, ensure_ascii=False))
        print(
            "注意：token 只在此处出现一次，服务端仅存哈希（app/security.hash_token）。",
            file=sys.stderr,
        )
        return 0

    if not args.agent_id or not args.token:
        print(f"缺 --agent-id/--token（或 {ENV_AGENT_ID}/{ENV_TOKEN}）", file=sys.stderr)
        return 2

    runtime = EdgeAgentRuntime(
        server=args.server,
        token=args.token,
        agent_id=args.agent_id,
        workdir=Path(args.workdir),
        driver=build_driver(args.driver),
        run_heartbeat_seconds=args.run_heartbeat,
    )
    try:
        outcomes = runtime.loop(interval_seconds=args.interval, iterations=args.iterations)
    except AgentClientError as exc:
        # 心跳/发现阶段的失败（401、服务不可达）：一句话 + 非零退出。
        # 不能让它变成 traceback 走 stdout 再被 `--json` 消费方当成数据。
        print(f"edge agent 无法与控制面通信：{exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([o.as_json() for o in outcomes], ensure_ascii=False, indent=2))
    else:
        for o in outcomes:
            print(f"{o.deployment_id[:8]} {o.status_before}→{o.status_after} {o.action} {o.detail}")
    # 非零退出的三种形状见 `_exit_code`：坏记录、一次没人知晓的物理运行、
    # 一段没能送达的存活证据。后两种都不会让"这一格跑完了"看起来有问题。
    return _exit_code(outcomes)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
