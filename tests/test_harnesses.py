"""Tests for the harness protocol + factory.

These are interface tests — they verify the harness layer's contract
without actually spawning Codex / Claude (which would be expensive and
need credentials). The subprocess behaviour itself is exercised by the
larger end-to-end runs documented in ARCHITECTURE.md.
"""

import unittest

from harnesses import get_harness
from harnesses.codex import CodexHarness
from harnesses.claude import ClaudeHarness


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


if __name__ == "__main__":
    unittest.main()
