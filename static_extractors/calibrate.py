"""Apply Phase A (snippet) and Phase B (AST) verifier reports to a catalog
payload's confidence values.

Combined verdict for `dependencies`:

  Phase A      Phase B       Combined            Confidence change
  ----------   -----------   -----------------   -----------------------------
  confirmed    confirmed     confirmed-strong    promote one tier
  confirmed    mixed         confirmed           promote one tier
  confirmed    disconfirmed  mixed               unchanged (cited line is
                                                  real but doesn't match the
                                                  claimed edge kind — flag
                                                  for triage)
  confirmed    unsupported   confirmed           promote one tier (B has no
                                                  rule for this lang/kind)
  mixed        any           mixed               unchanged
  disconfirmed any           disconfirmed        demote to "review"

For `resources` (and other categories the schema gives a confidence
field to): Phase B doesn't apply; calibration uses Phase A alone.

For all categories: each fact is annotated in place with `_cross_check`
(Phase A) and, for dependencies, `_cross_check_ast` (Phase B), so the
dashboard, reconcile audit, and downstream evals can see what happened
without re-running the verifier.
"""

from __future__ import annotations

from typing import Any

from static_extractors.ast_crosscheck import AstFactCheck, AstReport
from static_extractors.snippet_verify import FactCheck, VerifyReport


CONFIDENCE_PROMOTION = {
    "review": "low",
    "low": "medium",
    "medium": "high",
    "high": "high",
}

CONFIDENCE_DEMOTION_TARGET = "review"

CONFIDENCE_AWARE_CATEGORIES = {"dependencies", "resources"}


def _combined_verdict(a_verdict: str, b_verdict: str | None) -> tuple[str, str]:
    """Return (combined_verdict, explanation). b_verdict may be None when
    Phase B wasn't run (non-dependencies).
    """
    if a_verdict == "disconfirmed":
        return "disconfirmed", "phase A: no evidence verified"
    if a_verdict == "mixed":
        return "mixed", "phase A: partial evidence"
    if a_verdict == "empty":
        return "empty", "no evidence present"
    # a_verdict == "confirmed"
    if b_verdict in (None, "unsupported", "empty", "confirmed", "mixed"):
        return "confirmed", f"phase A: confirmed; phase B: {b_verdict or 'n/a'}"
    if b_verdict == "disconfirmed":
        return "mixed", "phase A confirmed citation but phase B AST pattern absent"
    return "confirmed", f"phase A: confirmed; phase B: {b_verdict}"


def _ast_by_index(ast_report: AstReport | None) -> dict[int, AstFactCheck]:
    if ast_report is None:
        return {}
    return {f.index: f for f in ast_report.facts}


def _confidence_change(combined_verdict: str, before: str) -> str:
    if combined_verdict == "confirmed":
        return CONFIDENCE_PROMOTION.get(before, before)
    if combined_verdict == "disconfirmed":
        return CONFIDENCE_DEMOTION_TARGET
    return before


