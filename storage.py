"""Pluggable storage backends for the ServiceScout MCP server.

Two backends ship today:

  - JSONBackend  — zero-dependency, reads `data/catalog.json` into memory
                   on init. Fine up to ~50k entities. Default.
  - KuzuBackend  — embedded graph DB (KuzuDB). Persistent, on-disk vector
                   and FTS indexes, Cypher-queryable. Recommended for
                   larger catalogs and production use.

The MCP server is a thin shim over the Backend interface, so adding a new
backend (Memgraph, SQLite, in-memory NetworkX) means implementing this
interface and registering it in `make_backend()`.

The Backend interface is small on purpose — the heavy lifting (hybrid
retrieval, async-aware BFS, fuzzy lookup) is implemented by each backend
in whatever native primitives it has.
"""

from __future__ import annotations

import abc
import math
import re
from pathlib import Path
from typing import Any, Iterable


DEFAULT_FLOW_EDGE_TYPES = [
    "communicatesWith",
    "consumesApi", "consumesMessage", "producesMessage",
    "dependsOn", "readsResource", "writesResource",
]

SEARCHABLE_KINDS = {"Component", "API", "Resource", "Provider"}

_KIND_PRIORITY = {"Component": 0, "Provider": 1, "Resource": 2, "API": 3, "System": 4, "Domain": 5, "Group": 6}

# Confidence weighting and ordering. Used by search/neighbors/trace to
# down-weight (or filter out) low-confidence entities and edges. None means
# "unspecified" — treated as somewhere between low and medium.
CONFIDENCE_WEIGHT = {"high": 1.0, "medium": 0.7, "low": 0.4, "review": 0.1, None: 0.5}
_CONFIDENCE_ORDER = {"review": 0, "low": 1, None: 1, "medium": 2, "high": 3}


def _kind_rank(entity: dict[str, Any]) -> int:
    return _KIND_PRIORITY.get(entity.get("kind", ""), 9)


def _confidence_at_least(c: str | None, threshold: str | None) -> bool:
    """True iff confidence c meets or exceeds threshold. None threshold → always True."""
    if not threshold:
        return True
    return _CONFIDENCE_ORDER.get(c, 1) >= _CONFIDENCE_ORDER.get(threshold, 1)


def _confidence_weight(c: str | None) -> float:
    return CONFIDENCE_WEIGHT.get(c, CONFIDENCE_WEIGHT[None])


def _search_terms(query: str) -> list[str]:
    terms = [t.lower() for t in re.split(r"[^A-Za-z0-9_-]+", query) if len(t) > 2]
    expanded = set(terms)
    if any(t.startswith("async") for t in terms):
        expanded.update({"async", "asynchronous", "asynchronously", "message", "messages", "queue", "broker"})
    if any(t in {"message", "messages", "queue", "queues", "topic", "topics", "broker", "brokers"} for t in terms):
        expanded.update({"message", "messages", "queue", "queues", "topic", "topics", "broker", "publish", "consume"})
    return sorted(expanded)


def _exact_term_boost(entity: dict[str, Any], terms: list[str]) -> float:
    if not terms:
        return 0.0
    generic = {
        "service", "services", "component", "components", "system", "systems",
        "api", "apis", "resource", "resources", "provider", "providers",
        "which", "what", "where", "does", "from", "into", "with", "uses",
    }
    wanted = {t.lower() for t in terms if t and t.lower() not in generic}
    meta = entity.get("metadata", {}) or {}
    spec = entity.get("spec", {}) or {}
    annotations = meta.get("annotations", {}) or {}
    candidates = [
        meta.get("name", ""),
        spec.get("technology", ""),
        spec.get("host", ""),
        spec.get("type", ""),
        *list(annotations.get("aliases") or []),
        *list(annotations.get("env_keys") or []),
    ]
    normalized = {str(value).lower() for value in candidates if value}
    normalized.update({re.sub(r"[^a-z0-9]+", "", value) for value in normalized})
    return 1.0 if wanted & normalized else 0.0


