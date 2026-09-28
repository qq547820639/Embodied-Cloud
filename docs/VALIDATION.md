# VALIDATION — EmbodiedCloud

> 版本：0.7.0（自动生成，勿手改）

## 总览：**PASS_WITH_PHYSICAL_PENDING**

| Gate | 状态 | 明细 |
|---|---|---|
| Test collected | PASS | 984 |
| Test run | PASS | failed=0 |
| lint | PASS |  |
| typecheck | PASS |  |
| migration | PASS |  |
| build | PASS |  |
| docs_test_counts | PASS | CHANGELOG/CURRENT_STATE 计数面与本报告一致 |
| docs_row_order | PASS | 带编号的登记表行均按号递增且无重号 |
| report_split | PASS | 环境读数清单与报告字段对得上 |
| docs_state_rows | PASS | 状态页的可复现数与本报告一致，且未手抄环境读数 |
| pending_reasons | PASS | PENDING 档的原因都指向可行动缺项（无 PENDING 档时视为通过） |
| reason_references | PASS | 文档与档位原因里的 env / make 目标 / extra 引用都有出处 |
| doc_references | PASS | 文档里的行号指针与章节锚点都落在实物上；指针写法四桶 已限定 209／缩写 0／歧义 0／外部 2（外部那几处按形状豁免，不当违规也不当通过） |
| Physical gpu | NOT_RUN | 需要真实硬件/凭据（NVIDIA GPU、Isaac Sim 流媒体面、物理机器人），本机不可执行 |
| Physical streaming | NOT_RUN | 需要真实硬件/凭据（NVIDIA GPU、Isaac Sim 流媒体面、物理机器人），本机不可执行 |
| Physical robot | NOT_RUN | 需要真实硬件/凭据（NVIDIA GPU、Isaac Sim 流媒体面、物理机器人），本机不可执行 |

> 由 `python scripts/validate_release.py` 生成。提交面只放**换机器重跑逐字节相同**的门禁；各集成档状态与本次跳过哪几支属环境读数，见 `dist/VALIDATION_RUN.md`（gitignored）。