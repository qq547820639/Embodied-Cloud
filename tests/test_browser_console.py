"""前端六视图控制台的浏览器级用例（browser_integration）。

这一档补的是 docs/CURRENT_STATE.md 长期挂着的债："前端无自动化浏览器测试"。
既有的 test_demo_workspace / test_version_consistency 只能证服务端 HTML 字符串和
"源码里含有某段文本"，看不见 SPA 真实渲染：app.js 用模板串写 innerHTML，
路由守卫、事件委托、确认弹窗、轮询启停、`esc()` 的实际效果都只在浏览器里存在。

关键一条是 test_xss_payload_does_not_execute_in_dom：它用 `<img onerror=...>`
作载荷。字符串级断言用的 `<script>` 经 innerHTML 注入本来就不会执行，所以即使
`esc()` 被删掉，那类断言也不会翻红 —— 只有真 DOM 能证伪转义。

运行：`make test-browser`（需 playwright + 本机 Chrome/Chromium）。
缺件时整档干净跳过，skip 文案带 BROWSER_VALIDATION_PENDING 供 release gate 登记。
"""

import json
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from tests.live_server import live_server as live_server_ctx

GATE_SENTINEL = "BROWSER_VALIDATION_PENDING"

pytestmark = pytest.mark.browser_integration

VIEW_IDS = ("view-overview", "view-usage", "view-courses", "view-deploy", "view-edge", "view-gpus")
VIEWS = ("overview", "usage", "courses", "deploy", "edge", "gpus")


def _browser_channel() -> tuple[str | None, str]:
    """返回 (channel, 原因)。优先复用已装 Chrome（免下载浏览器二进制）。"""
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return None, "未安装 playwright（pip install -e '.[dev]'）"
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if chrome.exists():
        return "chrome", ""
    if shutil.which("google-chrome") or shutil.which("chromium"):
        return "chrome", ""
    # 无系统 Chrome 时退回 playwright 自带 chromium（需 playwright install）
    cache = Path.home() / "Library/Caches/ms-playwright"
    if cache.exists() and any(cache.glob("chromium-*")):
        return "", ""
    if os.environ.get("PW_USE_BUNDLED_CHROMIUM") == "1":
        return "", ""
    return None, "既没有系统 Chrome，也没有 playwright 自带 chromium"


@pytest.fixture(scope="session")
def browser_channel():
    channel, reason = _browser_channel()
    if channel is None:
        pytest.skip(f"{GATE_SENTINEL}: {reason}")
    return channel


@pytest.fixture(scope="session")
def browser(browser_channel):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        launcher = {"headless": True}
        if browser_channel:
            launcher["channel"] = browser_channel
        proc = pw.chromium.launch(**launcher)
        try:
            yield proc
        finally:
            proc.close()


@pytest.fixture(scope="session")
def live_server(tmp_path_factory):
    """真起一个 uvicorn（mock provider + 独立 sqlite + 独立 workspace 目录）。

    进程生命周期与就绪判据在 `tests/live_server.py`（与 agent e2e 共用）；这里只是
    把 session 级夹具接到 pytest 上，并把根目录放进 pytest 的 tmp_path_factory，
    好让 CI 的 TMPDIR 策略统一生效。
    """
    root = Path(tempfile.mkdtemp(prefix="ec-browser-test-", dir=tmp_path_factory.mktemp("server")))
    with live_server_ctx(root) as server:
        yield server


def _httpx():
    import httpx

    return httpx


@pytest.fixture
def api(live_server):
    """控制面 API 客户端（准备数据用；断言仍看浏览器 DOM）。"""
    httpx = _httpx()

    class Api:
        base = live_server.url

        def register(self, password: str = "browserpass123") -> tuple[str, str]:  # noqa: S107 测试口令
            # email-validator 拒用保留域（.invalid/.test 都会 422），故用 example.org + uuid
            email = f"ec-{uuid.uuid4().hex[:10]}@example.org"
            r = httpx.post(
                f"{self.base}/api/auth/register",
                json={"email": email, "username": email.split("@")[0][:8], "password": password},
                timeout=20,
            )
            r.raise_for_status()
            return email, password

        def token(self, email: str, password: str) -> str:
            r = httpx.post(
                f"{self.base}/api/auth/login", json={"email": email, "password": password}, timeout=20
            )
            r.raise_for_status()
            return str(r.json()["token"])

        def auth(self, token: str) -> dict:
            return {"Authorization": f"Bearer {token}"}

        def get(self, path: str, token: str):
            return httpx.get(f"{self.base}{path}", headers=self.auth(token), timeout=20)

        def post(self, path: str, token: str, payload: dict):
            return httpx.post(f"{self.base}{path}", headers=self.auth(token), json=payload, timeout=30)

        def request(self, method: str, path: str, token: str):
            return httpx.request(method, f"{self.base}{path}", headers=self.auth(token), timeout=30)

    return Api()