# --------------------------------------------------------------------------- #
# Abstract interface
# --------------------------------------------------------------------------- #


class Backend(abc.ABC):
    name: str = "abstract"

    @abc.abstractmethod
    def status(self) -> dict[str, Any]:
        """Catalog metadata — counts, mtime, embedding state."""

    @abc.abstractmethod
    def list_entities(self, *, kind: str | None, limit: int) -> list[dict[str, Any]]:
        """Lightweight entity refs for the operator tool."""

    @abc.abstractmethod
    def describe(self, ref: str) -> dict[str, Any] | None:
        """Full record (metadata, spec, evidence) for a single ref."""

    @abc.abstractmethod
    def fuzzy_lookup(self, needle: str) -> dict[str, Any] | None:
        """Resolve free text (name, alias, hostname, or kind:name) to one entity."""

    @abc.abstractmethod
    def search(
        self,
        query: str,
        *,
        query_vector: list[float] | None,
        limit: int,
        min_confidence: str | None = None,
    ) -> list[dict[str, Any]]:
        """Hybrid retrieval. RRF over dense + lexical rankings, fused score
        multiplied by entity confidence weight; entities below min_confidence
        are dropped."""

    @abc.abstractmethod
    def neighbors(
        self,
        ref: str,
        *,
        direction: str,
        depth: int,
        edge_types: list[str] | None,
        min_confidence: str | None = None,
    ) -> list[dict[str, Any]]:
        """One-step (or multi-step) graph traversal in/out/both. Edges below
        min_confidence are skipped."""

    @abc.abstractmethod
    def trace(
        self,
        *,
        start_ref: str,
        end_match: str | None,
        max_hops: int,
        edge_types: list[str],
        include_async: bool,
        fanout_per_node: int,
        min_confidence: str | None = None,
    ) -> dict[str, Any]:
        """Multi-hop journey planner with optional async messaging chains.
        Hops along edges below min_confidence are pruned."""

    @abc.abstractmethod
    def evidence(self, src_ref: str, tgt_ref: str | None) -> list[dict[str, Any]]:
        """Edges + evidence between two entities (or all outgoing from src)."""


# --------------------------------------------------------------------------- #
# JSONBackend — zero-dependency in-memory implementation
# --------------------------------------------------------------------------- #


