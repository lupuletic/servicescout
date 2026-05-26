import json
import tempfile
import unittest
from pathlib import Path

from servicescout.storage import (
    JSONBackend,
    _entity_haystack,
    _exact_term_boost,
    _metadata_hit_count,
    _metadata_ranking,
    _rrf,
)


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


class CapabilitySheetHaystackTests(unittest.TestCase):
    def test_haystack_includes_capability_sheet(self) -> None:
        entity = {
            "kind": "Component",
            "metadata": {
                "name": "ledger",
                "description": "",
                "annotations": {"capability_sheet": "Handles refund settlement and chargeback reconciliation."},
            },
            "spec": {},
        }
        haystack = _entity_haystack(entity)
        self.assertIn("chargeback", haystack)
        self.assertIn("reconciliation", haystack)

    def test_capability_sheet_terms_make_the_owning_service_findable(self) -> None:
        # The thin proxy echoes "order" in a short, keyword-dense description; the
        # real owner only describes its behaviour in the capability sheet. Folding
        # that sheet into the lexical haystack is what lets the owner win.
        catalog = {
            "entities": [
                {
                    "kind": "API",
                    "metadata": {
                        "name": "orders-gateway",
                        "description": "proxies order requests to the orders service",
                        "annotations": {},
                    },
                    "spec": {"type": "rest"},
                    "confidence": "high",
                },
                {
                    "kind": "Component",
                    "metadata": {
                        "name": "orders",
                        "description": "core service",
                        "annotations": {
                            "capability_sheet": "Handles order placement, cancellation, refunds and fulfilment scheduling.",
                        },
                    },
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
            hits = JSONBackend(path).search(
                "which service handles order cancellation and refunds", query_vector=None, limit=1
            )

        self.assertEqual(hits[0]["ref"], "Component:orders")


class MetadataFusionTests(unittest.TestCase):
    def _entity(self) -> dict:
        return {
            "spec": {
                "domain_attributes": [
                    {"attribute": "ShipmentStatus", "values": ["DISPATCHED", "DELIVERED"], "meaning": "shipment lifecycle"},
                ],
                "glossary": [
                    {"term": "manifest", "definition": "list of parcels", "synonyms": ["waybill"]},
                ],
            }
        }

    def test_hit_count_counts_attribute_and_glossary(self) -> None:
        self.assertEqual(_metadata_hit_count(self._entity(), ["dispatched", "waybill"]), 2)

    def test_no_match_and_empty_terms_give_zero(self) -> None:
        self.assertEqual(_metadata_hit_count(self._entity(), ["unrelated"]), 0)
        self.assertEqual(_metadata_hit_count(self._entity(), []), 0)

    def test_metadata_ranking_orders_by_hit_count(self) -> None:
        # entity 1 has the most hits, then 2, then 0; zero-hit entities are excluded upstream.
        self.assertEqual(_metadata_ranking({0: 1, 1: 3, 2: 2}), [1, 2, 0])

    def test_domain_attribute_match_outranks_lexical_only_decoy(self) -> None:
        catalog = {
            "entities": [
                {
                    "kind": "API",
                    "metadata": {
                        "name": "shipping-proxy",
                        "description": "forwards dispatch requests downstream",
                        "annotations": {},
                    },
                    "spec": {"type": "rest"},
                    "confidence": "high",
                },
                {
                    "kind": "Component",
                    "metadata": {"name": "logistics", "description": "core service", "annotations": {}},
                    "spec": {
                        "type": "service",
                        "domain_attributes": [
                            {
                                "attribute": "ShipmentStatus",
                                "values": ["DISPATCHED", "DELIVERED"],
                                "meaning": "tracks the dispatch lifecycle of a shipment",
                            }
                        ],
                    },
                    "confidence": "high",
                },
            ],
            "relations": [],
            "summary": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            path.write_text(json.dumps(catalog), encoding="utf-8")
            hits = JSONBackend(path).search(
                "which service tracks the dispatch shipment lifecycle status", query_vector=None, limit=1
            )

        self.assertEqual(hits[0]["ref"], "Component:logistics")


class ExactTermBoostTests(unittest.TestCase):
    def test_coarse_type_does_not_trigger_exact_boost(self) -> None:
        # spec.type is a coarse classification shared by many entities; a query
        # token equal to it must not flat-boost every entity of that type.
        entity = {"metadata": {"name": "thing-proxy", "annotations": {}}, "spec": {"type": "graphql"}}
        self.assertEqual(_exact_term_boost(entity, ["graphql"]), 0.0)

    def test_name_alias_and_technology_still_trigger_exact_boost(self) -> None:
        by_name = {"metadata": {"name": "graphql", "annotations": {}}, "spec": {"type": "service"}}
        by_tech = {"metadata": {"name": "x", "annotations": {}}, "spec": {"technology": "GraphQL"}}
        by_alias = {"metadata": {"name": "x", "annotations": {"aliases": ["graphql"]}}, "spec": {}}
        self.assertEqual(_exact_term_boost(by_name, ["graphql"]), 1.0)
        self.assertEqual(_exact_term_boost(by_tech, ["graphql"]), 1.0)
        self.assertEqual(_exact_term_boost(by_alias, ["graphql"]), 1.0)

    def test_type_only_match_does_not_outrank_richer_entity(self) -> None:
        catalog = {
            "entities": [
                {
                    "kind": "API",
                    "metadata": {"name": "thing-proxy", "description": "forwards requests downstream", "annotations": {}},
                    "spec": {"type": "graphql"},
                    "confidence": "high",
                },
                {
                    "kind": "Component",
                    "metadata": {
                        "name": "thing",
                        "description": "graphql gateway that resolves customer queries",
                        "annotations": {},
                    },
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
            hits = JSONBackend(path).search(
                "which graphql service resolves customer queries", query_vector=None, limit=1
            )

        self.assertEqual(hits[0]["ref"], "Component:thing")


class WeightedRrfTests(unittest.TestCase):
    def test_weights_favour_the_higher_weighted_list(self) -> None:
        # entity 0 tops the (heavier) vector list; entity 1 tops the lexical list.
        vec_rank = [0, 1]
        lex_rank = [1, 0]
        fused = _rrf([vec_rank, lex_rank], weights=[1.0, 0.6])
        self.assertGreater(fused[0], fused[1])

    def test_equal_weights_are_symmetric(self) -> None:
        fused = _rrf([[0, 1], [1, 0]])
        self.assertAlmostEqual(fused[0], fused[1])

    def test_single_list_order_is_unchanged_by_weight(self) -> None:
        fused = _rrf([[2, 5, 9]], weights=[0.6])
        self.assertGreater(fused[2], fused[5])
        self.assertGreater(fused[5], fused[9])


if __name__ == "__main__":
    unittest.main()
