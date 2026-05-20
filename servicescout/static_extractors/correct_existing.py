"""Apply the Phase A+B correction loop to existing extractions.

Same code path as the in-line correction in extractor.run_for_repo, but
operates on already-written catalog JSONs so we don't re-pay the initial
extraction LLM cost. Useful for:

  - Retrofitting corrections on extractions produced before the
    correction loop existed.
  - Validating a correction-loop fix without re-running the initial
    extraction.

Usage:

    python -m static_extractors.correct_existing \\
        --catalog-dir evals/data/extractions \\
        --workspace evals/workspace \\
        --provider codex --model gpt-5.4-mini --effort medium \\
        --only orders,shipping,queue-master
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from servicescout import extractor as ex
from servicescout.static_extractors import calibrate, code_shape as ast_crosscheck, correction, snippet_verify


def _repo_dict_for(payload: dict[str, Any], workspace_root: Path) -> dict[str, Any] | None:
    repo_id = (payload.get("repo") or {}).get("id") or ""
    if not repo_id:
        return None
    candidates = [
        workspace_root / repo_id,
        workspace_root / repo_id.split("/", 1)[-1],
    ]
    repo_root = next((c for c in candidates if c.is_dir()), None)
    if repo_root is None:
        return None
    return {
        "id": repo_id,
        "name": repo_id.split("/", 1)[-1],
        "absolute_path": str(repo_root),
        "commit": (payload.get("_meta") or {}).get("commit", ""),
    }


def _correct_one(
    path: Path,
    workspace_root: Path,
    *,
    provider: str,
    model: str | None,
    effort: str,
    max_budget_usd: str | None,
    timeout_seconds: int,
    rounds: int,
) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    repo = _repo_dict_for(payload, workspace_root)
    if repo is None:
        return {"file": str(path), "status": "skip", "reason": "repo source not found"}
    repo_root = Path(repo["absolute_path"])
    correction_runs: list[dict[str, Any]] = []
    for round_idx in range(rounds):
        report_a = snippet_verify.verify_payload(payload, repo_root)
        report_b = ast_crosscheck.verify_payload(payload, repo_root)
        problems = calibrate.collect_problems(payload, report_a, report_b)
        if not problems:
            break
        try:
            prompt = correction.build_correction_prompt(
                repo["id"], repo["absolute_path"], problems
            )
            if provider == "claude":
                raw, c_run = ex.claude_correct(repo, prompt, model or "sonnet", effort, max_budget_usd)
            else:
                raw, c_run = ex.codex_correct(
                    repo, prompt, model, effort, timeout_seconds=timeout_seconds
                )
            directives = correction.parse_correction_response(raw)
            apply_summary = correction.apply_corrections(payload, directives)
            quarantined = ex.confine_evidence_paths(payload, repo_root)
            errors = ex.validate_against_schema(payload)
            correction_runs.append(
                {
                    "round": round_idx + 1,
                    "problems_input": len(problems),
                    "directives_returned": len(directives),
                    "applied": apply_summary,
                    "run": c_run,
                    "quarantined_after": quarantined,
                    "post_correction_validation_errors": errors,
                }
            )
            if errors:
                break
        except Exception as exc:  # noqa: BLE001
            correction_runs.append(
                {"round": round_idx + 1, "problems_input": len(problems), "error": str(exc)}
            )
            break

    # Final calibrate on the (possibly-corrected) payload.
    report_a = snippet_verify.verify_payload(payload, repo_root)
    report_b = ast_crosscheck.verify_payload(payload, repo_root)
    cross_check = calibrate.apply(payload, report_a, report_b)

    meta = payload.setdefault("_meta", {})
    meta["cross_check"] = cross_check
    meta["correction_runs"] = correction_runs

    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "file": str(path),
        "status": "ok",
        "rounds": len(correction_runs),
        "applied_total": {
            "fix": sum(r.get("applied", {}).get("fix", 0) for r in correction_runs),
            "drop": sum(r.get("applied", {}).get("drop", 0) for r in correction_runs),
            "keep": sum(r.get("applied", {}).get("keep", 0) for r in correction_runs),
        },
        "final_phase_a": cross_check["totals"].get("phase_a"),
        "final_phase_b": cross_check["totals"].get("phase_b"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-dir", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--provider", choices=("codex", "claude"), default="codex")
    parser.add_argument("--model", default=None)
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--max-budget-usd", default=None)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--only", default="")
    args = parser.parse_args()

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    results = []
    for path in sorted(args.catalog_dir.glob("*.json")):
        if only and path.stem not in only:
            continue
        result = _correct_one(
            path,
            args.workspace,
            provider=args.provider,
            model=args.model,
            effort=args.effort,
            max_budget_usd=args.max_budget_usd,
            timeout_seconds=args.timeout_seconds,
            rounds=args.rounds,
        )
        print(json.dumps(result, indent=2))
        results.append(result)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
