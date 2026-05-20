import unittest
from pathlib import Path

from static_extractors import calibrate, code_shape as ast_crosscheck, snippet_verify

FIXTURES = Path(__file__).parent / "fixtures"
REPO_A = FIXTURES / "repo_a"


def _dep(confidence: str, evidence: list[dict]) -> dict:
    return {
        "source": "orders",
        "target": "shipping-task",
        "target_kind": "resource",
        "kind": "producesMessage",
        "protocol": "amqp",
        "operation_or_usage": "",
        "message_or_event_name": "shipping-task",
        "env_or_config_keys": [],
        "aliases": [],
        "confidence": confidence,
        "notes": "",
        "evidence": evidence,
    }


def _resource(confidence: str, evidence: list[dict]) -> dict:
    return {
        "name": "shipping-task",
        "type": "queue",
        "technology": "RabbitMQ",
        "host_or_instance": "",
        "database_or_schema": "",
        "tables_or_collections": [],
        "access": "publish",
        "env_or_config_keys": [],
        "used_by": "orders",
        "messaging_pattern": "broker-queue",
        "subscribes_to": "",
        "datasource_url": "",
        "confidence": confidence,
        "notes": "",
        "evidence": evidence,
    }


def _payload(dependencies: list[dict], resources: list[dict] | None = None) -> dict:
    return {
        "repo": {"id": "fixture/repo_a"},
        "components": [],
        "apis": [],
        "resources": resources or [],
        "providers": [],
        "domain_attributes": [],
        "glossary": [],
        "dependencies": dependencies,
    }


def _verify(payload: dict) -> snippet_verify.VerifyReport:
    snippet_verify.clear_cache()
    return snippet_verify.verify_payload(payload, REPO_A)


def _verify_ast(payload: dict) -> ast_crosscheck.AstReport:
    ast_crosscheck.clear_cache()
    return ast_crosscheck.verify_payload(payload, REPO_A)


class CalibrateTests(unittest.TestCase):

    def test_confirmed_promotes_medium_to_high(self) -> None:
        payload = _payload([
            _dep(
                "medium",
                [{"path": "src/orders.py", "line": 15, "snippet": 'routing_key="shipping-task"'}],
            )
        ])
        result = calibrate.apply(payload, _verify(payload), None)
        self.assertEqual(payload["dependencies"][0]["confidence"], "high")
        self.assertEqual(len(result["changes"]), 1)
        self.assertEqual(result["changes"][0]["before"], "medium")
        self.assertEqual(result["changes"][0]["after"], "high")
        self.assertEqual(payload["dependencies"][0]["_cross_check"]["verdict"], "confirmed")

    def test_high_stays_high(self) -> None:
        payload = _payload([
            _dep(
                "high",
                [{"path": "src/orders.py", "line": 15, "snippet": 'routing_key="shipping-task"'}],
            )
        ])
        calibrate.apply(payload, _verify(payload), None)
        self.assertEqual(payload["dependencies"][0]["confidence"], "high")

    def test_disconfirmed_demotes_high_to_review(self) -> None:
        payload = _payload([
            _dep("high", [{"path": "src/nonexistent.py", "line": 1, "snippet": "anything"}])
        ])
        result = calibrate.apply(payload, _verify(payload), None)
        self.assertEqual(payload["dependencies"][0]["confidence"], "review")
        self.assertEqual(result["changes"][0]["before"], "high")
        self.assertEqual(result["changes"][0]["after"], "review")
        self.assertEqual(payload["dependencies"][0]["_cross_check"]["verdict"], "disconfirmed")

    def test_mixed_does_not_mutate_confidence(self) -> None:
        payload = _payload([
            _dep(
                "medium",
                [
                    {"path": "src/orders.py", "line": 15, "snippet": 'routing_key="shipping-task"'},
                    {"path": "src/orders.py", "line": 2, "snippet": 'routing_key="shipping-task"'},
                ],
            )
        ])
        calibrate.apply(payload, _verify(payload), None)
        self.assertEqual(payload["dependencies"][0]["confidence"], "medium")
        self.assertEqual(payload["dependencies"][0]["_cross_check"]["verdict"], "mixed")

    def test_resources_are_calibrated_too(self) -> None:
        payload = _payload(
            dependencies=[],
            resources=[
                _resource(
                    "low",
                    [{"path": "src/orders.py", "line": 14, "snippet": "queue_declare"}],
                )
            ],
        )
        calibrate.apply(payload, _verify(payload), None)
        self.assertEqual(payload["resources"][0]["confidence"], "medium")

    def test_non_confidence_categories_get_annotated_only(self) -> None:
        payload = {
            "repo": {"id": "fixture/repo_a"},
            "components": [
                {
                    "name": "orders",
                    "evidence": [
                        {"path": "src/orders.py", "line": 7, "snippet": "def publish_order"}
                    ],
                }
            ],
            "apis": [],
            "resources": [],
            "providers": [],
            "domain_attributes": [],
            "glossary": [],
            "dependencies": [],
        }
        calibrate.apply(payload, _verify(payload), None)
        # Components don't carry confidence in the schema — should only get
        # the cross-check annotation.
        self.assertNotIn("confidence", payload["components"][0])
        self.assertEqual(payload["components"][0]["_cross_check"]["verdict"], "confirmed")