def apply(
    payload: dict[str, Any],
    report_a: VerifyReport,
    report_b: AstReport | None = None,
) -> dict[str, Any]:
    """Mutate `payload` in place: apply combined Phase A + Phase B verdicts
    to `confidence` (where the schema has one) and stitch verdict
    annotations onto each fact for downstream consumers.

    Returns a summary record suitable for appending to
    reconcile_audit.jsonl.
    """
    changes: list[dict[str, Any]] = []
    ast_index = _ast_by_index(report_b)
    for fact_check in report_a.facts:
        cat = fact_check.category
        idx = fact_check.index
        a_verdict = fact_check.verdict
        a_payload = {
            "verdict": a_verdict,
            "evidence_total": fact_check.evidence_total,
            "evidence_matched": fact_check.evidence_matched,
            "evidence_partial": fact_check.evidence_partial,
            "evidence_missing": fact_check.evidence_missing,
            "evidence_invalid_path": fact_check.evidence_invalid_path,
        }

        b_check: AstFactCheck | None = None
        if cat == "dependencies":
            b_check = ast_index.get(idx)

        if cat == "repo.summary":
            repo = payload.get("repo")
            if isinstance(repo, dict):
                summary = repo.get("summary")
                if isinstance(summary, dict):
                    summary["_cross_check"] = a_payload
            continue

        items = payload.get(cat) or []
        if not (0 <= idx < len(items)):
            continue
        fact = items[idx]
        if not isinstance(fact, dict):
            continue

        fact["_cross_check"] = a_payload
        if b_check is not None:
            fact["_cross_check_ast"] = {
                "verdict": b_check.verdict,
                "kind": b_check.kind,
                "evidence_total": b_check.evidence_total,
                "confirmed": b_check.confirmed,
                "mixed": b_check.mixed,
                "disconfirmed": b_check.disconfirmed,
                "unsupported": b_check.unsupported,
            }

        if cat not in CONFIDENCE_AWARE_CATEGORIES:
            continue
        before = fact.get("confidence")
        if not before:
            continue
        b_verdict = b_check.verdict if b_check is not None else None
        combined, explanation = _combined_verdict(a_verdict, b_verdict)
        after = _confidence_change(combined, before)
        if after != before:
            fact["confidence"] = after
            changes.append(
                {
                    "category": cat,
                    "index": idx,
                    "label": fact_check.label,
                    "before": before,
                    "after": after,
                    "phase_a_verdict": a_verdict,
                    "phase_b_verdict": b_verdict,
                    "combined_verdict": combined,
                    "explanation": explanation,
                }
            )

    totals: dict[str, Any] = {"phase_a": report_a.to_dict()["totals"]}
    if report_b is not None:
        totals["phase_b"] = report_b.to_dict()["totals"]
    return {
        "totals": totals,
        "changes": changes,
    }


def collect_problems(
    payload: dict[str, Any],
    report_a: VerifyReport,
    report_b: AstReport | None = None,
) -> list[dict[str, Any]]:
    """Return a list of facts the LLM should re-examine. Used by the
    correction loop. A fact is a problem if its combined verdict is
    `disconfirmed` or `mixed`.

    The returned dicts are LLM-prompt-ready: they name the category and
    index, the human-readable label, the reasons each evidence item
    failed (concrete file:line + status + diagnostic message), and the
    fact's own current shape so the LLM can correct it without re-reading
    the rest of the catalog.
    """
    ast_index = _ast_by_index(report_b)
    out: list[dict[str, Any]] = []
    for fact_check in report_a.facts:
        cat = fact_check.category
        if cat == "repo.summary":
            continue
        items = payload.get(cat) or []
        if not (0 <= fact_check.index < len(items)):
            continue
        a_verdict = fact_check.verdict
        b_check = ast_index.get(fact_check.index) if cat == "dependencies" else None
        b_verdict = b_check.verdict if b_check else None
        combined, explanation = _combined_verdict(a_verdict, b_verdict)
        if combined not in ("disconfirmed", "mixed"):
            continue
        reasons: list[str] = []
        for d in fact_check.details:
            # Surface every non-matched evidence item: missing, invalid_path,
            # and partial (the partial case is the one that produces a Phase A
            # "mixed" verdict without specific reasons being recorded yet).
            if d.status in ("missing", "invalid_path", "partial"):
                reasons.append(
                    f"snippet check at {d.path}:{d.line} → {d.status} ({d.reason or 'no match'})"
                )
        if b_check is not None and b_check.verdict in ("disconfirmed", "mixed"):
            for d in b_check.details:
                if d.status in ("disconfirmed", "mixed"):
                    reasons.append(
                        f"AST check at {d.path}:{d.line} → {d.status} ({d.reason or 'pattern not found'})"
                    )
        # Strip in-band verifier annotations so the LLM sees the bare fact
        # rather than the cross-check verdict (which would confuse it).
        bare_fact = {
            k: v for k, v in items[fact_check.index].items()
            if k not in {"_cross_check", "_cross_check_ast"}
        }
        out.append(
            {
                "category": cat,
                "index": fact_check.index,
                "label": fact_check.label,
                "combined_verdict": combined,
                "explanation": explanation,
                "phase_a_verdict": a_verdict,
                "phase_b_verdict": b_verdict,
                "reasons": reasons,
                "current_fact": bare_fact,
            }
        )
    return out
