"""每条部署要有自己的运行时长预算，超时的运行由控制面判死（N-134）。

改前的形状：`running` 只有三种出口——设备回报（N-113）、用户手工 `/complete`、
以及设备被判离线时那趟顺带收口（N-133）。第三种用的是一个**全局**数
（`edge_agent_offline_after_seconds`，默认 90 s），它量的是"这台设备最后一次心跳距今多久"，
而运行需要回答的是另一件事："这条运行开了多久"。两个不同的事实共用一个数，双向都错：
一次合法要跑 20 分钟的巡检会被 90 s 的静默判死，而一次 5 秒的推理拿到 90 s 几乎不设防。
另外那一档 `edge_agent_id IS NULL` 的 `running`（运维/脚本直接在库外推上去的行）
N-133 明确不判——它拿不出"最后一次被看见"的证据。

修法借 K8s Job 那一格的形状：`spec.activeDeadlineSeconds` 是每个 Job 一列、相对它自己的
`.status.startTime` 量，超时的运行判成一次已结束的失败。本仓对应
`deployments.run_deadline_seconds`（预算）＋`run_started_at`（起跑时刻，由 `run_policy`
在 verified → running 那一刻盖章）。判死用条件 UPDATE，`status == running` 钉在 WHERE 里，
所以设备回报、N-133 的掉线收口与这条 sweep 是三个写者竞争同一行：第一个赢家定案。

为什么不能用 `updated_at` 当起跑时刻：它带 `onupdate`，任何一次与运行无关的写都会把它顶新，
那等于把"重置这条运行的时钟"的权力交给每一个写者（登记项 N-115 当年就点过这一格）。
预算与起跑时刻都是 nullable，NULL 一律**不判**：没定过预算＝没人做过这个决定，
存量那几条没有盖章时刻的 running＝证据不存在——ADR 0008 那条口径同样适用，
未知不等于违规，也不等于超时。

前提全部由真实生产者造：注册/心跳/begin/报摘要走 HTTP 端点，预算走
`POST /api/deployments` 的请求体，时钟前进用两种各有分工的手法——
服务侧那条判据接受 `now` 注入（于是"过了预算"这件事可以精确到秒地断言），
周期驱动者那一支不接受注入，就把 `run_started_at` 真的往前挪（模拟时间过去了一小时）。
只有"库外写进来的那条 running"例外，那一档本来就没有 API 生产者。
"""

import re
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import deps
from app.deps import SessionFactory, deployment_service
from app.main import app
from app.models import DeploymentRecord, utcnow
from tests.test_edge_run_closes_deployment import _report, _setup, _status, _to_running

ROBOT = "franka"
DEADLINE_MESSAGE = "run exceeded its own deadline before reporting"
_ROOT = Path(__file__).resolve().parents[1]
_DEPLOYMENT_SERVICE = _ROOT / "app" / "services" / "deployment.py"


def _deploy_with_budget(
    client: TestClient, st: dict, run_deadline_seconds: int | None
) -> dict:
    """预算由真实请求体带进来——它是部署的一个属性，不是运维的一个开关。"""
    body = {
        "workspace_id": st["workspace_id"],
        "artifact_path": st["path"],
        "robot_type": ROBOT,
        "edge_agent_id": st["agent_id"],
    }
    if run_deadline_seconds is not None:
        body["run_deadline_seconds"] = run_deadline_seconds
    resp = client.post("/api/deployments", json=body, headers=st["headers"])
    assert resp.status_code == 201, resp.text
    return resp.json()


def _judge(now=None) -> int:
    with SessionFactory() as db:
        return deployment_service.fail_overdue_runs(db, now=now)


def _row(deployment_id: str) -> DeploymentRecord:
    with SessionFactory() as db:
        return db.get(DeploymentRecord, deployment_id)


def test_a_run_that_overspends_its_own_budget_is_failed_by_the_control_plane():
    """过了预算 ⇒ failed，且这是控制面单方面的判决：没有人回报，也没人手工 complete。"""
    with TestClient(app) as client:
        st = _setup(client, "deadline-over-a@example.com")
        dep = _deploy_with_budget(client, st, 1)
        _to_running(client, st, dep)
        assert _status(dep["id"]) == ("running", None)

        judged = _judge(now=utcnow() + timedelta(seconds=61))

        assert judged >= 1, judged
        status, error = _status(dep["id"])
        assert status == "failed", status
        assert error == DEADLINE_MESSAGE, error
        # 秒数不进 error_message：它留在被核的那两列里，读者要的是同一份事实。
        assert _row(dep["id"]).run_deadline_seconds == 1


