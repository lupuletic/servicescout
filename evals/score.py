"""Scoring helpers + shared constants used by catalog_eval / agent_eval /
plots / report.
"""

from __future__ import annotations

import re
from typing import Iterable


CATEGORIES = ["routing", "sync-multihop", "async-multihop", "blast-radius"]
JUDGE_AXES = ["correctness", "specificity", "completeness", "hallucination_risk"]


def any_match(items: Iterable[str], expected_any_of: Iterable[str]) -> bool:
    """True if any item case-insensitively contains any expected token, or vice versa."""
    items_lower = [s.lower() for s in items if s]
    for exp in expected_any_of:
        if not exp:
            continue
        exp_lower = exp.lower()
        for it in items_lower:
            if exp_lower in it or it in exp_lower:
                return True
    return False


def recall_any_of(items: Iterable[str], expected_any_of: Iterable[str]) -> float:
    """1.0 if any expected_any_of element appears in items (or vice versa), else 0.0.

    "Any of" semantics — the question is satisfied if even one expected element
    matches. Most catalog questions are of this form because there are usually
    several valid ways to express the same answer.
    """
    return 1.0 if any_match(items, expected_any_of) else 0.0


def recall_set(found: Iterable[str], expected: Iterable[str]) -> float:
    """|found ∩ expected| / |expected|. 1.0 when expected is empty.

    Containment is one-way: an expected token counts as found when any
    `found` element contains it. Reversing the check let things like
    `user` match `username`.
    """
    exp = {e.lower() for e in expected if e}
    if not exp:
        return 1.0
    fnd = {f.lower() for f in found if f}
    return sum(1 for e in exp if any(e in f for f in fnd)) / len(exp)


def text_mentions_any(text: str, expected_any_of: Iterable[str]) -> bool:
    """Case-insensitive substring match — used to grade free-form agent answers."""
    if not text:
        return False
    t = text.lower()
    return any(exp.lower() in t for exp in expected_any_of if exp)


def text_mentions_all(text: str, expected_all_of: Iterable[str]) -> bool:
    if not text:
        return False
    t = text.lower()
    return all(exp.lower() in t for exp in expected_all_of if exp)


def repos_mentioned(text: str, candidate_repos: Iterable[str]) -> list[str]:
    """Return the subset of candidate_repos referenced in the text.

    Word-boundary match on the bare name (e.g. `user` in `microservices-demo/user`).
    Short bare names (<4 chars) are skipped to avoid common-word collisions.
    """
    if not text:
        return []
    t = text.lower()
    out: list[str] = []
    for repo in candidate_repos:
        name = repo.lower().split("/")[-1]
        if len(name) >= 4 and re.search(rf"\b{re.escape(name)}\b", t):
            out.append(repo)
    return out


def aggregate(per_question: list[dict], key: str) -> float:
    vals = [q.get(key) for q in per_question if isinstance(q.get(key), (int, float))]
    return sum(vals) / len(vals) if vals else 0.0
