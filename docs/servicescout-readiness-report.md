# ServiceScout Readiness Report

Date: 2026-05-20

## Status

ServiceScout is ready for a controlled next indexing run from the current
pipeline and UI/MCP harness, with one deployment caveat: local image rebuilds
in a package-filtered corporate network can fail unless a package mirror or
allowed PyPI/npm access is configured.

## Completion Audit

| Requirement | Evidence | Status |
| --- | --- | --- |
| No benchmark or private-org hardcoding | Generic `WORKSPACE` plumbing in `Makefile`, `evals/workspace_paths.py`, `evals/setup.sh`, `evals/build_eval_catalog.sh`, `evals/runner.py`, and `repo_discovery.py`; open-source hygiene scan excludes generated/ignored clones and returns no private-org matches. | PASS |
| Preserve Sock Shop as workspace 1 | Sock Shop remains under `evals/workspace` and `evals/data`; regression report `evals/runs/sock_shop_regression_20260520_095633.json`; audit `docs/sock-shop-catalog-audit.md`. | PASS |
| Add Online Boutique as workspace 2 | `evals/workspaces/online-boutique/workspace.json`, `workspace_orgs.json`, `questions.yaml`, and `workspace.lock.json`; generated clone/data/runs are ignored. | PASS |
| Repeatable setup/extract/catalog/Kuzu/eval/audit/stack commands | `Makefile` targets: `eval-setup`, `eval-extract`, `eval-kuzu`, `eval-run`, `eval-report`, `eval-audit`, `stack`, `stack-edge`, `stack-crawl`; documented in `README.md` and `evals/README.md`. | PASS |
| Sock Shop remains green | `evals/runs/sock_shop_regression_20260520_095633.json`; catalog eval 18/18; `docs/sock-shop-catalog-audit.md` has 0 high findings. | PASS |
| Online Boutique extraction end to end | `evals/workspaces/online-boutique/data/catalog.json`, `catalog.kuzu`, `extraction_runs.jsonl`, and per-unit extraction records for 12 focused units. | PASS |
| Online Boutique source-audited quality | `docs/online-boutique-manual-verification.md`; `docs/online-boutique-catalog-audit.md`; audit summary: 150 facts, 124 pass, 26 review, 0 fail, 0 high findings. | PASS |
| Generic fixes only | Generic repo-unit discovery, focus-path prompting, provider/resource duplicate handling, suffix duplicate merge, and generic search boosting changes; no benchmark-specific extractor branches. | PASS |
| Tests for generic bugs | `tests/test_repo_discovery.py`, `tests/test_catalog_reconciliation.py`, `tests/test_storage_search.py`, `tests/test_dashboard_api.py`, frontend e2e coverage. Full unit suite: 182 tests OK; frontend e2e: 5 tests OK. | PASS |
| UI readiness | Local current-code dashboard at `127.0.0.1:8792` reports Online Boutique through Kuzu, Operator summary, and Activity extraction runs. | PASS |
| MCP readiness | Local current-code MCP at `127.0.0.1:8793` uses Kuzu and exposes 8 tools. | PASS |
| Docker Compose runtime | Compose config renders for main, Sock Shop, and Online Boutique. Online Boutique runtime was smoke-tested with dashboard/MCP and nginx edge against the selected workspace data. | PASS |
| Docker Compose clean build | `docker compose up --build` reaches dependency installation, but local network returns HTTP 403 for PyPI simple-index requests. Dockerfile now supports split CA bundles, package mirror build args, and an offline `vendor/wheels/` wheelhouse. `.github/workflows/ci.yml` adds the clean-machine Docker build and Compose smoke gate for GitHub Actions. | BLOCKED LOCALLY; CI GATE ADDED |
| Persistent isolated volumes | `.env.*.example` files use separate `WORKSPACE_ROOT`, `SERVICESCOUT_DATA_DIR`, and `SERVICESCOUT_WORKSPACE_CONFIG`; `.gitignore` and `evals/.gitignore` keep generated data local. | PASS |
| README/.env runbook | `README.md`, `evals/README.md`, `.env.example`, `.env.main.example`, `.env.socks-shop.example`, `.env.online-boutique.example`. | PASS |
| Final risk list | See "Remaining Risks" below. | PASS |

