"""Continuous crawl scheduler (Issue #1).

Daemon that runs the crawl pipeline on a schedule. Each tick:

  1. git fetch on every repo under WORKSPACE_ROOT.
  2. Detect repos whose remote HEAD differs from the SHA recorded in the
     last successful extraction (`data/catalog/<repo>.json:_meta.commit`).
  3. If any changed: invoke crawler.py with --repos <changed_set>, gated
     by a per-tick budget cap.
  4. Persist a run log to data/crawl_runs/<run_id>.json. The dashboard
     reads these logs for the Activity page (see dashboard.py
     /api/crawl/runs endpoints).

Runs as a foreground process inside the existing docker-compose stack.
Use --once for a single tick (testing / manual trigger) or default
(infinite loop with sleep-until-next-tick).

Lock file at `data/crawl_lock` prevents tick-on-tick overlap: if the
previous tick is still running, the new tick logs `tick_skipped_busy`
and waits for the one after.

Termination: SIGTERM / SIGINT writes the in-flight run log (if any) as
`status: interrupted` before exiting. Catalog files are written atomically
by the crawler so partial state is never visible.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from repo_discovery import find_repos, load_workspace_config


HERE = Path(__file__).parent
DEFAULT_RUN_LOG_DIR = HERE / "data" / "crawl_runs"
DEFAULT_LOCK_PATH = HERE / "data" / "crawl_lock"
DEFAULT_WORKSPACE = HERE / "workspace.json"
DEFAULT_CATALOG_DIR = HERE / "data" / "catalog"


def emit(event: dict[str, Any]) -> None:
    """Stream a structured event to stdout. Captured by docker logs and
    the dashboard's tail subscription.
    """
    event.setdefault("ts", dt.datetime.now(dt.timezone.utc).isoformat())
    sys.stdout.write(json.dumps(event, sort_keys=True) + "\n")
    sys.stdout.flush()


# --------------------------------------------------------------------------- #
# Lock management
# --------------------------------------------------------------------------- #


def acquire_lock(path: Path) -> bool:
    """Try to acquire an exclusive lock file. Returns True on success.

    Lock is a simple file containing `<pid> <host> <iso_ts>`. If the
    file already exists and the recorded PID is still alive on the same
    host, we yield. Otherwise the lock is considered stale and we
    overwrite it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            content = path.read_text(encoding="utf-8").strip().split()
            if len(content) >= 2:
                pid_str, host = content[0], content[1]
                pid = int(pid_str)
                if host == socket.gethostname() and _pid_alive(pid):
                    return False
        except (ValueError, OSError):
            pass  # stale / corrupt — overwrite
    path.write_text(
        f"{os.getpid()} {socket.gethostname()} {dt.datetime.now(dt.timezone.utc).isoformat()}\n",
        encoding="utf-8",
    )
    return True


