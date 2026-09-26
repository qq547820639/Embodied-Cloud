# SUPPLY CHAIN — EmbodiedCloud 供应链可复现性

> 更新：2026-09-26（v0.7.0）。只记录仓库内已落地/已验证的事实；未验证项明确标注。

## 1. 镜像

| 项 | 现状 | 状态 |
|---|---|---|
| workspace 镜像 tag | `embodiedcloud/isaaclab-workspace:0.1.0`（seed.py `TEMPLATE_IMAGE`） | VERIFIED：immutable tag，**禁止 latest**（seed 模板与 TemplateVersion 均检查） |
| 镜像 digest | `image_digest` 字段已建模（TemplateVersion），真实 registry digest 待构建流水线回填 | PARTIAL：字段就绪，回填依赖 NGC/registry 凭据（BLOCKED_EXTERNAL_DEPENDENCY） |
| Template 镜像来源 | TemplateVersion.image → workspace.image 快照 → provider 启动（§15 已落地） | VERIFIED：`test_template_versions.py` 证明 Template A→image A、B→image B |
| 构建配方 | `runtime/Dockerfile.isaaclab-workspace`（`scripts/build_workspace_image.sh` 只是 docker build 的包装，下载步骤在 Dockerfile 内） | PARTIAL：真实构建需 NGC 凭据；配方内的下载/克隆已钉死（见 §2/§3） |

## 2. Isaac Lab / Isaac Sim 版本

| 项 | 现状 | 状态 |
|---|---|---|
| Isaac Sim 版本 | `FROM nvcr.io/nvidia/isaac-sim:6.0.1@sha256:783444c706538aa76cf5126e911ddc5e618779e6105305ad4af4260362a30aa9` | VERIFIED（本轮由 tag 升级为钉 digest）：digest 由**权威源 nvcr.io 本身**匿名解析得到（`/proxy_auth` 换取 pull 令牌 → `HEAD /v2/nvidia/isaac-sim/manifests/6.0.1` 的 `docker-content-digest`），并 `GET` 同一 manifest list 重算 body 的 sha256 与该读数一致（743 B，`…manifest.list.v2+json`）；子清单 linux/amd64 `sha256:b1c542b2…`、linux/arm64 `sha256:20269735…`。**钉的是多架构索引而非单个平台清单**，amd64 GPU 主机与 arm64 本机各自按平台解析。`name:tag@digest` 形式由本机 `docker build` 实测接受（进入解析并按 digest 开始拉层），tag 保留只为可读性。**"阻塞于 NGC 凭据"是错的**：解析 digest 不需要凭据，凭据只在拉层字节时才要 |
| 控制面基础镜像 | `FROM python:3.12-slim`（**未钉 digest，已登记的例外**） | 证据等级 `authoritative-reading-not-obtained`：Docker Hub 的 `auth.docker.io` 与 `hub.docker.com` 本机实测均不可达（curl 28 超时），拿不到该 tag 当前指向的索引 digest。`public.ecr.aws/docker/library/python` 的官方镜像镜像站给到 `sha256:f77ac9e4…`（body 重算 sha256 一致，OCI index，16 个子清单），但它是第三方镜像而非权威源；且其 amd64/arm64 子清单的 config digest（`9e87977b…` / `8630ab77…`）**都不等于**本机缓存那份 `python:3.12-slim` 的 config（`2f17fc04…`）——两个来源互不印证。这既证明该 tag 确实会移动（本机缓存已是旧内容），也说明"今天的权威 digest"仍未证实；钉一个未证实的 digest 会让构建直接失败，故按例外登记而非钉死 |
| Isaac Lab 版本 | `ARG ISAACLAB_REF=v3.0.0-beta2.patch1` + `ARG ISAACLAB_COMMIT=ffff603eafc6b74264a5261cc0183d6a65390d78`，clone 后 `test "$(git rev-parse HEAD)" = "$ISAACLAB_COMMIT"` | VERIFIED（v0.6.0）：commit 由两个独立来源核对——GitHub refs API 与 `git ls-remote` 给出同一 sha（该 tag 是 lightweight tag，直接指向 commit）。tag 可被 force push 移动，故只认 commit |
| 启动命令 | 模板 entrypoint 版本锁定（TemplateVersion） | VERIFIED |
| 配方机检 | `tests/test_supply_chain.py`：任何 `curl/wget -o` 必须同块 `sha256sum -c`；任何 `git clone --branch` 必须比对 HEAD commit；**任何非自有命名空间的 `FROM` 必须带 `@sha256:`，未钉者必须出现在双向对账的例外登记表里（多登记与漏登记都红），且登记表与本文逐字互核**；**消费侧引用同一基础镜像时必须与 Dockerfile 钉死的那份逐字相等**（`gpu_acceptance.sh`／`isaac_sim_smoke.sh`／`release.sh`／`docs/GPU_HOST.md` 四处，归属键刻意剥掉 tag）；并断言三条判据的作用域均非空 | VERIFIED（v0.6.0 建下载/克隆两条；本轮新增 digest 三条，改钉之前对两处开火，读数见 CHANGELOG 0.6.0 与本轮记录） |

