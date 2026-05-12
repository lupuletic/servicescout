"""Repo discovery under a workspace root.

Supports both flat layouts (<root>/<repo>/.git) and org-prefixed layouts
(<root>/<Org>/<repo>/.git). The GitHub org is derived from the configured
`git remote origin` URL when available, so the on-disk arrangement does not
constrain the identity used in the catalog.

Domain-agnostic: pass any GitHub orgs you care about as a list. An empty list
means "any org found via remote URL is acceptable".
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Iterable


GITHUB_REMOTE_RE = re.compile(r"github\.com[:/]([\w-]+)/([\w.-]+?)(?:\.git)?/?$")


def _run_git(repo_path: Path, *args: str, timeout: int = 10) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def _parse_github_remote(remote_url: str) -> tuple[str, str] | None:
    if not remote_url:
        return None
    match = GITHUB_REMOTE_RE.search(remote_url)
    if not match:
        return None
    return match.group(1), match.group(2)


def _gather_git_dirs(root: Path) -> list[Path]:
    out: list[Path] = []
    for entry in sorted(p for p in root.iterdir() if p.is_dir()):
        if (entry / ".git").exists():
            out.append(entry)
            continue
        for child in sorted(p for p in entry.iterdir() if p.is_dir()):
            if (child / ".git").exists():
                out.append(child)
    return out


def find_repos(
    root: Path,
    orgs: Iterable[str] | None = None,
    excluded: Iterable[str] | None = None,
) -> list[dict[str, str]]:
    root = root.resolve()
    org_filter = set(orgs) if orgs else None
    blocked = set(excluded or [])

    repos: list[dict[str, str]] = []
    for repo_dir in _gather_git_dirs(root):
        remote = _run_git(repo_dir, "remote", "get-url", "origin")
        parsed = _parse_github_remote(remote)
        if parsed is not None:
            org, github_name = parsed
        else:
            parent = repo_dir.parent
            org = parent.name if parent != root else ""
            github_name = repo_dir.name
        if not org:
            continue
        if org_filter is not None and org not in org_filter:
            continue
        repo_id = f"{org}/{github_name}"
        if repo_id in blocked or github_name in blocked or repo_dir.name in blocked:
            continue
        repos.append({
            "id": repo_id,
            "name": github_name,
            "local_name": repo_dir.name,
            "org": org,
            "path": str(repo_dir.relative_to(root)),
            "absolute_path": str(repo_dir),
            "remote": remote,
            "commit": _run_git(repo_dir, "rev-parse", "--short=12", "HEAD"),
        })
    return repos


def load_workspace_config(config_path: Path) -> dict[str, object]:
    if not config_path.exists():
        return {"orgs": [], "excluded_repos": []}
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    return {
        "orgs": list(payload.get("orgs", [])),
        "excluded_repos": list(payload.get("excluded_repos", [])),
    }