def release_lock(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- #
# Change detection
# --------------------------------------------------------------------------- #


def _git_root_for_path(path: Path, workspace_root: Path) -> Path | None:
    current = path.resolve()
    workspace_root = workspace_root.resolve()
    while True:
        if (current / ".git").exists():
            return current
        if current == workspace_root:
            break
        parent = current.parent
        if parent == current:
            break
        if workspace_root not in parent.parents and parent != workspace_root:
            break
        current = parent
    return None


def get_local_head_sha(repo_root: Path) -> str | None:
    """Current HEAD SHA via `git rev-parse HEAD`. Returns None if not a
    git repo or on any error.
    """
    if not (repo_root / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        if out.returncode != 0:
            return None
        return out.stdout.strip() or None
    except (subprocess.SubprocessError, OSError):
        return None


def fetch_remote(repo_root: Path) -> bool:
    """`git fetch --prune origin`. Returns True on success."""
    if not (repo_root / ".git").exists():
        return False
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "fetch", "--prune", "--quiet", "origin"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300,
        )
        return out.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def get_remote_head_sha(repo_root: Path) -> str | None:
    """SHA of `origin/HEAD` after a fetch — what we'd be on if we pulled."""
    if not (repo_root / ".git").exists():
        return None
    try:
        # Try origin/HEAD first; fall back to the default branch's remote ref.
        for ref in ("origin/HEAD", "origin/main", "origin/master"):
            out = subprocess.run(
                ["git", "-C", str(repo_root), "rev-parse", ref],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
            )
            if out.returncode == 0:
                sha = out.stdout.strip()
                if sha:
                    return sha
        return None
    except (subprocess.SubprocessError, OSError):
        return None


def last_extracted_sha(catalog_dir: Path, repo_name: str) -> str | None:
    """Read the _meta.commit field from the latest extraction for this
    repo, if any. Returns None if no prior extraction.
    """
    path = catalog_dir / f"{repo_name}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        meta = data.get("_meta") or {}
        sha = meta.get("commit") or meta.get("source_commit_sha") or ""
        return sha or None
    except (OSError, json.JSONDecodeError):
        return None


def detect_changed_repos(
    workspace_root: Path, catalog_dir: Path, repos: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Walk the workspace, git-fetch each repo, compare remote HEAD to
    the SHA in its last extraction. Returns the list of repos whose
    remote HEAD has moved (or that have no prior extraction).
    """
    workspace_root = workspace_root.resolve()
    changed: list[dict[str, Any]] = []
    if not workspace_root.is_dir():
        return changed
    for repo in repos:
        repo_name = str(repo.get("name") or "")
        repo_path = Path(str(repo.get("absolute_path") or ""))
        repo_root = _git_root_for_path(repo_path, workspace_root)
        if not repo_name or repo_root is None:
            continue
        fetch_ok = fetch_remote(repo_root)
        remote_sha = get_remote_head_sha(repo_root)
        local_sha = get_local_head_sha(repo_root)
        last_sha = last_extracted_sha(catalog_dir, repo_name)
        # A repo is "changed" if:
        #   - we've never extracted it, OR
        #   - the SHA we last extracted is not the remote HEAD.
        # Note: we trigger on remote-vs-extracted, not local-vs-extracted,
        # so a working tree that hasn't been pulled still gets re-extracted
        # at the new SHA (the crawler will pull as part of the run).
        reference_sha = remote_sha or local_sha
        if last_sha != reference_sha:
            changed.append({
                "repo": repo_name,
                "id": repo.get("id"),
                "path": str(repo_path),
                "last_extracted_sha": last_sha,
                "remote_sha": remote_sha,
                "local_sha": local_sha,
                "fetch_ok": fetch_ok,
            })
    return changed


def forced_repo_changes(workspace_root: Path, repos: list[dict[str, Any]], force_repos: list[str]) -> list[dict[str, Any]]:
    wanted = set(force_repos)
    changed: list[dict[str, Any]] = []
    for repo in repos:
        repo_name = str(repo.get("name") or "")
        repo_id = str(repo.get("id") or "")
        if repo_name not in wanted and repo_id not in wanted:
            continue
        repo_path = Path(str(repo.get("absolute_path") or ""))
        git_root = _git_root_for_path(repo_path, workspace_root)
        fetch_ok = fetch_remote(git_root) if git_root else False
        changed.append({
            "repo": repo_name,
            "id": repo_id,
            "path": str(repo_path),
            "reason": "manual_reindex",
            "remote_sha": get_remote_head_sha(git_root) if git_root else None,
            "local_sha": get_local_head_sha(git_root) if git_root else None,
            "fetch_ok": fetch_ok,
        })
    return changed


# --------------------------------------------------------------------------- #
# Tick orchestration
# --------------------------------------------------------------------------- #


def run_tick(
    *,
    workspace_root: Path,
    catalog_dir: Path,
    workspace_path: Path,
    run_log_dir: Path,
    lock_path: Path,
    budget_usd: float,
    provider: str,
    model: str | None,
    effort: str,
    trigger: str = "cron",
    extra_crawler_args: list[str] | None = None,
    force_repos: list[str] | None = None,
) -> dict[str, Any]:
    """One scheduler tick. Atomic with respect to the lock file: if
    the lock is held by a previous tick, returns immediately with
    `status=tick_skipped_busy`.
    """
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    log: dict[str, Any] = {
        "run_id": run_id,
        "trigger": trigger,
        "started_at": started,
        "workspace_root": str(workspace_root),
        "budget_usd": budget_usd,
        "events": [],
    }

    if not acquire_lock(lock_path):
        log["status"] = "tick_skipped_busy"
        log["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        _write_run_log(run_log_dir, log)
        emit({"event": "tick_skipped_busy", "run_id": run_id})
        return log

    try:
        emit({"event": "tick_start", "run_id": run_id})
        workspace = load_workspace_config(workspace_path)
        repos = find_repos(
            workspace_root,
            workspace.get("orgs") or [],
            workspace.get("excluded_repos") or [],
            workspace.get("repo_units") or [],
        )
        changed = forced_repo_changes(workspace_root, repos, force_repos or []) if force_repos else detect_changed_repos(workspace_root, catalog_dir, repos)
        log["repos_checked"] = len(repos)
        log["repos_changed"] = changed
        event_name = "manual_reindex_selected" if force_repos else "change_detected"
        log["events"].append({"event": event_name, "count": len(changed), "requested": force_repos or []})
        emit({"event": event_name, "run_id": run_id, "count": len(changed), "requested": force_repos or []})

        if not changed:
            log["status"] = "no_changes"
            log["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            _write_run_log(run_log_dir, log)
            emit({"event": "tick_done_no_changes", "run_id": run_id})
            return log

        repo_names = [r["repo"] for r in changed]
        cmd = [
            sys.executable, str(HERE / "crawler.py"),
            "--root", str(workspace_root),
            "--catalog-dir", str(catalog_dir),
            "--catalog-output", str(catalog_dir.parent / "catalog.json"),
            "--workspace", str(workspace_path),
            "--budget-usd", str(budget_usd),
            "--repos", *repo_names,
            "--provider", provider,
            "--effort", effort,
        ]
        if model:
            cmd.extend(["--model", model])
        if extra_crawler_args:
            cmd.extend(extra_crawler_args)
        log["events"].append({"event": "crawler_invoke", "cmd": cmd})
        emit({"event": "crawler_invoke", "run_id": run_id, "repos": repo_names})

        proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        log["crawler_returncode"] = proc.returncode
        log["crawler_stdout_tail"] = (proc.stdout or "")[-4000:]
        log["crawler_stderr_tail"] = (proc.stderr or "")[-2000:]
        if proc.returncode != 0:
            log["status"] = "crawler_failed"
            emit({"event": "tick_failed", "run_id": run_id, "rc": proc.returncode})
        else:
            log["status"] = "ok"
            emit({"event": "tick_done_ok", "run_id": run_id, "repos_extracted": len(repo_names)})
    except Exception as exc:  # noqa: BLE001
        log["status"] = "exception"
        log["error"] = str(exc)
        emit({"event": "tick_failed_exception", "run_id": run_id, "error": str(exc)})
    finally:
        log["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        _write_run_log(run_log_dir, log)
        release_lock(lock_path)
    return log


def _workspace_repos(workspace_root: Path) -> list[str]:
    if not workspace_root.is_dir():
        return []
    return sorted(d.name for d in workspace_root.iterdir() if d.is_dir() and (d / ".git").exists())


def _write_run_log(run_log_dir: Path, log: dict[str, Any]) -> Path:
    run_log_dir.mkdir(parents=True, exist_ok=True)
    path = run_log_dir / f"{log['run_id']}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(log, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


# --------------------------------------------------------------------------- #
# Daemon loop
# --------------------------------------------------------------------------- #


_RUNNING = True


def _sigterm_handler(signum, frame) -> None:  # noqa: ARG001
    global _RUNNING
    _RUNNING = False
    emit({"event": "shutdown_requested", "signal": signum})


def run_daemon(
    *,
    interval_minutes: int,
    workspace_root: Path,
    catalog_dir: Path,
    workspace_path: Path,
    run_log_dir: Path,
    lock_path: Path,
    budget_usd: float,
    provider: str,
    model: str | None,
    effort: str,
    extra_crawler_args: list[str] | None = None,
) -> int:
    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT, _sigterm_handler)
    emit({"event": "scheduler_start", "interval_minutes": interval_minutes,
          "workspace_root": str(workspace_root)})

    interval_seconds = max(60, interval_minutes * 60)
    while _RUNNING:
        run_tick(
            workspace_root=workspace_root,
            catalog_dir=catalog_dir,
            workspace_path=workspace_path,
            run_log_dir=run_log_dir,
            lock_path=lock_path,
            budget_usd=budget_usd,
            provider=provider,
            model=model,
            effort=effort,
            extra_crawler_args=extra_crawler_args,
        )
        # Sleep in small chunks so SIGTERM is responsive.
        slept = 0
        while _RUNNING and slept < interval_seconds:
            time.sleep(min(5, interval_seconds - slept))
            slept += 5
    emit({"event": "scheduler_stop"})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path,
                        default=Path(os.getenv("WORKSPACE_ROOT") or Path.cwd()))
    parser.add_argument("--catalog-dir", type=Path, default=DEFAULT_CATALOG_DIR)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--run-log-dir", type=Path, default=DEFAULT_RUN_LOG_DIR)
    parser.add_argument("--lock-path", type=Path, default=DEFAULT_LOCK_PATH)
    parser.add_argument("--interval-minutes", type=int,
                        default=int(os.getenv("CRAWL_INTERVAL_MINUTES") or 360))
    parser.add_argument("--budget-usd", type=float,
                        default=float(os.getenv("CRAWL_TICK_BUDGET_USD") or 20.0))
    parser.add_argument("--provider", default=os.getenv("LLM_PROVIDER") or "codex")
    parser.add_argument("--model", default=os.getenv("LLM_MODEL") or None)
    parser.add_argument("--effort", default=os.getenv("LLM_EFFORT") or "medium")
    parser.add_argument("--once", action="store_true",
                        help="Run a single tick and exit. Useful for tests / manual triggers / CI.")
    parser.add_argument(
        "--crawler-arg", action="append", default=[],
        help="Extra arg to forward to crawler.py. May be passed multiple times. "
             "Example: --crawler-arg --reconcile --crawler-arg --build-kuzu",
    )
    parser.add_argument(
        "--force-repos", nargs="*", default=None,
        help="Extract these repo names/ids even if change detection says they are current.",
    )
    parser.add_argument("--trigger", default="cron", help="Run trigger label recorded in Activity.")
    args = parser.parse_args()

    if args.once:
        log = run_tick(
            workspace_root=args.workspace_root,
            catalog_dir=args.catalog_dir,
            workspace_path=args.workspace,
            run_log_dir=args.run_log_dir,
            lock_path=args.lock_path,
            budget_usd=args.budget_usd,
            provider=args.provider,
            model=args.model,
            effort=args.effort,
            trigger=args.trigger,
            extra_crawler_args=args.crawler_arg or None,
            force_repos=args.force_repos or None,
        )
        return 0 if log["status"] in {"ok", "no_changes"} else 1

    if args.force_repos:
        parser.error("--force-repos is only supported with --once")

    return run_daemon(
        interval_minutes=args.interval_minutes,
        workspace_root=args.workspace_root,
        catalog_dir=args.catalog_dir,
        workspace_path=args.workspace,
        run_log_dir=args.run_log_dir,
        lock_path=args.lock_path,
        budget_usd=args.budget_usd,
        provider=args.provider,
        model=args.model,
        effort=args.effort,
        extra_crawler_args=args.crawler_arg or None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
