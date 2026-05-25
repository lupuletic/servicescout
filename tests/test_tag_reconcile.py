import unittest
import tempfile
from pathlib import Path
from unittest import mock

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

    def test_inventory_fingerprint_is_stable_and_detects_changes(self) -> None:
        inventory = [
            {"tag": "Java", "normalised": "java", "count": 2, "entity_kinds": ["Component"]},
            {"tag": "SQL Server", "normalised": "sql-server", "count": 1, "entity_kinds": ["Resource"]},
        ]
        same_inventory = list(reversed(inventory))
        changed_inventory = [
            {"tag": "Java", "normalised": "java", "count": 3, "entity_kinds": ["Component"]},
            {"tag": "SQL Server", "normalised": "sql-server", "count": 1, "entity_kinds": ["Resource"]},
        ]

        self.assertEqual(
            tag_reconcile.tag_inventory_fingerprint(inventory),
            tag_reconcile.tag_inventory_fingerprint(same_inventory),
        )
        self.assertNotEqual(
            tag_reconcile.tag_inventory_fingerprint(inventory),
            tag_reconcile.tag_inventory_fingerprint(changed_inventory),
        )

    def test_current_alias_document_checks_inventory_and_options(self) -> None:
        inventory = [{"tag": "Java", "normalised": "java", "count": 1, "entity_kinds": ["Component"]}]
        fingerprint = tag_reconcile.tag_inventory_fingerprint(inventory)
        document = tag_reconcile.build_alias_document(
            inventory,
            [],
            provider=None,
            model=None,
            min_confidence="medium",
            catalog_path=Path("catalog.json"),
            inventory_fingerprint=fingerprint,
            llm_assist=False,
            max_tags=500,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tag_aliases.json"
            tag_reconcile.write_json(path, document)
            self.assertTrue(tag_reconcile.is_current_alias_document(
                path,
                inventory_fingerprint=fingerprint,
                provider=None,
                model=None,
                min_confidence="medium",
                llm_assist=False,
                max_tags=500,
            ))
            self.assertFalse(tag_reconcile.is_current_alias_document(
                path,
                inventory_fingerprint=fingerprint,
                provider=None,
                model=None,
                min_confidence="high",
                llm_assist=False,
                max_tags=500,
            ))

    def test_codex_tag_call_surfaces_cli_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("servicescout.tag_reconcile.shutil.which", return_value="codex"), \
             mock.patch("servicescout.tag_reconcile._codex_headless_flags", return_value=[]), \
             mock.patch("servicescout.tag_reconcile.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=1, stderr="not authenticated", stdout="")

            with self.assertRaises(SystemExit) as ctx:
                tag_reconcile._codex_tag_call("prompt", "gpt-test", Path(tmp))

        self.assertIn("not authenticated", str(ctx.exception))

    def test_load_tag_aliases_supports_raw_and_normalised_keys(self) -> None:
        with self.subTest("raw key"):
            aliases = {"MSSQL": "sql-server"}
            self.assertEqual(canonical_tags(["MSSQL"], aliases), ["sql-server"])

        with self.subTest("file"):
            import json

            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "tag_aliases.json"
                path.write_text(json.dumps({"aliases": {"MSSQL": "sql-server"}}), encoding="utf-8")
                aliases = load_tag_aliases(path)
                self.assertEqual(canonical_tags(["mssql"], aliases), ["sql-server"])


if __name__ == "__main__":
    unittest.main()
