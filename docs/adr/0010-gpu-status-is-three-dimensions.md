# ADR 0010 — GPU 的三个维度：占用、健康、管理员意图，不能继续挤在 `gpus.status` 里

状态：Accepted（2026-09-28）。本 ADR 只**定形状**，不含实现——它落账登记项 N-126 的
待拍部分，实现由后续轮次按本文第 3、4 节做。
关联：ADR 0005（SQLite/PG 语义对等）、ADR 0006（冲突即 409）、
N-112／N-114／N-122／N-123／N-125（`CHANGELOG.md` 对应小节）、
`docs/ARCHITECTURE.md` §3 的两个 drain 分工条目。

## 1. 背景：一列两主是怎么一路长出来的

`gpus.status` 今天同时被要求回答三个问题：

| 问题 | 现在的载体 | 一手位点 |
| --- | --- | --- |
| 这张卡被占了吗 | `status == allocated` | `app/main.py:34-37`（对外的 `gpu_allocated` 指标就数这个值）、`app/services/scheduler.py` 的 `allocate` 候选筛选 |
| 这张卡还能不能用（观察） | `status == available / draining` | `app/services/scheduler.py:173-182`（归位守卫在 `:173`，缺席降级在 `:176-182`） |
| 管理员要不要它离开池子 | `status == drained` **＋** `gpus.drain_requested_at` | `app/services/scheduler.py:382-405`、`app/models.py:342-346` |
| 这张卡健康吗 | `status == unhealthy` | `app/services/scheduler.py:375-380`（`mark_unhealthy`，**没有状态前置**） |

前三行是 N-123／N-125 两轮的成果：把"缺席"与"下架"分成两个值，再把"下架意图"独立成列
（`gpus.drain_requested_at`，alembic 第 16 节）。第四行还没动，于是今天真实可达这样一格：

- 管理员对一张**正在被使用**的卡点 `/unhealthy`（`app/routers/gpus.py:31-36`，路由只查 404，
  服务不查状态）⇒ `status` 从 `allocated` 变成 `unhealthy`；
- 占用事实从此在 `gpus` 这一侧消失：`gpu_allocated` 指标当场少一张，
  而那一格的 `workspaces.gpu_id` 仍指着它，`gpus.workspace_id` 也仍指着那一格
  （`release` 之前谁都不会清）。两张表各说各话，且**没有任何判据会红**——
  现存判据只钉"unhealthy 不许被自动路径抬回 available"
  （`tests/test_gpu_drain_provenance.py` 的 `test_an_unhealthy_card_is_never_auto_restored`），
  没钉"判 unhealthy 不许把占用抹掉"。

这就是登记项 N-126：N-124 那一味药（把人的判决从状态列里搬出去）只喂给了 drain，
没喂给 health。

## 2. 候选（六维对比）

检索到的外部先例（本轮亲手打开的原文）：

- **Kubernetes**：`### Manual Node administration` 写 “To mark a Node unschedulable, run
  `kubectl cordon $NODENAME`”，而失联只改 `.status` 里的 `Ready` 条件
  （`## Node heartbeats`）。**意图与观察分字段**，心跳恢复不会替管理员撤销 cordon。
  出处 https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/concepts/architecture/nodes.md
- **SLURM**：`sinfo` 的手册里 DRAIN = “The node is unavailable for use per system
  administrator request.”，DOWN = “… Slurm can automatically place nodes in this state
  if some failure occurs.”，`*` = “The node is presently not responding and will not be
  allocated any new work.”。**每种判决一个名字**，管理员的与机器的不共用。
  出处 https://slurm.schedmd.com/sinfo.html

| 维 | A｜`status` 里再加组合值（allocated_unhealthy…） | B｜`health` 独立成列（选定） | C｜引第三方状态机库（PyPI `transitions` 0.9.3，MIT） |
| --- | --- | --- | --- |
| 功能匹配度 | 组合值数量随维度相乘（占用 2 × 健康 2 × 意图 2 = 8 个值），每个新维度都要重写全部读侧 | 三列各答一问，读侧按维度收窄：分配器看 `status`＋`health`，指标看 `status`，UI 三个维度各自上色 | 状态机库管"迁移合法性"，不解决"一个值承载几个事实"；本仓的写入者分散在 4 个文件里，库无从接管 |
| License 兼容性 | 不适用 | 不适用 | MIT，与本项目许可兼容 |
| 维护活跃度 | 不适用 | 不适用 | 抓取结果只读到最新版 0.9.3，发布日期在返回里被截断 ⇒ 活跃度未亲验 |
| 安全风险 | 低（无新依赖、无新列），但组合值会把"哪些组合合法"变成口口相传的约定 | 低：多一列即多一处要守的不变量，由判据与模型↔迁移对账门看着 | 引入第三方语义面；本项目全仓零状态机依赖 |
| 代码质量 | 差：`case`／登记册／前端标签三处都要按组合枚举重写，正是 N-125 弃掉那条路的理由 | 好：`status_writer_registry` 这类结构尺直接多一维，写入者仍然一维一个名 | 现有写入路径不改的话，库只是旁边挂着的一本说明书 |
| 适配成本 | 无迁移，但要重写所有 `status == …` 的读侧（本仓 10 处以上，见 §1 表） | 迁移第 17 节一列（nullable，形状照 `gpu_hosts.last_synced_at` 与 `gpus.drain_requested_at` 两个先例）＋读侧增一条 `health` 谓词 | 需重画 allocate/release/recover 的写路径，牵动 `release_column_pairing` 等一批结构尺 |