## 3. code-server / 运行时组件

| 项 | 现状 | 状态 |
|---|---|---|
| code-server 版本 | 4.130.0 | VERIFIED |
| code-server 下载校验 | Dockerfile 内 `echo "${cs_sha}  /tmp/code-server.tgz" \| sha256sum -c -`，两架构分别钉摘要（amd64 `3de23052…b7ab`、arm64 `795366c4…b725`） | VERIFIED（本轮）**带一条来源限制**：上游 v4.130.0 的 release 只发 tar.gz/rpm/deb 资产，下载其 `SHA256SUMS.txt` 实测返回 `Not Found`，release notes 亦无校验表 ⇒ 表中摘要来自本机对官方制品的实算（字节数与 GitHub API 报告的资产大小 201284549 / 197540112 逐一吻合）。它防的是后续构建拿到被替换/截断的制品，不构成第三方背书 |
| `sha256sum -c` 机制本身 | 两档对照实测：正确摘要 rc=0 且输出 `OK`，错误摘要 rc=1 且 `FAILED` | VERIFIED |
| WORKSPACE_PASSWORD | Fernet 加密落库（§20）；runtime env 为必要明文副本；K8s 侧轮换已由真集群证明"新 Pod 持新口令、无任何 Pod 残留旧口令" | VERIFIED：`test_workspace_credential.py` + `tests/test_k8s_control_plane.py`（ADR 0009） |

## 4. Python 依赖

| 项 | 现状 | 状态 |
|---|---|---|
| 依赖范围 | pyproject.toml：fastapi/sqlalchemy/pydantic-settings/alembic/kubernetes/prometheus-client/pyyaml/cryptography | VERIFIED |
| 可选后端 | `[postgres]` = psycopg；`[s3]` = boto3（对象存储生产后端，懒加载；未安装时 store 明确抛 Unavailable，不静默降级本地） | VERIFIED：`test_artifact_store.py` + `test_s3_artifact_store.py` |
| lockfile | `uv.lock`：universal 解析（多平台 marker + sha256 哈希），CI 跑 `make verify-lock` | VERIFIED（v0.5.0） |
| 安装路径 | pip（`make install`，不依赖 uv）与 uv 两条并存，CI 都验 | VERIFIED |
| 测试/构建工具 | pytest/httpx/playwright/psycopg/boto3/ruff/mypy/build | VERIFIED |

## 5. Release 校验

| 项 | 现状 | 状态 |
|---|---|---|
| release 流程 | scripts/release.sh：semver → lint/type/test → build → checksums → 分级验证矩阵 | VERIFIED（脚本存在且 CI 通过） |
| OpenAPI 新鲜度 | CI 重新生成 + git diff 门禁 | VERIFIED |
| SBOM | `make sbom` = `uv export --format cyclonedx1.5` → `dist/sbom.cdx.json`，随 `dist/checksums.txt` 入产物清单 | VERIFIED（v0.5.0） |
| 漏洞审计 | `make audit` = `uv audit --locked`，CI 在 sbom 之后执行 | VERIFIED（v0.5.0） |
| 集成档真实性 | CI 断言需要 docker 的档位（postgres/docker/browser/**object store**/**k8s 控制面**）必须 PASS，否则红；只有需要 GPU device plugin 的 `integration_k8s` 允许 PENDING。拉取测试镜像的步骤已移到 `make validate` **之前**（原顺序会让档位读数来自镜像尚未缓存的那一刻） | VERIFIED（本轮扩展） |