@pytest.fixture
def page_errors() -> list[str]:
    """JS 异常 + 4xx/5xx 资源加载现场（失败时能直接看到是哪个 URL）。"""
    return []


@pytest.fixture
def page(browser, live_server, page_errors):
    ctx = browser.new_context(base_url=live_server.url, viewport={"width": 1280, "height": 900})
    pg = ctx.new_page()
    pg.on("pageerror", lambda exc: page_errors.append(f"pageerror: {exc}"))
    pg.on("console", lambda msg: page_errors.append(f"console: {msg.text}") if msg.type == "error" else None)
    pg.on(
        "response",
        lambda r: page_errors.append(f"http {r.status} {r.url}") if r.status >= 400 else None,
    )
    pg.goto("/", wait_until="domcontentloaded")
    try:
        yield pg
    finally:
        ctx.close()


def login(pg, email: str, password: str) -> None:
    """走真实登录表单（同时覆盖 tab 切换与提交禁用）。"""
    pg.fill("#auth-email", email)
    pg.fill("#auth-password", password)
    pg.click("#auth-submit")
    pg.wait_for_selector("#userbar:not([hidden])", timeout=20000)


def logged_in_api_user(api, pg):
    email, password = api.register()
    login(pg, email, password)
    return email, password


def wait_for_workspace_named(pg, name: str, present: bool = True) -> None:
    """等卡片集合里出现/消失某个名字（选择器写短以免超行宽）。"""
    js = (
        "() => [...document.querySelectorAll('#workspaces article h3')]"
        f".some(e => e.textContent === {json.dumps(name)})"
    )
    pg.wait_for_function(js if present else f"() => !({js[6:]})", timeout=30000)


def workspace_names(pg) -> list[str]:
    return pg.eval_on_selector_all("#workspaces article h3", "els => els.map(e => e.textContent)")


# ---------------------------------------------------------------------------
# 渲染 / 路由
# ---------------------------------------------------------------------------


def test_six_views_render_without_js_errors(page, page_errors, api):
    """六视图逐个显示，且全程零 JS 异常（此前无任何自动化覆盖过这条路径）。"""
    logged_in_api_user(api, page)

    for view in VIEWS:
        if view == "gpus":
            continue  # 非管理员被守卫拦下，单独一条用例覆盖
        page.click(f'#nav-{view}' if page.query_selector(f'#nav-{view}') else f'.nav-item[data-view="{view}"]')
        page.wait_for_selector(f"#{VIEW_IDS[VIEWS.index(view)]}", state="visible", timeout=20000)
        assert page.is_visible(f"#{VIEW_IDS[VIEWS.index(view)]}"), f"{view} 视图未显示"
    assert page_errors == [], f"控制台出现 JS 错误：{page_errors}"


def test_unauthenticated_route_guard_sends_back_to_overview(page):
    page.goto("/#/usage", wait_until="domcontentloaded")
    page.wait_for_function("location.hash === '#/overview'", timeout=20000)
    assert "请先登录" in page.inner_text("#toast")
    assert page.is_visible("#view-usage") is False
    assert page.is_visible("#view-overview") is True


def test_non_admin_cannot_open_gpu_view(page, api):
    email, password = api.register()
    login(page, email, password)
    page.goto("/#/gpus", wait_until="domcontentloaded")
    page.wait_for_function("location.hash === '#/overview'", timeout=20000)
    assert "需要管理员权限" in page.inner_text("#toast")
    assert page.query_selector('.nav-item[data-view="gpus"]') is None or page.is_hidden('.nav-item[data-view="gpus"]')


