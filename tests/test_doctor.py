"""Tests for `servicescout doctor` diagnostics.

These exercise the check-gathering and report logic without spawning real
CLIs or hitting the network — the provider auth probe and the gh probe are
mocked, and the mount checks use real temp dirs.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from servicescout import doctor
from servicescout.doctor import FAIL, OK, SKIP, WARN, format_report, gather_checks


def _status(checks, name):
    return next(c.status for c in checks if c.name == name)


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_api_key_auth_passes_and_no_failures(self) -> None:
        with mock.patch.object(doctor, "provider_auth_status", return_value=(True, "codex: using CODEX_API_KEY")):
            checks = gather_checks("codex", str(self.dir), str(self.dir),
                                   env={"CODEX_API_KEY": "x"}, check_github=False)
        self.assertEqual(_status(checks, "extractor auth (codex)"), OK)
        self.assertFalse(any(c.status == FAIL for c in checks))

    def test_unauthenticated_provider_is_a_failure(self) -> None:
        with mock.patch.object(doctor, "provider_auth_status", return_value=(False, "codex CLI is not logged in")):
            checks = gather_checks("codex", str(self.dir), str(self.dir), env={}, check_github=False)
        self.assertEqual(_status(checks, "extractor auth (codex)"), FAIL)

    def test_codex_billing_footgun_warns(self) -> None:
        with mock.patch.object(doctor, "provider_auth_status", return_value=(True, "ok")):
            checks = gather_checks("codex", str(self.dir), str(self.dir),
                                   env={"OPENAI_API_KEY": "x"}, check_github=False)
        self.assertEqual(_status(checks, "codex billing"), WARN)

    def test_no_footgun_when_codex_api_key_set(self) -> None:
        with mock.patch.object(doctor, "provider_auth_status", return_value=(True, "ok")):
            checks = gather_checks("codex", str(self.dir), str(self.dir),
                                   env={"OPENAI_API_KEY": "x", "CODEX_API_KEY": "y"}, check_github=False)
        self.assertNotIn("codex billing", [c.name for c in checks])

    def test_missing_workspace_dir_fails(self) -> None:
        with mock.patch.object(doctor, "provider_auth_status", return_value=(True, "ok")):
            checks = gather_checks("codex", str(self.dir / "nope"), str(self.dir),
                                   env={"CODEX_API_KEY": "x"}, check_github=False)
        self.assertEqual(_status(checks, "workspace mount"), FAIL)

    def test_embeddings_skipped_without_project(self) -> None:
        with mock.patch.object(doctor, "provider_auth_status", return_value=(True, "ok")):
            checks = gather_checks("codex", str(self.dir), str(self.dir),
                                   env={"CODEX_API_KEY": "x"}, check_github=False)
        self.assertEqual(_status(checks, "embeddings"), SKIP)

    def test_github_token_env_passes(self) -> None:
        with mock.patch.object(doctor, "provider_auth_status", return_value=(True, "ok")):
            checks = gather_checks("codex", str(self.dir), str(self.dir),
                                   env={"CODEX_API_KEY": "x", "GITHUB_TOKEN": "ghp_x"}, check_github=True)
        self.assertEqual(_status(checks, "github access"), OK)

    def test_github_missing_cli_and_token_fails(self) -> None:
        with mock.patch.object(doctor, "provider_auth_status", return_value=(True, "ok")), \
             mock.patch("servicescout.doctor.shutil.which", return_value=None):
            checks = gather_checks("codex", str(self.dir), str(self.dir),
                                   env={"CODEX_API_KEY": "x"}, check_github=True)
        self.assertEqual(_status(checks, "github access"), FAIL)

    def test_main_returns_nonzero_on_failure(self) -> None:
        with mock.patch.object(doctor, "provider_auth_status", return_value=(False, "not logged in")):
            rc = doctor.main(["--provider", "codex", "--no-github",
                              "--workspace-root", str(self.dir), "--data-dir", str(self.dir)])
        self.assertEqual(rc, 1)

    def test_main_returns_zero_when_ready(self) -> None:
        with mock.patch.object(doctor, "provider_auth_status", return_value=(True, "ok")):
            rc = doctor.main(["--provider", "claude", "--no-github",
                              "--workspace-root", str(self.dir), "--data-dir", str(self.dir)])
        self.assertEqual(rc, 0)

    def test_format_report_reports_failure_summary(self) -> None:
        checks = [doctor.Check("x", FAIL, "broke")]
        self.assertIn("required check(s) failed", format_report("codex", checks))


if __name__ == "__main__":
    unittest.main()
