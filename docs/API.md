# API 参考 — EmbodiedCloud

> 版本：0.4.0。完整 OpenAPI 规范见 `docs/openapi.json`（`make api-docs` 生成，CI 校验 freshness）。
> 除标注「公开」的端点外，全部需要 `Authorization: Bearer <session-token>`；owner/org 隔离下越权一律 404（不泄露资源存在性，SECURITY.md T1）。

## 认证（公开）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/register` | 注册（email/username/password≥8），自动登录返回 token |
| POST | `/api/auth/login` | 登录返回 Bearer token（会话默认 7 天） |
| POST | `/api/auth/logout` | 注销当前用户全部会话 |
| GET | `/api/auth/me` | 当前用户信息（前端刷新页面校验会话用） |

## 健康与模板（公开）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | provider 就绪状态 + 版本号（UI 版本 pill 数据源） |
| GET | `/api/templates` | 启用的模板列表（version-locked） |
| GET | `/api/templates/{id}` | 模板详情 |
| GET | `/metrics` | Prometheus 指标（非 schema） |

## 工作区

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/workspaces` | 我的工作区（admin 为全部；不含已删除） |
| POST | `/api/workspaces` | 创建（template_id / name? / auto_start=true），自动过额度/配额门禁（不足 → 402） |
| GET | `/api/workspaces/{id}` | 详情 |
| GET | `/api/workspaces/{id}/access` | IDE URL / 密码（解密后仅返回 owner）/ stream 端口 |
| POST | `/api/workspaces/{id}/start` | 启动（durable operation，异步执行） |
| POST | `/api/workspaces/{id}/stop` | 停止并结算本运行段（durable，幂等） |
| DELETE | `/api/workspaces/{id}` | 删除（durable DESTROY，soft delete tombstone） |
| GET | `/api/workspaces/{id}/logs?tail=200` | runtime 日志 |
| POST | `/api/workspaces/{id}/demo-checkpoint` | **仅 mock 演示模式**：生成模拟 checkpoint 供 Sim2Real 流程端到端演示；真实 provider 返回 400 |
| GET | `/api/workspaces/admin/all` | admin：含 tombstone 的全量审计 |

## 用量与账本（不可变 Ledger）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/usage` | 聚合：运行中数 / 现存数 / GPU 秒（含运行中 live）/ 估算成本 / credits 余额 |
| GET | `/api/ledger` | 账本明细（最近 200 条；admin 全量） |
| POST | `/api/ledger/recharge` | 充值（演示语义：直接记账；幂等键可选） |
| POST | `/api/admin/ledger/adjustment` | admin：调账（可指定 target_user_id） |

## 课程（Course / Lab / Assignment / Submission）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/courses` | 创建课程（创建者自动成为 instructor） |
| GET | `/api/courses` | 我的课程（owner 或 member） |
| GET | `/api/courses/{id}` | 课程详情（member 可见） |
| POST | `/api/courses/{id}/join` | 按 id 加入 |
| POST | `/api/courses/join-by-slug` | **凭邀请码（slug）加入**，slug 即老师分享的邀请码 |
| POST | `/api/courses/{id}/members` | teacher：添加成员（user_id + role） |
| GET | `/api/courses/{id}/members` | teacher：成员列表 |
| GET | `/api/courses/{id}/completions` | teacher：全班 × 作业完成矩阵 |
| GET | `/api/courses/{id}/my-progress` | member：我的作业进度（状态/时间/关联工作区） |
| POST | `/api/courses/{id}/labs` | teacher：创建实验（模板 + 学生配额） |
| GET | `/api/courses/{id}/labs` | member：实验列表（学生可读以 launch） |
| POST | `/api/labs/{id}/assignments` | teacher：创建作业 |
| GET | `/api/labs/{id}/assignments` | member：作业列表 |
| POST | `/api/labs/{id}/launch` | member：一键启动实验工作区（course 配额门禁） |
| POST | `/api/assignments/{id}/submit` | member：提交作业（可选关联 workspace） |

## 流式会话（Streaming 状态机）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/streaming/{workspace_id}/start` | 为 RUNNING 工作区创建会话（starting → ready） |
| GET | `/api/streaming/workspace/{workspace_id}` | 会话列表 |
| POST | `/api/streaming/sessions/{id}/connect` | connected |
| POST | `/api/streaming/sessions/{id}/disconnect` | disconnected |
| POST | `/api/streaming/sessions/{id}/reconnect` | ready（等待重连） |
| GET | `/api/streaming/warmpool/metrics` | warm pool 观测 |
| GET | `/api/streaming/warmpool/benchmark?template_id=` | launch 基准（P50/P95） |

## 部署 · Sim2Real

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/deployments` | 创建：workspace 产出路径 → Artifact（sha256）→ 部署记录（pending） |
| GET | `/api/deployments?workspace_id=` | 我的部署（owner 隔离） |
| GET | `/api/deployments/{id}` | 详情 |
| POST | `/api/deployments/{id}/download` | pending → downloading（仅进入状态，绝不自动 VERIFIED） |
| POST | `/api/deployments/{id}/verify` | 控制面侧幂等校验（对象存储 checksum 比对） |
| POST | `/api/deployments/{id}/run` | verified → running，可选绑定自己的 Edge Agent |
| POST | `/api/deployments/{id}/complete` | running → success/failed（终态） |
| POST | `/api/deployments/{id}/report-checksum` | **Edge Agent 上报本地 sha256**（`X-Agent-Token` 认证）——唯一 edge→server 校验路径，防绕过/防 replay |

## 边缘设备（Edge Agent）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/edge/agents/register` | 注册（绑定当前用户），**token 仅返回一次** |
| GET | `/api/edge/agents` | 我的设备（admin 全量） |
| GET | `/api/edge/agents/{id}` | 详情（越权 404） |
| POST | `/api/edge/agents/{id}/heartbeat` | 心跳（`X-Agent-Token`） |
| POST | `/api/edge/agents/{id}/telemetry` | 遥测（`X-Agent-Token`） |

## GPU 管理（admin）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/gpus` | GPU inventory |
| GET | `/api/gpus/hosts` | 主机列表 |
| POST | `/api/gpus/{id}/unhealthy` | 标记异常（不参与调度） |
| POST | `/api/gpus/{id}/drain` | 进入维护（不再新分配） |

## 认证方式

- 用户会话：`Authorization: Bearer <token>`（token 仅注册/登录时返回一次，服务端只存哈希）。
- Edge Agent：`X-Agent-Token: <agent-token>`（注册时返回一次）。

## 示例

```bash
# 注册并创建 cartpole 工作区
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"me@example.com","username":"me","password":"password123"}' \
  | python -c 'import json,sys;print(json.load(sys.stdin)["token"])')

curl -s -X POST http://127.0.0.1:8000/api/workspaces \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"template_id":"cartpole","auto_start":true}'

# 学生凭邀请码加入课程
curl -s -X POST http://127.0.0.1:8000/api/courses/join-by-slug \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"slug":"intro-rl-2026"}'
```

> `/access` 在 v0.1 可信单团队模式会返回 code-server 密码；进入多租户 Beta 后必须替换成 OIDC/短期签名 token，不保留「任意 API 调用者都能拿明文密码」的模型（当前已限 owner/admin + 密码只存 Fernet 密文，见 SECURITY.md §8）。
