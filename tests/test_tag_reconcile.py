import unittest
from pathlib import Path

from servicescout import tag_reconcile
from servicescout.tags import canonical_tags, load_tag_aliases, normalize_tag


class TagReconcileTests(unittest.TestCase):
    def test_normalize_tag_only_handles_spelling_drift(self) -> None:
        self.assertEqual(normalize_tag("Java"), "java")
        self.assertEqual(normalize_tag("spring boot"), "spring-boot")
        self.assertEqual(normalize_tag("SQL_Server"), "sql-server")
        self.assertEqual(normalize_tag("schedule:0 2 * * *"), "schedule:0 2 * * *")

    def test_build_alias_document_applies_llm_semantic_groups(self) -> None:
        catalog = {
            "entities": [
                {"kind": "Component", "metadata": {"name": "a", "tags": ["Java"]}},
                {"kind": "Component", "metadata": {"name": "b", "tags": ["java"]}},
                {"kind": "Resource", "metadata": {"name": "c", "tags": ["MSSQL"]}},
                {"kind": "Resource", "metadata": {"name": "d", "tags": ["SQL Server"]}},
                {"kind": "Component", "metadata": {"name": "e", "tags": ["spring-boot"]}},
            ]
        }
        inventory = tag_reconcile.collect_tag_inventory(catalog)

        document = tag_reconcile.build_alias_document(
            inventory,
            [
                {
                    "canonical": "sql-server",
                    "aliases": ["MSSQL", "SQL Server"],
                    "confidence": "high",
                    "reason": "Both name the same database product.",
                },
                {
                    "canonical": "java",
                    "aliases": ["Java", "spring-boot"],
                    "confidence": "low",
                    "reason": "Too broad and below threshold.",
                },
            ],
            provider="codex",
            model="test-model",
            min_confidence="medium",
            catalog_path=Path("catalog.json"),
        )

        self.assertEqual(document["aliases"]["Java"], "java")
        self.assertEqual(document["aliases"]["MSSQL"], "sql-server")
        self.assertEqual(document["aliases"]["SQL Server"], "sql-server")
        self.assertEqual(document["aliases"]["spring-boot"], "spring-boot")
        llm_groups = [group for group in document["groups"] if group["source"] == "llm"]
        self.assertEqual(len(llm_groups), 1)
        self.assertEqual(llm_groups[0]["canonical"], "sql-server")

    def test_load_tag_aliases_supports_raw_and_normalised_keys(self) -> None:
        with self.subTest("raw key"):
            aliases = {"MSSQL": "sql-server"}
            self.assertEqual(canonical_tags(["MSSQL"], aliases), ["sql-server"])

        with self.subTest("file"):
            import json
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "tag_aliases.json"
                path.write_text(json.dumps({"aliases": {"MSSQL": "sql-server"}}), encoding="utf-8")
                aliases = load_tag_aliases(path)
                self.assertEqual(canonical_tags(["mssql"], aliases), ["sql-server"])


if __name__ == "__main__":
    unittest.main()