# ---------------------------------------------------------------------------
# 管理台的 GPU 行：三个维度各自的读者（N-128）
# ---------------------------------------------------------------------------


def _promote_admin(db_path, email: str) -> None:
    """把刚注册的用户抬成 admin。

    管理台是「占用／健康／下架意图」这三列唯一的人类读者，所以这条线必须真的以 admin
    身份进 `#/gpus`；注册接口只发 user，提升就直接写这个测试自己拥有的 sqlite 文件。
    rowcount 判 1：写不到人身上时不能继续，否则后面每一句都是"没有管理员"造成的假绿。
    """
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        cur = conn.execute("UPDATE users SET role='admin' WHERE email=?", (email,))
        conn.commit()
        assert cur.rowcount == 1, f"提升 admin 落空（users 里没有 {email}）"


def _click_and_confirm(page, selector: str, confirm_text: str) -> None:
    """点行内动作 → 确认弹窗里点那一颗同名按钮 → 等弹窗关掉（动作是异步发的）。"""
    page.click(selector)
    page.wait_for_selector("#dialog-root .dialog-backdrop", timeout=20000)
    page.evaluate(
        """(label) => {
            const btns = [...document.querySelectorAll('#dialog-root .dialog-actions button')];
            const b = btns.find((x) => x.textContent.trim() === label);
            if (!b) throw new Error('确认弹窗里没有 ' + label + '：' + btns.map((x) => x.textContent));
            b.click();
        }""",
        confirm_text,
    )
    page.wait_for_selector("#dialog-root .dialog-backdrop", state="detached", timeout=20000)


def _gpu_row(page, gpu_uuid: str) -> str:
    selector = f'#gpus table tbody tr:has-text("{gpu_uuid}")'
    page.wait_for_selector(selector, timeout=30000)
    return selector


def _has_chip(page, row: str, text: str) -> bool:
    """行内徽章位点：整行取文本会被按钮字面量污染（"标记异常"里就含"异常"）。"""
    return page.query_selector(f'{row} .badge:has-text("{text}")') is not None


def _gpu_by_id(api, token: str, gpu_id: str) -> dict:
    return next(g for g in api.get("/api/gpus", token).json() if g["id"] == gpu_id)


