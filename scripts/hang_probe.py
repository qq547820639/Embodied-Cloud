"""`make hang-probe`：真造一次"外部依赖卡住"，量每个 docker 档付多少秒、说了什么。

为什么需要它（N-49）：N-48 的挂起实验只覆盖到"版本探测阶段就挂"（它先短路），
"版本通、后续探测挂"那一支我只在登记行里写了个估算（20+30+30=80s/档）却没量过——
估算写进文档就成了没人核的主张。这台子把两种挂起都做成可复跑的取证件：

- `blackhole`：`DOCKER_HOST=tcp://192.0.2.1:2375`（RFC 5737 TEST-NET-1，黑洞：丢包不是拒连）。
- `hang-later`：在 PATH 最前面放一个假 `docker`——`version`/`info` 立刻正常返回，
  其余子命令 `sleep` 到天荒地老，于是每一层探测都被**自己的超时**掐掉。

两种都是真子进程、真超时（不是 monkeypatch）；唯一的替身是超时上限：子进程里把
`subprocess.run(timeout=…)` 的 timeout 夹到当场算出的**有效上限**
（`min(EFFECTIVE_CEILING, max(请求值, 4×预热后 p95))`）。所以常驻判据按 `--timeout 2`
跑完，人手动 `make hang-probe`（请求 20s）时三份替身剧本会夹到 10s 封顶——
只有 `blackhole` 不夹（它不起替身、也就没有往返可量），那一档才真是请求的 20 秒。
差值不藏：JSON 与表头都打 `ceiling_bit`。

每个档位还顺带过一遍 N-48 的可行动性判据（`pending_reason_offenders`）：
挂起时给出的原因必须是"能指着补"的句子，否则这台子自己就是红的。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

BLACKHOLE_HOST = "tcp://192.0.2.1:2375"  # TEST-NET-1：路由不到，表现为丢包（不是 connection refused）

# 档位 → (提供 gate_reason() 的模块, 它在 skip 闭集里的哨兵)
TIERS = {
    "docker": ("tests.test_docker_provider_integration", "DOCKER_VALIDATION_PENDING"),
    "postgres": ("tests.pg_server", "POSTGRES_VALIDATION_PENDING"),
    "object-store": ("tests.s3_server", "S3_VALIDATION_PENDING"),
    "k8s-control-plane": ("tests.k8s_server", "K8S_CONTROL_PLANE_PENDING"),
}

CHILD = """
import json, subprocess, sys, time
cap = float({cap!r})
_real = subprocess.run
hit = {{"timeouts": 0, "seconds": 0.0}}
def run(*args, **kwargs):
    requested = kwargs.get("timeout")
    kwargs["timeout"] = cap if requested is None else min(float(requested), cap)
    started = time.monotonic()
    try:
        return _real(*args, **kwargs)
    except subprocess.TimeoutExpired:
        # 数一次"被上限掐掉"的探测：判据要断的是"总耗时 = 次数 × 有效上限"，
        # 而不是一个拍脑袋的秒数天花板（档位探测次数会变，写死的上限必然过时）。
        hit["timeouts"] += 1
        hit["seconds"] += time.monotonic() - started
        raise
subprocess.run = run
sys.path.insert(0, ".")
import importlib
mod = importlib.import_module({module!r})
print(json.dumps({{"reason": mod.gate_reason(), "timeouts": hit["timeouts"], "waited": round(hit["seconds"], 2)}}))
"""

# 三种剧本，代价差别正是想知道的事：
#   hang-later —— 除 version/info 外全挂（极端：整层探测都在等）
#   half-hang  —— `image inspect` 立刻答"没有"、`image ls` 挂住（真实半挂：只有兜底那条路会等）
FAKE_HANG_LATER = """#!/bin/sh
case "$1 $2" in
  "version "*) echo "Client: Docker Engine (hang-probe fake)"; exit 0 ;;
  "info "*) echo "aarch64"; exit 0 ;;
  *) sleep 3600 ;;
