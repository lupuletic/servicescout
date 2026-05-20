"""Tests for the bi-directional Backstage YAML reconcile."""

import tempfile
import unittest
from pathlib import Path

from servicescout.static_extractors import backstage_reconcile


SAMPLE_YAML = """\
apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: orders
  description: Order processing service.
spec:
  type: service
  lifecycle: production
  owner: group:default/team-orders
  system: sock-shop
"""


def _payload(components: list[dict]) -> dict:
    return {
        "repo": {"id": "acme/orders"},
        "components": components,
        "apis": [],
        "resources": [],
        "providers": [],
        "domain_attributes": [],
        "glossary": [],
        "dependencies": [],
    }


class FindYamlTests(unittest.TestCase):
    def test_finds_catalog_info_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "catalog-info.yaml").write_text(SAMPLE_YAML, encoding="utf-8")
            found = backstage_reconcile.find_backstage_yaml(Path(tmp))
            self.assertIsNotNone(found)
            self.assertEqual(found.name, "catalog-info.yaml")

    def test_finds_yml_variant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "catalog-info.yml").write_text(SAMPLE_YAML, encoding="utf-8")
            found = backstage_reconcile.find_backstage_yaml(Path(tmp))
            self.assertIsNotNone(found)

    def test_no_yaml_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(backstage_reconcile.find_backstage_yaml(Path(tmp)))


class LoadDocumentsTests(unittest.TestCase):
    def test_loads_single_doc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog-info.yaml"
            path.write_text(SAMPLE_YAML, encoding="utf-8")
            docs = backstage_reconcile.load_backstage_documents(path)
            self.assertEqual(len(docs), 1)
            self.assertEqual(docs[0]["kind"], "Component")

    def test_filters_non_backstage_docs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog-info.yaml"
            path.write_text(
                "apiVersion: v1\nkind: Service\nmetadata: {name: x}\n---\n" + SAMPLE_YAML,
                encoding="utf-8",
            )
            docs = backstage_reconcile.load_backstage_documents(path)
            # k8s Service filtered out; only Backstage doc remains.
            self.assertEqual(len(docs), 1)
            self.assertEqual(docs[0]["kind"], "Component")

    def test_corrupt_yaml_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog-info.yaml"
            path.write_text("not: valid: yaml: [", encoding="utf-8")
            self.assertEqual(backstage_reconcile.load_backstage_documents(path), [])


class ReconcileComponentTests(unittest.TestCase):
    def test_agreement_marks_state_agreed(self) -> None:
        llm = {"name": "orders", "type": "service", "lifecycle": "production",
               "owner": "team-orders", "system": "sock-shop", "confidence": "high"}
        yaml_doc = {
            "kind": "Component",
            "metadata": {"name": "orders"},
            "spec": {"type": "service", "lifecycle": "production",
                     "owner": "group:default/team-orders", "system": "sock-shop"},
        }
        summary = backstage_reconcile.reconcile_component(llm, yaml_doc)
        self.assertEqual(summary["disagreement"], [])
        self.assertEqual(llm["_backstage_reconcile"]["state"], "agreed")
        self.assertEqual(llm["confidence"], "high")

    def test_disagreement_demotes_to_review(self) -> None:
        llm = {"name": "orders", "type": "service", "lifecycle": "production",
               "owner": "team-orders", "confidence": "high"}
        yaml_doc = {
            "kind": "Component",
            "metadata": {"name": "orders"},
            "spec": {"type": "service", "lifecycle": "experimental",
                     "owner": "team-orders"},
        }
        backstage_reconcile.reconcile_component(llm, yaml_doc)
        self.assertEqual(llm["confidence"], "review")
        annotations = llm["_backstage_reconcile"]
        self.assertEqual(annotations["state"], "disagreement")
        fields = [d["field"] for d in annotations["disagreement"]]
        self.assertIn("lifecycle", fields)

    def test_yaml_fills_missing_llm_fields(self) -> None:
        llm = {"name": "orders", "type": "service", "confidence": "medium"}
        yaml_doc = {
            "kind": "Component",
            "metadata": {"name": "orders", "description": "Orders service."},
            "spec": {"lifecycle": "production", "owner": "team-orders"},
        }
        backstage_reconcile.reconcile_component(llm, yaml_doc)
        self.assertEqual(llm["lifecycle"], "production")
        self.assertEqual(llm["owner"], "team-orders")
        self.assertEqual(llm["notes"], "Orders service.")
        self.assertEqual(llm["_backstage_reconcile"]["state"], "enriched_from_yaml")
        self.assertIn("lifecycle", llm["_backstage_reconcile"]["added_from_yaml"])

    def test_owner_namespacing_is_normalised(self) -> None:
        # Backstage stores `group:default/team-a`; LLM writes `team-a`.
        # Should NOT count as disagreement.
        llm = {"name": "x", "type": "service", "owner": "team-orders", "confidence": "high"}
        yaml_doc = {"kind": "Component", "metadata": {"name": "x"},
                    "spec": {"type": "service", "owner": "group:default/team-orders"}}
        backstage_reconcile.reconcile_component(llm, yaml_doc)
        self.assertEqual(llm["_backstage_reconcile"]["state"], "agreed")

    def test_skip_non_component_yaml(self) -> None:
        llm = {"name": "x", "type": "service"}
        yaml_doc = {"kind": "System", "metadata": {"name": "x"}, "spec": {}}
        summary = backstage_reconcile.reconcile_component(llm, yaml_doc)
        self.assertEqual(summary["added_from_yaml"], [])
        self.assertEqual(summary["disagreement"], [])


class ReconcilePayloadTests(unittest.TestCase):
    def test_no_yaml_in_repo_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = backstage_reconcile.reconcile_payload(
                _payload([{"name": "orders", "type": "service"}]),
                Path(tmp),
            )
            self.assertFalse(result["yaml_found"])

    def test_yaml_present_merges_component(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "catalog-info.yaml").write_text(SAMPLE_YAML, encoding="utf-8")
            payload = _payload([{"name": "orders", "type": "service"}])
            result = backstage_reconcile.reconcile_payload(payload, Path(tmp))
            self.assertTrue(result["yaml_found"])
            self.assertEqual(result["components_in_yaml"], 1)
            self.assertEqual(len(result["merges"]), 1)
            # LLM component was enriched with Backstage YAML data.
            self.assertEqual(payload["components"][0]["lifecycle"], "production")
            self.assertEqual(payload["components"][0]["owner"], "team-orders")


if __name__ == "__main__":
    unittest.main()
