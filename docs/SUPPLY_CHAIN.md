# SUPPLY CHAIN — EmbodiedCloud 供应链可复现性

> 更新：2026-09-26（v0.7.0）。只记录仓库内已落地/已验证的事实；未验证项明确标注。

## 1. 镜像

| 项 | 现状 | 状态 |
|---|---|---|
| workspace 镜像 tag | `embodiedcloud/isaaclab-workspace:0.1.0`（seed.py `TEMPLATE_IMAGE`） | VERIFIED：immutable tag，**禁止 latest**（seed 模板与 TemplateVersion 均检查） |
| 镜像 digest | `TemplateVersion.image_digest` 现在**有写入入口也有读者**：`python -m app.cli record-image-digest --template-id … --version … --digest sha256:…`（拒收形制不对的串、拒改已钉在另一摘要上的 released 版本），消费点 `app/services/image_ref.py:pinned_ref` 在 workspace 快照那一刻把 digest 拼进 `image`（`reg/img:1.0.0@sha256:…`），docker/k8s 两条 provider 因此启动的都是钉死的引用 | 机制 VERIFIED（本轮；4 支变异对照 PIN1–PIN4，见 CHANGELOG 0.7.0）；**取值仍 BLOCKED**：本机没有可核实的 workspace 镜像 digest（镜像没建出来）。本轮实测过摘要来源：`docker image inspect --format {{.Id}}` 与 `.RepoDigests[0]` 同值（本地构建、从未推送的 scratch 镜像、以及拉取来的 `postgres:16-alpine` 两例皆然），脚本据此回填并**拿不到就退出**，不写"看起来像 digest"的串 |
| Template 镜像来源 | TemplateVersion.image → workspace.image 快照 → provider 启动（§15 已落地） | VERIFIED：`test_template_versions.py` 证明 Template A→image A、B→image B |
| 构建配方 | `runtime/Dockerfile.isaaclab-workspace`（`scripts/build_workspace_image.sh` 只是 docker build 的包装，下载步骤在 Dockerfile 内） | PARTIAL：真实构建需 NGC 凭据；配方内的下载/克隆已钉死（见 §2/§3） |

## 2. Isaac Lab / Isaac Sim 版本