esac
"""
FAKE_HALF_HANG = """#!/bin/sh
case "$1 $2" in
  "version "*) echo "Client: Docker Engine (hang-probe fake)"; exit 0 ;;
  "info "*) echo "aarch64"; exit 0 ;;
  "image ls"*) sleep 3600 ;;
  "image inspect"*) echo "no such image" >&2; exit 1 ;;
  *) exit 1 ;;
esac
"""
# 对偶半挂：`image ls` 快答"没有"、`image inspect` 挂住——用来确认没有哪条路径
# 只是因为剧本恰好让它早退而看起来"不怕挂起"。
FAKE_HALF_HANG_B = """#!/bin/sh
case "$1 $2" in
  "version "*) echo "Client: Docker Engine (hang-probe fake)"; exit 0 ;;
  "info "*) echo "aarch64"; exit 0 ;;
  "image ls"*) exit 0 ;;
  "image inspect"*) sleep 3600 ;;
  *) exit 1 ;;
esac
"""
FAKES = {
    "hang-later": FAKE_HANG_LATER,
    "half-hang": FAKE_HALF_HANG,
    "half-hang-b": FAKE_HALF_HANG_B,
}
def hanging_subcommands(script: str) -> list[str]:
    """从剧本正文里读"哪几条子命令会睡死"，不另立一张手抄表。

    手抄表那份写宽过：给 hang-later 列了 image inspect／image ls／pull／build 四条，
    可剧本的真实形状是 `*) sleep 3600`——除 version／info 外**全部**会挂，而 pull、build
    这条取证路径根本不调用。派生之后剧本改了读数跟着改，不需要有人记得改第二处。
    兜底臂报成 `*`（"其余全部"），不假装能枚举出有限清单。
    """
    out: list[str] = []
    for line in script.splitlines():
        quoted = re.match(r'^\s+"([^"]*)"\*\)\s+sleep\b', line)
        if quoted:
            out.append(quoted.group(1))
            continue
        if re.match(r'^\s+\*\)\s+sleep\b', line):
            out.append("*")
    return out


def _fake_docker_dir(tmp: Path, mode: str) -> Path:
    bin_dir = tmp / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "docker"
    script.write_text(FAKES[mode], encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


# 有效上限的两个旋钮：倍率与封顶。封顶是"最坏墙钟"与"会不会把慢误判成挂"之间的取舍，
# 所以它必须**可见**：当封顶压过倍率、余量不足 2×往返时，这台子判定为"测不准"，
# 用退出码 2 明说，而不是交出一份把慢当成挂的读数。
LATENCY_MULTIPLIER = 4.0
EFFECTIVE_CEILING = 10.0
MIN_MARGIN = 2.0


def derive_effective(latency: float, requested: float) -> float:
    """由实测往返算出这次用的超时上限（纯函数，判据与实现共用一份）。"""
    if latency <= 0:
        return requested
    return min(EFFECTIVE_CEILING, max(requested, round(LATENCY_MULTIPLIER * latency, 2)))


def conclusive(latency: float, effective: float) -> bool:
    """余量够不够说"这是挂起不是慢"：effective 至少要是往返时间的 MIN_MARGIN 倍。"""
    if latency <= 0:
        return True
    return effective >= MIN_MARGIN * latency


def latency_profile(env: dict[str, str], fake_dir: Path, loads: tuple[int, ...], samples: int = 5) -> dict:
    """不同并发负载下的往返耗时分布（预热后）。"""
    return {str(load): _latency_stats(fake_dir, env, load, samples) for load in loads}


def _latency_stats(
    fake_dir: Path, env: dict[str, str], load: int, samples: int = 5, *, warm_up: bool = True
) -> dict[str, float]:
    """并发 `load` 下替身答一句话的耗时分布。

    `warm_up=False` 是给判据用的对照档：它把"这一轮的第一个样本"留在分布里。
    预热之所以是承重墙，只有一层是**已证实**的（复算入口是常驻用例
    `test_warm_up_is_the_only_thing_keeping_the_cold_exec_out`，它用夹具把两档极都造了出来）：
    ① 一个**刚写出来**的替身文件，第一次 exec 比之后慢一个数量级（本机 12 次新目录实测
       first p50=0.059s／max=0.274s，之后 p50=0.010s）。它抬高 5 样本的 p95（n=5 时
       nearest-rank p95 就是最大值），抬到越过可判线时 `conclusive()` 翻假、这台子退 2。
    ② 「机器忙时还会叠上偶发慢采样」是 N-55/N-56 写下的归因，**本轮没能复现**：宿主 load≈13
       下 60 次预热后采样最慢 0.0191s、>0.5s 零个。这句按"未证实"留着（没被否证，也没被证实），
       不拿它当判据的前提。
    被丢掉的那一次仍然报出来（`pre_warm_burst_s`），只是不进分布：主张要能被别人重开。
    """
    import statistics

    def burst() -> float:
        procs = [
            subprocess.Popen(  # noqa: S603 受控常量参数（替身脚本本身）
                [str(fake_dir / "docker"), "version"],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            for _ in range(load)
        ]
        started = time.monotonic()
        for proc in procs:
            proc.wait(timeout=60)
        return time.monotonic() - started

    first = burst()
    got = sorted(round(burst(), 3) for _ in range(samples))
    if not warm_up:
        got = sorted([*got, round(first, 3)])
    return {
        "p50": round(statistics.median(got), 3),
        "p95": got[min(len(got) - 1, int(0.95 * len(got)))],
        "max": got[-1],
        "pre_warm_burst_s": round(first, 3),
    }


def _fake_round_trip(fake_dir: Path, env: dict[str, str]) -> dict[str, float]:
    """预热后的往返分布：上限要连它的尾部都盖得住，否则读数不可信。"""
    return _latency_stats(fake_dir, env, 1, 5)


def measure(mode: str, cap_seconds: float) -> dict:
    """逐档在子进程里跑一次 gate_reason()，记录原因与耗时（互不污染环境与进程）。"""
    rows: dict[str, dict] = {}
    injected: dict[str, object] = {"mode": mode, "timeout_cap_seconds": cap_seconds, "hang_subcommands": []}
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = dict(os.environ)
        fake_dir: Path | None = None
        env["HANG_PROBE"] = mode
        env.pop("DOCKER_HOST", None)
        env.pop("EMBODIEDCLOUD_DOCKER_TEST_IMAGE", None)
        if mode == "blackhole":
            env["DOCKER_HOST"] = BLACKHOLE_HOST
        elif mode in FAKES:
            fake_dir = _fake_docker_dir(tmp, mode)
            env["PATH"] = f"{fake_dir}{os.pathsep}{env.get('PATH', '')}"
            # 报"这一档剧本里真正会挂的那几个子命令"，不是替身认识的子命令全集：
            # 半挂剧本的意义正是"只有一层挂"，报成五个全挂就是把读数写宽了一格。
            injected["hang_subcommands"] = hanging_subcommands(FAKES[mode])
        else:
            raise ValueError(f"未知模式：{mode}（可选 blackhole / {' / '.join(FAKES)}）")
        if fake_dir is not None:
            # 替身没拦到就不必再演了：真实守护进程的状态会决定读数，
            # 那会让"谁在等"变成环境问题而不是代码结构问题（本轮在另一棵树上就是这样翻车的）。
            found = shutil.which("docker", path=env.get("PATH", ""))
            if found is None or not Path(found).is_relative_to(fake_dir):
                raise RuntimeError(
                    f"PATH 替身未生效（解析到 {found!r}，期望在 {fake_dir} 下）："
                    "这台子必须在自己的替签下运行，否则读数描述的是环境而不是代码"
                )
        latency = 0.0
        round_trip: dict[str, float] = {}
        if fake_dir is not None:
            stats = _fake_round_trip(fake_dir, env)
            # 上限由预热后的 p95 算出；封顶是"最坏墙钟"与"能不能区分慢与挂"的取舍，
            # 一旦封顶把余量吃光，就明说测不准（退出码 2）而不是交出一份假读数
            latency = stats["p95"]
            round_trip = stats
            effective = derive_effective(latency, cap_seconds)
        else:
            effective = cap_seconds
        injected["fake_round_trip_seconds"] = latency
        injected["round_trip_profile"] = round_trip
        injected["conclusive"] = conclusive(latency, effective)
        injected["effective_timeout_seconds"] = effective
        # 封顶与请求值不是一回事：请求 20s 时三份替身剧本实际都跑在 10s 上，
        # 这个差必须自己说出来，否则"我请求了 20s"会被读成"每档最多等 20s"。
        injected["ceiling_bit"] = effective < cap_seconds
        for tier, (module, sentinel) in TIERS.items():
            code = CHILD.format(cap=effective, module=module)
            started = time.monotonic()
            proc = subprocess.run(  # noqa: S603 受控常量参数（本机解释器 + 模板代码）
                [sys.executable, "-c", code], cwd=ROOT, env=env, text=True, capture_output=True,
                timeout=int(cap_seconds * 12 + 60),
            )
            elapsed = round(time.monotonic() - started, 2)
            reason, timeouts, waited = None, 0, 0.0
            if proc.returncode == 0:
                try:
                    start = proc.stdout.index("{")
                    payload = json.loads(proc.stdout[start: proc.stdout.rindex("}") + 1])
                    reason = payload.get("reason")
                    timeouts = int(payload.get("timeouts", 0))
                    waited = float(payload.get("waited", 0.0))
                except ValueError:
                    reason = None
            rows[tier] = {
                # 上界只约束"被上限掐掉的等待"这一项。父侧那一段（起解释器 + import app）
                # 不是这台子能承诺的量：本轮实测黑子档 waited=2.02s 而 elapsed=12.62s，
                # 差出去的十秒全是宿主负载下的解释器/导入开销——它正是 N-55/N-56
                # 两次"复算红"真正的形状。分开报，不混进上界，也不假装它恒定。
                "bound_s": round(timeouts * effective + 4, 2),
                "startup_overhead_s": round(max(0.0, elapsed - waited), 2),
                "timeouts_observed": timeouts,
                "waited_s": waited,
                "effective_timeout_s": effective,
                "offenders": [],
                "reason": reason or "",
                "stderr": proc.stderr[-200:] if reason is None else "",
                "elapsed_s": elapsed,
                "sentinel": sentinel,
            }
    return {"injected": injected, "tiers": annotate(rows)}


def tier_offenders(rows: dict[str, dict]) -> list[str]:
    """台子自己的主张：每档都要**给得出**原因，且原因可行动（复用 N-48 的判据）。"""
    from validate_release import pending_reason_offenders

    if not rows:
        return ["一个档都没测：这台子无事可做（与恒真同形）"]
    statuses = {
        tier: {
            "status": "PENDING",
            "note": f"整档 ? 用例未执行，原因：{row.get('reason', '')}（哨兵 {row.get('sentinel', '?')}）",
        }
        for tier, row in rows.items()
    }
    return pending_reason_offenders(statuses)


def annotate(rows: dict[str, dict]) -> dict[str, dict]:
    """给每档补上它自己的偏离列表（判据与读数同源，读 JSON 的人不用自己再推一遍）。"""
    from validate_release import pending_reason_offenders

    statuses = {
        tier: {
            "status": "PENDING",
            "note": f"整档 ? 用例未执行，原因：{row.get('reason', '')}（哨兵 {row.get('sentinel', '?')}）",
        }
        for tier, row in rows.items()
    }
    offenders = pending_reason_offenders(statuses)
    per_tier: dict[str, list[str]] = {tier: [] for tier in rows}
    for text in offenders:  # pending_reason_offenders 的每条都以 "档名: " 开头
        tier = text.split(":", 1)[0].strip()
        per_tier.setdefault(tier, []).append(text)
    for tier, row in rows.items():
        if not row.get("reason"):
            per_tier[tier].append(f"挂起时没给出原因（{str(row.get('stderr', ''))[-120:]}）")
        row["offenders"] = per_tier[tier]
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--mode",
        choices=("blackhole", "hang-later", "half-hang", "half-hang-b", "both", "all", "latency"),
        default="both",
    )
    ap.add_argument("--timeout", type=float, default=20.0, help="单次探测的超时上限（秒）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.mode == "latency":
        # 只量负载下的替身往返分布：用来检查"倍率/封顶"这组常数还成不成立
        with tempfile.TemporaryDirectory() as td:
            fake_dir = _fake_docker_dir(Path(td), "hang-later")
            env = dict(os.environ, PATH=f"{fake_dir}{os.pathsep}{os.environ.get('PATH', '')}")
            env.pop("DOCKER_HOST", None)
            env.pop("EMBODIEDCLOUD_DOCKER_TEST_IMAGE", None)
            profile = latency_profile(env, fake_dir, (1, 4, 8))
        print(json.dumps({"latency_profile": profile}, ensure_ascii=False, indent=2))
        unverifiable = []
        for load, row in profile.items():
            eff = derive_effective(row["p95"], args.timeout)
            ok = conclusive(row["p95"], eff)
            # 并发 1 那一行的 pre_warm 是这个替身文件**这辈子第一次**被 exec；
            # 后面几档的 pre_warm 只是"被丢掉的那一次"，别再叫它冷启动。
            tag = "（替身文件的首次 exec）" if load == "1" else ""
            print(
                f"   并发 {load:>2s}: p50={row['p50']}s p95={row['p95']}s"
                f" 被预热的{row['pre_warm_burst_s']}s{tag}"
                f" ⇒ 有效上限 {eff}s（{'可区分慢与挂' if ok else '测不准：封顶把余量吃光了'}）"
            )
            if not ok:
                unverifiable.append(load)
        return 2 if unverifiable else 0

    if args.mode == "both":
        modes = ("blackhole", "hang-later")
    elif args.mode == "all":
        modes = ("blackhole", "hang-later", "half-hang", "half-hang-b")
    else:
        modes = (args.mode,)
    reports = {mode: measure(mode, args.timeout) for mode in modes}
    offenders = {mode: tier_offenders(report["tiers"]) for mode, report in reports.items()}
    if args.json:
        print(json.dumps({"reports": reports, "offenders": offenders}, ensure_ascii=False, indent=2))
    else:
        for mode, report in reports.items():
            head = reports[mode]["injected"]
            print(
                f"== {mode}（请求上限 {args.timeout:g}s；替身往返 "
                f"{head.get('fake_round_trip_seconds', 0):g}s ⇒ 有效上限 "
                f"{head.get('effective_timeout_seconds', args.timeout):g}s"
                f"{'（请求值被 10s 封顶夹过）' if head.get('ceiling_bit') else ''}）"
            )
            for tier, row in report["tiers"].items():
                bound = row.get("bound_s", "?")
                print(
                    f"   {tier:18s} {row['elapsed_s']:6.2f}s（上限 {bound}s）  "
                    f"{row['reason'] or '(没有原因)'}"
                )
            if offenders[mode]:
                print("   判据未过：", *offenders[mode], sep="\n     - ")
    total = [o for lst in offenders.values() for o in lst]
    if total:
        return 1
    if not all(r["injected"].get("conclusive", True) for r in reports.values()):
        print("[capacity] 这台机器上慢与挂无法区分（余量被封顶吃掉）——读数不作结论", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
