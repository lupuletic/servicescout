"""Phase B — universal code-shape verifier (replaces the per-language
tree-sitter AST rules).

Design intent: this module is **language-agnostic**. It does not parse
syntax, look up grammars, or pattern-match per language. It answers
exactly one question per evidence item:

  "Is the cited line a non-trivial line of actual code (or text), versus
   a blank line, a pure comment, or visibly nonsense?"

If yes → the LLM's citation lands on real content. If no → the LLM
fabricated or mis-quoted the line. The LLM's own *semantic* claim
(this is a `producesMessage` edge, this is a route registration, etc.)
is trusted by the verifier; we don't re-derive it from the AST. The
correction loop and the LLM prompt itself are where intent
classification happens — those scale to any language and any framework.

This is a deliberate philosophical reset:

  - Old design: per-language tree-sitter queries (Java, Python, Go, JS,
    TS) tried to verify the *kind* of operation on the cited line.
    Result: high false-positive and false-negative rates, narrow
    language coverage (~5 of dozens), constant rule maintenance.

  - New design: trust the LLM's semantic claim; verify only that the
    citation is real. Coverage becomes universal (every text file).
    Bugs that would have required per-language rules to catch are now
    caught at the source — by improving the LLM prompt and the
    correction loop's structured feedback.

Phase A still catches the high-frequency hallucination class
(snippet not at line). This module catches the lower-frequency one
(line exists but is whitespace/comment).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any


# Heuristic comment-prefix set across the broad set of languages humans
# write services in. Not exhaustive — by design, this is a *fast text
# check*, not a parser. False negatives (a real comment we miss) just
# fall through to "code", which is the safer direction.
_COMMENT_PREFIXES = (
    "//", "#", ";", "--", "/*", "*", "<!--", "%", "'", "\"\"\"", "'''",
    "rem ", "rem\t",
)


@dataclass
class CodeShapeCheck:
    path: str
    line: int
    status: str  # "code" | "blank" | "comment" | "unparseable"
    reason: str = ""


@dataclass
class FactShapeCheck:
    index: int
    label: str
    kind: str
    evidence_total: int = 0
    code: int = 0
    blank: int = 0
    comment: int = 0
    unparseable: int = 0
    details: list[CodeShapeCheck] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        """Three-way verdict matching the Phase A shape so the calibrator
        can combine them with one rule set.

          - `confirmed`: every evidence item lands on real code.
          - `mixed`: some code, some blank/comment.
          - `disconfirmed`: every evidence item is blank, comment, or
            unreachable. The citation does not point at real content.
          - `empty`: no evidence (should not happen post-validation).
        """
        if self.evidence_total == 0:
            return "empty"
        if self.code == self.evidence_total:
            return "confirmed"
        if self.code == 0:
            return "disconfirmed"
        return "mixed"


@dataclass
class CodeShapeReport:
    facts: list[FactShapeCheck] = field(default_factory=list)

    @property
    def total_facts(self) -> int:
        return len(self.facts)

    @property
    def confirmed_facts(self) -> int:
        return sum(1 for f in self.facts if f.verdict == "confirmed")

    @property
    def disconfirmed_facts(self) -> int:
        return sum(1 for f in self.facts if f.verdict == "disconfirmed")

    @property
    def mixed_facts(self) -> int:
        return sum(1 for f in self.facts if f.verdict == "mixed")

    @property
    def unsupported_facts(self) -> int:
        # Kept for API compatibility with the previous AstReport. With
        # the universal verifier there is no notion of "unsupported"
        # language — every text file is supported.
        return 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "totals": {
                "facts": self.total_facts,
                "facts_confirmed": self.confirmed_facts,
                "facts_mixed": self.mixed_facts,
                "facts_disconfirmed": self.disconfirmed_facts,
                "facts_unsupported": 0,
            },
            "facts": [
                {
                    "index": f.index,
                    "label": f.label,
                    "kind": f.kind,
                    "verdict": f.verdict,
                    "evidence_total": f.evidence_total,
                    "code": f.code,
                    "blank": f.blank,
                    "comment": f.comment,
                    "unparseable": f.unparseable,
                    "details": [
                        {"path": d.path, "line": d.line, "status": d.status, "reason": d.reason}
                        for d in f.details
                    ],
                }
                for f in self.facts
            ],
        }


# Aliases kept for callers that imported the old names from ast_crosscheck.
# `calibrate.py` and `verify_cli.py` reference these.
AstEvidenceCheck = CodeShapeCheck
AstFactCheck = FactShapeCheck
AstReport = CodeShapeReport


# --------------------------------------------------------------------------- #
# File I/O
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=2048)
def _read_lines(repo_root_str: str, rel_path: str) -> tuple[str, ...] | None:
    repo_root = Path(repo_root_str)
    candidate = repo_root / rel_path
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


def clear_cache() -> None:
    """Drop the file-read cache. Test-only."""
    _read_lines.cache_clear()


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


def _classify_line(text: str) -> tuple[str, str]:
    """Classify a raw line of text. Returns (status, reason).

    Heuristic. The space of comment syntaxes across all languages we may
    encounter is large, but the high-precision cases (the ones a careless
    LLM would actually cite by accident — empty lines, opening/closing
    braces, language keywords, doc-comment blocks) are uniform enough
    that a small prefix set catches them.
    """
    stripped = text.strip()
    if not stripped:
        return "blank", "empty line"
    # Lines that are JUST punctuation (e.g. `}`, `);`, `:`, `>`)
    # are technically code but carry no semantic weight as a citation.
    # We still count them as "code" because they ARE code, and falsely
    # disconfirming punctuation-only lines is worse than letting them
    # through — Phase A's snippet check is the high-frequency screen.
    lowered = stripped.lower()
    for prefix in _COMMENT_PREFIXES:
        if lowered.startswith(prefix):
            return "comment", f"line begins with {prefix!r}"
    return "code", ""


def _check_one(repo_root: Path, ev: dict[str, Any]) -> CodeShapeCheck:
    raw_path = (ev.get("path") or "").lstrip("/")
    line = ev.get("line")
    if not raw_path:
        return CodeShapeCheck(raw_path, line or 0, "unparseable", "empty path")
    if not isinstance(line, int) or line <= 0:
        return CodeShapeCheck(raw_path, line or 0, "unparseable", "invalid line")
    lines = _read_lines(str(repo_root), raw_path)
    if lines is None:
        return CodeShapeCheck(raw_path, line, "unparseable", "file not found or unreadable")
    if line > len(lines):
        return CodeShapeCheck(raw_path, line, "unparseable", f"line {line} > file length {len(lines)}")
    status, reason = _classify_line(lines[line - 1])
    return CodeShapeCheck(raw_path, line, status, reason)


def _label_for_dep(dep: dict[str, Any]) -> str:
    src = dep.get("source", "?")
    tgt = dep.get("target", "?")
    kind = dep.get("kind", "?")
    return f"{src} -{kind}-> {tgt}"


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def verify_payload(payload: dict[str, Any], repo_root: Path) -> CodeShapeReport:
    """Run the universal code-shape check on every dependency edge.

    Returns a report shaped identically to the old AstReport, so the
    calibrator's combined-verdict logic works without modification.

    Only `dependencies[]` are checked (matching the prior behaviour). A
    similar pass over other categories could be added if needed, but
    Phase A already covers them with the snippet substring check.
    """
    facts: list[FactShapeCheck] = []
    for idx, dep in enumerate(payload.get("dependencies") or []):
        if not isinstance(dep, dict):
            continue
        kind = dep.get("kind") or ""
        check = FactShapeCheck(index=idx, label=_label_for_dep(dep), kind=kind)
        for ev in dep.get("evidence") or []:
            if not isinstance(ev, dict):
                continue
            check.evidence_total += 1
            result = _check_one(repo_root, ev)
            check.details.append(result)
            if result.status == "code":
                check.code += 1
            elif result.status == "blank":
                check.blank += 1
            elif result.status == "comment":
                check.comment += 1
            else:
                check.unparseable += 1
        facts.append(check)
    return CodeShapeReport(facts=facts)
