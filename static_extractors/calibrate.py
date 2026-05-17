"""Apply a snippet-verify report to a catalog payload's confidence values.

Rules:

  - `confirmed`  → promote one tier (medium→high; low→medium; review→low).
                   `high` stays `high`.
  - `mixed`      → unchanged (some evidence holds, some doesn't — let humans
                   triage in the dashboard).
  - `disconfirmed` → demote to `review` regardless of starting confidence.
                   These are the bug-catching demotions Epic #9 #1 calls out.
  - `empty`      → unchanged (should not happen post-schema-validation).

Only the `dependencies` and `resources` arrays carry a `confidence` field
in catalog_schema.json. Other categories (components, apis, providers,
domain_attributes, glossary, repo.summary) get verifier annotations on the
payload but no confidence mutation.

The verifier's per-fact verdicts are also stitched onto each fact under
the `_cross_check` key so downstream consumers (dashboard triage,
reconcile.py audit log, eval reports) can see what happened without
re-running the verifier.
"""

from __future__ import annotations

from typing import Any

from static_extractors.snippet_verify import VerifyReport


CONFIDENCE_PROMOTION = {
    "review": "low",
    "low": "medium",
    "medium": "high",
    "high": "high",
}

CONFIDENCE_DEMOTION_TARGET = "review"

CONFIDENCE_AWARE_CATEGORIES = {"dependencies", "resources"}


def apply(payload: dict[str, Any], report: VerifyReport) -> dict[str, Any]:
    """Mutate `payload` in place, applying confidence changes derived from
    the verifier report. Returns a structured summary describing what
    changed, suitable for appending to reconcile_audit.jsonl.
    """
    changes: list[dict[str, Any]] = []
    for fact_check in report.facts:
        cat = fact_check.category
        idx = fact_check.index
        verdict = fact_check.verdict
        cross_check_payload = {
            "verdict": verdict,
            "evidence_total": fact_check.evidence_total,
            "evidence_matched": fact_check.evidence_matched,
            "evidence_partial": fact_check.evidence_partial,
            "evidence_missing": fact_check.evidence_missing,
            "evidence_invalid_path": fact_check.evidence_invalid_path,
        }
        if cat == "repo.summary":
            repo = payload.get("repo")
            if isinstance(repo, dict):
                summary = repo.get("summary")
                if isinstance(summary, dict):
                    summary["_cross_check"] = cross_check_payload
            continue
        items = payload.get(cat) or []
        if not (0 <= idx < len(items)):
            continue
        fact = items[idx]
        if not isinstance(fact, dict):
            continue
        fact["_cross_check"] = cross_check_payload

        if cat not in CONFIDENCE_AWARE_CATEGORIES:
            continue
        before = fact.get("confidence")
        if not before:
            continue
        after = before
        if verdict == "confirmed":
            after = CONFIDENCE_PROMOTION.get(before, before)
        elif verdict == "disconfirmed":
            after = CONFIDENCE_DEMOTION_TARGET
        if after != before:
            fact["confidence"] = after
            changes.append(
                {
                    "category": cat,
                    "index": idx,
                    "label": fact_check.label,
                    "before": before,
                    "after": after,
                    "verdict": verdict,
                }
            )
    return {
        "totals": report.to_dict()["totals"],
        "changes": changes,
    }
