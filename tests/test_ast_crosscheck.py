import unittest
from pathlib import Path

from static_extractors import ast_crosscheck


FIXTURES = Path(__file__).parent / "fixtures"
REPO_A = FIXTURES / "repo_a"


def _payload(dependencies: list[dict]) -> dict:
    return {
        "repo": {"id": "fixture/repo_a"},
        "components": [],
        "apis": [],
        "resources": [],
        "providers": [],
        "domain_attributes": [],
        "glossary": [],
        "dependencies": dependencies,
    }


def _dep(kind: str, evidence: list[dict]) -> dict:
    return {
        "source": "src",
        "target": "tgt",
        "target_kind": "resource",
        "kind": kind,
        "protocol": "amqp",
        "operation_or_usage": "",
        "message_or_event_name": "",
        "env_or_config_keys": [],
        "aliases": [],
        "confidence": "medium",
        "notes": "",
        "evidence": evidence,
    }


class AstCrossCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        ast_crosscheck.clear_cache()

    # --- producesMessage: Python publish call -----------------------------

    def test_python_publish_call_is_confirmed(self) -> None:
        payload = _payload([
            _dep(
                "producesMessage",
                [{"path": "src/orders.py", "line": 14, "snippet": "channel.basic_publish"}],
            )
        ])
        report = ast_crosscheck.verify_payload(payload, REPO_A)
        self.assertEqual(report.facts[0].verdict, "confirmed")

    def test_python_publish_call_within_window(self) -> None:
        # Cite the def line; basic_publish is several lines down.
        payload = _payload([
            _dep(
                "producesMessage",
                [{"path": "src/orders.py", "line": 8, "snippet": "def publish_order"}],
            )
        ])
        report = ast_crosscheck.verify_payload(payload, REPO_A)
        # basic_publish is within +8 lines of the def → confirmed.
        self.assertEqual(report.facts[0].verdict, "confirmed")

    def test_python_publish_far_from_cited_line_is_mixed(self) -> None:
        # Same correct kind, but cite line 1 (the docstring). The publish
        # call is 13 lines away — outside the search window.
        payload = _payload([
            _dep(
                "producesMessage",
                [{"path": "src/orders.py", "line": 1, "snippet": "Fixture"}],
            )
        ])
        report = ast_crosscheck.verify_payload(payload, REPO_A)
        self.assertEqual(report.facts[0].verdict, "mixed")

    # --- consumesMessage: Java RabbitListener annotation ------------------

    def test_java_rabbit_listener_is_confirmed(self) -> None:
        payload = _payload([
            _dep(
                "consumesMessage",
                [{"path": "src/shipping_consumer.java", "line": 8, "snippet": "@RabbitListener"}],
            )
        ])
        report = ast_crosscheck.verify_payload(payload, REPO_A)
        self.assertEqual(report.facts[0].verdict, "confirmed")

    def test_java_wrong_kind_for_listener_file_is_disconfirmed(self) -> None:
        # The Java fixture has a listener annotation but NO publish calls.
        # Asking Phase B "is this line a producesMessage?" should disconfirm.
        payload = _payload([
            _dep(
                "producesMessage",
                [{"path": "src/shipping_consumer.java", "line": 8, "snippet": "@RabbitListener"}],
            )
        ])
        report = ast_crosscheck.verify_payload(payload, REPO_A)
        # No publish-pattern match anywhere in the file → disconfirmed.
        self.assertEqual(report.facts[0].verdict, "disconfirmed")

    # --- unsupported file types -------------------------------------------

    def test_unknown_extension_is_unsupported(self) -> None:
        payload = _payload([
            _dep(
                "producesMessage",
                [{"path": "src/shipping_consumer.java.unknown", "line": 1, "snippet": "anything"}],
            )
        ])
        report = ast_crosscheck.verify_payload(payload, REPO_A)
        self.assertEqual(report.facts[0].verdict, "unsupported")

    def test_missing_file_is_unsupported(self) -> None:
        payload = _payload([
            _dep(
                "producesMessage",
                [{"path": "src/nonexistent.py", "line": 1, "snippet": "anything"}],
            )
        ])
        report = ast_crosscheck.verify_payload(payload, REPO_A)
        self.assertEqual(report.facts[0].verdict, "unsupported")

    # --- skips out-of-scope categories ------------------------------------

    def test_only_dependencies_are_checked(self) -> None:
        payload = _payload([])
        payload["resources"] = [
            {
                "name": "shipping-task",
                "evidence": [{"path": "src/orders.py", "line": 14, "snippet": "queue_declare"}],
            }
        ]
        report = ast_crosscheck.verify_payload(payload, REPO_A)
        # Empty since dependencies is empty.
        self.assertEqual(len(report.facts), 0)


if __name__ == "__main__":
    unittest.main()
