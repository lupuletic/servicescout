import unittest
import json
import tempfile
from copy import deepcopy
from pathlib import Path

from build_catalog import (
    Catalog,
    add_repo,
    demote_infrastructure_components,
    demote_provider_duplicate_components,
    derive_broker_resources,
    entity_ref,
    merge_static_api_operations,
    merge_suffix_duplicate_components,
    normalize_domain_label,
    normalize_api_granularity,
    resolve_deferred_dependencies,
    short_repo_name,
)


def _repo_payload() -> dict:
    return {
        "repo": {"id": "acme/orders", "system": "commerce", "domain": "checkout"},
        "components": [{"name": "orders", "type": "service", "runtime": "long-running"}],
        "apis": [
            {
                "name": "Orders API",
                "type": "rest",
                "operations": [{"method": "POST", "path": "/orders"}],
                "evidence": [{"path": "api/openapi.yaml", "line": 4, "snippet": "orders"}],
            }
        ],
        "resources": [
            {
                "name": "orders-db",
                "type": "database",
                "technology": "PostgreSQL",
                "host_or_instance": "orders-db:5432",
                "access": "read-write",
                "env_or_config_keys": ["DATABASE_URL"],
                "evidence": [{"path": "src/db.py", "line": 2, "snippet": "DATABASE_URL"}],
            },
            {
                "name": "shipping-task",
                "type": "queue",
                "technology": "RabbitMQ",
                "host_or_instance": "rabbitmq",
                "access": "publish",
                "evidence": [{"path": "src/ship.py", "line": 9, "snippet": "shipping-task"}],
            },
        ],
        "providers": [
            {
                "name": "Sentry",
                "category": "observability",
                "aliases": ["SENTRY_DSN"],
                "evidence": [{"path": "src/app.py", "line": 3, "snippet": "SENTRY_DSN"}],
            }
        ],
        "dependencies": [
            {
                "source": "orders",
                "target": "orders-db",
                "target_kind": "resource",
                "kind": "readsResource",
                "protocol": "database",
                "env_or_config_keys": ["DATABASE_URL"],
            },
            {
                "source": "orders",
                "target": "shipping-task",
                "target_kind": "resource",
                "kind": "producesMessage",
                "protocol": "amqp",
            },
            {
                "source": "orders",
                "target": "Sentry",
                "target_kind": "provider",
                "kind": "dependsOn",
                "protocol": "https",
            },
        ],
        "domain_attributes": [],
        "glossary": [],
    }


