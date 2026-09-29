# ADR 0009 — 用 kind 真集群验证 K8s provider 的控制面路径

状态：Accepted（2026-09-26）。
关联：§4 Kubernetes provider、§9 readiness、§20 凭据轮换、`docs/CURRENT_STATE.md` §3 TECH DEBT
（"K8s provider 的 `wait_ready/rotate_credentials/supports_credential_rotation` 真实集群路径"）、
ADR 0005（两层验证：能在软件里跑的一律跑，剩下的登记为物理待验）。

## 为什么要单独开一档

`tests/test_k8s_integration.py` 的前置是"有集群 **且节点带 `nvidia.com/gpu` 容量**"，
所以它在本机整档跳过；连带三个从未执行过的控制面方法一起挂着"未验证"：
`wait_ready`（§9）、`rotate_credentials` + `supports_credential_rotation`（§20）。
它们其实不需要 GPU：需要的是真 API server、真 kubelet、真 endpoints 控制器。
把"要 GPU"和"要控制面"混在同一个前置里，等于让 GPU 硬件阻塞了一个纯软件问题。

## 载体选型（2026-09-25 实测检索）

| 候选 | 功能匹配度 | License | 维护活跃度 | 适配成本 |
|---|---|---|---|---|
| kind v0.33.0 | 真 kubelet/调度器/readiness/endpoints | Apache-2.0 | pushed 2026-09-24；二进制 10 MB，发布物自带 `.sha256sum` | 单条 `kind create cluster` |
| minikube | 同上，默认多一层 VM driver | Apache-2.0 | pushed 2026-09-25 | 更重（driver 矩阵） |
| envtest（apiserver+etcd） | **没有 kubelet**：Pod 到不了 Running/Ready | Apache-2.0 | 活跃 | 轻，但要测试自己手写 pod status = 把结论当前提 |

选 kind。**本机实测**：`kind v0.33.0` + `kindest/node:v1.37.0`（sha256 与官方
`.sha256sum` 一致）在 docker 29.5.2 上 17 秒 Ready，节点 allocatable 4 CPU / 5.8 GiB。

## 真集群给出的独立事实（不来自我们的代码）

| 观测 | 实测读数 | 结论 |
|---|---|---|
| 生产 Pod spec（cpu 4 / memory 16Gi / `nvidia.com/gpu: 1`）在测试集群上 | 调度器原文：`0/1 nodes are available: 1 Insufficient cpu, 1 Insufficient memory, 1 Insufficient nvidia.com/gpu. preemption: ... not helpful` | 生产的资源声明本身就超出小集群规格 ⇒ 测试必须改 `resources`，这不是"绕过验证"而是集群规格 |
| `wait_ready` 的负向 | 未改 spec 时返回 `False`，且 Pod 条件 `Unschedulable` | 三段判据不是恒真 |
| 改完 `resources` 后 | Deployment `available_replicas=1`、Pod `Running` + `Ready=True`、Endpoints 有地址 ⇒ `wait_ready` 返回 `True` | 就绪链在真集群上成立；**Endpoints API 在 v1.37 仍可用**（deprecated 但未移除） |
| 凭据轮换 | `rotate_credentials` → 新 RS 的 Pod 起来、env 里是新口令、没有任何 Pod 还持旧口令、uid 变了 | "K8s 靠 patch env + 滚动重启换口令"这一 §20 判断成立（Docker 侧则确认不可换，见 G0.19） |
| §14 Pod 标签 | Pod 模板上 `embodiedcloud.workspace="true"`，Deployment 上才是 workspace id | 两处形态不同，已由用例分别钉住 |

## 三条工具层教训（都实测过，不是推断）

1. **strategic merge patch 对 `resources.limits` 是按键合并**：
   patch `{"limits":{"cpu":"100m","memory":"64Mi"}}` 之后，新 RS 模板里
   `nvidia.com/gpu: "1"` **仍然在**（实测），Pod 照样 Unschedulable。
   ⇒ fixture 改用 read-modify-write 整块替换 `resources`。
