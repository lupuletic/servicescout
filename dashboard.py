"""ServiceScout dashboard — FastAPI backend + React SPA.

Serves:

  /                 React app (graph viewer, catalog, triage, crawl progress).
  /api/state.json   Live crawl + catalog state.
  /api/entities     Searchable entity list, filterable by kind.
  /api/entity/:ref  Full record for one entity.
  /api/graph        Node/edge JSON for the Sigma graph viewer.
  /api/communications Derived service-to-service communication flows.
  /api/triage.json  Unresolved external components.
  /triage/decide    POST endpoint for triage actions.

The React app is built into `frontend/dist/` by `npm --prefix frontend run
build`. If that directory doesn't exist (e.g. fresh checkout, dev mode),
the dashboard prints a one-line message and serves a JSON shim so the API
still works.

CLI:
  python dashboard.py --catalog data/catalog.json --host 127.0.0.1 --port 8788
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles


HERE = Path(__file__).parent
DEFAULT_CATALOG = HERE / "data" / "catalog.json"
DEFAULT_EXTRACTION_LOG = HERE / "data" / "extraction_runs.jsonl"
DEFAULT_DECISIONS = HERE / "data" / "triage_decisions.jsonl"
DEFAULT_KUZU = HERE / "data" / "catalog.kuzu"
FRONTEND_DIST = HERE / "frontend" / "dist"


# ---------- data loading ----------

_CATALOG_CACHE: dict[str, Any] = {"mtime": 0.0, "payload": None}


def load_catalog(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"entities": [], "relations": [], "summary": {}}
    mtime = path.stat().st_mtime
    if _CATALOG_CACHE["payload"] is not None and _CATALOG_CACHE["mtime"] == mtime:
        return _CATALOG_CACHE["payload"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    _CATALOG_CACHE["mtime"] = mtime
    _CATALOG_CACHE["payload"] = payload
    return payload


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


# ---------- crawler status ----------

def crawler_status() -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["pgrep", "-f", "servicescout/crawler.py"],
            text=True, capture_output=True, check=False, timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return {"running": False, "pid": None, "uptime": None}
    pids = [int(p) for p in result.stdout.split() if p.strip().isdigit()]
    if not pids:
        return {"running": False, "pid": None, "uptime": None}
    pid = pids[0]
    try:
        ps = subprocess.run(["ps", "-o", "etime=", "-p", str(pid)], text=True, capture_output=True, check=False, timeout=3)
        etime = ps.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        etime = ""
    return {"running": True, "pid": pid, "uptime": etime}


def latest_crawl_log() -> tuple[Path | None, list[str]]:
    candidates: list[Path] = []
    tmp = Path("/tmp")
    if tmp.exists():
        candidates.extend(tmp.glob("crawl*.log"))
    if not candidates:
        return None, []
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        text = newest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return newest, []
    lines = text.splitlines()
    return newest, lines[-20:]


# ---------- catalog helpers ----------

def cumulative_spend(extraction_log: Path) -> float:
    total = 0.0
    for record in read_jsonl(extraction_log):
        cost = (record.get("cost") or {}).get("estimated_usd")
        if isinstance(cost, (int, float)):
            total += float(cost)
    return round(total, 2)


def recent_extractions(extraction_log: Path, n: int = 10) -> list[dict[str, Any]]:
    records = read_jsonl(extraction_log)
    out = []
    for record in records[-n:][::-1]:
        out.append({
            "repo": record.get("repo"),
            "model": record.get("model"),
            "provider": record.get("provider"),
            "cost": round(float((record.get("cost") or {}).get("estimated_usd") or 0.0), 4),
            "duration_seconds": record.get("duration_seconds"),
            "status": record.get("status"),
        })
    return out


def last_build_timestamp(catalog_path: Path) -> str | None:
    if not catalog_path.exists():
        return None
    return dt.datetime.fromtimestamp(catalog_path.stat().st_mtime, tz=dt.timezone.utc).isoformat()


def inbound_counts(relations: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for relation in relations:
        target = relation.get("to")
        if target:
            counts[target] = counts.get(target, 0) + 1
    return counts


def latest_decisions(decisions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for record in decisions:
        entity = record.get("entity")
        if entity:
            latest[entity] = record
    return latest


def _entity_brief(entity: dict[str, Any]) -> dict[str, Any]:
    meta = entity.get("metadata") or {}
    ann = meta.get("annotations") or {}
    spec = entity.get("spec") or {}
    return {
        "ref": f"{entity['kind']}:{meta.get('name','')}",
        "kind": entity["kind"],
        "name": meta.get("name", ""),
        "tagline": ann.get("tagline") or "",
        "description": meta.get("description") or "",
        "type": spec.get("type") or "",
        "system": spec.get("system") or "",
        "source_repos": ann.get("source_repos") or [],
        "environments": spec.get("environments") or [],
    }


# ---------- app factory ----------

def create_app(*, catalog_path: Path, extraction_log: Path, decisions_path: Path) -> FastAPI:
    app = FastAPI(title="ServiceScout dashboard")

    @app.get("/api/state.json")
    def api_state() -> JSONResponse:
        catalog = load_catalog(catalog_path)
        summary = catalog.get("summary") or {}
        log_path, log_tail = latest_crawl_log()
        return JSONResponse({
            "backend": "kuzu" if DEFAULT_KUZU.exists() else "json",
            "summary": {
                "entities": summary.get("entities") or 0,
                "relations": summary.get("relations") or 0,
                "repos_indexed": summary.get("repos_indexed") or 0,
                "unresolved_external_components": summary.get("unresolved_external_components") or 0,
                "derived_communication_flows": summary.get("derived_communication_flows") or 0,
                "node_kinds": summary.get("node_kinds") or {},
                "relation_types": summary.get("relation_types") or {},
            },
            "last_build_at": last_build_timestamp(catalog_path),
            "cumulative_spend_usd": cumulative_spend(extraction_log),
            "recent_extractions": recent_extractions(extraction_log, 10),
            "crawler": crawler_status(),
            "log_path": str(log_path) if log_path else None,
            "log_tail": log_tail,
            "now": dt.datetime.now(dt.timezone.utc).isoformat(),
        })

    @app.get("/api/entities")
    def api_entities(
        kind: str | None = None,
        query: str | None = None,
        owner: list[str] | None = None,
        lifecycle: list[str] | None = None,
        environment: list[str] | None = None,
        tag: list[str] | None = None,
        runtime: list[str] | None = None,
        limit: int = 500,
    ) -> JSONResponse:
        catalog = load_catalog(catalog_path)
        q = (query or "").lower().strip()
        owners_filter = set(owner or [])
        lifecycles_filter = set(lifecycle or [])
        envs_filter = set(environment or [])
        tags_filter = set(tag or [])
        runtimes_filter = set(runtime or [])
        out: list[dict[str, Any]] = []
        for entity in catalog.get("entities") or []:
            if kind and entity.get("kind") != kind:
                continue
            spec = entity.get("spec") or {}
            meta = entity.get("metadata") or {}
            if owners_filter and (spec.get("owner") or "") not in owners_filter:
                continue
            if lifecycles_filter and (spec.get("lifecycle") or "") not in lifecycles_filter:
                continue
            if envs_filter and not envs_filter.intersection(spec.get("environments") or []):
                continue
            if tags_filter and not tags_filter.intersection(meta.get("tags") or []):
                continue
            if runtimes_filter and (spec.get("runtime") or "") not in runtimes_filter:
                continue
            brief = _entity_brief(entity)
            if q:
                ann = meta.get("annotations") or {}
                haystack = " ".join([
                    brief["name"].lower(),
                    brief["tagline"].lower(),
                    brief["description"].lower(),
                    " ".join(brief["source_repos"]).lower(),
                    " ".join(ann.get("aliases") or []).lower(),
                ])
                if q not in haystack:
                    continue
            out.append(brief)
            if len(out) >= limit:
                break
        return JSONResponse({"count": len(out), "entities": out})

    @app.get("/api/facets")
    def api_facets() -> JSONResponse:
        """Distinct values per facet for the Catalog page sidebar."""
        catalog = load_catalog(catalog_path)
        kinds_count: dict[str, int] = {}
        types_count: dict[str, int] = {}
        owners_count: dict[str, int] = {}
        lifecycles_count: dict[str, int] = {}
        envs_count: dict[str, int] = {}
        tags_count: dict[str, int] = {}
        runtimes_count: dict[str, int] = {}
        for entity in catalog.get("entities") or []:
            kind = entity.get("kind") or ""
            kinds_count[kind] = kinds_count.get(kind, 0) + 1
            spec = entity.get("spec") or {}
            meta = entity.get("metadata") or {}
            t = spec.get("type") or ""
            if t:
                types_count[t] = types_count.get(t, 0) + 1
            o = spec.get("owner") or ""
            if o and o != "unknown":
                owners_count[o] = owners_count.get(o, 0) + 1
            lc = spec.get("lifecycle") or ""
            if lc and lc != "unknown":
                lifecycles_count[lc] = lifecycles_count.get(lc, 0) + 1
            for env in spec.get("environments") or []:
                envs_count[env] = envs_count.get(env, 0) + 1
            for tg in meta.get("tags") or []:
                tags_count[tg] = tags_count.get(tg, 0) + 1
            rt = spec.get("runtime") or ""
            if rt:
                runtimes_count[rt] = runtimes_count.get(rt, 0) + 1
        def sorted_facet(d: dict[str, int]) -> list[dict[str, Any]]:
            return [{"value": k, "count": v} for k, v in sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))]
        return JSONResponse({
            "kind": sorted_facet(kinds_count),
            "type": sorted_facet(types_count),
            "owner": sorted_facet(owners_count),
            "lifecycle": sorted_facet(lifecycles_count),
            "environment": sorted_facet(envs_count),
            "tag": sorted_facet(tags_count),
            "runtime": sorted_facet(runtimes_count),
        })

    @app.get("/api/entity/{ref:path}")
    def api_entity(ref: str) -> JSONResponse:
        catalog = load_catalog(catalog_path)
        for entity in catalog.get("entities") or []:
            entity_ref = f"{entity['kind']}:{entity['metadata']['name']}"
            if entity_ref == ref:
                return JSONResponse({
                    "ref": entity_ref,
                    "kind": entity["kind"],
                    "name": entity["metadata"]["name"],
                    "metadata": entity.get("metadata") or {},
                    "spec": entity.get("spec") or {},
                    "evidence": entity.get("evidence") or [],
                    "confidence": entity.get("confidence"),
                })
        return JSONResponse({"error": "not_found", "ref": ref}, status_code=404)

    @app.get("/api/graph")
    def api_graph(
        kind: list[str] | None = Query(default=None),
        limit: int = 400,
        include_orphans: bool = False,
        center: str | None = None,
        depth: int = 1,
        edge_type: list[str] | None = Query(default=None),
    ) -> JSONResponse:
        """Return nodes + edges in a Sigma-friendly shape.

        - `kind` repeated → filter by entity kinds. Default: Component + Provider + Resource.
        - `limit` → cap to top-N nodes by total degree (in+out). Edges are pruned to those whose
          source and target are both in the kept set.
        - `include_orphans=False` (default) drops nodes with zero edges to/from another kept
          node — these otherwise form a useless visual halo in force-directed layouts.
        - `center` (optional) → return the ego-graph of this entity ref at `depth` hops.
          Overrides the kind filter (we include any kind in the ego-graph).
        """
        catalog = load_catalog(catalog_path)
        relations = catalog.get("relations") or []
        edge_types_filter = set(edge_type or [])
        if edge_types_filter:
            relations = [r for r in relations if r.get("type") in edge_types_filter]
        edge_total = len(relations)

        # Ego-graph mode: BFS from `center` for `depth` hops (any kind).
        if center:
            entities_by_ref = {f"{e['kind']}:{e['metadata']['name']}": e for e in (catalog.get("entities") or [])}
            adj_out: dict[str, list[dict[str, Any]]] = {}
            adj_in: dict[str, list[dict[str, Any]]] = {}
            for r in relations:
                adj_out.setdefault(r["from"], []).append(r)
                adj_in.setdefault(r["to"], []).append(r)
            visited: set[str] = set()
            frontier = [center]
            picked_edges: list[dict[str, Any]] = []
            seen_edges: set[tuple[str, str, str]] = set()
            for _ in range(max(depth, 1)):
                next_frontier: list[str] = []
                for node in frontier:
                    if node in visited:
                        continue
                    visited.add(node)
                    for r in adj_out.get(node, []) + adj_in.get(node, []):
                        key = (r["from"], r["type"], r["to"])
                        if key in seen_edges:
                            continue
                        seen_edges.add(key)
                        picked_edges.append(r)
                        other = r["to"] if r["from"] == node else r["from"]
                        if other not in visited:
                            next_frontier.append(other)
                frontier = next_frontier
            # Include the last-hop neighbours that we touched via edges even
            # though we didn't expand them — otherwise the returned subgraph
            # has edges pointing at nodes that aren't in the node list.
            for r in picked_edges:
                visited.add(r["from"])
                visited.add(r["to"])
            ego_entities = [entities_by_ref[r] for r in sorted(visited) if r in entities_by_ref]
            edges = []
            for i, relation in enumerate(picked_edges):
                edges.append({
                    "id": f"e{i}",
                    "source": relation["from"],
                    "target": relation["to"],
                    "type": relation["type"],
                    "confidence": relation.get("confidence"),
                    "properties": relation.get("properties") or {},
                })
            nodes = []
            for entity in ego_entities:
                ann = (entity.get("metadata") or {}).get("annotations") or {}
                ref = f"{entity['kind']}:{entity['metadata']['name']}"
                nodes.append({
                    "id": ref,
                    "label": entity["metadata"]["name"],
                    "kind": entity["kind"],
                    "system": (entity.get("spec") or {}).get("system") or "",
                    "tagline": ann.get("tagline") or "",
                    "degree": sum(1 for e in picked_edges if e["from"] == ref or e["to"] == ref),
                    "is_center": ref == center,
                })
            return JSONResponse({
                "nodes": nodes,
                "edges": edges,
                "truncated": False,
                "node_total": len(nodes),
                "edge_total": len(edges),
                "include_orphans": True,
                "center": center,
                "depth": depth,
            })

        all_kinds = set(kind or ["Component", "Provider", "Resource"])
        entities = [e for e in (catalog.get("entities") or []) if e.get("kind") in all_kinds]
        node_total = len(entities)

        # Total degree (in + out), counted against entities of the visible kinds only.
        ref_for = lambda e: f"{e['kind']}:{e['metadata']['name']}"
        all_visible_refs = {ref_for(e) for e in entities}
        degree: dict[str, int] = {}
        for r in relations:
            if r["from"] in all_visible_refs and r["to"] in all_visible_refs:
                degree[r["from"]] = degree.get(r["from"], 0) + 1
                degree[r["to"]] = degree.get(r["to"], 0) + 1

        entities.sort(key=lambda e: -degree.get(ref_for(e), 0))
        kept = entities[:limit]
        kept_refs = {ref_for(e) for e in kept}

        # Build edges between kept entities.
        edges: list[dict[str, Any]] = []
        connected: set[str] = set()
        for i, relation in enumerate(relations):
            if relation["from"] in kept_refs and relation["to"] in kept_refs:
                edges.append({
                    "id": f"e{i}",
                    "source": relation["from"],
                    "target": relation["to"],
                    "type": relation["type"],
                    "confidence": relation.get("confidence"),
                    "properties": relation.get("properties") or {},
                })
                connected.add(relation["from"])
                connected.add(relation["to"])

        # Drop orphans for a cleaner force-directed picture.
        if not include_orphans:
            kept = [e for e in kept if ref_for(e) in connected]

        nodes = []
        for entity in kept:
            ann = (entity.get("metadata") or {}).get("annotations") or {}
            nodes.append({
                "id": ref_for(entity),
                "label": entity["metadata"]["name"],
                "kind": entity["kind"],
                "system": (entity.get("spec") or {}).get("system") or "",
                "tagline": ann.get("tagline") or "",
                "degree": degree.get(ref_for(entity), 0),
            })
        return JSONResponse({
            "nodes": nodes,
            "edges": edges,
            "truncated": len(kept) < node_total,
            "node_total": node_total,
            "edge_total": edge_total,
            "include_orphans": include_orphans,
        })

    @app.get("/api/communications")
    def api_communications(limit: int = 500, transport: str | None = None) -> JSONResponse:
        catalog = load_catalog(catalog_path)
        entities_by_ref = {
            f"{e['kind']}:{e['metadata']['name']}": e
            for e in (catalog.get("entities") or [])
            if e.get("kind") and e.get("metadata", {}).get("name")
        }
        flows: list[dict[str, Any]] = []
        for relation in catalog.get("relations") or []:
            if relation.get("type") != "communicatesWith":
                continue
            props = relation.get("properties") or {}
            transports = [str(t) for t in (props.get("transports") or []) if t]
            primary_transport = props.get("transport") or (transports[0] if transports else "")
            if transport and transport.lower() not in {t.lower() for t in transports + [primary_transport]}:
                continue
            source_entity = entities_by_ref.get(relation.get("from"))
            target_entity = entities_by_ref.get(relation.get("to"))
            flows.append({
                "source": relation.get("from"),
                "source_name": ((source_entity or {}).get("metadata") or {}).get("name") or relation.get("from"),
                "target": relation.get("to"),
                "target_name": ((target_entity or {}).get("metadata") or {}).get("name") or relation.get("to"),
                "endpoint": props.get("endpoint") or "",
                "endpoints": props.get("endpoints") or [],
                "transport": primary_transport,
                "transports": transports,
                "mechanism": props.get("mechanism") or "",
                "mechanisms": props.get("mechanisms") or [],
                "confidence": relation.get("confidence"),
                "evidence_count": len(relation.get("evidence") or []),
            })
        flows.sort(key=lambda f: (f["source_name"], f["target_name"], f["endpoint"]))
        return JSONResponse({"flows": flows[:limit], "count": len(flows), "limit": limit})

    @app.get("/api/triage.json")
    def api_triage() -> JSONResponse:
        catalog = load_catalog(catalog_path)
        decisions = read_jsonl(decisions_path)
        decided = latest_decisions(decisions)
        inbound = inbound_counts(catalog.get("relations") or [])
        out: list[dict[str, Any]] = []
        for entity in catalog.get("entities") or []:
            if entity.get("kind") != "Component":
                continue
            annotations = (entity.get("metadata") or {}).get("annotations") or {}
            if annotations.get("external") != "true":
                continue
            ref = f"Component:{entity['metadata']['name']}"
            if ref in decided:
                continue
            out.append({
                "ref": ref,
                "name": entity["metadata"]["name"],
                "aliases": annotations.get("aliases") or [],
                "inbound": inbound.get(ref, 0),
                "evidence": entity.get("evidence") or [],
            })
        out.sort(key=lambda r: (-r["inbound"], r["name"].lower()))
        return JSONResponse({"count": len(out), "components": out})

    @app.post("/triage/decide")
    def decide(
        entity: str = Form(...), action: str = Form(...),
        category: str = Form(""), repo: str = Form(""),
        into: str = Form(""), reviewer: str = Form(""), reason: str = Form(""),
    ) -> RedirectResponse:
        record: dict[str, Any] = {
            "entity": entity, "action": action, "reviewer": reviewer.strip(),
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        if reason.strip():
            record["reason"] = reason.strip()
        if action == "mark_external":
            record["category"] = category.strip() or "other"
        elif action == "link":
            record["repo"] = repo.strip()
        elif action == "merge":
            record["into"] = into.strip()
        elif action == "skip":
            pass
        else:
            return RedirectResponse(url="/triage", status_code=303)
        append_jsonl(decisions_path, record)
        return RedirectResponse(url="/triage", status_code=303)

    # ---- frontend (React SPA) ----
    if FRONTEND_DIST.exists() and (FRONTEND_DIST / "index.html").exists():
        # Static files (JS/CSS/etc.) under /assets.
        app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")

        @app.get("/", response_model=None)
        @app.get("/{path:path}", response_model=None)
        def spa(request: Request, path: str = ""):
            # Don't intercept API paths — FastAPI dispatches matching routes
            # before falling through to this catch-all.
            if request.url.path.startswith("/api/") or request.url.path.startswith("/triage/"):
                return JSONResponse({"error": "not_found"}, status_code=404)
            return FileResponse(str(FRONTEND_DIST / "index.html"))
    else:
        @app.get("/", response_class=HTMLResponse)
        def missing_frontend() -> HTMLResponse:
            return HTMLResponse(
                """<html><body style="font-family: ui-sans-serif, system-ui; padding: 2rem; max-width: 600px;">
                <h1>ServiceScout dashboard</h1>
                <p>The React frontend hasn't been built yet.</p>
                <pre style="background: #f4f4f5; padding: 1rem; border-radius: 6px;">
cd frontend && npm install && npm run build</pre>
                <p>Then reload. The JSON APIs at <code>/api/*</code> are already live.</p>
                </body></html>""",
                status_code=200,
            )

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--extraction-log", type=Path, default=DEFAULT_EXTRACTION_LOG)
    parser.add_argument("--decisions", type=Path, default=DEFAULT_DECISIONS)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()

    import uvicorn
    app = create_app(catalog_path=args.catalog, extraction_log=args.extraction_log, decisions_path=args.decisions)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
