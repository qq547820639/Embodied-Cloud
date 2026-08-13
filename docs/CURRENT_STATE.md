# CURRENT_STATE — EmbodiedCloud

> 版本：**0.4.0 Product UX Iteration 软件面完成**（2026-08-14）。
> 数据来源：`docs/VALIDATION.json`（`make validate` 自动生成，JUnit 稳定计数）。

## 1. 本次真实验证（实测，非复制旧文档）

| Gate | 结果 |
|---|---|
| Test | **PASS（328 passed + 1 skipped[k8s_integration]）** |
| Lint / Type | PASS（ruff 0 / mypy 40 files） |
| Migration | PASS（clean DB empty→head 11 文件链 + schema 落地校验 + downgrade 循环） |
| OpenAPI / VALIDATION freshness | PASS（make api-docs / make validate 无 diff） |
| 前端冒烟 | PASS（六视图 SPA 静态资源 200；课程/部署/Edge/流全链路 API 冒烟 OK） |

## 2. v0.4.0 本轮交付

| § | 内容 | 验证 |
|---|---|---|
| UX-1 | **前端重构为六视图 SPA**（概览/用量账单/课程/部署·Sim2Real/边缘设备/GPU 管理），此前 68 行 JS 只覆盖登录+模板+工作区，后端 9 组路由大部分能力前端零入口 | 手动 + API 冒烟；`test_version_consistency`（UI 版本 pill 动态） |
| UX-2 | 用量页（per-workspace 明细/账本明细/演示充值/口径说明）、课程页（slug 邀请码加入/教师全班矩阵/学生进度与提交）、部署页（状态机操作 + checksum）、边缘设备页（token 一次性）、流会话面板、GPU 管理页 | `test_course_onboarding.py`（6 用例）、`test_demo_checkpoint.py`（3 用例）、`test_gpu_admin.py`（2 用例） |
| UX-3 | 状态反馈：瞬态自动轮询（终态即停/页面隐藏暂停）、状态中文映射、按状态渲染操作（修复「非 running 一律显示启动」）、破坏性操作确认弹窗 + in-flight 防重 | 手动 + 现有 lifecycle 测试 |
| SEC-1 | **前端 XSS 全面转义**（workspace 名/错误信息/模板字段/日志标题）；`/demo-workspace` 后端同步转义 name/launch_command + 按真实状态渲染徽标（不再无条件 RUNNING）；IDE 密码改「复制密码」按钮 | `test_demo_workspace.py` 转义回归（name + launch_command 双断言） |
| SEC-2 | 认证盲区补齐：login/logout/me + `verify_password` 边界（pepper 变化/畸形格式）+ token 仅存哈希 | `test_auth.py`（5 用例） |
| API-1 | 新端点：`POST /api/courses/join-by-slug`、`GET /api/courses/{id}/my-progress`、`POST /api/workspaces/{id}/demo-checkpoint`（mock 专用，非 mock 400）；labs/assignments 列表对 member 可读 | `test_course_onboarding.py`、`test_demo_checkpoint.py` |
| T-1 | 修正伪覆盖：deployment「恒真 VERIFIED」改为「PENDING 直接 verify 409 防绕过」；迁移测试校验 22 张关键表落地/移除；admin_adjustment 补 HTTP 403/200；GPU 释放补真实断言 | `test_deployment_verification.py`、`test_migrations.py`、`test_usage_admin_adjustment.py`、`test_gpu_admin.py` |
| D-1 | 文档对齐：API.md 重写为全量端点参考；ARCHITECTURE 对齐代码；ACCEPTANCE_GATES 去重 + 数字刷新；四份 08-13 review 报告加「已修复」历史快照头；模板 slug 修正（GPU_HOST/ACCEPTANCE）；版本标号统一 0.4.0 | grep 校验 |
| D-2 | 死代码清理：WorkspaceStatusLegacy / require_admin / release_all_for_workspaces / ledger.history / warmpool.drain/mark_failed / new_request_id / workspace_log_context | ruff F401 全绿 |

## 3. 分项状态

### VERIFIED PASS
328 tests 全绿；lint/type/migration/build/smoke/release 全链路；前端六视图 + 全链路 API 冒烟。

### PHYSICAL_VALIDATION_PENDING / NOT_RUN（不假装 PASS）
GPU（G1–G4 脚本就绪）· K8s（pytest -m k8s_integration 正确 skip）· Streaming 媒体面 · Robot 真机 · Warm pool SLA。

### BLOCKED_EXTERNAL_DEPENDENCY
NGC 凭据（镜像 digest 回填）· PostgreSQL 生产验证/容器测试（无 docker daemon）· S3 凭据 · 物理机器人 · 真实 K8s 集群。

### TECH DEBT（已知、有意延后）
BillingAccount 重构（§17，当前 user/org 双 FK 聚合视角）· CreditHold 预授权（§18）· edge agent 独立包（§25）· SQLite FK 约束（§30）· lockfile/SBOM · 并发语义测试迁移到 PostgreSQL · `default_idle_timeout_minutes`（缺 runtime 活动信号，标注预留）· 前端无自动化浏览器测试（当前以 API 冒烟 + 转义回归覆盖）。

## 4. 结论

v0.4.0 Product UX Iteration 软件面完成：前端从"演示级原型"升级为覆盖
**全部后端能力**的六视图控制台（用量账本/课程/部署/Edge/流/GPU），状态反馈、
确认/防重、XSS 转义、可达性、移动端按审计 H1–H3 清单逐项落地；后端补齐学生端
课程闭环（slug 邀请码/我的进度/member 可见）与演示模式 Sim2Real 闭环（mock
checkpoint），并清理死代码、修正 4 处伪覆盖测试、补齐认证盲区测试。
剩余工作依赖真实硬件/凭据（GPU/K8s/机器人/S3/PostgreSQL 容器）。
