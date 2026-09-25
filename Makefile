PYTHON ?= .venv/bin/python
UV ?= uv
VERSION ?= 0.4.0

.PHONY: install dev test test-pg lint typecheck build smoke clean check demo \
        control-image workspace-image gpu-preflight gpu-test \
        compose-up compose-down migrate migrate-up migrate-downgrade \
        release api-docs validate lock verify-lock sbom audit supply-chain

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

lint:
	$(PYTHON) -m ruff check app tests
	@for f in scripts/*.sh runtime/workspace-entrypoint.sh; do bash -n "$$f"; done

typecheck:
	$(PYTHON) -m mypy app

build:
	$(PYTHON) -m build

smoke:
	./scripts/smoke_api.sh

clean:
	rm -rf dist build *.egg-info .pytest_cache .mypy_cache .ruff_cache
	rm -f embodiedcloud.db test-embodiedcloud.db
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

# --- 供应链：锁文件 / SBOM / 漏洞审计 ---
# uv.lock 是 universal 解析（多平台 marker + sha256 哈希）。pip 路径不受影响，
# 贡献者仍可 `make install`；CI 两条路径都验（pip 安装 + 锁一致性）。
lock:
	$(UV) lock

verify-lock:
	$(UV) lock --check

sbom:
	@mkdir -p dist
	@$(UV) export --frozen --format cyclonedx1.5 > dist/sbom.cdx.json
	@$(PYTHON) -c "import json;d=json.load(open('dist/sbom.cdx.json'));print('[sbom] dist/sbom.cdx.json', d['bomFormat'], d['specVersion'], len(d['components']), 'components')"

audit:
	@$(UV) audit --locked

supply-chain: verify-lock sbom audit
