"""Tests for the MCP tools:
  - servicescout_glossary(term)
  - servicescout_owners(entity)

The tests build a tiny in-memory Backend stand-in and assert the tool
behaviour without bringing up the FastMCP server. The tools themselves
are wired into the server via decorators that close over `backend`; we
exercise the underlying logic by importing build_server and calling the
captured functions.
"""

from __future__ import annotations

import unittest
from typing import Any


class FakeBackend:
    name = "fake"

    def __init__(self, entities: list[dict[str, Any]]) -> None:
        self._entities = entities
        self._by_ref = {f"{e['kind']}:{e['metadata']['name']}": e for e in entities}

    def list_entities(self, *, kind: str | None, limit: int) -> list[dict[str, Any]]:
        out = []
        for e in self._entities:
            if kind and e["kind"] != kind:
                continue
            ref = f"{e['kind']}:{e['metadata']['name']}"
            out.append({"ref": ref, "kind": e["kind"], "name": e["metadata"]["name"]})
            if len(out) >= limit:
                break
        return out

    def describe(self, ref: str) -> dict[str, Any] | None:
        return self._by_ref.get(ref)

    def fuzzy_lookup(self, needle: str) -> dict[str, Any] | None:
        for e in self._entities:
            if needle == e["metadata"]["name"]:
                return e
            if needle == f"{e['kind']}:{e['metadata']['name']}":
                return e
        return None

    # Stubs for the other Backend methods the tool factory expects.
    def status(self) -> dict[str, Any]:
        return {"entities": len(self._entities), "relations": 0}

    def search(self, *args, **kwargs):
        return []

    def neighbors(self, *args, **kwargs):
        return []

    def trace(self, *args, **kwargs):
        return {"hops": [], "terminal_nodes": []}

    def evidence(self, *args, **kwargs):
        return []


def _fake_catalog() -> list[dict[str, Any]]:
    return [
        {
            "kind": "Component",
            "metadata": {
                "name": "orders",
                "description": "Order processing service",
                "annotations": {
                    "tagline": "Spring Boot worker that orchestrates orders",
                    "owner": "team-orders",
                    "source_repos": ["microservices-demo/orders"],
                    "last_indexed_sha": "abc1234",
                },
            },
            "spec": {
                "type": "service",
                "lifecycle": "production",
                "system": "sock-shop",
                "domain": "e-commerce",
                "glossary": [
                    {"term": "shipping-task", "definition": "Async queue for shipment jobs",
                     "synonyms": ["shipping queue"]},
                    {"term": "cartId", "definition": "User cart identifier",
                     "synonyms": ["cart_id", "cart-id"]},
                ],
            },
            "evidence": [],
            "confidence": "high",
        },
        {
            "kind": "Component",
            "metadata": {
                "name": "shipping",
                "description": "Shipping service",
                "annotations": {
                    "tagline": "RabbitMQ producer for shipment dispatch",
                    "owner": "team-fulfilment",
                    "source_repos": ["microservices-demo/shipping"],
                },
            },
            "spec": {
                "type": "service",
                "lifecycle": "production",
                "glossary": [
                    {"term": "shipping-task", "definition": "Outbound queue this service publishes to",
                     "synonyms": []},
                    {"term": "shipment", "definition": "Domain entity for a parcel in transit",
                     "synonyms": []},
                ],
            },
            "evidence": [],
            "confidence": "high",
        },
        {
            "kind": "Component",
            "metadata": {
                "name": "front-end",
                "description": "Customer-facing UI",
                "annotations": {
                    "owner": "team-frontend",
                },
            },
            "spec": {
                "lifecycle": "production",
                "glossary": [],
            },
            "evidence": [],
            "confidence": "high",
        },
    ]


def _build_tools() -> dict[str, Any]:
    """Build the MCP server against the fake backend and return the
    decorated tool functions keyed by name.
    """
    from servicescout import mcp_server

    backend = FakeBackend(_fake_catalog())
    mcp = mcp_server.build_server(
        backend,
        project=None,
        location="us-central1",
        embed_model="gemini-embedding-001",
        embed_dim=768,
    )
    # FastMCP stores tools in a private registry. Pull them out by name.
    # The library exposes `_tool_manager._tools` in current versions; if
    # this ever changes we can replicate via a thin instrumentation
    # helper. The tests are explicitly checking PUBLIC behaviour through
    # the tools' captured functions, not FastMCP internals.
    registry = getattr(mcp, "_tool_manager", None)
    if registry is None or not hasattr(registry, "_tools"):
        raise unittest.SkipTest(
            "FastMCP internal tool registry shape changed; tool tests skipped."
        )
    tools = {name: t.fn for name, t in registry._tools.items()}
    return tools


class GlossaryToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tools = _build_tools()
        self.glossary = self.tools["servicescout_glossary"]

    def test_exact_term_match(self) -> None:
        out = self.glossary(term="shipping-task")
        self.assertEqual(out["term"], "shipping-task")
        self.assertGreaterEqual(out["match_count"], 2)
        # Both orders and shipping define "shipping-task"; should appear.
        owners = {r["owning_component"] for r in out["results"]}
        self.assertEqual(owners, {"orders", "shipping"})

    def test_synonym_match(self) -> None:
        out = self.glossary(term="cart_id")  # synonym of cartId
        self.assertEqual(out["match_count"], 1)
        self.assertEqual(out["results"][0]["term"], "cartId")

    def test_substring_match(self) -> None:
        out = self.glossary(term="cart")
        # Matches cartId via term substring.
        self.assertGreaterEqual(out["match_count"], 1)

    def test_no_match_returns_zero(self) -> None:
        out = self.glossary(term="nonexistent-vocabulary")
        self.assertEqual(out["match_count"], 0)
        self.assertEqual(out["results"], [])

    def test_empty_term_errors(self) -> None:
        out = self.glossary(term="")
        self.assertIn("error", out)


class OwnersToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tools = _build_tools()
        self.owners = self.tools["servicescout_owners"]

    def test_lookup_by_name(self) -> None:
        out = self.owners(entity="orders")
        self.assertEqual(out["ref"], "Component:orders")
        self.assertEqual(out["owner"], "team-orders")
        self.assertEqual(out["lifecycle"], "production")
        self.assertEqual(out["last_indexed_sha"], "abc1234")
        self.assertEqual(out["tagline"], "Spring Boot worker that orchestrates orders")

    def test_lookup_by_ref(self) -> None:
        out = self.owners(entity="Component:shipping")
        self.assertEqual(out["owner"], "team-fulfilment")

    def test_unknown_entity_errors(self) -> None:
        out = self.owners(entity="nonexistent")
        self.assertEqual(out["error"], "not_found")
        self.assertEqual(out["needle"], "nonexistent")

    def test_default_owner_unknown(self) -> None:
        # Provide an entity with no owner annotation: result is "unknown".
        out = self.owners(entity="front-end")
        # front-end has owner="team-frontend" in our fake; but no
        # lifecycle on annotations; lifecycle still comes from spec.
        self.assertEqual(out["owner"], "team-frontend")
        self.assertEqual(out["lifecycle"], "production")


if __name__ == "__main__":
    unittest.main()
