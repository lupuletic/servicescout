"""Extraction harnesses.

A `Harness` is anything that can take a repository + a prompt + a JSON
schema and return a structured JSON payload. Today: Codex CLI and Claude
Code CLI. Tomorrow: direct OpenAI / Anthropic / other-vendor APIs, or
local LLM endpoints. The interface is deliberately thin so new harnesses
can be added without changing the rest of the pipeline.

Each `extract` / `correct` call returns `(payload, run_metadata)` where
`run_metadata` records provider, model, effort, duration, return code,
and any cost telemetry the harness can produce. The extractor (and the
correction loop) consume this uniformly.
"""

from __future__ import annotations

from typing import Any, Protocol


class Harness(Protocol):
    """Protocol for any extraction harness.

    Implementations may capture configuration (model, effort, timeout,
    budget, stream flag) at construction time and apply it across all
    calls, so the calling code passes only the data needed per call.
    """

    name: str

    def extract(self, repo: dict[str, Any], prompt: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Run a full extraction with the supplied prompt. Returns
        (raw_payload, run_metadata). Raises SystemExit / RuntimeError
        on unrecoverable failure.
        """
        ...

    def correct(
        self,
        repo: dict[str, Any],
        prompt: str,
        timeout_seconds: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Run a targeted correction call with the supplied prompt.
        Returns (raw_payload, run_metadata) where raw_payload should be
        consumable by `static_extractors.correction.parse_correction_response`.

        Harnesses may use a shorter timeout for corrections than the
        primary extraction; if `timeout_seconds` is None the harness
        picks a sensible default.
        """
        ...


def get_harness(name: str, **config: Any) -> Harness:
    """Factory. Returns a Harness instance for the named provider.

    Known names: "codex" (default), "claude".
    Future: "openai-api", "anthropic-api", "vllm", "ollama".
    """
    if name == "codex":
        from servicescout.harnesses.codex import CodexHarness
        return CodexHarness(**config)
    if name == "claude":
        from servicescout.harnesses.claude import ClaudeHarness
        return ClaudeHarness(**config)
    raise SystemExit(
        f"Unknown harness: {name!r}. "
        f"Known harnesses: codex, claude. "
        f"Add a new one by implementing the Harness protocol in harnesses/."
    )
