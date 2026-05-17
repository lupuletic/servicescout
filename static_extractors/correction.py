"""Correction loop — Phase A+B feedback to the LLM for re-investigation.

After a first extraction pass, Phase A (snippet substring) and Phase B
(tree-sitter AST) identify facts whose cited evidence doesn't hold up.
This module turns that list into a *targeted* re-prompt: it asks the
same LLM to re-investigate each flagged fact and either correct the
citation, replace the fact, or drop it. The result is merged back into
the payload, and the verifiers are re-run.

Stateless replay design — the correction call is a new harness
invocation with a small, focused prompt. No session resumption is
required; the prompt embeds enough context (the original facts, why each
was flagged) for the model to act. This works identically against Codex
and Claude harnesses and avoids the version-coupling that native
session-resume APIs introduce.

Cost shape: per correction round, the LLM only needs to verify N
flagged facts (typically <20 per repo) — it does not re-explore the
whole codebase. Expect <10% of first-pass token cost per round; cap
rounds (default: 1) to avoid runaway loops.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


CORRECTION_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Catalog correction response",
    "type": "object",
    "additionalProperties": False,
    "required": ["corrections"],
    "properties": {
        "corrections": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["category", "index", "action"],
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": [
                            "dependencies",
                            "resources",
                            "components",
                            "apis",
                            "providers",
                            "domain_attributes",
                            "glossary",
                        ],
                    },
                    "index": {"type": "integer", "minimum": 0},
                    "action": {
                        "type": "string",
                        "enum": ["fix", "drop", "keep"],
                    },
                    "reason": {"type": "string"},
                    "fact": {
                        "type": "object",
                        # Free-form: validated against the main catalog
                        # schema post-merge.
                        "additionalProperties": True,
                    },
                },
            },
        }
    },
}


PROMPT_TEMPLATE = """You previously produced a catalog extraction for repository {repo_id}.
A deterministic cross-checker has flagged {n_problems} facts whose evidence
does not verify against the repository contents. Your task: re-investigate
each flagged fact and respond with corrections.

REPO PATH (read-only): {repo_path}

FLAGGED FACTS:

{problems_block}

WHAT TO RETURN

Return JSON conforming to the supplied output schema. For each flagged
fact above, emit exactly one entry under `corrections[]`:

  - action="fix"  — provide a corrected `fact` object. Same shape as in
                    the original payload, with evidence[] items that
                    DO exist verbatim at the cited file:line in this
                    repo. Snippets must be substrings of the actual
                    line content (whitespace-normalised matches are OK).

  - action="drop" — emit `reason` explaining why no supporting evidence
                    exists in this repo (e.g. you searched and the
                    referenced code no longer exists / was never there
                    / was confused with a different repo).

  - action="keep" — emit `reason` justifying why the original is correct
                    despite the cross-checker flag. Use sparingly: the
                    cross-checker is deterministic, so if it says the
                    snippet is not at that line, it is not at that line.
                    "keep" makes sense mostly when an unsupported
                    language/kind pair caused Phase B to be inconclusive.

CONSTRAINTS

  - Do not introduce new facts not present in the flagged list.
  - For `fix`: search the actual files using rg/grep/Read. Verify every
    `evidence[].path` is a real file and `evidence[].line` contains the
    `snippet` you cite.
  - For `dependencies` of kind producesMessage/consumesMessage: the
    cited line should contain an actual publish/consume call or a
    listener annotation (`@KafkaListener`, `@RabbitListener`, channel.publish,
    channel.consume, basic_publish, basic_consume, etc.), not a route
    registration or unrelated method.
  - Keep `category` and `index` exactly as listed above so the merge can
    find the right slot.

