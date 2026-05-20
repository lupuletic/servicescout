<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./assets/servicescout-logo-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="./assets/servicescout-logo-light.png">
    <img alt="ServiceScout" src="./assets/servicescout-logo-light.png" width="720">
  </picture>
</p>

<div align="center">

# ServiceScout

**Org-wide code context for AI coding agents. One MCP server, every repo.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Made for MCP](https://img.shields.io/badge/MCP-compatible-7c3aed)](https://modelcontextprotocol.io)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue)](https://www.python.org)
[![Status: alpha](https://img.shields.io/badge/status-alpha-orange)](#status)

</div>

In a multi-repo enterprise, AI coding agents struggle the moment a question
crosses one repo. They grep the open repo, guess names, or fabricate
connections. ServiceScout fixes that by giving them an evidence-backed graph
of every service, API, datastore, queue, and dependency in the org — built
automatically from source code, and served over MCP.

Ask:

> *"How does checkout work end to end?"*

Your agent calls ServiceScout, finds the storefront, BFF, backend services,
queues, databases, and owners involved, clones the right repos into context,
and answers with file:line citations from real code.

---

## Quickstart

**1. Run the server (Docker, HTTP streamable on `:8765`):**

```bash
git clone https://github.com/lupuletic/servicescout.git
cd servicescout
cp .env.example .env  # fill in WORKSPACE_ROOT, GOOGLE_CLOUD_PROJECT
docker compose up -d mcp dashboard
```

Open the operator UI at [`http://127.0.0.1:8788`](http://127.0.0.1:8788).

Workspace state is selected by env vars:

- `WORKSPACE_ROOT`: mounted source checkout root.
- `SERVICESCOUT_WORKSPACE_CONFIG`: org/exclusion config used by crawler/scheduler.
- `SERVICESCOUT_DATA_DIR`: persistent catalog, Kuzu DB, run logs, and decisions.

Keeping `SERVICESCOUT_DATA_DIR` different per workspace is what prevents an
eval crawl from overwriting your real catalog.

**2. Crawl your GitHub orgs to build the catalog:**

```bash
cp workspace.json.example workspace.json && $EDITOR workspace.json   # add your orgs
docker compose run --rm crawler                                       # extracts + embeds
```

The crawler is auto-convergent: it clones repos referenced by the catalog,
re-runs extraction, and stops when no new repos are discovered. Budget cap
in `.env` (`BUDGET_USD=100` by default) is a hard stop.

**3. Wire it up to your coding agent (one line):**

```bash
curl -fsSL https://raw.githubusercontent.com/lupuletic/servicescout/main/install.sh | bash
```

Interactive — asks which agent (Claude Code / Codex / both) and the MCP
URL (default `http://127.0.0.1:8765/mcp`). Installs the `servicescout` and
`journey` skills into `~/.claude/skills/` **and** registers the MCP server
with your chosen agent(s).

Headless / scripted:

```bash
curl -fsSL https://raw.githubusercontent.com/lupuletic/servicescout/main/install.sh \
  | bash -s -- --agent both --url http://127.0.0.1:8765/mcp --yes
```

Or from a local checkout: `./install.sh` (same flags).

Under the hood it runs the equivalent of:

```bash
claude mcp add servicescout --scope user --transport http http://127.0.0.1:8765/mcp
codex  mcp add servicescout --url http://127.0.0.1:8765/mcp
```

Restart the agent. Ask: *"trace the login flow end-to-end"* and watch it
call `servicescout_search` → `servicescout_trace` → clone repos → answer
with citations.

### Remote one-box setup

For a new VM or workstation where you want the full product behind one HTTP
port, use the bundled nginx edge profile:

```bash
git clone https://github.com/lupuletic/servicescout.git
cd servicescout
cp .env.example .env
$EDITOR .env                 # set WORKSPACE_ROOT, credentials, SERVICESCOUT_HTTP_BIND
cp workspace.json.example workspace.json
$EDITOR workspace.json       # add orgs / exclusions
docker compose --profile scheduler --profile edge up -d --build
```

That starts a self-contained Compose network:

- `edge` nginx exposes `${SERVICESCOUT_HTTP_BIND:-127.0.0.1:8080}`.
- `dashboard` serves the React operator UI and API.
- `mcp` serves streamable HTTP MCP at `/mcp`.
- `scheduler` keeps the catalog fresh and writes run logs for Activity.
- `${SERVICESCOUT_DATA_DIR:-./data}` is the persistent catalog/Kuzu/run-log
  volume.

After the stack is up:

```bash
docker compose run --rm crawler
```

Then open `http://<host>:<port>/` for the UI or point agents at
`http://<host>:<port>/mcp`. Put TLS/auth in front with your normal load
balancer or reverse proxy; the bundled nginx is intentionally a small internal
edge, not an identity provider.

### Switch between workspaces and public evals

The repo includes a pinned Weaveworks sock-shop eval workspace under
`evals/workspace` and an isolated catalog under `evals/data`. You can run the
same dashboard/MCP stack against it without touching your main catalog:

```bash
make eval-setup WORKSPACE=sock-shop
make eval-extract WORKSPACE=sock-shop
python build_kuzu.py --catalog evals/data/catalog.json --db evals/data/catalog.kuzu
docker compose --env-file .env.socks-shop.example up -d mcp dashboard
open http://127.0.0.1:8790
python evals/runner.py --workspace sock-shop
```

Watch extraction progress in the terminal: every repo prints
`==> extracting microservices-demo/<repo>` and ends with an `EXTRACTOR_RESULT`
line containing status, duration, estimated cost, and output path.

For a one-port remote-style sock-shop stack:

```bash
docker compose --env-file .env.socks-shop.example --profile edge up -d --build
open http://127.0.0.1:8081
```

The second public benchmark is Google Online Boutique / `microservices-demo`.
It is isolated under `evals/workspaces/online-boutique`:

```bash
make eval-setup WORKSPACE=online-boutique
make eval-extract WORKSPACE=online-boutique
make eval-kuzu WORKSPACE=online-boutique
make eval-run WORKSPACE=online-boutique
make eval-audit WORKSPACE=online-boutique
docker compose --env-file .env.online-boutique.example up -d mcp dashboard
open http://127.0.0.1:8792
```

For a one-port remote-style Online Boutique stack:

```bash
docker compose --env-file .env.online-boutique.example --profile edge up -d --build
open http://127.0.0.1:8082
```

To switch back to your main/private workspace, use your normal `.env` or a
copied `.env.main` based on `.env.main.example`:

```bash
docker compose --env-file .env.main up -d mcp dashboard
```

The two workspaces stay separate as long as these pairs stay separate:

| Workspace | Source root | Data root | Workspace config |
| --- | --- | --- | --- |
| Main/private | `WORKSPACE_ROOT=/Users/.../work` | `SERVICESCOUT_DATA_DIR=./data` | `SERVICESCOUT_WORKSPACE_CONFIG=./workspace.json` |
| sock-shop evals | `WORKSPACE_ROOT=./evals/workspace` | `SERVICESCOUT_DATA_DIR=./evals/data` | `SERVICESCOUT_WORKSPACE_CONFIG=./evals/workspace_orgs.json` |
| online-boutique evals | `WORKSPACE_ROOT=./evals/workspaces/online-boutique/workspace` | `SERVICESCOUT_DATA_DIR=./evals/workspaces/online-boutique/data` | `SERVICESCOUT_WORKSPACE_CONFIG=./evals/workspaces/online-boutique/workspace_orgs.json` |

The example env files also use different local ports:

- Main/private: dashboard `127.0.0.1:8788`, MCP `127.0.0.1:8765`.
- sock-shop: dashboard `127.0.0.1:8790`, MCP `127.0.0.1:8791`.
- online-boutique: dashboard `127.0.0.1:8792`, MCP `127.0.0.1:8793`.

---

## What you get

- **A Backstage-shaped catalog** of every Component, API, Resource, System,
  Domain, and Group across your orgs — built from source code, not
  hand-maintained YAML.
- **Hybrid retrieval** (BM25 IDF + dense embeddings, fused with Reciprocal
  Rank Fusion) so any natural-language prompt routes to the right repos.
- **Multi-hop journey planning** including async messaging chains
  (`producesMessage` → topic → `consumesMessage`), so your agent can trace
  flows that span 15+ services.
- **Evidence at every edge** — every relation carries `file:line` citations
  the agent can verify in source.
- **Two ready-made Claude Code skills**: `servicescout` (routing) and
  `journey` (multi-hop tracing).

---

## MCP tool surface

Eight tools. Designed so an agent uses the first one for any new task and
rarely needs the operator tool.

| Tool | Purpose |
|---|---|
| `servicescout_search(query, limit=8)` | Hybrid retrieval. Top entities + matched domain attributes / glossary. |
| `servicescout_describe(entity)` | One entity's full record + tagline + capability sheet. |
| `servicescout_neighbors(entity, direction="out"\|"in"\|"both", depth=1, edge_types=[...])` | One-step graph traversal. `direction="in"` answers "who depends on / consumes X". |
| `servicescout_trace(start, end=None, max_hops=6, include_async=True)` | Multi-hop journey planner. Returns CANDIDATE hops; agent verifies each in code. |
| `servicescout_evidence(source, target=None)` | File:line citations for an edge. |
| `servicescout_glossary(term, limit=20)` | Vocabulary lookup across component capability sheets. |
| `servicescout_owners(entity)` | Fast owner/lifecycle lookup without fetching the full entity. |
| `servicescout_status()` | Read-only catalog state. |

Write operations are intentionally **not** exposed over MCP. Operators can
trigger crawls from the dashboard Activity page; maintenance jobs still run
through Compose/CLI.

---

## Pluggable storage backends

ServiceScout's MCP server reads from a `Backend` — pick the one that fits
your environment. New backends drop into `storage.py` behind the same
interface.

| Backend | When to use | Setup |
|---|---|---|
| **KuzuDB** *(recommended)* | Embedded graph DB with HNSW vector index + BM25 FTS. Persistent, fast at any size. | `pip install kuzu` (already in `requirements.txt`); run `python build_kuzu.py` after each crawl. |
| **JSON** | Zero-dependency fallback. Reads `data/catalog.json` into memory. Fine up to ~50k entities. | No setup — just point at `data/catalog.json`. |

Select with `--backend kuzu|json|auto` (default `auto` — Kuzu when a
database exists, JSON otherwise). The interface is the same eight tools
either way.

---

## Dashboard

A React + Sigma.js dashboard ships with the project at
[`http://localhost:8788`](http://localhost:8788) (Docker) or via
`python dashboard.py`. Primary pages:

- **Explorer** — interactive force-directed layout of the catalog. Kind,
  edge, and confidence filters, hover-to-highlight neighbourhood,
  click-to-inspect.
- **Catalog** — searchable entity table with facets for kind, owner,
  lifecycle, runtime, environment, tag, type, and confidence.
- **Activity** — scheduler status, extraction/crawl run history, drilldown
  into each run, and trigger-now control.
- **Operator** — cost trendline, verifier signal, staleness heatmap, and
  stale repo queue.
- **Triage** — disconfirmed verifier facts, owner assignment, terminal
  decisions, external-component decisions, and decision log.

Built with **Vite + React + SWR + react-router-dom + Sigma.js
(graphology)** — no TanStack — and packaged into the same single
Docker image (`Dockerfile` runs `npm run build` automatically). The
FastAPI backend serves the built `frontend/dist/` and exposes
`/api/state.json`, `/api/entities`, `/api/entity/:ref`, `/api/graph`,
`/api/triage.json`, `/api/triage/facts`, `/api/crawl/*`, and
`/api/operator/summary`.

---

## How it works

```
  GitHub orgs ──► crawler ──► LLM extractor ──► JSON-schema validation
                                                       │
                                                       ▼
                              build_catalog ─► identity reconciliation
                                                       │
                                                       ▼
                              embed_catalog ─► dense embeddings (Gemini)
                                                       │
                                                       ▼
                              build_kuzu    ─► Kuzu index (FTS + HNSW vectors)
                                                       │
                                                       ▼
                                                  MCP server ──► AI coding agent
```

The extractor runs `codex` or `claude` CLI in non-interactive mode against
each repo, asks for a Backstage-shaped JSON document with `file:line`
evidence, and emits it through a strict JSON schema. The build step merges
per-repo extractions into a single catalog and reconciles aliases (service
names that appear as hostnames, config keys, or generated client classes).
The embed step adds Gemini embeddings to each entity. The Kuzu build step
loads the catalog into an embedded graph DB with a BM25 FTS index and an
HNSW vector index. The MCP server fuses dense + lexical retrieval (RRF,
k=60) and exposes eight tools for agents to navigate the result.

---

## Configuration

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `WORKSPACE_ROOT` | yes | — | Path where the crawler clones repos. |
| `LLM_PROVIDER` | yes | `codex` | Extractor harness: `codex` or `claude`. |
| `LLM_MODEL` | yes | `gpt-5.4-mini` | Model name passed to the harness. |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | one of | — | Credential for the harness. Skip if your CLI is already logged in. |
| `GOOGLE_CLOUD_PROJECT` | optional | — | GCP project for Vertex AI embeddings. Empty disables embeddings (lexical-only fallback). |
| `BUDGET_USD` | optional | `100` | Hard cost cap per crawl. |
| `SERVICESCOUT_HTTP_BIND` | optional | `127.0.0.1:8080` | nginx edge bind address for remote Compose deployments. |
| `CRAWL_INTERVAL_MINUTES` | optional | `360` | Continuous scheduler interval. |
| `CRAWL_TICK_BUDGET_USD` | optional | `20` | Hard cost cap per scheduler tick. |

See `.env.example` for the full list. The first-run wizard
(`python init_wizard.py`) walks you through everything interactively.
For the first controlled indexing run on a private estate, follow
[`docs/controlled-indexing-runbook.md`](docs/controlled-indexing-runbook.md).

### Corporate TLS / package mirrors

Docker builds install Python and Node packages. In networks with TLS
inspection or package-policy blocks, add local CA PEM files under `certs/`
before building; the Dockerfile splits multi-certificate bundles and trusts
each certificate. If your network blocks public package indexes entirely,
set the usual Docker build environment or mirror variables (`PIP_INDEX_URL`,
`PIP_EXTRA_INDEX_URL`, `PIP_TRUSTED_HOST`, `NPM_CONFIG_REGISTRY`, proxy
variables) before running Compose. For fully offline or allowlisted builds,
populate `vendor/wheels/` on a network that can reach your package source:

```bash
python -m pip download -r requirements.txt -d vendor/wheels
PIP_NO_INDEX=1 docker compose build
```

Equivalent Make targets:

```bash
make wheelhouse
make docker-build-offline WORKSPACE=online-boutique
```

The runtime stack can still be started from a pre-built image with
`docker compose up -d --no-build mcp dashboard`.

---

## Status

Alpha. The pipeline works end-to-end and has been validated on a real
~200-repo workspace producing a Backstage-shaped catalog served to coding
agents. Expect rough edges in the operator path:

- `crawler_state.json` is written but not yet read for resume — restart is
  full-recrawl.
- MCP/dashboard have no built-in user auth and the dashboard can trigger
  crawls using mounted repo/LLM credentials. Keep the default localhost bind
  for SSH-tunnel use, or put the bundled nginx edge behind your normal VPN,
  firewall, SSO proxy, or load balancer before binding to a public interface.
- GitHub only — GitLab, Bitbucket, and GitHub Enterprise discovery are
  planned.

Issues and PRs welcome.

---

## Roadmap

- **Embedding providers.** Today: Gemini (Vertex AI). Planned: OpenAI,
  Voyage, Cohere, Sentence-Transformers, and local models behind a single
  pluggable provider interface.
- **Extraction harnesses.** Today: `codex` and `claude` CLIs. Planned:
  Cursor agent mode, Cline, Continue, and direct OpenAI / Anthropic API
  invocation for environments without the CLIs.
- **Source-control hosts.** Today: GitHub. Planned: GitLab, Bitbucket,
  GitHub Enterprise.
- **Runtime correlation.** OpenTelemetry / Datadog / service-mesh
  integrations to enrich the catalog with live topology.
- **Resumeable crawl** + **eval suite** for the extractor.
- **Hosted MCP** with auth, for orgs that want a managed deployment.

---

## License

MIT. See [LICENSE](LICENSE).
