"""Populate a KuzuDB database from data/catalog.json.

Run after embed_catalog.py. Idempotent: drops and recreates the schema each
time so it's safe to re-run. The output directory is the database itself
(Kuzu uses a directory, not a single file).

Usage:
  python build_kuzu.py
  python build_kuzu.py --catalog data/catalog.json --db data/catalog.kuzu
  python build_kuzu.py --no-fts          # skip the FTS index (large catalogs)
  python build_kuzu.py --no-vector       # skip the vector index
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = HERE / "data" / "catalog.json"
DEFAULT_DB = HERE / "data" / "catalog.kuzu"
DEFAULT_DIM = 768

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


def _entity_haystack(entity: dict[str, Any]) -> str:
    """Concatenated searchable text. Mirrors storage._entity_haystack."""
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


def _strip_spec(spec: dict[str, Any]) -> str:
    """Return spec as JSON, removing fields that we promote to columns."""
    spec = dict(spec or {})
    for key in ("type", "system", "category"):
        pass  # keep them too — JSON is the source of truth for spec
    return json.dumps(spec, ensure_ascii=False)


def _strip_annotations(annotations: dict[str, Any]) -> str:
    """Annotations as JSON minus fields promoted to columns."""
    annotations = dict(annotations or {})
    annotations.pop("aliases", None)
    annotations.pop("source_repos", None)
    annotations.pop("tagline", None)
    return json.dumps(annotations, ensure_ascii=False)


def build(catalog_path: Path, db_path: Path, *, dim: int, build_fts: bool, build_vector: bool) -> dict[str, Any]:
    try:
        import kuzu
    except ImportError as exc:
        raise SystemExit("Install the `kuzu` package first — pip install kuzu") from exc

    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    entities = payload.get("entities") or []
    relations = payload.get("relations") or []

    # Always rebuild for now: simpler than incremental upserts. With 1700
    # entities this completes in a couple of seconds.
    if db_path.exists():
        if db_path.is_dir():
            shutil.rmtree(db_path)
        else:
            db_path.unlink()
    # Kuzu also writes sibling files (.wal etc.) — clean those too.
    for sibling in db_path.parent.glob(db_path.name + ".*"):
        try:
            if sibling.is_dir():
                shutil.rmtree(sibling)
            else:
                sibling.unlink()
        except OSError:
            pass
    db_path.parent.mkdir(parents=True, exist_ok=True)

    db = kuzu.Database(str(db_path))
    conn = kuzu.Connection(db)

    # Load extensions (they're statically linked in Kuzu 0.11+, but a fresh
    # database still needs them loaded for index creation).
    for ext in ("VECTOR", "FTS"):
        try:
            conn.execute(f"LOAD EXTENSION {ext}")
        except RuntimeError:
            pass

    # Schema.
    conn.execute(f"""
        CREATE NODE TABLE Entity(
            ref STRING,
            kind STRING,
            name STRING,
            confidence STRING,
            summary_text STRING,
            tagline STRING,
            haystack STRING,
            capability_sheet STRING,
            aliases STRING[],
            source_repos STRING[],
            spec_json STRING,
            annotations_json STRING,
            embedding FLOAT[{int(dim)}],
            PRIMARY KEY(ref)
        )
    """)
    for rel_type in REL_TYPES:
        conn.execute(f"""
            CREATE REL TABLE {rel_type}(
                FROM Entity TO Entity,
                evidence_json STRING,
                confidence STRING,
                properties_json STRING
            )
        """)

    # Ingest entities.
    started = time.monotonic()
    zero_vec = [0.0] * int(dim)
    entity_refs: set[str] = set()
    inserted = 0
    for entity in entities:
        kind = entity.get("kind") or ""
        name = entity.get("metadata", {}).get("name") or ""
        if not kind or not name:
            continue
        ref = f"{kind}:{name}"
        if ref in entity_refs:
            continue  # dedupe defensively
        entity_refs.add(ref)
        meta = entity.get("metadata", {})
        annotations = meta.get("annotations", {}) or {}
        spec = entity.get("spec", {}) or {}
        emb = entity.get("embedding")
        if not emb or len(emb) != int(dim):
            emb = zero_vec
        conn.execute(
            "CREATE (n:Entity {ref: $ref, kind: $kind, name: $name, confidence: $confidence, summary_text: $summary, tagline: $tagline, haystack: $haystack, capability_sheet: $cap, aliases: $aliases, source_repos: $repos, spec_json: $spec, annotations_json: $ann, embedding: $emb})",
            {
                "ref": ref,
                "kind": kind,
                "name": name,
                "confidence": entity.get("confidence") or "",
                "summary": meta.get("description") or "",
                "tagline": annotations.get("tagline") or "",
                "haystack": _entity_haystack(entity),
                "cap": annotations.get("capability_sheet") or "",
                "aliases": list(annotations.get("aliases") or []),
                "repos": list(annotations.get("source_repos") or []),
                "spec": _strip_spec(spec),
                "ann": _strip_annotations(annotations),
                "emb": [float(x) for x in emb],
            },
        )
        inserted += 1

    # Ingest relations. Skip any pointing at entities we didn't insert.
    skipped_rels = 0
    rel_counts: dict[str, int] = {}
    for relation in relations:
        rel_type = relation.get("type") or ""
        if rel_type not in REL_TYPES:
            skipped_rels += 1
            continue
        a_ref, b_ref = relation.get("from"), relation.get("to")
        if a_ref not in entity_refs or b_ref not in entity_refs:
            skipped_rels += 1
            continue
        conn.execute(
            f"MATCH (a:Entity {{ref: $a}}), (b:Entity {{ref: $b}}) CREATE (a)-[r:{rel_type} {{evidence_json: $ev, confidence: $conf, properties_json: $props}}]->(b)",
            {
                "a": a_ref,
                "b": b_ref,
                "ev": json.dumps(relation.get("evidence") or [], ensure_ascii=False),
                "conf": relation.get("confidence") or "",
                "props": json.dumps(relation.get("properties") or {}, ensure_ascii=False),
            },
        )
        rel_counts[rel_type] = rel_counts.get(rel_type, 0) + 1

    # Build indexes after ingest (faster).
    if build_fts:
        conn.execute("CALL CREATE_FTS_INDEX('Entity', 'entity_fts_idx', ['haystack'])")
    if build_vector:
        conn.execute("CALL CREATE_VECTOR_INDEX('Entity', 'entity_emb_idx', 'embedding')")

    elapsed = time.monotonic() - started
    return {
        "entities": inserted,
        "relations": sum(rel_counts.values()),
        "rel_counts": rel_counts,
        "skipped_rels": skipped_rels,
        "fts_index": build_fts,
        "vector_index": build_vector,
        "db_path": str(db_path),
        "duration_seconds": round(elapsed, 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--dim", type=int, default=DEFAULT_DIM)
    parser.add_argument("--no-fts", action="store_true")
    parser.add_argument("--no-vector", action="store_true")
    args = parser.parse_args()

    if not args.catalog.exists():
        print(f"catalog not found: {args.catalog}", file=sys.stderr)
        return 1
    summary = build(args.catalog, args.db, dim=args.dim, build_fts=not args.no_fts, build_vector=not args.no_vector)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