class JSONBackend(Backend):
    name = "json"

    def __init__(self, catalog_path: Path) -> None:
        import json
        self.catalog_path = catalog_path
        self._mtime = 0.0
        self._catalog: dict[str, Any] = {}
        self._entity_index: dict[str, dict[str, Any]] = {}
        self._by_source: dict[str, list[dict[str, Any]]] = {}
        self._by_target: dict[str, list[dict[str, Any]]] = {}
        self._haystacks: list[str] = []
        self._candidates: list[dict[str, Any]] = []
        self._json = json
        self._reload()

    def _reload(self) -> None:
        if not self.catalog_path.exists():
            self._catalog = {"entities": [], "relations": [], "summary": {}}
            self._mtime = 0.0
        else:
            mtime = self.catalog_path.stat().st_mtime
            if mtime == self._mtime and self._catalog:
                return
            self._catalog = self._json.loads(self.catalog_path.read_text(encoding="utf-8"))
            self._mtime = mtime
        entities = self._catalog.get("entities") or []
        self._entity_index = {f"{e['kind']}:{e['metadata']['name']}": e for e in entities}
        self._by_source.clear()
        self._by_target.clear()
        for relation in self._catalog.get("relations") or []:
            self._by_source.setdefault(relation["from"], []).append(relation)
            self._by_target.setdefault(relation["to"], []).append(relation)
        self._candidates = [e for e in entities if e.get("kind") in SEARCHABLE_KINDS]
        self._haystacks = [_entity_haystack(e) for e in self._candidates]

    def _maybe_reload(self) -> None:
        if not self.catalog_path.exists():
            return
        mtime = self.catalog_path.stat().st_mtime
        if mtime != self._mtime:
            self._reload()

    # -- status / list -----------------------------------------------------

    def status(self) -> dict[str, Any]:
        self._maybe_reload()
        return {
            "backend": self.name,
            "catalog_path": str(self.catalog_path),
            "exists": self.catalog_path.exists(),
            "mtime": self._mtime or None,
            "summary": self._catalog.get("summary", {}),
            "embedding_present": any(e.get("embedding") for e in self._candidates),
            "vector_search_available": True,
        }

    def list_entities(self, *, kind: str | None, limit: int) -> list[dict[str, Any]]:
        self._maybe_reload()
        out = []
        for e in self._catalog.get("entities") or []:
            if kind and e["kind"] != kind:
                continue
            out.append({
                "ref": f"{e['kind']}:{e['metadata']['name']}",
                "kind": e["kind"],
                "name": e["metadata"]["name"],
                "type": e.get("spec", {}).get("type", ""),
                "system": e.get("spec", {}).get("system", ""),
            })
            if len(out) >= limit:
                break
        return out

    # -- describe / fuzzy_lookup ------------------------------------------

    def describe(self, ref: str) -> dict[str, Any] | None:
        self._maybe_reload()
        return self._entity_index.get(ref)

    def fuzzy_lookup(self, needle: str) -> dict[str, Any] | None:
        self._maybe_reload()
        if not needle:
            return None
        if ":" in needle:
            ent = self._entity_index.get(needle)
            if ent:
                return ent
        needle_lower = needle.lower()
        entities = self._catalog.get("entities") or []
        exact = [e for e in entities if e["metadata"]["name"].lower() == needle_lower]
        if exact:
            exact.sort(key=_kind_rank)
            return exact[0]
        contains = [e for e in entities if needle_lower in e["metadata"]["name"].lower()]
        if contains:
            contains.sort(key=lambda e: (_kind_rank(e), len(e["metadata"]["name"])))
            return contains[0]
        matches_alias = [
            e for e in entities
            if needle_lower in {a.lower() for a in (e["metadata"].get("annotations", {}).get("aliases") or [])}
        ]
        if matches_alias:
            matches_alias.sort(key=_kind_rank)
            return matches_alias[0]
        return None

    # -- search ------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        query_vector: list[float] | None,
        limit: int,
        min_confidence: str | None = None,
    ) -> list[dict[str, Any]]:
        self._maybe_reload()
        if not self._candidates:
            return []
        terms = _search_terms(query)
        lex_scores = _idf_lexical_scores(self._haystacks, terms)
        vec_scores: dict[int, float] = {}
        if query_vector:
            for i, entity in enumerate(self._candidates):
                emb = entity.get("embedding")
                if emb:
                    v = _cosine(query_vector, emb)
                    if v > 0:
                        vec_scores[i] = v
        rankings = []
        if vec_scores:
            rankings.append([i for i, _ in sorted(vec_scores.items(), key=lambda kv: -kv[1])])
        if lex_scores:
            rankings.append([i for i, _ in sorted(lex_scores.items(), key=lambda kv: -kv[1])])
        if not rankings:
            return []
        fused = _rrf(rankings)
        # Confidence-aware: multiply each fused score by entity confidence
        # weight (high=1.0, medium=0.7, low=0.4, review=0.1, unspecified=0.5).
        # Drop entities below min_confidence entirely.
        weighted: dict[int, float] = {}
        for idx, score in fused.items():
            ent_conf = self._candidates[idx].get("confidence")
            if not _confidence_at_least(ent_conf, min_confidence):
                continue
            weighted[idx] = (score * _confidence_weight(ent_conf)) + _exact_term_boost(self._candidates[idx], terms)
        scored = [(s, vec_scores.get(i, 0.0), lex_scores.get(i, 0.0), self._candidates[i])
                  for i, s in sorted(weighted.items(), key=lambda kv: -kv[1])]
        out = []
        for rrf_score, vec, lex, entity in scored[:limit]:
            out.append(_hit_record(entity, rrf_score, vec, lex, terms))
        return out

    # -- neighbors --------------------------------------------------------

    def neighbors(
        self,
        ref: str,
        *,
        direction: str,
        depth: int,
        edge_types: list[str] | None,
        min_confidence: str | None = None,
    ) -> list[dict[str, Any]]:
        self._maybe_reload()
        visited: set[str] = set()
        frontier = [ref]
        paths: list[dict[str, Any]] = []
        for _ in range(max(depth, 1)):
            next_frontier: list[str] = []
            for cur in frontier:
                if cur in visited:
                    continue
                visited.add(cur)
                if direction in {"out", "both"}:
                    for rel in self._by_source.get(cur, []):
                        if edge_types and rel["type"] not in edge_types:
                            continue
                        if not _confidence_at_least(rel.get("confidence"), min_confidence):
                            continue
                        paths.append(_edge_record(rel["from"], rel["to"], "out", rel))
                        next_frontier.append(rel["to"])
                if direction in {"in", "both"}:
                    for rel in self._by_target.get(cur, []):
                        if edge_types and rel["type"] not in edge_types:
                            continue
                        if not _confidence_at_least(rel.get("confidence"), min_confidence):
                            continue
                        paths.append(_edge_record(rel["from"], rel["to"], "in", rel))
                        next_frontier.append(rel["from"])
            frontier = next_frontier
        return paths

    # -- trace ------------------------------------------------------------

    def trace(
        self,
        *,
        start_ref: str,
        end_match: str | None,
        max_hops: int,
        edge_types: list[str],
        include_async: bool,
        fanout_per_node: int,
        min_confidence: str | None = None,
    ) -> dict[str, Any]:
        self._maybe_reload()

        def tagline_for(r: str) -> str:
            ent = self._entity_index.get(r)
            if not ent:
                return ""
            ann = ent["metadata"].get("annotations") or {}
            return ann.get("tagline") or (ent["metadata"].get("description") or "")[:160]

        def matches_end(r: str) -> bool:
            if not end_match:
                return False
            if end_match.startswith("kind:"):
                wanted = end_match.split(":", 1)[1]
                ent = self._entity_index.get(r)
                return bool(ent and ent.get("kind") == wanted)
            return r == end_match

        def is_message_resource(r: str) -> bool:
            ent = self._entity_index.get(r)
            if not ent or ent.get("kind") != "Resource":
                return False
            return ent.get("spec", {}).get("type") in {"virtual-topic", "topic", "queue", "consumer-queue", "stream"}

        hops: list[dict[str, Any]] = []
        terminal_nodes: set[str] = set()
        next_seq = [1]
        frontier: list[tuple[str, str, int, frozenset[str]]] = [(start_ref, "0", 0, frozenset({start_ref}))]
        while frontier:
            next_frontier: list[tuple[str, str, int, frozenset[str]]] = []
            for node_ref, branch_id, current_depth, path in frontier:
                if current_depth >= max_hops:
                    terminal_nodes.add(node_ref)
                    continue
                raw = [
                    r for r in self._by_source.get(node_ref, [])
                    if r["type"] in edge_types
                    and _confidence_at_least(r.get("confidence"), min_confidence)
                ]
                if not raw:
                    terminal_nodes.add(node_ref)
                    continue
                by_type: dict[str, list[dict[str, Any]]] = {}
                for r in raw:
                    by_type.setdefault(r["type"], []).append(r)
                outgoing: list[dict[str, Any]] = []
                for et in edge_types:
                    outgoing.extend((by_type.get(et) or [])[:fanout_per_node])
                for i, relation in enumerate(outgoing):
                    target_ref = relation["to"]
                    if target_ref in path:
                        continue
                    if i == 0:
                        new_branch = branch_id
                    else:
                        new_branch = f"{branch_id}.{next_seq[0]}"
                        next_seq[0] += 1
                    hops.append({
                        "step": len(hops) + 1,
                        "branch_id": new_branch,
                        "depth": current_depth + 1,
                        "from": node_ref,
                        "to": target_ref,
                        "edge_type": relation["type"],
                        "evidence_count": len(relation.get("evidence") or []),
                        "confidence": relation.get("confidence"),
                        "to_tagline": tagline_for(target_ref),
                    })
                    if matches_end(target_ref):
                        terminal_nodes.add(target_ref)
                        continue
                    new_path = path | {target_ref}
                    next_frontier.append((target_ref, new_branch, current_depth + 1, new_path))
                    if include_async and relation["type"] == "producesMessage" and is_message_resource(target_ref):
                        for consumer_rel in self._by_target.get(target_ref, []):
                            if consumer_rel["type"] != "consumesMessage":
                                continue
                            if not _confidence_at_least(consumer_rel.get("confidence"), min_confidence):
                                continue
                            consumer_ref = consumer_rel["from"]
                            if consumer_ref in new_path:
                                continue
                            async_branch = f"{new_branch}.async{next_seq[0]}"
                            next_seq[0] += 1
                            hops.append({
                                "step": len(hops) + 1,
                                "branch_id": async_branch,
                                "depth": current_depth + 2,
                                "from": target_ref,
                                "to": consumer_ref,
                                "edge_type": "consumesMessage",
                                "evidence_count": len(consumer_rel.get("evidence") or []),
                                "confidence": consumer_rel.get("confidence"),
                                "to_tagline": tagline_for(consumer_ref),
                                "async": True,
                            })
                            next_frontier.append((consumer_ref, async_branch, current_depth + 2, new_path | {consumer_ref}))
            frontier = next_frontier
        return {"hops": hops, "terminal_nodes": sorted(terminal_nodes)}

    # -- evidence ---------------------------------------------------------

    def evidence(self, src_ref: str, tgt_ref: str | None) -> list[dict[str, Any]]:
        self._maybe_reload()
        out = []
        for rel in self._by_source.get(src_ref, []):
            if tgt_ref and rel["to"] != tgt_ref:
                continue
            out.append({
                "from": rel["from"],
                "to": rel["to"],
                "type": rel["type"],
                "confidence": rel.get("confidence"),
                "evidence": rel.get("evidence") or [],
                "properties": rel.get("properties") or {},
            })
        return out


