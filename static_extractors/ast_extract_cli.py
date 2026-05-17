"""Apply the AST-as-extractor (Phase B as primary signal) to existing
catalog extractions.

Usage:

    python -m static_extractors.ast_extract_cli \\
        --catalog-dir evals/data/extractions \\
        --workspace evals/workspace
        [--apply]   # mutate extractions in place; otherwise dry-run report

Without `--apply`, prints a per-repo report of what edges the AST would
add, correct, or confirm. With `--apply`, writes the merged result back
to each JSON file.

This is the "AST owns structural facts" arm of the hybrid pipeline
sketched in ARCHITECTURE.md. The LLM remains the source of semantic
facts (taglines, capability sheets, domain assignment). When AST and
LLM disagree on edge kind/direction, AST wins (the regex-resolved
literal queue/topic name in a publish call is more reliable than the
LLM's natural-language inference of direction).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from static_extractors import ast_extract


def _repo_root_for(payload: dict[str, Any], workspace_root: Path) -> Path | None:
    repo_id = (payload.get("repo") or {}).get("id") or ""
    if not repo_id:
        return None
    candidates = [
        workspace_root / repo_id,
        workspace_root / repo_id.split("/", 1)[-1],
    ]
    return next((c for c in candidates if c.is_dir()), None)


def _process_one(
    path: Path,
    workspace_root: Path,
    *,
    apply: bool,
) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    repo_root = _repo_root_for(payload, workspace_root)
    if repo_root is None:
        return {"file": str(path), "status": "skip", "reason": "repo source not found"}

    components = payload.get("components") or []
    # Sock-shop convention: one Component per repo; the component_name is
    # the repo name slug. For multi-component repos, we'd iterate.
    primary_component = (
        components[0].get("name")
        if components and isinstance(components[0], dict)
        else (payload.get("repo") or {}).get("id", "").split("/", 1)[-1]
    )

    edges = ast_extract.extract_messaging_edges(repo_root, component_name=primary_component)
    summary = ast_extract.merge_into_payload(payload, edges, component_name=primary_component)

    result = {
        "file": str(path),
        "status": "ok",
        "component": primary_component,
        "edges_found": len(edges),
        "summary": summary,
    }

    if apply and (summary["added"] or summary["kind_corrected"]):
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        result["written"] = True

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-dir", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--apply", action="store_true",
                        help="Write merged catalogs in place. Without this flag, dry-run only.")
    parser.add_argument("--only", default="")
    args = parser.parse_args()

    if not args.catalog_dir.is_dir():
        print(f"catalog-dir not a directory: {args.catalog_dir}", file=sys.stderr)
        return 2
    if not args.workspace.is_dir():
        print(f"workspace not a directory: {args.workspace}", file=sys.stderr)
        return 2

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    results = []
    for path in sorted(args.catalog_dir.glob("*.json")):
        if only and path.stem not in only:
            continue
        results.append(_process_one(path, args.workspace.resolve(), apply=args.apply))

    aggregate = {
        "totals": {
            "files": len(results),
            "edges_found": sum(r.get("edges_found", 0) for r in results),
            "added": sum(len((r.get("summary") or {}).get("added") or []) for r in results),
            "kind_corrected": sum(len((r.get("summary") or {}).get("kind_corrected") or []) for r in results),
            "confirmed": sum(len((r.get("summary") or {}).get("confirmed") or []) for r in results),
            "resources_added": sum(len((r.get("summary") or {}).get("resources_added") or []) for r in results),
        },
        "per_repo": results,
    }
    print(json.dumps(aggregate, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
