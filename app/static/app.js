"use strict";
/* ==========================================================================
   EmbodiedCloud 前端（v0.4 · 多视图 SPA，无构建工具链）
   - 所有用户可控内容渲染前一律 esc() 转义（防存储型 XSS）
   - 破坏性操作一律 confirm 弹窗；昂贵操作 in-flight 防重
   - 工作区瞬态（排队/准备中/停止中）自动轮询，终态即停
   ========================================================================== */

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const TOKEN_KEY = "embodiedcloud.token";
const AGENT_TOKENS_KEY = "embodiedcloud.agentTokens";

const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );

const WS_STATUS_CN = {
  created: "已创建", queued: "排队中", provisioning: "准备中", running: "运行中",
  stopping: "停止中", stopped: "已停止", failed: "失败", deleted: "已删除",
};
const WS_TRANSIENT = new Set(["created", "queued", "provisioning", "stopping"]);
const DEPLOY_STATUS_CN = {
  pending: "待下载", downloading: "下载中", verified: "已校验",
  running: "运行中", success: "成功", failed: "失败",
};
const AGENT_STATUS_CN = { registered: "已注册", online: "在线", offline: "离线" };
const STREAM_STATUS_CN = { starting: "启动中", ready: "就绪", connected: "已连接", disconnected: "已断开", failed: "失败" };
const LEDGER_TYPE_CN = { recharge: "充值", usage: "用量扣费", promotion: "赠送", refund: "退款", adjustment: "调整" };
const GPU_STATUS_CN = { available: "可用", allocated: "已分配", unhealthy: "异常", draining: "暂时缺席", drained: "人工下架" };

const fmtSec = (total) => {
  const t = Math.max(0, Math.floor(total || 0));
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
};
const fmtTime = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString("zh-CN", { hour12: false });
};
const shortId = (id) => (id || "").slice(0, 8);

/* ---------------- 全局状态 ---------------- */
let TOKEN = localStorage.getItem(TOKEN_KEY) || "";
let me = null;                 // UserOut
let templates = [];
let workspaces = [];
let usage = null;
let courses = [];
let currentCourseId = null;
let courseCache = {};          // courseId → {course, labs, progress, members, completions, isTeacher}
let deployments = [];
let agents = [];
let gpus = [];
let gpuHosts = [];
let health = null;
let agentTokens = {};
try { agentTokens = JSON.parse(localStorage.getItem(AGENT_TOKENS_KEY) || "{}"); } catch { agentTokens = {}; }

let pollTimer = null;
let toastTimer = null;
const busy = new Set(); // 防重集合：'create:templateId' / 'stop:id' ...

/* ---------------- API ---------------- */
class ApiError extends Error {
  constructor(status, detail, url) {
    const msg = (typeof detail === "string" && detail) ? detail
      : (detail && (detail.detail || detail.message)) || `HTTP ${status}`;
    super(msg);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.url = url;
  }
}

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  if (TOKEN) headers["Authorization"] = `Bearer ${TOKEN}`;
  if (opts.agentToken) headers["X-Agent-Token"] = opts.agentToken;
  if (opts.headers) Object.assign(headers, opts.headers);
  let r;
  try {
    r = await fetch(path, { ...opts, headers });
  } catch {
    throw new ApiError(0, "网络错误，请检查连接", path);
  }
  if (r.status === 401 && !path.startsWith("/api/auth/")) {
    if (TOKEN) { toast("会话已过期，请重新登录", "error"); doLogout(true); }
    throw new ApiError(401, "unauthorized", path);
  }
  if (!r.ok) {
    let body = null;
    try { body = await r.json(); } catch { /* 非 JSON body */ }
    throw new ApiError(r.status, body, path);
  }
  if (r.status === 204) return null;
  return r.json();
}

/* ---------------- Toast ---------------- */
function toast(msg, kind = "ok") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = `toast ${kind === "error" ? "error" : kind === "ok" ? "ok" : ""}`;
  el.style.display = "block";
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.style.display = "none"; }, kind === "error" ? 6000 : 4000);
}

/* ---------------- Dialog ---------------- */
function openDialog({ title, body, actions = [], onMount }) {
  const root = $("#dialog-root");
  root.innerHTML = "";
  const backdrop = document.createElement("div");
  backdrop.className = "dialog-backdrop";
  backdrop.innerHTML = `
    <div class="dialog" role="dialog" aria-modal="true" aria-label="${esc(title)}">
      <h3>${esc(title)}</h3>
      <div class="dialog-body"></div>
      <div class="dialog-actions"></div>
    </div>`;
  const dialog = backdrop.querySelector(".dialog");
  dialog.querySelector(".dialog-body").append(body);
  const actionsEl = dialog.querySelector(".dialog-actions");
  const close = () => backdrop.remove();
  for (const a of actions) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = a.kind || "secondary";
    btn.textContent = a.label;
    btn.addEventListener("click", () => {
      const result = a.onClick ? a.onClick() : undefined;
      if (result !== false) close();
    });
    actionsEl.append(btn);
  }
  backdrop.addEventListener("click", (e) => { if (e.target === backdrop) close(); });
  document.addEventListener("keydown", function escHandler(e) {
    if (e.key === "Escape") { close(); document.removeEventListener("keydown", escHandler); }
  });
  root.append(backdrop);
  if (onMount) onMount(dialog);
  const first = dialog.querySelector("button, input, select");
  if (first) first.focus();
  return { close };
}

function confirmDialog(title, message, { danger = false, confirmText = "确认" } = {}) {
  return new Promise((resolve) => {
    openDialog({
      title,
      body: Object.assign(document.createElement("div"), { textContent: message }),
      actions: [
        { label: "取消", onClick: () => resolve(false) },
        { label: confirmText, kind: danger ? "danger" : "primary", onClick: () => resolve(true) },
      ],
    });
  });
}

function promptDialog(title, fields) {
  return new Promise((resolve) => {
    const wrap = document.createElement("div");
    const inputs = {};
    for (const f of fields) {
      const label = document.createElement("label");
      label.textContent = f.label + (f.required ? " *" : "");
      const input = document.createElement(f.type === "select" ? "select" : "input");
      if (f.type === "select") {
        for (const opt of f.options || []) {
          const o = document.createElement("option");
          o.value = opt.value; o.textContent = opt.label;
          if (opt.value === f.value) o.selected = true;
          input.append(o);
        }
      } else {
        input.type = f.type || "text";
        input.placeholder = f.placeholder || "";
        input.value = f.value ?? "";
        input.minLength = f.minLength ?? 0;
        input.maxLength = f.maxLength ?? 524288;
      }
      inputs[f.name] = input;
      wrap.append(label, input);
    }
    openDialog({
      title,
      body: wrap,
      actions: [
        { label: "取消", onClick: () => resolve(null) },
        {
          label: "确定", kind: "primary",
          onClick: () => {
            const out = {};
            for (const f of fields) {
              const v = inputs[f.name].value.trim();
              if (f.required && !v) { toast(`${f.label}为必填`, "error"); return false; }
              out[f.name] = v || undefined;
            }
            resolve(out);
          },
        },
      ],
    });
  });
}

/* ---------------- 认证 ---------------- */
function setAuthUI() {
  const logged = Boolean(me);
  $("#auth-form").style.display = logged ? "none" : "flex";
  $("#userbar").hidden = !logged;
  if (logged) $("#auth-user").textContent = `${me.username} · ${me.role === "admin" ? "管理员" : "用户"}`;
  // 视图导航：未登录只开放概览
  $$(".nav-item").forEach((b) => {
    const view = b.dataset.view;
    b.hidden = !logged && view !== "overview";
    if (view === "gpus") b.hidden = !(logged && me.role === "admin");
  });
}