# --------------------------------------------------------------------------- #
# Shared helpers — used by JSONBackend and (with adaptation) KuzuBackend
# --------------------------------------------------------------------------- #


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _idf_lexical_scores(haystacks: list[str], terms: list[str]) -> dict[int, float]:
    """BM25-style smoothed IDF over precomputed haystacks."""
    if not terms or not haystacks:
        return {}
    n = len(haystacks) or 1
    df = {t: sum(1 for h in haystacks if t in h) for t in terms}
    idf = {t: math.log((n - df[t] + 0.5) / (df[t] + 0.5) + 1.0) for t in terms}
    scores: dict[int, float] = {}
    for i, h in enumerate(haystacks):
        s = sum(idf[t] for t in terms if t in h)
        if s > 0:
            scores[i] = s
    return scores


def _rrf(rankings: list[list[int]], k: int = 60) -> dict[int, float]:
    out: dict[int, float] = {}
    for ranking in rankings:
        for rank, idx in enumerate(ranking):
            out[idx] = out.get(idx, 0.0) + 1.0 / (k + rank + 1)
    return out


def _entity_haystack(entity: dict[str, Any]) -> str:
    meta = entity.get("metadata", {})
    spec = entity.get("spec", {})
    annotations = meta.get("annotations", {})
    parts: list[str] = [
        meta.get("name", ""),
        meta.get("description", ""),
        " ".join(meta.get("tags") or []),
        " ".join(annotations.get("aliases") or []),
        annotations.get("tagline", "") or "",
        spec.get("type", ""),
        spec.get("technology", ""),
        spec.get("host", ""),
        spec.get("access", ""),
        spec.get("system", ""),
        spec.get("category", "") or "",
        " ".join(annotations.get("env_keys") or []),
        annotations.get("datasource_url", "") or "",
        annotations.get("messaging_pattern", "") or "",
        " ".join(spec.get("environments") or []),
    ]
    for attr in spec.get("domain_attributes") or []:
        if not isinstance(attr, dict):
            continue
        parts.append(attr.get("attribute", "") or "")
        parts.extend([v for v in (attr.get("values") or []) if isinstance(v, str)])
        parts.append(attr.get("meaning", "") or "")
    for term in spec.get("glossary") or []:
        if not isinstance(term, dict):
            continue
        parts.append(term.get("term", "") or "")
        parts.append(term.get("definition", "") or "")
        parts.extend([s for s in (term.get("synonyms") or []) if isinstance(s, str)])
    return " ".join(parts).lower()