## 6. 禁止项

- `latest` 可变 tag：禁止（seed 与模板版本均检查，测试断言 `"latest" not in image`）
- 未经验证的镜像覆盖：seed 不允许覆盖已发布 TemplateVersion
- **外部基础镜像裸 tag：禁止**。非 `embodiedcloud/` 命名空间的 `FROM` 必须带 `@sha256:`；
  确实拿不到权威 digest 时只能走**例外登记表**（`tests/test_supply_chain.py::UNPINNED_EXCEPTIONS`），
  且登记表与本文双向对账——多登记（其实已经钉上）与漏登记（新引入的裸 tag）都判红，
  例外条目必须带固定词表里的证据等级（`authoritative-reading-not-obtained` /
  `third-party-reading-only` / `accepted-risk`）与 ≥40 字的理由。登记即定级：
  例外不接受无等级的"先放着"
- **测试档位镜像同样不许用可变 tag**：`versity/versitygw:v1.8.0`、`kindest/node:v1.37.0`、
  `postgres:16-alpine` 均带具体版本；`test_test_tier_image_tag_is_pinned` 常驻把关

## 7. 测试档位自带的外部制品（本轮登记）

| 用途 | 制品 | 获取与校验 |
|---|---|---|
| 对象存储真后端（`make test-s3`） | `versity/versitygw:v1.8.0`（Apache-2.0，29 MB，镜像 built 2026-09-04） | 一次性容器、loopback-only、随机凭据；缺 daemon/镜像/SDK → 整档 skip 并登记 `S3_VALIDATION_PENDING`。选型对比（含 MinIO 已归档、LocalStack 已归档且许可 NOASSERTION、moto 属二次实现故不入常驻）记录在 ADR 0008 |
| K8s 控制面真集群（`make test-k8s-control-plane`） | `kindest/node:v1.37.0` + `kind v0.33.0`（Apache-2.0） | kind 二进制发布物自带 `.sha256sum`，本机实测下载后摘要一致；集群只写进临时 KUBECONFIG，**不合并 `~/.kube/config`**，退出即 `kind delete cluster`（atexit 兜底）。选型（kind vs minikube vs envtest）与真集群读数见 ADR 0009 |
| 交叉核对（一次性，不常驻） | `minio/minio:latest`（AGPL-3.0，仓库已归档） | 仅用于验证 ADR 0008 的错误码读数是 S3 通用行为而非单一厂商方言，用完即删 |

## 8. 待办（按优先级）

1. **镜像 digest 回填**：`scripts/build_workspace_image.sh` 构建成功后把 registry digest 写入 `TemplateVersion.image_digest`（阻塞于 NGC 凭据 + x86 GPU 主机——**这一条是真阻塞**：要的是"把镜像建出来并推到 registry"，不是"知道基础镜像是哪个内容"）
2. **`python:3.12-slim` 钉 digest**：当前按例外登记（见 §2）。补上只需一次"在能出网的环境里向**权威源** `registry-1.docker.io` 解析该 tag 的索引 digest"的机会（本机三条路径均不可达，见 §2 同一行）；拿到后把 `@sha256:` 加进 `runtime/Dockerfile.control-plane` 并从例外登记表删除——登记表是双向核对的，钉上却不删登记会直接红
3. **控制面镜像 SBOM 化**：wheel 级 SBOM 已有，镜像层 SBOM 需真实构建后由 trivy/syft 生成

（原"Isaac Sim 基础镜像钉 digest"一项已于本轮闭合：它曾被登记为"阻塞于 NGC 凭据"，实测**不成立**——
nvcr.io 的 manifest 与 digest 用匿名 pull 令牌即可解析，凭据只在拉层字节时才需要。
原第 2 项 code-server SHA256 与原第 3 项 IsaacLab exact revision 已于 v0.6.0 落地，见 §2/§3。
原第 1 项 Python lockfile 与原第 4 项 SBOM 已于 v0.5.0 落地，见 §4/§5。）
