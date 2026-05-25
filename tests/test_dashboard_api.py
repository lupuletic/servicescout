import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from servicescout import dashboard


def _write_catalog(root: Path) -> Path:
    catalog_path = root / "catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "summary": {"entities": 2, "relations": 2, "repos_indexed": 1},
                "entities": [
                    {
                        "kind": "Component",
                        "metadata": {"name": "orders", "annotations": {"source_repos": ["acme/orders"]}},
                        "spec": {"owner": "platform"},
                        "confidence": "high",
                    },
                    {
                        "kind": "Component",
                        "metadata": {"name": "legacy", "annotations": {"source_repos": ["acme/legacy"]}},
                        "spec": {"owner": "platform"},
                        "confidence": "review",
                    },
                ],
                "relations": [
                    {"from": "Component:orders", "to": "Component:legacy", "type": "dependsOn", "confidence": "high"},
                    {"from": "Component:legacy", "to": "Component:orders", "type": "dependsOn", "confidence": "low"},
                ],
            }
        ),
        encoding="utf-8",
    )
    repo_dir = root / "catalog"
    repo_dir.mkdir()
    (repo_dir / "orders.json").write_text(
        json.dumps(
            {
                "_meta": {
                    "extracted_at": "2026-05-18T10:00:00+00:00",
                    "provider": "codex",
                    "model": "gpt-5.4-mini",
                    "run": {
                        "status": "ok",
                        "duration_seconds": 12.5,
                        "cost": {"estimated_usd": 0.42},
                    },
                    "validation_errors": [],
                    "evidence_quarantined": 1,
                },
                "repo": {"id": "acme/orders"},
                "components": [],
                "apis": [],
                "resources": [],
                "dependencies": [
                    {
                        "source": "orders",
                        "target": "payments",
                        "kind": "consumesApi",
                        "confidence": "review",
                        "evidence": [
                            {"path": "src/orders.py", "line": 12, "snippet": "payments.charge(order)"}
                        ],
                        "_cross_check": {
                            "verdict": "disconfirmed",
                            "evidence_total": 1,
                            "evidence_matched": 0,
                            "evidence_partial": 0,
                            "evidence_missing": 1,
                            "evidence_invalid_path": 0,
                        },
                        "_cross_check_ast": {"verdict": "unsupported"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return catalog_path


class DashboardApiTests(unittest.TestCase):
    def test_confidence_filters_entities_and_graph(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = dashboard.create_app(
                catalog_path=_write_catalog(root),
                extraction_log=root / "extractions.jsonl",
                decisions_path=root / "decisions.jsonl",
            )
            client = TestClient(app)

            entities = client.get("/api/entities", params={"confidence": "high"}).json()
            self.assertEqual([e["ref"] for e in entities["entities"]], ["Component:orders"])

            graph = client.get(
                "/api/graph",
                params=[("kind", "Component"), ("edge_type", "dependsOn"), ("confidence", "high")],
            ).json()
            self.assertEqual(graph["edge_total"], 1)
            self.assertEqual(graph["edges"][0]["confidence"], "high")

    def test_graph_limit_keeps_selected_kinds_represented(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog_path = root / "catalog.json"
            catalog_path.write_text(
                json.dumps(
                    {
                        "summary": {"entities": 10, "relations": 0, "repos_indexed": 1},
                        "entities": [
                            *[
                                {
                                    "kind": "API",
                                    "metadata": {"name": f"api-{idx}", "annotations": {}},
                                    "spec": {},
                                    "confidence": "high",
                                }
                                for idx in range(8)
                            ],
                            {
                                "kind": "Provider",
                                "metadata": {"name": "stripe", "annotations": {}},
                                "spec": {},
                                "confidence": "high",
                            },
                            {
                                "kind": "Domain",
                                "metadata": {"name": "checkout", "annotations": {}},
                                "spec": {},
                                "confidence": "high",
                            },
                        ],
                        "relations": [],
                    }
                ),
                encoding="utf-8",
            )
            app = dashboard.create_app(
                catalog_path=catalog_path,
                extraction_log=root / "extractions.jsonl",
                decisions_path=root / "decisions.jsonl",
            )
            client = TestClient(app)

            graph = client.get(
                "/api/graph",
                params=[
                    ("kind", "API"),
                    ("kind", "Provider"),
                    ("kind", "Domain"),
                    ("include_orphans", "true"),
                    ("limit", "5"),
                ],
            ).json()

            kinds = {node["kind"] for node in graph["nodes"]}
            self.assertEqual(len(graph["nodes"]), 5)
            self.assertEqual(graph["node_total"], 10)
            self.assertIn("API", kinds)
            self.assertIn("Provider", kinds)
            self.assertIn("Domain", kinds)

            full_graph = client.get(
                "/api/graph",
                params=[
                    ("kind", "API"),
                    ("kind", "Provider"),
                    ("kind", "Domain"),
                    ("include_orphans", "true"),
                    ("limit", "0"),
                ],
            ).json()
            self.assertEqual(len(full_graph["nodes"]), 10)
            self.assertFalse(full_graph["truncated"])

    def test_operator_summary_reads_repo_run_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = dashboard.create_app(
                catalog_path=_write_catalog(root),
                extraction_log=root / "extractions.jsonl",
                decisions_path=root / "decisions.jsonl",
            )
            client = TestClient(app)

            payload = client.get("/api/operator/summary").json()
            self.assertEqual(payload["catalog"]["repos_indexed"], 1)
            self.assertEqual(payload["cost_trend"][0]["cost"], 0.42)
            self.assertEqual(payload["verifier"]["evidence_quarantined"], 1)
            self.assertEqual(payload["verifier"]["entity_confidence"]["review"], 1)

    def test_operator_summary_returns_full_stale_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog_path = _write_catalog(root)
            repo_dir = root / "catalog"
            for index in range(45):
                (repo_dir / f"stale-{index}.json").write_text(
                    json.dumps(
                        {
                            "_meta": {
                                "extracted_at": "2026-05-01T10:00:00+00:00",
                                "run": {
                                    "status": "ok",
                                    "duration_seconds": 1,
                                    "cost": {"estimated_usd": 0.01},
                                },
                            },
                            "repo": {"id": f"acme/stale-{index}"},
                        }
                    ),
                    encoding="utf-8",
                )
            app = dashboard.create_app(
                catalog_path=catalog_path,
                extraction_log=root / "extractions.jsonl",
                decisions_path=root / "decisions.jsonl",
            )
            client = TestClient(app)

            stale = client.get("/api/operator/summary").json()["staleness"]
            self.assertGreater(len(stale["repos"]), 40)
            self.assertEqual(stale["repo_total"], len(stale["repos"]))

    def test_activity_falls_back_to_extraction_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = dashboard.create_app(
                catalog_path=_write_catalog(root),
                extraction_log=root / "extractions.jsonl",
                decisions_path=root / "decisions.jsonl",
            )
            client = TestClient(app)

            runs = client.get("/api/crawl/runs").json()
            self.assertEqual(runs["source"], "extractions")
            self.assertEqual(runs["total"], 1)
            self.assertEqual(runs["runs"][0]["run_id"], "extraction-orders")
            self.assertEqual(runs["runs"][0]["trigger"], "extraction")
            self.assertEqual(runs["runs"][0]["cost_usd"], 0.42)

            status = client.get("/api/crawl/status").json()
            self.assertEqual(status["last_run"]["run_id"], "extraction-orders")

            detail = client.get("/api/crawl/runs/extraction-orders").json()
            self.assertEqual(detail["trigger"], "extraction")
            self.assertEqual(detail["repos_changed"][0]["repo"], "orders")
            self.assertEqual(detail["events"][0]["event"], "repo_extracted")

    def test_activity_run_summaries_include_run_cost_and_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "crawl_runs"
            run_dir.mkdir()
            (run_dir / "20260522T100000Z-test.json").write_text(
                json.dumps({
                    "run_id": "20260522T100000Z-test",
                    "trigger": "cli",
                    "status": "running",
                    "started_at": "2026-05-22T10:00:00+00:00",
                    "pid": 999999,
                    "repos_checked": 8,
                    "repos_changed": [{"repo": "acme/orders", "status": "ok"}],
                    "budget_usd": 25.0,
                    "cost_usd": 1.25,
                    "catalog_cost_usd": 12.5,
                }),
                encoding="utf-8",
            )
            app = dashboard.create_app(
                catalog_path=_write_catalog(root),
                extraction_log=root / "extractions.jsonl",
                decisions_path=root / "decisions.jsonl",
            )
            client = TestClient(app)

            with mock.patch("servicescout.dashboard.pid_alive", return_value=False):
                run = client.get("/api/crawl/runs").json()["runs"][0]
                status = client.get("/api/crawl/status").json()
                detail = client.get("/api/crawl/runs/20260522T100000Z-test").json()

            self.assertEqual(run["status"], "abandoned")
            self.assertEqual(run["repos_checked"], 8)
            self.assertEqual(run["budget_usd"], 25.0)
            self.assertEqual(run["cost_usd"], 1.25)
            self.assertEqual(run["catalog_cost_usd"], 12.5)
            self.assertEqual(detail["status"], "abandoned")
            self.assertEqual(status["last_run"]["repos_checked"], 8)
            self.assertEqual(status["last_run"]["status"], "abandoned")
            self.assertEqual(status["last_run"]["cost_usd"], 1.25)

    def test_trigger_requires_mounted_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = dashboard.create_app(
                catalog_path=_write_catalog(root),
                extraction_log=root / "extractions.jsonl",
                decisions_path=root / "decisions.jsonl",
            )
            client = TestClient(app)

            with mock.patch.dict("os.environ", {"WORKSPACE_ROOT": str(root / "missing")}, clear=False):
                response = client.post("/api/crawl/trigger")

            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"], "workspace_root_missing")

    def test_scheduler_start_persists_settings_and_launches_managed_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            app = dashboard.create_app(
                catalog_path=_write_catalog(root),
                extraction_log=root / "extractions.jsonl",
                decisions_path=root / "decisions.jsonl",
            )
            client = TestClient(app)

            with mock.patch.dict("os.environ", {"WORKSPACE_ROOT": str(workspace)}, clear=False), \
                 mock.patch("servicescout.dashboard.scheduler_status", return_value={"running": False, "pid": None}), \
                 mock.patch("servicescout.dashboard.pid_alive", side_effect=lambda pid: pid == 1234), \
                 mock.patch("servicescout.dashboard.subprocess.run") as run, \
                 mock.patch("servicescout.dashboard.subprocess.Popen") as popen:
                run.return_value = mock.Mock(stdout="00:01\n", stderr="", returncode=0)
                popen.return_value.pid = 1234
                response = client.post(
                    "/api/crawl/scheduler/start",
                    data={"interval_minutes": "45", "budget_usd": "7.5"},
                )

            self.assertEqual(response.status_code, 202)
            payload = response.json()
            self.assertEqual(payload["scheduler"]["pid"], 1234)
            self.assertTrue(payload["scheduler"]["running"])
            self.assertEqual(payload["scheduler"]["interval_minutes"], 45)
            self.assertEqual(payload["scheduler"]["budget_usd"], 7.5)
            cmd = popen.call_args[0][0]
            self.assertIn("--interval-minutes", cmd)
            self.assertIn("45", cmd)
            self.assertIn("--budget-usd", cmd)
            self.assertIn("7.5", cmd)
            self.assertIn("--crawler-arg=--reconcile", cmd)
            self.assertIn("--crawler-arg=--build-kuzu", cmd)

    def test_scheduler_stop_only_pauses_dashboard_managed_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = dashboard.create_app(
                catalog_path=_write_catalog(root),
                extraction_log=root / "extractions.jsonl",
                decisions_path=root / "decisions.jsonl",
            )
            client = TestClient(app)
            (root / "scheduler_daemon.pid").write_text("1234", encoding="utf-8")

            with mock.patch("servicescout.dashboard.pid_alive", return_value=True), \
                 mock.patch("servicescout.dashboard.os.kill") as kill:
                response = client.post("/api/crawl/scheduler/stop")

            self.assertEqual(response.status_code, 200)
            kill.assert_called_once_with(1234, dashboard.signal.SIGTERM)
            self.assertFalse((root / "scheduler_daemon.pid").exists())

    def test_disconfirmed_fact_queue_and_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = dashboard.create_app(
                catalog_path=_write_catalog(root),
                extraction_log=root / "extractions.jsonl",
                decisions_path=root / "decisions.jsonl",
            )
            client = TestClient(app)

            payload = client.get("/api/triage/facts").json()
            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["facts"][0]["id"], "acme/orders:dependencies:0")
            self.assertEqual(payload["facts"][0]["combined_verdict"], "disconfirmed")

            response = client.post(
                "/api/triage/facts/decide",
                data={
                    "fact_id": "acme/orders:dependencies:0",
                    "action": "assign_owner",
                    "owner": "platform",
                    "due_date": "2026-05-30",
                    "reviewer": "ops@example.com",
                    "reason": "Payments owner to verify",
                },
            )
            self.assertEqual(response.status_code, 200)
            assigned = client.get("/api/triage/facts").json()
            self.assertEqual(assigned["assigned"], 1)
            self.assertEqual(assigned["facts"][0]["owner"], "platform")

            client.post(
                "/api/triage/facts/decide",
                data={
                    "fact_id": "acme/orders:dependencies:0",
                    "action": "mark_corrected",
                    "reviewer": "ops@example.com",
                },
            )
            closed = client.get("/api/triage/facts").json()
            self.assertEqual(closed["count"], 0)


class OnboardingApiTests(unittest.TestCase):
    """Phases 2-3: PAT -> orgs/repos picker, and workspace config persistence."""

    def _app(self, root: Path):
        return dashboard.create_app(
            catalog_path=_write_catalog(root),
            extraction_log=root / "extractions.jsonl",
            decisions_path=root / "decisions.jsonl",
        )

    def test_github_validate_returns_orgs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(self._app(Path(tmp)))
            with mock.patch("servicescout.dashboard.github_client.validate_token",
                            return_value={"login": "octocat", "orgs": [{"login": "acme"}]}):
                resp = client.post("/api/github/validate", json={"token": "tok"})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["orgs"], [{"login": "acme"}])

    def test_github_validate_maps_error_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(self._app(Path(tmp)))
            from servicescout.github_client import GithubError
            with mock.patch("servicescout.dashboard.github_client.validate_token",
                            side_effect=GithubError(401, "bad token")):
                resp = client.post("/api/github/validate", json={"token": "x"})
            self.assertEqual(resp.status_code, 401)
            self.assertEqual(resp.json()["error"], "github_error")

    def test_workspace_config_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / "workspace.json"
            client = TestClient(self._app(root))
            with mock.patch.dict("os.environ", {"SERVICESCOUT_WORKSPACE_CONFIG": str(cfg)}, clear=False), \
                 mock.patch("servicescout.dashboard._persist_github_token", return_value=True) as persist:
                save = client.post("/api/workspace/config", json={
                    "orgs": ["acme"],
                    "seeds": ["acme/storefront", "acme/mobile"],
                    "discover": True,
                    "max_discovery_rounds": 4,
                    "budget_usd": 25,
                    "token": "ghp_secret",
                })
                self.assertEqual(save.status_code, 200)
                body = save.json()
                self.assertTrue(body["token_stored"])
                persist.assert_called_once()  # token persisted server-side, never echoed
                self.assertNotIn("token", body)

                got = client.get("/api/workspace/config").json()
                self.assertEqual(got["orgs"], ["acme"])
                self.assertEqual(got["seeds"], ["acme/storefront", "acme/mobile"])
                self.assertEqual(got["scope"], {"discover": True, "max_discovery_rounds": 4})
                self.assertEqual(got["budget_usd"], 25)

                # Audit trail records the config change with token_stored as a
                # bool — and never the token itself.
                audit = client.get("/api/audit").json()
                self.assertGreaterEqual(audit["count"], 1)
                entry = audit["entries"][0]
                self.assertEqual(entry["action"], "workspace_config_saved")
                self.assertTrue(entry["token_stored"])
                self.assertEqual(entry["after"]["seeds"], ["acme/storefront", "acme/mobile"])
                self.assertNotIn("ghp_secret", json.dumps(entry))  # the secret VALUE is never written


if __name__ == "__main__":
    unittest.main()
