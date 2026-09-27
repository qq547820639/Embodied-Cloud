PYTHON ?= .venv/bin/python
UV ?= uv
VERSION ?= 0.7.0
# 构建产物的时间基准：zip/tar 条目默认带"打包那一刻"，于是 dist/checksums.txt 里的
# sha256 只是某一次构建的记录，第三方重建对不上。取 HEAD 提交时间 ⇒ 同一个 commit
# 构建出的 wheel 逐字节相同（实测；sdist 仍不完全可复算，见 CURRENT_STATE N-34）。
SOURCE_DATE_EPOCH ?= $(shell git log -1 --format=%ct 2>/dev/null || echo 0)
export SOURCE_DATE_EPOCH

.PHONY: install dev test test-pg test-docker test-browser test-s3 test-k8s-control-plane policy-bench warm-sla warm-capacity \
        lint typecheck build verify-artifacts hang-probe smoke clean check demo amd64-probe \
        control-image workspace-image gpu-preflight gpu-test \
        compose-up compose-down migrate migrate-up migrate-downgrade \
        release api-docs validate lock verify-lock sbom image-sbom image-cve audit supply-chain

install:
	python3.12 -m venv .venv
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -e '.[dev]'

dev:
	$(PYTHON) -m uvicorn app.main:app --reload --port 8000

test:
	$(PYTHON) -m pytest

# PostgreSQL 真并发档：需要 docker daemon + 本地 postgres 镜像，
# 缺件时整档干净跳过并在 docs/VALIDATION.json 里登记 PENDING(原因)。
test-pg:
	$(PYTHON) -m pytest -m pg_integration

# Docker provider 真容器档：需要 docker daemon + 一个与 daemon 架构一致、
# 已本地缓存的测试镜像（缺件时整档干净跳过并登记 PENDING 原因）。
test-docker:
	$(PYTHON) -m pytest -m docker_integration

# 浏览器档：Playwright 驱动真 DOM（复用系统 Chrome，免下载浏览器二进制）。
test-browser:
	$(PYTHON) -m pytest -m browser_integration

# 对象存储档：自起一次性 S3 兼容服务端（VersityGW 容器），用真实 boto3 打真协议。
# 缺 docker/镜像未缓存/未装 boto3 时整档干净跳过并登记 PENDING(原因)。
test-s3:
	$(PYTHON) -m pytest -m s3_integration

# K8s 控制面档：kind 起一次性真集群（真 kubelet/调度器/endpoints 控制器），
# 验 wait_ready 与凭据轮换。GPU 声明原样保留，只有资源请求按测试集群规格下调。
test-k8s-control-plane:
	$(PYTHON) -m pytest -m k8s_control_plane

# 分配策略实测台：同一个分配器、同一份工作负载，只换候选排序，把 best-fit 值多少张卡量出来。
# 常驻判据（tests/test_scheduler_policy.py）复用这里的 run_policy，读数与 CI 同源。
policy-bench:
	$(PYTHON) -m tests.scheduler_policy_lab

# warm pool SLA 取证台：一次真 claim 的客户端/服务端双读数 + 冷启动基准（mock）。
# 不进每轮 validate（要起栈、且绝对值判定本就归 G1–G4），但常驻用例的四条反向对照
# （tests/test_warmpool_http_surface.py）与这里的三条 print 判的是同一批事实。
warm-sla:
	$(PYTHON) scripts/warm_pool_sla_lab.py

# warm pool 补位量 vs 舰队容量的对账台（N-29 的读数从这里出）。人工/CI 档，不进每轮 validate。
# 用法：make warm-capacity [SIZES=1,2,4 RESERVE=0,0,2]
warm-capacity:
	$(PYTHON) -m scripts.warm_pool_capacity_lab $(if $(SIZES),--sizes $(SIZES),) $(if $(RESERVE),--reserve $(RESERVE),)