def test_a_run_inside_its_budget_is_not_judged_even_after_the_global_90s_would_kill_it():
    """这条判据的正主：长任务不该因为"设备 90 秒没说话"就被判死。

    预算 3600 s 的运行，此刻只开了 300 s ⇒ 那一行一动不动。这一支与上一支是同一趟
    sweep 的两极，只差被核的那个时间——少了这一支，"超时判死"永远绿着把长任务杀光。
    计数一律不比：这一库是全测试共用的，别的用例留下的 running 也会被同一趟扫到，
    所以每支都只钉"我这一行的状态"，那才是本用例造出来的那个前提。
    """
    with TestClient(app) as client:
        st = _setup(client, "deadline-inside-b@example.com")
        dep = _deploy_with_budget(client, st, 3600)
        _to_running(client, st, dep)

        _judge(now=utcnow() + timedelta(seconds=300))

        assert _status(dep["id"]) == ("running", None), "预算没花完就判死＝把预算列当装饰"


def test_a_run_without_a_budget_is_never_judged():
    """NULL＝没人定过预算，控制面不替人做这个决定（不填默认值、不拿全局数顶上）。"""
    with TestClient(app) as client:
        st = _setup(client, "deadline-none-c@example.com")
        dep = _deploy_with_budget(client, st, None)
        _to_running(client, st, dep)
        assert dep["run_deadline_seconds"] is None

        _judge(now=utcnow() + timedelta(days=365))

        assert _status(dep["id"]) == ("running", None), (
            "没有预算的行被扫掉，等于控制面替所有人定了一个从未被说过的默认时限"
        )


def test_a_running_row_without_a_stamp_is_not_judged():
    """有预算但没有起跑时刻（迁移之前进过 running 的存量行）⇒ 不判，不回填猜测值。"""
    with TestClient(app) as client:
        st = _setup(client, "deadline-nostamp-d@example.com")
        dep = _deploy_with_budget(client, st, 1)
        _to_running(client, st, dep)
        with SessionFactory() as db:
            row = db.get(DeploymentRecord, dep["id"])
            row.run_started_at = None  # 存量行的形状：这一格从来没被盖过章
            db.commit()

        _judge(now=utcnow() + timedelta(days=1))

        assert _status(dep["id"]) == ("running", None), (
            "拿 `updated_at` 或 now 顶替起跑时刻，等于凭空造一条从未发生过的超时"
        )


def test_the_budget_judges_a_running_row_that_is_bound_to_no_device():
    """N-133 明确跳过的那一档在这里第一次有了归属者：预算量时间，不需要设备在场。"""
    with TestClient(app) as client:
        st = _setup(client, "deadline-unbound-e@example.com")
        dep = _deploy_with_budget(client, st, 1)
        _to_running(client, st, dep)
        with SessionFactory() as db:
            row = db.get(DeploymentRecord, dep["id"])
            row.edge_agent_id = None  # 运维/脚本直接在库外推上去的那条 running
            db.commit()

        judged = _judge(now=utcnow() + timedelta(seconds=120))

        assert judged >= 1, judged
        status, error = _status(dep["id"])
        assert status == "failed" and error == DEADLINE_MESSAGE, (status, error)


def test_the_report_and_the_deadline_do_not_both_write_the_terminal_state():
    """三个写者竞争同一行：谁先定案谁说了算，后到的读数改不动它（两个方向都测）。"""
    with TestClient(app) as client:
        # 先报成功，再让预算过期：已成的终态不被时效翻掉。
        st = _setup(client, "deadline-race-f@example.com")
        dep = _deploy_with_budget(client, st, 1)
        _to_running(client, st, dep)
        _report(client, st, dep["id"], ok=True, detail="franka done")
        assert _status(dep["id"]) == ("success", None)

        _judge(now=utcnow() + timedelta(hours=2))
        assert _status(dep["id"]) == ("success", None), "超时判决不许把已成功的运行翻成失败"

        # 反向：先被判超时，后到的 ok=true 也救不回来。
        st2 = _setup(client, "deadline-race-g@example.com")
        dep2 = _deploy_with_budget(client, st2, 1)
        _to_running(client, st2, dep2)
        assert _judge(now=utcnow() + timedelta(hours=2)) >= 1
        _report(client, st2, dep2["id"], ok=True, detail="late reconnect")
        status, error = _status(dep2["id"])
        assert status == "failed" and error == DEADLINE_MESSAGE, (status, error)