2. **`replace` 会撞 409**：控制器刚写完 status，`resourceVersion` 已变
   （实测 `Operation cannot be fulfilled on deployments.apps ... the object has been modified`）。
   ⇒ 按乐观并发有界重试（读→改→写，≤10 次）。
3. **判断"spec 有没有被动过"要用 `generation` 而不是 `resourceVersion`**：
   后者连 status 写都会 bump（实测 740 → 744），用它做断言会假红。

## 新增配置项 `k8s_kubeconfig`

`_load_client()` 原本调 `config.load_kube_config()`（无参）。kubernetes Python SDK（site-packages 里的 `config/kube_config.py` 第 48 行是 `KUBE_CONFIG_DEFAULT_LOCATION`）在
**模块 import 时**就把环境变量固化：上面那一行
`KUBE_CONFIG_DEFAULT_LOCATION = os.environ.get('KUBECONFIG', '~/.kube/config')`——
本机实测：进程起来后再设 `KUBECONFIG`，无参加载直接
`ConfigException: Invalid kube-config file. No configuration found.`。

对生产：只要环境变量早于进程存在就没问题；但"边车/多集群/只读挂载一份专用
kubeconfig"这类部署无法靠改 `$HOME` 表达，且测试进程里 kubernetes 必然先于集群被
import。故新增 `EMBODIEDCLOUD_K8S_KUBECONFIG`（留空 = 原行为），并在 `.env.example`
里写明这条 import 期固化语义。

## fixture 与生产代码的边界

只允许一处差异，且是**一个字段**：`container.resources`（去掉 device plugin 才会
提供的扩展资源 + 降到测试集群规格）。其余全部来自生产路径：对象名字
（`ec-<ws12>` / `ec-pvc-<ws12>`）、labels、selector、Service 端口、PVC access mode
与容量、env 集合、`nodeSelector`、GPU limit 的**声明**（在改之前先断言它等于
reservation 的 `gpu_count`）。镜像走 `workspace.image`（TemplateVersion 快照优先级），
值取节点自带的 `registry.k8s.io/pause:3.10`。

## 仍属物理待验（不假装 PASS）

`nvidia.com/gpu` 能否被真实 device plugin 分配、Isaac Sim/Isaac Lab 镜像能否起跑、
WebRTC 媒体面。入口仍是 `scripts/gpu_acceptance.sh` 与 `docs/ACCEPTANCE_GATES.md` G1–G4。

## 门禁接线

- marker `k8s_control_plane`；`make test-k8s-control-plane`
- `tests/k8s_server.py::gate_reason`：docker CLI/daemon、kind 二进制
  （`EMBODIEDCLOUD_KIND_BIN` 或 PATH）、节点镜像已缓存（**kind 按 digest 拉取，
  `docker images` 里只有 `kindest/node@sha256:…` 没有 tag**，所以缓存判定两种形态都认）、
  kubernetes SDK 可导入；任一缺失 → 整档 skip 并带哨兵 `K8S_CONTROL_PLANE_PENDING`
- `scripts/validate_release.py` 新增 `integration_k8s_control_plane`，CI 的
  "Docker-backed tiers really ran" 要求它为 PASS（因此 CI 需在 `make validate` **之前**
  `docker pull kindest/node:v1.37.0`——本轮顺带修正了原 workflow 里"先 validate、
  后拉镜像"的顺序，否则档位读数永远来自镜像尚未缓存的那一刻）

## 验证（本机实测）

未变异：`make test-k8s-control-plane` **7/7 通过**（一次集群生命周期内跑完，
退出后 `docker ps` 确认无残留节点）。

| 变异 | 预期 | 读数 |
|---|---|---|
| M6 `wait_ready()` 开头直接 `return True` | 负向对照必须开火；正向用例不能被骗过 | 红 **3** 条：`test_wait_ready_is_false_while_the_gpu_request_cannot_be_scheduled`（False→True）、`test_wait_ready_true_...`、`test_stop_start_...` 两条因"提前放行、Pod 实际未就绪"在**独立复核**处失败（`available_replicas` 读到 `None`）。⇒ 正向用例不是"信 wait_ready 的话"，它自己重读 Deployment/Pod/Endpoints，stub 掉判读骗不过去 |

