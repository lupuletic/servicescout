"""Tests for export_claude_md.py — per-system AGENTS.md / CLAUDE.md emitter."""

import json
import tempfile
import unittest
from pathlib import Path

from servicescout import export_claude_md


def _entity(kind: str, name: str, *, system: str = "", **extra):
    return {
        "kind": kind,
        "metadata": {"name": name, "description": "", "annotations": extra.pop("annotations", {})},
        "spec": {"system": system, **extra},
        "evidence": [],
        "confidence": "high",
    }


def _relation(src: str, tgt: str, type_: str):
    return {"from": src, "to": tgt, "type": type_}


def _catalog() -> dict:
    return {
        "entities": [
            _entity("Component", "orders", system="sock-shop",
                    annotations={"tagline": "Order service",
                                 "source_repos": ["acme/orders"]},
                    runtime="long-running", lifecycle="production"),
            _entity("Component", "shipping", system="sock-shop",
                    annotations={"tagline": "Shipping service",
                                 "source_repos": ["acme/shipping"]},
                    runtime="long-running", lifecycle="production"),
            _entity("Component", "billing", system="finance",
                    runtime="long-running", lifecycle="production"),
            _entity("API", "Orders-REST-API", type="rest"),
            _entity("Resource", "shipping-task", type="queue", technology="RabbitMQ"),
        ],
        "relations": [
            _relation("Component:orders", "API:Orders-REST-API", "providesApi"),
            _relation("Component:orders", "Resource:shipping-task", "producesMessage"),
            _relation("Component:shipping", "Resource:shipping-task", "consumesMessage"),
            _relation("Component:billing", "Component:orders", "consumesApi"),
            _relation("Component:orders", "Component:billing", "consumesApi"),
        ],
    }


class RenderTests(unittest.TestCase):
    def test_writes_one_md_per_system(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = export_claude_md.write_per_system(_catalog(), Path(tmp))
            self.assertEqual(len(paths), 2)
            stems = sorted(p.stem for p in paths)
            self.assertEqual(stems, ["finance", "sock-shop"])

    def test_brief_contains_components_apis_resources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = export_claude_md.write_per_system(_catalog(), Path(tmp))
            text = next(p.read_text() for p in paths if p.stem == "sock-shop")
            self.assertIn("orders", text)
            self.assertIn("shipping", text)
            self.assertIn("Orders-REST-API", text)
            self.assertIn("shipping-task", text)
            self.assertIn("acme/orders", text)

    def test_internal_vs_outbound_vs_inbound_split(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = export_claude_md.write_per_system(_catalog(), Path(tmp))
            text = next(p.read_text() for p in paths if p.stem == "sock-shop")
            # orders → API:Orders-REST-API → both in sock-shop → internal
            internal_section = text.split("## Internal dependencies")[1].split("## Outbound")[0]
            self.assertIn("`Component:orders` →`providesApi`→ `API:Orders-REST-API`", internal_section)
            # orders → Component:billing (billing is in finance) → outbound
            outbound_section = text.split("## Outbound dependencies")[1].split("## Inbound")[0]
            self.assertIn("Component:orders` →`consumesApi`→ `Component:billing", outbound_section)
            # billing → orders → inbound
            inbound_section = text.split("## Inbound dependencies")[1].split("## Asking")[0]
            self.assertIn("Component:billing` →`consumesApi`→ `Component:orders", inbound_section)

    def test_filter_by_system(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = export_claude_md.write_per_system(
                _catalog(), Path(tmp), only_system="finance"
            )
            self.assertEqual(len(paths), 1)
            self.assertEqual(paths[0].stem, "finance")

    def test_mentions_mcp_tools(self) -> None:
        # The brief should point the agent at the MCP tools so it knows
        # where to follow up. Check all 7 are referenced.
        with tempfile.TemporaryDirectory() as tmp:
            paths = export_claude_md.write_per_system(_catalog(), Path(tmp))
            text = paths[0].read_text()
            for tool in [
                "servicescout_search",
                "servicescout_describe",
                "servicescout_neighbors",
                "servicescout_trace",
                "servicescout_evidence",
                "servicescout_glossary",
                "servicescout_owners",
            ]:
                self.assertIn(tool, text, f"missing reference to {tool}")

    def test_empty_catalog_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = export_claude_md.write_per_system(
                {"entities": [], "relations": []}, Path(tmp)
            )
            self.assertEqual(paths, [])

    def test_slugifies_system_names(self) -> None:
        self.assertEqual(export_claude_md._slug("Sock Shop"), "sock-shop")
        self.assertEqual(export_claude_md._slug("e-commerce / orders"), "e-commerce-orders")
        self.assertEqual(export_claude_md._slug("My System: V2"), "my-system-v2")


if __name__ == "__main__":
    unittest.main()