Phase A flagged these because the snippet text was missing at the cited
line. Phase B flagged additional ones because the cited line did not
contain the type of operation the edge claims. Both signals are
authoritative — trust them and re-investigate."""


def build_correction_prompt(
    repo_id: str, repo_path: Path | str, problems: list[dict[str, Any]]
) -> str:
    """Render the correction prompt. `problems` is the output of
    `static_extractors.calibrate.collect_problems(...)`."""
    blocks: list[str] = []
    for i, p in enumerate(problems, 1):
        reasons = "\n      ".join("- " + r for r in p.get("reasons", []))
        if not reasons:
            reasons = "- (no specific reason recorded)"
        current_json = json.dumps(p["current_fact"], indent=2, sort_keys=True)
        blocks.append(
            f"[{i}] {p['category']}[{p['index']}] = {p['label']!s}\n"
            f"    combined verdict: {p['combined_verdict']}\n"
            f"    why flagged:\n      {reasons}\n"
            f"    current value:\n{_indent(current_json, 6)}"
        )
    return PROMPT_TEMPLATE.format(
        repo_id=repo_id,
        repo_path=str(repo_path),
        n_problems=len(problems),
        problems_block="\n\n".join(blocks),
    )


def _indent(text: str, n: int) -> str:
    pad = " " * n
    return "\n".join(pad + line for line in text.splitlines())


def parse_correction_response(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate the harness's structured output. Returns the list of
    corrections; raises ValueError on shape mismatch."""
    if not isinstance(raw, dict):
        raise ValueError("correction response must be a JSON object")
    corrections = raw.get("corrections")
    if not isinstance(corrections, list):
        raise ValueError("correction response missing `corrections` array")
    out: list[dict[str, Any]] = []
    for c in corrections:
        if not isinstance(c, dict):
            continue
        if c.get("action") not in {"fix", "drop", "keep"}:
            continue
        if c.get("category") not in CORRECTION_SCHEMA["properties"]["corrections"]["items"]["properties"]["category"]["enum"]:
            continue
        if not isinstance(c.get("index"), int):
            continue
        out.append(c)
    return out


def apply_corrections(
    payload: dict[str, Any], corrections: list[dict[str, Any]]
) -> dict[str, Any]:
    """Apply correction directives to the payload in place. Returns a
    summary record describing what changed.

    Drops are applied after fixes so indices in `corrections` always
    refer to the *pre-correction* payload. (Otherwise dropping index 3
    would shift index 7's referent.)
    """
    applied = {"fix": 0, "drop": 0, "keep": 0, "skipped": 0, "details": []}

    # Apply fixes / keeps first. Drops are handled in the second loop
    # below in descending-index order to keep referenced indices stable.
    for c in corrections:
        action = c["action"]
        if action == "drop":
            continue
        cat = c["category"]
        idx = c["index"]
        items = payload.get(cat) or []
        if not (0 <= idx < len(items)):
            applied["skipped"] += 1
            applied["details"].append({"action": action, "category": cat, "index": idx, "result": "out-of-range"})
            continue
        if action == "fix":
            new_fact = c.get("fact")
            if not isinstance(new_fact, dict):
                applied["skipped"] += 1
                applied["details"].append({"action": "fix", "category": cat, "index": idx, "result": "missing-fact"})
                continue
            items[idx] = new_fact
            applied["fix"] += 1
            applied["details"].append({"action": "fix", "category": cat, "index": idx, "result": "applied"})
        elif action == "keep":
            applied["keep"] += 1
            applied["details"].append({"action": "keep", "category": cat, "index": idx, "reason": c.get("reason", "")})

    # Apply drops, processing high-to-low so we don't shift earlier indices.
    drops = sorted(
        ((c["category"], c["index"], c.get("reason", ""))
         for c in corrections if c["action"] == "drop"),
        key=lambda t: (-t[1],),
    )
    for cat, idx, reason in drops:
        items = payload.get(cat) or []
        if not (0 <= idx < len(items)):
            applied["skipped"] += 1
            continue
        del items[idx]
        applied["drop"] += 1
        applied["details"].append({"action": "drop", "category": cat, "index": idx, "reason": reason})

    return applied