| 项 | 现状 | 状态 |
|---|---|---|
| Isaac Sim 版本 | `FROM nvcr.io/nvidia/isaac-sim:6.0.1@sha256:783444c706538aa76cf5126e911ddc5e618779e6105305ad4af4260362a30aa9` | VERIFIED（本轮由 tag 升级为钉 digest）：digest 由**权威源 nvcr.io 本身**匿名解析得到（`/proxy_auth` 换取 pull 令牌 → `HEAD /v2/nvidia/isaac-sim/manifests/6.0.1` 的 `docker-content-digest`），并 `GET` 同一 manifest list 重算 body 的 sha256 与该读数一致（743 B，`…manifest.list.v2+json`）；子清单 linux/amd64 `sha256:b1c542b2…`、linux/arm64 `sha256:20269735…`。**钉的是多架构索引而非单个平台清单**，amd64 GPU 主机与 arm64 本机各自按平台解析。`name:tag@digest` 形式由本机 `docker build` 实测接受（进入解析并按 digest 开始拉层），tag 保留只为可读性。**"阻塞于 NGC 凭据"是错的**：解析 digest 不需要凭据，凭据只在拉层字节时才要 |
| 控制面基础镜像 | `FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f` | VERIFIED（本轮由"已登记的例外"升级为钉 digest）：**权威读数取自 docker.io 本身**——`docker pull --platform linux/amd64 python:3.12-slim` 打印的 `Digest` 即此值，随后 `docker pull docker.io/library/python@sha256:f77ac9e4…` 按 digest 再解析一次并成功（本机 arm64 走同一索引解析出可运行镜像）。上一轮只在第三方镜像 `public.ecr.aws/docker/library/python` 上读到同一个索引 digest，当时记为"两源互不印证"（第三方索引 digest 与权威一致本就是镜像站的预期行为，但当时缺权威侧那一手）；本轮补上的正是这一手，两个独立传输路径同值。取证路径本身要记一条限制：本机 `curl` 到 `auth.docker.io`/`registry-1.docker.io`/`hub.docker.com` 仍全部超时，能出网的是 **docker 守护进程**那条传输（`docker manifest inspect` 走 CLI 直连，同样超时、读不到），所以读数只能来自 pull 这条路径，不是"三条路径都试过"。钉的是多架构索引而非单个平台清单 ⇒ amd64 GPU 主机与 arm64 本机各自按平台解析。**失败时怎么定案**（本轮新增常驻机制）：守护进程这条传输今晚出现过对**有效**摘要回 `not found`（它走配置里的镜像站 `docker.1panel.live`，同一条通道上一轮还能 pull 成功），所以 `test_pinned_base_of_the_control_plane_recipe_is_fetchable` 不再「取不到就红」，而是先用第二条传输定案：取 `public.ecr.aws/docker/library/python` 的 manifest 并逐字节重算摘要——两边都说没有＝钉错了（红）；镜像站说没有而第二条通道逐字节确认有＝通道故障（报两条读数并跳过，主张本轮未获证）；**这条分流的完整形状**（与 `_pull_failure_action` 逐档一致，2×3 全组合由 `test_pull_failure_action_adjudicates_every_combination` 常驻打靶）：`absent` 永远先赢——第二通道说「这份摘要不存在」时，不管守护进程的错误文本长得多像通道故障都判红；只有「守护进程报的不是通道类错误、第二通道又不给任何读数」才落 `red_undecided`，而守护进程错文本属通道类时 present／unknown 都判跳。第二通道按注册表分策略：Docker Hub 官方库映射到 `public.ecr.aws/docker/library/<repo>`（换一家注册表），`ghcr.io/…` 映射到同一个 ghcr 但换一条传输（宿主 urllib 直连 registry API——它能定案「摘要在不在、字节对不对」，定不了「这家注册表本身有没有被篡改」），映射不出前缀的仓库一律 `unknown`，不靠猜把别家的名字拼到官方库路径上。本轮还量到一次这套机制该跳的真实形状：ghcr 那条按摘要拉 `04d046b1…` 时报 `failed to do request`，同一刻宿主那条也没通 ⇒ 用例干净跳过、整套 skip 从 1 变 2；约 20 分钟后同一条 `docker pull` 退 0。所以这条判据钉的是「坏摘要必红」，不是「每轮必跑」。另：present/absent 极性对照从只核第一份引用扩成「配方里每份钉死的引用各跑一对」，ghcr 那条分支的两档本轮实测为 present（2196 B、重算相等）与 absent（翻一位回 404）。翻一位的假摘要在第二条通道必须同样是 absent（`test_independent_digest_read_discriminates_present_from_absent` 实测：真摘要 present／翻转 absent），否则这条定案通道就成了免检通道。**构建侧实跑**：`make control-image` 的 `Step 1/10 : FROM python:3.12-slim@sha256:f77ac9e4…` 用的就是这条引用，`Successfully built 53af23a7ecd0`，产物容器内 `python -V` = `Python 3.12.14`（本次构建产物随后 `docker rmi` 删除；此前没有任何常驻门禁构建过控制面镜像，见 §8 第 3 项） |
| 构建期工具镜像（uv） | `COPY --from=ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424 /uv /uvx /bin/`（`runtime/Dockerfile.control-plane:18`） | VERIFIED（本轮新增）：多架构索引摘要由**两条独立通道同值**取证——宿主用匿名令牌取 `ghcr.io/v2/astral-sh/uv/manifests/0.12.19`，回的 `Docker-Content-Digest` ＝ `sha256:04d046b1…`，body 2196 B、`mediaType=application/vnd.oci.image.index.v1+json`，**钉的方向单独数过**：索引里 4 条子清单，`linux/amd64`＝`sha256:d46db4c7b7f2…`、`linux/arm64`＝`sha256:de342e010065…` 各在（另两条是 attestation 用的 `unknown/unknown`）——钉的是多架构索引而不是本机 arm64 那一份，否则生产那批 amd64 主机会当场构建失败，这正是 §2 基础镜像那行记过的同一条教训，对同一批字节自算 sha256 逐字相等；守护进程 `docker pull ghcr.io/astral-sh/uv:0.12.19` 退 `0`。记一条通道读数：**ghcr 今晚是通的**，而上一轮同一台机器记的是 `ghcr.io` 拨号 i/o timeout（§8 那条"通道会自己变"又添一手——同一注册表、同一台机器、隔几小时两种相反读数，所以任何"这条通道不行"都只在它被记的那一刻成立）。uv 许可证 `MIT OR Apache-2.0`（PyPI `license_expression` 实测读数）。`COPY --from=` 已并入钉 digest 的扫面，并**按本文件声明的阶段名扣除**（`COPY --from=builder` 不是引用），两个方向各有注入档：`test_copy_from_refs_are_under_the_same_pin_rule` |
| Isaac Lab 版本 | `ARG ISAACLAB_REF=v3.0.0-beta2.patch1` + `ARG ISAACLAB_COMMIT=ffff603eafc6b74264a5261cc0183d6a65390d78`，clone 后 `test "$(git rev-parse HEAD)" = "$ISAACLAB_COMMIT"` | VERIFIED（v0.6.0）：commit 由两个独立来源核对——GitHub refs API 与 `git ls-remote` 给出同一 sha（该 tag 是 lightweight tag，直接指向 commit）。tag 可被 force push 移动，故只认 commit |
| 启动命令 | 模板 entrypoint 版本锁定（TemplateVersion） | VERIFIED |
| 配方机检 | `tests/test_supply_chain.py`：任何 `curl/wget -o` 必须同块 `sha256sum -c`；任何 `git clone --branch` 必须比对 HEAD commit；**任何非自有命名空间的 `FROM` 必须带 `@sha256:`，未钉者必须出现在双向对账的例外登记表里（多登记与漏登记都红），且登记表与本文逐字互核**；**消费侧引用同一基础镜像时必须与 Dockerfile 钉死的那份逐字相等**（`gpu_acceptance.sh`／`isaac_sim_smoke.sh`／`release.sh`／`docs/GPU_HOST.md` 四处，归属键刻意剥掉 tag）；并断言三条判据的作用域均非空 | VERIFIED（v0.6.0 建下载/克隆两条；本轮新增 digest 三条，改钉之前对两处开火，读数见 CHANGELOG 0.6.0 与本轮记录） |

## 3. code-server / 运行时组件