class CombinedCalibrationTests(unittest.TestCase):
    """Phase A + Phase B verdict combination."""

    def test_a_confirmed_b_confirmed_promotes(self) -> None:
        # producesMessage at orders.py:15 (basic_publish present)
        payload = _payload([
            _dep(
                "medium",
                [{"path": "src/orders.py", "line": 15, "snippet": 'routing_key="shipping-task"'}],
            )
        ])
        payload["dependencies"][0]["kind"] = "producesMessage"
        result = calibrate.apply(payload, _verify(payload), _verify_ast(payload))
        self.assertEqual(payload["dependencies"][0]["confidence"], "high")
        self.assertEqual(result["changes"][0]["combined_verdict"], "confirmed")

    def test_a_confirmed_b_disconfirmed_becomes_mixed(self) -> None:
        # The Java consumer file has a RabbitListener but no publish.
        # If LLM cites it as producesMessage, snippet matches the line
        # (LLM put the listener annotation in the snippet too) but the
        # AST for producesMessage doesn't match anywhere → mixed.
        payload = _payload([
            _dep(
                "high",
                [{"path": "src/shipping_consumer.java", "line": 8, "snippet": "@RabbitListener"}],
            )
        ])
        payload["dependencies"][0]["kind"] = "producesMessage"
        result = calibrate.apply(payload, _verify(payload), _verify_ast(payload))
        # Phase A says snippet is there. Phase B says: no producesMessage
        # pattern in this file. Combined → mixed. Confidence unchanged.
        self.assertEqual(payload["dependencies"][0]["confidence"], "high")
        # The annotation lands on the fact.
        self.assertEqual(
            payload["dependencies"][0]["_cross_check_ast"]["verdict"], "disconfirmed"
        )

    def test_a_disconfirmed_dominates(self) -> None:
        payload = _payload([
            _dep(
                "high",
                [{"path": "src/nonexistent.py", "line": 1, "snippet": "anything"}],
            )
        ])
        payload["dependencies"][0]["kind"] = "producesMessage"
        calibrate.apply(payload, _verify(payload), _verify_ast(payload))
        # Phase A disconfirms → review regardless of B.
        self.assertEqual(payload["dependencies"][0]["confidence"], "review")

    def test_b_unsupported_falls_back_to_a(self) -> None:
        # Phase B has no rules for repos without language files — but here
        # we use the python orders.py, where producesMessage IS supported,
        # so we engineer "unsupported" with a different kind: dependsOn
        # would require an import statement. Use a non-existent kind to
        # simulate unsupported.
        payload = _payload([
            _dep(
                "medium",
                [{"path": "src/orders.py", "line": 15, "snippet": 'routing_key="shipping-task"'}],
            )
        ])
        payload["dependencies"][0]["kind"] = "weirdKindNotInRules"
        result = calibrate.apply(payload, _verify(payload), _verify_ast(payload))
        # Phase A confirmed; Phase B unsupported → falls back to A → promote.
        self.assertEqual(payload["dependencies"][0]["confidence"], "high")


class CollectProblemsTests(unittest.TestCase):
    def test_disconfirmed_facts_become_problems(self) -> None:
        payload = _payload([
            _dep(
                "high",
                [{"path": "src/nonexistent.py", "line": 1, "snippet": "anything"}],
            )
        ])
        payload["dependencies"][0]["kind"] = "producesMessage"
        problems = calibrate.collect_problems(
            payload, _verify(payload), _verify_ast(payload)
        )
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]["combined_verdict"], "disconfirmed")
        self.assertTrue(problems[0]["reasons"])
        self.assertEqual(problems[0]["category"], "dependencies")

    def test_confirmed_facts_are_not_problems(self) -> None:
        payload = _payload([
            _dep(
                "medium",
                [{"path": "src/orders.py", "line": 15, "snippet": 'routing_key="shipping-task"'}],
            )
        ])
        payload["dependencies"][0]["kind"] = "producesMessage"
        problems = calibrate.collect_problems(
            payload, _verify(payload), _verify_ast(payload)
        )
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
