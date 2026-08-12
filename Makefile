PYTHON ?= .venv/bin/python
UV ?= uv
VERSION ?= 0.3.0

.PHONY: install dev test lint typecheck build smoke clean check demo \
        control-image workspace-image gpu-preflight gpu-test \
        compose-up compose-down migrate migrate-up migrate-downgrade \
        release api-docs validate

install:
	python3.12 -m venv .venv
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -e '.[dev]'

dev:
	$(PYTHON) -m uvicorn app.main:app --reload --port 8000

test:
	$(PYTHON) -m pytest

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