def test_running_stamps_its_own_start_and_a_repeat_run_does_not_reset_the_clock():
    """预算要有可量的起点，而且这个起点不能被第二个幂等 POST 悄悄续命。

    重复调用要测**两种 body**：不带 agent 的那一支走的是"什么都不写"的分支（连
    `db.commit()` 都没有），带 agent 的那一支才真的提交。只测前者时，"重盖章"这种改动
    会因为没走到 commit 而完全不可见——本仓的变异电池实测过这一点，所以两支都得按。
    """
    with TestClient(app) as client:
        st = _setup(client, "deadline-stamp-h@example.com")
        dep = _deploy_with_budget(client, st, 60)
        assert _row(dep["id"]).run_started_at is None, "还没进 running 不该有起跑时刻"

        _to_running(client, st, dep)
        first = _row(dep["id"]).run_started_at
        assert first is not None

        again = client.post(f"/api/deployments/{dep['id']}/run", json={}, headers=st["headers"])
        assert again.status_code == 200, again.text
        assert _row(dep["id"]).run_started_at == first, (
            "重复 /run（无 body）重盖起跑时刻＝用一个幂等 POST 给永远不会结束的 running 续命"
        )

        with_agent = client.post(
            f"/api/deployments/{dep['id']}/run",
            json={"edge_agent_id": st["agent_id"]},
            headers=st["headers"],
        )
        assert with_agent.status_code == 200, with_agent.text
        assert _row(dep["id"]).run_started_at == first, (
            "带 agent 的重复 /run 会 commit，那一支里重盖章同样是不允许的续命"
        )


def test_the_budget_and_the_stamp_are_on_the_wire_the_device_reads():
    """设备在跑之前就得知道自己的预算：assigned 那一面必须把两列透出去。"""
    with TestClient(app) as client:
        st = _setup(client, "deadline-wire-i@example.com")
        dep = _deploy_with_budget(client, st, 900)
        assigned = client.get(
            f"/api/edge/agents/{st['agent_id']}/deployments/assigned",
            headers={"X-Agent-Token": st["agent_token"]},
        )
        assert assigned.status_code == 200, assigned.text
        record = next(item for item in assigned.json() if item["id"] == dep["id"])
        assert record["run_deadline_seconds"] == 900, record
        assert record["run_started_at"] is None, record

        _to_running(client, st, dep)
        assigned2 = client.get(
            f"/api/edge/agents/{st['agent_id']}/deployments/assigned",
            headers={"X-Agent-Token": st["agent_token"]},
        )
        record2 = next(item for item in assigned2.json() if item["id"] == dep["id"])
        assert record2["run_started_at"], "进过 running 的行在对外那面上读不到起跑时刻"


@pytest.mark.parametrize("rejected", [0, -1])
def test_a_budget_that_is_not_a_positive_number_is_refused_at_the_boundary(rejected: int) -> None:
    """`ge=1` 的来由：0 秒的预算等于「派工即判死」，那不是预算，是要表达「不设预算」的另一种写法，
    而那一层意思已经由 NULL 承担了。K8s 同一条字段的注释也写着 value must be positive integer。
    """
    with TestClient(app) as client:
        st = _setup(client, f"deadline-boundary-{rejected}@example.com")
        resp = client.post(
            "/api/deployments",
            json={
                "workspace_id": st["workspace_id"],
                "artifact_path": st["path"],
                "robot_type": ROBOT,
                "run_deadline_seconds": rejected,
            },
            headers=st["headers"],
        )
        assert resp.status_code == 422, (rejected, resp.status_code, resp.text)


def test_the_periodic_driver_actually_judges_overdue_runs():
    """没有周期驱动者，这条判决就只在有人手工叫它时才发生（N-93／N-99／N-110 的规矩）。

    这一支不接受 `now` 注入——驱动者自己取当前时间，所以把起跑时刻真的往前挪一小时，
    量的才是"运行开了很久"这件事本身。
    """
    with TestClient(app) as client:
        st = _setup(client, "deadline-driver-j@example.com")
        dep = _deploy_with_budget(client, st, 60)
        _to_running(client, st, dep)
        with SessionFactory() as db:
            row = db.get(DeploymentRecord, dep["id"])
            row.run_started_at = row.run_started_at - timedelta(hours=1)
            db.commit()

        assert deps._fail_overdue_runs() >= 1
        assert _status(dep["id"])[0] == "failed"


def test_the_deadline_is_measured_from_its_own_stamp_and_not_from_updated_at() -> None:
    """时钟的来源要钉死：判据读 `run_started_at`，不读那个所有写者都能顶新的列。

    `updated_at` 带 `onupdate`，把它当起跑时刻等于让每一次写都重置预算——而这条改动
    在行为上是静默的（测试照样绿，因为写操作总是把时间推到"还没超时"）。
    """
    src = _DEPLOYMENT_SERVICE.read_text(encoding="utf-8")
    body = src[src.index("def fail_overdue_runs") : src.index("def _fail(")]
    assert "DeploymentRecord.run_started_at.is_not(None)" in body, body[:200]
    assert "run_deadline_seconds" in body
    # 钉的是**列引用**而不是那个词本身：说明段里必须解释"为什么不用 updated_at"，
    # 拿散文当被核面会让这条判据在自己的注释上翻红。
    assert "DeploymentRecord.updated_at" not in body, "预算判据一旦改读 updated_at，任何写都能把超时推走"
    stamp = src[src.index("def run_policy") : src.index("def complete(")]
    assert "deployment.run_started_at = utcnow()" in stamp, "进 running 不盖章就没有起跑时刻"