function setAuthMode(mode) {
  const isRegister = mode === "register";
  $("#tab-login").classList.toggle("active", !isRegister);
  $("#tab-register").classList.toggle("active", isRegister);
  $("#tab-login").setAttribute("aria-selected", String(!isRegister));
  $("#tab-register").setAttribute("aria-selected", String(isRegister));
  $("#auth-username").hidden = !isRegister;
  $("#auth-password").autocomplete = isRegister ? "new-password" : "current-password";
  $("#auth-submit").textContent = isRegister ? "注册并登录" : "登录";
}

async function handleAuthSubmit(e) {
  e.preventDefault();
  const isRegister = $("#auth-username").hidden === false;
  const email = $("#auth-email").value.trim();
  const password = $("#auth-password").value;
  const submit = $("#auth-submit");
  submit.disabled = true;
  try {
    const body = isRegister
      ? { email, username: $("#auth-username").value.trim() || email.split("@")[0], password }
      : { email, password };
    const r = await api(`/api/auth/${isRegister ? "register" : "login"}`, { method: "POST", body: JSON.stringify(body) });
    TOKEN = r.token;
    localStorage.setItem(TOKEN_KEY, TOKEN);
    me = r.user;
    setAuthUI();
    toast(isRegister ? `注册成功，欢迎 ${r.user.username}` : `欢迎回来，${r.user.username}`);
    await refreshOverview();
  } catch (err) {
    toast(`登录失败：${err.message}`, "error");
  } finally {
    submit.disabled = false;
  }
}

async function doLogout(silent = false) {
  try { if (TOKEN) await api("/api/auth/logout", { method: "POST" }); } catch { /* 忽略 */ }
  TOKEN = "";
  me = null;
  localStorage.removeItem(TOKEN_KEY);
  stopPolling();
  setAuthUI();
  $("#workspaces").innerHTML = '<div class="empty">登录后管理你的工作区</div>';
  $("#metrics").innerHTML = "";
  if (!silent) toast("已退出登录");
  showView("overview");
}

