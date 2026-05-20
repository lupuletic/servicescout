"""Tests for the universal code-shape verifier (Phase B v2).

These exercise the LANGUAGE-AGNOSTIC behaviour: code vs blank vs
comment vs unparseable. There are intentionally NO language-specific
or framework-specific assertions — the goal is to verify the citation
points at real text content, not to re-derive the LLM's semantic claim.
"""

import unittest
from pathlib import Path

from servicescout.static_extractors import code_shape


FIXTURES = Path(__file__).parent / "fixtures"
REPO_A = FIXTURES / "repo_a"


def _payload(deps: list[dict]) -> dict:
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


class CodeShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        code_shape.clear_cache()

    # --- citation lands on actual code -----------------------------------

    def test_code_line_is_confirmed(self) -> None:
        # Line 14 of orders.py: `    channel.queue_declare(queue="shipping-task", durable=True)`
        payload = _payload([
            _dep("producesMessage", [{"path": "src/orders.py", "line": 14, "snippet": "queue_declare"}]),
        ])
        report = code_shape.verify_payload(payload, REPO_A)
        self.assertEqual(report.facts[0].verdict, "confirmed")

    def test_java_code_line_is_confirmed_without_language_knowledge(self) -> None:
        # Line 11 of shipping_consumer.java: a real `@RabbitListener` annotation.
        # The verifier doesn't *know* it's Java — it just sees text content.
        payload = _payload([
            _dep("consumesMessage", [{"path": "src/shipping_consumer.java", "line": 11, "snippet": "@RabbitListener"}]),
        ])
        report = code_shape.verify_payload(payload, REPO_A)
        self.assertEqual(report.facts[0].verdict, "confirmed")

    # --- citation lands on a non-code line --------------------------------

    def test_blank_line_is_disconfirmed(self) -> None:
        # Line 2 of orders.py is blank (after the docstring).
        payload = _payload([
            _dep("producesMessage", [{"path": "src/orders.py", "line": 2, "snippet": "anything"}]),
        ])
        report = code_shape.verify_payload(payload, REPO_A)
        self.assertEqual(report.facts[0].verdict, "disconfirmed")
        self.assertEqual(report.facts[0].blank, 1)

    def test_python_comment_line_is_disconfirmed(self) -> None:
        # Line 1 of orders.py is a docstring opener starting with `"""`.
        payload = _payload([
            _dep("producesMessage", [{"path": "src/orders.py", "line": 1, "snippet": "anything"}]),
        ])
        report = code_shape.verify_payload(payload, REPO_A)
        self.assertEqual(report.facts[0].verdict, "disconfirmed")

    # --- mixed: some real lines, some not ---------------------------------

    def test_mixed_citation_yields_mixed_verdict(self) -> None:
        payload = _payload([
            _dep("producesMessage", [
                {"path": "src/orders.py", "line": 14, "snippet": "queue_declare"},
                {"path": "src/orders.py", "line": 2, "snippet": "blank line"},
            ]),
        ])
        report = code_shape.verify_payload(payload, REPO_A)
        self.assertEqual(report.facts[0].verdict, "mixed")
        self.assertEqual(report.facts[0].code, 1)
        self.assertEqual(report.facts[0].blank, 1)

    # --- file / path failures -------------------------------------------

    def test_missing_file_is_disconfirmed(self) -> None:
        payload = _payload([
            _dep("producesMessage", [{"path": "src/nonexistent.go", "line": 5, "snippet": "anything"}]),
        ])
        report = code_shape.verify_payload(payload, REPO_A)
        self.assertEqual(report.facts[0].verdict, "disconfirmed")
        self.assertEqual(report.facts[0].unparseable, 1)

    def test_line_beyond_eof_is_disconfirmed(self) -> None:
        payload = _payload([
            _dep("producesMessage", [{"path": "src/orders.py", "line": 9999, "snippet": "anything"}]),
        ])
        report = code_shape.verify_payload(payload, REPO_A)
        self.assertEqual(report.facts[0].unparseable, 1)

    # --- universal: this works on ANY text file --------------------------

    def test_works_on_arbitrary_file_extension(self) -> None:
        # The verifier doesn't restrict to Java/Python/Go/etc. — give it a
        # made-up extension and it still checks code shape.
        custom = REPO_A / "src" / "config.example.toml"
        custom.write_text("[server]\nport = 8080\n# this is a comment\n", encoding="utf-8")
        try:
            payload = _payload([
                _dep("dependsOn", [{"path": "src/config.example.toml", "line": 2, "snippet": "port"}]),
            ])
            report = code_shape.verify_payload(payload, REPO_A)
            self.assertEqual(report.facts[0].verdict, "confirmed")

            payload2 = _payload([
                _dep("dependsOn", [{"path": "src/config.example.toml", "line": 3, "snippet": "comment"}]),
            ])
            report2 = code_shape.verify_payload(payload2, REPO_A)
            self.assertEqual(report2.facts[0].verdict, "disconfirmed")
        finally:
            custom.unlink(missing_ok=True)

    # --- only dependencies are checked ------------------------------------

    def test_only_dependencies_are_checked(self) -> None:
        payload = _payload([])
        payload["resources"] = [
            {
                "name": "shipping-task",
                "evidence": [{"path": "src/orders.py", "line": 14, "snippet": "queue_declare"}],
            }
        ]
        report = code_shape.verify_payload(payload, REPO_A)
        # Resources aren't in scope for Phase B (Phase A covers them).
        self.assertEqual(len(report.facts), 0)


if __name__ == "__main__":
    unittest.main()
