"""Embed catalog entities for hybrid retrieval.

Uses Vertex AI via google-genai with Application Default Credentials. Each
Component / API / Resource / Provider is embedded from a concatenation of its
description, spec, annotations, and evidence snippets. Embeddings are written
back into the catalog JSON under each entity's `embedding` field; the Kuzu
backend (`build_kuzu.py`) then pushes them into an on-disk HNSW vector index.

Run:
  python embed_catalog.py --catalog data/catalog.json --project your-gcp-project --location us-central1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


HERE = Path(__file__).parent
DEFAULT_CATALOG = HERE / "data" / "catalog.json"
DEFAULT_MODEL = "gemini-embedding-001"
DEFAULT_DIM = 768
DEFAULT_TASK = "RETRIEVAL_DOCUMENT"
MAX_TEXT_CHARS = 7000


def entity_to_text(entity: dict[str, Any]) -> str:
    meta = entity.get("metadata", {})
    spec = entity.get("spec", {})
    annotations = meta.get("annotations", {})
    lines = [
        f"kind: {entity.get('kind', '')}",
        f"name: {meta.get('name', '')}",
    ]
    if annotations.get("tagline"):
        lines.append(f"tagline: {annotations['tagline']}")
    if meta.get("description"):
        lines.append(f"description: {meta['description']}")
    if spec.get("type"):
        lines.append(f"type: {spec['type']}")
    if spec.get("technology"):
        lines.append(f"technology: {spec['technology']}")
    if spec.get("system"):
        lines.append(f"system: {spec['system']}")
    if spec.get("domain"):
        lines.append(f"domain: {spec['domain']}")
    if meta.get("tags"):
        lines.append("tags: " + ", ".join(meta["tags"]))
    if annotations.get("aliases"):
        lines.append("aliases: " + ", ".join(annotations["aliases"]))
    if annotations.get("env_keys"):
        lines.append("env_keys: " + ", ".join(annotations["env_keys"][:12]))
    if annotations.get("tables_or_collections"):
        lines.append("tables: " + ", ".join(annotations["tables_or_collections"][:12]))
    if annotations.get("messaging_pattern"):
        lines.append(f"messaging_pattern: {annotations['messaging_pattern']}")
    if annotations.get("subscribes_to"):
        lines.append(f"subscribes_to: {annotations['subscribes_to']}")
    for attr in (spec.get("domain_attributes") or [])[:20]:
        if not isinstance(attr, dict):
            continue
        name = attr.get("attribute", "")
        values = ", ".join((attr.get("values") or [])[:8])
        meaning = (attr.get("meaning") or "").replace("\n", " ").strip()[:200]
        if name:
            lines.append(f"domain_attribute: {name} = [{values}] — {meaning}")
    for term in (spec.get("glossary") or [])[:20]:
        if not isinstance(term, dict):
            continue
        t = term.get("term", "")
        d = (term.get("definition") or "").replace("\n", " ").strip()[:200]
        syn = ", ".join((term.get("synonyms") or [])[:5])
        if t:
            line = f"glossary: {t} — {d}"
            if syn:
                line += f" (synonyms: {syn})"
            lines.append(line)
    capability_sheet = annotations.get("capability_sheet") or ""
    if capability_sheet:
        capability_sheet = capability_sheet.replace("\n", " ").strip()
        lines.append(f"capability_sheet: {capability_sheet[:2000]}")
    for evidence in entity.get("evidence", [])[:6]:
        snippet = (evidence.get("snippet") or "").replace("\n", " ").strip()
        if snippet:
            lines.append(f"evidence: {evidence.get('path')}: {snippet[:200]}")
    text = "\n".join(lines)
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
    return text


def make_client(project: str | None, location: str):
    from google import genai
    if project:
        return genai.Client(vertexai=True, project=project, location=location or "us-central1")
    return genai.Client()


def embed_batch(client, model: str, texts: list[str], dim: int, task_type: str) -> list[list[float]]:
    from google.genai.types import EmbedContentConfig
    config = EmbedContentConfig(output_dimensionality=dim, task_type=task_type)
    response = client.models.embed_content(model=model, contents=texts, config=config)
    return [list(emb.values) for emb in response.embeddings]


def attach_embeddings(catalog_path: Path, *, model: str, dim: int, project: str | None, location: str, batch_size: int, only_kinds: set[str] | None) -> dict[str, Any]:
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    entities = payload.get("entities") or []
    kinds = only_kinds or {"Component", "API", "Resource", "Provider"}
    indices = [i for i, e in enumerate(entities) if e.get("kind") in kinds]
    if not indices:
        print("no entities to embed")
        return payload

    client = make_client(project, location)
    embedded = 0
    failed = 0
    total = len(indices)
    started = time.monotonic()

    for batch_start in range(0, total, batch_size):
        batch_indices = indices[batch_start : batch_start + batch_size]
        texts = [entity_to_text(entities[i]) for i in batch_indices]
        try:
            vectors = embed_batch(client, model, texts, dim, DEFAULT_TASK)
        except Exception as exc:  # noqa: BLE001 - we report and move on
            print(f"WARN: batch {batch_start}-{batch_start + len(batch_indices)} failed: {exc}", file=sys.stderr)
            failed += len(batch_indices)
            continue
        for idx, vector in zip(batch_indices, vectors):
            entities[idx]["embedding"] = vector
            embedded += 1
        elapsed = time.monotonic() - started
        rate = embedded / elapsed if elapsed > 0 else 0
        print(f"embedded {embedded}/{total} ({rate:.1f}/s)", flush=True)

    payload.setdefault("summary", {})["embedded_entities"] = embedded
    payload["summary"]["embedding_model"] = model
    payload["summary"]["embedding_dim"] = dim
    payload["summary"]["embedding_failures"] = failed
    tmp = catalog_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, catalog_path)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--dim", type=int, default=DEFAULT_DIM)
    parser.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT") or None)
    parser.add_argument("--location", default=os.getenv("GOOGLE_CLOUD_LOCATION") or "us-central1")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--only-kinds", nargs="*", default=None)
    args = parser.parse_args()

    only_kinds = set(args.only_kinds) if args.only_kinds else None
    result = attach_embeddings(
        args.catalog,
        model=args.model,
        dim=args.dim,
        project=args.project,
        location=args.location,
        batch_size=args.batch_size,
        only_kinds=only_kinds,
    )
    out = {"embedded": result.get("summary", {}).get("embedded_entities", 0), "dim": args.dim, "model": args.model}
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
