"""Reconcile the catalog: collapse external duplicates into real Components.

Reads `data/catalog.json`, finds external (unresolved) Components whose name
or aliases overlap with an extracted Component's alias index, and merges them
in place — rerouting edges, appending evidence, deduplicating aliases — with
an audit trail at `data/reconcile_audit.jsonl`.

Deterministic v1. The LLM-augmented R2 step (proposing merges for ambiguous
candidates) is left for a follow-up; everything done here is high-precision
and reversible via the audit log.

Run:
  python reconcile.py --catalog data/catalog.json
  python reconcile.py --catalog data/catalog.json --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


HERE = Path(__file__).parent
DEFAULT_CATALOG = HERE / "data" / "catalog.json"
DEFAULT_AUDIT = HERE / "data" / "reconcile_audit.jsonl"


def canonical_key(name: str) -> str:
    """Same canonicalisation rule as build_catalog.canonical_key."""
    if not name:
        return ""
    key = re.sub(r"[^a-z0-9]+", "", name.lower())
    suffixes = ["serviceapi", "apiservice", "service", "api", "backend", "frontend", "client", "gateway"]
    allow = {"paymentinterface", "userinterface"}
    if key in allow:
        return key
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            if key.endswith(suffix) and len(key) - len(suffix) >= 3:
                key = key[: -len(suffix)]
                changed = True
                break
    return key


URL_RE = re.compile(r"^[a-z]+://")
HOST_LIKE_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*\.(com|net|org|io|local|cloud|app|dev|co|me|us|gslb|svc|ai|google)([:/].*)?$")


def looks_like_url(value: str) -> bool:
    if not value:
        return False
    v = value.lower().strip()
    return bool(URL_RE.match(v) or HOST_LIKE_RE.match(v))


def url_to_first_label(url: str) -> str:
    s = url.lower().strip()
    s = URL_RE.sub("", s)
    s = s.split("/", 1)[0].split(":", 1)[0]
    return s.split(".", 1)[0]


# Generic-name stopwords. If a Component's canonical key (after suffix stripping)
# is one of these, it's too noisy to use for alias-overlap merging. Adyen-API
# vs Trustly-API both have a "test" alias → without this filter they collapse
# into a single mythical "test" Component.
GENERIC_KEY_STOPWORDS = {
    "test", "tests", "mock", "mocks", "sandbox", "fake", "stub", "demo",
    "dev", "prod", "staging", "qa", "uat", "preview", "draft",
    "server", "client", "service", "api", "app", "web", "ui", "backend",
    "frontend", "gateway", "platform", "shared", "common", "core", "main",
    "default", "base", "internal", "external", "public", "private",
    "hub", "edge", "node", "worker", "consumer", "producer",
    "search", "store", "data", "log", "auth", "user", "admin",
}


def all_alias_keys(component: dict[str, Any]) -> set[str]:
    """Canonical keys that route to this Component: name + aliases.

    Only real hostnames (with a scheme or a known TLD-like suffix) get reduced
    to their first DNS label. Plain dotted strings like Java config keys are
    canonical-keyed whole, because their dots are property separators, not
    domain separators.

    Filters that prevent false-positive merges:
      - Keys shorter than 5 chars (was 4) — `test`, `cart`, `auth` are all 4
        chars and notoriously cause cross-domain false positives.
      - Keys in GENERIC_KEY_STOPWORDS — single generic English words that
        appear as aliases on many distinct services.
    """
    meta = component.get("metadata", {})
    annotations = meta.get("annotations", {})
    keys: set[str] = set()
    name = meta.get("name") or ""
    if name:
        keys.add(canonical_key(name))
    aliases = annotations.get("aliases") or []
    for alias in aliases:
        if not alias:
            continue
        keys.add(canonical_key(alias))
        if looks_like_url(alias):
            host_label = url_to_first_label(alias)
            if host_label:
                keys.add(canonical_key(host_label))
    keys.discard("")
    keys = {k for k in keys if len(k) >= 5 and k not in GENERIC_KEY_STOPWORDS}
    return keys


def is_external(component: dict[str, Any]) -> bool:
    meta = component.get("metadata", {})
    ann = meta.get("annotations", {})
    if ann.get("external") == "true":
        return True
    return not ann.get("source_repos")


def is_real(component: dict[str, Any]) -> bool:
    return not is_external(component)


def find_merge_candidates(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """For each external Component, find the real Component it should merge into.

    Returns merge proposals: [{from_ref, into_ref, matching_keys, reason}].
    """
    real_components = [e for e in entities if e.get("kind") == "Component" and is_real(e)]
    external_components = [e for e in entities if e.get("kind") == "Component" and is_external(e)]

    real_index: dict[str, str] = {}
    real_evidence_index: dict[str, set[str]] = defaultdict(set)
    for component in real_components:
        ref = f"Component:{component['metadata']['name']}"
        for key in all_alias_keys(component):
            real_index.setdefault(key, ref)
            real_evidence_index[ref].add(key)

    proposals: list[dict[str, Any]] = []
    for component in external_components:
        ref = f"Component:{component['metadata']['name']}"
        ext_keys = all_alias_keys(component)
        matches = sorted({real_index[k] for k in ext_keys if k in real_index})
        if len(matches) == 1:
            target = matches[0]
            if target == ref:
                continue
            common = sorted(ext_keys & real_evidence_index[target])
            proposals.append({
                "from": ref,
                "into": target,
                "matching_keys": common,
                "reason": "single real-Component match via name/alias canonical key",
            })
        elif len(matches) > 1:
            proposals.append({
                "from": ref,
                "into": None,
                "candidates": matches,
                "matching_keys": sorted(ext_keys),
                "reason": "ambiguous — multiple real Components claim overlapping aliases (skipped, needs LLM judgment)",
            })
    return proposals


def apply_merges(catalog: dict[str, Any], proposals: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply unambiguous merges in place. Returns audit record."""
    entities = catalog.get("entities") or []
    relations = catalog.get("relations") or []

    merges = [p for p in proposals if p.get("into")]
    if not merges:
        return {"merges_applied": 0, "edges_rerouted": 0, "edges_dropped": 0}

    entity_by_ref = {f"{e['kind']}:{e['metadata']['name']}": e for e in entities}
    redirect: dict[str, str] = {p["from"]: p["into"] for p in merges}

    # Fold the external entity's aliases + evidence into the target.
    for merge in merges:
        src = entity_by_ref.get(merge["from"])
        dst = entity_by_ref.get(merge["into"])
        if not src or not dst:
            continue
        src_meta = src.get("metadata", {})
        dst_meta = dst.setdefault("metadata", {})
        dst_ann = dst_meta.setdefault("annotations", {})
        # aliases
        new_aliases = set(dst_ann.get("aliases") or [])
        new_aliases.update(src_meta.get("annotations", {}).get("aliases") or [])
        new_aliases.add(src_meta.get("name"))
        new_aliases.discard(None)
        new_aliases.discard("")
        dst_ann["aliases"] = sorted(new_aliases)
        # description: keep dst's, but if dst has none and src has something, fall back
        if not dst_meta.get("description") and src_meta.get("description"):
            dst_meta["description"] = src_meta["description"]
        # tags
        dst_meta["tags"] = sorted(set((dst_meta.get("tags") or []) + (src_meta.get("tags") or [])))
        # evidence
        dst.setdefault("evidence", []).extend(src.get("evidence") or [])

    # Remove merged entities.
    kept_entities = [e for e in entities if f"{e['kind']}:{e['metadata']['name']}" not in redirect]
    catalog["entities"] = kept_entities

    # Reroute relations.
    rerouted = 0
    dropped = 0
    new_relations: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for relation in relations:
        f = redirect.get(relation["from"], relation["from"])
        t = redirect.get(relation["to"], relation["to"])
        if f != relation["from"] or t != relation["to"]:
            rerouted += 1
        if f == t:
            dropped += 1
            continue
        new_relation = dict(relation)
        new_relation["from"] = f
        new_relation["to"] = t
        key = f"{f}|{relation['type']}|{t}"
        if key in seen_keys:
            # merge evidence into the existing relation
            existing = next(r for r in new_relations if relation_key(r) == key)
            existing.setdefault("evidence", []).extend(new_relation.get("evidence") or [])
            continue
        seen_keys.add(key)
        new_relations.append(new_relation)
    catalog["relations"] = new_relations

    # Recompute summary.
    counts: dict[str, int] = {}
    for entity in catalog["entities"]:
        counts[entity["kind"]] = counts.get(entity["kind"], 0) + 1
    edge_counts: dict[str, int] = {}
    for relation in catalog["relations"]:
        edge_counts[relation["type"]] = edge_counts.get(relation["type"], 0) + 1
    summary = catalog.setdefault("summary", {})
    summary["node_kinds"] = counts
    summary["relation_types"] = edge_counts
    summary["entities"] = len(catalog["entities"])
    summary["relations"] = len(catalog["relations"])

    return {
        "merges_applied": len(merges),
        "edges_rerouted": rerouted,
        "edges_dropped": dropped,
    }


