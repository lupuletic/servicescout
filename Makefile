PYTHON ?= $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)
WORKSPACE ?= sock-shop
CATALOG := $(shell $(PYTHON) evals/workspace_paths.py --workspace $(WORKSPACE) | $(PYTHON) -c 'import json,sys; print(json.load(sys.stdin)["data_dir"] + "/catalog.json")')
KUZU := $(shell $(PYTHON) evals/workspace_paths.py --workspace $(WORKSPACE) | $(PYTHON) -c 'import json,sys; print(json.load(sys.stdin)["data_dir"] + "/catalog.kuzu")')
SOURCE := $(shell $(PYTHON) evals/workspace_paths.py --workspace $(WORKSPACE) | $(PYTHON) -c 'import json,sys; print(json.load(sys.stdin)["clone_root"])')
AUDIT_MD := docs/$(WORKSPACE)-catalog-audit.md

.PHONY: eval-setup eval-extract eval-kuzu eval-run eval-report eval-audit stack stack-edge stack-crawl wheelhouse docker-build docker-build-offline

eval-setup:
	PYTHON=$(PYTHON) ./evals/setup.sh --workspace $(WORKSPACE)

eval-extract:
	PYTHON=$(PYTHON) ./evals/build_eval_catalog.sh --workspace $(WORKSPACE)

eval-kuzu:
	$(PYTHON) build_kuzu.py --catalog $(CATALOG) --db $(KUZU)

eval-run:
	$(PYTHON) evals/runner.py --workspace $(WORKSPACE)

eval-report:
	$(PYTHON) evals/report.py --runs-dir $(shell dirname $(CATALOG))/../runs

eval-audit:
	$(PYTHON) evals/audit_catalog.py --catalog $(CATALOG) --workspace $(SOURCE) --output $(AUDIT_MD) --fail-on-high

stack:
	docker compose --env-file .env.$(WORKSPACE).example up -d mcp dashboard

stack-edge:
	docker compose --env-file .env.$(WORKSPACE).example --profile edge up -d

stack-crawl:
	docker compose --env-file .env.$(WORKSPACE).example --profile crawl run --rm crawler

wheelhouse:
	mkdir -p vendor/wheels
	$(PYTHON) -m pip download -r requirements.txt -d vendor/wheels

docker-build:
	docker compose --env-file .env.$(WORKSPACE).example build

docker-build-offline:
	PIP_NO_INDEX=1 docker compose --env-file .env.$(WORKSPACE).example build
