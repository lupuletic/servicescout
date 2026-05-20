import json
import tempfile
import unittest
from pathlib import Path

from storage import JSONBackend


class StorageSearchTests(unittest.TestCase):
    def test_exact_alias_or_technology_match_is_boosted(self) -> None:
        catalog = {
            "entities": [
                {
                    "kind": "Component",
                    "metadata": {"name": "shipping", "description": "Spring service using RabbitMQ heavily", "annotations": {}},
                    "spec": {"type": "service"},
                    "confidence": "high",
                },
                {
                    "kind": "Resource",
                    "metadata": {
                        "name": "rabbitmq",
                        "description": "RabbitMQ broker",
                        "annotations": {"aliases": ["RabbitMQ"]},
                    },
                    "spec": {"type": "broker", "technology": "RabbitMQ", "host": "rabbitmq"},
                    "confidence": "medium",
                },
            ],
            "relations": [],
            "summary": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            path.write_text(json.dumps(catalog), encoding="utf-8")
            hits = JSONBackend(path).search("Which services consume messages from RabbitMQ?", query_vector=None, limit=1)

        self.assertEqual(hits[0]["ref"], "Resource:rabbitmq")

    def test_generic_service_term_does_not_boost_every_component(self) -> None:
        catalog = {
            "entities": [
                {
                    "kind": "Component",
                    "metadata": {"name": "frontend", "description": "Storefront web UI", "annotations": {}},
                    "spec": {"type": "website"},
                    "confidence": "high",
                },
                {
                    "kind": "Component",
                    "metadata": {"name": "paymentservice", "description": "Payment authorization service", "annotations": {}},
                    "spec": {"type": "service"},
                    "confidence": "high",
                },
            ],
            "relations": [],
            "summary": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            path.write_text(json.dumps(catalog), encoding="utf-8")
            hits = JSONBackend(path).search("Which service serves the storefront web UI?", query_vector=None, limit=1)

        self.assertEqual(hits[0]["ref"], "Component:frontend")


if __name__ == "__main__":
    unittest.main()