| 项 | 现状 | 状态 |
|---|---|---|
| code-server 版本 | 4.130.0 | VERIFIED |
| code-server 下载校验 | Dockerfile 内 `echo "${cs_sha}  /tmp/code-server.tgz" \| sha256sum -c -`，两架构分别钉摘要（amd64 `3de23052…b7ab`、arm64 `795366c4…b725`） | VERIFIED（本轮）**带一条来源限制**：上游 v4.130.0 的 release 只发 tar.gz/rpm/deb 资产，下载其 `SHA256SUMS.txt` 实测返回 `Not Found`，release notes 亦无校验表 ⇒ 表中摘要来自本机对官方制品的实算（字节数与 GitHub API 报告的资产大小 201284549 / 197540112 逐一吻合）。它防的是后续构建拿到被替换/截断的制品，不构成第三方背书。该结论本轮又核了一次、走的是另一条通道：GitHub release API 逐枚枚举
`v4.130.0` 的 9 个资产（rpm/deb/tar.gz × 两架构 + `package.tar.gz`），里面没有任何校验或签名文件——
与当时"下载 `SHA256SUMS.txt` 返回 Not Found"是两条独立通道得出的同一结论，不是措辞沿用 |
| `sha256sum -c` 机制本身 | 两档对照实测：正确摘要 rc=0 且输出 `OK`，错误摘要 rc=1 且 `FAILED` | VERIFIED |
| WORKSPACE_PASSWORD | Fernet 加密落库（§20）；runtime env 为必要明文副本；K8s 侧轮换已由真集群证明"新 Pod 持新口令、无任何 Pod 残留旧口令" | VERIFIED：`test_workspace_credential.py` + `tests/test_k8s_control_plane.py`（ADR 0009） |

## 4. Python 依赖

| 项 | 现状 | 状态 |
|---|---|---|
| 依赖范围 | pyproject.toml：fastapi/sqlalchemy/pydantic-settings/alembic/kubernetes/prometheus-client/pyyaml/cryptography | VERIFIED |
| 可选后端 | `[postgres]` = psycopg；`[s3]` = boto3（对象存储生产后端，懒加载；未安装时 store 明确抛 Unavailable，不静默降级本地） | VERIFIED：`test_artifact_store.py` + `test_s3_artifact_store.py` |
| lockfile | `uv.lock`：universal 解析（多平台 marker + sha256 哈希），CI 跑 `make verify-lock` | VERIFIED（v0.5.0） |
| 安装路径 | pip（`make install`，不依赖 uv）与 uv 两条并存，CI 都验 | VERIFIED |
| **镜像里的 wheel 来自哪** | 两段构建：builder 用 `uv sync --frozen --no-dev --extra postgres --no-install-project --no-editable` 从 `uv.lock` 装出 `/app/.venv`，runtime 只 `COPY --from=builder /app/.venv` 加源码，`PATH` 指进 venv（迁移动作 `command: ["alembic", …]` 因此仍解析得到）。**产物不是"能 import 就算"**：`docker run -d -p 127.0.0.1:18112:8000` 起真容器后，`GET /api/health` 在第 3 次探测（约 2～3s）回 `200` ＝ `{"status":"ok","provider":"mock","provider_ready":true,"version":"0.7.0"}`，`GET /metrics` 回 `200`／3770 B 的 Prometheus 文本；容器随后 `docker rm -f`，残留 0 个。这是控制面镜像第一次被"真的服务过一次"——此前记录到的取证只到 `python -V` 与 `import app.main`。**生产架构那一侧也复算过（本机 `docker build --platform` 这条路走不通，原因与替代做法见 OPERATIONS）**：
  用 `docker run --platform linux/amd64` 起同一个基础镜像的 amd64 子清单（容器内 `uname -m`＝`x86_64`、
  `python -V`＝3.12.14），把与 Dockerfile 里**同一行**的 `uv sync --frozen --no-dev --extra postgres
  --no-install-project --no-editable` 在模拟的 x86_64 环境里跑通（退 0），装完 `sqlalchemy 2.1.0`＋
  `psycopg 3.3.6` 都能在 x86_64 解释器下 import，`alembic --version`＝1.20.0，site-packages 里确有
  1 个目录名带 `x86_64` 标签的发行（amd64 专用轮子被选中并装上）。顺带钉住一件事：那份按摘要钉死的
  uv 工具镜像**取出来的二进制是 `ELF 64-bit LSB executable, x86-64, statically linked`**——
  也就是说"钉的是多架构索引"这句话在 amd64 侧真的解析到了 x86_64 那一份，而不只是我们希望的读数。
  未覆盖的一面如实记下：这验证的是依赖层在生产架构上的解析与安装，不是 `docker build` 的跨架构 plumbing
  （本机无 buildx/BuildKit），也不是在真 amd64 主机上跑一次完整构建——那条等 G2 那台机器 | **本轮闭合（原 N-22）**：旧配方 `RUN pip install --no-cache-dir ".[postgres]"` 在构建时对 PyPI **现解析**，实测抓到镜像里 `SQLAlchemy 2.1.1` 而锁钉 `2.1.0`。改成 `uv sync --frozen` 后重建并逐个核对：**47 个 pypi 组件里 46 个与 `uv.lock` 逐名逐版本相等，剩下一个是基础镜像自带的 `pip 25.0.1`**（`grep -c '^name = "pip"$' uv.lock` → `0`）。一致性由两条常驻判据钉住——配方层 `test_control_plane_recipe_installs_from_the_lock`（三档注入：退回旧配方／摘掉 `--frozen`／换成 `pip install --require-hashes -r` 的合规变体）与产物层（`make image-sbom` 把 `uv.lock` 交给判据，反证用那一夜真产物裁出来的 `tests/fixtures/sbom.image.prefix-drift.json`）。选型侧另测了 uv 的两条极性：把 `uv.lock` 里 **wheel** 的 sha256 整条置零，`uv sync --frozen` 退 `1` 并打印"Computed: …"；还原后同命令退 `0`——"版本精确"与"哈希校验"两样都拿到，于是不需要再留一份 requirements.txt（uv 文档自己不建议同时保留两份真源） |
