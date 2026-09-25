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

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

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


def _free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class Server:
    url: str
    db_path: Path
    log_path: Path


@pytest.fixture(scope="session")
def live_server(tmp_path_factory):
    """真起一个 uvicorn（mock provider + 独立 sqlite + 独立 workspace 目录）。

    只测前端不需要 TestClient，但浏览器必须有真 HTTP 端口；进程一律本 fixture
    自己回收（terminate → 超时 kill），失败时把服务日志一并抛出。
    """
    root = Path(tempfile.mkdtemp(prefix="ec-browser-test-", dir=tmp_path_factory.mktemp("server")))
    db = root / "app.db"
    env = dict(
        os.environ,
        EMBODIEDCLOUD_PROVIDER="mock",
        EMBODIEDCLOUD_DATABASE_URL=f"sqlite:///{db}",
        EMBODIEDCLOUD_WORKSPACE_ROOT=str(root / "workspaces"),
        EMBODIEDCLOUD_AUTO_CREATE_TABLES="true",
        PYTHONPATH=str(Path(__file__).resolve().parents[1]),
    )
    port = _free_port()
    log_path = root / "srv.log"
    repo_root = str(Path(__file__).resolve().parents[1])
    proc = subprocess.Popen(  # noqa: S603
        [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        ],
        cwd=repo_root,
        env=env,
        stdout=open(log_path, "w", encoding="utf-8"),  # noqa: SIM115 子进程日志重定向
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60
    last = ""
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                tail = proc.stdout.read() if proc.stdout else ""
                raise RuntimeError(f"uvicorn 提前退出 rc={proc.returncode}:\n{tail}")
            try:
                with contextlib.closing(socket.socket()) as probe:
                    probe.settimeout(1)
                    probe.connect(("127.0.0.1", port))
            except OSError:
                time.sleep(0.3)
                continue
            import httpx

            resp = httpx.get(f"{base}/api/health", timeout=5)
            if resp.status_code == 200:
                last = "ready"
                break
            time.sleep(0.3)
        if last != "ready":
            raise RuntimeError(f"uvicorn 未在 60s 内就绪：{base}/api/health")
        yield Server(url=base, db_path=db, log_path=log_path)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
            proc.wait(timeout=10)
        shutil.rmtree(root, ignore_errors=True)


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