/* ---------------- 路由 ---------------- */
function currentView() {
  const name = (location.hash || "#/overview").replace(/^#\//, "");
  return ["overview", "usage", "courses", "deploy", "edge", "gpus"].includes(name) ? name : "overview";
}

async function showView(name) {
  if (name !== "overview" && !me) {
    toast("请先登录", "error");
    location.hash = "#/overview";
    return;
  }
  if (name === "gpus" && (!me || me.role !== "admin")) {
    toast("需要管理员权限", "error");
    location.hash = "#/overview";
    return;
  }
  if (location.hash !== `#/${name}`) location.hash = `#/${name}`;
  $$(".view").forEach((v) => { v.hidden = v.dataset.viewName !== name; });
  $$(".nav-item").forEach((b) => {
    const active = b.dataset.view === name;
    b.classList.toggle("active", active);
    if (active) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
  window.scrollTo({ top: 0 });
  if (name === "overview") await refreshOverview();
  else if (name === "usage") await refreshUsage();
  else if (name === "courses") await refreshCourses();
  else if (name === "deploy") await refreshDeploy();
  else if (name === "edge") await refreshEdge();
  else if (name === "gpus") await refreshGpus();
}

/* ---------------- 概览 ---------------- */
async function loadHealth() {
  try {
    const h = await api("/api/health");
    health = h;
    const el = $("#health");
    el.textContent = `${h.provider} · ${h.provider_ready ? "ready" : "not ready"}`;
    el.className = `health ${h.provider_ready ? "ok" : "bad"}`;
    el.title = h.provider_detail;
    const vp = $("#version-pill");
    if (vp && h.version) vp.textContent = `v${h.version}`;
  } catch {
    $("#health").textContent = "API 不可用";
    $("#health").className = "health bad";
  }
}

async function loadMetrics() {
  if (!me) return;
  try {
    usage = await api("/api/usage");
    const u = usage;
    $("#metrics").innerHTML = `
      <div class="metric"><b>${u.running_workspaces}</b><span>运行中工作区</span></div>
      <div class="metric"><b>${u.total_workspaces}</b><span>现存工作区</span></div>
      <div class="metric"><b>${fmtSec(u.accumulated_gpu_seconds)}</b><span>GPU 用量（含运行中）</span></div>
      <div class="metric"><b>${u.credits_balance}</b><span>Credits 余额</span></div>
      <div class="metric"><b>¥${(u.estimated_cost_cny || 0).toFixed(2)}</b><span>估算成本</span></div>`;
  } catch (err) {
    $("#metrics").innerHTML = `<div class="error-block">用量加载失败：${esc(err.message)} <button class="ghost" data-action="load-metrics">重试</button></div>`;
  }
}

const templateById = (id) => templates.find((t) => t.id === id);

async function loadTemplates() {
  const el = $("#templates");
  el.innerHTML = '<div class="loading">模板加载中</div>';
  try {
    templates = await api("/api/templates");
    if (!templates.length) { el.innerHTML = '<div class="empty">暂无可用模板</div>'; return; }
    el.innerHTML = templates.map((t) => {
      const acceptance = (t.metadata_json && t.metadata_json.acceptance) || "";
      const outputs = (t.outputs || []).join(" / ");
      return `
      <article class="card">
        <div class="category">${esc(t.category)}</div>
        <h3>${esc(t.name)}</h3>
        <p>${esc(t.description)}</p>
        <div class="spec">
          <span>${t.gpu_requirement_gb}GB VRAM+</span>
          <span>约 ¥${t.estimated_hourly_cost_cny}/h</span>
          <span>v${esc(t.version)} 锁定</span>
          ${t.requires_streaming ? '<span class="stream">WebRTC 实时仿真</span>' : ""}
        </div>
        ${acceptance ? `<div class="acceptance">验收：${esc(acceptance)}</div>` : ""}
        ${outputs ? `<div class="acceptance">产出：${esc(outputs)}</div>` : ""}
        <div class="command">$ ${esc(t.launch_command)}</div>
        <button class="primary" data-action="create-workspace" data-id="${esc(t.id)}">一键启动</button>
      </article>`;
    }).join("");
  } catch (err) {
    el.innerHTML = `<div class="error-block">模板加载失败：${esc(err.message)} <button class="ghost" data-action="load-templates">重试</button></div>`;
  }
}

function workspaceActions(w) {
  const tpl = templateById(w.template_id);
  const transient = WS_TRANSIENT.has(w.status);
  const running = w.status === "running";
  const actions = [];
  if (running && w.ide_url) {
    actions.push(`<button class="primary" data-action="open-ide" data-id="${w.id}">打开 IDE</button>`);
  }
  if (running && tpl && tpl.requires_streaming) {
    actions.push(`<button class="secondary" data-action="stream-panel" data-id="${w.id}">流会话</button>`);
  }
  actions.push(`<button class="secondary" data-action="show-logs" data-id="${w.id}">日志</button>`);
  if (transient) {
    actions.push('<span class="muted"><span class="spinner"></span>进行中…</span>');
  } else if (running) {
    actions.push(`<button class="secondary" data-action="stop-workspace" data-id="${w.id}">停止</button>`);
  } else {
    actions.push(`<button class="secondary" data-action="start-workspace" data-id="${w.id}">启动</button>`);
  }
  actions.push(`<button class="danger" data-action="delete-workspace" data-id="${w.id}">删除</button>`);
  return actions.join("");
}

async function loadWorkspaces() {
  const el = $("#workspaces");
  if (!me) { el.innerHTML = '<div class="empty">登录后管理你的工作区</div>'; return; }
  try {
    workspaces = await api("/api/workspaces");
    $("#workspace-count").textContent = workspaces.length ? `${workspaces.length} 个` : "";
    if (!workspaces.length) { el.innerHTML = '<div class="empty">还没有工作区。上面选一个模板直接启动。</div>'; ensurePolling(); return; }
    el.innerHTML = workspaces.map((w) => {
      const tpl = templateById(w.template_id);
      // 已入账的那一段由服务端否定 live（`usage_segment_booked`）：destroy 失败窗口里
      // 账本已有这一行，再按 started_at 加一次就是两倍（N-64 的第三个读者）。
      const live = w.status === "running" && w.started_at && !w.usage_segment_booked
        ? Math.max(0, Math.floor((Date.now() - new Date(w.started_at).getTime()) / 1000))
        : 0;
      return `
      <article class="workspace">
        <div>
          <h3>${esc(w.name)}</h3>
          <div class="meta">
            <span class="badge ${esc(w.status)}">${WS_STATUS_CN[w.status] || esc(w.status)}</span>
            <span>${esc(tpl ? tpl.name : w.template_id)}</span>
            <span>${esc(w.gpu_name || "等待 GPU")}</span>
            <span>#${shortId(w.id)}</span>
          </div>
        </div>
        ${w.error_message ? `<div class="error-block">${esc(w.error_message)}</div>` : ""}
        <div class="time-row">
          <span>已用 ${fmtSec(w.accumulated_seconds + (live || 0))}</span>
          <span>创建于 ${fmtTime(w.created_at)}</span>
        </div>
        <div class="actions">${workspaceActions(w)}</div>
        <div class="stream-slot" data-stream-slot="${w.id}" hidden></div>
      </article>`;
    }).join("");
    ensurePolling();
  } catch (err) {
    el.innerHTML = `<div class="error-block">工作区加载失败：${esc(err.message)} <button class="ghost" data-action="load-workspaces">重试</button></div>`;
  }
}

async function refreshOverview() {
  await Promise.allSettled([loadHealth(), loadTemplates()]);
  if (me) await Promise.allSettled([loadMetrics(), loadWorkspaces()]);
}

async function createWorkspace(templateId) {
  if (!me) { toast("请先登录再创建", "error"); return; }
  if (busy.has(`create:${templateId}`)) return;
  const tpl = templateById(templateId);
  const result = await promptDialog("创建工作区", [
    { name: "name", label: "工作区名称（可选）", placeholder: `默认：${tpl ? tpl.name : ""} · 随机后缀`, maxLength: 120 },
  ]);
  if (result === null) return;
  busy.add(`create:${templateId}`);
  toast("正在创建工作区…");
  try {
    await api("/api/workspaces", {
      method: "POST",
      body: JSON.stringify({ template_id: templateId, name: result.name || undefined, auto_start: true }),
    });
    await Promise.allSettled([loadWorkspaces(), loadMetrics()]);
    ensurePolling();
    toast("工作区已创建，正在准备中…");
  } catch (err) {
    toast(`创建失败：${err.message}`, "error");
  } finally {
    busy.delete(`create:${templateId}`);
  }
}

async function openIDE(id) {
  try {
    const a = await api(`/api/workspaces/${id}/access`);
    if (!a.ide_url) { toast("IDE 尚未就绪", "error"); return; }
    if (a.status !== "running") { toast(`工作区当前为「${WS_STATUS_CN[a.status] || a.status}」，请先启动`, "error"); return; }
    const body = document.createElement("div");
    if (a.ide_password) {
      body.innerHTML = `<p>IDE 地址：<code style="word-break:break-all">${esc(a.ide_url)}</code></p>
        <p>密码（点「复制密码」复制到剪贴板）：</p>
        <div class="token-box" id="ide-password-box">${esc(a.ide_password)}</div>`;
      openDialog({
        title: "打开 IDE",
        body,
        actions: [
          { label: "复制密码", onClick: async () => {
              try { await navigator.clipboard.writeText(a.ide_password); toast("密码已复制到剪贴板"); } catch { toast("复制失败，请手动选择复制", "error"); }
              return false; // 不关闭
          } },
          { label: "打开 IDE", kind: "primary", onClick: () => { window.open(a.ide_url, "_blank", "noopener"); } },
          { label: "关闭" },
        ],
      });
    } else {
      window.open(a.ide_url, "_blank", "noopener");
    }
  } catch (err) { toast(err.message, "error"); }
}

/* ---- 流会话 ---- */
async function streamPanel(id) {
  const slot = $(`[data-stream-slot="${id}"]`);
  const isOpen = !slot.hidden;
  slot.hidden = isOpen;
  if (isOpen) return;
  slot.innerHTML = '<div class="loading">会话加载中</div>';
  try {
    const sessions = await api(`/api/streaming/workspace/${id}`);
    renderStreamSessions(id, sessions);
  } catch (err) {
    slot.innerHTML = `<div class="error-block">${esc(err.message)}</div>`;
  }
}

function renderStreamSessions(id, sessions) {
  const slot = $(`[data-stream-slot="${id}"]`);
  slot.innerHTML = `
    <div class="section-title"><h3>流会话</h3>
      <button class="secondary small" data-action="stream-start" data-id="${id}">新建会话</button>
    </div>
    ${sessions.length ? `<div class="stream-list">${sessions.map((s) => `
      <div class="stream-row">
        <span class="badge ${esc(s.status)}">${STREAM_STATUS_CN[s.status] || esc(s.status)}</span>
        <span class="muted">#${shortId(s.id)} · 端口 ${s.signal_port ?? "—"}/${s.media_port ?? "—"}</span>
        ${s.error_message ? `<span class="muted">· ${esc(s.error_message)}</span>` : ""}
        <span class="actions">
          ${s.status !== "connected" ? `<button class="secondary small" data-action="stream-connect" data-sid="${s.id}">连接</button>` : ""}
          ${s.status === "connected" ? `<button class="secondary small" data-action="stream-disconnect" data-sid="${s.id}">断开</button>` : ""}
          ${s.status === "disconnected" ? `<button class="secondary small" data-action="stream-reconnect" data-sid="${s.id}">重连</button>` : ""}
        </span>
      </div>`).join("")}</div>`
    : '<div class="empty">暂无会话。点击「新建会话」开始。</div>'}`;
}

async function startStream(id) {
  try {
    const s = await api(`/api/streaming/${id}/start`, { method: "POST" });
    toast("流会话已创建");
    const slot = $(`[data-stream-slot="${id}"]`);
    slot.hidden = false;
    const sessions = await api(`/api/streaming/workspace/${id}`);
    renderStreamSessions(id, sessions);
  } catch (err) { toast(err.message, "error"); }
}

async function streamTransition(action, sid) {
  try {
    await api(`/api/streaming/sessions/${sid}/${action}`, { method: "POST" });
    const slot = $$(".stream-slot").find((el) => !el.hidden);
    toast("操作成功");
    if (slot) {
      const id = slot.dataset.streamSlot;
      const sessions = await api(`/api/streaming/workspace/${id}`);
      renderStreamSessions(id, sessions);
    }
  } catch (err) { toast(err.message, "error"); }
}

/* ---- 日志 ---- */
async function showLogs(id) {
  let w = workspaces.find((x) => x.id === id);
  try {
    const r = await api(`/api/workspaces/${id}/logs?tail=500`);
    const pre = document.createElement("pre");
    pre.textContent = r.logs || "(暂无 runtime 日志)";
    const body = document.createElement("div");
    body.append(pre);
    openDialog({
      title: `日志 · ${w ? w.name : shortId(id)}`,
      body,
      actions: [
        { label: "刷新", onClick: async () => {
            const again = await api(`/api/workspaces/${id}/logs?tail=500`);
            pre.textContent = again.logs || "(暂无 runtime 日志)";
            return false;
        } },
        { label: "关闭" },
      ],
    });
  } catch (err) { toast(`日志获取失败：${err.message}`, "error"); }
}

/* ---- 生命周期 ---- */
async function stopWorkspace(id) {
  const w = workspaces.find((x) => x.id === id);
  if (!await confirmDialog("停止工作区", `停止「${w ? w.name : id}」将结束本次运行并结算用量（GPU 会释放，代码与数据保留）。`, { danger: true, confirmText: "停止" })) return;
  if (busy.has(`stop:${id}`)) return;
  busy.add(`stop:${id}`);
  try {
    await api(`/api/workspaces/${id}/stop`, { method: "POST" });
    toast("已发出停止请求");
    await Promise.allSettled([loadWorkspaces(), loadMetrics()]);
    ensurePolling();
  } catch (err) { toast(err.message, "error"); }
  finally { busy.delete(`stop:${id}`); }
}

async function startWorkspace(id) {
  if (busy.has(`start:${id}`)) return;
  busy.add(`start:${id}`);
  toast("正在启动工作区…");
  try {
    await api(`/api/workspaces/${id}/start`, { method: "POST" });
    await Promise.allSettled([loadWorkspaces(), loadMetrics()]);
    ensurePolling();
  } catch (err) { toast(err.message, "error"); }
  finally { busy.delete(`start:${id}`); }
}

async function deleteWorkspace(id) {
  const w = workspaces.find((x) => x.id === id);
  if (!await confirmDialog("删除工作区", `删除「${w ? w.name : id}」会永久清理运行时并释放 GPU，累计用量会计入账单，且删除后无法恢复。确认删除？`, { danger: true, confirmText: "永久删除" })) return;
  if (busy.has(`delete:${id}`)) return;
  busy.add(`delete:${id}`);
  try {
    await api(`/api/workspaces/${id}`, { method: "DELETE" });
    toast("已删除");
    await Promise.allSettled([loadWorkspaces(), loadMetrics()]);
    ensurePolling();
  } catch (err) { toast(err.message, "error"); }
  finally { busy.delete(`delete:${id}`); }
}

/* ---------------- 轮询 ---------------- */
function hasTransient() {
  return workspaces.some((w) => WS_TRANSIENT.has(w.status));
}

function ensurePolling() {
  if (!me) return stopPolling();
  if (hasTransient()) {
    if (!pollTimer) pollTimer = setInterval(pollTick, 2000);
  } else {
    stopPolling();
  }
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

async function pollTick() {
  if (document.hidden) return; // 页面不可见时暂停，避免无谓请求
  if (!hasTransient()) { stopPolling(); return; }
  try {
    await Promise.allSettled([loadWorkspaces(), loadMetrics()]);
  } catch { /* 下轮重试 */ }
}

/* ---------------- 用量与账单 ---------------- */
async function refreshUsage() {
  const el = $("#usage-metrics");
  el.innerHTML = '<div class="loading">加载中</div>';
  $("#ledger").innerHTML = '<div class="loading">加载中</div>';
  $("#usage-workspaces").innerHTML = "";
  try {
    usage = await api("/api/usage");
    const u = usage;
    el.innerHTML = `
      <div class="metric"><b>${u.running_workspaces}</b><span>运行中工作区</span></div>
      <div class="metric"><b>${u.total_workspaces}</b><span>现存工作区</span></div>
      <div class="metric"><b>${fmtSec(u.accumulated_gpu_seconds)}</b><span>GPU 用量（含运行中）</span></div>
      <div class="metric"><b>${u.credits_balance}</b><span>Credits 余额</span></div>
      <div class="metric"><b>¥${(u.estimated_cost_cny || 0).toFixed(2)}</b><span>估算成本</span></div>`;
    $("#usage-note").textContent =
      "口径说明：账单以不可变账本为准，1 credit = 1 GPU 秒；估算成本按模板标价（¥/h）换算，仅供直观参考。工作区停止时才结算本次运行段。";
    await Promise.allSettled([loadLedger(), loadUsageWorkspaces()]);
  } catch (err) {
    el.innerHTML = `<div class="error-block">用量加载失败：${esc(err.message)} <button class="ghost" data-action="refresh-usage">重试</button></div>`;
  }
}

async function loadLedger() {
  const el = $("#ledger");
  try {
    const entries = await api("/api/ledger");
    if (!entries.length) { el.innerHTML = '<div class="empty">暂无账本记录</div>'; return; }
    el.innerHTML = `<table>
      <thead><tr><th>时间</th><th>类型</th><th>说明</th><th class="num">GPU 秒</th><th class="num">金额</th><th>关联</th></tr></thead>
      <tbody>${entries.map((e) => `
        <tr>
          <td>${fmtTime(e.created_at)}</td>
          <td>${LEDGER_TYPE_CN[e.type] || esc(e.type)}</td>
          <td>${esc(e.description)}</td>
          <td class="num">${e.gpu_seconds ?? "—"}</td>
          <td class="num ${e.amount >= 0 ? "amount-pos" : "amount-neg"}">${e.amount >= 0 ? "+" : ""}${e.amount}</td>
          <td class="muted">${e.workspace_id ? `#${shortId(e.workspace_id)}` : "—"}</td>
        </tr>`).join("")}</tbody></table>`;
  } catch (err) {
    el.innerHTML = `<div class="error-block">账本加载失败：${esc(err.message)} <button class="ghost" data-action="refresh-usage">重试</button></div>`;
  }
}

async function loadUsageWorkspaces() {
  const el = $("#usage-workspaces");
  try {
    if (!workspaces.length) await loadWorkspaces();
    if (!workspaces.length) { el.innerHTML = '<div class="empty">暂无工作区</div>'; return; }
    el.innerHTML = `<table>
      <thead><tr><th>工作区</th><th>模板</th><th>状态</th><th class="num">已结算 GPU 秒</th><th class="num">估算 ¥</th></tr></thead>
      <tbody>${workspaces.map((w) => {
        const tpl = templateById(w.template_id);
        const rate = tpl ? tpl.estimated_hourly_cost_cny : 0;
        const live = w.status === "running" && w.started_at && !w.usage_segment_booked
          ? Math.max(0, Math.floor((Date.now() - new Date(w.started_at).getTime()) / 1000))
          : 0;
        const seconds = (w.accumulated_seconds || 0) + live;
        return `<tr>
          <td>${esc(w.name)} <span class="muted">#${shortId(w.id)}</span></td>
          <td>${esc(tpl ? tpl.name : w.template_id)}</td>
          <td><span class="badge ${esc(w.status)}">${WS_STATUS_CN[w.status] || esc(w.status)}</span></td>
          <td class="num">${seconds}</td>
          <td class="num">¥${((seconds / 3600) * rate).toFixed(2)}</td>
        </tr>`;
      }).join("")}</tbody></table>`;
  } catch (err) {
    el.innerHTML = `<div class="error-block">明细加载失败：${esc(err.message)}</div>`;
  }
}

async function handleRecharge(e) {
  e.preventDefault();
  const amount = parseInt($("#recharge-amount").value, 10);
  if (!amount || amount <= 0) { toast("请输入正整数金额", "error"); return; }
  try {
    await api("/api/ledger/recharge", { method: "POST", body: JSON.stringify({ amount }) });
    toast(`已充值 ${amount} credits（演示语义）`);
    $("#recharge-amount").value = "";
    await Promise.allSettled([refreshUsage()]);
  } catch (err) { toast(err.message, "error"); }
}

/* ---------------- 课程 ---------------- */
async function refreshCourses() {
  await loadCourseList();
  if (currentCourseId) await selectCourse(currentCourseId);
}

async function loadCourseList() {
  const el = $("#course-list");
  el.innerHTML = '<div class="loading">加载中</div>';
  try {
    courses = await api("/api/courses");
    if (!courses.length) { el.innerHTML = '<div class="empty">还没有课程。凭邀请码加入，或创建自己的课程。</div>'; return; }
    el.innerHTML = courses.map((c) => `
      <button class="course-item ${c.id === currentCourseId ? "active" : ""}" data-action="select-course" data-id="${esc(c.id)}">
        ${esc(c.name)}<br /><span class="role-tag">${esc(c.slug)}</span>
      </button>`).join("");
  } catch (err) {
    el.innerHTML = `<div class="error-block">课程加载失败：${esc(err.message)}</div>`;
  }
}

async function selectCourse(courseId) {
  currentCourseId = courseId;
  await loadCourseList();
  const detail = $("#course-detail");
  const empty = $("#course-empty");
  detail.hidden = false;
  empty.hidden = true;
  $("#course-title").textContent = "加载中…";
  try {
    const course = await api(`/api/courses/${courseId}`);
    courseCache[courseId] = { course, labs: [], progress: [], members: null, completions: null, isTeacher: false };
    $("#course-title").textContent = course.name;
    $("#course-slug-pill").textContent = course.slug;
    $("#course-desc").textContent = course.description || "（无简介）";
    const roleEl = $("#course-role");
    roleEl.textContent = course.owner_id === me.id ? "角色：课程创建者（教师）" : "";
    await Promise.allSettled([
      loadCourseLabs(courseId),
      loadCourseProgress(courseId),
      loadCourseMembers(courseId),
    ]);
  } catch (err) {
    detail.hidden = true;
    empty.hidden = false;
    empty.innerHTML = `<div class="error-block">${esc(err.message)}</div>`;
  }
}

async function loadCourseLabs(courseId) {
  const block = $("#course-labs-block");
  try {
    const labs = await api(`/api/courses/${courseId}/labs`);
    const cache = courseCache[courseId];
    cache.labs = labs;
    const isTeacher = cache.isTeacher;
    block.innerHTML = `<div class="lab-block">
      <div class="section-title"><h3>实验（${labs.length}）</h3>
      ${isTeacher ? `<button class="secondary small" data-action="create-lab" data-id="${esc(courseId)}">新建实验</button>` : ""}
      </div>
      ${labs.length ? `<div class="progress-grid">${labs.map((lab) => `
        <div class="progress-cell">
          <b>${esc(lab.name)}</b>
          <div class="muted">配额 ${fmtSec(lab.quota_seconds)} · 模板 ${esc(lab.template_id)}</div>
          ${esc(lab.description) ? `<div class="muted">${esc(lab.description)}</div>` : ""}
          <div class="actions">
            <button class="secondary small" data-action="launch-lab" data-id="${esc(lab.id)}">一键启动实验</button>
            <button class="ghost small" data-action="list-assignments" data-id="${esc(lab.id)}" data-name="${esc(lab.name)}">作业</button>
            ${isTeacher ? `<button class="ghost small" data-action="create-assignment" data-id="${esc(lab.id)}">＋作业</button>` : ""}
          </div>
          <div class="assign-slot" data-assign-slot="${esc(lab.id)}" hidden></div>
        </div>`).join("")}</div>` : '<div class="empty">暂无实验</div>'}
    </div>`;
  } catch (err) {
    block.innerHTML = `<div class="lab-block"><div class="error-block">实验加载失败：${esc(err.message)}</div></div>`;
  }
}

async function loadCourseProgress(courseId) {
  const block = $("#course-progress-block");
  try {
    const progress = await api(`/api/courses/${courseId}/my-progress`);
    const cache = courseCache[courseId];
    cache.progress = progress;
    if (!progress.length) { block.innerHTML = ""; return; }
    const done = progress.filter((p) => p.status === "completed").length;
    block.innerHTML = `<div class="lab-block">
      <div class="section-title"><h3>我的进度</h3><span>已完成 ${done}/${progress.length}</span></div>
      <div class="progress-grid">${progress.map((p) => `
        <div class="progress-cell">
          <b>${esc(p.assignment_name)}</b>
          <div class="muted">${esc(p.lab_name)}${p.due_at ? ` · 截止 ${fmtTime(p.due_at)}` : ""}</div>
          <span class="badge ${p.status === "completed" ? "success" : "queued"}">${p.status === "completed" ? "已提交" : "未提交"}</span>
          ${p.completed_at ? `<div class="muted">提交于 ${fmtTime(p.completed_at)}</div>` : ""}
        </div>`).join("")}</div>
    </div>`;
  } catch {
    block.innerHTML = ""; // 非成员或加载失败：静默（course 详情已可见则基本可见）
  }
}

async function loadCourseMembers(courseId) {
  const block = $("#course-members-block");
  const cache = courseCache[courseId];
  try {
    const members = await api(`/api/courses/${courseId}/members`);
    cache.members = members;
    cache.isTeacher = true;
    block.innerHTML = `<div class="member-block">
      <div class="section-title"><h3>成员（${members.length}）</h3></div>
      <form id="add-member-form" class="inline-form">
        <input type="text" id="add-member-user" placeholder="用户 ID" aria-label="用户 ID" required />
        <select id="add-member-role" aria-label="成员角色">
          <option value="student">学生</option>
          <option value="instructor">教师</option>
          <option value="org_admin">组织管理员</option>
        </select>
        <button type="submit" class="primary small">添加成员</button>
      </form>
      <div class="table-wrap" style="margin-top:10px">
        <table><thead><tr><th>用户</th><th>角色</th></tr></thead>
        <tbody>${members.map((m) => `<tr><td>${esc(m.user_id)}</td><td>${esc(m.role)}</td></tr>`).join("")}</tbody></table>
      </div>
    </div>`;
    $("#add-member-form").addEventListener("submit", (e) => handleAddMember(e, courseId));
  } catch {
    cache.isTeacher = false;
    block.innerHTML = "";
  }
  if (cache.isTeacher) await loadCourseCompletions(courseId);
  else $("#course-completions-block").innerHTML = "";
}

async function loadCourseCompletions(courseId) {
  const block = $("#course-completions-block");
  try {
    const completions = await api(`/api/courses/${courseId}/completions`);
    if (!completions.length) { block.innerHTML = ""; return; }
    block.innerHTML = `<div class="member-block">
      <div class="section-title"><h3>全班完成情况</h3></div>
      <div class="table-wrap"><table>
        <thead><tr><th>实验</th><th>作业</th><th>学生</th><th>状态</th></tr></thead>
        <tbody>${completions.map((c) => c.submissions.map((s) => `
          <tr>
            <td>${esc(c.lab_name)}</td>
            <td>${esc(c.assignment_name)}</td>
            <td>${esc(s.username)}</td>
            <td><span class="badge ${s.status === "completed" ? "success" : "queued"}">${s.status === "completed" ? "已提交" : "未提交"}</span></td>
          </tr>`).join("")).join("")}</tbody>
      </table></div>
    </div>`;
  } catch { block.innerHTML = ""; }
}

async function handleJoinCourse(e) {
  e.preventDefault();
  const slug = $("#join-slug").value.trim();
  try {
    const m = await api("/api/courses/join-by-slug", { method: "POST", body: JSON.stringify({ slug }) });
    toast(`已加入课程 #${shortId(m.course_id)}`);
    $("#join-slug").value = "";
    await loadCourseList();
    await selectCourse(m.course_id);
  } catch (err) { toast(`加入失败：${err.message}`, "error"); }
}

async function handleCreateCourse(e) {
  e.preventDefault();
  const payload = {
    name: $("#course-name").value.trim(),
    slug: $("#course-slug").value.trim(),
    description: $("#course-desc").value.trim(),
  };
  try {
    const c = await api("/api/courses", { method: "POST", body: JSON.stringify(payload) });
    toast(`课程「${c.name}」已创建，你已成为教师`);
    $("#create-course-form").reset();
    await loadCourseList();
    await selectCourse(c.id);
  } catch (err) { toast(`创建失败：${err.message}`, "error"); }
}

async function createLab(courseId) {
  const result = await promptDialog("新建实验", [
    { name: "name", label: "实验名称", required: true, maxLength: 120 },
    { name: "template_id", label: "模板", type: "select", required: true, options: templates.map((t) => ({ value: t.id, label: t.name })) },
    { name: "quota_seconds", label: "学生配额（GPU 秒）", type: "number", value: "3600" },
  ]);
  if (!result) return;
  try {
    await api(`/api/courses/${courseId}/labs`, {
      method: "POST",
      body: JSON.stringify({ name: result.name, template_id: result.template_id, quota_seconds: parseInt(result.quota_seconds, 10) || 3600 }),
    });
    toast("实验已创建");
    await loadCourseLabs(courseId);
  } catch (err) { toast(err.message, "error"); }
}

async function createAssignment(labId) {
  const result = await promptDialog("新建作业", [
    { name: "name", label: "作业名称", required: true, maxLength: 120 },
    { name: "description", label: "要求说明（可选）" },
  ]);
  if (!result) return;
  try {
    await api(`/api/labs/${labId}/assignments`, { method: "POST", body: JSON.stringify(result) });
    toast("作业已创建");
    await loadCourseLabs(currentCourseId);
  } catch (err) { toast(err.message, "error"); }
}

async function launchLab(labId) {
  if (!await confirmDialog("启动实验", "将按该实验的模板创建并启动一个工作区（受额度/配额门禁约束）。确认启动？", { confirmText: "启动" })) return;
  try {
    const ws = await api(`/api/labs/${labId}/launch`, { method: "POST" });
    toast("实验工作区已创建，正在准备…");
    await Promise.allSettled([loadWorkspaces(), loadMetrics()]);
    ensurePolling();
    location.hash = "#/overview";
    setTimeout(() => toast(`工作区 #${shortId(ws.id)} 准备中`), 400);
  } catch (err) { toast(err.message, "error"); }
}

async function listAssignments(labId, labName) {
  const slot = $(`[data-assign-slot="${labId}"]`);
  const isOpen = !slot.hidden;
  slot.hidden = isOpen;
  if (isOpen) return;
  slot.innerHTML = '<div class="loading">加载中</div>';
  try {
    const assignments = await api(`/api/labs/${labId}/assignments`);
    const cache = courseCache[currentCourseId] || { progress: [] };
    const progress = Object.fromEntries(cache.progress.map((p) => [p.assignment_id, p]));
    slot.innerHTML = assignments.length ? `<div class="assign-list">${assignments.map((a) => {
      const p = progress[a.id];
      return `<div class="progress-cell">
        <b>${esc(a.name)}</b>
        ${a.due_at ? `<div class="due">截止 ${fmtTime(a.due_at)}</div>` : ""}
        <span class="badge ${p && p.status === "completed" ? "success" : "queued"}">${p && p.status === "completed" ? "已提交" : "未提交"}</span>
        ${p && p.status !== "completed" ? `<button class="secondary small" data-action="submit-assignment" data-id="${esc(a.id)}">提交作业</button>` : ""}
        ${!p ? `<button class="secondary small" data-action="submit-assignment" data-id="${esc(a.id)}">提交作业</button>` : ""}
      </div>`;
    }).join("")}</div>` : '<div class="empty">暂无作业</div>';
  } catch (err) {
    slot.innerHTML = `<div class="error-block">${esc(err.message)}</div>`;
  }
}

async function submitAssignment(assignmentId) {
  const options = [{ value: "", label: "不关联工作区" }, ...workspaces.filter((w) => !WS_TRANSIENT.has(w.status) || w.status === "running").map((w) => ({ value: w.id, label: `${w.name} (#${shortId(w.id)})` }))];
  const result = await promptDialog("提交作业", [
    { name: "workspace_id", label: "关联工作区（可选）", type: "select", options },
  ]);
  if (result === null) return;
  try {
    await api(`/api/assignments/${assignmentId}/submit`, {
      method: "POST",
      body: JSON.stringify({ workspace_id: result.workspace_id || null }),
    });
    toast("已提交作业");
    await loadCourseProgress(currentCourseId);
    if (courseCache[currentCourseId] && courseCache[currentCourseId].isTeacher) await loadCourseCompletions(currentCourseId);
    // 刷新打开的作业列表
    $$(".assign-slot:not([hidden])").forEach((slot) => {
      const labId = slot.dataset.assignSlot;
      listAssignments(labId);
    });
  } catch (err) { toast(err.message, "error"); }
}

async function handleAddMember(e, courseId) {
  e.preventDefault();
  const userId = $("#add-member-user").value.trim();
  const role = $("#add-member-role").value;
  try {
    await api(`/api/courses/${courseId}/members`, { method: "POST", body: JSON.stringify({ user_id: userId, role }) });
    toast("成员已添加");
    await loadCourseMembers(courseId);
  } catch (err) { toast(err.message, "error"); }
}

/* ---------------- 部署 · Sim2Real ---------------- */
async function refreshDeploy() {
  const select = $("#deploy-workspace");
  const btn = $("#deploy-demo-checkpoint");
  btn.hidden = !(health && health.provider === "mock");
  try {
    const ws = await api("/api/workspaces");
    workspaces = ws;
    select.innerHTML = ws.length
      ? ws.map((w) => `<option value="${esc(w.id)}">${esc(w.name)} (#${shortId(w.id)})</option>`).join("")
      : '<option value="">（暂无工作区）</option>';
    await loadDeployments();
  } catch (err) {
    $("#deployments").innerHTML = `<div class="error-block">加载失败：${esc(err.message)}</div>`;
  }
}

async function loadDeployments() {
  const el = $("#deployments");
  try {
    deployments = await api("/api/deployments");
    if (!deployments.length) { el.innerHTML = '<div class="empty">暂无部署记录。选择工作区与 checkpoint 路径创建第一个部署。</div>'; return; }
    el.innerHTML = `<table>
      <thead><tr><th>工作区</th><th>机器人</th><th>模型版本</th><th>状态</th><th>校验码</th><th>创建时间</th><th>操作</th></tr></thead>
      <tbody>${deployments.map((d) => {
        const w = workspaces.find((x) => x.id === d.workspace_id);
        let ops = "";
        if (d.status === "pending") ops = `<button class="secondary small" data-action="deploy-download" data-id="${d.id}">下载</button>`;
        else if (d.status === "downloading") ops = `<button class="secondary small" data-action="deploy-verify" data-id="${d.id}">服务端校验</button><span class="muted">（真机端由 Edge Agent 上报 sha256）</span>`;
        else if (d.status === "verified") ops = `<button class="primary small" data-action="deploy-run" data-id="${d.id}">运行</button>`;
        else if (d.status === "running") ops = `<button class="secondary small" data-action="deploy-complete" data-id="${d.id}" data-ok="1">完成·成功</button><button class="danger small" data-action="deploy-complete" data-id="${d.id}" data-ok="0">完成·失败</button>`;
        return `<tr>
          <td>${esc(w ? w.name : shortId(d.workspace_id))}</td>
          <td>${esc(d.robot_type)}</td>
          <td>${esc(d.model_version)}<span class="muted"> / tpl ${esc(d.template_version)}</span></td>
          <td><span class="badge ${esc(d.status)}">${DEPLOY_STATUS_CN[d.status] || esc(d.status)}</span>${d.error_message ? `<div class="muted" style="margin-top:4px">${esc(d.error_message)}</div>` : ""}</td>
          <td class="checksum">${esc(d.checksum).slice(0, 16)}…</td>
          <td>${fmtTime(d.created_at)}</td>
          <td><div class="actions-cell">${ops}</div></td>
        </tr>`;
      }).join("")}</tbody></table>`;
  } catch (err) {
    el.innerHTML = `<div class="error-block">部署记录加载失败：${esc(err.message)} <button class="ghost" data-action="refresh-deploy">重试</button></div>`;
  }
}

async function handleDeploySubmit(e) {
  e.preventDefault();
  const workspace_id = $("#deploy-workspace").value;
  const artifact_path = $("#deploy-path").value.trim();
  const robot_type = $("#deploy-robot").value;
  if (!workspace_id) { toast("请先创建工作区", "error"); return; }
  try {
    const d = await api("/api/deployments", {
      method: "POST",
      body: JSON.stringify({ workspace_id, artifact_path, robot_type }),
    });
    toast(`部署已创建：checksum ${d.checksum.slice(0, 16)}…`);
    await loadDeployments();
  } catch (err) { toast(err.message, "error"); }
}

async function createDemoCheckpoint() {
  const workspace_id = $("#deploy-workspace").value;
  if (!workspace_id) { toast("请先选择工作区", "error"); return; }
  try {
    const ckpt = await api(`/api/workspaces/${workspace_id}/demo-checkpoint`, { method: "POST" });
    $("#deploy-path").value = ckpt.path;
    toast(`演示 checkpoint 已生成：${ckpt.path}（sha256 ${ckpt.sha256.slice(0, 12)}…）`);
  } catch (err) { toast(err.message, "error"); }
}

async function deployDownload(id) {
  try {
    await api(`/api/deployments/${id}/download`, { method: "POST" });
    toast("已进入下载中；真机端 Edge Agent 下载后会上报 sha256 校验");
    await loadDeployments();
  } catch (err) { toast(err.message, "error"); }
}

async function deployVerify(id) {
  try {
    const d = await api(`/api/deployments/${id}/verify`, { method: "POST" });
    toast(d.status === "verified" ? "校验通过" : `校验失败：${d.error_message || ""}`, d.status === "verified" ? "ok" : "error");
    await loadDeployments();
  } catch (err) { toast(err.message, "error"); }
}

async function deployRun(id) {
  let options = [{ value: "", label: "不绑定（模拟执行）" }];
  try {
    agents = await api("/api/edge/agents");
    options = options.concat(agents.map((a) => ({ value: a.id, label: `${a.name} (#${shortId(a.id)})` })));
  } catch { /* agents 可选 */ }
  const result = await promptDialog("运行部署", [
    { name: "edge_agent_id", label: "绑定 Edge Agent（可选）", type: "select", options },
  ]);
  if (result === null) return;
  try {
    await api(`/api/deployments/${id}/run`, {
      method: "POST",
      body: JSON.stringify({ edge_agent_id: result.edge_agent_id || null }),
    });
    toast("部署已进入运行中");
    await loadDeployments();
  } catch (err) { toast(err.message, "error"); }
}

async function deployComplete(id, ok) {
  const okBool = ok === "1";
  if (okBool) {
    if (!await confirmDialog("确认部署成功", "将部署标记为成功（终态，不可再变更）。确认？", { confirmText: "标记成功" })) return;
  } else {
    if (!await confirmDialog("确认部署失败", "将部署标记为失败（终态，不可再变更）。确认？", { danger: true, confirmText: "标记失败" })) return;
  }
  try {
    await api(`/api/deployments/${id}/complete`, {
      method: "POST",
      body: JSON.stringify({ success: okBool, error_message: okBool ? "" : "marked failed by operator" }),
    });
    toast(okBool ? "部署成功 🎉（真机验证需真实硬件）" : "部署已标记失败");
    await loadDeployments();
  } catch (err) { toast(err.message, "error"); }
}

/* ---------------- 边缘设备 ---------------- */
async function refreshEdge() {
  await loadAgents();
}

async function loadAgents() {
  const el = $("#agents");
  try {
    agents = await api("/api/edge/agents");
    if (!agents.length) { el.innerHTML = '<div class="empty">还没有设备。注册一台边缘设备（如实验室的 Franka 工控机）。</div>'; return; }
    el.innerHTML = `<table>
      <thead><tr><th>设备</th><th>状态</th><th>最近心跳</th><th>设备信息</th><th>操作</th></tr></thead>
      <tbody>${agents.map((a) => `
        <tr>
          <td>${esc(a.name)} <span class="muted">#${shortId(a.id)}</span></td>
          <td><span class="badge ${esc(a.status)}">${AGENT_STATUS_CN[a.status] || esc(a.status)}</span></td>
          <td>${fmtTime(a.last_heartbeat)}</td>
          <td class="checksum">${esc(JSON.stringify(a.device_info || {}))}</td>
          <td><div class="actions-cell">
            <button class="secondary small" data-action="agent-heartbeat" data-id="${esc(a.id)}" ${agentTokens[a.id] ? "" : "disabled title='token 仅注册时返回一次；本浏览器未保存，无法代发心跳'"}>心跳</button>
            <button class="secondary small" data-action="agent-telemetry" data-id="${esc(a.id)}" ${agentTokens[a.id] ? "" : "disabled"}>遥测</button>
          </div></td>
        </tr>`).join("")}</tbody></table>`;
  } catch (err) {
    el.innerHTML = `<div class="error-block">设备加载失败：${esc(err.message)} <button class="ghost" data-action="refresh-edge">重试</button></div>`;
  }
}

async function handleAgentSubmit(e) {
  e.preventDefault();
  const name = $("#agent-name").value.trim();
  let deviceInfo = {};
  const raw = $("#agent-device").value.trim();
  if (raw) {
    try { deviceInfo = JSON.parse(raw); }
    catch { toast("设备信息不是合法 JSON", "error"); return; }
  }
  try {
    const r = await api("/api/edge/agents/register", { method: "POST", body: JSON.stringify({ name, device_info: deviceInfo }) });
    agentTokens[r.agent.id] = r.token;
    localStorage.setItem(AGENT_TOKENS_KEY, JSON.stringify(agentTokens));
    const body = document.createElement("div");
    body.innerHTML = `<p>设备已注册。<strong>以下 token 仅显示这一次</strong>，请立即保存到 Edge Agent 配置（服务端只存哈希，丢失后需重新注册）：</p>
      <div class="token-box">${esc(r.token)}</div>`;
    openDialog({
      title: "设备注册成功 · 保存 token",
      body,
      actions: [
        { label: "复制 token", onClick: async () => {
            try { await navigator.clipboard.writeText(r.token); toast("token 已复制"); } catch { toast("复制失败，请手动选择复制", "error"); }
            return false;
        } },
        { label: "已保存", kind: "primary" },
      ],
    });
    $("#agent-form").reset();
    await loadAgents();
  } catch (err) { toast(err.message, "error"); }
}

async function agentHeartbeat(id) {
  const token = agentTokens[id];
  if (!token) { toast("本浏览器未保存该设备 token（仅注册时返回一次）", "error"); return; }
  try {
    await api(`/api/edge/agents/${id}/heartbeat`, { method: "POST", body: JSON.stringify({}), agentToken: token });
    toast("心跳已上报");
    await loadAgents();
  } catch (err) { toast(err.message, "error"); }
}

async function agentTelemetry(id) {
  const token = agentTokens[id];
  if (!token) { toast("本浏览器未保存该设备 token", "error"); return; }
  const result = await promptDialog("上报遥测", [
    { name: "kind", label: "类型（如 joint_state / battery）", required: true, maxLength: 64 },
    { name: "payload", label: "JSON 内容", placeholder: '{"joint_1": 0.5}' },
  ]);
  if (!result) return;
  let payload = {};
  if (result.payload) {
    try { payload = JSON.parse(result.payload); }
    catch { toast("payload 不是合法 JSON", "error"); return; }
  }
  try {
    await api(`/api/edge/agents/${id}/telemetry`, { method: "POST", body: JSON.stringify({ kind: result.kind, payload }), agentToken: token });
    toast("遥测已上报");
  } catch (err) { toast(err.message, "error"); }
}

/* ---------------- GPU 管理（admin） ---------------- */
async function refreshGpus() {
  const hostsEl = $("#gpu-hosts");
  const gpusEl = $("#gpus");
  hostsEl.innerHTML = '<div class="loading">加载中</div>';
  gpusEl.innerHTML = "";
  try {
    const [gpuList, hostList] = await Promise.all([api("/api/gpus"), api("/api/gpus/hosts")]);
    gpus = gpuList;
    gpuHosts = hostList;
    hostsEl.innerHTML = `<table>
      <thead><tr><th>主机</th><th>地址</th><th>Provider</th><th>状态</th></tr></thead>
      <tbody>${hostList.map((h) => `<tr><td>${esc(h.name)}</td><td>${esc(h.address)}</td><td>${esc(h.provider)}</td><td>${esc(h.status)}</td></tr>`).join("")}</tbody></table>`;
    gpusEl.innerHTML = `<table>
      <thead><tr><th>UUID</th><th>型号</th><th>主机</th><th>显存(MiB)</th><th>状态</th><th>占用</th><th>操作</th></tr></thead>
      <tbody>${gpus.map((g) => `
        <tr>
          <td class="checksum">${esc(g.gpu_uuid)}</td>
          <td>${esc(g.model)}</td>
          <td>${esc(g.host_id)}</td>
          <td class="num">${g.memory_total}</td>
          <td><span class="badge ${esc(g.status)}">${GPU_STATUS_CN[g.status] || esc(g.status)}</span></td>
          <td class="muted">${g.workspace_id ? `#${shortId(g.workspace_id)}` : "—"}</td>
          <td><div class="actions-cell">
            ${g.status === "available" ? `<button class="secondary small" data-action="gpu-drain" data-id="${esc(g.id)}">进入维护</button>` : ""}
            ${g.status !== "unhealthy" ? `<button class="danger small" data-action="gpu-unhealthy" data-id="${esc(g.id)}">标记异常</button>` : ""}
          </div></td>
        </tr>`).join("")}</tbody></table>`;
  } catch (err) {
    hostsEl.innerHTML = `<div class="error-block">${esc(err.message)}</div>`;
    gpusEl.innerHTML = "";
  }
}

async function gpuDrain(id) {
  if (!await confirmDialog("GPU 进入维护", "该 GPU 将不再接收新工作区（已有分配不受影响）。确认？", { confirmText: "确认维护" })) return;
  try {
    await api(`/api/gpus/${id}/drain`, { method: "POST" });
    toast("已标记维护中");
    await refreshGpus();
  } catch (err) { toast(err.message, "error"); }
}

async function gpuUnhealthy(id) {
  if (!await confirmDialog("标记 GPU 异常", "异常 GPU 不再参与调度。确认？", { danger: true, confirmText: "标记异常" })) return;
  try {
    await api(`/api/gpus/${id}/unhealthy`, { method: "POST" });
    toast("已标记异常");
    await refreshGpus();
  } catch (err) { toast(err.message, "error"); }
}

/* ---------------- 事件委托 ---------------- */
const actionHandlers = {
  "create-workspace": (el) => createWorkspace(el.dataset.id),
  "open-ide": (el) => openIDE(el.dataset.id),
  "show-logs": (el) => showLogs(el.dataset.id),
  "stream-panel": (el) => streamPanel(el.dataset.id),
  "stream-start": (el) => startStream(el.dataset.id),
  "stream-connect": (el) => streamTransition("connect", el.dataset.sid),
  "stream-disconnect": (el) => streamTransition("disconnect", el.dataset.sid),
  "stream-reconnect": (el) => streamTransition("reconnect", el.dataset.sid),
  "stop-workspace": (el) => stopWorkspace(el.dataset.id),
  "start-workspace": (el) => startWorkspace(el.dataset.id),
  "delete-workspace": (el) => deleteWorkspace(el.dataset.id),
  "load-metrics": () => loadMetrics(),
  "load-templates": () => loadTemplates(),
  "load-workspaces": () => loadWorkspaces(),
  "refresh-usage": () => refreshUsage(),
  "refresh-deploy": () => refreshDeploy(),
  "refresh-edge": () => refreshEdge(),
  "select-course": (el) => selectCourse(el.dataset.id),
  "create-lab": (el) => createLab(el.dataset.id),
  "create-assignment": (el) => createAssignment(el.dataset.id),
  "launch-lab": (el) => launchLab(el.dataset.id),
  "list-assignments": (el) => listAssignments(el.dataset.id, el.dataset.name),
  "submit-assignment": (el) => submitAssignment(el.dataset.id),
  "deploy-download": (el) => deployDownload(el.dataset.id),
  "deploy-verify": (el) => deployVerify(el.dataset.id),
  "deploy-run": (el) => deployRun(el.dataset.id),
  "deploy-complete": (el) => deployComplete(el.dataset.id, el.dataset.ok),
  "agent-heartbeat": (el) => agentHeartbeat(el.dataset.id),
  "agent-telemetry": (el) => agentTelemetry(el.dataset.id),
  "gpu-drain": (el) => gpuDrain(el.dataset.id),
  "gpu-unhealthy": (el) => gpuUnhealthy(el.dataset.id),
};

document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-action]");
  if (!el || el.disabled) return;
  const handler = actionHandlers[el.dataset.action];
  if (handler) { e.preventDefault(); handler(el); }
});

/* ---------------- 静态事件绑定 ---------------- */
function bindStaticEvents() {
  $("#auth-form").addEventListener("submit", handleAuthSubmit);
  $("#tab-login").addEventListener("click", () => setAuthMode("login"));
  $("#tab-register").addEventListener("click", () => setAuthMode("register"));
  $("#auth-logout").addEventListener("click", () => doLogout());
  $$(".nav-item").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
  window.addEventListener("hashchange", () => showView(currentView()));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) pollTick(); });
  $("#recharge-form").addEventListener("submit", handleRecharge);
  $("#join-course-form").addEventListener("submit", handleJoinCourse);
  $("#create-course-form").addEventListener("submit", handleCreateCourse);
  $("#deploy-form").addEventListener("submit", handleDeploySubmit);
  $("#deploy-demo-checkpoint").addEventListener("click", createDemoCheckpoint);
  $("#agent-form").addEventListener("submit", handleAgentSubmit);
}

/* ---------------- 初始化 ---------------- */
(async function init() {
  bindStaticEvents();
  setAuthMode("login");
  await loadHealth();
  if (TOKEN) {
    try {
      me = await api("/api/auth/me");
    } catch {
      TOKEN = "";
      localStorage.removeItem(TOKEN_KEY);
      me = null;
    }
  }
  setAuthUI();
  // 模板匿名可浏览（点击启动时才要求登录）
  loadTemplates();
  if (me) {
    await Promise.allSettled([loadMetrics(), loadWorkspaces()]);
  }
  await showView(currentView());
})();
