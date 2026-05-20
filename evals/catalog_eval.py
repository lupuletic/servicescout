"""Catalog-tier evaluation — grades ServiceScout's query layer against
hand-curated ground truth by driving the same `Backend` interface the MCP
server uses. Free per run, deterministic, finishes in milliseconds.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from storage import Backend, DEFAULT_FLOW_EDGE_TYPES, make_backend  # noqa: E402

from score import recall_any_of, recall_set  # noqa: E402


PASS_THRESHOLD = 1.0


def evaluate_question(backend: Any, question: dict) -> dict:
    """Run all catalog expectations on one question. Returns the result record."""
    qid = question["id"]
    catalog_exp = question.get("catalog") or {}
    metrics: dict[str, float] = {}
    details: dict[str, Any] = {}

    # --- search ---
    if "search_must_include_any_of" in catalog_exp:
        top_k = int(catalog_exp.get("search_top_k", 5))
        hits = backend.search(question["question"], query_vector=None, limit=top_k)
        refs = [h["ref"] for h in hits]
        details["search_returned"] = refs
        metrics["search_recall"] = recall_any_of(refs, catalog_exp["search_must_include_any_of"])

    # --- neighbors ---
    if "neighbors" in catalog_exp:
        spec = catalog_exp["neighbors"]
        paths = backend.neighbors(
            spec["of"],
            direction=spec.get("direction", "out"),
            depth=int(spec.get("depth", 1)),
            edge_types=spec.get("edge_types"),
        )
        if spec.get("direction") == "in":
            found = [p["from"] for p in paths]
            expected = spec.get("must_include_sources_any_of") or []
        elif spec.get("direction") == "both":
            found = [p["from"] for p in paths] + [p["to"] for p in paths]
            expected = spec.get("must_include_neighbors_any_of") or []
        else:
            found = [p["to"] for p in paths]
            expected = spec.get("must_include_targets_any_of") or []
        details["neighbors_returned"] = sorted(set(found))
        if expected:
            metrics["neighbors_recall"] = recall_set(found, expected)

    # --- must_not_exist (negative structural assertions) ---
    if "must_not_exist_refs" in catalog_exp:
        forbidden = catalog_exp["must_not_exist_refs"]
        present = [ref for ref in forbidden if backend.describe(ref) is not None]
        details["forbidden_refs_present"] = present
        metrics["no_forbidden_refs"] = 1.0 if not present else 0.0

    # --- trace ---
    if "trace" in catalog_exp:
        spec = catalog_exp["trace"]
        plan = backend.trace(
            start_ref=spec["start"],
            end_match=spec.get("end"),
            max_hops=int(spec.get("max_hops", 5)),
            edge_types=spec.get("edge_types") or DEFAULT_FLOW_EDGE_TYPES,
            include_async=bool(spec.get("include_async", True)),
            fanout_per_node=int(spec.get("fanout_per_node", 12)),
        )
        visited = {hop["to"] for hop in plan.get("hops", [])}
        visited.add(spec["start"])
        details["trace_visited"] = sorted(visited)
        expected = spec.get("must_visit_any_of") or []
        if expected:
            metrics["trace_recall"] = recall_any_of(visited, expected)

    passed = all(v >= PASS_THRESHOLD for v in metrics.values()) if metrics else True

    return {
        "question_id": qid,
        "category": question.get("category", "uncategorised"),
        "question": question["question"],
        "metrics": metrics,
        "details": details,
        "pass": passed,
    }


def run_catalog_eval(backend: Backend, questions: list[dict]) -> list[dict]:
    return [evaluate_question(backend, q) for q in questions]
