import unittest
from pathlib import Path

from static_extractors import calibrate, snippet_verify

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


class CalibrateTests(unittest.TestCase):

    def test_confirmed_promotes_medium_to_high(self) -> None:
        payload = _payload([
            _dep(
                "medium",
                [{"path": "src/orders.py", "line": 15, "snippet": 'routing_key="shipping-task"'}],
            )
        ])
        result = calibrate.apply(payload, _verify(payload))
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
        calibrate.apply(payload, _verify(payload))
        self.assertEqual(payload["dependencies"][0]["confidence"], "high")

    def test_disconfirmed_demotes_high_to_review(self) -> None:
        payload = _payload([
            _dep("high", [{"path": "src/nonexistent.py", "line": 1, "snippet": "anything"}])
        ])
        result = calibrate.apply(payload, _verify(payload))
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
        calibrate.apply(payload, _verify(payload))
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
        calibrate.apply(payload, _verify(payload))
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
        calibrate.apply(payload, _verify(payload))
        # Components don't carry confidence in the schema — should only get
        # the cross-check annotation.
        self.assertNotIn("confidence", payload["components"][0])
        self.assertEqual(payload["components"][0]["_cross_check"]["verdict"], "confirmed")


if __name__ == "__main__":
    unittest.main()
