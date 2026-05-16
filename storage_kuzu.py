"""KuzuDB-backed implementation of the ServiceScout Backend interface.

Layout in Kuzu:

  NODE TABLE Entity(
    ref STRING PRIMARY KEY,
    kind STRING,
    name STRING,
    description STRING,
    tagline STRING,
    haystack STRING,            -- concatenated text for FTS
    aliases STRING[],
    source_repos STRING[],
    capability_sheet STRING,
    spec_json STRING,           -- the rest of `spec` as JSON
    annotations_json STRING,    -- the rest of `annotations` as JSON
    domain_attributes_json STRING,
    glossary_json STRING,
    embedding FLOAT[768]
  )

  REL TABLE communicatesWith / consumesApi / consumesMessage / producesMessage /
            dependsOn / readsResource / writesResource / providesApi / ownedBy /
            partOf / subcomponentOf (
    FROM Entity TO Entity,
    evidence_json STRING,
    confidence STRING,
    properties_json STRING
  )

Indexes:
  FTS    on Entity.haystack         (stop-words + stemming, k1=1.2, b=0.75)
  VECTOR on Entity.embedding        (HNSW, cosine)

Graph traversal (neighbours, trace) still happens in Python via an
in-memory adjacency built at startup. Kuzu owns the heavy storage and
the proper FTS + HNSW indexes — exactly where the JSON backend hurts.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from storage import (
    Backend,
    SEARCHABLE_KINDS,
    _edge_record,
    _kind_rank,
    _rrf,
)


# Edge types we model as Kuzu relation tables. Must match the relation types
# emitted by build_catalog.py.
REL_TYPES = [
    "communicatesWith",
    "consumesApi",
    "consumesMessage",
    "producesMessage",
    "dependsOn",
    "readsResource",
    "writesResource",
    "providesApi",
    "ownedBy",
    "partOf",
    "subcomponentOf",
]


class KuzuBackend(Backend):
    name = "kuzu"

    def __init__(self, db_path: Path, *, embedding_dim: int = 768) -> None:
        try:
            import kuzu  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("KuzuBackend requires the `kuzu` package — pip install kuzu") from exc
        self.db_path = db_path
        self.embedding_dim = embedding_dim
        self._open()

    def _open(self) -> None:
        import kuzu
        self._kuzu = kuzu
        self._db = kuzu.Database(str(self.db_path))
        self._conn = kuzu.Connection(self._db)
        # Extensions are statically linked in Kuzu 0.11+, but loading is still required.
        for ext in ("VECTOR", "FTS"):
            try:
                self._conn.execute(f"LOAD EXTENSION {ext}")
            except RuntimeError:
                pass  # already loaded
        self._refresh_in_memory()

    # -- in-memory adjacency (built from Kuzu on startup) -----------------

    def _refresh_in_memory(self) -> None:
        self._entity_index: dict[str, dict[str, Any]] = {}
        self._by_source: dict[str, list[dict[str, Any]]] = {}
        self._by_target: dict[str, list[dict[str, Any]]] = {}
        # Hydrate entities (lightweight — no embedding).
        r = self._conn.execute("MATCH (n:Entity) RETURN n.ref, n.kind, n.name, n.summary_text, n.tagline, n.aliases, n.source_repos, n.spec_json, n.annotations_json")
        while r.has_next():
            row = r.get_next()
            ref, kind, name, desc, tagline, aliases, source_repos, spec_json, annotations_json = row
            spec = _safe_json(spec_json) or {}
            annotations = _safe_json(annotations_json) or {}
            if aliases:
                annotations["aliases"] = list(aliases)
            if source_repos:
                annotations["source_repos"] = list(source_repos)
            if tagline:
                annotations["tagline"] = tagline
            self._entity_index[ref] = {
                "kind": kind,
                "metadata": {
                    "name": name,
                    "description": desc or "",
                    "annotations": annotations,
                    "tags": annotations.get("tags") or [],
                },
                "spec": spec,
            }
        # Hydrate relations.
        for rel_type in REL_TYPES:
            try:
                r = self._conn.execute(f"MATCH (a:Entity)-[r:{rel_type}]->(b:Entity) RETURN a.ref, b.ref, r.evidence_json, r.confidence, r.properties_json")
            except RuntimeError:
                continue
            while r.has_next():
                a_ref, b_ref, evidence_json, confidence, properties_json = r.get_next()
                rel = {
                    "from": a_ref,
                    "to": b_ref,
                    "type": rel_type,
                    "evidence": _safe_json(evidence_json) or [],
                    "confidence": confidence or "",
                    "properties": _safe_json(properties_json) or {},
                }
                self._by_source.setdefault(a_ref, []).append(rel)
                self._by_target.setdefault(b_ref, []).append(rel)

    # -- status / list ----------------------------------------------------

    def status(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        r = self._conn.execute("MATCH (n:Entity) RETURN n.kind, count(*)")
        while r.has_next():
            kind, count = r.get_next()
            counts[kind] = count
        rel_counts: dict[str, int] = {}
        for rel_type in REL_TYPES:
            try:
                r = self._conn.execute(f"MATCH ()-[r:{rel_type}]->() RETURN count(r)")
                if r.has_next():
                    (count,) = r.get_next()
                    if count:
                        rel_counts[rel_type] = count
            except RuntimeError:
                continue
        return {
            "backend": self.name,
            "kuzu_db_path": str(self.db_path),
            "exists": self.db_path.exists(),
            "summary": {
                "node_kinds": counts,
                "relation_types": rel_counts,
                "entities": sum(counts.values()),
                "relations": sum(rel_counts.values()),
            },
            "embedding_present": True,  # we always carry the embedding column
            "vector_search_available": True,
        }

    def list_entities(self, *, kind: str | None, limit: int) -> list[dict[str, Any]]:
        if kind:
            r = self._conn.execute(
                "MATCH (n:Entity {kind: $kind}) RETURN n.ref, n.kind, n.name, n.spec_json LIMIT $limit",
                {"kind": kind, "limit": int(limit)},
            )
        else:
            r = self._conn.execute(
                "MATCH (n:Entity) RETURN n.ref, n.kind, n.name, n.spec_json LIMIT $limit",
                {"limit": int(limit)},
            )
        out = []
        while r.has_next():
            ref, k, name, spec_json = r.get_next()
            spec = _safe_json(spec_json) or {}
            out.append({"ref": ref, "kind": k, "name": name, "type": spec.get("type", ""), "system": spec.get("system", "")})
        return out

    # -- describe / fuzzy_lookup -----------------------------------------

    def describe(self, ref: str) -> dict[str, Any] | None:
        return self._entity_index.get(ref)

    def fuzzy_lookup(self, needle: str) -> dict[str, Any] | None:
        if not needle:
            return None
        if ":" in needle:
            ent = self._entity_index.get(needle)
            if ent:
                return ent
        needle_lower = needle.lower()
        # Exact name match.
        exact = [e for e in self._entity_index.values() if e["metadata"]["name"].lower() == needle_lower]
        if exact:
            exact.sort(key=_kind_rank)
            return exact[0]
        # Substring match.
        contains = [e for e in self._entity_index.values() if needle_lower in e["metadata"]["name"].lower()]
        if contains:
            contains.sort(key=lambda e: (_kind_rank(e), len(e["metadata"]["name"])))
            return contains[0]
        # Alias match.
        matches_alias = [
            e for e in self._entity_index.values()
            if needle_lower in {a.lower() for a in (e["metadata"].get("annotations", {}).get("aliases") or [])}
        ]
        if matches_alias:
            matches_alias.sort(key=_kind_rank)
            return matches_alias[0]
        return None

    # -- search ----------------------------------------------------------

    def search(self, query: str, *, query_vector: list[float] | None, limit: int, min_confidence: str | None = None) -> list[dict[str, Any]]:
        if not query:
            return []
        candidates: dict[str, dict[str, Any]] = {}
        # Lexical via FTS.
        fts_query = _to_fts_query(query)
        lex_rank: list[str] = []
        try:
            r = self._conn.execute(
                "CALL QUERY_FTS_INDEX('Entity', 'entity_fts_idx', $q, top := $k) RETURN node.ref AS ref, score ORDER BY score DESC",
                {"q": fts_query, "k": max(limit * 5, 30)},
            )
            while r.has_next():
                ref, score = r.get_next()
                ent = self._entity_index.get(ref)
                if not ent or ent.get("kind") not in SEARCHABLE_KINDS:
                    continue
                candidates.setdefault(ref, {"ent": ent, "vec": 0.0, "lex": 0.0})["lex"] = float(score)
                lex_rank.append(ref)
        except RuntimeError:
            pass
        # Vector via HNSW.
        vec_rank: list[str] = []
        if query_vector:
            try:
                r = self._conn.execute(
                    "CALL QUERY_VECTOR_INDEX('Entity', 'entity_emb_idx', $qv, $k) RETURN node.ref AS ref, distance ORDER BY distance",
                    {"qv": query_vector, "k": max(limit * 5, 30)},
                )
                while r.has_next():
                    ref, distance = r.get_next()
                    ent = self._entity_index.get(ref)
                    if not ent or ent.get("kind") not in SEARCHABLE_KINDS:
                        continue
                    # Convert distance to similarity (cosine: 1 - distance).
                    sim = max(0.0, 1.0 - float(distance))
                    candidates.setdefault(ref, {"ent": ent, "vec": 0.0, "lex": 0.0})["vec"] = sim
                    vec_rank.append(ref)
            except RuntimeError:
                pass
        if not candidates:
            return []
        rank_lists: list[list[str]] = []
        if vec_rank:
            rank_lists.append(vec_rank)
        if lex_rank:
            rank_lists.append(lex_rank)
        # RRF over refs.
        ref_to_idx = {ref: i for i, ref in enumerate(candidates)}
        ranking_indices = [[ref_to_idx[r] for r in rl if r in ref_to_idx] for rl in rank_lists]
        fused = _rrf(ranking_indices)
        idx_to_ref = {i: r for r, i in ref_to_idx.items()}
        from storage import _confidence_at_least, _confidence_weight, _hit_record  # local import to avoid cycle
        weighted: dict[int, float] = {}
        for idx, score in fused.items():
            ent_conf = candidates[idx_to_ref[idx]]["ent"].get("confidence")
            if not _confidence_at_least(ent_conf, min_confidence):
                continue
            weighted[idx] = score * _confidence_weight(ent_conf)
        scored = [(s, idx_to_ref[i], candidates[idx_to_ref[i]]) for i, s in sorted(weighted.items(), key=lambda kv: -kv[1])]
        terms = [t.lower() for t in re.split(r"[^A-Za-z0-9_-]+", query) if len(t) > 2]
        out = []
        for rrf_score, ref, data in scored[:limit]:
            ent = data["ent"]
            out.append(_hit_record(ent, rrf_score, data["vec"], data["lex"], terms))
        return out

    # -- neighbors / trace / evidence (in-memory) -------------------------
    # Identical algorithms to JSONBackend, just operating on the adjacency
    # built from the Kuzu DB at startup.

    def neighbors(self, ref: str, *, direction: str, depth: int, edge_types: list[str] | None, min_confidence: str | None = None) -> list[dict[str, Any]]:
        from storage import _confidence_at_least
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

    def trace(self, *, start_ref: str, end_match: str | None, max_hops: int, edge_types: list[str], include_async: bool, fanout_per_node: int, min_confidence: str | None = None) -> dict[str, Any]:
        from storage import _confidence_at_least
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
                    new_branch = branch_id if i == 0 else f"{branch_id}.{next_seq[0]}"
                    if i > 0:
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

    def evidence(self, src_ref: str, tgt_ref: str | None) -> list[dict[str, Any]]:
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
# Helpers
# --------------------------------------------------------------------------- #


def _safe_json(s: str | None) -> Any:
    if not s:
        return None
    try:
        return json.loads(s)
    except (TypeError, json.JSONDecodeError):
        return None


def _to_fts_query(query: str) -> str:
    """Kuzu FTS expects whitespace-separated terms. Strip punctuation, keep
    alphanumeric tokens longer than 2 chars."""
    terms = [t for t in re.split(r"[^A-Za-z0-9_-]+", query) if len(t) > 2]
    return " ".join(terms) or query