| **发布 SBOM 覆盖哪几组 extra** | `make sbom` ＝ `uv export --frozen --extra postgres --extra s3 --format cyclonedx1.5` | **本轮修的另一半（原 §8 第 7 项）**：加 extra 之前那条只导主依赖集，读数 `44` 个组件，里面**没有** `psycopg`/`psycopg-binary`/`boto3`——而 `psycopg` 正是生产镜像装的（`.[postgres]`）、`deploy/kubernetes` 用的驱动。`dist/sbom.cdx.json` 是进 `dist/checksums.txt` 的发布产物，少报一组依赖等于给审计交了一份不全的清单。改完实测 `44 → 51` 个组件，`psycopg`／`psycopg-binary`／`boto3` 三条都到齐，dev 组（pytest/ruff）仍不进清单（`--extra` 是按需点名，不是 `--all-extras`） |
| 测试/构建工具 | pytest/httpx/playwright/psycopg/boto3/ruff/mypy/build | VERIFIED |

## 5. Release 校验

| 项 | 现状 | 状态 |
|---|---|---|
| release 流程 | scripts/release.sh：semver → lint/type/test → build → checksums → 分级验证矩阵 | VERIFIED（脚本存在且 CI 通过） |
| OpenAPI 新鲜度 | CI 重新生成 + git diff 门禁 | VERIFIED |
| SBOM | `make sbom` = `uv export --frozen --extra postgres --extra s3 --format cyclonedx1.5` → `dist/sbom.cdx.json`，随 `dist/checksums.txt` 入产物清单 | VERIFIED（v0.5.0；本轮补两个 extra）：加 extra 之前那份只导主依赖集 **44** 个组件，而 `psycopg`／`psycopg-binary`（生产镜像装的 `[postgres]` 驱动）与 `boto3`（`[s3]` 后端）**都不在里面**——进 `checksums.txt` 的清单少报了一组部署实际会装的依赖。改完实测 **51** 个组件、三条都到齐，dev 组（pytest/ruff）仍不进清单（按 `--extra` 点名，不是 `--all-extras`） |
| 镜像层 SBOM（控制面镜像） | `make image-sbom` = `scripts/image_sbom.sh`：对已构建的控制面镜像跑 trivy 出 CycloneDX → `dist/sbom.image.cdx.json`，落盘后过 `scripts/check_image_sbom.py` 的形状判据。工具镜像钉死为 `public.ecr.aws/aquasecurity/trivy:0.74.0@sha256:62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969`；这一份 digest 的**方向**单独核过：ECR Public 匿名令牌 + `Accept: …image.index.v1+json` 取回 manifest body（3772 B）逐字节重算 sha256 得同一个值，子清单 amd64 `ee940acb…`／arm64 `55ad20f8…` 各在——钉的是多架构索引，不是本机 arm64 那一份（§2 上一条留过「钉错方向会让所有人的构建当场失败」这条教训） | VERIFIED（本轮，§8 第 3 项闭合）：判据条款＝被审对象必须是 `type=container` 且 purl 严格是 `pkg:oci/<name>@sha256:<64>` 的形状（只写 tag、或把摘要塞进查询参数位的清单都不算）＋**清单自报的摘要必须等于 `docker image inspect` 的 `.Id`**（这一条才是防"扫错对象"：名字只是 trivy 对它收到的命令行参数的回声）、至少一个 `pkg:deb/`（OS 层）、至少一个 `pkg:pypi/`（wheel 层，缺它这份就与 `make sbom` 没区别）、**镜像里每个 `pkg:pypi` 必须落在 `uv.lock` 钉的那一组里**（`--lock` 由 `image_sbom.sh` 显式传入；缺基准＝红、锁解析出 0 个包＝红、名字只在 purl 里也照样查、基础镜像自带的 `pip` 走一张只认它的白名单——这一条是 N-22 的反面，判的是产物不是措辞）、`bomFormat=CycloneDX`、`components` 非空、不得混入 `vulnerabilities` 结论；量具自带 23 档注入（`--self-test` 全 OK，**每档核对精确条数**，不只判"有没有开火"）＋常驻 `test_image_sbom_validator_fires_per_clause` 覆盖同样这批条款；再往上一层，这 23 档**本身**也被常驻用例当子进程跑（`test_image_sbom_validator_self_test_runs_in_this_gate`：断言退 0、无 BAD、档位数不低于 23；把 `lock_offenders` 换成永不开火的桩，6 档立刻转 BAD——牙是量过的）。此前它只活在「人手跑一次 --self-test」的惯例里，锁那一族判据等于没有常驻反证。**真读数**：trivy 0.74.0 对配方里那份钉死的基础镜像跑 `image --format cyclonedx` 用时 12.5s，产出 `components=89 / deb=87 / pypi=1`，其自报 purl 里的摘要 `f77ac9e4…` 与 §2 钉进 `Dockerfile.control-plane` 的那个 digest 同值——两把独立的尺子（构建配方 / 清单工具自报）对上同一个事实。**两条实测记账**：① 结果走 stdout 重定向而不是 `--output` 指挂载路径——这台机器（colima）的 `/tmp` **不是共享进虚拟机的挂载点**（容器内写成功、宿主看不见；同一分钟内换成工程目录下的挂载点就可见），产物由宿主自己写才不依赖这条随时会变的约定；② trivy 自己会打印「`--format cyclonedx` disables security scanning」，因此这份产物是**清单**、不是"没有漏洞"的结论（OS 层漏洞扫描另立 §8 第 5 项）。**接线常驻**：docker 档 `test_pinned_sbom_tool_actually_produces_a_checkable_image_sbom` 每轮核"工具 digest 取得到 + 挂 docker.sock 读得到本地镜像 + 输出过同一份判据"（判据只有一份实现，测试与脚本共用，不抄读数）；**控制面镜像那一份产物本轮已真落盘**：`dist/sbom.image.cdx.json`，`components=137 / deb=87 / pypi=49`，并绑到 inspect 的 `.Id`＝`sha256:cd371b31…`（＝trivy 自报 ImageID＝purl 摘要，三处同值）。**改造配方后再跑一遍的读数是 `components=135 / deb=87 / pypi=47`、`.Id`＝`sha256:aa6500ec…`**（少掉的两条正是原先作为发行包装进镜像的本项目 `embodiedcloud`，它在旧清单里以同一个 purl 出现了两次）；137/87/49 那一组是**旧配方**产物的读数，保留在这里是因为那条反证夹具就是从它裁出来的（`tests/fixtures/sbom.image.prefix-drift.json`）；但因 `image-sbom` 依赖一次真构建（今晚前 5 次全败在容器侧→PyPI CDN 那条通道，第 6 次 165s 才成，逐条读数见 §8 第 3 项），它与 `control-image` 一样留在人工/CI 档，不折进每轮 |
| 镜像层漏洞扫描（OS 包） | `make image-cve` = `scripts/image_cve.sh`：同一份钉死 digest 的 trivy 加 `--scanners vuln` 扫已构建镜像，报告落 `dist/vulns.image.json`，库下载日志落 `dist/vulns.image.log`；漏洞库通道 `public.ecr.aws/aquasecurity/trivy-db:2` **按设计不钉 digest**（钉了就等于天天拿一份过期库说「没有漏洞」），因此走**免检登记表**：`tests/test_supply_chain.py::TOOL_UNPINNED_EXCEPTIONS` 定级 `accepted-risk` + ≥40 字理由 + 与扫描到的引用集合双向对账（漏登记／死登记／等级不在词表／理由过短 各一档注入） | VERIFIED（本轮，§8 第 5 项的机制侧闭合）：**为什么必须补这一档**——`make audit`（uv）只看 Python 侧，本机对控制面镜像的真读数是不等价的：`os-pkgs 156 条 / lang-pkgs 6 条`，按等级 `HIGH 44 / MEDIUM 58 / LOW 58 / UNKNOWN 2`（合计 162）。**库通道是量出来的不是猜的**：trivy 0.74.0 的默认优先级（它自己 `--help` 的原文）是 `mirror.gcr.io/aquasec/trivy-db:2` → `ghcr.io/aquasecurity/trivy-db:2`，本机实测两条都不通（前者 `dial tcp 108.177.125.82:443: connect: connection refused`，重试 77s 后 FATAL；后者拨号 i/o timeout），而工具镜像所在的 ECR Public 同命名空间下有库（匿名 manifest GET → 200，manifest v2，751 B），显式指定后跑通。**报告必须说得出它扫的是哪个镜像**：脚本把 `Metadata.ImageID` 与 `docker image inspect .Id` 逐字比，实测同值 `sha256:cd371b31…`。**今天只出报告、不接门禁**：44 条 HIGH 一条都没分诊，直接给阈值的结果只会是每次都红→整体跳过它；分诊与阈值另立 §8 第 5 项的剩余部分。缓存 1.4 GB 落 `.trivy-cache/`（已 gitignore），热缓存后整步 2.5s，首次含库下载约 15 分钟（今晚链路慢） | 
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
  例外不接受无等级的"先放着"。**当前该表为空**（`nvcr.io/nvidia/isaac-sim`、
  `python:3.12-slim` 与 builder 拉的 `ghcr.io/astral-sh/uv` 三份外部引用都已钉多架构索引 digest；
  第三份自本轮起也在扫面内，见 §2 与 §6 那条 `COPY --from=` 规则）；空表不等于判据停摆——
  两个方向的开火夹具常驻在
  `tests/test_supply_chain.py::test_exception_reconciliation_fires_in_both_directions`
