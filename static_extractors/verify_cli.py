"""Post-hoc cross-checker for existing extractions.

Run the snippet substring verifier (Phase A) over already-extracted catalog
JSON files without re-running the LLM. Useful for:

  - Retrofitting confidence calibration onto an existing workspace catalog.
  - Producing a quality report (how many facts hold up to their own citations?).
  - A/B comparing prompt changes by diffing reports.

Usage:

    python -m static_extractors.verify_cli \\
        --catalog-dir evals/data/extractions \\
        --workspace evals/workspace \\
        --report report.json

    # Apply confidence changes in place:
    python -m static_extractors.verify_cli \\
        --catalog-dir evals/data/extractions \\
        --workspace evals/workspace \\
        --apply

`--workspace` should point at the directory containing the cloned repo
trees (each entry's `repo.id` slug is used as the subdirectory name).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from static_extractors import ast_crosscheck, calibrate, snippet_verify


def _repo_root_for(payload: dict[str, Any], workspace_root: Path) -> Path | None:
    repo_id = (payload.get("repo") or {}).get("id") or ""
    if not repo_id:
        return None
    # Try repo_id as a full path under workspace_root (handles "org/repo"
    # → workspace/org/repo), then fall back to just the trailing slug
    # (handles "org/repo" → workspace/repo, which is the sock-shop layout
    # produced by evals/setup.sh).
    candidates = [
        workspace_root / repo_id,
        workspace_root / repo_id.split("/", 1)[-1],
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return None


def _process_one(
    path: Path,
    workspace_root: Path,
    apply: bool,
) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    repo_root = _repo_root_for(payload, workspace_root)
    result: dict[str, Any] = {
        "file": str(path),
        "repo_id": (payload.get("repo") or {}).get("id"),
    }
    if repo_root is None:
        result["status"] = "skip"
        result["reason"] = "repo source dir not found under workspace"
        return result

    report_a = snippet_verify.verify_payload(payload, repo_root)
    report_b = ast_crosscheck.verify_payload(payload, repo_root)
    result["status"] = "ok"
    result["repo_root"] = str(repo_root)
    result["totals"] = report_a.to_dict()["totals"]
    result["totals_ast"] = report_b.to_dict()["totals"]

    if apply:
        change_report = calibrate.apply(payload, report_a, report_b)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        result["changes"] = change_report["changes"]
        result["applied"] = True
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-dir", type=Path, required=True,
                        help="Directory of per-repo extraction JSONs.")
    parser.add_argument("--workspace", type=Path, required=True,
                        help="Directory of cloned repo source trees.")
    parser.add_argument("--apply", action="store_true",
                        help="Mutate extractions in place with new confidence values.")
    parser.add_argument("--report", type=Path, default=None,
                        help="Write aggregate JSON report to this path.")
    parser.add_argument("--only", default="",
                        help="Comma-separated list of repo names to limit processing.")
    args = parser.parse_args()

    cat_dir: Path = args.catalog_dir.resolve()
    ws_root: Path = args.workspace.resolve()
    if not cat_dir.is_dir():
        print(f"catalog-dir not a directory: {cat_dir}", file=sys.stderr)
        return 2
    if not ws_root.is_dir():
        print(f"workspace not a directory: {ws_root}", file=sys.stderr)
        return 2

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    results: list[dict[str, Any]] = []
    for path in sorted(cat_dir.glob("*.json")):
        if only and path.stem not in only:
            continue
        results.append(_process_one(path, ws_root, apply=args.apply))

    aggregate = {
        "totals": {
            "files": len(results),
            "ok": sum(1 for r in results if r.get("status") == "ok"),
            "skipped": sum(1 for r in results if r.get("status") == "skip"),
            "phase_a": {
                "facts": sum((r.get("totals") or {}).get("facts", 0) for r in results),
                "facts_confirmed": sum((r.get("totals") or {}).get("facts_confirmed", 0) for r in results),
                "facts_mixed": sum((r.get("totals") or {}).get("facts_mixed", 0) for r in results),
                "facts_disconfirmed": sum((r.get("totals") or {}).get("facts_disconfirmed", 0) for r in results),
                "evidence": sum((r.get("totals") or {}).get("evidence", 0) for r in results),
                "evidence_matched": sum((r.get("totals") or {}).get("matched", 0) for r in results),
                "evidence_partial": sum((r.get("totals") or {}).get("partial", 0) for r in results),
                "evidence_missing": sum((r.get("totals") or {}).get("missing", 0) for r in results),
                "evidence_invalid_path": sum((r.get("totals") or {}).get("invalid_path", 0) for r in results),
            },
            "phase_b": {
                "facts": sum((r.get("totals_ast") or {}).get("facts", 0) for r in results),
                "facts_confirmed": sum((r.get("totals_ast") or {}).get("facts_confirmed", 0) for r in results),
                "facts_mixed": sum((r.get("totals_ast") or {}).get("facts_mixed", 0) for r in results),
                "facts_disconfirmed": sum((r.get("totals_ast") or {}).get("facts_disconfirmed", 0) for r in results),
                "facts_unsupported": sum((r.get("totals_ast") or {}).get("facts_unsupported", 0) for r in results),
            },
            "changes_applied": sum(len(r.get("changes") or []) for r in results),
        },
        "per_repo": results,
    }
    out = json.dumps(aggregate, indent=2, sort_keys=True)
    if args.report:
        args.report.write_text(out + "\n", encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
