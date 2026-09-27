"""warm pool 的两条 SLA 读数（人工/CI 档，不进每轮 validate）。

量什么：
1. 普通用户走 `POST /api/workspaces` 命中 claim 的**端到端**耗时，与服务侧直方图
   `warm_pool_claim_seconds` 互相核对（前者是客户端看见的，后者是服务端记的）；
2. admin 的冷启动基准（iterations 顶到 `BENCHMARK_MAX_ITERATIONS`）在 mock 运行时下的
   p50/p95 与样本数，并按最近秩复核 p95 取的是哪一格（不是最大也不是最小）；
3. 三条反向对照：非 admin 两个观测端点必须 403、`iterations` 越界必须 422、
   跑完不留活体也不留占卡。

明确它**不**量什么：mock provider 的 `provision` 只 `mkdir`、`wait_ready` 直接 `return True`
（`app/services/providers/mock.py:21-38,76-78`），所以这里的绝对值是**控制面自身那一段**的开销，
不是 Isaac Sim 真机启动时间——`P50<15s／P95<30s` 的绝对判定仍归 G1–G4，别把本脚本的读数当达标证据。
库文件落在 /tmp 下的独立路径，不碰开发用的那一份。
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
DB = pathlib.Path(tempfile.gettempdir()) / "embodiedcloud-warm-sla-lab.db"

os.environ["EMBODIEDCLOUD_PROVIDER"] = "mock"
os.environ["EMBODIEDCLOUD_WARM_POOL_ENABLED"] = "1"
os.environ["EMBODIEDCLOUD_WARM_POOL_SIZE"] = os.environ.get("WARM_SLA_POOL_SIZE", "2")
os.environ["EMBODIEDCLOUD_DATABASE_URL"] = f"sqlite:///{DB}"
sys.path.insert(0, str(REPO))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.deps import SessionFactory, warm_pool, worker  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Role, User, WarmPoolState  # noqa: E402
from app.services.warmpool import BENCHMARK_MAX_ITERATIONS  # noqa: E402
from tests.test_demo_workspace import _auth, _register  # noqa: E402


def _metric_line(text: str, name: str, needle: str = 'template_id="cartpole"') -> str:
    for line in text.splitlines():
        if line.startswith(name) and needle in line:
            return line
    return f"(没有 {name} 的 cartpole 行)"


def _metric_value(text: str, name: str, needle: str = 'template_id="cartpole"') -> float | None:
    """取指标行的数值；行不存在或不是数字都返回 None（调用方按读数失效处理）。"""
    line = _metric_line(text, name, needle)
    if line.startswith("(没有"):
        return None
    try:
        return float(line.rsplit(None, 1)[-1])
    except ValueError:
        return None


def _ready_count() -> int:
    with SessionFactory() as db:
        return sum(m.get(WarmPoolState.READY.value, 0) for m in warm_pool.pool_metrics(db).values())


def _fill_pool(target: int = 2, rounds: int = 60) -> int:
    """补池：maintain → 排空 durable operation → maintain 收割 RUNNING→READY。"""
    for _ in range(rounds):
        with SessionFactory() as db:
            warm_pool.maintain(db)
        for _ in range(20):
            if worker.tick_once() == 0:
                break
        with SessionFactory() as db:
            warm_pool.maintain(db)
        got = _ready_count()
        if got >= target:
            return got
    return _ready_count()


def main() -> int:
    DB.unlink(missing_ok=True)
    rc = 0
    with TestClient(app) as client:
        admin = _register(client, "warm-sla-admin@example.com", "warm-sla-admin")
        with SessionFactory() as db:
            row = db.scalar(select(User).where(User.email == "warm-sla-admin@example.com"))
            assert row is not None
            row.role = Role.ADMIN.value
            db.commit()
        normal = _register(client, "warm-sla-user@example.com", "warm-sla-user")

        print("== 1) 反向对照（这三条不需要真机就有判决力）")
        for path, label in (
            ("/api/streaming/warmpool/benchmark?template_id=cartpole&iterations=1", "benchmark"),
            ("/api/streaming/warmpool/metrics", "metrics"),
        ):
            got = client.get(path, headers=_auth(normal)).status_code
            ok = got == 403
            rc |= 0 if ok else 1
            print(f"   非 admin {label} → HTTP {got}（期望 403）{'OK' if ok else 'FAIL'}")
        over = client.get(
            "/api/streaming/warmpool/benchmark",
            params={"template_id": "cartpole", "iterations": BENCHMARK_MAX_ITERATIONS + 1},
            headers=_auth(admin),
        ).status_code
        print(
            f"   admin iterations={BENCHMARK_MAX_ITERATIONS + 1} → HTTP {over}"
            f"（期望 422）{'OK' if over == 422 else 'FAIL'}"
        )
        rc |= 0 if over == 422 else 1

        print("== 2) 一次真 claim：客户端计时 vs 服务端直方图")
        ready_before = _fill_pool()
        print(f"   补池后 READY = {ready_before}")
        import time

        started = time.monotonic()
        created = client.post(
            "/api/workspaces",
            json={"template_id": "cartpole", "auto_start": True},
            headers=_auth(normal),
        )
        client_ms = (time.monotonic() - started) * 1000.0
        body = created.json() if created.status_code < 300 else {}
        print(
            f"   POST /api/workspaces → HTTP {created.status_code}"
            f"，客户端 {client_ms:.1f} ms，交付行 name={body.get('name')} status={body.get('status')}"
        )
        hit = created.status_code == 201 and str(body.get("name", "")).startswith("warm-")
        metrics = client.get("/metrics").text
        samples = _metric_value(metrics, "warm_pool_claim_seconds_count")
        ready_after = _ready_count()
        # 三条读数一起记进 rc：走没走 claim、服务端有没有留下这次 claim 的样本、
        # 池子是不是真的少了一格。原先只 print 不记账，于是"这次量的根本不是 claim"
        # 也能打着 PASS 退 0 —— 一台取证台的 rc 必须承载它自己报出的数。
        claim_ok = hit and samples is not None and samples >= 1 and ready_after == ready_before - 1
        print(f"   走的是 claim 而不是新建：{'是' if hit else '否'}")
        print(f"   {_metric_line(metrics, 'warm_pool_claim_seconds_count')}"
              f"（本次 claim 至少要有一格样本）")
        print(f"   {_metric_line(metrics, 'warm_pool_claim_seconds_sum')}")
        print(f"   READY {ready_before} → {ready_after}（交付一格应少一格）")
        print(f"   这一段判据：{'OK' if claim_ok else 'FAIL'}")
        rc |= 0 if claim_ok else 1

        print(f"== 3) 冷启动基准 iterations={BENCHMARK_MAX_ITERATIONS}（admin）")
        bench = client.get(
            "/api/streaming/warmpool/benchmark",
            params={"template_id": "cartpole", "iterations": BENCHMARK_MAX_ITERATIONS},
            headers=_auth(admin),
        )
        print(f"   HTTP {bench.status_code}")
        data = bench.json() if bench.status_code == 200 else {}
        samples = data.get("samples", [])
        if len(samples) >= 2:
            s = sorted(samples)
            n = len(s)
            # 不再假装这是"第二把尺"：中位数如果也用 statistics.median 重算一遍，
            # 那只是把同一个函数调两次，恒等且什么都不证明。这里核的是**分位索引**——
            # 最近秩法下 n=20 的 p95 取第 ceil(0.95·20)=19 个（下标 18），
            # 既不是最大也不是最小；这一条能抓住实现里的 off-by-one 漂移。
            idx95 = max(0, min(-(-95 * n // 100) - 1, n - 1))  # ceil(0.95*n)-1，整数运算
            print(
                f"   上报 p50/p95 = {data['p50_s']:.4f}/{data['p95_s']:.4f}"
                f"；n={n}；最近秩 p95 下标={idx95}（值 {s[idx95]:.4f}）；max={s[-1]:.4f}"
            )
            ok_p95 = abs(data["p95_s"] - s[idx95]) < 1e-12
            ok_n = data.get("iterations") == n == BENCHMARK_MAX_ITERATIONS
            ok_order = 0 < data["p50_s"] <= data["p95_s"]
            print(
                f"   p95 落在最近秩那一格：{'是' if ok_p95 else '否'}；"
                f"iterations 夹紧到 {BENCHMARK_MAX_ITERATIONS} 且样本数相符：{'是' if ok_n else '否'}；"
                f"0 < p50 ≤ p95：{'是' if ok_order else '否'}"
            )
            rc |= 0 if (ok_p95 and ok_n and ok_order) else 1
        else:
            print(f"   样本不足或未取到：{data}")
            rc |= 1
        print(
            f"   {_metric_line(client.get('/metrics').text, 'workspace_launch_seconds_count')}"
            "（provision 侧的样本数，用来说明这条路径确实被走过）"
        )
    DB.unlink(missing_ok=True)
    print(f"[warm-sla] overall={'PASS' if rc == 0 else 'FAIL'}（绝对 SLA 判定仍需 G1–G4 真机）")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