- **多阶段配方的 `COPY --from=` 与 `FROM` 同一条规矩**：builder 拉进来的工具镜像（现在是
  `ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b1…`）也是"构建真正消费的字节"，必须钉 digest 且
  逐字进本文；同一文件里声明过的**阶段名**（`COPY --from=builder`）不算引用，按名字扣除。
  两个方向各有注入档：`test_copy_from_refs_are_under_the_same_pin_rule`（裸 `alpine:3.20` 必须红、
  阶段名不许被当成引用、真实树里那份 uv 引用必须在册且已钉）。这条是被本轮自己的改造逼出来的：
  配方拆成两段之后，如果判据还只看 `FROM`，"把 uv 换成 `latest`"就能一路绿过所有钉死判据
- **测试档位镜像同样不许用可变 tag**：`versity/versitygw:v1.8.0`、`kindest/node:v1.37.0`、
  `postgres:16-alpine` 均带具体版本；`test_test_tier_image_tag_is_pinned` 常驻把关
- **产线工具镜像同样必须钉 digest，且钉的那份逐字出现在本文**：`scripts/image_sbom.sh` 的
  `TRIVY_IMAGE` 默认值由 `test_image_sbom_step_exists_and_is_pinned` 把关，两个方向各注入一次
  （裸 tag／钉了但没进文档／钉了且进了文档＝不开火）常驻在
  `test_tool_image_criterion_fires_in_both_directions`。这条比基础镜像更要紧的理由是取字节的通道：
  本机到得了的那个注册表是**第三方公开镜像**，字节不经过我们自己的构建流水线，
  内容摘要就是把"拿到的东西"和"想要的东西"对上的唯一手段。**唯一的例外是漏洞库通道**（`public.ecr.aws/aquasecurity/trivy-db:2`）：它必须每天新鲜，钉 digest 会把扫描冻在过期库上、把「库里还没有」读成「没有漏洞」，所以它走**登记式免检**——`TOOL_UNPINNED_EXCEPTIONS` 里定级 `accepted-risk` + ≥40 字理由 + 逐字进本文，并与脚本真会拉起来的引用集合双向对账（漏登记、死登记、等级不在词表、理由过短 各有注入档）

