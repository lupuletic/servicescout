# Release readiness notes

## Current recommendation

ServiceScout is ready for a public `v0.1.0` alpha release after the responsive
dashboard polish lands. It should not be called `v1.0` yet: the extraction
pipeline is useful and eval-backed, but broader enterprise accuracy, auth, and
packaging hardening still need more mileage.

## Repo hierarchy scan

The large local tree is mostly generated state and dependencies:

- `.venv/`, `frontend/node_modules/`, `data/`, `logs/`, `legacy/`, `__pycache__/`,
  and eval workspace/data folders are ignored runtime or development artifacts.
- Python implementation now lives under `servicescout/`; root remains focused
  on docs, config, Compose/Docker, install scripts, evals, and frontend assets.
  CLI entrypoints use `python -m servicescout.<module>`.
- The eval harness is reasonably isolated under `evals/`; generated eval data
  and cloned workspaces are ignored.
- Docs are useful but could be reorganized before a `v1.0` into
  `docs/evals/socks-shop/`, `docs/evals/online-boutique/`, and
  `docs/operations/`.

Recommended cleanup before `v0.1.0`:

- Remove local `.DS_Store`, cache, log, and old runtime directories before
  creating the release archive.
- Keep `certs/` empty in git except `.gitkeep`; real CA bundles stay local.
- Decide whether `.mcp.json` should stay as a convenience sample or move to
  docs. It currently points to localhost only and contains no secret.
- Replace personal repository/image namespace references with the final public
  owner before publishing.

Recommended cleanup before `v1.0`:

- Move Python modules into a package such as `servicescout/`.
- Keep root-level files to project metadata, Compose, Docker, Makefile, README,
  and thin CLI wrappers.
- Split public user docs from internal eval/audit notes.

## First release shape

Target tags:

- GitHub release: `v0.1.0`
- Container image: `ghcr.io/<owner>/servicescout:v0.1.0`
- Optional Docker Hub mirror: `<owner>/servicescout:v0.1.0`
- Avoid relying on `latest` in setup docs except for quick local experiments.

Minimum release gates:

- Python tests pass.
- Frontend build passes.
- Frontend e2e smoke passes.
- Docker image builds.
- Compose smoke starts `mcp`, `dashboard`, and optional nginx `edge`.
- Online Boutique README validation remains green.
- Dashboard manual smoke covers Explorer, Catalog, Activity, Operator, Triage,
  and an entity detail page.

## Self-hosted setup target

The desired new-machine flow should be:

```bash
git clone https://github.com/<owner>/servicescout.git
cd servicescout
cp .env.example .env
cp workspace.json.example workspace.json
$EDITOR .env
$EDITOR workspace.json
docker compose pull
docker compose --profile edge up -d
```

Expected endpoints:

- UI: `http://<host>:8080/`
- MCP: `http://<host>:8080/mcp`
- Persistent state: `${SERVICESCOUT_DATA_DIR:-./data}`
- Scheduler: controlled from Activity in the dashboard.

Security posture for the first release:

- Bind to localhost by default.
- For remote use, run behind VPN/firewall or an existing authenticated reverse
  proxy.
- MCP exposes read-only catalog tools; crawl/re-index controls stay in the
  dashboard/API.
- Built-in auth can be added after the internal/VPN deployment model is proven.
