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
