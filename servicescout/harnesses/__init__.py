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

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
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


# API-key env vars that let a provider run non-interactively, with no
# interactive CLI login. If one is set we assume the harness can
# authenticate and skip the CLI status probe — codex picks up
# CODEX_API_KEY / OPENAI_API_KEY; the claude CLI and Agent SDK use
# ANTHROPIC_API_KEY. (codex can silently switch to API-key billing when
# OPENAI_API_KEY is present, so treat it as a valid auth source.)
_PROVIDER_API_KEYS: dict[str, tuple[str, ...]] = {
    "codex": ("CODEX_API_KEY", "OPENAI_API_KEY"),
    "claude": ("ANTHROPIC_API_KEY",),
}

# The CLI binary and the command that reports auth state, per provider.
# These are the same checks the README documents for headless VMs.
_AUTH_STATUS_CMD: dict[str, tuple[str, list[str]]] = {
    "codex": ("codex", ["codex", "login", "status"]),
    "claude": ("claude", ["claude", "auth", "status"]),
}


def provider_auth_status(
    provider: str, env: Mapping[str, str] | None = None
) -> tuple[bool, str]:
    """Best-effort check of whether `provider`'s extractor can authenticate.

    Returns ``(authenticated, human_detail)``. An API key in the environment
    counts as authenticated without spawning the CLI. Otherwise we run the
    provider's own status command (the same one the README documents) and
    parse its output. If the probe itself cannot run, we fail open — return
    ``True`` with a note rather than block a possibly-working setup.
    """
    env = os.environ if env is None else env
    for key in _PROVIDER_API_KEYS.get(provider, ()):
        if env.get(key):
            return True, f"{provider}: using {key} from environment"

    spec = _AUTH_STATUS_CMD.get(provider)
    if spec is None:
        return True, f"{provider}: no auth probe available"
    cli, cmd = spec
    if shutil.which(cli) is None:
        return False, f"{cli} CLI was not found on PATH"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as exc:  # never block a crawl on a flaky probe
        return True, f"{provider}: could not verify auth ({exc}); continuing"

    if provider == "claude":
        # `claude auth status` prints JSON, e.g. {"loggedIn": false, ...}.
        # If it ever prints something we can't parse, fail open rather than
        # guess from substrings — same policy as a flaky probe above.
        try:
            logged_in = bool(json.loads(proc.stdout).get("loggedIn"))
        except (json.JSONDecodeError, AttributeError):
            return True, "claude: could not parse auth status; continuing"
        return (True, "claude: logged in") if logged_in else (
            False,
            "claude CLI is not logged in",
        )
    # codex: `codex login status` prints "Not logged in" when unauthenticated.
    combined = f"{proc.stdout}\n{proc.stderr}"
    if "not logged in" in combined.lower():
        return False, "codex CLI is not logged in"
    return True, "codex: logged in"


def ensure_provider_authenticated(
    provider: str, env: Mapping[str, str] | None = None
) -> None:
    """Fail fast (``SystemExit``) if the extractor provider cannot authenticate.

    Call once before a crawl/extraction run so a missing CLI login or API key
    surfaces a clear, actionable message up front, instead of an opaque
    "did not write a usable JSON result" after repos have been cloned.
    """
    ok, detail = provider_auth_status(provider, env)
    if ok:
        return
    keys = " / ".join(_PROVIDER_API_KEYS.get(provider, ())) or "the provider API key"
    status_cmd = " ".join(_AUTH_STATUS_CMD.get(provider, (provider, [provider]))[1])
    raise SystemExit(
        f"Extractor provider {provider!r} is not authenticated: {detail}.\n"
        f"  Fix one of:\n"
        f"    • Log the CLI in on the host so Compose can mount it: "
        f"`{provider} login` (the crawler/scheduler mount ~/.{provider} read-only), or\n"
        f"    • Set {keys} in your .env for non-interactive / headless use.\n"
        f"  Verify with: `{status_cmd}`."
    )
