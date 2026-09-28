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

from .agent import EdgeAgentRuntime
from .client import AgentClientError
from .drivers import build_driver

ENV_SERVER = "EMBODIEDCLOUD_EDGE_SERVER"
ENV_TOKEN = "EMBODIEDCLOUD_EDGE_TOKEN"  # noqa: S105 环境变量名，不是凭据
ENV_AGENT_ID = "EMBODIEDCLOUD_EDGE_AGENT_ID"
ENV_WORKDIR = "EMBODIEDCLOUD_EDGE_WORKDIR"


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
    # 非零退出的两种形状：一处坏记录（error），或一次**机器人真动过而控制面不知道**
    # 的运行（reported=False）——后者若退 0，冒烟跑的绿灯会盖住一条无人知晓的物理动作。
    return 1 if any(o.action == "error" or not o.reported for o in outcomes) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
