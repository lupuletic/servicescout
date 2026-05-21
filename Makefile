PYTHON ?= $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)
WORKSPACE ?= sock-shop
CATALOG := $(shell $(PYTHON) evals/workspace_paths.py --workspace $(WORKSPACE) | $(PYTHON) -c 'import json,sys; print(json.load(sys.stdin)["data_dir"] + "/catalog.json")')
KUZU := $(shell $(PYTHON) evals/workspace_paths.py --workspace $(WORKSPACE) | $(PYTHON) -c 'import json,sys; print(json.load(sys.stdin)["data_dir"] + "/catalog.kuzu")')
SOURCE := $(shell $(PYTHON) evals/workspace_paths.py --workspace $(WORKSPACE) | $(PYTHON) -c 'import json,sys; print(json.load(sys.stdin)["clone_root"])')
AUDIT_MD := docs/$(WORKSPACE)-catalog-audit.md

.PHONY: eval-setup eval-extract eval-kuzu eval-run eval-report eval-audit stack stack-edge stack-crawl wheelhouse docker-build docker-build-offline demo demo-down

# Zero-cost instant demo: serve a bundled, pre-extracted catalog (no LLM call,
# no credentials). Defaults to sock-shop; `make demo WORKSPACE=online-boutique`
# serves the Google Online Boutique. First run builds the image.
DEMO_CATALOG = examples/$(WORKSPACE)-catalog.json
DEMO_ENV = WORKSPACE_ROOT=$(PWD)/.demo-data SERVICESCOUT_DATA_DIR=./.demo-data \
	SERVICESCOUT_WORKSPACE_CONFIG=./workspace.json.example

demo:
	@test -f $(DEMO_CATALOG) || { echo "no bundled catalog: $(DEMO_CATALOG)"; exit 1; }
	mkdir -p .demo-data
	cp $(DEMO_CATALOG) .demo-data/catalog.json
	$(DEMO_ENV) docker compose up -d mcp dashboard
	@echo ""
	@echo "Demo running with the bundled $(WORKSPACE) catalog (no LLM, no auth):"
	@echo "  Dashboard: http://127.0.0.1:8788"
	@echo "  MCP:       http://127.0.0.1:8765/mcp"
	@echo "Stop with: make demo-down"

demo-down:
	$(DEMO_ENV) docker compose down

eval-setup:
	PYTHON=$(PYTHON) ./evals/setup.sh --workspace $(WORKSPACE)

eval-extract:
	PYTHON=$(PYTHON) ./evals/build_eval_catalog.sh --workspace $(WORKSPACE)

eval-kuzu:
	$(PYTHON) -m servicescout.build_kuzu --catalog $(CATALOG) --db $(KUZU)

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
	$(PYTHON) -m pip download -r requirements.lock -d vendor/wheels

docker-build:
	docker compose --env-file .env.$(WORKSPACE).example build

docker-build-offline:
	PIP_NO_INDEX=1 docker compose --env-file .env.$(WORKSPACE).example build
