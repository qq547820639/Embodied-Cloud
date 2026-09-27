# RELEASE_PROCESS — EmbodiedCloud

> 版本：0.7.0（2026-09-26）。语义化版本；每一 release 必须产出规定的 artifacts 与验证矩阵。

## 1. 版本策略

- 语义化：MAJOR.MINOR.PATCH。
- 0.x 阶段：MINOR 增加功能，PATCH 修 bug。

## 2. Release 前置 Gate（必须全绿）

```bash
make lint
make typecheck
make test
make build
make smoke
make migrate-up   # 并在全新库上验证 up/down
```

任何一项失败 → 不发布。

## 3. Release 产物

| 产物 | 说明 |
|---|---|
| CHANGELOG.md | 自上一版本以来的 changes（本仓库手工维护，release 时核对） |
| dist/*.whl | 构建 artifact |
| dist/checksums.txt | SHA-256 checksum（wheel + 镜像 manifest 若有） |
| migration notes | 新增迁移列表 + 回滚说明（Alembic revision ids） |
| known issues | 已知问题清单（含 BLOCKED 硬件项） |
| validation status | 分级验证矩阵（见下） |

## 4. 分级验证矩阵（严禁混为一谈）

| 级别 | 含义 |
|---|---|
| Software Verified | 本环境/CI 真实验证通过（lint/type/test/smoke/migration） |
| GPU Verified | 在真实 NVIDIA GPU 上通过 G1–G4（当前 BLOCKED） |
| Streaming Verified | 真实 WebRTC 链路验证（当前 BLOCKED） |
| Physical Robot Verified | 真机 Sim2Real 验证（当前 BLOCKED） |

## 5. 发布流程（脚本）

```bash
make release   # scripts/release.sh：校验 → build → 供应链 → 验证矩阵 → 产物清单（不自动打 tag）
```

`scripts/release.sh` 实际执行（按脚本内 `==========` 步骤名读，编号只是排版）：
1. `make lint` / `make typecheck` / `make test`（任一失败即中止并输出原因）
2. `make build` 生成 wheel 与 sdist，并对 sdist 追一道**头部归一**（`scripts/sdist_normalize.py`：成员按名字排序、mtime 钉到 `SOURCE_DATE_EPOCH`、mode &= 0o755、uid/gid 归零、清空 uname/gname、不落 pax 记录（格式钉成 GNU_FORMAT）、gzip 写 `filename=""` 且 mtime 同样钉到 epoch——只动头部、内容逐字节保留；构建后端仍是 `python -m build --no-isolation` 取 `uv.lock` 里的 setuptools，不经 PyPI 现解析；`SOURCE_DATE_EPOCH` 由 Makefile 缺省成 HEAD 提交时间 ⇒ 同一个 commit 重建出的 wheel 逐字节相同，sdist 归一后也相同——2026-09-27 实测两建 sdist 同为 `35e21305d9…`、wheel 同为 `374ea4b59f…`。上一轮那句"sdist 目前仍带打包时刻"是当时的实测主张，这一半现在由这道工序闭上，历史读数与成因仍见 CURRENT_STATE N-34／N-36）
3. `make verify-lock` / `make sbom` / `make audit`（uv.lock 一致性、CycloneDX SBOM、漏洞审计）——本轮起 `make sbom` 带 `--extra postgres --extra s3`，导出的清单才覆盖得住部署真装的那两组依赖（改前 44 个组件里没有 psycopg/boto3，改后 51 个）
4. `make validate` 重生成 `docs/VALIDATION.json` / `.md`（**可复现门禁面**）与
   `dist/VALIDATION_RUN.json` / `.md`（**本次跑读数**，gitignored；无条件重跑 ——
   "版本一致就复用旧报告"会让工件比工作树少几条用例）
5. 校验 sdist 清洁度（不得含 `__pycache__` / `*.pyc` / cache / `test-*.db` / `.env` / `.venv`）
6. `make verify-artifacts` 复算探针：每类产物各建两次（每次一份新目录）比 sha，全等才写
   `recomputable=yes` 进 `dist/checksums.manifest`；主张与实测不一致（说反了、缺项、有项没测）退出码非 0。
   探针在自己的构建步里调同一份 `scripts/sdist_normalize.py`，所以它量的是发出去的那个形状。
   **排在 checksums 之前**——那行 sha 一旦发出去就是主张，主张必须先被核过。2026-09-27 实测：
   `[artifacts] sdist=yes wheel=yes（SOURCE_DATE_EPOCH=1790533457）`，`sdist 35e21305d9 ==`、
   `wheel 374ea4b59f ==` ⇒ 两行都 `yes`。上一轮（`SOURCE_DATE_EPOCH=1790482819`）的
   `wheel 4259babd17 ==`、`sdist 41dda386f2 != 174db2b484` ⇒ `wheel=yes / sdist=no` 是当日读数，不是现状；
   探针哪天打印回 `no`，运维读法见 `docs/OPERATIONS.md` §7.2
7. 生成 `dist/checksums.txt`（wheel / sdist / sbom 的 SHA-256）。口径由 `checksums.manifest` 逐行说明：
   `yes` 的那行可被第三方复算（同一 commit + 同一份锁 ⇒ 同 sha；换 commit 会变，因为时间基准取 HEAD 提交时间；
   sdist 那一行还要含第 2 步那道归一——它量的是**归一后**的字节，第三方走同一条 `make build` 才复算得到），
   今天两行都是 `yes`；若某行是 `no`，那一行就只是本次构建的记录，重建对不上不异常
8. 生成 `dist/VALIDATION_STATUS.md`（集成档表由 `dist/VALIDATION_RUN.json` 逐行生成，
   不手抄；写完强制检查产物里不残留反引号或用例运行输出）
9. 打印产物清单并**提示**人工执行 `git tag v<version>` 与 push（脚本本身不打 tag、不 push）

## 6. 镜像策略

- 镜像 tag = 版本号（如 `embodiedcloud/control-plane:0.2.0`）。
- 禁止推送/引用 `latest`。
- 镜像层清单：`make image-sbom`（= `scripts/image_sbom.sh`）对**已构建**的控制面镜像出
  CycloneDX → `dist/sbom.image.cdx.json`，落盘后过形状判据。它与 `make control-image` 同档，
  **不在**上面第 1–8 步里：release 链不保证现场有镜像，而把一次真构建（受外网波动影响）
  引进发布步骤，只会让人在下一次发布时整步跳过它。判据条款与选型读数见
  `docs/SUPPLY_CHAIN.md` §5、验收行见 `docs/ACCEPTANCE_GATES.md` G0.35。

## 7. 发布后

- 更新 `docs/CURRENT_STATE.md`、`docs/MASTER_PLAN.md`、`docs/ACCEPTANCE_GATES.md`。
- commit + push；必要时打 tag。
