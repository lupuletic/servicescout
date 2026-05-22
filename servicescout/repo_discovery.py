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
from typing import Any, Iterable


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


def _normalise_repo_units(
    root: Path,
    repo_units: Iterable[dict[str, Any]],
    org_filter: set[str] | None,
    blocked: set[str],
) -> list[dict[str, str]]:
    repos: list[dict[str, str]] = []
    for unit in repo_units:
        repo_id = str(unit.get("id") or "").strip()
        rel_path = str(unit.get("path") or "").strip()
        if not repo_id or not rel_path:
            continue
        org = repo_id.split("/", 1)[0] if "/" in repo_id else ""
        name = str(unit.get("name") or repo_id.rstrip("/").split("/")[-1])
        if not org:
            continue
        if org_filter is not None and org not in org_filter:
            continue
        if repo_id in blocked or name in blocked:
            continue
        repo_path = (root / rel_path).resolve()
        if not repo_path.exists():
            continue
        git_root = repo_path
        while git_root != root and not (git_root / ".git").exists():
            git_root = git_root.parent
        remote = _run_git(git_root, "remote", "get-url", "origin") if (git_root / ".git").exists() else ""
        repos.append({
            "id": repo_id,
            "name": name,
            "local_name": name,
            "org": org,
            "path": str(repo_path.relative_to(root)),
            "absolute_path": str(repo_path),
            "remote": remote,
            "commit": _run_git(git_root, "rev-parse", "--short=12", "HEAD") if (git_root / ".git").exists() else "",
            "focus_path": str(unit.get("focus_path") or ""),
        })
    return repos


def find_repos(
    root: Path,
    orgs: Iterable[str] | None = None,
    excluded: Iterable[str] | None = None,
    repo_units: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    root = root.resolve()
    org_filter = set(orgs) if orgs else None
    blocked = set(excluded or [])

    explicit_units = list(repo_units or [])
    if explicit_units:
        return _normalise_repo_units(root, explicit_units, org_filter, blocked)

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
        return {"orgs": [], "excluded_repos": [], "seeds": [], "scope": {}}
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    return {
        "orgs": list(payload.get("orgs", [])),
        "excluded_repos": list(payload.get("excluded_repos", [])),
        "repo_units": list(payload.get("repo_units") or payload.get("extract_units") or []),
        "communication_discovery_role_suffixes": list(payload.get("communication_discovery_role_suffixes", [])),
        # Journey-root repos ("org/name") to clone + extract first; downstream
        # dependencies are then discovered from their references.
        "seeds": [str(s).strip() for s in (payload.get("seeds") or []) if str(s).strip()],
        # Crawl bounds: {"discover": bool, "max_discovery_rounds": int}.
        "scope": dict(payload.get("scope") or {}),
    }


def write_workspace_config(
    config_path: Path,
    *,
    orgs: Iterable[str],
    seeds: Iterable[str],
    scope: dict[str, Any] | None,
    excluded_repos: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Persist orgs/seeds/scope to workspace.json, preserving any existing
    fields (repo_units, role suffixes) the UI doesn't manage. Returns the
    written config. Never handles secrets — the PAT is stored separately.
    """
    existing: dict[str, Any] = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
    merged = dict(existing)
    merged["orgs"] = list(orgs)
    merged["seeds"] = [str(s).strip() for s in seeds if str(s).strip()]
    merged["scope"] = dict(scope or {})
    if excluded_repos is not None:
        merged["excluded_repos"] = list(excluded_repos)
    else:
        merged.setdefault("excluded_repos", list(existing.get("excluded_repos") or []))
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return merged
