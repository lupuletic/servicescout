"""Codex CLI harness.

Thin wrapper around the `codex exec --ephemeral` invocation. The actual
subprocess plumbing remains in extractor.py for now (the streaming-log
handling has subtle timing semantics around `--output-last-message`);
this module is the dispatch layer the rest of the pipeline talks to.

When/if the subprocess plumbing is also moved here, this class becomes
self-contained.
"""

from __future__ import annotations

from typing import Any


class CodexHarness:
    name = "codex"

    def __init__(
        self,
        *,
        model: str | None = None,
        effort: str = "high",
        timeout_seconds: int = 1200,
        post_result_grace: int = 15,
        stream_logs: bool = False,
    ) -> None:
        self.model = model
        self.effort = effort
        self.timeout_seconds = timeout_seconds
        self.post_result_grace = post_result_grace
        self.stream_logs = stream_logs

    def extract(self, repo: dict[str, Any], prompt: str) -> tuple[dict[str, Any], dict[str, Any]]:
        # Import here to avoid a circular import at module load.
        from servicescout import extractor
        # The existing function uses extractor.PROMPT_TEMPLATE-rendered
        # prompt internally; we wrap it for protocol compliance even
        # though the prompt is currently fixed.
        return extractor.codex_extract(
            repo,
            self.model,
            self.effort,
            self.timeout_seconds,
            self.post_result_grace,
            self.stream_logs,
        )

    def correct(
        self,
        repo: dict[str, Any],
        prompt: str,
        timeout_seconds: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        from servicescout import extractor
        return extractor.codex_correct(
            repo,
            prompt,
            self.model,
            self.effort,
            timeout_seconds=timeout_seconds or max(300, self.timeout_seconds // 2),
        )