def test_admin_gpu_row_shows_the_matching_pole_of_each_verdict(page, api, live_server, page_errors):
    """每张卡的按钮和徽章必须由当前判决决定——死比较只有真 DOM 能抓到（N-126 的读者面）。

    改前的 `app/static/app.js:1253` 写的是 `g.status !== "unhealthy"`：状态列在 N-126 之后
    不存在这个值，于是这个条件恒为真，"标记异常"按钮在任何卡上都不消失。字符串级断言
    读不出"恒为真"（它只看得见文本在不在源码里），六视图那条用例又跳过 gpus 视图，
    所以这一档此前没有任何读者。

    两个极性各自都要走到，才谈得上"按钮跟着判决走"：
    - 空闲卡：`/unhealthy` ⇒ 异常徽章 + 按钮翻成`恢复健康`；`/healthy` ⇒ 回到原极。
    - 占用卡：`/drain` 只登记意图 ⇒ `已请求排水`读者出现、状态**仍是**已分配；工作区停掉
      之后意图被兑现成人工下架，再`撤回下架要求`回到可用（末尾这步顺手把池子还回去）。
    """
    email, password = api.register()
    _promote_admin(live_server.db_path, email)
    token = api.token(email, password)
    free = next(
        g
        for g in api.get("/api/gpus", token).json()
        if g["status"] == "available" and g["health"] is None and g["drain_requested_at"] is None
    )

    login(page, email, password)
    page.goto("/#/gpus", wait_until="domcontentloaded")
    row = _gpu_row(page, free["gpu_uuid"])

    # 前提自证：这一行的初始极是"下判决"，不是"撤判决"
    assert page.is_visible(f'{row} [data-action="gpu-unhealthy"]'), "空闲卡没给标记异常按钮"
    assert not page.is_visible(f'{row} [data-action="gpu-healthy"]'), (
        "从没被判过的卡直接出现`恢复健康`：健康列不是这个形状（NULL＝没人判过）"
    )

    _click_and_confirm(page, f'{row} [data-action="gpu-unhealthy"]', "标记异常")
    page.wait_for_selector(f'{row} [data-action="gpu-healthy"]', timeout=30000)
    assert page.query_selector(f'{row} .badge.unhealthy') is not None, (
        "判了异常却没有健康徽章——刚拆出来的那一维又被藏回去"
    )
    judged = _gpu_by_id(api, token, free["id"])
    assert judged["health"] == "unhealthy", judged
    assert judged["status"] == "available" and judged["drain_requested_at"] is None, (
        f"/unhealthy 越界改了别的维度：{judged}"
    )

    _click_and_confirm(page, f'{row} [data-action="gpu-healthy"]', "恢复健康")
    page.wait_for_selector(f'{row} [data-action="gpu-unhealthy"]', timeout=30000)
    restored = _gpu_by_id(api, token, free["id"])
    assert restored["health"] is None, restored
    # 徽章那一半只能按位点判：行内文字里"标记异常"这颗按钮本来就含"异常"，整行取文本分不出两极
    assert page.query_selector(f'{row} .badge.unhealthy') is None, "撤了判决徽章还在"

    # ── 占用卡：意图是证据列，不覆盖占用事实 ──
    template_id = api.get("/api/templates", token).json()[0]["id"]
    created = api.post(
        "/api/workspaces", token, {"name": "gpu-intent", "template_id": template_id, "auto_start": True}
    )
    assert created.status_code == 201, created.text
    ws_id = created.json()["id"]
    deadline = time.monotonic() + 60
    busy: dict | None = None
    while time.monotonic() < deadline:
        busy = next((g for g in api.get("/api/gpus", token).json() if g["workspace_id"] == ws_id), None)
        if busy is not None:
            break
        time.sleep(0.5)
    assert busy is not None, f"60s 内没有卡被分配给 {ws_id}，占用前提没造出来"

    row = _gpu_row(page, busy["gpu_uuid"])
    assert page.is_visible(f'{row} [data-action="gpu-drain"]'), "占用中的卡不给下架按钮（N-125 之后判得动）"
    assert not _has_chip(page, row, "已请求排水")
    _click_and_confirm(page, f'{row} [data-action="gpu-drain"]', "确认下架")
    page.wait_for_selector(f'{row} [data-action="gpu-undrain"]', timeout=30000)
    assert _has_chip(page, row, "已请求排水"), "意图列有值却在表格里读不到——这一列就没有人类读者"
    pending = _gpu_by_id(api, token, busy["id"])
    assert pending["status"] == "allocated" and pending["drain_requested_at"], (
        f"登记意图时把占用抹掉了：{pending}"
    )

    api.request("POST", f"/api/workspaces/{ws_id}/stop", token)
    page.evaluate("() => showView('gpus')")
    released = _gpu_by_id(api, token, busy["id"])
    assert released["status"] == "drained", f"释放后意图没被兑现成下架：{released}"
    gpu_row = _gpu_row(page, busy["gpu_uuid"])
    assert not _has_chip(page, gpu_row, "已请求排水"), (
        "下架已兑现却还挂着待兑现的读者，表格里同一件事出现两种说法"
    )
    _click_and_confirm(page, f'{row} [data-action="gpu-undrain"]', "撤回要求")
    back = _gpu_by_id(api, token, busy["id"])
    assert back["status"] == "available" and back["drain_requested_at"] is None, back
    assert page.is_visible(f'{_gpu_row(page, busy["gpu_uuid"])} [data-action="gpu-drain"]')
    # 全程零 JS 异常、零 4xx/5xx：改前占用中的卡 `/drain` 回 409，会被这根探针直接记下来
    assert page_errors == [], f"控制台出现 JS 错误或失败请求：{page_errors}"



def test_register_login_and_logout_userbar(page, api):
    email, password = api.register()
    login(page, email, password)
    assert email.split("@")[0][:8] in page.inner_text("#auth-user")
    assert page.is_visible("#auth-form") is False
    page.click("#auth-logout")
    page.wait_for_selector("#auth-form", state="visible", timeout=20000)
    assert page.is_hidden("#userbar")


# ---------------------------------------------------------------------------
# 安全：真 DOM 里的转义
# ---------------------------------------------------------------------------


