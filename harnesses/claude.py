"""Claude Code CLI harness.

Wraps `claude -p` invocations with the appropriate JSON schema
constraints, tool allowlist, and read-only sandbox.

Like the Codex harness, the subprocess plumbing lives in extractor.py
for now; this module is the dispatch layer.
"""

from __future__ import annotations

from typing import Any


class ClaudeHarness:
    name = "claude"

    def __init__(
        self,
        *,
        model: str = "sonnet",
        effort: str = "high",
        max_budget_usd: str | None = None,
    ) -> None:
        self.model = model
        self.effort = effort
        self.max_budget_usd = max_budget_usd

    def extract(self, repo: dict[str, Any], prompt: str) -> tuple[dict[str, Any], dict[str, Any]]:
        import extractor
        return extractor.claude_extract(repo, self.model, self.effort, self.max_budget_usd)

    def correct(
        self,
        repo: dict[str, Any],
        prompt: str,
        timeout_seconds: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        import extractor
        # Claude Code currently doesn't expose a per-call timeout flag —
        # the harness contract accepts the arg for protocol parity.
        return extractor.claude_correct(repo, prompt, self.model, self.effort, self.max_budget_usd)