def _hit_record(entity: dict[str, Any], rrf_score: float, vec: float, lex: float, terms: list[str]) -> dict[str, Any]:
    annotations = entity["metadata"].get("annotations", {})
    spec = entity.get("spec", {})
    matched_attrs = []
    for attr in (spec.get("domain_attributes") or [])[:30]:
        if not isinstance(attr, dict):
            continue
        haystack = (attr.get("attribute", "") + " " + " ".join(attr.get("values") or [])).lower()
        if any(t in haystack for t in terms):
            matched_attrs.append({
                "attribute": attr.get("attribute", ""),
                "values": (attr.get("values") or [])[:8],
                "meaning": attr.get("meaning", ""),
            })
    matched_glossary = []
    for term in (spec.get("glossary") or [])[:40]:
        if not isinstance(term, dict):
            continue
        haystack = (term.get("term", "") + " " + " ".join(term.get("synonyms") or [])).lower()
        if any(t in haystack for t in terms):
            matched_glossary.append({
                "term": term.get("term", ""),
                "definition": term.get("definition", ""),
            })
    return {
        "kind": entity["kind"],
        "name": entity["metadata"]["name"],
        "ref": f"{entity['kind']}:{entity['metadata']['name']}",
        "type": spec.get("type", ""),
        "system": spec.get("system", ""),
        "tagline": annotations.get("tagline", ""),
        "description": entity["metadata"].get("description", ""),
        "tags": entity["metadata"].get("tags", []),
        "confidence": entity.get("confidence"),
        "score": round(rrf_score, 4),
        "vector_score": round(vec, 4),
        "lexical_score": round(lex, 4),
        "source_repos": annotations.get("source_repos", []),
        "matched_domain_attributes": matched_attrs[:4],
        "matched_glossary": matched_glossary[:4],
    }


