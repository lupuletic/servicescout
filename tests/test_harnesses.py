"""Tests for the harness protocol + factory.

These are interface tests — they verify the harness layer's contract
without actually spawning Codex / Claude (which would be expensive and
need credentials). The subprocess behaviour itself is exercised by the
larger end-to-end runs documented in ARCHITECTURE.md.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from servicescout.harnesses import (
    ensure_provider_authenticated,
    get_harness,
    provider_auth_status,
)
from servicescout.harnesses.codex import CodexHarness
from servicescout.harnesses.claude import ClaudeHarness


class HarnessFactoryTests(unittest.TestCase):
    def test_codex_factory_returns_codex_harness(self) -> None:
        h = get_harness("codex", model="gpt-5.4-mini", effort="medium")
        self.assertIsInstance(h, CodexHarness)
        self.assertEqual(h.name, "codex")
        self.assertEqual(h.model, "gpt-5.4-mini")
        self.assertEqual(h.effort, "medium")

    def test_claude_factory_returns_claude_harness(self) -> None:
        h = get_harness("claude", model="sonnet", effort="high")
        self.assertIsInstance(h, ClaudeHarness)
        self.assertEqual(h.name, "claude")
        self.assertEqual(h.model, "sonnet")

    def test_unknown_harness_raises(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            get_harness("invented")
        self.assertIn("Unknown harness", str(cm.exception))

    def test_codex_default_config(self) -> None:
        h = CodexHarness()
        self.assertIsNone(h.model)
        self.assertEqual(h.effort, "high")
        self.assertEqual(h.timeout_seconds, 1200)
        self.assertFalse(h.stream_logs)

    def test_codex_custom_timeout(self) -> None:
        h = CodexHarness(timeout_seconds=300)
        self.assertEqual(h.timeout_seconds, 300)

    def test_harness_implements_protocol(self) -> None:
        # Both harnesses must expose the same public surface.
        for harness in (CodexHarness(), ClaudeHarness()):
            self.assertTrue(hasattr(harness, "name"))
            self.assertTrue(callable(getattr(harness, "extract", None)))
            self.assertTrue(callable(getattr(harness, "correct", None)))


class ProviderAuthStatusTests(unittest.TestCase):
    """Auth preflight: API keys short-circuit; CLI probe is parsed; failures
    are explicit; a broken probe fails open."""

    def test_api_key_counts_as_authenticated_without_cli(self) -> None:
        with mock.patch("servicescout.harnesses.shutil.which") as which:
            ok, detail = provider_auth_status("codex", env={"OPENAI_API_KEY": "sk-x"})
            self.assertTrue(ok)
            self.assertIn("OPENAI_API_KEY", detail)
            which.assert_not_called()  # never probe the CLI when a key is set

        ok, _ = provider_auth_status("claude", env={"ANTHROPIC_API_KEY": "sk-x"})
        self.assertTrue(ok)

    def test_codex_not_logged_in_is_unauthenticated(self) -> None:
        fake = SimpleNamespace(stdout="Not logged in\n", stderr="")
        with mock.patch("servicescout.harnesses.shutil.which", return_value="/usr/bin/codex"), \
             mock.patch("servicescout.harnesses.subprocess.run", return_value=fake):
            ok, detail = provider_auth_status("codex", env={})
        self.assertFalse(ok)
        self.assertIn("not logged in", detail.lower())

    def test_claude_status_json_is_parsed(self) -> None:
        with mock.patch("servicescout.harnesses.shutil.which", return_value="/usr/bin/claude"):
            with mock.patch(
                "servicescout.harnesses.subprocess.run",
                return_value=SimpleNamespace(stdout='{"loggedIn": false}', stderr=""),
            ):
                ok, _ = provider_auth_status("claude", env={})
                self.assertFalse(ok)
            with mock.patch(
                "servicescout.harnesses.subprocess.run",
                return_value=SimpleNamespace(stdout='{"loggedIn": true}', stderr=""),
            ):
                ok, _ = provider_auth_status("claude", env={})
                self.assertTrue(ok)

    def test_claude_unparseable_status_fails_open(self) -> None:
        with mock.patch("servicescout.harnesses.shutil.which", return_value="/usr/bin/claude"), \
             mock.patch(
                 "servicescout.harnesses.subprocess.run",
                 return_value=SimpleNamespace(stdout="not json at all", stderr=""),
             ):
            ok, detail = provider_auth_status("claude", env={})
        self.assertTrue(ok)  # don't block when we can't parse the probe
        self.assertIn("continuing", detail)

    def test_missing_cli_is_unauthenticated(self) -> None:
        with mock.patch("servicescout.harnesses.shutil.which", return_value=None):
            ok, detail = provider_auth_status("codex", env={})
        self.assertFalse(ok)
        self.assertIn("not found", detail.lower())

    def test_flaky_probe_fails_open(self) -> None:
        with mock.patch("servicescout.harnesses.shutil.which", return_value="/usr/bin/codex"), \
             mock.patch("servicescout.harnesses.subprocess.run", side_effect=OSError("boom")):
            ok, detail = provider_auth_status("codex", env={})
        self.assertTrue(ok)  # do not block on a broken probe
        self.assertIn("continuing", detail)

    def test_ensure_raises_with_actionable_message(self) -> None:
        with mock.patch("servicescout.harnesses.shutil.which", return_value=None):
            with self.assertRaises(SystemExit) as cm:
                ensure_provider_authenticated("claude", env={})
        msg = str(cm.exception)
        self.assertIn("not authenticated", msg)
        self.assertIn("ANTHROPIC_API_KEY", msg)  # tells the user the env var
        self.assertIn("claude auth status", msg)  # tells the user how to verify

    def test_ensure_passes_when_api_key_present(self) -> None:
        # Should not raise.
        ensure_provider_authenticated("codex", env={"CODEX_API_KEY": "sk-x"})


if __name__ == "__main__":
    unittest.main()
