"""Phase A — snippet substring cross-check.

For every `evidence[].{path, line, snippet}` item the LLM emits, open the
cited file at the cited line (±2 lines of context) and check the snippet
actually appears there. Catches the most common hallucinations:

  - wrong file path
  - wrong line number (drift from code edits since training cutoff)
  - paraphrased / fabricated snippet
  - hallucinated citations to plausible but nonexistent code

Pure function. Returns a structured report. A separate calibration step
(see `calibrate.py`) reads the report and adjusts `confidence` on the
payload.

Zero external dependencies. Free per run. Runs in milliseconds for a
9-repo workspace.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable


CONTEXT_BEFORE = 2
CONTEXT_AFTER = 4
TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[@#${}().,/:=<>!+\-*&|^~%]+|\d+")
WS_RE = re.compile(r"\s+")
MIN_TOKEN_LEN = 2
PARTIAL_TOKEN_RATIO = 0.5


@dataclass
class EvidenceCheck:
    path: str
    line: int
    status: str
    reason: str = ""


@dataclass
class FactCheck:
    category: str
    index: int
    label: str
    evidence_total: int = 0
    evidence_matched: int = 0
    evidence_partial: int = 0
    evidence_missing: int = 0
    evidence_invalid_path: int = 0
    details: list[EvidenceCheck] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        """Three-way verdict per fact.

        - `confirmed`: every evidence item matches.
        - `mixed`: at least one match AND at least one miss.
        - `disconfirmed`: no matches at all (and ≥1 evidence item present).
        - `empty`: no evidence at all (the schema requires evidence, so this
          should never happen post-validation, but guard for completeness).
        """
        if self.evidence_total == 0:
            return "empty"
        if self.evidence_matched == self.evidence_total:
            return "confirmed"
        misses = self.evidence_missing + self.evidence_invalid_path
        if self.evidence_matched == 0 and misses > 0:
            return "disconfirmed"
        return "mixed"


@dataclass
class VerifyReport:
    facts: list[FactCheck] = field(default_factory=list)

    @property
    def total_facts(self) -> int:
        return len(self.facts)

    @property
    def total_evidence(self) -> int:
        return sum(f.evidence_total for f in self.facts)

    @property
    def evidence_matched(self) -> int:
        return sum(f.evidence_matched for f in self.facts)

    @property
    def evidence_partial(self) -> int:
        return sum(f.evidence_partial for f in self.facts)

    @property
    def evidence_missing(self) -> int:
        return sum(f.evidence_missing for f in self.facts)

    @property
    def evidence_invalid_path(self) -> int:
        return sum(f.evidence_invalid_path for f in self.facts)

    @property
    def confirmed_facts(self) -> int:
        return sum(1 for f in self.facts if f.verdict == "confirmed")

    @property
    def disconfirmed_facts(self) -> int:
        return sum(1 for f in self.facts if f.verdict == "disconfirmed")

    @property
    def mixed_facts(self) -> int:
        return sum(1 for f in self.facts if f.verdict == "mixed")

    def to_dict(self) -> dict[str, Any]:
        return {
            "totals": {
                "facts": self.total_facts,
                "evidence": self.total_evidence,
                "matched": self.evidence_matched,
                "partial": self.evidence_partial,
                "missing": self.evidence_missing,
                "invalid_path": self.evidence_invalid_path,
                "facts_confirmed": self.confirmed_facts,
                "facts_mixed": self.mixed_facts,
                "facts_disconfirmed": self.disconfirmed_facts,
            },
            "facts": [
                {
                    "category": f.category,
                    "index": f.index,
                    "label": f.label,
                    "verdict": f.verdict,
                    "evidence_total": f.evidence_total,
                    "evidence_matched": f.evidence_matched,
                    "evidence_partial": f.evidence_partial,
                    "evidence_missing": f.evidence_missing,
                    "evidence_invalid_path": f.evidence_invalid_path,
                    "details": [
                        {"path": d.path, "line": d.line, "status": d.status, "reason": d.reason}
                        for d in f.details
                    ],
                }
                for f in self.facts
            ],
        }


def _normalise(text: str) -> str:
    return WS_RE.sub(" ", text).strip().lower()


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in TOKEN_RE.findall(text) if len(t) >= MIN_TOKEN_LEN]


def _read_lines(repo_root: Path, rel_path: str) -> list[str] | None:
    """Return the file's lines, or None if the file is missing/unreadable.

    LRU-cached via the keyed helper so a 9-repo workspace doesn't re-read
    the same file dozens of times when many evidence items point at it.
    """
    return _read_lines_cached(str(repo_root), rel_path)


@lru_cache(maxsize=2048)
def _read_lines_cached(repo_root_str: str, rel_path: str) -> tuple[str, ...] | None:
    repo_root = Path(repo_root_str)
    candidate = (repo_root / rel_path)
    try:
        resolved = candidate.resolve()
        resolved.relative_to(repo_root.resolve())
    except (ValueError, OSError):
        return None
    if not resolved.is_file():
        return None
    try:
        return tuple(resolved.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return None


def _check_one(
    repo_root: Path, ev: dict[str, Any]
) -> EvidenceCheck:
    raw_path = (ev.get("path") or "").lstrip("/")
    line = ev.get("line")
    snippet = ev.get("snippet") or ""
    if not raw_path:
        return EvidenceCheck(raw_path, line or 0, "invalid_path", "empty path")
    if not isinstance(line, int) or line <= 0:
        return EvidenceCheck(raw_path, line or 0, "invalid_path", "invalid line")

    lines = _read_lines(repo_root, raw_path)
    if lines is None:
        return EvidenceCheck(raw_path, line, "invalid_path", "file not found")
    if line > len(lines):
        return EvidenceCheck(
            raw_path, line, "missing", f"line {line} > file length {len(lines)}"
        )
    if not snippet.strip():
        # Schema requires snippet but if empty we can't verify; treat as
        # missing rather than confirmed.
        return EvidenceCheck(raw_path, line, "missing", "empty snippet")

    # Asymmetric context: LLM citations typically point at the opening
    # line of a multi-line construct (function-call open paren, annotation
    # above a method, decorator) and the snippet extends downward. A wider
    # downward window catches those without inflating false-positive risk
    # from unrelated nearby code.
    lo = max(1, line - CONTEXT_BEFORE)
    hi = min(len(lines), line + CONTEXT_AFTER)
    slice_text = "\n".join(lines[lo - 1 : hi])
    slice_norm = _normalise(slice_text)
    snippet_norm = _normalise(snippet)

    if snippet_norm and snippet_norm in slice_norm:
        return EvidenceCheck(raw_path, line, "matched")

    snippet_tokens = [t for t in _tokens(snippet) if len(t) >= 3]
    if not snippet_tokens:
        # Snippet was all punctuation/short — fall back to plain substring on
        # raw normalised text (already tried) → missing.
        return EvidenceCheck(raw_path, line, "missing", "no comparable tokens")

    slice_tokens = set(_tokens(slice_text))
    matched_tokens = sum(1 for t in snippet_tokens if t in slice_tokens)
    ratio = matched_tokens / len(snippet_tokens)
    if ratio >= PARTIAL_TOKEN_RATIO:
        return EvidenceCheck(
            raw_path,
            line,
            "partial",
            f"{matched_tokens}/{len(snippet_tokens)} tokens at line {line}",
        )
    return EvidenceCheck(
        raw_path,
        line,
        "missing",
        f"{matched_tokens}/{len(snippet_tokens)} tokens at line {line}",
    )


def _label_for(category: str, fact: dict[str, Any]) -> str:
    if category == "dependencies":
        src = fact.get("source", "?")
        tgt = fact.get("target", "?")
        kind = fact.get("kind", "?")
        return f"{src} -{kind}-> {tgt}"
    if category == "domain_attributes":
        return fact.get("attribute", "?")
    if category == "glossary":
        return fact.get("term", "?")
    return fact.get("name", "?")


def _verify_category(
    payload: dict[str, Any],
    category: str,
    repo_root: Path,
    out: list[FactCheck],
) -> None:
    items = payload.get(category) or []
    for idx, fact in enumerate(items):
        if not isinstance(fact, dict):
            continue
        evidence = fact.get("evidence") or []
        check = FactCheck(category=category, index=idx, label=_label_for(category, fact))
        for ev in evidence:
            if not isinstance(ev, dict):
                continue
            check.evidence_total += 1
            result = _check_one(repo_root, ev)
            check.details.append(result)
            if result.status == "matched":
                check.evidence_matched += 1
            elif result.status == "partial":
                check.evidence_partial += 1
            elif result.status == "invalid_path":
                check.evidence_invalid_path += 1
            else:
                check.evidence_missing += 1
        out.append(check)


def _verify_repo_summary(
    payload: dict[str, Any], repo_root: Path, out: list[FactCheck]
) -> None:
    repo = payload.get("repo") or {}
    if not isinstance(repo, dict):
        return
    summary = repo.get("summary") or {}
    if not isinstance(summary, dict):
        return
    evidence = summary.get("evidence") or []
    if not evidence:
        return
    check = FactCheck(
        category="repo.summary",
        index=0,
        label=repo.get("id") or "repo",
    )
    for ev in evidence:
        if not isinstance(ev, dict):
            continue
        check.evidence_total += 1
        result = _check_one(repo_root, ev)
        check.details.append(result)
        if result.status == "matched":
            check.evidence_matched += 1
        elif result.status == "partial":
            check.evidence_partial += 1
        elif result.status == "invalid_path":
            check.evidence_invalid_path += 1
        else:
            check.evidence_missing += 1
    out.append(check)


# Categories to verify. `domain_attributes` and `glossary` are included
# because their snippets are some of the most hallucination-prone (LLM
# inventing plausible enum values that don't appear in the codebase).
EVIDENCE_CATEGORIES = (
    "components",
    "apis",
    "resources",
    "dependencies",
    "providers",
    "domain_attributes",
    "glossary",
)


def verify_payload(payload: dict[str, Any], repo_root: Path) -> VerifyReport:
    """Cross-check every cited file:line in a catalog payload.

    Cheap, deterministic, side-effect free. Caller decides what to do with
    the report (logging, confidence adjustment, triage routing).
    """
    facts: list[FactCheck] = []
    _verify_repo_summary(payload, repo_root, facts)
    for category in EVIDENCE_CATEGORIES:
        _verify_category(payload, category, repo_root, facts)
    return VerifyReport(facts=facts)


def clear_cache() -> None:
    """Drop the file-read cache. Test-only."""
    _read_lines_cached.cache_clear()
