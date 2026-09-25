# ADR 0008 — 对象存储的「不可用」与「不存在」必须是两种判决

状态：Accepted（2026-09-26）。
关联：§21 对象存储抽象、`app/services/artifact_store.py`、`docs/CURRENT_STATE.md` §3 TECH DEBT
（"S3 `ArtifactStore` 真实后端"）、ADR 0005（SQLite/PG 语义差与两层验证）、ADR 0006（冲突即 409）。

## 背景

`S3CompatibleArtifactStore` 从写下到本轮之前**一次都没有执行过**：

- boto3 不在任何依赖组里，`_client()` 的懒加载 `import boto3` 直接 ImportError；
- 覆盖它的 6 条用例全部 `monkeypatch` 掉 `_client()`，再注入一个自造的
  `botocore.exceptions.ClientError`；`botocore` 没装时用 `_install_fake_botocore()`
  往 `sys.modules` 里塞一个假的 `ClientError` 类。

也就是说：这一档测的是"我们自己的 fake 与我们自己的 if-else 是否自洽"，与线上协议无关。
把它接到真实服务端上之后，两处立刻不一致。

## 实测（2026-09-25，两台互相独立的 S3 兼容服务端）

同一份探针脚本（`/tmp/s3probe.py`，本轮一次性取证，不入仓库）跑两台服务端，
读数列成表。服务端版本：VersityGW `v1.8.0`（镜像 built 2026-09-04，Apache-2.0）、
MinIO `latest`（镜像 built 2025-09-07，仓库已归档）。客户端均为 boto3 1.43.102。

| 操作 | 场景 | VersityGW 返回的 `Error.Code` | MinIO 返回的 `Error.Code` |
|---|---|---|---|
| HeadObject | key 不存在（桶在） | `404` / "Not Found" | `404` / "Not Found" |
| HeadObject | **桶不存在** | `404` / "Not Found" | `404` / "Not Found" |
| GetObject | key 不存在 | `NoSuchKey` | `NoSuchKey` |
| GetObject | 桶不存在 | `NoSuchBucket` | `NoSuchBucket` |
| PutObject | 桶不存在 | `NoSuchBucket` | `NoSuchBucket` |
| DeleteObject | 桶不存在 | `NoSuchBucket` | `NoSuchBucket` |
| HeadObject | 签名错误 | `403` / "Forbidden" | `403` / "Forbidden" |

**两台逐点一致 ⇒ 这不是某家的方言，是 S3 协议本身的形状**：HEAD 按协议无响应体，
botocore 只能把 HTTP 状态码填进 `Error.Code`，于是「key 不存在」与「桶不存在」
在 HeadObject 上**同形、不可分辨**；只有带 body 的 GET/PUT/DELETE 才给出具名错误码。

## 决策

1. **`exists()` 在 404 分支上再探一次桶**（`HeadBucket`）：桶可用 → 真的 `False`；
   桶不可达（404/403/网络）→ `ArtifactStoreUnavailableError`。代价只在"对象缺失"
   这条少见的分支上多一个请求。
2. **异常家族一分为二**：`ArtifactNotFoundError`（业务结论：产物确实不在）与
   `ArtifactStoreUnavailableError`（基础设施故障：无从判断）。`LocalArtifactStore`
   与 S3 后端共用同一族，SDK 异常不得外泄到 Protocol 边界之外。
3. **`verify_checksum` 只把"不存在"写成 FAILED**，故障上抛 → `POST /deployments/{id}/verify`
   返回 **503**，部署记录**留在 `downloading`**。理由：`verified/failed` 都是终态
   （函数开头对终态直接幂等返回），一旦把一次存储抖动写进 FAILED，这条部署
   就永远无法重验——而它其实什么都没做错。
4. **配置面接通**：`EMBODIEDCLOUD_ARTIFACT_BACKEND=local|s3` 由组合根 `app/deps.py`
   装配 store。此前 `DeploymentService` 只接受注入、生产路径永远拿不到 S3 后端；
   `backend=s3` 而凭据不全时装配期即抛 `BLOCKED_EXTERNAL_DEPENDENCY`，**不静默回退
   local**（回退会让产物落控制面文件系统，而 `Artifact.store_name` 仍记 "s3"）。

## 测试载体选型（为什么常驻档用 VersityGW）

| 候选 | License | 维护活跃度（2026-09-25 实测） | 与本案的关系 |
|---|---|---|---|
| VersityGW v1.8.0 | Apache-2.0 | 当日有提交；镜像 29 MB | **选定**：真实 HTTP + SigV4 + XML 错误体，单容器 posix 后端 |
| MinIO | AGPL-3.0 | 仓库 `archived=true`，末次 release 2025-10-15 | 只做**一次性交叉核对**：把常驻门禁钉在停维镜像上是负债 |
| moto | Apache-2.0 | 活跃 | 不用：它是对 AWS 语义的**二次实现**，用它验证"我们如何区分错误码"是循环自证 |
| LocalStack | NOASSERTION（非 OSI） | 仓库已归档 | 排除：许可不确定 + 停维 + 全家桶超出需要 |

判据本身不依赖任何一家：离线档（`tests/test_artifact_store.py`）用**按实测形状构造的**
fake 载荷，真实档（`tests/test_s3_artifact_store.py`，marker `s3_integration`）打真服务端，
两条档同一判据并排。

## 改判的既有断言

`tests/test_artifact_store.py::test_s3_exists_nosuchbucket_raises` 原来给 HEAD 编造了
`Error.Code = "NoSuchBucket"` 的载荷——协议上不存在这种响应。该用例连同 fake 一起改写为
「HEAD 两种 404 同形 + 靠 HeadBucket 二次探测分开」，判据强度不变、可读事实变真。
其余 5 条 fake 用例保留，载荷换成实测形状。

## 验证（变异对照，均在本机实测）

| 变异 | 预期 | 读数 |
|---|---|---|
| M1 删掉 `exists()` 的 HeadBucket 探测 | 真实档开火；**改写前的**离线 fake 档不开火 | 真实档红 **3** 条；旧 fake 档红 **0** 条（这正是旧测试失明的证据） |
| M1b 同一变异，跑**改写后**的离线档 | 无 docker 也应开火 | 红 **2** 条（`test_s3_head_404_on_dead_bucket_is_unavailable_not_absent`、`test_s3_bucket_and_key_are_reported_back`）⇒ 离线档现在自己也带牙 |
| M2 `_OBJECT_ABSENT_CODES` 清空（不认 404） | 分类器失去"不存在"判据 | 真实档红 5 条 + 离线 fake 档红 2 条 |
| M3 `verify_checksum` 退回 `except Exception → _fail` | 故障被写成终态 | 红 2 条，且 503 用例实际读到 `status":"failed"`、`error_message":"artifact object missing: S3 head_bucket failed"` |

改后常驻：`make test`（含 s3 档）+ `make test-s3` 单独可跑；缺 docker/镜像/boto3 时整档
干净跳过并在 `docs/VALIDATION.json` 记 `PENDING(原因)`，CI 的 "Docker-backed tiers really ran"
门禁要求它为 PASS。