def _edge_record(src: str, tgt: str, direction: str, rel: dict[str, Any]) -> dict[str, Any]:
    return {
        "from": src,
        "to": tgt,
        "type": rel["type"],
        "direction": direction,
        "confidence": rel.get("confidence"),
        "evidence_count": len(rel.get("evidence") or []),
        "via": (rel.get("properties") or {}).get("via"),
    }


# --------------------------------------------------------------------------- #
# Backend factory
# --------------------------------------------------------------------------- #


def make_backend(backend: str, catalog_path: Path, kuzu_path: Path | None = None) -> Backend:
    """Construct a backend by name.

    backend='auto'  — Kuzu if a Kuzu DB exists at kuzu_path; otherwise JSON.
    backend='json'  — always JSON (catalog.json).
    backend='kuzu'  — always Kuzu (requires the database at kuzu_path).
    """
    backend = (backend or "auto").lower()
    if backend in {"auto", "kuzu"} and kuzu_path and kuzu_path.exists():
        from storage_kuzu import KuzuBackend  # type: ignore[import-not-found]
        return KuzuBackend(kuzu_path)
    if backend == "kuzu":
        raise RuntimeError(
            f"backend=kuzu requested but no Kuzu database at {kuzu_path}. "
            "Run `python build_kuzu.py` to create it from catalog.json."
        )
    return JSONBackend(catalog_path)
