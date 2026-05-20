"""Tests for the OpenLineage exporter (Epic #9 Tier 4 #10)."""

import json
import tempfile
import unittest
from pathlib import Path

from servicescout import export_openlineage


def _entity(kind: str, name: str, **fields):
    annotations = fields.pop("annotations", {})
    return {
        "kind": kind,
        "metadata": {"name": name, "description": "", "annotations": annotations},
        "spec": fields,
        "evidence": [],
        "confidence": fields.pop("confidence", "high"),
    }


def _relation(src: str, tgt: str, type_: str):
    return {"from": src, "to": tgt, "type": type_}


def _catalog() -> dict:
    return {
        "entities": [
            _entity("Component", "orders", system="sock-shop",
                    type="service", runtime="long-running",
                    lifecycle="production",
                    annotations={"owner": "team-orders", "tagline": "Order processor"}),
            _entity("Component", "billing", type="service",
                    annotations={"owner": "team-finance"}),
            _entity("Resource", "orders-db", type="database",
                    technology="MongoDB",
                    host_or_instance="mongodb-prod-1",
                    database_or_schema="orders",
                    tables_or_collections=["orders", "customers", "shipments"],
                    env_or_config_keys=["MONGO_URL", "MONGO_USER"]),
            _entity("Resource", "shipping-task", type="queue",
                    technology="RabbitMQ"),
        ],
        "relations": [
            _relation("Component:orders", "Resource:orders-db", "readsResource"),
            _relation("Component:orders", "Resource:orders-db", "writesResource"),
            _relation("Component:billing", "Resource:orders-db", "readsResource"),
            # Non-data-lineage edges should be ignored:
            _relation("Component:orders", "Resource:shipping-task", "producesMessage"),
        ],
    }


class BuildEventsTests(unittest.TestCase):
    def test_emits_one_event_per_data_active_component(self) -> None:
        events = export_openlineage.build_events(_catalog())
        names = sorted(e["job"]["name"] for e in events)
        self.assertEqual(names, ["billing", "orders"])

    def test_orders_event_has_input_and_output_for_db(self) -> None:
        events = export_openlineage.build_events(_catalog())
        orders = next(e for e in events if e["job"]["name"] == "orders")
        self.assertEqual(len(orders["inputs"]), 1)
        self.assertEqual(len(orders["outputs"]), 1)
        self.assertEqual(orders["inputs"][0]["name"], "orders.orders-db")
        self.assertEqual(orders["inputs"][0]["namespace"], "mongodb-prod-1")

    def test_dataset_carries_schema_facet_from_tables(self) -> None:
        events = export_openlineage.build_events(_catalog())
        orders = next(e for e in events if e["job"]["name"] == "orders")
        schema = orders["inputs"][0]["facets"].get("schema")
        self.assertIsNotNone(schema)
        names = {f["name"] for f in schema["fields"]}
        self.assertEqual(names, {"orders", "customers", "shipments"})

    def test_dataset_carries_config_facet_from_env_keys(self) -> None:
        events = export_openlineage.build_events(_catalog())
        orders = next(e for e in events if e["job"]["name"] == "orders")
        config = orders["inputs"][0]["facets"].get("servicescout_config")
        self.assertIsNotNone(config)
        self.assertIn("MONGO_URL", config["config_keys"])

    def test_job_namespace_uses_system(self) -> None:
        events = export_openlineage.build_events(_catalog())
        orders = next(e for e in events if e["job"]["name"] == "orders")
        self.assertEqual(orders["job"]["namespace"], "sock-shop")

    def test_event_has_required_top_level_fields(self) -> None:
        events = export_openlineage.build_events(_catalog())
        for e in events:
            for field in ("eventType", "eventTime", "run", "job", "inputs", "outputs",
                          "producer", "schemaURL"):
                self.assertIn(field, e, f"event missing required field {field}")
            self.assertEqual(e["eventType"], "COMPLETE")

    def test_components_without_data_edges_are_skipped(self) -> None:
        # A catalog with a component that has no reads/writes shouldn't
        # produce an event.
        catalog = {
            "entities": [
                _entity("Component", "frontend"),
                _entity("Resource", "db", type="database", technology="postgres",
                        tables_or_collections=["t"]),
            ],
            "relations": [],
        }
        events = export_openlineage.build_events(catalog)
        self.assertEqual(events, [])

    def test_non_resource_targets_are_ignored(self) -> None:
        catalog = {
            "entities": [
                _entity("Component", "a"),
                _entity("Component", "b"),
            ],
            "relations": [
                {"from": "Component:a", "to": "Component:b", "type": "readsResource"},
            ],
        }
        # Component-to-Component readsResource is semantically wrong;
        # the exporter should skip it.
        events = export_openlineage.build_events(catalog)
        self.assertEqual(events, [])


class WriteEventsTests(unittest.TestCase):
    def test_writes_one_file_per_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events = export_openlineage.build_events(_catalog())
            paths = export_openlineage.write_events(events, Path(tmp))
            self.assertEqual(len(paths), len(events))
            for p in paths:
                doc = json.loads(p.read_text())
                self.assertEqual(doc["eventType"], "COMPLETE")


if __name__ == "__main__":
    unittest.main()