## 7. 测试档位自带的外部制品（本轮登记）

| 用途 | 制品 | 获取与校验 |
|---|---|---|
| 对象存储真后端（`make test-s3`） | `versity/versitygw:v1.8.0`（Apache-2.0，29 MB，镜像 built 2026-09-04） | 一次性容器、loopback-only、随机凭据；缺 daemon/镜像/SDK → 整档 skip 并登记 `S3_VALIDATION_PENDING`。选型对比（含 MinIO 已归档、LocalStack 已归档且许可 NOASSERTION、moto 属二次实现故不入常驻）记录在 ADR 0008 |
| K8s 控制面真集群（`make test-k8s-control-plane`） | `kindest/node:v1.37.0` + `kind v0.33.0`（Apache-2.0） | kind 二进制发布物自带 `.sha256sum`，本机实测下载后摘要一致；集群只写进临时 KUBECONFIG，**不合并 `~/.kube/config`**，退出即 `kind delete cluster`（atexit 兜底）。选型（kind vs minikube vs envtest）与真集群读数见 ADR 0009 |
| 交叉核对（一次性，不常驻） | `minio/minio:latest`（AGPL-3.0，仓库已归档） | 仅用于验证 ADR 0008 的错误码读数是 S3 通用行为而非单一厂商方言，用完即删 |

## 8. 待办（按优先级）

1. **对真镜像跑一次 digest 回填**：机制本轮已闭（`build_workspace_image.sh` 构建后打印摘要，
   `python -m app.cli record-image-digest` 写入，`image_ref.pinned_ref` 在快照时消费，
   常驻用例把整条链连起来跑）。剩下的只是"没人拿真镜像跑过它"——阻塞于 NGC 条款 + x86 GPU 主机
   （要的是把 workspace 镜像**建出来**；本机是 Apple Silicon，且 G2–G4 的验收口径本来就要求
   NVIDIA x86 主机）
2. ~~**`python:3.12-slim` 钉 digest**~~ **本轮闭合**（见 §2 同一行：权威索引 digest 已从 docker.io 取到并钉进
   `runtime/Dockerfile.control-plane`，登记表同步清空）。留一条方法论记账：上一条登记写的阻塞理由是
   "本机三条路径均不可达"，而那三条全是 **CLI/curl 那条传输**（`auth.docker.io`、`hub.docker.com`、
   `registry-1.docker.io`）；**守护进程自己那条出网路径从没被试过**，一试就通。这与"Isaac Sim 钉 digest
   阻塞于 NGC 凭据"是同一类错——把"我试过的某条通道不通"记成"这件事做不了"。今后写"取不到权威读数"之前，
   必须先把通道列全（CLI 直连／守护进程／构建器／另一台机器），并写明哪几条试过、怎么试的。
3. ~~**控制面镜像 SBOM 化**~~ **本轮闭合（机制与真产物都在）**：机制与判据见 §5 新增那一行。
   真产物 `dist/sbom.image.cdx.json` 由 `make image-sbom` 对**本轮真构建出来的**
   `embodiedcloud/control-plane:0.7.0` 产出，读数 `components=137 / deb=87 / pypi=49`
   （OS 层 87 个 Debian 包 + 镜像里实际装上的 49 个 wheel），判据还把它绑到
   `docker image inspect` 的 `.Id`＝`sha256:cd371b31…`——三处同值（Id＝trivy 自报的 ImageID 属性
   ＝purl 里的摘要），所以"扫错对象"这一次是真的会被判红，而不是靠名字回声。
   构建侧今晚确实难：**前 5 次全败**（153s／102s／224s／156s／50s，全部死在
   `Step 7/10 : RUN pip install --no-cache-dir ".[postgres]"`，两种形状——
   `ReadTimeoutError(host='files.pythonhosted.org')` 与
   `Could not find a version that satisfies the requirement setuptools>=75 (from versions: none)`），
   第 6 次 165s 成功。定位靠同一时刻两条并排探针：容器内取
   `https://pypi.org/simple/setuptools/` 是 **200／535 KB／1.3s**，容器内对
   `https://files.pythonhosted.org/packages/source/s/setuptools/…` **TLS 握手超时**，
   而宿主 `curl` 同一 URL 拿得到 `302`——卡的是**容器侧→PyPI CDN** 那条通道，不是配方
   （同一配方 §2 那行 160s 成功过）。`make image-sbom` 在这种情况下退 2，不交空产物或假清单。