def relation_key(relation: dict[str, Any]) -> str:
    return f"{relation['from']}|{relation['type']}|{relation['to']}"


def append_audit(audit_path: Path, record: dict[str, Any]) -> None:
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


LLM_MERGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["merges"],
    "properties": {
        "merges": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["from", "into", "confidence", "reason"],
                "properties": {
                    "from": {"type": "string"},
                    "into": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "reason": {"type": "string"},
                },
            },
        }
    },
}


def llm_propose_merges(catalog: dict[str, Any], provider: str = "codex", model: str | None = None) -> list[dict[str, Any]]:
    """Ask an LLM to propose merges for external Components that look like real ones.

    Returns a list of {from, into, confidence, reason}. Only `confidence >= medium`
    merges should be applied; the caller filters.
    """
    entities = catalog.get("entities") or []
    externals = [e for e in entities if e.get("kind") == "Component" and is_external(e)]
    reals = [e for e in entities if e.get("kind") == "Component" and is_real(e)]
    if not externals or not reals:
        return []

    def compact(component: dict[str, Any], include_aliases: bool = True) -> dict[str, Any]:
        meta = component.get("metadata", {})
        ann = meta.get("annotations", {})
        spec = component.get("spec", {})
        out = {"name": meta.get("name"), "description": (meta.get("description") or "")[:160]}
        if include_aliases:
            out["aliases"] = (ann.get("aliases") or [])[:8]
        if meta.get("tags"):
            out["tags"] = meta["tags"][:5]
        # Extra signals for the LLM: runtime shape + environments make
        # synonyms more or less plausible. e.g. a long-running service is
        # never a cron; a `prod`-deployed component shouldn't be merged
        # with a `test`-only stub.
        if spec.get("system"):
            out["system"] = spec["system"]
        if spec.get("runtime"):
            out["runtime"] = spec["runtime"]
        if spec.get("environments"):
            out["environments"] = list(spec["environments"])[:4]
        if ann.get("tagline"):
            out["tagline"] = ann["tagline"][:120]
        return out

    payload = {
        "external_components": [compact(e) for e in externals],
        "real_components": [compact(e, include_aliases=True) for e in reals],
    }

    prompt = (
        "You are reconciling a software catalog. Many `external_components` are "
        "actually the same service as one of the `real_components` — the LLM that "
        "extracted them used different labels (marketing names, classes, hostnames). "
        "For each external, decide if it should be MERGED INTO a specific real "
        "component. Be conservative: only emit a merge when the names clearly "
        "describe the same service. When unsure, omit the merge entry — do not guess.\n\n"
        "Strong signals (use these to MERGE):\n"
        "- A hostname alias on the external matches the real component's name or its hostnames.\n"
        "- A client class name (e.g. HorizonProvider) matches a real service (horizon-graphql-api).\n"
        "- A config key alias contains the real service's short name.\n"
        "- The descriptions describe the same domain/purpose.\n"
        "- The taglines describe the same product surface or domain.\n\n"
        "Strong signals AGAINST merging (use these to REJECT):\n"
        "- Names share a prefix but describe different services (e.g. account vs account-service-tests).\n"
        "- The external is a generic third-party (Google reCAPTCHA, Bazaarvoice, Auth0).\n"
        "- The two components have INCOMPATIBLE `runtime` shapes (e.g. cron vs long-running) — they\n"
        "  are probably different deployables even if the names overlap.\n"
        "- The environments are disjoint and exclusive (e.g. `[\"test\"]` vs `[\"prod\"]` only).\n\n"
        "Return JSON matching the schema. `from` must be `Component:<external-name>`; "
        "`into` must be `Component:<real-name>`. Use confidence=high only when you would "
        "stake your reputation on it.\n\n"
        f"DATA:\n{json.dumps(payload, indent=2)}\n"
    )

    if provider == "claude":
        return _claude_merge_call(prompt, model or "sonnet")
    return _codex_merge_call(prompt, model or "gpt-5.4-mini")


