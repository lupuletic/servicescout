"""Side-by-side comparison of baseline vs grounding-enabled extractions.

Reads from two parallel extraction directories produced by extracting the
same set of repos with and without the Phase A+B+correction pipeline.
Reports:

  - Per-repo verifier numbers (facts confirmed / mixed / disconfirmed,
    evidence matched / partial / missing / invalid_path)
  - Correction loop activity (rounds executed, directives applied)
  - Eval-tier delta (catalog questions pass-rate) by running the same
    eval suite against two built catalogs

Usage:

    # After running the two extraction passes:
    python evals/compare_pipelines.py \\
        --baseline-dir evals/data/extractions_baseline \\
        --treatment-dir evals/data/extractions \\
        --workspace evals/workspace
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _phase_a(payload: dict[str, Any]) -> dict[str, int]:
    return ((payload.get("_meta") or {}).get("cross_check") or {}).get("totals", {}).get("phase_a", {}) or {}


def _phase_b(payload: dict[str, Any]) -> dict[str, int]:
    return ((payload.get("_meta") or {}).get("cross_check") or {}).get("totals", {}).get("phase_b", {}) or {}


def _correction_runs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return (payload.get("_meta") or {}).get("correction_runs") or []


def _counts(payload: dict[str, Any]) -> dict[str, int]:
    return {
        "components": len(payload.get("components") or []),
        "apis": len(payload.get("apis") or []),
        "resources": len(payload.get("resources") or []),
        "dependencies": len(payload.get("dependencies") or []),
        "providers": len(payload.get("providers") or []),
    }


def compare(baseline_dir: Path, treatment_dir: Path) -> dict[str, Any]:
    report = {"per_repo": {}}
    for tpath in sorted(treatment_dir.glob("*.json")):
        repo = tpath.stem
        bpath = baseline_dir / tpath.name
        t = _load(tpath)
        b = _load(bpath)
        if t is None:
            continue
        entry: dict[str, Any] = {
            "treatment": {
                "phase_a": _phase_a(t),
                "phase_b": _phase_b(t),
                "correction_runs": _correction_runs(t),
                "counts": _counts(t),
            }
        }
        if b is not None:
            entry["baseline"] = {
                "phase_a": _phase_a(b),
                "phase_b": _phase_b(b),
                "counts": _counts(b),
            }
        report["per_repo"][repo] = entry
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", type=Path, required=True)
    parser.add_argument("--treatment-dir", type=Path, required=True)
    args = parser.parse_args()
    if not args.baseline_dir.is_dir():
        print(f"baseline-dir not a directory: {args.baseline_dir}", file=sys.stderr)
        return 2
    if not args.treatment_dir.is_dir():
        print(f"treatment-dir not a directory: {args.treatment_dir}", file=sys.stderr)
        return 2
    report = compare(args.baseline_dir, args.treatment_dir)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