4. **完整镜像构建不常驻**（读数随配方改造一起更新）：常驻判据覆盖的是**配方里每一份钉死的引用都取得到**——多阶段之后这是两份（基础镜像 + builder 拉的 uv），覆盖面从"只看第一个 FROM"扩成"所有钉死的引用逐个 pull"（docker 档，默认 21～60s 级）。整条 `docker build` 冷跑：旧配方实测 160～165s（大头是 pip 现解析＋构建隔离还要装 setuptools），改成 `uv sync --frozen` 之后**同一台机器、同一守护进程 55.6s**（`Successfully built aa6500eca625`，产物 `python -V` 3.12.14、`alembic` 从 venv 解析得到）。它仍不折进每轮 validate：那 55.6s 里依然要在构建时把全部 wheel 字节拉一遍，属外网波动面（今晚同一条通道连红过五次），接成每轮判据换来的是环境红而不是信息。读数见 §2/§4 同一行
5. ~~**镜像层漏洞扫描（OS 包）**~~ **机制本轮闭合**：`make image-cve` 已经真跑出 162 条命中（`os-pkgs 156 / lang-pkgs 6`、HIGH 44）；**配方改造后对重建出来的那份镜像（`.Id`＝`sha256:aa6500ec…`）重跑一遍，162 条 / `os-pkgs 156`+`lang-pkgs 6` / `HIGH 44 / MEDIUM 58 / LOW 58 / UNKNOWN 2`、HIGH 塌成的 17 包 8 CVE、带 `FixedVersion` 的 6 条全部逐格不变**——那 156 条本就与 Python 层无关，重跑是给"换了 wheel"这件事做的对照，也说明这份基线读数不绑定在某一次构建上，库通道是量出来的（trivy 默认那两条 mirror.gcr.io／ghcr.io 本机都不通，改用 ECR Public 同命名空间的库）。**分诊本轮做完，据此裁决：这一步保持只出报告、不接阈值**——44 条 HIGH 逐条读下来塌成 **17 个二进制包 / 8 个 CVE**，其中 9 个包（`util-linux`／`mount`／`libblkid1`／`libmount1`／`libsmartcols1`／`libuuid1`／`liblastlog2-2` 同一版本串，加 `bsdutils` 的 `1:2.41.5-0+deb13u1` 与 `login` 的 `1:4.16.0-2+really2.41.5-0+deb13u1`，即同一个 util-linux 源包的三种 epoch 写法）共享同一组 4 个 CVE，一条就占掉 36/44；余下是 ncurses 4 包 1 CVE、systemd 2 包 1 CVE、`libacl1`、`perl-base` 各 1。**关键读数**：`FixedVersion` 这个键在 156 条 OS 命中里一条都没有（全量 162 条只有 6 条带它，全在 `lang-pkgs`），`Status` 分布 `affected 154 / fix_deferred 2 / fixed 6`，HIGH 那一档是 `affected 43 + fix_deferred 1`——把"HIGH==0"接进门禁今天就是一条**我们无能为力**的红（43 条上游没发版、1 条 Debian 自己标 `fix_deferred`），正是"每次都红→整步被跳过"的那种死法。真正可行动的尺子是 **`Status == fixed`**：今天命中 6 条、全是基础镜像自带的 `pip 25.0.1`（`PkgPath` 只有一条 `usr/local/lib/python3.12/site-packages/pip-25.0.1.dist-info/METADATA`，可升到 25.3／26.0／26.1／26.1.2／26.2.0，0 条 HIGH），而 `pip` 不在 `uv.lock` 里（`grep -c '^name = "pip"$' uv.lock` → `0`），所以 `make audit` 和 wheel 层清单都看不见它——这一格恰好证明镜像层扫描不是重复劳动。第 6 项本轮已落地，但这把尺子**仍不接成每轮判据**，理由是读数不是机制：`Status==fixed` 今天命中 6 条（基础镜像自带的 pip 25.0.1，可升到 25.3／26.0／26.1／26.1.2／26.2.0，全是 MEDIUM/LOW、0 条 HIGH），接进每轮就是一条每次必红、而修它要等基础镜像换档的项——正是本项上面躲开的那种死法。它留作"基础镜像的 pip 升上去之后自动失效"的观察项，读数由 `make image-cve` 出；`dist/vulns.image.json` 是否进发布产物清单按同一逻辑处理：**不进**（进 checksums 就等于把一份随时会变的漏洞读数冻成发布物的一部分）。UNKNOWN 那 2 条 `SeveritySource` 均为 `null`，`liblzma5` 的编号还是 Debian 占位 `TEMP-1147318-639065`（没有 CVE 号），按"看不见"处理、不折算成无漏洞。`dist/vulns.image.json` 是否进发布产物清单，随第 6 项一起定
6. ~~**镜像里的 wheel 与 `uv.lock` 不一致**（本轮由镜像层清单量出的缺陷，登记表 N-22）~~ **本轮闭合**：配方改成两段构建（builder 用 `uv sync --frozen --extra postgres --no-install-project` 从锁装、运行层只 COPY 那份 `.venv`），重建后逐个核对＝**47 个 pypi 组件里 46 个与 `uv.lock` 逐名逐版本相等**，唯一剩下的 `pip 25.0.1` 是基础镜像自带的、由判据里一张只认它的白名单接住。取证原文（当时的形状，行号是改造前的）：`runtime/Dockerfile.control-plane` 第 14 行 ＝ `RUN pip install --no-cache-dir ".[postgres]"`，构建时对 PyPI **现解析**。两笔现算差异：镜像里是 `pkg:pypi/sqlalchemy@2.1.1`、`uv.lock` 钉 `2.1.0`；镜像里 `pkg:pypi/pip@25.0.1` 在锁文件里根本没有条目（基础镜像自带）。也就是说当时**发布物的 Python 侧内容没有任何一份锁文件能描述**——`make verify-lock` 验的是仓库里的解析，管不到镜像里实际装上了什么。做法与选型（四档对比，全部开过官方文档）：多阶段 `uv sync --frozen`＋COPY `.venv`／`uv export` 出 requirements 再 `pip --require-hashes`／`uv pip sync` 直读锁／pip-tools 自解析——第三条被文档自己否掉（`uv pip sync` 枚举的支持格式里没有 `uv.lock`），第四条等于再造一个解析器（两份真源），第二条的唯一真源问题被 uv 文档点破（不建议同时保留 `uv.lock` 与 `requirements.txt`），所以引依赖走第一条；而第二条换来的哈希校验，实测在第一条里也有：把 `uv.lock` 里 **wheel** 的 sha256 整条置零后 `uv sync --frozen` 退 `1` 并打印 "Computed: …"，还原后同命令退 `0`（这两档是我自己跑的；**记账一次自己的无效测试**：头两次我把 sdist 那一行的哈希改了、而安装走的是 wheel，于是得到两次"uv 不校验哈希"的错误读数——错的不是 uv，是夹具没打在该打的那一行上）。常驻判据两条：配方层 `test_control_plane_recipe_installs_from_the_lock`（三条注入档：退回旧配方／摘掉 `--frozen`／换成 `pip install --require-hashes -r` 的合规变体；判的是"这一组依赖由 uv.lock 决定"这个谓词，不是工具名字，且只看去掉注释、折好续行之后的指令行——这一点也是被自己的反例逼出来的：注释里那句讲 `uv sync --frozen` 的话一度替被摘掉旗标的 RUN 行背书）；产物层 `check_image_sbom.py --lock uv.lock`，反证不是编的，是从旧配方那台真镜像（`sha256:cd371b31…`）上跑真 trivy 得到的清单里裁出来的 `tests/fixtures/sbom.image.prefix-drift.json`，判据对它开出 1 条并点名 `sqlalchemy 2.1.1 vs 2.1.0`；把那一处版本改回锁里钉的那份，同一把尺子整体不开火。顺带钉一条形状事实：真产物里名字写的是 `SQLAlchemy`（大写）而锁里是 `sqlalchemy`，所以判据按 PEP 503 归一化比名，大小写敏感的裸名比对会把它读成"锁里没这个包"那种假红
7. ~~**发布 wheel 清单少报了产品实际装的那一组依赖**~~ **本轮闭合**（登记表与 §4 同一行）：`make sbom` 用的 `uv export --frozen --format cyclonedx1.5` 默认只导主依赖集——实测导出的 44 个组件里**没有** `psycopg`、`psycopg-binary`（这正是生产镜像装的 `[postgres]` 驱动，`deploy/kubernetes` 的迁移动作要用它）、也没有 `boto3`（`[s3]` 后端）。`dist/sbom.cdx.json` 是进 `dist/checksums.txt` 的发布产物（`scripts/release.sh:150`），少报一组就是给审计交了一份不全的清单。改成 `--extra postgres --extra s3` 后实测 **51** 个组件、三条到齐、dev 组（pytest/ruff）仍然不进——用点名而不是 `--all-extras`，免得把开发工具混进生产清单。同轮补上的一格：不能拿这份清单与镜像层清单（`pkg:pypi` 47 条）比**基数**——前者是"可安装的两组 extra"、后者是"这一份镜像里装了什么"，本来就不该相等。钉成的是**单边子集、权威侧是配方**：镜像真装的每一组 extra 都必须出现在 `make sbom` 的导出命令里（`test_image_recipe_extras_are_declared_in_the_release_sbom`，两侧非空各有一道守卫，空对空会被读成合规）。配方侧解析走"去掉注释、折好续行"那份逻辑——`Dockerfile.control-plane:25` 的注释里就有一个字面的 `--extra postgres`，不剥注释就会多算一组
（原"Isaac Sim 基础镜像钉 digest"一项已于本轮闭合：它曾被登记为"阻塞于 NGC 凭据"，实测**不成立**——
nvcr.io 的 manifest 与 digest 用匿名 pull 令牌即可解析，凭据只在拉层字节时才需要。
原第 2 项 code-server SHA256 与原第 3 项 IsaacLab exact revision 已于 v0.6.0 落地，见 §2/§3。
原第 1 项 Python lockfile 与原第 4 项 SBOM 已于 v0.5.0 落地，见 §4/§5。）

> 第 2 条那条方法论记账（"把'我试过的某条通道不通'记成'这件事做不了'"）本轮又添一手读数，
> 方向恰好相反，因此更值得留在原地：**通道会自己变**。同一台机器同一晚，
> `docker pull` 走守护进程配置的镜像站 `docker.1panel.live` 时 TLS handshake 超时（上一轮它就是那条
> 唯一通的通道），`ghcr.io` 拨号 i/o timeout，GitHub release 下载在宿主 `curl` 与容器内 `urllib`
> 两处都拿不到字节，而 `api.github.com` 与 `raw.githubusercontent.com` 全程可读。
> 所以"取不到"必须写成**哪条通道、什么错误、几点钟的读数**，并且下一次动手前重跑那条探针——
> 本轮 SBOM 工具的选型就是被这件事直接改变的：功能上更对口的候选取不到字节，
> 于是采用官方列了三个注册表、当天还到得了的那一个（读数见 §5 同一行）。
