.PHONY: demo test check control-image workspace-image

demo:
	./scripts/run_demo.sh

test:
	pytest

check:
	python -m compileall -q app
	@for f in scripts/*.sh runtime/workspace-entrypoint.sh; do bash -n "$$f"; done
	pytest

control-image:
	docker build -f runtime/Dockerfile.control-plane -t embodiedcloud/control-plane:0.1.0 .

workspace-image:
	./scripts/build_workspace_image.sh