# ---------------------------------------------------------------------------
# 管理台那一列：预算要有读者，NULL 要有自己的说法（与 N-135 的主机行同形）
# ---------------------------------------------------------------------------

_DEP_ROW_CLEAN = """
const el = `<table>
  <tbody>${deployments.map((d) => {
    return `<tr><td>${esc(d.status)}</td>"
      + "<td>${d.run_deadline_seconds ? `${d.run_deadline_seconds} 秒` : \"未设预算\"}</td></tr>`;
  }).join("")}</tbody></table>`;
"""

_DEP_ROW_NO_FIELD = """
const el = `<table>
  <tbody>${deployments.map((d) => {
    return `<tr><td>${esc(d.status)}</td><td>未设预算</td></tr>`;
  }).join("")}</tbody></table>`;
"""

_DEP_ROW_NO_NULL_FACE = """
const el = `<table>
  <tbody>${deployments.map((d) => {
    return `<tr><td>${esc(d.status)}</td><td>${d.run_deadline_seconds} 秒</td></tr>`;
  }).join("")}</tbody></table>`;
"""

# 部署那一行是**块体**箭头函数（要先算 ops 再 return 模板），而 `row_template` 那把尺
# 只认表达式体（GPU/主机行的形状）。这里不 widening 别人的尺子——它的控制是按表达式体写的，
# 改动会让那条已在册的判据换语义；改用自己这条，并把"必须读完整行"的自证带上。
_DEP_ROW_BLOCK = re.compile(
    r"deployments\.map\(\((\w+)\)\s*=>\s*\{.*?return\s*`(.*?)`;\s*\}\)\.join\(",
    re.S,
)


def dep_row_template(js: str) -> tuple[str, str]:
    """取块体行模板，返回 (别名, 模板体)；读不全必须响亮地失败。"""
    row = _DEP_ROW_BLOCK.search(js)
    assert row, "app.js 里读不到 deployments.map((别名) => { … return `…` }) 这段行渲染，这条判据没有分母"
    body = row.group(2)
    assert body.rstrip().endswith("</tr>"), (
        f"行模板只读到一截（末尾 {body[-40:]!r}）：再往后的内容会静默漏掉，判据就不能算读过"
    )
    return row.group(1), body


def dep_row_offenders(js: str) -> list[str]:
    """部署行必须 ① 读 `run_deadline_seconds`，② 给 NULL 一个「未设预算」的说法。

    ②是 ADR 0008 那一半：NULL 是"没人定过预算"，渲染成空白会被读成"这一列没数据"，
    渲染成 `0 秒` 更会被读成"这条运行一派工就该死"——两种都是编造。
    """
    _, body = dep_row_template(js)
    out = []
    if "run_deadline_seconds" not in body:
        out.append("部署行没渲染 `run_deadline_seconds`：这条运行的预算对人类读者不可见")
    if "未设预算" not in body:
        out.append("部署行缺 NULL 的兜底写法：没定过预算会被渲染成空白或 0 秒")
    return out


def test_the_deployment_row_ruler_fires_on_each_missing_half() -> None:
    """尺子自己要有牙：两格各能单独开火，合规夹具必须干净。"""
    assert dep_row_offenders(_DEP_ROW_CLEAN) == []
    # 把预算写死成一句「未设预算」：NULL 的说法在，字段读者没了——这一支只该开第一枪。
    assert dep_row_offenders(_DEP_ROW_NO_FIELD) == [
        "部署行没渲染 `run_deadline_seconds`：这条运行的预算对人类读者不可见",
    ]
    assert dep_row_offenders(_DEP_ROW_NO_NULL_FACE) == [
        "部署行缺 NULL 的兜底写法：没定过预算会被渲染成空白或 0 秒"
    ]
    with pytest.raises(AssertionError, match="没有分母"):
        dep_row_offenders("const t = `<p>没有部署表</p>`;")


def test_the_deployment_row_shows_the_run_budget() -> None:
    """真树上那一格：读得到字段、NULL 有说法，而且表头与单元格数量对得上。"""
    js = (_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert dep_row_offenders(js) == []
    region = js[js.index("async function loadDeployments") : js.index("async function handleDeploySubmit")]
    head = next(line for line in region.splitlines() if "<thead>" in line)
    assert "<th>运行预算</th>" in head, "表头没有那一格：列数与单元格数会不一致"
    _, body = dep_row_template(js)
    assert body.count("<td") == head.count("<th>"), (
        f"表头 {head.count('<th>')} 格而行里 {body.count('<td')} 格：多出来的列会整表错位"
    )