def test_xss_payload_does_not_execute_in_dom(page, api):
    """载荷必须能在"esc() 被删掉"时翻红：用 <img onerror> 而不是 <script>。"""
    email, password = api.register()
    token = api.token(email, password)
    templates = api.get("/api/templates", token).json()
    template_id = templates[0]["id"] if isinstance(templates, list) else templates["items"][0]["id"]
    payload = '<img src=x onerror="window.__pwned=1">'
    created = api.post("/api/workspaces", token, {"name": payload, "template_id": template_id})
    assert created.status_code in (200, 201), created.text

    login(page, email, password)
    page.wait_for_function(
        "() => document.querySelectorAll('#workspaces article h3').length > 0", timeout=30000
    )
    assert payload in workspace_names(page), f"工作区名未按原文渲染：{workspace_names(page)}"
    assert page.evaluate("() => window.__pwned === undefined"), "onerror 载荷被执行了（转义失效）"
    assert page.query_selector_all("#workspaces img") == [], "DOM 里出现了真实 img 节点"


# ---------------------------------------------------------------------------
# 交互：事件委托 / 确认弹窗 / 防重
# ---------------------------------------------------------------------------


def test_create_workspace_through_prompt_dialog_and_delegation(page, api):
    email, password = api.register()
    login(page, email, password)
    page.wait_for_selector('[data-action="create-workspace"]', timeout=30000)
    with page.expect_request("**/api/workspaces") as req:
        page.click('[data-action="create-workspace"]')
        page.wait_for_selector("#dialog-root input", timeout=20000)
        page.fill("#dialog-root input", "ui-created-ws")
        page.click('#dialog-root button:has-text("确定")')
    assert req.value.method == "POST"
    wait_for_workspace_named(page, "ui-created-ws")


def wait_until_no_transient(pg, timeout: int = 60) -> None:
    """等所有工作区徽标脱离瞬态（created/queued/provisioning/stopping）。

    这是删除用例的前提：生命周期操作未落定时删除，服务端会如实返回 409
    （见 tests/test_api.py::test_lifecycle_conflict_…），那时卡片本就该留下。
    """
    js = (
        "() => ![...document.querySelectorAll('#workspaces .badge')]"
        ".some(b => /created|queued|provisioning|stopping/.test(b.className))"
    )
    pg.wait_for_function(js, timeout=timeout * 1000)


def test_destructive_delete_requires_confirmation_then_runs_once(page, api):
    """破坏性操作：取消不得发 DELETE；确认后只发一次，且卡片真的消失。"""
    email, password = api.register()
    token = api.token(email, password)
    template_id = api.get("/api/templates", token).json()[0]["id"]
    api.post("/api/workspaces", token, {"name": "to-delete", "template_id": template_id})
    login(page, email, password)
    page.wait_for_selector('[data-action^="delete"]', timeout=30000)
    wait_until_no_transient(page)

    # 取消路径：不发 DELETE，卡片仍在
    page.click('[data-action^="delete"]')
    page.wait_for_selector("#dialog-root .dialog-backdrop", timeout=20000)
    page.click('#dialog-root button:has-text("取消")')
    page.wait_for_selector("#dialog-root .dialog-backdrop", state="detached", timeout=20000)
    assert "to-delete" in workspace_names(page)

    # 确认路径：连点三次也只发一次 DELETE（busy 防重集合）
    deletes: list[str] = []
    page.on(
        "request",
        lambda r: deletes.append(r.url) if r.method == "DELETE" and "/api/workspaces/" in r.url else None,
    )
    page.click('[data-action^="delete"]')
    page.wait_for_selector("#dialog-root .dialog-backdrop", timeout=20000)
    page.evaluate(
        """() => {
            const btns = [...document.querySelectorAll('#dialog-root .dialog-actions button')];
            const b = btns.find(x => x.textContent.trim() === '永久删除');
            if (!b) throw new Error('未渲染 danger 确认按钮：' + btns.map(x => x.textContent));
            b.click(); b.click(); b.click();
        }"""
    )
    wait_for_workspace_named(page, "to-delete", present=False)
    assert len(deletes) == 1, f"in-flight 防重失效，DELETE 发了 {len(deletes)} 次"


# ---------------------------------------------------------------------------
# 状态反馈：轮询启停 / API 不可用 / 视口
# ---------------------------------------------------------------------------


