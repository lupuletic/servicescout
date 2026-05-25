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


class TagReconcileCommandTests(unittest.TestCase):
    def test_tag_reconcile_uses_llm_and_skip_unchanged_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("servicescout.crawler.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="{}")

            result = crawler.run_tag_reconcile(
                catalog_output=Path(tmp) / "catalog.json",
                provider="codex",
                model="gpt-5.4-mini",
            )

        self.assertEqual(result.returncode, 0)
        cmd = run.call_args[0][0]
        self.assertIn("servicescout.tag_reconcile", cmd)
        self.assertIn("--skip-unchanged", cmd)
        self.assertIn("--provider", cmd)
        self.assertIn("codex", cmd)
        self.assertIn("--model", cmd)
        self.assertIn("gpt-5.4-mini", cmd)
        self.assertIn(str(Path(tmp) / "tag_aliases.json"), cmd)
        self.assertIn("--min-confidence", cmd)
        self.assertIn(crawler.TAG_RECONCILE_MIN_CONFIDENCE, cmd)
        self.assertIn("--max-tags", cmd)
        self.assertIn(str(crawler.TAG_RECONCILE_MAX_TAGS), cmd)

    def test_tag_reconcile_skips_when_run_budget_is_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("servicescout.crawler.emit") as emit, \
             mock.patch("servicescout.crawler.run_tag_reconcile") as run_tags:
            root = Path(tmp)
            workspace = root / "workspace.json"
            workspace.write_text(json.dumps({"orgs": []}), encoding="utf-8")

            crawler.crawl(
                root=root,
                catalog_dir=root / "catalog",
                catalog_output=root / "catalog.json",
                workspace_path=workspace,
                seeds_dir=root / "seeds",
                state_path=root / "state.json",
                provider="codex",
                model=None,
                effort="medium",
                timeout_seconds=600,
                max_age_hours=24,
                parallelism=2,
                batch_size=4,
                runtime_config_path=None,
                max_batches=1,
                budget_usd=0,
                repos_filter=None,
                embed=False,
                build_kuzu=False,
                stream_logs=False,
                discover=False,
                max_discovery_rounds=0,
                reconcile_after_build=False,
                reconcile_llm=False,
                reconcile_tags=True,
            )

        run_tags.assert_not_called()
        events = [call.args[0] for call in emit.call_args_list]
        self.assertIn(
            {"event": "tag_reconcile_done", "returncode": None, "skipped": True, "reason": "budget_exhausted", "run_spent": 0.0, "budget": 0},
            events,
        )


class CrawlAccountingTests(unittest.TestCase):
    def test_nested_extractor_cost_is_authoritative_run_spend(self) -> None:
        self.assertEqual(
            crawler.extractor_result_cost({"run": {"cost": {"estimated_usd": 0.75}}}),
            0.75,
        )
        self.assertIsNone(crawler.extractor_result_cost({"cost": {"estimated_usd": 9.99}}))

    def test_targeted_reindex_does_not_inherit_workspace_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("servicescout.crawler.find_repos") as find_repos, \
             mock.patch("servicescout.crawler.build_org_repo_index") as build_index, \
             mock.patch("servicescout.crawler.is_stale", return_value=None), \
             mock.patch("servicescout.crawler.emit"):
            root = Path(tmp)
            workspace = root / "workspace.json"
            workspace.write_text(json.dumps({"orgs": ["acme"], "scope": {"discover": True}}), encoding="utf-8")
            find_repos.return_value = [
                {"name": "alpha", "id": "acme/alpha", "absolute_path": str(root / "alpha")},
                {"name": "beta", "id": "acme/beta", "absolute_path": str(root / "beta")},
            ]

            crawler.crawl(
                root=root,
                catalog_dir=root / "catalog",
                catalog_output=root / "catalog.json",
                workspace_path=workspace,
                seeds_dir=root / "seeds",
                state_path=root / "state.json",
                provider="codex",
                model=None,
                effort="medium",
                timeout_seconds=600,
                max_age_hours=24,
                parallelism=1,
                batch_size=1,
                runtime_config_path=None,
                max_batches=1,
                budget_usd=10,
                repos_filter=["alpha"],
                embed=False,
                build_kuzu=False,
                stream_logs=False,
                discover=False,
                max_discovery_rounds=0,
                reconcile_after_build=False,
                reconcile_llm=False,
                reconcile_tags=False,
            )

        build_index.assert_not_called()


class ActivityRunTests(unittest.TestCase):
    def test_repo_done_records_operator_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            activity = crawler.ActivityRun(
                run_log_dir=root / "runs",
                lock_path=root / "crawl.lock",
                trigger="test",
                workspace_root=root,
                workspace_path=root / "workspace.json",
                budget_usd=10.0,
            )

            activity.append({"event": "crawl_start", "workspace_repos": 3})
            activity.append({
                "event": "repo_done",
                "repo": "acme/orders",
                "status": "ok",
                "duration_seconds": 12.5,
                "returncode": 0,
                "cost_usd": 0.42,
            })
            activity.append({"event": "crawl_done", "spent": 2.0, "run_spent": 0.42})

            doc = json.loads(activity.path.read_text(encoding="utf-8"))
            self.assertEqual(doc["repos_checked"], 3)
            self.assertEqual(doc["cost_usd"], 0.42)
            self.assertEqual(doc["catalog_cost_usd"], 2.0)
            self.assertEqual(doc["repos_changed"][0]["repo"], "acme/orders")
            self.assertEqual(doc["repos_changed"][0]["duration_seconds"], 12.5)
            self.assertEqual(doc["repos_changed"][0]["cost_usd"], 0.42)


class RunSpendTests(unittest.TestCase):
    """Run spend = sum of per-repo extraction costs, read from the extractor's
    nested run.cost — not the net catalog-cost delta, which collapses for
    re-index runs because re-extracting a repo overwrites its stored cost."""

    def _run_crawl(self, *, budget_usd: float, repos_filter=None, parallelism=2, batch_size=4):
        repos = [
            {"id": "acme/a", "name": "a", "commit": "c1", "absolute_path": "/ws/a"},
            {"id": "acme/b", "name": "b", "commit": "c2", "absolute_path": "/ws/b"},
        ]

        def fake_extract(repo, **kwargs):
            return {
                "repo": repo["id"],
                "status": "ok",
                "duration_seconds": 1.0,
                "returncode": 0,
                # The real extractor nests cost here, not at result["cost"].
                "run": {"cost": {"estimated_usd": 0.5}},
            }

        captured: list[dict] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace.json"
            workspace.write_text(json.dumps({"orgs": ["acme"]}), encoding="utf-8")
            with mock.patch("servicescout.crawler.find_repos", return_value=repos), \
                 mock.patch("servicescout.crawler.is_stale", return_value="stale"), \
                 mock.patch("servicescout.crawler.run_extractor", side_effect=fake_extract), \
                 mock.patch("servicescout.crawler.build_catalog", return_value={}), \
                 mock.patch("servicescout.crawler.write_state"), \
                 mock.patch("servicescout.crawler.repo_total_cost", return_value=100.0), \
                 mock.patch("servicescout.crawler.emit", side_effect=lambda e: captured.append(e)):
                crawler.crawl(
                    root=root, catalog_dir=root / "catalog", catalog_output=root / "catalog.json",
                    workspace_path=workspace, seeds_dir=root / "seeds", state_path=root / "state.json",
                    provider="codex", model=None, effort="medium", timeout_seconds=600,
                    max_age_hours=24, parallelism=parallelism, batch_size=batch_size, runtime_config_path=None,
                    max_batches=5, budget_usd=budget_usd, repos_filter=repos_filter, embed=False,
                    build_kuzu=False, stream_logs=False, discover=False, max_discovery_rounds=0,
                    reconcile_after_build=False, reconcile_llm=False, reconcile_tags=False,
                )
        return captured

    def test_repo_done_carries_cost_and_run_spent_sums_them(self) -> None:
        captured = self._run_crawl(budget_usd=100.0)
        repo_done = [e for e in captured if e.get("event") == "repo_done"]
        self.assertEqual(len(repo_done), 2)
        self.assertTrue(all(e["cost_usd"] == 0.5 for e in repo_done))
        crawl_done = next(e for e in captured if e.get("event") == "crawl_done")
        # 2 repos x $0.5 = $1.0 actually spent, even though catalog total is flat.
        self.assertAlmostEqual(crawl_done["run_spent"], 1.0)
        self.assertAlmostEqual(crawl_done["spent"], 100.0)

    def test_budget_guard_uses_actual_spend(self) -> None:
        # One repo per batch so the guard is re-evaluated before batch 2; the
        # $0.4 budget must trip on summed spend ($0.5 after batch 1), even
        # though the (mocked) catalog total never moves.
        captured = self._run_crawl(budget_usd=0.4, parallelism=1, batch_size=1)
        self.assertTrue(any(e.get("event") == "budget_exhausted" for e in captured))


class ScopedDiscoveryTests(unittest.TestCase):
    """A targeted crawl follows only the in-scope repos' dependency tree."""

    def _write_repo(self, catalog_dir: Path, name: str, deps: list[str]) -> None:
        catalog_dir.mkdir(parents=True, exist_ok=True)
        (catalog_dir / f"{name}.json").write_text(
            json.dumps({"dependencies": [{"target": d} for d in deps], "resources": []}),
            encoding="utf-8",
        )

    def test_follows_in_scope_refs_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog_dir = Path(tmp)
            self._write_repo(catalog_dir, "x", ["Payments"])   # in scope
            self._write_repo(catalog_dir, "y", ["Shipping"])   # out of scope
            org_index = {
                crawler.canonical_key("Payments"): "acme/payments",
                crawler.canonical_key("Shipping"): "acme/shipping",
            }
            missing = {
                m["repo"]
                for m in crawler.find_missing_repos_scoped(catalog_dir, ["x"], org_index, set())
            }
            self.assertIn("acme/payments", missing)
            self.assertNotIn("acme/shipping", missing)

    def test_skips_already_cloned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog_dir = Path(tmp)
            self._write_repo(catalog_dir, "x", ["Payments"])
            org_index = {crawler.canonical_key("Payments"): "acme/payments"}
            missing = crawler.find_missing_repos_scoped(
                catalog_dir, ["x"], org_index, cloned_ids={"acme/payments"}
            )
            self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
