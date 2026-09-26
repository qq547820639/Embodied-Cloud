# RELEASE_PROCESS — EmbodiedCloud

> 版本：0.6.0（2026-09-26）。语义化版本；每一 release 必须产出规定的 artifacts 与验证矩阵。

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
2. `make build` 生成 wheel 与 sdist
3. `make verify-lock` / `make sbom` / `make audit`（uv.lock 一致性、CycloneDX SBOM、漏洞审计）
4. `make validate` 重生成 `docs/VALIDATION.json` / `.md`（分级矩阵的唯一事实源；
   无条件重跑 —— "版本一致就复用旧报告"会让工件比工作树少几条用例）
5. 校验 sdist 清洁度（不得含 `__pycache__` / `*.pyc` / cache / `test-*.db` / `.env` / `.venv`）
6. 生成 `dist/checksums.txt`（wheel / sdist / sbom 的 SHA-256）
7. 生成 `dist/VALIDATION_STATUS.md`（集成档表由 `docs/VALIDATION.json` 逐行生成，
   不手抄；写完强制检查产物里不残留反引号或用例运行输出）
8. 打印产物清单并**提示**人工执行 `git tag v<version>` 与 push（脚本本身不打 tag、不 push）

## 6. 镜像策略

- 镜像 tag = 版本号（如 `embodiedcloud/control-plane:0.2.0`）。
- 禁止推送/引用 `latest`。

## 7. 发布后

- 更新 `docs/CURRENT_STATE.md`、`docs/MASTER_PLAN.md`、`docs/ACCEPTANCE_GATES.md`。
- commit + push；必要时打 tag。