## Verified Workspaces

| Workspace | Catalog | Eval | Audit | Manual review |
| --- | --- | --- | --- | --- |
| Sock Shop | 44 entities, 76 relations | 18/18 pass | 0 high findings | `docs/socks-shop-manual-verification.md` |
| Online Boutique | 47 entities, 108 relations | 10/10 pass | 0 high findings | `docs/online-boutique-manual-verification.md` |

Online Boutique is isolated under `evals/workspaces/online-boutique`. Sock Shop
remains isolated under `evals/workspace` and `evals/data`.

## Deployment Smoke

Validated:

- Compose config renders for main, Sock Shop, and Online Boutique env files.
- Online Boutique dashboard and MCP stack starts via Compose with isolated
  `WORKSPACE_ROOT`, `SERVICESCOUT_DATA_DIR`, and workspace config.
- nginx edge profile proxies dashboard API and MCP at the workspace-specific
  HTTP bind.
- GitHub Actions workflow `.github/workflows/ci.yml` builds the Docker image
  from scratch on `ubuntu-latest`, starts dashboard/MCP/nginx with a fixture
  catalog, and curls both dashboard and MCP endpoints.
- Repository Actions are expected to run the Docker and Compose smoke gate
  after the release branch is pushed.
- Local current-code dashboard on `127.0.0.1:8792` reports 12 extraction runs
  in Activity and 12 repos / 47 entities / 108 relations in Operator.
- Local current-code MCP on `127.0.0.1:8793` uses the Kuzu backend and exposes
  8 tools.
- Security review assumes internal/VPN-only access for now. The Compose edge
  now binds to localhost by default, nginx emits basic browser hardening
  headers, and the runbook calls out that the dashboard/API trigger can use
  mounted repo/cloud/LLM credentials.

Build caveat:

- A clean `docker compose up --build` was attempted. The Docker build reached
  Python dependency installation but the local network returned HTTP 403 for
  PyPI simple-index requests. The Dockerfile now splits local CA bundles
  correctly and Compose accepts `PIP_INDEX_URL`, `PIP_EXTRA_INDEX_URL`,
  `PIP_TRUSTED_HOST`, `PIP_NO_INDEX`, `PIP_FIND_LINKS`, and
  `NPM_CONFIG_REGISTRY` build args for restricted networks. It also supports
  an offline wheelhouse under `vendor/wheels/`.

## Verification Commands

```bash
make eval-setup WORKSPACE=online-boutique
make eval-extract WORKSPACE=online-boutique
make eval-kuzu WORKSPACE=online-boutique
make eval-run WORKSPACE=online-boutique
make eval-audit WORKSPACE=online-boutique

docker compose --env-file .env.online-boutique.example config --quiet
docker compose --env-file .env.online-boutique.example up -d mcp dashboard
docker compose --env-file .env.online-boutique.example --profile edge up -d
```

## Remaining Risks

- Private-estate runs should start as controlled indexing with audit gates,
  not an unattended all-org crawl.
- The dashboard and MCP intentionally have no app-level auth in this alpha.
  Keep localhost/SSH tunnel access or deploy behind VPN/firewall/SSO before
  exposing the nginx edge.
- Domain and ownership quality still depends on source evidence, descriptors,
  CODEOWNERS, or workspace seeds where code alone is ambiguous.
- Docker builds in package-filtered networks require an approved package mirror,
  a populated `vendor/wheels/` wheelhouse, or pre-built image promotion.
- The scheduler path is smoke-tested at API/UI level, but a long-running
  production scheduler soak is still a separate operational check.
