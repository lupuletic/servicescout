"""Crawler orchestration tests."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from servicescout import crawler


class RunExtractorRootTests(unittest.TestCase):
    """run_extractor must resolve against the workspace root, not the focus-path
    parent — otherwise monorepo repo_units (path relative to the workspace) never
    resolve and every unit fails instantly."""

    def _run(self, repo, root):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return SimpleNamespace(stdout='EXTRACTOR_RESULT {"status": "ok"}\n', returncode=0)

        with mock.patch("servicescout.crawler.subprocess.run", side_effect=fake_run):
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


if __name__ == "__main__":
    unittest.main()
