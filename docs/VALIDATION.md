# VALIDATION — EmbodiedCloud

> 版本：0.7.0（自动生成，勿手改）

## 总览：**PASS_WITH_PHYSICAL_PENDING**

| Gate | 状态 | 明细 |
|---|---|---|
| Test collected | PASS | 568 |
| Test run | PASS | failed=0 |
| lint | PASS |  |
| typecheck | PASS |  |
| migration | PASS |  |
| build | PASS |  |
| docs_test_counts | PASS | CHANGELOG/CURRENT_STATE 计数面与本报告一致 |
| docs_row_order | PASS | 带编号的登记表行均按号递增且无重号 |
| report_split | PASS | 环境读数清单与报告字段对得上 |
| Physical gpu | NOT_RUN | 需要真实硬件/凭据（NVIDIA GPU、Isaac Sim 流媒体面、物理机器人），本机不可执行 |
| Physical streaming | NOT_RUN | 需要真实硬件/凭据（NVIDIA GPU、Isaac Sim 流媒体面、物理机器人），本机不可执行 |
| Physical robot | NOT_RUN | 需要真实硬件/凭据（NVIDIA GPU、Isaac Sim 流媒体面、物理机器人），本机不可执行 |

> 由 `python scripts/validate_release.py` 生成。提交面只放**换机器重跑逐字节相同**的门禁；各集成档状态与本次跳过哪几支属环境读数，见 `dist/VALIDATION_RUN.md`（gitignored）。