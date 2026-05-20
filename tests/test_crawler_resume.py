"""Tests for crawler.py --resume support (Issue #5).

Covers:
  - load_state: missing / corrupt → None
  - state_matches_inputs: compatibility validation, with concrete reasons
    for incompatibilities (workspace change, model change, repo allowlist
    change)
  - state_completed_repos: extracts the set of repos whose status was OK
    in prior batches, ignoring failures
"""

import json
import unittest
from pathlib import Path
import tempfile

from servicescout import crawler


class LoadStateTests(unittest.TestCase):
    def test_missing_file_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(crawler.load_state(Path(tmp) / "nope.json"))

    def test_corrupt_file_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "state.json"
            p.write_text("{ not json", encoding="utf-8")
            self.assertIsNone(crawler.load_state(p))

    def test_valid_file_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "state.json"
            p.write_text(json.dumps({"started_at": "x", "batches": []}), encoding="utf-8")
            state = crawler.load_state(p)
            self.assertEqual(state["started_at"], "x")


class StateMatchesInputsTests(unittest.TestCase):
    BASE_INPUTS = {
        "root": "/repos",
        "workspace_path": "/repos/workspace.json",
        "provider": "codex",
        "model": "gpt-5.4-mini",
        "repos_filter": None,
    }

    def _state(self, **overrides):
        inputs = {**self.BASE_INPUTS, **overrides}
        return {"started_at": "x", "inputs": inputs, "batches": []}

    def test_identical_inputs_match(self) -> None:
        ok, reason = crawler.state_matches_inputs(self._state(), self.BASE_INPUTS)
        self.assertTrue(ok, reason)

    def test_workspace_change_rejected(self) -> None:
        state = self._state(workspace_path="/repos/other.json")
        ok, reason = crawler.state_matches_inputs(state, self.BASE_INPUTS)
        self.assertFalse(ok)
        self.assertIn("workspace_path", reason)

    def test_model_change_rejected(self) -> None:
        state = self._state(model="gpt-5.4")
        ok, reason = crawler.state_matches_inputs(state, self.BASE_INPUTS)
        self.assertFalse(ok)
        self.assertIn("model", reason)

    def test_repos_filter_order_does_not_matter(self) -> None:
        state = self._state(repos_filter=["b", "a", "c"])
        current = {**self.BASE_INPUTS, "repos_filter": ["a", "c", "b"]}
        ok, reason = crawler.state_matches_inputs(state, current)
        self.assertTrue(ok, reason)

    def test_repos_filter_change_rejected(self) -> None:
        state = self._state(repos_filter=["a", "b"])
        current = {**self.BASE_INPUTS, "repos_filter": ["a", "b", "c"]}
        ok, reason = crawler.state_matches_inputs(state, current)
        self.assertFalse(ok)
        self.assertIn("repos_filter", reason)

    def test_missing_inputs_section_rejected(self) -> None:
        ok, reason = crawler.state_matches_inputs({"started_at": "x"}, self.BASE_INPUTS)
        self.assertFalse(ok)
        self.assertIn("inputs", reason)

    def test_non_dict_state_rejected(self) -> None:
        ok, reason = crawler.state_matches_inputs([], self.BASE_INPUTS)  # type: ignore[arg-type]
        self.assertFalse(ok)


class StateCompletedReposTests(unittest.TestCase):
    def test_extracts_only_ok_results(self) -> None:
        state = {
            "batches": [
                {
                    "results": [
                        {"name": "alpha", "status": "ok"},
                        {"name": "beta", "status": "ok"},
                        {"name": "gamma", "status": "quarantined"},
                        {"name": "delta", "status": "failed"},
                    ]
                },
                {
                    "results": [
                        {"name": "epsilon", "status": "ok"},
                    ]
                },
            ]
        }
        self.assertEqual(
            crawler.state_completed_repos(state),
            {"alpha", "beta", "epsilon"},
        )

    def test_handles_no_batches(self) -> None:
        self.assertEqual(crawler.state_completed_repos({"batches": []}), set())

    def test_handles_missing_status(self) -> None:
        state = {
            "batches": [{"results": [{"name": "a"}, {"name": "b", "status": "ok"}]}]
        }
        self.assertEqual(crawler.state_completed_repos(state), {"b"})

    def test_accepts_alternate_success_labels(self) -> None:
        state = {"batches": [{"results": [
            {"name": "a", "status": "success"},
            {"name": "b", "status": "completed"},
            {"name": "c", "status": "OK"},  # case-insensitive
        ]}]}
        self.assertEqual(crawler.state_completed_repos(state), {"a", "b", "c"})


if __name__ == "__main__":
    unittest.main()
