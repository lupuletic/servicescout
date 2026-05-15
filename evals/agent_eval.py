"""Agent-tier evaluation — measures the *user-facing* value of ServiceScout:
when a developer (or PM) has no clue which repos hold the answer and can't
clone the workspace, does the catalog let an agent answer correctly?

The comparison is intentionally cold-start:

    baseline   — agent gets only the question + its training-time knowledge.
                 No filesystem access to the workspace. Models "engineer
                 parachuted into a new org without the repos cloned."

    treatment  — same agent, same prompt, plus a context blob built from
                 ServiceScout outputs (search hits + trace plan). Models
                 "agent has ServiceScout MCP wired in and called the
                 obvious tools."

Scored on expected-repo mentions, keyword presence, and an LLM-judged
rubric. Backed by codex CLI (matches the rest of the project's harness).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from storage import Backend, DEFAULT_FLOW_EDGE_TYPES  # noqa: E402

from score import recall_set, repos_mentioned, text_mentions_any  # noqa: E402


HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE / "cache"
DEFAULT_MODEL = os.environ.get("EVAL_AGENT_MODEL", "gpt-5.4-mini")
DEFAULT_JUDGE_MODEL = os.environ.get("EVAL_JUDGE_MODEL", "gpt-5.4-mini")
DEFAULT_TIMEOUT_SEC = 300


# ----------------- context assembly -----------------

def build_servicescout_context(backend: Any, question: str, *, top_k: int = 5) -> str:
    """Run the same MCP tools an agent would call, render as a context blob."""
    parts: list[str] = ["## ServiceScout context\n"]

    hits = backend.search(question, query_vector=None, limit=top_k)
    if hits:
        parts.append("### Top candidate entities")
        for h in hits:
            tagline = h.get("tagline") or h.get("description") or ""
            parts.append(f"- **{h['ref']}** ({h.get('type','')}) — {tagline.strip()[:160]}")
        parts.append("")

        start_ref = hits[0]["ref"]
        plan = backend.trace(
            start_ref=start_ref,
            end_match=None,
            max_hops=4,
            edge_types=DEFAULT_FLOW_EDGE_TYPES,
            include_async=True,
            fanout_per_node=4,
        )
        if plan["hops"]:
            parts.append(f"### Trace from {start_ref}")
            for hop in plan["hops"][:20]:
                async_tag = " (async)" if hop.get("async") else ""
                parts.append(
                    f"- step {hop['step']}: {hop['from']} --[{hop['edge_type']}]--> {hop['to']}"
                    f"{async_tag}  (confidence={hop.get('confidence')})"
                )
            parts.append("")
    return "\n".join(parts) if len(parts) > 1 else ""


# ----------------- codex invocation -----------------

def call_codex(prompt: str, model: str, *, timeout_sec: int = DEFAULT_TIMEOUT_SEC) -> dict[str, Any]:
    """Shell out to `codex exec`. Sandboxed in a fresh tempdir so the agent
    has zero filesystem access to anything that could leak answers (sock-shop
    repos, ServiceScout source, user dotfiles). Treatment context comes via
    the prompt, never via filesystem.
    """
    with tempfile.TemporaryDirectory(prefix="ss-eval-") as scratch:
        cmd = [
            "codex", "exec",
            "--model", model,
            "--sandbox", "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-rules",
            "--ignore-user-config",
            "--cd", scratch,
            "--color", "never",
            "-",  # read prompt from stdin
        ]
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    if proc.returncode != 0:
        return {
            "text": "",
            "model": model,
            "returncode": proc.returncode,
            "stderr": (proc.stderr or "")[-2000:],
            "error": "codex_exec_failed",
        }
    return {
        "text": _extract_final_answer(proc.stdout),
        "model": model,
        "returncode": 0,
        "raw_stdout_tail": proc.stdout[-2000:],
    }


def _extract_final_answer(stdout: str) -> str:
    """Walk backward through codex's stdout, skipping its `[event]` progress
    lines, collect the trailing non-event block as the final agent reply.
    """
    if not stdout:
        return ""
    tail: list[str] = []
    for line in reversed(stdout.splitlines()):
        stripped = line.rstrip()
        if not stripped and not tail:
            continue
        if stripped.startswith("[") and ("] " in stripped or "tokens used:" in stripped.lower()):
            if tail:
                break
            continue
        tail.append(stripped)
    tail.reverse()
    return "\n".join(tail).strip()


def _cache_key(question_id: str, trial: str, model: str, payload: str) -> Path:
    h = hashlib.sha256((trial + "::" + model + "::" + payload).encode("utf-8")).hexdigest()[:16]
    CACHE_DIR.mkdir(exist_ok=True)
    return CACHE_DIR / f"{question_id}__{trial}__{h}.json"


def get_answer(question_id: str, trial: str, prompt: str, model: str, *, refresh: bool) -> dict[str, Any]:
    cache_path = _cache_key(question_id, trial, model, prompt)
    if not refresh and cache_path.exists():
        cached = json.loads(cache_path.read_text())
        cached["from_cache"] = True
        return cached
    result = call_codex(prompt, model)
    result["from_cache"] = False
    cache_path.write_text(json.dumps(result, indent=2))
    return result


# ----------------- scoring -----------------

def score_answer(answer_text: str, expectations: dict) -> dict[str, Any]:
    expected_repos = expectations.get("expected_repos") or []
    keywords = expectations.get("expected_keywords_any_of") or []
    mentioned = repos_mentioned(answer_text, expected_repos)
    repo_recall = recall_set(mentioned, expected_repos)
    keyword_hit = text_mentions_any(answer_text, keywords)
    return {
        "repo_recall": repo_recall,
        "mentioned_repos": mentioned,
        "expected_repos": expected_repos,
        "keyword_hit": bool(keyword_hit),
    }


def judge_answer(question: str, expectations: dict, answer_text: str, *, judge_model: str, refresh: bool) -> dict[str, Any] | None:
    """Run the LLM judge with the rubric at evals/judge_prompt.txt. Optional."""
    judge_prompt_path = HERE / "judge_prompt.txt"
    if not judge_prompt_path.exists():
        return None
    rubric = judge_prompt_path.read_text()
    item = {
        "question": question,
        "canonical_answer": expectations.get("canonical_answer", ""),
        "expected_repos": expectations.get("expected_repos") or [],
        "expected_keywords": expectations.get("expected_keywords_any_of") or [],
        "answer": answer_text,
    }
    prompt = (
        rubric
        + "\n\nITEM:\n"
        + json.dumps(item, indent=2)
        + "\n\nReturn ONLY the JSON object, no markdown fences."
    )
    cache_path = _cache_key(
        "judge_" + hashlib.sha1(item["answer"].encode()).hexdigest()[:8],
        "judge", judge_model, prompt,
    )
    if not refresh and cache_path.exists():
        return json.loads(cache_path.read_text())
    raw = call_codex(prompt, judge_model)
    text = (raw.get("text") or "").strip()
    # Tolerant JSON extraction: strip code fences if codex wrapped them.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip("\n")
    # Tolerant: pick the first {...} block if codex prefixed with prose.
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if 0 <= start < end:
            text = text[start : end + 1]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Don't cache parse failures — re-run gets a fresh shot.
        return {"raw": text[:1000], "parse_error": True}
    cache_path.write_text(json.dumps(parsed, indent=2))
    return parsed


# ----------------- top-level run -----------------

BASELINE_TEMPLATE = """You are answering a question about a microservices system you may not be
familiar with. You have NO filesystem access and no way to look up code.
Answer ONLY from your general knowledge. If you don't know, say so plainly —
do NOT invent service names, file paths, or APIs.