def _codex_merge_call(prompt: str, model: str) -> list[dict[str, Any]]:
    codex = shutil.which("codex")
    if not codex:
        raise SystemExit("codex CLI not on PATH")
    work = HERE / "data" / ".reconcile-scratch"
    work.mkdir(parents=True, exist_ok=True)
    schema_path = work / "merge_schema.json"
    result_path = work / "merge_result.json"
    schema_path.write_text(json.dumps(LLM_MERGE_SCHEMA, indent=2), encoding="utf-8")
    if result_path.exists():
        result_path.unlink()
    cmd = [
        codex, "exec", "--ephemeral",
        "-c", 'model_reasoning_effort="medium"',
        "--sandbox", "read-only",
        "--skip-git-repo-check",
        "--output-schema", str(schema_path),
        "--output-last-message", str(result_path),
        "--model", model,
        prompt,
    ]
    subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
    if not result_path.exists():
        return []
    try:
        out = json.loads(result_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return out.get("merges") or []


def _claude_merge_call(prompt: str, model: str) -> list[dict[str, Any]]:
    claude = shutil.which("claude")
    if not claude:
        raise SystemExit("claude CLI not on PATH")
    cmd = [
        claude, "-p", prompt,
        "--output-format", "json",
        "--json-schema", json.dumps(LLM_MERGE_SCHEMA, separators=(",", ":")),
        "--permission-mode", "dontAsk",
        "--model", model,
        "--allowedTools", "Read",
    ]
    completed = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
    if completed.returncode != 0:
        return []
    try:
        raw = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(raw, dict) and isinstance(raw.get("structured_output"), dict):
        return raw["structured_output"].get("merges") or []
    if isinstance(raw, dict) and isinstance(raw.get("result"), str):
        try:
            return json.loads(raw["result"]).get("merges") or []
        except json.JSONDecodeError:
            return []
    if isinstance(raw, dict) and "merges" in raw:
        return raw.get("merges") or []
    return []


def write_catalog(catalog_path: Path, payload: dict[str, Any]) -> None:
    tmp = catalog_path.with_suffix(catalog_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    os.replace(tmp, catalog_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--dry-run", action="store_true", help="Print proposals only; do not modify catalog.")
    parser.add_argument("--llm-assist", action="store_true", help="Ask an LLM to propose additional merges for ambiguous externals.")
    parser.add_argument("--llm-provider", choices=("codex", "claude"), default="codex")
    parser.add_argument("--llm-model", default=None)
    parser.add_argument("--min-llm-confidence", default="medium", choices=("high", "medium", "low"))
    args = parser.parse_args()

    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    entities = catalog.get("entities") or []

    proposals = find_merge_candidates(entities)
    unambiguous = [p for p in proposals if p.get("into")]
    ambiguous = [p for p in proposals if not p.get("into")]

    llm_merges_raw: list[dict[str, Any]] = []
    if args.llm_assist:
        print("\nAsking LLM for additional merge proposals...")
        llm_merges_raw = llm_propose_merges(catalog, provider=args.llm_provider, model=args.llm_model)
        rank = {"high": 3, "medium": 2, "low": 1}
        threshold = rank.get(args.min_llm_confidence, 2)
        for m in llm_merges_raw:
            if rank.get(m.get("confidence"), 0) < threshold:
                continue
            from_ref = m.get("from")
            into_ref = m.get("into")
            if not from_ref or not into_ref:
                continue
            if from_ref == into_ref:
                continue
            if any(p.get("from") == from_ref for p in unambiguous):
                continue
            unambiguous.append({
                "from": from_ref,
                "into": into_ref,
                "matching_keys": ["llm-judgment"],
                "reason": f"llm: {m.get('reason', '')[:200]}",
                "confidence": m.get("confidence"),
            })

    print(json.dumps({
        "catalog_entities_before": len(entities),
        "external_components_before": sum(1 for e in entities if e.get("kind") == "Component" and is_external(e)),
        "unambiguous_merge_proposals": len(unambiguous),
        "ambiguous_skipped": len(ambiguous),
    }, indent=2))

    if not unambiguous:
        print("\nNo unambiguous merges to apply.")
        return 0

    print("\nUnambiguous merges:")
    for p in unambiguous[:30]:
        print(f"  {p['from']:50s} → {p['into']}  via {p['matching_keys']}")
    if len(unambiguous) > 30:
        print(f"  ... and {len(unambiguous) - 30} more")
    if ambiguous:
        print("\nAmbiguous (skipped, would benefit from LLM judgment):")
        for p in ambiguous[:10]:
            print(f"  {p['from']:40s} candidates: {p['candidates']}")

    if args.dry_run:
        print("\n--dry-run: catalog NOT modified.")
        return 0

    result = apply_merges(catalog, unambiguous)
    write_catalog(args.catalog, catalog)
    record = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "catalog": str(args.catalog),
        **result,
        "merges": [
            {"from": p["from"], "into": p["into"], "matching_keys": p["matching_keys"]}
            for p in unambiguous
        ],
        "ambiguous": [
            {"from": p["from"], "candidates": p["candidates"]}
            for p in ambiguous
        ],
    }
    append_audit(args.audit, record)
    print(f"\nApplied: {result['merges_applied']} merges, "
          f"{result['edges_rerouted']} edges rerouted, {result['edges_dropped']} self-loops dropped.")
    print(f"Audit appended to {args.audit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
