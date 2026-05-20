import json
import unittest
from pathlib import Path

from servicescout.static_extractors.snippet_verify import (
    clear_cache,
    verify_payload,
)


FIXTURES = Path(__file__).parent / "fixtures"
REPO_A = FIXTURES / "repo_a"


def _make_payload(evidence: list[dict]) -> dict:
    """Minimal catalog payload with one dependency edge carrying the
    supplied evidence list. Shape matches catalog_schema.json."""
    return {
        "repo": {"id": "fixture/repo_a"},
        "components": [],
        "apis": [],
        "resources": [],
        "providers": [],
        "domain_attributes": [],
        "glossary": [],
        "dependencies": [
            {
                "source": "orders",
                "target": "shipping-task",
                "target_kind": "resource",
                "kind": "producesMessage",
                "protocol": "amqp",
                "operation_or_usage": "publish order to shipping queue",
                "message_or_event_name": "shipping-task",
                "env_or_config_keys": ["RABBITMQ_HOST"],
                "aliases": [],
                "confidence": "medium",
                "notes": "",
                "evidence": evidence,
            }
        ],
    }


class SnippetVerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_cache()

    # --- exact match -------------------------------------------------------

    def test_exact_snippet_at_cited_line_is_confirmed(self) -> None:
        # Line 15 of orders.py: `        routing_key="shipping-task",`
        payload = _make_payload([
            {"path": "src/orders.py", "line": 15, "snippet": 'routing_key="shipping-task"'},
        ])
        report = verify_payload(payload, REPO_A)
        self.assertEqual(report.total_facts, 1)
        self.assertEqual(report.evidence_matched, 1)
        self.assertEqual(report.facts[0].verdict, "confirmed")

    def test_match_within_context_window(self) -> None:
        # Line 13 cites basic_publish, but snippet text is from line 15.
        # Within ±2 context window → still confirmed.
        payload = _make_payload([
            {"path": "src/orders.py", "line": 13, "snippet": 'routing_key="shipping-task"'},
        ])
        report = verify_payload(payload, REPO_A)
        self.assertEqual(report.facts[0].verdict, "confirmed")

    def test_whitespace_and_case_are_normalised(self) -> None:
        payload = _make_payload([
            {"path": "src/orders.py", "line": 15, "snippet": '   ROUTING_KEY = "shipping-task"   '},
        ])
        report = verify_payload(payload, REPO_A)
        # Tokens match even when case/whitespace differ → at minimum partial.
        self.assertIn(report.facts[0].verdict, {"confirmed", "mixed"})
        self.assertGreaterEqual(
            report.facts[0].evidence_matched + report.facts[0].evidence_partial, 1
        )

    # --- partial match -----------------------------------------------------

    def test_paraphrased_snippet_drops_to_partial(self) -> None:
        # Snippet uses words from the code but isn't a verbatim substring.
        payload = _make_payload([
            {"path": "src/orders.py", "line": 15, "snippet": "publishes to shipping-task via routing_key"},
        ])
        report = verify_payload(payload, REPO_A)
        # Real tokens ("shipping-task", "routing_key") appear within ±2; the
        # rest don't. Should be partial, not missing, not matched.
        self.assertEqual(report.facts[0].verdict, "mixed")
        self.assertEqual(report.facts[0].evidence_partial, 1)

    # --- misses ------------------------------------------------------------

    def test_wrong_line_is_disconfirmed(self) -> None:
        # Snippet would match line 15, but cite line 2 (just imports).
        payload = _make_payload([
            {"path": "src/orders.py", "line": 2, "snippet": 'routing_key="shipping-task"'},
        ])
        report = verify_payload(payload, REPO_A)
        self.assertEqual(report.facts[0].verdict, "disconfirmed")
        self.assertEqual(report.facts[0].evidence_missing, 1)

    def test_wrong_file_path_is_invalid_path(self) -> None:
        payload = _make_payload([
            {"path": "src/nonexistent.py", "line": 1, "snippet": "anything"},
        ])
        report = verify_payload(payload, REPO_A)
        self.assertEqual(report.facts[0].verdict, "disconfirmed")
        self.assertEqual(report.facts[0].evidence_invalid_path, 1)

    def test_path_escape_is_invalid_path(self) -> None:
        # Path traversal should be quarantined as invalid (defense in depth
        # — extractor.confine_evidence_paths normally removes these earlier,
        # but the verifier should still refuse to read outside the repo).
        payload = _make_payload([
            {"path": "../../../etc/passwd", "line": 1, "snippet": "root"},
        ])
        report = verify_payload(payload, REPO_A)
        self.assertEqual(report.facts[0].evidence_invalid_path, 1)

    def test_hallucinated_snippet_is_disconfirmed(self) -> None:
        # Snippet is plausible for an orders publisher but doesn't appear.
        payload = _make_payload([
            {
                "path": "src/orders.py",
                "line": 14,
                "snippet": 'kafkaTemplate.send("orders.created", payload)',
            },
        ])
        report = verify_payload(payload, REPO_A)
        self.assertEqual(report.facts[0].verdict, "disconfirmed")

    def test_line_beyond_eof_is_missing(self) -> None:
        payload = _make_payload([
            {"path": "src/orders.py", "line": 9999, "snippet": "anything"},
        ])
        report = verify_payload(payload, REPO_A)
        self.assertEqual(report.facts[0].evidence_missing, 1)

    # --- mixed verdict ----------------------------------------------------

    def test_mixed_evidence_produces_mixed_verdict(self) -> None:
        payload = _make_payload([
            {"path": "src/orders.py", "line": 15, "snippet": 'routing_key="shipping-task"'},
            {"path": "src/orders.py", "line": 2, "snippet": 'routing_key="shipping-task"'},
        ])
        report = verify_payload(payload, REPO_A)
        self.assertEqual(report.facts[0].verdict, "mixed")
        self.assertEqual(report.facts[0].evidence_matched, 1)
        self.assertEqual(report.facts[0].evidence_missing, 1)

    # --- multi-category ----------------------------------------------------

    def test_all_categories_are_checked(self) -> None:
        payload = {
            "repo": {
                "id": "fixture/repo_a",
                "summary": {
                    "evidence": [
                        {"path": "src/orders.py", "line": 1, "snippet": "Fixture"}
                    ]
                },
            },
            "components": [
                {
                    "name": "orders",
                    "evidence": [
                        {"path": "src/orders.py", "line": 7, "snippet": "def publish_order"}
                    ],
                }
            ],
            "apis": [],
            "resources": [
                {
                    "name": "shipping-task",
                    "evidence": [
                        {"path": "src/orders.py", "line": 14, "snippet": "queue_declare"}
                    ],
                }
            ],
            "dependencies": [
                {
                    "source": "orders",
                    "target": "shipping-task",
                    "kind": "producesMessage",
                    "evidence": [
                        {"path": "src/orders.py", "line": 15, "snippet": "routing_key"}
                    ],
                }
            ],
            "providers": [
                {
                    "name": "RabbitMQ",
                    "evidence": [
                        {"path": "src/orders.py", "line": 4, "snippet": "import pika"}
                    ],
                }
            ],
            "domain_attributes": [],
            "glossary": [],
        }
        report = verify_payload(payload, REPO_A)
        categories = {f.category for f in report.facts}
        # Every category that had at least one fact should be represented.
        self.assertIn("repo.summary", categories)
        self.assertIn("components", categories)
        self.assertIn("resources", categories)
        self.assertIn("dependencies", categories)
        self.assertIn("providers", categories)
        self.assertEqual(report.disconfirmed_facts, 0)

    # --- report serialisation ---------------------------------------------

    def test_report_to_dict_is_jsonable(self) -> None:
        payload = _make_payload([
            {"path": "src/orders.py", "line": 15, "snippet": 'routing_key="shipping-task"'},
        ])
        report = verify_payload(payload, REPO_A)
        # Round-trips cleanly through json.
        as_json = json.dumps(report.to_dict())
        loaded = json.loads(as_json)
        self.assertEqual(loaded["totals"]["facts"], 1)
        self.assertEqual(loaded["facts"][0]["verdict"], "confirmed")


if __name__ == "__main__":
    unittest.main()
