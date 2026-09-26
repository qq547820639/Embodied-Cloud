# ADR 0002: Provider 边界异常处理（盲捕获是设计）

状态：Accepted（2026-08-12）

## 背景
Provider/orchestrator 调用的底层（subprocess、docker CLI、k8s client）可能抛出任意异常。

## 决策
在 orchestrator 的 provision/stop 路径与 provider 的 health 路径上，**有意使用 `except Exception`**：
任何底层异常都必须转换为 Workspace FAILED 状态 + `error_message`，不能泄漏到 API 或崩溃。
ruff 的 BLE001 因此被全局豁免（见 pyproject.toml 注释）。

## 后果
- FAILED workspace 一定带有可诊断的 error_message。
- 业务逻辑错误仍使用具体异常类型；盲捕获仅限 provider 边界。

## 修订（2026-09-26）：转换到的状态要区分"最后一次尝试"

原决策的两句都保留（异常不得泄漏到 API、失败必须带可诊断的 `error_message`），但
"任何底层异常都必须转换为 Workspace **FAILED**"这一句过强：provision 由 durable worker 驱动，
一次异常之后通常还有下一次尝试（`MAX_ATTEMPTS=3`，backoff 1s/2s）。当场写 FAILED 等于让
workspace 替一个还没下的结论背书——实测到的形状是共享卡池被借走的那 1s 里第 1 次尝试报
`No GPU available`、workspace 变 FAILED，第 2 次尝试成功后它又回到 RUNNING；期间
`GET /api/workspaces/{id}` 的读者（前端、等收敛的用例）看到的是一次假死，而且下一轮尝试开头
会把 `error_message` 清空，假死连痕迹都不留。`workspace_operations` 在 API 层零读者
（`grep -rn WorkspaceOperation app/routers/` = 0），status 是"还在重试"这件事的唯一出口。

因此：**盲捕获不变，转换目标按尝试轮次分流**——`OperationWorker.will_retry(op)` 为真时写
QUEUED + 保留 `error_message` + 归还 GPU；为假（或没有 worker 接管，即同步 `_start` 路径）
才写 FAILED。判据只有一份，`finish_failure` 与 `orchestrator._fail(terminal=...)` 同读它。
常驻对照：`tests/test_worker.py::test_retryable_provision_failure_is_not_published_as_terminal`
与同文件的"attempts 用尽仍写 FAILED"那支（极性成对）。
