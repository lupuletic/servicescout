"""Generate a reviewable tag alias map for catalog facets.

The catalog keeps raw tags from extraction. This command asks the configured
LLM to propose semantic tag groups, writes ``data/tag_aliases.json``, and the
dashboard uses that map to display/filter canonical tags.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from servicescout.extractor import _codex_headless_flags
from servicescout.tags import normalize_tag


HERE = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = HERE / "data" / "catalog.json"
DEFAULT_OUTPUT = HERE / "data" / "tag_aliases.json"
CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


TAG_GROUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["groups"],
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["canonical", "aliases", "confidence", "reason"],
                "properties": {
                    "canonical": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}


def collect_tag_inventory(catalog: dict[str, Any], *, max_samples: int = 5) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for entity in catalog.get("entities") or []:
        meta = entity.get("metadata") or {}
        ref = f"{entity.get('kind')}:{meta.get('name')}"
        for tag in meta.get("tags") or []:
            raw = str(tag or "").strip()
            if not raw:
                continue
            record = records.setdefault(
                raw,
                {
                    "tag": raw,
                    "normalised": normalize_tag(raw),
                    "count": 0,
                    "entity_kinds": set(),
                    "samples": [],
                },
            )
            record["count"] += 1
            if entity.get("kind"):
                record["entity_kinds"].add(entity["kind"])
            if len(record["samples"]) < max_samples:
                record["samples"].append(ref)
    out: list[dict[str, Any]] = []
    for record in records.values():
        out.append({
            **record,
            "entity_kinds": sorted(record["entity_kinds"]),
        })
    return sorted(out, key=lambda item: (-int(item["count"]), str(item["normalised"]), str(item["tag"])))


def automatic_spelling_groups(inventory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_normal: dict[str, list[str]] = {}
    for item in inventory:
        normal = item["normalised"]
        if normal:
            by_normal.setdefault(normal, []).append(item["tag"])
    groups: list[dict[str, Any]] = []
    for normal, raw_tags in sorted(by_normal.items()):
        if len(raw_tags) > 1 or raw_tags[0] != normal:
            groups.append({
                "canonical": normal,
                "aliases": sorted(raw_tags),
                "confidence": "high",
                "reason": "Same tag after casing/separator normalisation.",
                "source": "deterministic",
            })
    return groups


def tag_inventory_fingerprint(inventory: list[dict[str, Any]]) -> str:
    """Stable fingerprint of the tag inventory that affects alias output."""
    payload = [
        {
            "tag": item.get("tag"),
            "normalised": item.get("normalised"),
            "count": int(item.get("count") or 0),
            "entity_kinds": sorted(item.get("entity_kinds") or []),
        }
        for item in sorted(
            inventory,
            key=lambda tag: (str(tag.get("normalised") or ""), str(tag.get("tag") or "")),
        )
    ]
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def is_current_alias_document(
    path: Path,
    *,
    inventory_fingerprint: str,
    provider: str | None,
    model: str | None,
    min_confidence: str,
    llm_assist: bool,
    max_tags: int,
) -> bool:
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    options = payload.get("options") or {}
    return (
        payload.get("schema_version") == "tag-aliases-v1"
        and payload.get("inventory_fingerprint") == inventory_fingerprint
        and payload.get("provider") == provider
        and payload.get("model") == model
        and payload.get("min_confidence") == min_confidence
        and options.get("llm_assist") == llm_assist
        and int(options.get("max_tags") or 0) == int(max_tags)
    )


def build_alias_document(
    inventory: list[dict[str, Any]],
    llm_groups: list[dict[str, Any]],
    *,
    provider: str | None,
    model: str | None,
    min_confidence: str,
    catalog_path: Path,
    inventory_fingerprint: str | None = None,
    llm_assist: bool = True,
    max_tags: int | None = None,
) -> dict[str, Any]:
    min_rank = CONFIDENCE_RANK[min_confidence]
    by_raw = {item["tag"]: item for item in inventory}
    by_normal: dict[str, list[str]] = {}
    for item in inventory:
        by_normal.setdefault(item["normalised"], []).append(item["tag"])

    aliases = {item["tag"]: item["normalised"] for item in inventory if item["normalised"]}
    groups = automatic_spelling_groups(inventory)
    for group in llm_groups:
        if CONFIDENCE_RANK.get(str(group.get("confidence") or ""), 0) < min_rank:
            continue
        canonical = normalize_tag(group.get("canonical"))
        if not canonical:
            continue
        raw_matches: set[str] = set()
        for alias in [group.get("canonical"), *(group.get("aliases") or [])]:
            alias_raw = str(alias or "").strip()
            alias_normal = normalize_tag(alias_raw)
            if alias_raw in by_raw:
                raw_matches.add(alias_raw)
            raw_matches.update(by_normal.get(alias_normal, []))
        if len(raw_matches) < 2:
            continue
        for raw in raw_matches:
            aliases[raw] = canonical
        groups.append({
            "canonical": canonical,
            "aliases": sorted(raw_matches),
            "confidence": group.get("confidence") or "medium",
            "reason": str(group.get("reason") or "")[:240],
            "source": "llm",
        })

    return {
        "schema_version": "tag-aliases-v1",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_catalog": str(catalog_path),
        "inventory_fingerprint": inventory_fingerprint or tag_inventory_fingerprint(inventory),
        "provider": provider,
        "model": model,
        "min_confidence": min_confidence,
        "options": {
            "llm_assist": llm_assist,
            "max_tags": max_tags,
        },
        "aliases": dict(sorted(aliases.items(), key=lambda item: normalize_tag(item[0]))),
        "groups": groups,
    }


def llm_propose_tag_groups(
    inventory: list[dict[str, Any]],
    *,
    provider: str,
    model: str | None,
    max_tags: int,
    scratch_dir: Path,
) -> list[dict[str, Any]]:
    compact_inventory = [
        {
            "tag": item["tag"],
            "normalised": item["normalised"],
            "count": item["count"],
            "entity_kinds": item["entity_kinds"],
            "samples": item["samples"][:3],
        }
        for item in inventory[:max_tags]
    ]
    prompt = (
        "You are reconciling tag facets for a software catalog. Tags come from many "
        "repositories and may contain casing differences, abbreviations, framework "
        "names, vendor names, or implementation technologies.\n\n"
        "Group tags only when they are genuinely the same concept for filtering. "
        "Be conservative. Do not merge broad/narrow concepts: for example, do not "
        "merge a language with a framework, a runtime with a cloud provider, or "
        "a generic tag with a specific product. Prefer canonical values in lower "
        "kebab-case. If unsure, omit the group.\n\n"
        "Return only groups where at least two existing tags should map to one "
        "canonical value. Aliases must refer to tags in the inventory.\n\n"
        f"TAG INVENTORY:\n{json.dumps(compact_inventory, indent=2)}\n"
    )
    if provider == "claude":
        return _claude_tag_call(prompt, model or "sonnet")
    return _codex_tag_call(prompt, model or "gpt-5.4-mini", scratch_dir)


def _codex_tag_call(prompt: str, model: str, scratch_dir: Path) -> list[dict[str, Any]]:
    codex = shutil.which("codex")
    if not codex:
        raise SystemExit("codex CLI was not found on PATH")
    scratch_dir.mkdir(parents=True, exist_ok=True)
    schema_path = scratch_dir / "tag_group_schema.json"
    result_path = scratch_dir / "tag_group_result.json"
    schema_path.write_text(json.dumps(TAG_GROUP_SCHEMA, indent=2), encoding="utf-8")
    if result_path.exists():
        result_path.unlink()
    cmd = [
        codex,
        "exec",
        "--ephemeral",
        *_codex_headless_flags(),
        "-c",
        'model_reasoning_effort="medium"',
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(result_path),
        "--model",
        model,
        prompt,
    ]
    completed = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
    if completed.returncode != 0:
        raise SystemExit(f"codex tag reconciliation failed: {(completed.stderr or completed.stdout)[-1000:]}")
    return _read_groups(result_path)


def _claude_tag_call(prompt: str, model: str) -> list[dict[str, Any]]:
    claude = shutil.which("claude")
    if not claude:
        raise SystemExit("claude CLI was not found on PATH")
    cmd = [
        claude,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(TAG_GROUP_SCHEMA, separators=(",", ":")),
        "--permission-mode",
        "dontAsk",
        "--model",
        model,
        "--allowedTools",
        "Read",
    ]
    completed = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
    if completed.returncode != 0:
        raise SystemExit(f"claude tag reconciliation failed: {(completed.stderr or completed.stdout)[-1000:]}")
    try:
        raw = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(raw, dict) and isinstance(raw.get("structured_output"), dict):
        return raw["structured_output"].get("groups") or []
    if isinstance(raw, dict) and isinstance(raw.get("result"), str):
        try:
            return json.loads(raw["result"]).get("groups") or []
        except json.JSONDecodeError:
            return []
    if isinstance(raw, dict):
        return raw.get("groups") or []
    return []


def _read_groups(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    groups = payload.get("groups") or []
    return groups if isinstance(groups, list) else []


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{int(time.time())}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--provider", choices=("codex", "claude"), default=os.getenv("LLM_PROVIDER") or "codex")
    parser.add_argument("--model", default=os.getenv("LLM_MODEL") or None)
    parser.add_argument("--min-confidence", choices=("high", "medium", "low"), default="medium")
    parser.add_argument("--max-tags", type=int, default=500)
    parser.add_argument("--no-llm", action="store_true", help="Only write casing/separator normalisation aliases.")
    parser.add_argument("--skip-unchanged", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help="Print the generated alias document instead of writing it.")
    args = parser.parse_args()

    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    inventory = collect_tag_inventory(catalog)
    fingerprint = tag_inventory_fingerprint(inventory)
    llm_assist = not args.no_llm
    provider = None if args.no_llm else args.provider
    if args.skip_unchanged and not args.dry_run and is_current_alias_document(
        args.output,
        inventory_fingerprint=fingerprint,
        provider=provider,
        model=args.model,
        min_confidence=args.min_confidence,
        llm_assist=llm_assist,
        max_tags=args.max_tags,
    ):
        existing = json.loads(args.output.read_text(encoding="utf-8"))
        print(json.dumps({
            "tags_seen": len(inventory),
            "aliases": len(existing.get("aliases") or {}),
            "groups": len(existing.get("groups") or []),
            "llm_groups_returned": None,
            "output": str(args.output),
            "inventory_fingerprint": fingerprint,
            "skipped": True,
            "reason": "tag_inventory_unchanged",
        }, indent=2))
        return 0

    llm_groups: list[dict[str, Any]] = []
    if llm_assist and inventory:
        llm_groups = llm_propose_tag_groups(
            inventory,
            provider=args.provider,
            model=args.model,
            max_tags=args.max_tags,
            scratch_dir=args.output.parent / ".tag-reconcile-scratch",
        )
    document = build_alias_document(
        inventory,
        llm_groups,
        provider=provider,
        model=args.model,
        min_confidence=args.min_confidence,
        catalog_path=args.catalog,
        inventory_fingerprint=fingerprint,
        llm_assist=llm_assist,
        max_tags=args.max_tags,
    )

    summary = {
        "tags_seen": len(inventory),
        "aliases": len(document["aliases"]),
        "groups": len(document["groups"]),
        "llm_groups_returned": len(llm_groups),
        "output": str(args.output),
        "inventory_fingerprint": fingerprint,
        "skipped": False,
    }
    print(json.dumps(summary, indent=2))
    if args.dry_run:
        print(json.dumps(document, indent=2))
        return 0
    write_json(args.output, document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
