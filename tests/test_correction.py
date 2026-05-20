import unittest

from servicescout.static_extractors import correction


def _payload_with_deps(deps: list[dict]) -> dict:
    return {
        "repo": {"id": "fixture/repo_a"},
        "components": [],
        "apis": [],
        "resources": [],
        "providers": [],
        "domain_attributes": [],
        "glossary": [],
        "dependencies": deps,
    }


def _problem(category: str, index: int, label: str, verdict: str, reasons: list[str], current: dict) -> dict:
    return {
        "category": category,
        "index": index,
        "label": label,
        "combined_verdict": verdict,
        "explanation": "test",
        "phase_a_verdict": "disconfirmed",
        "phase_b_verdict": "disconfirmed",
        "reasons": reasons,
        "current_fact": current,
    }


class PromptBuilderTests(unittest.TestCase):
    def test_prompt_includes_each_problem(self) -> None:
        problems = [
            _problem(
                "dependencies", 3, "orders -producesMessage-> shipping",
                "disconfirmed",
                ["snippet check at src/Foo.java:50 → missing (1/8 tokens)"],
                {"source": "orders", "target": "shipping", "kind": "producesMessage"},
            ),
            _problem(
                "dependencies", 7, "user -consumesApi-> auth",
                "mixed",
                ["AST check at src/User.java:120 → disconfirmed (pattern not found)"],
                {"source": "user", "target": "auth", "kind": "consumesApi"},
            ),
        ]
        prompt = correction.build_correction_prompt("acme/orders", "/repos/orders", problems)
        self.assertIn("acme/orders", prompt)
        self.assertIn("/repos/orders", prompt)
        self.assertIn("dependencies[3]", prompt)
        self.assertIn("dependencies[7]", prompt)
        self.assertIn("producesMessage", prompt)
        self.assertIn("disconfirmed", prompt)

    def test_prompt_handles_no_reasons(self) -> None:
        problems = [
            _problem("dependencies", 0, "a -kind-> b", "mixed", [], {"x": 1})
        ]
        prompt = correction.build_correction_prompt("r", "/r", problems)
        self.assertIn("no specific reason recorded", prompt)


class ParseResponseTests(unittest.TestCase):
    def test_valid_response_parses(self) -> None:
        raw = {
            "corrections": [
                {
                    "category": "dependencies",
                    "index": 0,
                    "action": "fix",
                    "fact": {"source": "x", "target": "y", "kind": "consumesApi"},
                },
                {
                    "category": "dependencies",
                    "index": 1,
                    "action": "drop",
                    "reason": "no evidence found",
                },
            ]
        }
        out = correction.parse_correction_response(raw)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["action"], "fix")
        self.assertEqual(out[1]["action"], "drop")

    def test_invalid_action_is_dropped(self) -> None:
        raw = {"corrections": [{"category": "dependencies", "index": 0, "action": "yolo"}]}
        out = correction.parse_correction_response(raw)
        self.assertEqual(out, [])

    def test_missing_index_is_dropped(self) -> None:
        raw = {"corrections": [{"category": "dependencies", "action": "drop"}]}
        out = correction.parse_correction_response(raw)
        self.assertEqual(out, [])

    def test_non_dict_raises(self) -> None:
        with self.assertRaises(ValueError):
            correction.parse_correction_response([])  # type: ignore[arg-type]

    def test_missing_corrections_array_raises(self) -> None:
        with self.assertRaises(ValueError):
            correction.parse_correction_response({})


class ApplyCorrectionsTests(unittest.TestCase):
    def test_fix_replaces_item(self) -> None:
        original = {"source": "a", "target": "b", "kind": "consumesApi", "confidence": "high"}
        payload = _payload_with_deps([original])
        corrected = {"source": "a", "target": "b2", "kind": "consumesApi", "confidence": "medium"}
        applied = correction.apply_corrections(
            payload,
            [{"category": "dependencies", "index": 0, "action": "fix", "fact": corrected}],
        )
        self.assertEqual(payload["dependencies"][0]["target"], "b2")
        self.assertEqual(applied["fix"], 1)

    def test_drop_removes_item(self) -> None:
        payload = _payload_with_deps([
            {"source": "a", "target": "b", "kind": "consumesApi"},
            {"source": "c", "target": "d", "kind": "consumesApi"},
        ])
        correction.apply_corrections(
            payload,
            [{"category": "dependencies", "index": 0, "action": "drop", "reason": "no evidence"}],
        )
        self.assertEqual(len(payload["dependencies"]), 1)
        self.assertEqual(payload["dependencies"][0]["source"], "c")

    def test_keep_leaves_item_unchanged(self) -> None:
        payload = _payload_with_deps([
            {"source": "a", "target": "b", "kind": "consumesApi"}
        ])
        before = dict(payload["dependencies"][0])
        applied = correction.apply_corrections(
            payload,
            [{"category": "dependencies", "index": 0, "action": "keep", "reason": "correct"}],
        )
        self.assertEqual(payload["dependencies"][0], before)
        self.assertEqual(applied["keep"], 1)

    def test_drops_processed_high_to_low_to_preserve_indices(self) -> None:
        # If we drop both 0 and 2 in low-to-high order, dropping 0 first
        # shifts the original index 2 down to index 1, then we'd drop
        # the wrong item. Verify the implementation processes drops in
        # descending index order to avoid this.
        payload = _payload_with_deps([
            {"source": "a", "target": "0", "kind": "consumesApi"},
            {"source": "b", "target": "1", "kind": "consumesApi"},
            {"source": "c", "target": "2", "kind": "consumesApi"},
        ])
        correction.apply_corrections(
            payload,
            [
                {"category": "dependencies", "index": 0, "action": "drop", "reason": ""},
                {"category": "dependencies", "index": 2, "action": "drop", "reason": ""},
            ],
        )
        # Should be left with only the middle one.
        self.assertEqual(len(payload["dependencies"]), 1)
        self.assertEqual(payload["dependencies"][0]["target"], "1")

    def test_out_of_range_is_skipped(self) -> None:
        payload = _payload_with_deps([
            {"source": "a", "target": "b", "kind": "consumesApi"}
        ])
        applied = correction.apply_corrections(
            payload,
            [{"category": "dependencies", "index": 99, "action": "drop", "reason": ""}],
        )
        self.assertEqual(applied["skipped"], 1)
        self.assertEqual(len(payload["dependencies"]), 1)

    def test_fix_without_fact_is_skipped(self) -> None:
        payload = _payload_with_deps([
            {"source": "a", "target": "b", "kind": "consumesApi"}
        ])
        applied = correction.apply_corrections(
            payload,
            [{"category": "dependencies", "index": 0, "action": "fix"}],
        )
        self.assertEqual(applied["skipped"], 1)
        # Original unchanged.
        self.assertEqual(payload["dependencies"][0]["target"], "b")


if __name__ == "__main__":
    unittest.main()
