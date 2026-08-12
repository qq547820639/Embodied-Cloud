# SUPPLY CHAIN — EmbodiedCloud 供应链可复现性

> 更新：2026-08-12。只记录仓库内已落地/已验证的事实；未验证项明确标注。

## 1. 镜像

| 项 | 现状 | 状态 |
|---|---|---|
| workspace 镜像 tag | `embodiedcloud/isaaclab-workspace:0.1.0`（seed.py `TEMPLATE_IMAGE`） | VERIFIED：immutable tag，**禁止 latest**（seed 模板与 TemplateVersion 均检查） |
| 镜像 digest | `image_digest` 字段已建模（TemplateVersion），真实 registry digest 待构建流水线回填 | PARTIAL：字段就绪，回填依赖 NGC/registry 凭据（BLOCKED_EXTERNAL_DEPENDENCY） |
| Template 镜像来源 | TemplateVersion.image → workspace.image 快照 → provider 启动（§15 已落地） | VERIFIED：`test_template_versions.py` 证明 Template A→image A、B→image B |
| Dockerfile | `scripts/build_workspace_image.sh`（runtime/ 目录） | PARTIAL：真实构建需 NGC 凭据 |

## 2. Isaac Lab / Isaac Sim 版本

| 项 | 现状 | 状态 |
|---|---|---|
| Isaac Lab 版本 | CHANGELOG v0.1.0：Isaac Lab v3.0.0-beta2.patch1、Isaac Sim 6.0.1 | PARTIAL：版本记录在 CHANGELOG/runtime 脚本，exact revision（git hash）待镜像构建验证 |
| 启动命令 | 模板 entrypoint 版本锁定（TemplateVersion） | VERIFIED |

## 3. code-server / 运行时组件

| 项 | 现状 | 状态 |
|---|---|---|
| code-server 版本 | 4.130.0（CHANGELOG 记录） | PARTIAL：下载 SHA256 校验需在镜像构建脚本中落地（scripts/build_workspace_image.sh） |
| WORKSPACE_PASSWORD | Fernet 加密落库（§20）；runtime env 为必要明文副本 | VERIFIED：`test_workspace_credential.py` |

## 4. Python 依赖

| 项 | 现状 | 状态 |
|---|---|---|
| 依赖范围 | pyproject.toml：fastapi/sqlalchemy/pydantic-settings/alembic/kubernetes/prometheus-client/pyyaml/cryptography | VERIFIED |
| lockfile | 未提交 lockfile（uv.lock / requirements.lock） | PENDING（TECHNICAL DEBT，见下） |
| 测试/构建工具 | pytest/httpx/pytest-asyncio/ruff/mypy/build | VERIFIED |

## 5. Release 校验

| 项 | 现状 | 状态 |
|---|---|---|
| release 流程 | scripts/release.sh：semver → lint/type/test → build → checksums → 分级验证矩阵 | VERIFIED（脚本存在且 CI 通过） |
| OpenAPI 新鲜度 | CI 重新生成 + git diff 门禁 | VERIFIED（本轮新增） |
| SBOM | 未生成 | PENDING（建议 cyclonedx 接入 CI） |

## 6. 禁止项

- `latest` 可变 tag：禁止（seed 与模板版本均检查，测试断言 `"latest" not in image`）
- 未经验证的镜像覆盖：seed 不允许覆盖已发布 TemplateVersion

## 7. 待办（按优先级）

1. **Python lockfile**（uv.lock）：CI 可复现安装
2. **镜像 digest 回填**：build_workspace_image.sh 构建成功后把 digest 写入 TemplateVersion.image_digest
3. **code-server SHA256 校验**：build 脚本内验证下载物
4. **SBOM 生成**：`cyclonedx-py` 或 `syft` 接入 CI
5. **IsaacLab exact revision**：镜像 recipe 锁定 git rev
