"""Crawler orchestration tests."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from servicescout import crawler
from servicescout.repo_discovery import load_workspace_config, write_workspace_config


class JourneySeedTests(unittest.TestCase):
    """Phase 1: explicit journey seeds get cloned before discovery; scope sets
    crawl bounds in the workspace config itself."""

    def test_config_parses_seeds_and_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "workspace.json"
            cfg.write_text(json.dumps({
                "orgs": ["acme"],
                "seeds": ["acme/storefront-web", " acme/mobile-app ", ""],
                "scope": {"discover": True, "max_discovery_rounds": 3},
            }), encoding="utf-8")
            ws = load_workspace_config(cfg)
            self.assertEqual(ws["seeds"], ["acme/storefront-web", "acme/mobile-app"])  # trimmed, empties dropped
            self.assertEqual(ws["scope"], {"discover": True, "max_discovery_rounds": 3})

    def test_config_defaults_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "workspace.json"
            cfg.write_text(json.dumps({"orgs": ["acme"]}), encoding="utf-8")
            ws = load_workspace_config(cfg)
            self.assertEqual(ws["seeds"], [])
            self.assertEqual(ws["scope"], {})

    def test_clone_seeds_clones_each_valid_seed(self) -> None:
        calls = []

        def fake_clone(full, root, **kw):
            calls.append(full)
            return True, ""

        with mock.patch("servicescout.crawler.clone_repo", side_effect=fake_clone):
            results = crawler.clone_seeds(["acme/web", "acme/api"], Path("/ws"))
        self.assertEqual(calls, ["acme/web", "acme/api"])
        self.assertTrue(all(r["cloned"] for r in results))

    def test_clone_seeds_skips_invalid_and_tolerates_existing(self) -> None:
        def fake_clone(full, root, **kw):
            return (False, "already_exists")

        with mock.patch("servicescout.crawler.clone_repo", side_effect=fake_clone) as m:
            results = crawler.clone_seeds(["no-slash", "acme/web"], Path("/ws"))
        # invalid seed never reaches clone_repo
        m.assert_called_once_with("acme/web", Path("/ws"))
        self.assertEqual(results[0], {"seed": "no-slash", "cloned": False, "reason": "invalid"})
        self.assertFalse(results[1]["cloned"])  # already_exists is not a clone, not an error

    def test_write_workspace_config_roundtrips_and_preserves_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "workspace.json"
            # Pre-existing config with a field the UI doesn't manage.
            cfg.write_text(json.dumps({
                "orgs": ["old"],
                "repo_units": [{"id": "acme/mono/svc", "path": "mono", "focus_path": "src/svc"}],
            }), encoding="utf-8")
            written = write_workspace_config(
                cfg, orgs=["acme"], seeds=["acme/web"], scope={"discover": True, "max_discovery_rounds": 5},
            )
            self.assertEqual(written["orgs"], ["acme"])
            self.assertEqual(written["seeds"], ["acme/web"])
            reloaded = load_workspace_config(cfg)
            self.assertEqual(reloaded["scope"], {"discover": True, "max_discovery_rounds": 5})
            # repo_units the UI didn't touch must survive.
            self.assertEqual(len(reloaded["repo_units"]), 1)


class RunExtractorRootTests(unittest.TestCase):
    """run_extractor must resolve against the workspace root, not the focus-path
    parent — otherwise monorepo repo_units (path relative to the workspace) never
    resolve and every unit fails instantly."""

    def _run(self, repo, root):
        captured = {}

        class FakeProcess:
            pid = 12345
            returncode = 0

            def __init__(self, output: str) -> None:
                self.stdout = io.StringIO(output)

            def poll(self) -> int:
                return 0

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            return FakeProcess('EXTRACTOR_RESULT {"status": "ok"}\n')

        with mock.patch("servicescout.crawler.subprocess.Popen", side_effect=fake_popen):
            result = crawler.run_extractor(
                repo,
                root=root,
                provider="codex",
                model=None,
                effort="medium",
                timeout_seconds=600,
                catalog_dir=Path("/data/catalog"),
                workspace_path=Path("/app/ws.json"),
                stream_logs=False,
            )
        return captured["cmd"], result

    def test_monorepo_unit_uses_workspace_root(self) -> None:
        repo = {
            "name": "frontend",
            "id": "org/mono/frontend",
            "absolute_path": "/workspace/mono/src/frontend",
        }
        cmd, result = self._run(repo, Path("/workspace"))
        root_arg = cmd[cmd.index("--root") + 1]
        self.assertEqual(root_arg, "/workspace")  # not /workspace/mono/src
        self.assertEqual(result["status"], "ok")

    def test_normal_repo_also_uses_workspace_root(self) -> None:
        repo = {"name": "carts", "id": "org/carts", "absolute_path": "/workspace/carts"}
        cmd, _ = self._run(repo, Path("/workspace"))
        self.assertEqual(cmd[cmd.index("--root") + 1], "/workspace")


class RuntimeConfigTests(unittest.TestCase):
    def test_missing_runtime_config_uses_cli_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = crawler.read_runtime_config(
                Path(tmp) / "missing.json",
                default_parallelism=8,
                default_batch_size=12,
            )
        self.assertEqual(config["parallelism"], 8)
        self.assertEqual(config["batch_size"], 12)
        self.assertEqual(config["source"], "cli")

    def test_runtime_config_can_raise_limits_between_batches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.json"
            path.write_text(json.dumps({"parallelism": 12, "batch_size": 48}), encoding="utf-8")
            config = crawler.read_runtime_config(
                path,
                default_parallelism=8,
                default_batch_size=12,
            )
        self.assertEqual(config["parallelism"], 12)
        self.assertEqual(config["batch_size"], 48)
        self.assertEqual(config["source"], "file")

    def test_batch_size_never_drops_below_parallelism(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.json"
            path.write_text(json.dumps({"parallelism": 16, "batch_size": 4}), encoding="utf-8")
            config = crawler.read_runtime_config(
                path,
                default_parallelism=8,
                default_batch_size=12,
            )
        self.assertEqual(config["parallelism"], 16)
        self.assertEqual(config["batch_size"], 16)


if __name__ == "__main__":
    unittest.main()
