"""Tests for the continuous crawl scheduler (Issue #1).

Covers:
  - Lock acquire/release semantics, including stale-lock detection
  - Last-extracted-SHA discovery from per-repo catalog JSONs
  - Run-log writing
  - Tick orchestration with the crawler subprocess mocked

git interaction (fetch / rev-parse) is not exercised here — it would
require building a real temp git repo per test. The functions are
small wrappers around subprocess + ASCII-only output so the risk of
breakage is low; manual smoke testing covers them.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from servicescout import scheduler


class LockTests(unittest.TestCase):
    def test_acquire_then_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "crawl_lock"
            self.assertTrue(scheduler.acquire_lock(lock))
            self.assertTrue(lock.exists())
            scheduler.release_lock(lock)
            self.assertFalse(lock.exists())

    def test_second_acquire_yields(self) -> None:
        # Pretend a previous process (this pid) holds the lock.
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "crawl_lock"
            self.assertTrue(scheduler.acquire_lock(lock))
            # PID is alive (ours), so second acquire from a hypothetical
            # other instance would yield. We exercise by NOT releasing
            # before the second call.
            self.assertFalse(scheduler.acquire_lock(lock))
            scheduler.release_lock(lock)

    def test_stale_lock_is_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "crawl_lock"
            # Stale lock with a PID that's almost certainly dead and a
            # made-up host. The same host gate matters — give it a
            # plausibly-this-host name to test the alive check fallback.
            # Use PID=1 with bogus hostname so the host gate fails and
            # we treat it as stale.
            lock.write_text("999999 nonexistent-host 2020-01-01T00:00:00Z\n",
                            encoding="utf-8")
            self.assertTrue(scheduler.acquire_lock(lock))

    def test_corrupt_lock_is_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "crawl_lock"
            lock.write_text("garbage", encoding="utf-8")
            self.assertTrue(scheduler.acquire_lock(lock))


class LastSHATests(unittest.TestCase):
    def test_returns_meta_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Path(tmp)
            (catalog / "alpha.json").write_text(
                json.dumps({"_meta": {"commit": "deadbeef"}}), encoding="utf-8"
            )
            self.assertEqual(
                scheduler.last_extracted_sha(catalog, {"name": "alpha", "id": "acme/alpha"}),
                "deadbeef",
            )

    def test_returns_none_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(scheduler.last_extracted_sha(Path(tmp), {"name": "missing", "id": "acme/missing"}))

    def test_returns_none_on_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Path(tmp)
            (catalog / "broken.json").write_text("{ not json", encoding="utf-8")
            self.assertIsNone(scheduler.last_extracted_sha(catalog, {"name": "broken", "id": "acme/broken"}))

    def test_handles_no_meta_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Path(tmp)
            (catalog / "nometa.json").write_text(
                json.dumps({"components": []}), encoding="utf-8"
            )
            self.assertIsNone(scheduler.last_extracted_sha(catalog, {"name": "nometa", "id": "acme/nometa"}))

    def test_uses_repo_unit_record_name_for_monorepos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Path(tmp)
            (catalog / "microservices-demo__checkoutservice.json").write_text(
                json.dumps({"_meta": {"commit": "cafebabe"}}), encoding="utf-8"
            )
            self.assertEqual(
                scheduler.last_extracted_sha(
                    catalog,
                    {
                        "name": "checkoutservice",
                        "id": "GoogleCloudPlatform/microservices-demo/checkoutservice",
                    },
                ),
                "cafebabe",
            )


class WriteRunLogTests(unittest.TestCase):
    def test_writes_named_file_and_returns_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = {"run_id": "20260518T000000Z-abcd1234", "status": "ok"}
            path = scheduler._write_run_log(Path(tmp), log)
            self.assertTrue(path.exists())
            loaded = json.loads(path.read_text())
            self.assertEqual(loaded["run_id"], log["run_id"])

    def test_overwrites_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = {"run_id": "test-run-1", "status": "ok"}
            scheduler._write_run_log(Path(tmp), log)
            log["status"] = "updated"
            scheduler._write_run_log(Path(tmp), log)
            loaded = json.loads((Path(tmp) / "test-run-1.json").read_text())
            self.assertEqual(loaded["status"], "updated")


class RunTickTests(unittest.TestCase):
    """The tick orchestration end-to-end, with the crawler subprocess
    mocked so no real LLM is called.
    """

    def _setup_workspace(self, tmp: Path) -> tuple[Path, Path]:
        workspace = tmp / "workspace"
        workspace.mkdir()
        for name in ("alpha", "beta"):
            r = workspace / name
            r.mkdir()
            (r / ".git").mkdir()
        catalog = tmp / "catalog"
        catalog.mkdir()
        return workspace, catalog

    def test_tick_with_no_changes_writes_no_change_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            workspace, catalog = self._setup_workspace(tmp_p)
            run_log_dir = tmp_p / "run_logs"
            lock_path = tmp_p / "crawl_lock"
            with mock.patch("servicescout.scheduler.detect_changed_repos", return_value=[]):
                log = scheduler.run_tick(
                    workspace_root=workspace,
                    catalog_dir=catalog,
                    workspace_path=tmp_p / "workspace.json",
                    run_log_dir=run_log_dir,
                    lock_path=lock_path,
                    budget_usd=10.0,
                    provider="codex", model="gpt-5.4-mini", effort="medium",
                )
            self.assertEqual(log["status"], "no_changes")
            # Run log was written
            files = list(run_log_dir.iterdir())
            self.assertEqual(len(files), 1)
            # Lock is released
            self.assertFalse(lock_path.exists())

    def test_tick_with_changes_invokes_crawler_with_changed_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            workspace, catalog = self._setup_workspace(tmp_p)
            run_log_dir = tmp_p / "run_logs"
            lock_path = tmp_p / "crawl_lock"
            changed = [
                {"repo": "alpha", "path": str(workspace / "alpha"),
                 "last_extracted_sha": None, "remote_sha": "aaa", "local_sha": "aaa", "fetch_ok": True},
            ]
            with mock.patch("servicescout.scheduler.detect_changed_repos", return_value=changed), \
                 mock.patch("servicescout.scheduler.subprocess.run") as run_mock:
                run_mock.return_value = mock.Mock(returncode=0, stdout="", stderr="")
                log = scheduler.run_tick(
                    workspace_root=workspace,
                    catalog_dir=catalog,
                    workspace_path=tmp_p / "workspace.json",
                    run_log_dir=run_log_dir,
                    lock_path=lock_path,
                    budget_usd=10.0,
                    provider="codex", model="gpt-5.4-mini", effort="medium",
                )
            self.assertEqual(log["status"], "ok")
            self.assertEqual(log["repos_changed"], changed)
            cmd = run_mock.call_args[0][0]
            self.assertIn("--repos", cmd)
            self.assertIn("alpha", cmd)
            self.assertNotIn("beta", cmd)
            self.assertFalse(lock_path.exists())

    def test_tick_skips_when_lock_already_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            workspace, catalog = self._setup_workspace(tmp_p)
            run_log_dir = tmp_p / "run_logs"
            lock_path = tmp_p / "crawl_lock"
            # Pre-acquire the lock (this PID holds it).
            self.assertTrue(scheduler.acquire_lock(lock_path))
            try:
                log = scheduler.run_tick(
                    workspace_root=workspace,
                    catalog_dir=catalog,
                    workspace_path=tmp_p / "workspace.json",
                    run_log_dir=run_log_dir,
                    lock_path=lock_path,
                    budget_usd=10.0,
                    provider="codex", model=None, effort="medium",
                )
                self.assertEqual(log["status"], "tick_skipped_busy")
            finally:
                scheduler.release_lock(lock_path)

    def test_tick_records_crawler_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            workspace, catalog = self._setup_workspace(tmp_p)
            run_log_dir = tmp_p / "run_logs"
            lock_path = tmp_p / "crawl_lock"
            changed = [{"repo": "alpha", "path": str(workspace / "alpha"),
                        "last_extracted_sha": None, "remote_sha": "a",
                        "local_sha": "a", "fetch_ok": True}]
            with mock.patch("servicescout.scheduler.detect_changed_repos", return_value=changed), \
                 mock.patch("servicescout.scheduler.subprocess.run") as run_mock:
                run_mock.return_value = mock.Mock(returncode=2,
                                                  stdout="", stderr="boom")
                log = scheduler.run_tick(
                    workspace_root=workspace,
                    catalog_dir=catalog,
                    workspace_path=tmp_p / "workspace.json",
                    run_log_dir=run_log_dir,
                    lock_path=lock_path,
                    budget_usd=10.0,
                    provider="codex", model=None, effort="medium",
                )
            self.assertEqual(log["status"], "crawler_failed")
            self.assertEqual(log["crawler_returncode"], 2)
            self.assertIn("boom", log["crawler_stderr_tail"])


class WorkspaceReposTests(unittest.TestCase):
    def test_lists_only_git_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "real-repo").mkdir()
            (ws / "real-repo" / ".git").mkdir()
            (ws / "not-a-repo").mkdir()
            (ws / "file.txt").write_text("x", encoding="utf-8")
            self.assertEqual(scheduler._workspace_repos(ws), ["real-repo"])


if __name__ == "__main__":
    unittest.main()