lint:
	$(PYTHON) -m ruff check app tests edge_agent scripts
	@for f in scripts/*.sh runtime/workspace-entrypoint.sh; do bash -n "$$f"; done

typecheck:
	$(PYTHON) -m mypy app edge_agent

build:
	$(PYTHON) -m build --no-isolation

smoke:
	./scripts/smoke_api.sh

clean:
	rm -rf dist build *.egg-info .pytest_cache .mypy_cache .ruff_cache
	rm -f embodiedcloud.db test-*.db
	rm -rf /tmp/embodiedcloud-workspaces /tmp/test-embodiedcloud-workspaces

check: lint typecheck test

demo:
	./scripts/run_demo.sh

control-image:
	docker build -f runtime/Dockerfile.control-plane -t embodiedcloud/control-plane:$(VERSION) .

workspace-image:
	./scripts/build_workspace_image.sh

gpu-preflight:
	./scripts/preflight_gpu_host.sh

gpu-test:
	./scripts/gpu_acceptance.sh

compose-up:
	docker compose -f deploy/docker-compose.control-plane.yml up -d --build

compose-down:
	docker compose -f deploy/docker-compose.control-plane.yml down

migrate: migrate-up

migrate-up:
	$(PYTHON) -m alembic upgrade head

migrate-downgrade:
	$(PYTHON) -m alembic downgrade -1

release:
	./scripts/release.sh $(VERSION)

api-docs:
	$(PYTHON) -c "from app.main import app; import json, pathlib; open('docs/openapi.json','w').write(json.dumps(app.openapi(), indent=2))"

validate:
	$(PYTHON) scripts/validate_release.py

# 挂起取证：真造一次"外部依赖卡住"（黑洞地址 / 假 docker CLI），量每档付几秒、说了什么。
# --timeout 是**请求值**：三份假 CLI 剧本还要过一道 10s 封顶（慢与挂之间必须留 2× 余量），
# 只有 blackhole 不夹（见 scripts/hang_probe.py 模块头）。常驻判据用 --timeout 2 的同一份实现。
hang-probe:
	$(PYTHON) -m scripts.hang_probe $(if $(TIMEOUT),--timeout $(TIMEOUT),)

# 产物可复算性探针：每类产物各建两次比 sha，全等才允许对第三方声明 recomputable=yes。
# 读数写进 dist/checksums.manifest（gitignored），主张与实测不一致即退出码 1。
verify-artifacts:
	$(PYTHON) scripts/artifact_reproducibility.py

# --- 供应链：锁文件 / SBOM / 漏洞审计 ---
# uv.lock 是 universal 解析（多平台 marker + sha256 哈希）。pip 路径不受影响，
# 贡献者仍可 `make install`；CI 两条路径都验（pip 安装 + 锁一致性）。
lock:
	$(UV) lock

verify-lock:
	$(UV) lock --check

sbom:
	@mkdir -p dist
	@$(UV) export --frozen --extra postgres --extra s3 --format cyclonedx1.5 > dist/sbom.cdx.json
	@$(PYTHON) -c "import json;d=json.load(open('dist/sbom.cdx.json'));print('[sbom] dist/sbom.cdx.json', d['bomFormat'], d['specVersion'], len(d['components']), 'components')"

# 镜像层 SBOM：需要真实守护进程 + 已构建的控制面镜像，因此**不在** supply-chain 链里
# （那条要能在无 docker 的机器上跑）。产出前先确认这两点，缺任何一点都退 2 而不是静默出空文件。
image-sbom:
	./scripts/image_sbom.sh

# 镜像层漏洞扫描（OS 包那一层的盲区）：只出报告。44 条 HIGH 已分诊完，裁决是不接阈值——
# 43 条上游没发版、1 条 Debian 标 fix_deferred，接成门禁就是一条每次必红却无从修的红（SUPPLY_CHAIN §8 第 5 项）。
image-cve:
	./scripts/image_cve.sh

# 生产架构（x86_64）侧的依赖层复算：本机是 arm64，`docker build --platform` 这条路在这台机器上
# 走不通（无 buildx，legacy builder 不传 --platform），所以用 amd64 运行时直接跑同一行 uv sync。
amd64-probe:
	./scripts/probe_control_plane_amd64.sh

audit:
	@$(UV) audit --locked

supply-chain: verify-lock sbom audit