class CatalogReconciliationTests(unittest.TestCase):
    def test_domain_labels_normalize_common_ecommerce_drift(self) -> None:
        self.assertEqual(normalize_domain_label("ecommerce"), "e-commerce")
        self.assertEqual(normalize_domain_label("e-commerce / shopping carts"), "e-commerce")
        self.assertEqual(normalize_domain_label("e-commerce storefront"), "e-commerce")
        self.assertEqual(normalize_domain_label("payments"), "payments")

    def test_short_repo_name_uses_last_segment_for_monorepo_units(self) -> None:
        self.assertEqual(short_repo_name("acme/orders"), "orders")
        self.assertEqual(short_repo_name("acme/commerce/orders"), "orders")

    def test_component_aliases_are_indexed_for_dependency_resolution(self) -> None:
        catalog = Catalog()
        producer = _repo_payload()
        producer["repo"]["id"] = "acme/cartservice"
        producer["components"] = [
            {
                "name": "cartservice",
                "type": "service",
                "aliases": ["CART_SERVICE_ADDR", "cartservice:7070"],
            }
        ]
        add_repo(catalog, producer)

        caller = _repo_payload()
        caller["repo"]["id"] = "acme/frontend"
        caller["components"] = [{"name": "frontend", "type": "website"}]
        caller["dependencies"] = [
            {
                "source": "frontend",
                "target": "microservices-demo-cartservice",
                "aliases": ["CART_SERVICE_ADDR"],
                "target_kind": "component",
                "kind": "consumesApi",
                "protocol": "grpc",
            }
        ]
        result = add_repo(catalog, caller)
        resolve_deferred_dependencies(catalog, {"acme/frontend": result["deferred_dependencies"]})
        entities = {entity_ref(e) for e in catalog.to_json()["entities"]}
        edges = {(r["from"], r["type"], r["to"]) for r in catalog.to_json()["relations"]}

        self.assertIn("Component:cartservice", entities)
        self.assertNotIn("Component:microservices-demo-cartservice", entities)
        self.assertIn(("Component:frontend", "consumesApi", "Component:cartservice"), edges)

    def test_deferred_dependencies_respect_target_kind(self) -> None:
        catalog = Catalog()
        result = add_repo(catalog, _repo_payload())
        unresolved = resolve_deferred_dependencies(catalog, {"acme/orders": result["deferred_dependencies"]})
        payload = catalog.to_json()
        refs = {entity_ref(e) for e in payload["entities"]}

        self.assertEqual(unresolved, 0)
        self.assertIn("Resource:orders-db", refs)
        self.assertIn("Resource:shipping-task", refs)
        self.assertIn("Provider:Sentry", refs)
        self.assertNotIn("Component:orders-db", refs)
        self.assertNotIn("Component:shipping-task", refs)
        self.assertNotIn("Component:Sentry", refs)

        edges = {(r["from"], r["type"], r["to"]) for r in payload["relations"]}
        self.assertIn(("Component:orders", "readsResource", "Resource:orders-db"), edges)
        self.assertIn(("Component:orders", "producesMessage", "Resource:shipping-task"), edges)
        self.assertIn(("Component:orders", "dependsOn", "Provider:Sentry"), edges)

    def test_non_component_entities_keep_source_provenance(self) -> None:
        catalog = Catalog()
        add_repo(catalog, _repo_payload())
        entities = {entity_ref(e): e for e in catalog.to_json()["entities"]}

        for ref in ("API:Orders-API", "Resource:orders-db", "Provider:Sentry"):
            annotations = entities[ref]["metadata"].get("annotations") or {}
            self.assertEqual(annotations.get("source_repos"), ["acme/orders"])

    def test_unresolved_resource_dependency_creates_resource_placeholder(self) -> None:
        catalog = Catalog()
        payload = _repo_payload()
        payload["resources"] = []
        payload["providers"] = []
        payload["dependencies"] = [
            {
                "source": "orders",
                "target": "missing-topic",
                "target_kind": "resource",
                "kind": "consumesMessage",
                "protocol": "Kafka",
                "env_or_config_keys": ["TOPIC"],
            }
        ]
        result = add_repo(catalog, payload)
        unresolved = resolve_deferred_dependencies(catalog, {"acme/orders": result["deferred_dependencies"]})
        entities = {entity_ref(e): e for e in catalog.to_json()["entities"]}

        self.assertEqual(unresolved, 1)
        self.assertIn("Resource:missing-topic", entities)
        self.assertNotIn("Component:missing-topic", entities)
        self.assertEqual(entities["Resource:missing-topic"]["confidence"], "review")

    def test_caller_supplied_api_dependency_does_not_create_component_service(self) -> None:
        catalog = Catalog()
        payload = _repo_payload()
        payload["dependencies"] = [
            {
                "source": "orders",
                "target": "Cart Items API",
                "target_kind": "component",
                "kind": "consumesApi",
                "protocol": "http",
                "aliases": ["item.items", "items"],
                "operation_or_usage": "GET caller-supplied items URI before order creation",
                "notes": "The concrete host and path come from the request body.",
            }
        ]

        result = add_repo(catalog, payload)
        resolve_deferred_dependencies(catalog, {"acme/orders": result["deferred_dependencies"]})
        entities = {entity_ref(e): e for e in catalog.to_json()["entities"]}
        edges = {(r["from"], r["type"], r["to"]) for r in catalog.to_json()["relations"]}

        self.assertIn("API:Cart-Items-API", entities)
        self.assertNotIn("Component:Cart-Items-API", entities)
        self.assertEqual(entities["API:Cart-Items-API"]["confidence"], "review")
        self.assertEqual(
            entities["API:Cart-Items-API"]["metadata"]["annotations"].get("dynamic_dependency"),
            "true",
        )
        self.assertIn(("Component:orders", "consumesApi", "API:Cart-Items-API"), edges)
        relation = next(
            r for r in catalog.to_json()["relations"]
            if (r["from"], r["type"], r["to"]) == ("Component:orders", "consumesApi", "API:Cart-Items-API")
        )
        self.assertEqual(relation["confidence"], "review")

    def test_unresolved_external_infrastructure_dependency_becomes_review_provider(self) -> None:
        catalog = Catalog()
        payload = _repo_payload()
        payload["providers"] = []
        payload["dependencies"] = [
            {
                "source": "orders",
                "target": "Docker Engine",
                "target_kind": "external",
                "kind": "dependsOn",
                "protocol": "unknown",
                "operation_or_usage": "Creates containers through a runtime API.",
                "evidence": [{"path": "src/runtime.py", "line": 3, "snippet": "DockerClient()"}],
                "confidence": "high",
            }
        ]

        result = add_repo(catalog, payload)
        resolve_deferred_dependencies(catalog, {"acme/orders": result["deferred_dependencies"]})
        entities = {entity_ref(e): e for e in catalog.to_json()["entities"]}
        relation = next(
            r for r in catalog.to_json()["relations"]
            if r["from"] == "Component:orders" and r["type"] == "dependsOn"
        )

        self.assertIn("Provider:Docker-Engine", entities)
        self.assertNotIn("Component:Docker-Engine", entities)
        self.assertEqual(entities["Provider:Docker-Engine"]["confidence"], "review")
        self.assertEqual(relation["to"], "Provider:Docker-Engine")
        self.assertEqual(relation["confidence"], "review")

    def test_external_component_duplicate_of_provider_is_demoted(self) -> None:
        payload = {
            "entities": [
                {
                    "kind": "Component",
                    "metadata": {
                        "name": "opentelemetrycollector",
                        "annotations": {
                            "external": "true",
                            "aliases": ["COLLECTOR_SERVICE_ADDR", "opentelemetrycollector:4317"],
                        },
                    },
                    "spec": {"type": "service"},
                    "evidence": [{"path": "deployment.yaml", "line": 1, "snippet": "opentelemetrycollector"}],
                    "confidence": "medium",
                },
                {
                    "kind": "Provider",
                    "metadata": {
                        "name": "OpenTelemetry-Collector",
                        "annotations": {
                            "external": "true",
                            "aliases": ["opentelemetrycollector"],
                            "source_repos": ["acme/orders"],
                        },
                    },
                    "spec": {"type": "provider"},
                    "evidence": [],
                    "confidence": "high",
                },
                {
                    "kind": "Component",
                    "metadata": {"name": "orders", "annotations": {"source_repos": ["acme/orders"]}},
                    "spec": {"type": "service"},
                    "evidence": [],
                    "confidence": "high",
                },
            ],
            "relations": [
                {
                    "from": "Component:orders",
                    "type": "consumesApi",
                    "to": "Component:opentelemetrycollector",
                    "properties": {"target_kind": "component"},
                    "evidence": [],
                    "confidence": "medium",
                }
            ],
        }

        self.assertEqual(demote_provider_duplicate_components(payload), 1)
        refs = {entity_ref(e) for e in payload["entities"]}
        self.assertNotIn("Component:opentelemetrycollector", refs)
        self.assertIn("Provider:OpenTelemetry-Collector", refs)
        self.assertEqual(payload["relations"][0]["type"], "dependsOn")
        self.assertEqual(payload["relations"][0]["to"], "Provider:OpenTelemetry-Collector")
        self.assertEqual(payload["relations"][0]["properties"]["target_kind"], "provider")
        self.assertEqual(payload["relations"][0]["confidence"], "review")

    def test_external_component_suffix_duplicate_merges_into_sourced_component(self) -> None:
        payload = {
            "entities": [
                {
                    "kind": "Component",
                    "metadata": {"name": "cartservice", "annotations": {"source_repos": ["acme/mono/cartservice"]}},
                    "spec": {"type": "service"},
                    "evidence": [],
                    "confidence": "high",
                },
                {
                    "kind": "Component",
                    "metadata": {
                        "name": "microservices-demo-cartservice",
                        "annotations": {"external": "true", "aliases": ["CART_SERVICE_ADDR"]},
                    },
                    "spec": {"type": "service"},
                    "evidence": [],
                    "confidence": "medium",
                },
                {
                    "kind": "Component",
                    "metadata": {"name": "frontend", "annotations": {"source_repos": ["acme/mono/frontend"]}},
                    "spec": {"type": "website"},
                    "evidence": [],
                    "confidence": "high",
                },
            ],
            "relations": [
                {
                    "from": "Component:frontend",
                    "type": "consumesApi",
                    "to": "Component:microservices-demo-cartservice",
                    "properties": {"target_kind": "component"},
                    "evidence": [],
                    "confidence": "medium",
                }
            ],
        }

        self.assertEqual(merge_suffix_duplicate_components(payload), 1)
        refs = {entity_ref(e) for e in payload["entities"]}
        self.assertIn("Component:cartservice", refs)
        self.assertNotIn("Component:microservices-demo-cartservice", refs)
        self.assertEqual(payload["relations"][0]["to"], "Component:cartservice")

    def test_broker_resource_is_derived_from_queue_technology(self) -> None:
        catalog = Catalog()
        result = add_repo(catalog, _repo_payload())
        resolve_deferred_dependencies(catalog, {"acme/orders": result["deferred_dependencies"]})
        payload = catalog.to_json()

        added = derive_broker_resources(payload)
        refs = {entity_ref(e) for e in payload["entities"]}
        edges = {(r["from"], r["type"], r["to"]) for r in payload["relations"]}

        self.assertGreaterEqual(added, 2)
        self.assertIn("Resource:rabbitmq", refs)
        self.assertIn(("Resource:shipping-task", "dependsOn", "Resource:rabbitmq"), edges)
        self.assertIn(("Component:orders", "producesMessage", "Resource:rabbitmq"), edges)

    def test_route_level_api_fragments_are_collapsed_into_contract(self) -> None:
        payload = {
            "entities": [
                {
                    "kind": "Component",
                    "metadata": {"name": "shipping", "annotations": {}},
                    "spec": {"type": "service"},
                    "evidence": [],
                    "confidence": "high",
                },
                *[
                    {
                        "kind": "API",
                        "metadata": {
                            "name": name,
                            "annotations": {
                                "exposed_by": "shipping",
                                "operations": [{"method": method, "path": path}],
                                "source_repos": ["acme/shipping"],
                            },
                        },
                        "spec": {"type": "rest"},
                        "evidence": [{"path": "src/api.java", "line": i, "snippet": path}],
                        "confidence": "high",
                    }
                    for i, (name, method, path) in enumerate(
                        [
                            ("createShipping", "POST", "/shipping"),
                            ("getShippingById", "GET", "/shipping/{id}"),
                            ("listShipping", "GET", "/shipping"),
                            ("health", "GET", "/health"),
                        ],
                        start=1,
                    )
                ],
            ],
            "relations": [
                {"from": "Component:shipping", "type": "providesApi", "to": f"API:{name}", "evidence": [], "properties": {}, "confidence": "high"}
                for name in ("createShipping", "getShippingById", "listShipping", "health")
            ],
        }

        collapsed = normalize_api_granularity(payload)
        refs = {entity_ref(e) for e in payload["entities"]}
        edges = {(r["from"], r["type"], r["to"]) for r in payload["relations"]}
        merged = next(e for e in payload["entities"] if entity_ref(e) == "API:shipping-API")

        self.assertEqual(collapsed, 3)
        self.assertIn("API:shipping-API", refs)
        self.assertNotIn("API:createShipping", refs)
        self.assertEqual(len(merged["metadata"]["annotations"]["operations"]), 4)
        self.assertIn(("Component:shipping", "providesApi", "API:shipping-API"), edges)

    def test_verbose_database_resource_name_prefers_concrete_host_when_safe(self) -> None:
        payload = _repo_payload()
        payload["resources"][0]["name"] = "MongoDB orders data store"
        payload["resources"][0]["host_or_instance"] = "orders-db:27017"

        catalog = Catalog()
        add_repo(catalog, payload)
        refs = {entity_ref(e) for e in catalog.to_json()["entities"]}

        self.assertIn("Resource:orders-db", refs)
        self.assertNotIn("Resource:MongoDB-orders-data-store", refs)

    def test_infrastructure_only_component_is_demoted_to_matching_resource(self) -> None:
        payload = {
            "entities": [
                {
                    "kind": "Component",
                    "metadata": {"name": "user", "annotations": {}, "tags": []},
                    "spec": {"type": "service"},
                    "evidence": [],
                    "confidence": "high",
                },
                {
                    "kind": "Component",
                    "metadata": {
                        "name": "user-db",
                        "description": "MongoDB seed data image",
                        "annotations": {},
                        "tags": ["mongodb", "seed-data"],
                    },
                    "spec": {"type": "unknown"},
                    "evidence": [{"path": "docker/user-db/Dockerfile", "line": 1, "snippet": "FROM mongo:3"}],
                    "confidence": "high",
                },
                {
                    "kind": "Resource",
                    "metadata": {"name": "users", "annotations": {"source_repos": ["acme/user"]}},
                    "spec": {"type": "database", "technology": "MongoDB", "host": "user-db:27017"},
                    "evidence": [],
                    "confidence": "high",
                },
            ],
            "relations": [
                {"from": "Component:user", "type": "dependsOn", "to": "Component:user-db", "evidence": [], "properties": {}, "confidence": "high"},
                {"from": "Component:user-db", "type": "partOf", "to": "System:demo", "evidence": [], "properties": {}, "confidence": "medium"},
            ],
        }

        demoted = demote_infrastructure_components(payload)
        refs = {entity_ref(e) for e in payload["entities"]}
        edges = {(r["from"], r["type"], r["to"]) for r in payload["relations"]}

        self.assertEqual(demoted, 1)
        self.assertNotIn("Component:user-db", refs)
        self.assertIn("Resource:users", refs)
        self.assertIn(("Component:user", "dependsOn", "Resource:users"), edges)

    def test_static_openapi_operations_enrich_matching_api(self) -> None:
        payload = {
            "repo": {"id": "acme/user"},
            "apis": [
                {
                    "name": "User-API",
                    "type": "rest",
                    "operations": [{"method": "GET", "path": "/customers", "name": "List Customers"}],
                    "evidence": [],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec_dir = root / "apispec"
            spec_dir.mkdir()
            (spec_dir / "user.json").write_text(
                json.dumps({
                    "swagger": "2.0",
                    "info": {"title": "User"},
                    "paths": {
                        "/customers": {"get": {"operationId": "Get customers"}},
                        "/cards/{id}": {"get": {"operationId": "Get Card"}},
                        "/addresses/{id}": {"get": {"operationId": "Get Address"}},
                    },
                }),
                encoding="utf-8",
            )
            summary = merge_static_api_operations(payload, root)

        paths = {op["path"] for op in payload["apis"][0]["operations"]}
        self.assertEqual(summary["merged_apis"], 1)
        self.assertEqual(summary["operations_added"], 2)
        self.assertIn("/cards/{id}", paths)
        self.assertIn("/addresses/{id}", paths)

    def test_relation_evidence_is_not_mutated_when_entity_merges(self) -> None:
        first = _repo_payload()
        second = deepcopy(_repo_payload())
        second["repo"]["id"] = "acme/payments"
        second["components"][0]["name"] = "payments"
        second["providers"][0]["evidence"] = [
            {"path": "src/payments.py", "line": 8, "snippet": "SENTRY_DSN"}
        ]

        catalog = Catalog()
        add_repo(catalog, first)
        add_repo(catalog, second)
        relations = {
            (relation["from"], relation["to"]): relation
            for relation in catalog.to_json()["relations"]
            if relation["type"] == "dependsOn" and relation["to"] == "Provider:Sentry"
        }

        self.assertEqual(len(relations[("Component:orders", "Provider:Sentry")]["evidence"]), 1)
        self.assertEqual(len(relations[("Component:payments", "Provider:Sentry")]["evidence"]), 1)


if __name__ == "__main__":
    unittest.main()
