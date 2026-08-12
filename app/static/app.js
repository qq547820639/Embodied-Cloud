const $ = (s) => document.querySelector(s);
const TOKEN_KEY = "embodiedcloud.token";
let TOKEN = localStorage.getItem(TOKEN_KEY) || "";
function toast(msg){const el=$('#toast');el.textContent=msg;el.style.display='block';setTimeout(()=>el.style.display='none',5000)}
async function api(path, opts={}){
  const headers={'Content-Type':'application/json'};
  if(TOKEN) headers['Authorization']=`Bearer ${TOKEN}`;
  const r=await fetch(path,{headers,...opts});
  if(r.status===401 && !path.startsWith('/api/auth/')){toast('会话已过期，请重新登录');doLogout();throw new Error('unauthorized')}
  if(!r.ok){let t=await r.text();try{t=JSON.parse(t).detail||t}catch(_){}}// eslint-disable-line no-empty
  if(r.status===204)return null;return r.json()}
function setAuthUI(user){
  $('#auth-email').style.display=user?'none':'inline-block';
  $('#auth-username').style.display=user?'none':'inline-block';
  $('#auth-password').style.display=user?'none':'inline-block';
  $('#auth-login').style.display=user?'none':'inline-block';
  $('#auth-register').style.display=user?'none':'inline-block';
  $('#auth-logout').style.display=user?'inline-block':'none';
  $('#auth-user').style.display=user?'inline':'none';
  if(user)$('#auth-user').textContent=`${user.username} (${user.role})`;
}
async function doLogin(){
  try{const r=await api('/api/auth/login',{method:'POST',body:JSON.stringify({email:$('#auth-email').value,password:$('#auth-password').value})});
    TOKEN=r.token;localStorage.setItem(TOKEN_KEY,TOKEN);setAuthUI(r.user);toast(`欢迎回来，${r.user.username}`);refreshAll()}
  catch(e){toast(`登录失败：${e.message}`)}
}
async function doRegister(){
  try{const r=await api('/api/auth/register',{method:'POST',body:JSON.stringify({email:$('#auth-email').value,username:$('#auth-username').value||$('#auth-email').value.split('@')[0],password:$('#auth-password').value})});
    TOKEN=r.token;localStorage.setItem(TOKEN_KEY,TOKEN);setAuthUI(r.user);toast(`注册成功：${r.user.username}`);refreshAll()}
  catch(e){toast(`注册失败：${e.message}`)}
}
function doLogout(){TOKEN='';localStorage.removeItem(TOKEN_KEY);setAuthUI(null);$('#templates').innerHTML='';$('#workspaces').innerHTML='';toast('已退出')}
async function loadHealth(){try{const h=await api('/api/health');const el=$('#health');el.textContent=`${h.provider} · ${h.provider_ready?'ready':'not ready'}`;el.className=`health ${h.provider_ready?'ok':'bad'}`;el.title=h.provider_detail}catch(e){$('#health').textContent='API 不可用';$('#health').className='health bad'}}
async function loadUsage(){const u=await api('/api/usage');$('#metrics').innerHTML=`<div class="metric"><b>${u.running_workspaces}</b><span>运行中工作区</span></div><div class="metric"><b>${u.total_workspaces}</b><span>累计创建</span></div><div class="metric"><b>${Math.round(u.accumulated_gpu_seconds/60)}</b><span>GPU 分钟（含运行中）</span></div><div class="metric"><b>${u.credits_balance}</b><span>Credits</span></div><div class="metric"><b>¥${(u.estimated_cost_cny||0).toFixed(2)}</b><span>估算成本</span></div>`}
async function loadTemplates(){const ts=await api('/api/templates');$('#templates').innerHTML=ts.map(t=>`<article class="card"><div class="category">${t.category}</div><h3>${t.name}</h3><p>${t.description}</p><div class="spec"><span>${t.recommended_vram_gb}GB VRAM+</span><span>约 ¥${t.estimated_hourly_cost_cny}/h</span>${t.requires_streaming?'<span>WebRTC</span>':''}</div><div class="command">${t.launch_command}</div><button onclick="createWorkspace('${t.id}')">一键启动</button></article>`).join('')}
async function createWorkspace(templateId){try{toast('正在创建工作区…');await api('/api/workspaces',{method:'POST',body:JSON.stringify({template_id:templateId,auto_start:true})});await waitAndRefresh()}catch(e){toast(`创建失败：${e.message}`)}}
async function waitAndRefresh(){for(let i=0;i<8;i++){await new Promise(r=>setTimeout(r,450));await loadWorkspaces();await loadUsage()}}
async function openIDE(id){try{const a=await api(`/api/workspaces/${id}/access`);if(!a.ide_url){toast('IDE 尚未就绪');return}if(a.ide_password){toast(`IDE 密码：${a.ide_password}（已尝试复制到剪贴板）`);try{await navigator.clipboard.writeText(a.ide_password)}catch(_){}}window.open(a.ide_url,'_blank','noopener')}catch(e){toast(e.message)}}
async function showStream(id){try{const a=await api(`/api/workspaces/${id}/access`);toast(a.stream_hint||'该工作区没有 WebRTC 流')}catch(e){toast(e.message)}}
async function loadWorkspaces(){const ws=await api('/api/workspaces');if(!ws.length){$('#workspaces').innerHTML='<div class="empty">还没有工作区。上面选一个模板直接启动。</div>';return}$('#workspaces').innerHTML=ws.map(w=>`<article class="workspace"><div><h3>${w.name}</h3><div class="muted">${w.template_id} · ${w.gpu_name||'等待 GPU'}</div>${w.error_message?`<div class="muted" style="color:#ff9191">${w.error_message}</div>`:''}</div><div><span class="status ${w.status}">${w.status}</span><div class="muted" style="margin-top:6px">${w.id.slice(0,8)}</div></div><div class="actions">${w.ide_url?`<button onclick="openIDE('${w.id}')">打开 IDE</button>`:''}${w.stream_hint?`<button class="secondary" onclick="showStream('${w.id}')">Stream 信息</button>`:''}${w.status==='running'?`<button class="secondary" onclick="stopWorkspace('${w.id}')">停止</button>`:`<button class="secondary" onclick="startWorkspace('${w.id}')">启动</button>`}<button class="danger" onclick="deleteWorkspace('${w.id}')">删除</button></div></article>`).join('')}
async function stopWorkspace(id){try{await api(`/api/workspaces/${id}/stop`,{method:'POST'});await refreshAll()}catch(e){toast(e.message)}}
async function startWorkspace(id){try{await api(`/api/workspaces/${id}/start`,{method:'POST'});await waitAndRefresh()}catch(e){toast(e.message)}}
async function deleteWorkspace(id){try{await api(`/api/workspaces/${id}`,{method:'DELETE'});await refreshAll()}catch(e){toast(e.message)}}
async function refreshAll(){try{await Promise.all([loadHealth(),loadUsage(),loadTemplates(),loadWorkspaces()])}catch(e){toast(e.message)}}
(async function init(){setAuthUI(TOKEN?{username:TOKEN.slice(0,6)}:null);loadHealth();if(TOKEN)refreshAll();else{$('#templates').innerHTML='<div class="empty">登录后查看模板与工作区</div>'}})();