结论：**选 B**。A 的成本不是"改得动改不动"，而是它把多维事实压成一维字符串，
下一维再加时又是同样的病；C 不改变本 ADR 要治的东西。

## 3. 决策（形状）

1. `gpus` 新增 `health` 列：`healthy` / `unhealthy`，**nullable**（NULL＝这台设备从没被
   判过健康，与"判过且健康"区分；存量行不回填，理由与第 16 节迁移一致：
   回填等于宣称管理员从没提出过的判决）。加一个 `ix_gpus_health` 索引与
   `ix_gpus_status` 同形。迁移为第 17 节，SQLite/PG 两侧都走 `batch_alter_table`（ADR 0005）。
2. `status` 从此**只答调度可用性**：`available` / `allocated` / `draining` / `drained`。
   `GpuStatus.UNHEALTHY` 不再是 `status` 的取值——它是 `health` 的取值。
   `mark_unhealthy` 改成只写 `health`，**不再覆盖 `status`**，占用中的卡判完仍是 allocated。
3. 分配候选与容量余量各加一条 `health != unhealthy` 谓词，且必须是**同一条**谓词
   （N-122 立的规矩：余量与挑卡吃同一份证据，不手写第二份）。
4. 解除路径与 drain 成对：`POST /api/gpus/{gpu_id}/healthy`（admin，204）把 `health` 写回
   `healthy`。凡"人的判决"都必须有一条只属于人的解除路径——否则一次误点就是永久掉卡，
   只能改库（N-125 给 `/undrain` 立的就是这条规矩）。
5. 自动路径一律不写 `health`：缺席降级、重报归位、`release`、recover 都不碰它。
   这一条由 `tests/test_gpu_drain_provenance.py` 的写入者登记册扩一维来钉
   （`health` 的合法写入者只有 `mark_unhealthy` 与 `mark_healthy`）。

## 4. 后果与落地清单（给下一轮）

- 判据：`mark_unhealthy` 判一张占用中的卡之后，必须同时读到 `status == allocated`
  **与** `health == unhealthy`（今天这两件事不可能同时为真，正是缺陷的形状）；
  以及"`/healthy` 只抬自己判下去的那一档"对照"自动路径不许抬"。
  结构尺：`status_writer_registry` 的期望集里 `UNHEALTHY` 消失、新增一张
  `health` 写入者登记册，配"自动路径写 health 必须被点名"的合成反证。
- 既有位点要重指（本轮不做完，列在这里防漏）：`app/services/scheduler.py` 的
  `mark_unhealthy` 与候选谓词、`app/routers/gpus.py:31-36`、`app/schemas.py` 的 `GpuOut`
  ＋ `make api-docs`（路由 docstring 会进工件——见本仓既有教训）、
  `app/static/app.js` 的 `GPU_STATUS_CN`（`unhealthy` 移出状态表、健康另开一列标签）、
  `app/metrics.py` 若要把不健康卡单报则加一支 gauge、
  `tests/test_gpu_drain_provenance.py`／`tests/test_gpu_admin.py`／`tests/gpu_pool.py`
  里凡"设成 unhealthy 当前提"的档位。
- `CHANGELOG.md` 与 `docs/CURRENT_STATE.md` 的迁移数 16→17、门禁目录新增一行；
  `docs/ARCHITECTURE.md` §3 把"两个 drain 的分工"那一条扩成"三个维度"。
- **不在本 ADR 决定范围内**：GPU 秒计费（现规则是 RUNNING 起止差，与本形状无关）、
  真机硬件故障判决由谁写入（本机无 NVIDIA 设备，仍属线下项）。

## 5. 未证实

- 本文只定形状，没有代码与判据落地；`health` 列在真 PG 上的表现由实现那一轮跑认证时验
  （`tests/conftest.py` 每条 PG 用例走完整迁移链）。
- 表里"今天真实可达那一格"（占用中被判 unhealthy ⇒ 占用事实消失）是**读码得出**的形状，
  并有既有判据只钉住一半为旁证；本轮**没有**为它写一支必开火的常驻用例——
  那是实现那一轮的第一条判据，不是本 ADR 的产物。
