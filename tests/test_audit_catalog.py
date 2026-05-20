import unittest

from evals.audit_catalog import audit_catalog


class CatalogAuditTests(unittest.TestCase):
    def test_flags_resource_component_duplicate_and_bad_relation_target(self) -> None:
        catalog = {
            "entities": [
                {
                    "kind": "Component",
                    "metadata": {"name": "orders", "annotations": {"source_repos": ["acme/orders"]}},
                    "evidence": [{"path": "src/app.py", "line": 1, "snippet": "orders"}],
                },
                {
                    "kind": "Component",
                    "metadata": {"name": "orders-db", "annotations": {"external": "true"}},
                    "evidence": [],
                },
                {
                    "kind": "Resource",
                    "metadata": {"name": "orders-db", "annotations": {"source_repos": ["acme/orders"]}},
                    "evidence": [{"path": "src/db.py", "line": 1, "snippet": "orders-db"}],
                },
            ],
            "relations": [
                {
                    "from": "Component:orders",
                    "type": "readsResource",
                    "to": "Component:orders-db",
                    "evidence": [{"path": "src/db.py", "line": 1, "snippet": "orders-db"}],
                    "properties": {"source_repo": "acme/orders"},
                }
            ],
        }

        audit = audit_catalog(catalog)
        high_findings = [row for row in audit["rows"] if row["severity"] == "high"]

        self.assertTrue(any(row["scope"] == "entity" and row["fact"] == "Component:orders-db" for row in high_findings))
        self.assertTrue(any(row["scope"] == "relation" and "target-kind-mismatch" in row["recommended_fix"] for row in high_findings))

    def test_missing_api_resource_provenance_is_high_severity(self) -> None:
        catalog = {
            "entities": [
                {"kind": "API", "metadata": {"name": "Orders-API", "annotations": {}}, "evidence": []},
                {"kind": "Resource", "metadata": {"name": "orders-db", "annotations": {}}, "evidence": []},
            ],
            "relations": [],
        }

        audit = audit_catalog(catalog)
        facts = {row["fact"]: row for row in audit["rows"]}

        self.assertEqual(facts["API:Orders-API"]["severity"], "high")
        self.assertIn("missing-provenance", facts["API:Orders-API"]["recommended_fix"])
        self.assertEqual(facts["Resource:orders-db"]["severity"], "high")
        self.assertIn("missing-provenance", facts["Resource:orders-db"]["recommended_fix"])


if __name__ == "__main__":
    unittest.main()
