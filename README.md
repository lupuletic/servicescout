<div align="center">

<img src="docs/assets/servicescout-logo-readme.png" alt="ServiceScout" width="720" />

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

**2. Crawl your GitHub orgs to build the catalog:**

```bash
cp workspace.json.example workspace.json && $EDITOR workspace.json   # add your orgs
docker compose run --rm crawler                                       # extracts + embeds
python build_kuzu.py                                                  # populates the Kuzu index
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

Six tools. Designed so an agent uses the first one for any new task and
rarely needs the operator tool.

| Tool | Purpose |
|---|---|
| `servicescout_search(query, limit=8)` | Hybrid retrieval. Top entities + matched domain attributes / glossary. |
| `servicescout_describe(entity)` | One entity's full record + tagline + capability sheet. |
| `servicescout_neighbors(entity, direction="out"\|"in"\|"both", depth=1, edge_types=[...])` | One-step graph traversal. `direction="in"` answers "who depends on / consumes X". |
| `servicescout_trace(start, end=None, max_hops=6, include_async=True)` | Multi-hop journey planner. Returns CANDIDATE hops; agent verifies each in code. |
| `servicescout_evidence(source, target=None)` | File:line citations for an edge. |
| `servicescout_status()` | Read-only catalog state. |

Write operations (rebuild / embed / reconcile / build-kuzu) are intentionally
**not** exposed over MCP — they run from the CLI only.

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
database exists, JSON otherwise). The interface is the same six tools
either way.

---

## Dashboard

A React + Sigma.js dashboard ships with the project at
[`http://localhost:8788`](http://localhost:8788) (Docker) or via
`python dashboard.py`. Four pages:

- **Graph** — interactive force-directed layout of the catalog. Kind
  filters, hover-to-highlight neighbourhood, click-to-inspect.
- **Catalog** — searchable table of every entity, filterable by kind.
- **Triage** — review unresolved external components and decide
  merge / link / mark-external (the legacy POST handler is still wired).
- **Crawl** — live progress with polling — entities, relations, spend,
  recent extractions, crawler status.

Built with **Vite + React + SWR + react-router-dom + Sigma.js
(graphology)** — no TanStack — and packaged into the same single
Docker image (`Dockerfile` runs `npm run build` automatically). The
FastAPI backend serves the built `frontend/dist/` and exposes
`/api/state.json`, `/api/entities`, `/api/entity/:ref`, `/api/graph`,
`/api/triage.json`.

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
k=60) and exposes six tools for agents to navigate the result.

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

See `.env.example` for the full list. The first-run wizard
(`python init_wizard.py`) walks you through everything interactively.

---

## Status

Alpha. The pipeline works end-to-end and has been validated on a real
~200-repo workspace producing a Backstage-shaped catalog served to coding
agents. Expect rough edges in the operator path:

- `crawler_state.json` is written but not yet read for resume — restart is
  full-recrawl.
- MCP server binds `127.0.0.1` with no auth (fine for local agents; auth
  for hosted deployments is on the roadmap).
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