Be concise (2-5 sentences).

Question: {question}
"""

TREATMENT_TEMPLATE = """You are answering a question about the Weaveworks sock-shop microservices
demo. You have NO direct filesystem access, but you have been given a
context blob from ServiceScout — an internal knowledge graph of the
services, APIs, datastores, and message queues in the system.

Use the ServiceScout context as ground truth. Cite the entity refs (e.g.
Component:orders) when relevant. Do not invent services not present in the
context.

{context}

Question: {question}

Answer concisely (2-5 sentences).
"""


def run_agent_eval(
    backend: Backend,
    questions: list[dict],
    *,
    model: str = DEFAULT_MODEL,
    judge_model: str | None = DEFAULT_JUDGE_MODEL,
    refresh_cache: bool = False,
    verbose: bool = True,
) -> list[dict]:
    results: list[dict] = []
    for q in questions:
        agent_exp = q.get("agent") or {}
        if not agent_exp:
            continue
        if verbose:
            print(f"  [{q['id']}] querying...", flush=True)
        context = build_servicescout_context(backend, q["question"])

        baseline_prompt = BASELINE_TEMPLATE.format(question=q["question"])
        treatment_prompt = TREATMENT_TEMPLATE.format(context=context, question=q["question"])

        baseline = get_answer(q["id"], "baseline", baseline_prompt, model, refresh=refresh_cache)
        treatment = get_answer(q["id"], "treatment", treatment_prompt, model, refresh=refresh_cache)

        baseline_score = score_answer(baseline["text"], agent_exp)
        treatment_score = score_answer(treatment["text"], agent_exp)

        baseline_judge = treatment_judge = None
        if judge_model:
            baseline_judge = judge_answer(q["question"], agent_exp, baseline["text"], judge_model=judge_model, refresh=refresh_cache)
            treatment_judge = judge_answer(q["question"], agent_exp, treatment["text"], judge_model=judge_model, refresh=refresh_cache)

        results.append({
            "question_id": q["id"],
            "category": q.get("category", "uncategorised"),
            "question": q["question"],
            "baseline": {
                "answer": baseline["text"],
                "from_cache": baseline.get("from_cache", False),
                "returncode": baseline.get("returncode"),
                "score": baseline_score,
                "judge": baseline_judge,
            },
            "treatment": {
                "answer": treatment["text"],
                "from_cache": treatment.get("from_cache", False),
                "returncode": treatment.get("returncode"),
                "score": treatment_score,
                "judge": treatment_judge,
            },
            "delta": {
                "repo_recall": treatment_score["repo_recall"] - baseline_score["repo_recall"],
                "keyword_hit": int(treatment_score["keyword_hit"]) - int(baseline_score["keyword_hit"]),
            },
        })
    return results
