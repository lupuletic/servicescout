import json
import tempfile
import unittest
from pathlib import Path

from servicescout.repo_discovery import find_repos, load_workspace_config


class RepoUnitDiscoveryTests(unittest.TestCase):
    def test_loads_explicit_repo_units_from_workspace_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "workspace.json"
            config.write_text(
                json.dumps(
                    {
                        "orgs": ["ExampleOrg"],
                        "repo_units": [
                            {
                                "id": "ExampleOrg/mono/orders",
                                "name": "orders",
                                "path": "mono",
                                "focus_path": "services/orders",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            loaded = load_workspace_config(config)

        self.assertEqual(loaded["orgs"], ["ExampleOrg"])
        self.assertEqual(loaded["repo_units"][0]["id"], "ExampleOrg/mono/orders")
        self.assertEqual(loaded["repo_units"][0]["focus_path"], "services/orders")

    def test_find_repos_uses_explicit_units_instead_of_whole_monorepo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mono" / "services" / "orders").mkdir(parents=True)
            (root / "mono" / ".git").mkdir()

            repos = find_repos(
                root,
                orgs=["ExampleOrg"],
                repo_units=[
                    {
                        "id": "ExampleOrg/mono/orders",
                        "name": "orders",
                        "path": "mono",
                        "focus_path": "services/orders",
                    }
                ],
            )

        self.assertEqual(len(repos), 1)
        self.assertEqual(repos[0]["id"], "ExampleOrg/mono/orders")
        self.assertEqual(repos[0]["name"], "orders")
        self.assertEqual(repos[0]["absolute_path"], str((root / "mono").resolve()))
        self.assertEqual(repos[0]["focus_path"], "services/orders")


if __name__ == "__main__":
    unittest.main()
