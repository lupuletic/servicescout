# Controlled Indexing Runbook

Use this for the first private-estate ServiceScout run after the public
benchmarks are green.

## Preconditions

- Pick a small, representative repo set first: 10-25 services with known
  request, API, resource, and ownership patterns.
- Keep a dedicated workspace root and data root for this run. Do not reuse
  public eval paths or an existing production catalog path.
- Confirm package access for Docker builds:
  - normal internet access, or
  - approved `PIP_INDEX_URL` / `NPM_CONFIG_REGISTRY`, or
  - a populated `vendor/wheels/` plus `PIP_NO_INDEX=1`.
- Confirm at least one extractor auth path:
  - mounted Codex/Claude CLI auth, or
  - provider API key in the env file.

## Setup

Create a private env file from the main example:

```bash
cp .env.main.example .env.private
```

Set these values:

```bash
WORKSPACE_ROOT=/absolute/path/to/private-workspace
SERVICESCOUT_DATA_DIR=./data/private-first-run
SERVICESCOUT_WORKSPACE_CONFIG=./workspace.private.json
SERVICESCOUT_DASHBOARD_BIND=127.0.0.1:8788
SERVICESCOUT_MCP_BIND=127.0.0.1:8765
SERVICESCOUT_HTTP_BIND=127.0.0.1:8080
BUDGET_USD=50
CRAWL_TICK_BUDGET_USD=10
```

Create `workspace.private.json` with the selected org/repo discovery scope and
any exclusions. Keep private paths and org names out of committed files.

## Security Posture

The bundled stack is intended for an internal network or SSH tunnel. It does
not implement application-level auth yet, and the dashboard trigger endpoint
can start crawler work with the mounted GitHub, cloud, and LLM credentials.
Keep `SERVICESCOUT_HTTP_BIND=127.0.0.1:8080` unless the host is protected by
VPN/firewall/SSO proxy. For shared deployments, prefer read-only credential
mounts, a dedicated service account, and short-lived tokens where possible.

Do not expose `/mcp` or the dashboard directly to the public internet. The
nginx edge adds a small internal reverse proxy and security headers, but TLS,
identity, and network policy should come from the deployment environment.

## Run

Build and start the operator stack:

```bash
docker compose --env-file .env.private build
docker compose --env-file .env.private up -d mcp dashboard
```

Run the first crawl explicitly rather than enabling the scheduler:

```bash
docker compose --env-file .env.private --profile crawl run --rm crawler
```

Open the dashboard:

```bash
open http://127.0.0.1:8788
```

## Validation Gates

Before expanding the scope:

- Operator shows the expected repo count, extraction cost, staleness, and
  verifier signal.
- Activity has one run per extracted repo or a scheduler tick record.
- Catalog component count roughly matches deployable/runtime units.
- Resource entities are databases, caches, queues, topics, buckets, indexes,
  or configuration stores, not services.
- Provider entities are external services/platform dependencies.
- A sample of high-confidence relations has source evidence and correct
  direction.
- Triage has no high-severity verifier failures.
- MCP `servicescout_search`, `servicescout_describe`, `servicescout_neighbors`,
  and `servicescout_trace` return useful results for known journeys.

## Audit

Run a source audit before using the graph for broad engineering work:

```bash
python evals/audit_catalog.py \
  --catalog ./data/private-first-run/catalog.json \
  --workspace /absolute/path/to/private-workspace \
  --output docs/private-first-run-catalog-audit.local.md \
  --fail-on-high
```

Do not commit private audit output. Use it to decide whether fixes are generic
pipeline bugs, workspace-seed gaps, or source-documentation gaps.

## Expansion Criteria

Expand to the next repo batch only when:

- Audit has no high findings.
- Medium findings are understood and either accepted, fixed generically, or
  captured as required workspace seed/context inputs.
- Runtime smoke for dashboard, MCP, and nginx edge passes.
- Extraction cost per repo is within the expected range for the chosen model.

## Rollback

Stop the stack without deleting data:

```bash
docker compose --env-file .env.private down
```

Archive or delete the isolated data root if the run should not be reused:

```bash
tar -czf servicescout-private-first-run-data.tgz ./data/private-first-run
rm -rf ./data/private-first-run
```

Never overwrite the public eval data or a previous private run while debugging.