def test_polling_runs_while_transient_then_stops(page, api):
    """轮询两档判据：有非终态工作区时必须持续拉，全部落定后必须停。

    只测"停"是空断言（轮询从未启动时同样为 0 请求）。这里用 route.fetch 把真实
    /api/workspaces 响应里的 status 改写成 provisioning，确定性地制造瞬态；再放行
    真实响应（mock provider 早已 RUNNING）观察停止。
    """
    email, password = api.register()
    token = api.token(email, password)
    templates = api.get("/api/templates", token).json()
    template_id = templates[0]["id"]
    api.post("/api/workspaces", token, {"name": "poll-probe", "template_id": template_id})
    login(page, email, password)

    hold = {"transient": True}

    def handler(route):
        resp = route.fetch()
        if not hold["transient"]:
            route.fulfill(response=resp)
            return
        try:
            body = resp.json()
        except ValueError:
            route.fulfill(response=resp)
            return
        if isinstance(body, list) and body:
            body[0] = {**body[0], "status": "provisioning", "started_at": None}
            route.fulfill(response=resp, json=body)
        else:
            route.fulfill(response=resp)

    page.route("**/api/workspaces", handler)
    list_calls: list[float] = []
    page.on(
        "request",
        lambda r: list_calls.append(time.monotonic())
        if r.method == "GET" and r.url.endswith("/api/workspaces")
        else None,
    )

    def count_in_window(seconds: float) -> int:
        """窗口内计数。必须用 page.wait_for_timeout 而不是 time.sleep：
        Playwright 同步 API 的事件只在被调用时派发，纯 sleep 会让 request
        回调整批滞后，读数恒为 0（实测踩到）。"""
        base = len(list_calls)
        elapsed = 0.0
        while elapsed < seconds:
            page.wait_for_timeout(200)
            elapsed += 0.2
        return len(list_calls) - base

    # 触发一次列表加载（改写后为瞬态 → ensurePolling 应启动 2s 定时器）。
    # 注意：showView("overview") 只刷 health+templates，工作区列表在 boot 时加载，
    # 所以这里用 reload 而不是切视图来触发。
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("#workspaces article", timeout=30000)
    assert count_in_window(7) >= 2, "工作区处于瞬态时没有观察到轮询（判据前提未成立）"

    # 放行真实状态后：最后一条 tick 会再拉一次并发现已落定 → 清掉定时器；
    # 判据是"清掉之后不再有请求"，而不是"总共 0 次"（那一次收尾请求是设计内的）
    hold["transient"] = False
    page.wait_for_function("() => pollTimer === null", timeout=30000)
    assert count_in_window(7) == 0, "定时器已清除却仍在发 /api/workspaces"


def test_health_badge_switches_to_api_unavailable(page, api):
    page.wait_for_function("() => !document.querySelector('#health').textContent.includes('检查中')", timeout=20000)
    assert "mock" in page.inner_text("#health")
    page.route("**/api/health", lambda route: route.abort())
    page.click(".nav-item[data-view='overview']")
    page.wait_for_function(
        "() => document.querySelector('#health').textContent.trim() === 'API 不可用'", timeout=20000
    )
    assert "bad" in (page.get_attribute("#health", "class") or "")


def test_mobile_viewport_has_no_horizontal_overflow(page, api):
    logged_in_api_user(api, page)
    page.set_viewport_size({"width": 390, "height": 844})
    for view in ("overview", "usage", "courses", "deploy", "edge"):
        page.click(f'.nav-item[data-view="{view}"]')
        page.wait_for_selector(f"#view-{view}", state="visible", timeout=20000)
        metrics = page.evaluate(
            "() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]"
        )
        assert metrics[0] <= metrics[1] + 1, f"{view} 在 390px 下横向溢出：{metrics}"


def test_version_pill_comes_from_api_not_hardcoded(page, api):
    """#version-pill 必须显示 /api/health 里的版本（源码 grep 证明不了这一点）。"""
    health = json.loads(page.evaluate("async () => JSON.stringify(await (await fetch('/api/health')).json())"))
    page.wait_for_function(
        "(v) => document.querySelector('#version-pill').textContent === 'v' + v",
        arg=health["version"],
        timeout=20000,
    )
    assert page.inner_text("#version-pill") == f"v{health['version']}"
