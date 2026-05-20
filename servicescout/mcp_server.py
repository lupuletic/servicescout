"""ServiceScout MCP server.

Eight tools for AI coding agents — exposed over stdio or streamable-http MCP.
The server is a thin shim over a pluggable storage Backend; see `storage.py`
for the JSONBackend (zero-deps in-memory) and `storage_kuzu.py` for the
KuzuBackend (embedded graph DB with HNSW + FTS).

Tools:
  servicescout_search     — hybrid retrieval (BM25 + dense embeddings + RRF).
  servicescout_describe   — full record for one entity.
  servicescout_neighbors  — graph traversal in / out / both.
  servicescout_trace      — multi-hop journey plan with async chains.
  servicescout_evidence   — file:line citations on an edge.
  servicescout_glossary   — direct vocab lookup across all components.
  servicescout_owners     — owner / lifecycle lookup for one entity.
  servicescout_status     — read-only catalog state.

Write operations (rebuild / embed / push) are CLI-only — not exposed over MCP.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from servicescout.storage import Backend, DEFAULT_FLOW_EDGE_TYPES, make_backend
from servicescout import auth as auth_module


HERE = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = HERE / "data" / "catalog.json"
DEFAULT_KUZU = HERE / "data" / "catalog.kuzu"


# ---------- query-time embedding ----------

def embed_query(text: str, *, project: str | None, location: str, model: str, dim: int) -> list[float] | None:
    """Embed the user query via Vertex AI / Gemini. Returns None when ADC or
    project is not configured — the backend then falls back to lexical-only."""
    try:
        from google import genai
        from google.genai.types import EmbedContentConfig
    except ImportError:
        return None
    try:
        client = genai.Client(vertexai=True, project=project, location=location) if project else genai.Client()
        response = client.models.embed_content(
            model=model,
            contents=[text],
            config=EmbedContentConfig(output_dimensionality=dim, task_type="RETRIEVAL_QUERY"),
        )
        return list(response.embeddings[0].values)
    except Exception:  # noqa: BLE001
        return None


# ---------- server ----------

def build_server(
    backend: Backend,
    *,
    project: str | None,
    location: str,
    embed_model: str,
    embed_dim: int,
    http_config: dict[str, Any] | None = None,
) -> Any:
    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError as exc:
        raise SystemExit("Install servicescout/requirements.txt to get the MCP SDK") from exc

    http_config = http_config or {}
    mcp = FastMCP(
        "ServiceScout",
        json_response=True,
        host=http_config.get("host", "127.0.0.1"),
        port=int(http_config.get("port", 8765)),
        streamable_http_path=http_config.get("path", "/mcp"),
        stateless_http=True,
    )

    @mcp.tool()
    def servicescout_search(query: str, limit: int = 8, min_confidence: str | None = None) -> dict[str, Any]:
        """Find the most relevant entities for a free-text prompt. Recommended entrypoint.

        Hybrid retrieval: Reciprocal Rank Fusion (k=60) over dense embedding similarity
        and BM25-style lexical scoring, with the fused score weighted by the entity's
        catalog confidence (high=1.0, medium=0.7, low=0.4, review=0.1, unspecified=0.5).
        Returns the top-N Components / APIs / Resources / Providers with score, tagline,
        source repos, confidence, and any matched domain_attribute or glossary entries.

        min_confidence: optional 'high' | 'medium' | 'low' | 'review' — drop entities below
        this confidence threshold entirely. Default: include everything (down-weighted).

        To walk dependencies from a hit, call servicescout_neighbors or servicescout_trace.
        To get evidence for an edge, call servicescout_evidence.
        """
        qv = embed_query(query, project=project, location=location, model=embed_model, dim=embed_dim)
        top = backend.search(query, query_vector=qv, limit=limit, min_confidence=min_confidence)
        return {
            "query": query,
            "limit": limit,
            "backend": backend.name,
            "vector_search_available": qv is not None,
            "top_entities": top,
            "source_repos": sorted({repo for hit in top for repo in hit.get("source_repos") or []}),
        }

    @mcp.tool()
    def servicescout_describe(entity: str) -> dict[str, Any]:
        """Return one entity's full record: metadata, spec, annotations, evidence.

        Accepts a fully-qualified ref (e.g. `Component:checkout-api`) or a name like
        `checkout-api` or an alias / hostname.
        """
        found = backend.fuzzy_lookup(entity)
        if not found:
            return {"error": "not_found", "needle": entity}
        return {
            "ref": f"{found['kind']}:{found['metadata']['name']}",
            "kind": found["kind"],
            "metadata": found.get("metadata", {}),
            "spec": found.get("spec", {}),
            "evidence": found.get("evidence", []),
            "confidence": found.get("confidence"),
        }

    @mcp.tool()
    def servicescout_neighbors(
        entity: str,
        direction: str = "out",
        depth: int = 1,
        edge_types: list[str] | None = None,
        min_confidence: str | None = None,
    ) -> dict[str, Any]:
        """One-step (or multi-step) graph traversal in any direction.

        Args:
          entity: entity ref or name (Component:foo, foo, or an alias).
          direction: 'out' (what this entity depends on / publishes),
                     'in'  (what depends on this entity / consumes from it),
                     'both' (union of in + out).
          depth: how many hops to expand. Default 1 = direct neighbours only.
          edge_types: optional filter, e.g. ['consumesApi'] or ['consumesMessage'].

        Use direction='in' to answer "who depends on X" or "who consumes this
        topic/queue/resource" — questions that pure outgoing traversal cannot
        answer. For multi-hop journey planning with async chaining, use
        servicescout_trace instead.
        """
        found = backend.fuzzy_lookup(entity)
        if not found:
            return {"error": "not_found", "needle": entity}
        if direction not in {"out", "in", "both"}:
            return {"error": "invalid_direction", "valid": ["out", "in", "both"]}
        ref = f"{found['kind']}:{found['metadata']['name']}"
        paths = backend.neighbors(ref, direction=direction, depth=max(depth, 1), edge_types=edge_types, min_confidence=min_confidence)
        return {
            "start": ref,
            "direction": direction,
            "depth": depth,
            "edge_count": len(paths),
            "paths": paths,
        }

    @mcp.tool()
    def servicescout_trace(
        start: str,
        end: str | None = None,
        max_hops: int = 6,
        edge_types: list[str] | None = None,
        include_async: bool = True,
        fanout_per_node: int = 12,
        min_confidence: str | None = None,
    ) -> dict[str, Any]:
        """Plan a multi-hop business-journey traversal across the KG. CANDIDATE hops only.

        Use this for prompts that span multiple services end-to-end: 'trace the login flow',
        'how does a placed order get fulfilled', 'what services touch a refund'. Returns
        ordered candidate hops with edge metadata; the agent MUST verify each hop in code
        before including it in an answer.

        Args:
          start: free-text description ('user clicks login button') OR an entity ref ('Component:account').
          end: optional terminal — entity ref, or 'kind:Resource' to stop at any datastore/topic.
          max_hops: BFS depth cap. Default 6. Bump for very long async chains (e.g. 10-12).
          edge_types: which edges to follow. Defaults to communicatesWith plus
                      the lower-level API/message/resource dependency edges. Pass
                      a subset to focus traversal.
          include_async: when a producesMessage edge lands on a topic, also follow downstream
                         consumesMessage edges (turns synchronous traces into full async-aware traces).
          fanout_per_node: cap branches per edge type per node so the trace stays tractable.
                           Default 12.

        Returns:
          {start_resolved, end_condition, hops[], terminal_nodes[], hop_count, agent_instructions}
        """
        types = edge_types or DEFAULT_FLOW_EDGE_TYPES
        # Resolve start.
        start_entity = backend.fuzzy_lookup(start)
        if start_entity is None:
            qv = embed_query(start, project=project, location=location, model=embed_model, dim=embed_dim)
            top = backend.search(start, query_vector=qv, limit=1)
            if not top:
                return {"error": "start_not_found", "start": start}
            start_ref = top[0]["ref"]
        else:
            start_ref = f"{start_entity['kind']}:{start_entity['metadata']['name']}"
        # Resolve end.
        end_match = end
        if end and ":" not in end and not end.startswith("kind:"):
            ent = backend.fuzzy_lookup(end)
            if ent:
                end_match = f"{ent['kind']}:{ent['metadata']['name']}"
        plan = backend.trace(
            start_ref=start_ref,
            end_match=end_match,
            max_hops=max_hops,
            edge_types=types,
            include_async=include_async,
            fanout_per_node=fanout_per_node,
            min_confidence=min_confidence,
        )
        start_ent = backend.describe(start_ref)
        start_annotations = (start_ent.get("metadata", {}).get("annotations") if start_ent else {}) or {}
        return {
            "start": start,
            "start_resolved": {
                "ref": start_ref,
                "kind": start_ent.get("kind") if start_ent else None,
                "tagline": start_annotations.get("tagline", ""),
                "source_repos": start_annotations.get("source_repos", []),
            },
            "end_condition": end_match,
            "max_hops": max_hops,
            "include_async": include_async,
            "edge_types": types,
            "hops": plan["hops"],
            "terminal_nodes": plan["terminal_nodes"],
            "hop_count": len(plan["hops"]),
            "agent_instructions": (
                "These hops are CANDIDATES, not verified facts. For each hop, call "
                "servicescout_evidence(from, to) to get file:line citations, then read at "
                "least one cited file in the source repo to confirm the edge is real. Reject "
                "any hop you can't confirm. Produce a verified chain with file:line evidence "
                "at every step. When presenting the result, distinguish confirmed-in-code "
                "hops from KG-only hops explicitly."
            ),
        }

    @mcp.tool()
    def servicescout_evidence(source: str, target: str | None = None) -> dict[str, Any]:
        """Return file:line evidence for relations from `source` (optionally narrowed to `target`)."""
        src = backend.fuzzy_lookup(source)
        if not src:
            return {"error": "source_not_found", "source": source}
        src_ref = f"{src['kind']}:{src['metadata']['name']}"
        tgt_ref = None
        if target:
            tgt = backend.fuzzy_lookup(target)
            if not tgt:
                return {"error": "target_not_found", "target": target}
            tgt_ref = f"{tgt['kind']}:{tgt['metadata']['name']}"
        return {
            "source": src_ref,
            "target": tgt_ref,
            "edges": backend.evidence(src_ref, tgt_ref),
        }

    @mcp.tool()
    def servicescout_glossary(term: str, limit: int = 20) -> dict[str, Any]:
        """Look up a domain term across every Component's glossary.

        Cheaper than `servicescout_search` when you already have a candidate
        term and just want its definition + which services use it. Returns
        every glossary entry whose `term` or any `synonym` contains the
        needle (case-insensitive substring match).

        Args:
          term: the word or phrase to look up (e.g. "shipping-task",
                "cartId", "tenant", "checkout session"). Matched against
                every glossary entry's term and synonyms.
          limit: cap the number of results. Default 20.

        Use this BEFORE `servicescout_search` when the agent already knows
        a domain word and wants the precise definition + owning service,
        not a ranked list of components. Use `servicescout_describe` for
        the full record of a specific component named in a hit.
        """
        if not term or not term.strip():
            return {"error": "empty_term"}
        needle = term.strip().lower()
        hits: list[dict[str, Any]] = []
        for entity in backend.list_entities(kind="Component", limit=10000):
            ref = entity.get("ref")
            full = backend.describe(ref) if ref else None
            if not full:
                continue
            spec = full.get("spec") or {}
            for entry in (spec.get("glossary") or []):
                if not isinstance(entry, dict):
                    continue
                t = (entry.get("term") or "").lower()
                synonyms = [s.lower() for s in (entry.get("synonyms") or []) if isinstance(s, str)]
                definition = (entry.get("definition") or "").lower()
                hay = " ".join([t] + synonyms + [definition])
                if needle not in hay:
                    continue
                # Score: exact term match > synonym match > definition match.
                if t == needle:
                    score = 3
                elif needle in synonyms or t == needle.replace(" ", "-") or t == needle.replace("-", " "):
                    score = 2
                elif needle in t:
                    score = 2
                elif any(needle in s for s in synonyms):
                    score = 2
                else:
                    score = 1
                hits.append({
                    "score": score,
                    "term": entry.get("term", ""),
                    "definition": entry.get("definition", ""),
                    "synonyms": entry.get("synonyms") or [],
                    "owning_component": entity.get("name"),
                    "owning_ref": ref,
                })
                if len(hits) >= limit * 4:
                    break
            if len(hits) >= limit * 4:
                break
        hits.sort(key=lambda h: (-h["score"], h["term"]))
        return {
            "term": term,
            "match_count": len(hits),
            "results": hits[:limit],
        }

    @mcp.tool()
    def servicescout_owners(entity: str) -> dict[str, Any]:
        """Return ownership + lifecycle metadata for one entity.

        Cheaper than `servicescout_describe` when the agent only needs to
        answer "who do I open a PR against?" or "is this service deprecated?".
        Doesn't return evidence, glossary, dependencies, or domain attributes —
        just the smallest record that answers ownership questions.

        Args:
          entity: entity ref (e.g. `Component:orders`) or a name / alias.

        Returns: owning team / individual (from CODEOWNERS, catalog-info.yaml,
                 package.json author, pom.xml developers, etc.), lifecycle
                 (production / experimental / deprecated / unknown),
                 system, domain, source_repos, and last_indexed_sha.
                 Returns `{error: 'not_found'}` if the entity isn't in the
                 catalog.
        """
        found = backend.fuzzy_lookup(entity)
        if not found:
            return {"error": "not_found", "needle": entity}
        md = found.get("metadata") or {}
        spec = found.get("spec") or {}
        annotations = md.get("annotations") or {}
        return {
            "ref": f"{found['kind']}:{md.get('name')}",
            "kind": found["kind"],
            "owner": annotations.get("owner") or spec.get("owner") or "unknown",
            "lifecycle": spec.get("lifecycle") or annotations.get("lifecycle") or "unknown",
            "system": spec.get("system") or annotations.get("system") or "",
            "domain": spec.get("domain") or annotations.get("domain") or "",
            "source_repos": annotations.get("source_repos") or [],
            "last_indexed_sha": annotations.get("last_indexed_sha") or annotations.get("commit") or "",
            "tagline": annotations.get("tagline") or "",
        }

    @mcp.tool()
    def servicescout_status(kind: str | None = None, list_entities: bool = False, limit: int = 200) -> dict[str, Any]:
        """Read-only catalog introspection.

        Args:
          kind: optional filter when listing entities (Component|API|Resource|Provider|System|Domain|Group).
          list_entities: when True, also return a list of entity refs and names.
          limit: cap the entity list.

        Returns catalog path, mtime, summary counts, embedding state, vector-search
        availability, and optionally an entity list. Write actions (rebuild / embed /
        export) are intentionally NOT exposed here — run them from the CLI.
        """
        out = dict(backend.status())
        if list_entities:
            out["entries"] = backend.list_entities(kind=kind, limit=int(limit))
            out["count"] = len(out["entries"])
        return out

    return mcp


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG,
                        help="Path to data/catalog.json (used by the JSON backend).")
    parser.add_argument("--kuzu-db", type=Path, default=DEFAULT_KUZU,
                        help="Path to the Kuzu database (used by the Kuzu backend).")
    parser.add_argument("--backend", choices=("auto", "json", "kuzu"), default="auto",
                        help="Storage backend: 'auto' (Kuzu when db exists, else JSON), 'json', 'kuzu'.")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--path", default="/mcp")
    parser.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT") or None)
    parser.add_argument("--location", default=os.getenv("GOOGLE_CLOUD_LOCATION") or "us-central1")
    parser.add_argument("--embed-model", default="gemini-embedding-001")
    parser.add_argument("--embed-dim", type=int, default=768)
    parser.add_argument("--auth-issuer", default=os.getenv("MCP_AUTH_ISSUER") or None,
                        help="OIDC issuer URL. When set, every MCP tool call requires a "
                             "valid bearer token; responses are filtered to entities the "
                             "caller's teams own. Default: no auth (127.0.0.1 sidecar mode).")
    parser.add_argument("--auth-audience", default=os.getenv("MCP_AUTH_AUDIENCE") or "",
                        help="Required JWT audience claim. Only meaningful with --auth-issuer.")
    parser.add_argument("--auth-teams-claim", default=os.getenv("MCP_AUTH_TEAMS_CLAIM") or "groups",
                        help="JWT claim name containing the caller's team / group identifiers. "
                             "Default: `groups`.")
    args = parser.parse_args()

    auth_config = auth_module.AuthConfig.from_env()
    if args.auth_issuer:
        auth_config.enabled = True
        auth_config.issuer = args.auth_issuer
    if args.auth_audience:
        auth_config.audience = args.auth_audience
    if args.auth_teams_claim:
        auth_config.teams_claim = args.auth_teams_claim
    if auth_config.enabled:
        print(f"servicescout: auth enabled, issuer={auth_config.issuer}", file=sys.stderr)

    backend = make_backend(args.backend, args.catalog, kuzu_path=args.kuzu_db)
    print(f"servicescout: backend={backend.name}", file=sys.stderr)

    mcp = build_server(
        backend,
        project=args.project,
        location=args.location,
        embed_model=args.embed_model,
        embed_dim=args.embed_dim,
        http_config={"host": args.host, "port": args.port, "path": args.path},
    )
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
