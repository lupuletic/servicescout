"""ServiceScout dashboard — FastAPI backend + React SPA.

Serves:

  /                 React app (graph viewer, catalog, triage, crawl progress).
  /api/state.json   Live crawl + catalog state.
  /api/entities     Searchable entity list, filterable by kind.
  /api/entity/:ref  Full record for one entity.
  /api/graph        Node/edge JSON for the Sigma graph viewer.
  /api/communications Derived service-to-service communication flows.
  /api/triage.json  Unresolved external components.
  /triage/decide    POST endpoint for triage actions.

The React app is built into `frontend/dist/` by `npm --prefix frontend run
build`. If that directory doesn't exist (e.g. fresh checkout, dev mode),
the dashboard prints a one-line message and serves a JSON shim so the API
still works.

CLI:
  python dashboard.py --catalog data/catalog.json --host 127.0.0.1 --port 8788
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import signal
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from servicescout import github_client
from servicescout.repo_discovery import load_workspace_config, write_workspace_config


class GithubTokenBody(BaseModel):
    token: str


class GithubReposBody(BaseModel):
    token: str
    org: str


class WorkspaceConfigBody(BaseModel):
    orgs: list[str] = []
    seeds: list[str] = []
    scope: dict[str, Any] = {}
    discover: bool | None = None
    max_discovery_rounds: int | None = None
    budget_usd: float | None = None
    token: str | None = None  # optional: persist for the crawl via `gh auth`


class CrawlerRuntimeBody(BaseModel):
    parallelism: int | None = None
    batch_size: int | None = None


class RepoReindexBody(BaseModel):
    repos: list[str] = Field(default_factory=list)


def _balanced_entity_selection(
    entities: list[dict[str, Any]],
    *,
    selected_kinds: list[str],
    limit: int,
    score_key: Any,
) -> list[dict[str, Any]]:
    """Keep large kinds from crowding smaller selected kinds out of the graph."""
    sorted_entities = sorted(entities, key=score_key)
    if limit <= 0:
        return sorted_entities
    if len(sorted_entities) <= limit:
        return sorted_entities

    groups: dict[str, list[dict[str, Any]]] = {
        kind: [entity for entity in sorted_entities if entity.get("kind") == kind]
        for kind in selected_kinds
    }
    active_kinds = [kind for kind in selected_kinds if groups.get(kind)]
    if not active_kinds:
        return sorted_entities[:limit]

    base_quota = limit // len(active_kinds)
    quotas = {kind: min(len(groups[kind]), base_quota) for kind in active_kinds}
    remaining = limit - sum(quotas.values())

    while remaining > 0:
        candidates = [
            (score_key(groups[kind][quotas[kind]]), kind)
            for kind in active_kinds
            if quotas[kind] < len(groups[kind])
        ]
        if not candidates:
            break
        _, kind = min(candidates)
        quotas[kind] += 1
        remaining -= 1

    kept = [
        entity
        for kind in active_kinds
        for entity in groups[kind][:quotas[kind]]
    ]
    return sorted(kept, key=score_key)


def _persist_github_token(token: str) -> bool:
    """Store the PAT server-side so the crawl can clone private repos, via
    `gh auth login --with-token` (stdin — never logged). Single-tenant; the
    multi-tenant credential vault is Phase 4 on the roadmap."""
    try:
        result = subprocess.run(
            ["gh", "auth", "login", "--with-token"],
            input=token, text=True, capture_output=True, timeout=20,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _workspace_config_path() -> Path:
    return Path(os.environ.get("SERVICESCOUT_WORKSPACE_CONFIG") or str(HERE / "workspace.json"))


def _audit(data_dir: Path, action: str, **fields: Any) -> None:
    """Append-only audit trail for mutating actions (config changes, token
    storage, crawl triggers). Records the *fact* and a before->after diff, never
    the secret itself. Actor identity slots in once the dashboard has auth."""
    record = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "action": action,
        **fields,
    }
    append_jsonl(data_dir / "audit.jsonl", record)


HERE = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = HERE / "data" / "catalog.json"
DEFAULT_EXTRACTION_LOG = HERE / "data" / "extraction_runs.jsonl"
DEFAULT_DECISIONS = HERE / "data" / "triage_decisions.jsonl"
DEFAULT_KUZU = HERE / "data" / "catalog.kuzu"
FRONTEND_DIST = HERE / "frontend" / "dist"


# ---------- data loading ----------

_CATALOG_CACHE: dict[str, Any] = {"mtime": 0.0, "payload": None}


def load_catalog(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"entities": [], "relations": [], "summary": {}}
    mtime = path.stat().st_mtime
    if _CATALOG_CACHE["payload"] is not None and _CATALOG_CACHE["mtime"] == mtime:
        return _CATALOG_CACHE["payload"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    _CATALOG_CACHE["mtime"] = mtime
    _CATALOG_CACHE["payload"] = payload
    return payload


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


# ---------- crawler status ----------

def process_status(pattern: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            text=True, capture_output=True, check=False, timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return {"running": False, "pid": None, "uptime": None}
    pids = [int(p) for p in result.stdout.split() if p.strip().isdigit()]
    if not pids:
        return {"running": False, "pid": None, "uptime": None}
    candidates: list[tuple[int, str]] = []
    for candidate in pids:
        try:
            ps_args = subprocess.run(
                ["ps", "-o", "args=", "-p", str(candidate)],
                text=True, capture_output=True, check=False, timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        candidates.append((candidate, ps_args.stdout.strip()))
    if not candidates:
        return {"running": False, "pid": None, "uptime": None}
    module_pattern = f"-m {pattern}"
    preferred = [
        candidate
        for candidate in candidates
        if (
            candidate[1].split()
            and Path(candidate[1].split()[0]).name.startswith("python")
            and (
                module_pattern in candidate[1]
                or re.search(rf"(^|/){re.escape(pattern.rsplit('.', 1)[-1])}\.py(\s|$)", candidate[1])
            )
        )
    ]
    pid = (preferred or candidates)[0][0]
    try:
        ps = subprocess.run(["ps", "-o", "etime=", "-p", str(pid)], text=True, capture_output=True, check=False, timeout=3)
        etime = ps.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        etime = ""
    return {"running": True, "pid": pid, "uptime": etime}


def crawler_status() -> dict[str, Any]:
    return process_status("servicescout.crawler")


def scheduler_status() -> dict[str, Any]:
    return process_status("servicescout.scheduler")


def tail_file(path: Path, limit: int = 40) -> list[str]:
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-limit:]


def read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def scheduler_control_paths(data_dir: Path) -> dict[str, Path]:
    return {
        "pid": data_dir / "scheduler_daemon.pid",
        "settings": data_dir / "scheduler_settings.json",
        "log": data_dir / "scheduler_daemon.log",
    }


def read_scheduler_settings(data_dir: Path) -> dict[str, Any]:
    paths = scheduler_control_paths(data_dir)
    settings: dict[str, Any] = {
        "interval_minutes": int(os.environ.get("CRAWL_INTERVAL_MINUTES") or 360),
        "budget_usd": float(os.environ.get("CRAWL_TICK_BUDGET_USD") or os.environ.get("BUDGET_USD") or 20.0),
    }
    try:
        if paths["settings"].is_file():
            stored = json.loads(paths["settings"].read_text(encoding="utf-8"))
            if isinstance(stored.get("interval_minutes"), int) and stored["interval_minutes"] >= 1:
                settings["interval_minutes"] = int(stored["interval_minutes"])
            if isinstance(stored.get("budget_usd"), (int, float)) and stored["budget_usd"] > 0:
                settings["budget_usd"] = float(stored["budget_usd"])
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        pass
    return settings


def write_scheduler_settings(data_dir: Path, *, interval_minutes: int, budget_usd: float) -> None:
    paths = scheduler_control_paths(data_dir)
    paths["settings"].parent.mkdir(parents=True, exist_ok=True)
    paths["settings"].write_text(
        json.dumps({
            "interval_minutes": interval_minutes,
            "budget_usd": budget_usd,
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def crawler_runtime_config_path(data_dir: Path) -> Path:
    return data_dir / "crawler_runtime.json"


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def read_crawler_runtime_config(data_dir: Path) -> dict[str, Any]:
    path = crawler_runtime_config_path(data_dir)
    payload: dict[str, Any] = {
        "parallelism": _bounded_int(os.environ.get("CRAWLER_PARALLELISM"), default=8, minimum=1, maximum=64),
        "batch_size": _bounded_int(os.environ.get("CRAWLER_BATCH_SIZE"), default=24, minimum=1, maximum=500),
        "path": str(path),
        "source": "default",
    }
    if path.is_file():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                payload.update({
                    "parallelism": _bounded_int(stored.get("parallelism"), default=payload["parallelism"], minimum=1, maximum=64),
                    "batch_size": _bounded_int(stored.get("batch_size"), default=payload["batch_size"], minimum=1, maximum=500),
                    "updated_at": stored.get("updated_at"),
                    "source": "file",
                })
        except (OSError, json.JSONDecodeError):
            payload["source"] = "invalid"
    payload["batch_size"] = max(int(payload["batch_size"]), int(payload["parallelism"]))
    return payload


def write_crawler_runtime_config(data_dir: Path, *, parallelism: int, batch_size: int) -> dict[str, Any]:
    path = crawler_runtime_config_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    parallelism = _bounded_int(parallelism, default=8, minimum=1, maximum=64)
    batch_size = max(_bounded_int(batch_size, default=max(parallelism, 24), minimum=1, maximum=500), parallelism)
    payload = {
        "parallelism": parallelism,
        "batch_size": batch_size,
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return read_crawler_runtime_config(data_dir)


def managed_scheduler_status(data_dir: Path) -> dict[str, Any]:
    paths = scheduler_control_paths(data_dir)
    settings = read_scheduler_settings(data_dir)
    pid: int | None = None
    if paths["pid"].is_file():
        try:
            pid = int(paths["pid"].read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = None
    running = bool(pid and pid_alive(pid))
    if paths["pid"].is_file() and not running:
        try:
            paths["pid"].unlink()
        except OSError:
            pass
    uptime = None
    if running and pid:
        try:
            ps = subprocess.run(["ps", "-o", "etime=", "-p", str(pid)], text=True, capture_output=True, check=False, timeout=3)
            uptime = ps.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            uptime = None
    return {
        "managed": paths["pid"].is_file() or paths["settings"].is_file(),
        "running": running,
        "pid": pid if running else None,
        "uptime": uptime,
        "interval_minutes": settings["interval_minutes"],
        "budget_usd": settings["budget_usd"],
        "log_path": str(paths["log"]) if paths["log"].is_file() else None,
        "log_tail": tail_file(paths["log"], 60),
    }


def latest_crawl_log() -> tuple[Path | None, list[str]]:
    candidates: list[Path] = []
    tmp = Path("/tmp")
    if tmp.exists():
        candidates.extend(tmp.glob("crawl*.log"))
    if not candidates:
        return None, []
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        text = newest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return newest, []
    lines = text.splitlines()
    return newest, lines[-20:]


def latest_activity_log(data_dir: Path, limit: int = 40) -> tuple[Path | None, list[str]]:
    run_log_dir = data_dir / "crawl_runs"
    if not run_log_dir.is_dir():
        return None, []
    candidates = sorted(run_log_dir.glob("*.json"), reverse=True)
    for path in candidates:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        events = doc.get("events") or []
        lines = [
            json.dumps(event, sort_keys=True)
            for event in events[-limit:]
            if isinstance(event, dict)
        ]
        return path, lines
    return None, []


def normalise_activity_run_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """Mark a previously interrupted crawler run as no longer active."""
    if doc.get("status") != "running":
        return _with_observed_duration(doc)
    try:
        pid = int(doc.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid and not pid_alive(pid):
        return _with_observed_duration({**doc, "status": "abandoned"})
    return _with_observed_duration(doc)


def _aware_dt(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


def _last_activity_at(doc: dict[str, Any]) -> str | None:
    candidates = [doc.get("finished_at")]
    for event in doc.get("events") or []:
        if isinstance(event, dict):
            candidates.append(event.get("ts"))
    parsed = [_aware_dt(_parse_dt(str(value))) for value in candidates if value]
    parsed = [value for value in parsed if value is not None]
    return max(parsed).isoformat() if parsed else None


def _with_observed_duration(doc: dict[str, Any]) -> dict[str, Any]:
    started = _aware_dt(_parse_dt(doc.get("started_at")))
    if started is None:
        return doc
    if doc.get("finished_at"):
        observed = _aware_dt(_parse_dt(doc.get("finished_at")))
    elif doc.get("status") == "running":
        observed = dt.datetime.now(dt.timezone.utc)
    else:
        observed = _aware_dt(_parse_dt(_last_activity_at(doc)))
    if observed is None:
        return doc
    duration = max((observed - started).total_seconds(), 0.0)
    return {**doc, "duration_seconds": round(duration, 1), "observed_at": observed.isoformat()}


def _repo_catalog_metrics(catalog_dir: Path, repo: str, *, started_at: str | None = None) -> dict[str, Any]:
    name = repo.split("/")[-1]
    if not name:
        return {}
    path = catalog_dir / f"{name}.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    meta = payload.get("_meta") or {}
    extracted_at = _aware_dt(_parse_dt(meta.get("extracted_at")))
    started = _aware_dt(_parse_dt(started_at))
    if started is not None:
        if extracted_at is None or extracted_at < started:
            return {}
    run = meta.get("run") or {}
    cost = (run.get("cost") or {}).get("estimated_usd")
    out: dict[str, Any] = {}
    if extracted_at is not None:
        out["extracted_at"] = extracted_at.isoformat()
    if isinstance(cost, (int, float)):
        out["cost_usd"] = float(cost)
    duration = run.get("duration_seconds")
    if isinstance(duration, (int, float)):
        out["duration_seconds"] = float(duration)
    if run.get("status"):
        out["status"] = run.get("status")
    elif extracted_at is not None:
        out["status"] = "ok"
    return out


def _activity_log_events(lines: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in lines:
        raw = line.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            start = raw.find("{")
            if start < 0:
                continue
            try:
                event = json.loads(raw[start:])
            except json.JSONDecodeError:
                continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _last_event(events: list[dict[str, Any]], *names: str) -> dict[str, Any] | None:
    allowed = set(names)
    for event in reversed(events):
        if event.get("event") in allowed:
            return event
    return None


def _event_repo(event: dict[str, Any]) -> str:
    repo = event.get("repo")
    return repo if isinstance(repo, str) else ""


def _event_ts(event: dict[str, Any]) -> str | None:
    value = event.get("ts")
    return value if isinstance(value, str) and value else None


def _event_number(event: dict[str, Any], key: str) -> float | None:
    value = event.get(key)
    return float(value) if isinstance(value, (int, float)) else None


def _parse_child_event(event: dict[str, Any]) -> dict[str, Any] | None:
    line = event.get("line")
    if not isinstance(line, str):
        return None
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _activity_child_message(event: dict[str, Any]) -> tuple[str, str]:
    inner = _parse_child_event(event)
    if not inner:
        return "agent output", "agent emitted output"
    name = str(inner.get("event") or "agent_event")
    item_type = str(inner.get("item_type") or "").replace("_", " ")
    status = str(inner.get("status") or "").replace("_", " ")
    if name == "item_started":
        label = item_type or "item"
        return label, f"{label} running"
    if name == "item_completed":
        label = item_type or "item"
        suffix = f" {status}" if status else ""
        return label, f"{label}{suffix}"
    if name == "turn_started":
        return "agent turn", "agent turn started"
    if name == "turn_completed":
        usage = inner.get("usage") if isinstance(inner.get("usage"), dict) else {}
        output_tokens = usage.get("output_tokens") if isinstance(usage, dict) else None
        suffix = f" · {output_tokens} output tokens" if isinstance(output_tokens, int) else ""
        return "agent turn", f"agent turn completed{suffix}"
    if name == "thread_started":
        return "agent session", "agent session started"
    return name.replace("_", " "), name.replace("_", " ")


def _activity_run_operational_state(events: list[dict[str, Any]], *, recent_limit: int = 20) -> dict[str, Any]:
    active: dict[str, dict[str, Any]] = {}
    recent: list[dict[str, Any]] = []
    recent_index: dict[str, int] = {}
    batch: dict[str, Any] | None = None

    def ensure_worker(repo: str, event: dict[str, Any]) -> dict[str, Any]:
        worker = active.get(repo)
        if worker is None:
            worker = {
                "repo": repo,
                "phase": "running",
                "started_at": _event_ts(event),
                "last_event": str(event.get("event") or "event"),
                "last_at": _event_ts(event),
                "last_message": "running",
            }
            active[repo] = worker
        return worker

    def update_worker(repo: str, event: dict[str, Any], **fields: Any) -> None:
        worker = ensure_worker(repo, event)
        worker.update({key: value for key, value in fields.items() if value is not None})
        worker["last_event"] = str(event.get("event") or worker.get("last_event") or "event")
        worker["last_at"] = _event_ts(event) or worker.get("last_at")

    def record_completion(repo: str, event: dict[str, Any]) -> None:
        active.pop(repo, None)
        cost = _event_number(event, "cost_usd")
        duration_seconds = _event_number(event, "duration_seconds")
        payload = {
            "repo": repo,
            "status": event.get("status") or ("ok" if event.get("returncode") == 0 else None),
            "duration_seconds": duration_seconds,
            "cost_usd": cost,
            "completed_at": _event_ts(event),
            "returncode": event.get("returncode"),
            "error": event.get("error"),
            "reason": "crawler_extraction",
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        if repo in recent_index:
            index = recent_index[repo]
            recent[index] = {**recent[index], **payload}
            return
        recent_index[repo] = len(recent)
        recent.append(payload)

    for event in events:
        name = str(event.get("event") or "")
        repo = _event_repo(event)
        if name == "batch_start":
            batch = {
                key: event.get(key)
                for key in ("n", "parallelism", "batch_size", "stale_remaining", "spent_so_far", "run_spent_so_far", "budget_usd", "ts")
                if event.get(key) is not None
            }
            continue
        if not repo:
            continue
        if name == "extractor_process_start":
            update_worker(
                repo,
                event,
                phase="starting",
                started_at=_event_ts(event),
                timeout_seconds=event.get("timeout_seconds"),
                last_message="extractor starting",
            )
        elif name == "extractor_process_pid":
            update_worker(
                repo,
                event,
                phase="extracting",
                pid=event.get("pid"),
                last_message="extractor process running",
            )
        elif name == "extractor_heartbeat":
            update_worker(
                repo,
                event,
                phase="extracting",
                pid=event.get("pid"),
                elapsed_seconds=_event_number(event, "elapsed_seconds"),
                result_file_present=event.get("result_file_present"),
                last_message="heartbeat",
            )
        elif name == "extractor_child_event":
            phase, message = _activity_child_message(event)
            update_worker(repo, event, phase=phase, last_message=message)
        elif name in {"extractor_process_exit", "repo_done"}:
            record_completion(repo, event)

    workers = sorted(
        active.values(),
        key=lambda worker: (worker.get("started_at") or worker.get("last_at") or "", worker.get("repo") or ""),
    )
    for index, worker in enumerate(workers, start=1):
        worker["worker_id"] = f"W{index}"
    return {
        "active_workers": workers,
        "recent_completions": list(reversed(recent[-recent_limit:])),
        "latest_batch": batch,
        "repos_completed_count": len(recent),
    }


def _active_run_doc(
    *,
    data_dir: Path,
    catalog_dir: Path,
    lock_info: dict[str, Any],
    scheduler_payload: dict[str, Any],
    active_log_lines: list[str],
    event_limit: int = 2000,
) -> dict[str, Any] | None:
    if not lock_info.get("held"):
        return None

    events = _activity_log_events(active_log_lines)
    last_tick_index = next(
        (idx for idx in range(len(events) - 1, -1, -1) if events[idx].get("event") == "tick_start"),
        -1,
    )
    run_events_all = events[last_tick_index:] if last_tick_index >= 0 else events
    run_events_tail = run_events_all[-event_limit:] if event_limit > 0 else run_events_all
    operational = _activity_run_operational_state(run_events_all)
    tick_start = _last_event(run_events_all, "tick_start")
    selected = _last_event(run_events_all, "manual_reindex_selected", "change_detected")
    invoked = _last_event(run_events_all, "crawler_invoke")
    run_id = (
        (tick_start or {}).get("run_id")
        or (selected or {}).get("run_id")
        or (invoked or {}).get("run_id")
        or f"active-{lock_info.get('pid') or 'crawl'}"
    )
    started_at = (
        (tick_start or {}).get("ts")
        or (selected or {}).get("ts")
        or lock_info.get("acquired_at")
    )
    invoked_repos = [
        repo for repo in (invoked or {}).get("repos", [])
        if isinstance(repo, str) and repo
    ] if isinstance((invoked or {}).get("repos"), list) else []
    selected_count = (selected or {}).get("count")
    repos_checked = selected_count if isinstance(selected_count, int) else (len(invoked_repos) or None)

    seen: set[str] = set()
    repos_changed: list[dict[str, Any]] = []
    for repo in invoked_repos:
        if repo in seen:
            continue
        seen.add(repo)
        metrics = _repo_catalog_metrics(catalog_dir, repo, started_at=str(started_at) if started_at else None)
        if not metrics:
            continue
        repos_changed.append({
            "repo": repo,
            "reason": "crawler_extraction",
            **metrics,
        })

    doc: dict[str, Any] = {
        "run_id": str(run_id),
        "trigger": "manual-reindex" if (selected or {}).get("event") == "manual_reindex_selected" else "manual",
        "started_at": started_at,
        "status": "running",
        "workspace_root": os.environ.get("WORKSPACE_ROOT") or "",
        "workspace_config": os.environ.get("SERVICESCOUT_WORKSPACE_CONFIG") or str(HERE / "workspace.json"),
        "budget_usd": float(scheduler_payload.get("budget_usd") or 20.0),
        "pid": lock_info.get("pid"),
        "events": run_events_tail,
        "event_count": len(run_events_all),
        **operational,
        "repos_changed": repos_changed,
    }
    if len(run_events_tail) < len(run_events_all):
        doc["events_truncated"] = True
    if repos_checked is not None:
        doc["repos_checked"] = repos_checked
    if data_dir:
        doc["active_log_path"] = str(data_dir / "crawl_trigger.log")
    return _enrich_activity_run_doc(doc, catalog_dir)


def _enrich_activity_run_doc(doc: dict[str, Any], catalog_dir: Path) -> dict[str, Any]:
    doc = normalise_activity_run_doc(doc)
    started_at = doc.get("started_at")
    events = [event for event in (doc.get("events") or []) if isinstance(event, dict)]
    operational = _activity_run_operational_state(events) if events else {}
    repos = []
    total_cost = 0.0
    saw_cost = False
    total_duration = 0.0
    saw_duration = False
    for repo in doc.get("repos_changed") or []:
        if not isinstance(repo, dict):
            continue
        metrics = _repo_catalog_metrics(catalog_dir, str(repo.get("repo") or ""), started_at=started_at)
        merged = {**metrics, **repo}
        if merged.get("cost_usd") is None and metrics.get("cost_usd") is not None:
            merged["cost_usd"] = metrics["cost_usd"]
        if merged.get("duration_seconds") is None and metrics.get("duration_seconds") is not None:
            merged["duration_seconds"] = metrics["duration_seconds"]
        cost = merged.get("cost_usd")
        if isinstance(cost, (int, float)):
            total_cost += float(cost)
            saw_cost = True
        duration = merged.get("duration_seconds")
        if isinstance(duration, (int, float)):
            total_duration += float(duration)
            saw_duration = True
        repos.append(merged)
    completed_count = doc.get("repos_completed_count")
    if not isinstance(completed_count, int):
        completed_count = max(len(repos), int(operational.get("repos_completed_count") or 0))
    out = {
        **doc,
        **{key: value for key, value in operational.items() if key not in doc},
        "repos_changed": repos,
        "repos_changed_count": len(repos),
        "repos_completed_count": completed_count,
    }
    if out.get("cost_usd") is None and saw_cost:
        out["cost_usd"] = round(total_cost, 4)
    if out.get("worker_duration_seconds") is None and saw_duration:
        out["worker_duration_seconds"] = round(total_duration, 1)
    return out


def _activity_run_summary(doc: dict[str, Any], path: Path) -> dict[str, Any]:
    return {
        "run_id": doc.get("run_id") or path.stem,
        "trigger": doc.get("trigger") or "cron",
        "started_at": doc.get("started_at"),
        "finished_at": doc.get("finished_at"),
        "observed_at": doc.get("observed_at"),
        "status": doc.get("status"),
        "repos_checked": doc.get("repos_checked"),
        "repos_changed_count": len(doc.get("repos_changed") or []),
        "repos_completed_count": doc.get("repos_completed_count"),
        "budget_usd": doc.get("budget_usd"),
        "cost_usd": doc.get("cost_usd"),
        "catalog_cost_usd": doc.get("catalog_cost_usd"),
        "duration_seconds": doc.get("duration_seconds"),
        "worker_duration_seconds": doc.get("worker_duration_seconds"),
        "crawler_returncode": doc.get("crawler_returncode"),
    }


# ---------- catalog helpers ----------

def cumulative_spend(extraction_log: Path) -> float:
    total = 0.0
    for record in read_jsonl(extraction_log):
        cost = (record.get("cost") or {}).get("estimated_usd")
        if isinstance(cost, (int, float)):
            total += float(cost)
    return round(total, 2)


def recent_extractions(extraction_log: Path, n: int = 10) -> list[dict[str, Any]]:
    records = read_jsonl(extraction_log)
    out = []
    for record in records[-n:][::-1]:
        out.append({
            "repo": record.get("repo"),
            "model": record.get("model"),
            "provider": record.get("provider"),
            "cost": round(float((record.get("cost") or {}).get("estimated_usd") or 0.0), 4),
            "duration_seconds": record.get("duration_seconds"),
            "status": record.get("status"),
        })
    return out


def last_build_timestamp(catalog_path: Path) -> str | None:
    if not catalog_path.exists():
        return None
    return dt.datetime.fromtimestamp(catalog_path.stat().st_mtime, tz=dt.timezone.utc).isoformat()


def inbound_counts(relations: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for relation in relations:
        target = relation.get("to")
        if target:
            counts[target] = counts.get(target, 0) + 1
    return counts


def latest_decisions(decisions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for record in decisions:
        entity = record.get("entity")
        if entity:
            latest[entity] = record
    return latest


def _confidence_allowed(value: str | None, filters: set[str]) -> bool:
    if not filters:
        return True
    return (value or "review") in filters


def _read_lock(lock_path: Path) -> dict[str, Any]:
    if not lock_path.is_file():
        return {"held": False}
    try:
        parts = lock_path.read_text(encoding="utf-8").strip().split()
        if len(parts) >= 3:
            pid = int(parts[0])
            host = parts[1]
            stale = host == socket.gethostname() and not pid_alive(pid)
            return {
                "held": True,
                "pid": pid,
                "host": host,
                "acquired_at": parts[2],
                "stale_or_corrupt": stale,
            }
    except (OSError, ValueError):
        pass
    return {"held": True, "stale_or_corrupt": True}


def _parse_dt(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _repo_run_records(catalog_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not catalog_dir.is_dir():
        return records
    for path in sorted(catalog_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        meta = payload.get("_meta") or {}
        run = meta.get("run") or {}
        cost = (run.get("cost") or {}).get("estimated_usd")
        records.append({
            "repo": path.stem,
            "extracted_at": meta.get("extracted_at"),
            "provider": meta.get("provider") or run.get("provider"),
            "model": meta.get("model") or run.get("model"),
            "effort": meta.get("effort") or run.get("effort"),
            "status": run.get("status") or ("ok" if not meta.get("validation_errors") else "validation_errors"),
            "cost": float(cost or 0.0),
            "duration_seconds": float(run.get("duration_seconds") or 0.0),
            "validation_errors": len(meta.get("validation_errors") or []),
            "evidence_quarantined": int(meta.get("evidence_quarantined") or 0),
        })
    return records


def _repo_record_dir(data_dir: Path) -> Path:
    """Return the directory that contains per-repo extraction records.

    Normal runtime crawls write to `data/catalog/`. The sock-shop eval harness
    historically writes to `evals/data/extractions/`; support both so the
    operator view works for a fresh eval clone and for the packaged app.
    """
    catalog_dir = data_dir / "catalog"
    if catalog_dir.is_dir() and any(catalog_dir.glob("*.json")):
        return catalog_dir
    extractions_dir = data_dir / "extractions"
    if extractions_dir.is_dir() and any(extractions_dir.glob("*.json")):
        return extractions_dir
    return catalog_dir


def _activity_run_id(repo: str) -> str:
    safe = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", repo.lower())).strip("-")
    return f"extraction-{safe or 'repo'}"


def _started_at_from_finish(finished_at: str | None, duration_seconds: float) -> str | None:
    finished = _parse_dt(finished_at)
    if not finished:
        return None
    return (finished - dt.timedelta(seconds=max(duration_seconds, 0.0))).isoformat()


def _extraction_activity_summary(record: dict[str, Any]) -> dict[str, Any]:
    finished_at = record.get("extracted_at")
    duration_seconds = float(record.get("duration_seconds") or 0.0)
    return {
        "run_id": _activity_run_id(str(record.get("repo") or "")),
        "trigger": "extraction",
        "started_at": _started_at_from_finish(finished_at, duration_seconds),
        "finished_at": finished_at,
        "status": record.get("status"),
        "repos_checked": 1,
        "repos_changed_count": 1,
        "budget_usd": None,
        "cost_usd": record.get("cost"),
        "duration_seconds": duration_seconds,
        "crawler_returncode": 0 if record.get("status") == "ok" else None,
    }


def _extraction_activity_runs(data_dir: Path, limit: int) -> list[dict[str, Any]]:
    rows = [_extraction_activity_summary(record) for record in _repo_run_records(_repo_record_dir(data_dir))]
    rows.sort(key=lambda row: row.get("finished_at") or row.get("started_at") or "", reverse=True)
    return rows[:limit]


def _extraction_activity_detail(data_dir: Path, run_id: str) -> dict[str, Any] | None:
    for record in _repo_run_records(_repo_record_dir(data_dir)):
        repo = str(record.get("repo") or "")
        if _activity_run_id(repo) != run_id:
            continue
        summary = _extraction_activity_summary(record)
        duration_seconds = float(summary.get("duration_seconds") or 0.0)
        cost = float(summary.get("cost_usd") or 0.0)
        return {
            **summary,
            "cost_usd": cost,
            "workspace_root": os.environ.get("WORKSPACE_ROOT") or "",
            "repos_changed": [{
                "repo": repo,
                "reason": "catalog extraction",
            }],
            "events": [
                {
                    "event": "repo_extracted",
                    "repo": repo,
                    "status": summary.get("status"),
                    "provider": record.get("provider"),
                    "model": record.get("model"),
                    "effort": record.get("effort"),
                    "duration_seconds": duration_seconds,
                    "cost_usd": cost,
                    "validation_errors": record.get("validation_errors"),
                    "evidence_quarantined": record.get("evidence_quarantined"),
                }
            ],
        }
    return None


TERMINAL_FACT_ACTIONS = {"mark_corrected", "accept_risk", "false_positive"}


def _fact_label(category: str, fact: dict[str, Any]) -> str:
    if category == "dependencies":
        source = fact.get("source") or "unknown"
        target = fact.get("target") or "unknown"
        kind = fact.get("kind") or "dependsOn"
        return f"{source} {kind} {target}"
    return str(fact.get("name") or fact.get("target") or fact.get("operation_or_usage") or category)


def _combined_fact_verdict(fact: dict[str, Any]) -> tuple[str | None, str]:
    phase_a = ((fact.get("_cross_check") or {}).get("verdict") or "").strip()
    phase_b = ((fact.get("_cross_check_ast") or {}).get("verdict") or "").strip()
    if phase_a == "disconfirmed":
        return "disconfirmed", "phase A: no evidence verified"
    if phase_a == "mixed":
        return "mixed", "phase A: partial evidence"
    if phase_a == "confirmed" and phase_b == "disconfirmed":
        return "mixed", "phase A confirmed citation but phase B pattern absent"
    return None, ""


def _fact_reasons(fact: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    phase_a = fact.get("_cross_check") or {}
    phase_b = fact.get("_cross_check_ast") or {}
    if phase_a.get("evidence_missing"):
        reasons.append(f"{phase_a['evidence_missing']} cited snippet(s) missing from file")
    if phase_a.get("evidence_invalid_path"):
        reasons.append(f"{phase_a['evidence_invalid_path']} evidence path(s) invalid")
    if phase_a.get("evidence_partial"):
        reasons.append(f"{phase_a['evidence_partial']} partial snippet match(es)")
    if phase_b.get("verdict") == "disconfirmed":
        reasons.append("code-shape verifier did not find the claimed dependency pattern")
    if not reasons:
        verdict, explanation = _combined_fact_verdict(fact)
        if verdict:
            reasons.append(explanation)
    return reasons


def _latest_fact_decisions(decisions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for record in decisions:
        if record.get("type") == "fact" and record.get("entity"):
            latest[str(record["entity"])] = record
    return latest


def disconfirmed_facts(catalog_dir: Path, decisions_path: Path, *, include_mixed: bool = True) -> list[dict[str, Any]]:
    decisions = _latest_fact_decisions(read_jsonl(decisions_path))
    rows: list[dict[str, Any]] = []
    if not catalog_dir.is_dir():
        return rows
    for path in sorted(catalog_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        repo = str(((payload.get("repo") or {}).get("id")) or path.stem)
        for category in ("components", "apis", "dependencies", "resources"):
            items = payload.get(category) or []
            if not isinstance(items, list):
                continue
            for index, fact in enumerate(items):
                if not isinstance(fact, dict):
                    continue
                verdict, explanation = _combined_fact_verdict(fact)
                if verdict == "mixed" and not include_mixed:
                    continue
                if verdict not in {"disconfirmed", "mixed"}:
                    continue
                fact_id = f"{repo}:{category}:{index}"
                decision = decisions.get(fact_id)
                if decision and decision.get("action") in TERMINAL_FACT_ACTIONS:
                    continue
                evidence = fact.get("evidence") or []
                rows.append({
                    "id": fact_id,
                    "repo": repo,
                    "category": category,
                    "index": index,
                    "label": _fact_label(category, fact),
                    "combined_verdict": verdict,
                    "explanation": explanation,
                    "phase_a_verdict": (fact.get("_cross_check") or {}).get("verdict"),
                    "phase_b_verdict": (fact.get("_cross_check_ast") or {}).get("verdict"),
                    "confidence": fact.get("confidence"),
                    "reasons": _fact_reasons(fact),
                    "evidence": evidence[:5] if isinstance(evidence, list) else [],
                    "owner": (decision or {}).get("owner") or "",
                    "due_date": (decision or {}).get("due_date") or "",
                    "last_action": (decision or {}).get("action") or "",
                    "current_fact": {
                        k: v for k, v in fact.items()
                        if k not in {"_cross_check", "_cross_check_ast", "evidence"}
                    },
                })
    rows.sort(key=lambda r: (
        0 if r["combined_verdict"] == "disconfirmed" else 1,
        r["owner"] != "",
        r["repo"].lower(),
        r["category"],
        r["index"],
    ))
    return rows


def _entity_brief(entity: dict[str, Any]) -> dict[str, Any]:
    meta = entity.get("metadata") or {}
    ann = meta.get("annotations") or {}
    spec = entity.get("spec") or {}
    return {
        "ref": f"{entity['kind']}:{meta.get('name','')}",
        "kind": entity["kind"],
        "name": meta.get("name", ""),
        "tagline": ann.get("tagline") or "",
        "description": meta.get("description") or "",
        "type": spec.get("type") or "",
        "system": spec.get("system") or "",
        "source_repos": ann.get("source_repos") or [],
        "environments": spec.get("environments") or [],
        "confidence": entity.get("confidence") or "review",
    }


# ---------- app factory ----------

def create_app(*, catalog_path: Path, extraction_log: Path, decisions_path: Path) -> FastAPI:
    app = FastAPI(title="ServiceScout dashboard")

    def scheduler_command(*, interval_minutes: int, budget_usd: float, trigger: str | None = None, force_repos: list[str] | None = None) -> list[str]:
        data_dir = catalog_path.parent.resolve()
        workspace_root = Path(os.environ.get("WORKSPACE_ROOT") or "/workspace").resolve()
        workspace_config = os.environ.get("SERVICESCOUT_WORKSPACE_CONFIG") or str(HERE / "workspace.json")
        provider = os.environ.get("LLM_PROVIDER") or "codex"
        effort = os.environ.get("LLM_EFFORT") or "medium"
        model = os.environ.get("LLM_MODEL")
        catalog_dir = _repo_record_dir(data_dir)
        cmd = [
            sys.executable, "-m", "servicescout.scheduler",
            "--workspace-root", str(workspace_root),
            "--catalog-dir", str(catalog_dir),
            "--workspace", workspace_config,
            "--run-log-dir", str(data_dir / "crawl_runs"),
            "--lock-path", str(data_dir / "crawl_lock"),
            "--interval-minutes", str(interval_minutes),
            "--budget-usd", str(budget_usd),
            "--provider", provider,
            "--effort", effort,
            "--crawler-arg=--reconcile",
            "--crawler-arg=--embed",
            "--crawler-arg=--build-kuzu",
            "--crawler-arg=--runtime-config",
            f"--crawler-arg={crawler_runtime_config_path(data_dir)}",
        ]
        if trigger:
            cmd.extend(["--once", "--trigger", trigger])
        if force_repos:
            cmd.append("--force-repos")
            cmd.extend(force_repos)
        if model:
            cmd.extend(["--model", model])
        return cmd

    @app.get("/api/state.json")
    def api_state() -> JSONResponse:
        catalog = load_catalog(catalog_path)
        summary = catalog.get("summary") or {}
        log_path, log_tail = latest_crawl_log()
        return JSONResponse({
            "backend": "kuzu" if (catalog_path.parent / "catalog.kuzu").exists() else "json",
            "summary": {
                "entities": summary.get("entities") or 0,
                "relations": summary.get("relations") or 0,
                "repos_indexed": summary.get("repos_indexed") or 0,
                "unresolved_external_components": summary.get("unresolved_external_components") or 0,
                "derived_communication_flows": summary.get("derived_communication_flows") or 0,
                "node_kinds": summary.get("node_kinds") or {},
                "relation_types": summary.get("relation_types") or {},
            },
            "last_build_at": last_build_timestamp(catalog_path),
            "cumulative_spend_usd": cumulative_spend(extraction_log),
            "recent_extractions": recent_extractions(extraction_log, 10),
            "crawler": crawler_status(),
            "log_path": str(log_path) if log_path else None,
            "log_tail": log_tail,
            "now": dt.datetime.now(dt.timezone.utc).isoformat(),
        })

    @app.get("/api/entities")
    def api_entities(
        kind: str | None = None,
        query: str | None = None,
        owner: list[str] | None = Query(default=None),
        lifecycle: list[str] | None = Query(default=None),
        environment: list[str] | None = Query(default=None),
        tag: list[str] | None = Query(default=None),
        runtime: list[str] | None = Query(default=None),
        confidence: list[str] | None = Query(default=None),
        limit: int = 500,
    ) -> JSONResponse:
        catalog = load_catalog(catalog_path)
        q = (query or "").lower().strip()
        owners_filter = set(owner or [])
        lifecycles_filter = set(lifecycle or [])
        envs_filter = set(environment or [])
        tags_filter = set(tag or [])
        runtimes_filter = set(runtime or [])
        confidence_filter = set(confidence or [])
        out: list[dict[str, Any]] = []
        for entity in catalog.get("entities") or []:
            if kind and entity.get("kind") != kind:
                continue
            if not _confidence_allowed(entity.get("confidence"), confidence_filter):
                continue
            spec = entity.get("spec") or {}
            meta = entity.get("metadata") or {}
            if owners_filter and (spec.get("owner") or "") not in owners_filter:
                continue
            if lifecycles_filter and (spec.get("lifecycle") or "") not in lifecycles_filter:
                continue
            if envs_filter and not envs_filter.intersection(spec.get("environments") or []):
                continue
            if tags_filter and not tags_filter.intersection(meta.get("tags") or []):
                continue
            if runtimes_filter and (spec.get("runtime") or "") not in runtimes_filter:
                continue
            brief = _entity_brief(entity)
            if q:
                ann = meta.get("annotations") or {}
                haystack = " ".join([
                    brief["name"].lower(),
                    brief["tagline"].lower(),
                    brief["description"].lower(),
                    " ".join(brief["source_repos"]).lower(),
                    " ".join(ann.get("aliases") or []).lower(),
                ])
                if q not in haystack:
                    continue
            out.append(brief)
            if len(out) >= limit:
                break
        return JSONResponse({"count": len(out), "entities": out})

    @app.get("/api/facets")
    def api_facets() -> JSONResponse:
        """Distinct values per facet for the Catalog page sidebar."""
        catalog = load_catalog(catalog_path)
        kinds_count: dict[str, int] = {}
        types_count: dict[str, int] = {}
        owners_count: dict[str, int] = {}
        lifecycles_count: dict[str, int] = {}
        envs_count: dict[str, int] = {}
        tags_count: dict[str, int] = {}
        runtimes_count: dict[str, int] = {}
        confidence_count: dict[str, int] = {}
        for entity in catalog.get("entities") or []:
            kind = entity.get("kind") or ""
            kinds_count[kind] = kinds_count.get(kind, 0) + 1
            confidence = entity.get("confidence") or "review"
            confidence_count[confidence] = confidence_count.get(confidence, 0) + 1
            spec = entity.get("spec") or {}
            meta = entity.get("metadata") or {}
            t = spec.get("type") or ""
            if t:
                types_count[t] = types_count.get(t, 0) + 1
            o = spec.get("owner") or ""
            if o and o != "unknown":
                owners_count[o] = owners_count.get(o, 0) + 1
            lc = spec.get("lifecycle") or ""
            if lc and lc != "unknown":
                lifecycles_count[lc] = lifecycles_count.get(lc, 0) + 1
            for env in spec.get("environments") or []:
                envs_count[env] = envs_count.get(env, 0) + 1
            for tg in meta.get("tags") or []:
                tags_count[tg] = tags_count.get(tg, 0) + 1
            rt = spec.get("runtime") or ""
            if rt:
                runtimes_count[rt] = runtimes_count.get(rt, 0) + 1
        def sorted_facet(d: dict[str, int]) -> list[dict[str, Any]]:
            return [{"value": k, "count": v} for k, v in sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))]
        return JSONResponse({
            "kind": sorted_facet(kinds_count),
            "type": sorted_facet(types_count),
            "owner": sorted_facet(owners_count),
            "lifecycle": sorted_facet(lifecycles_count),
            "environment": sorted_facet(envs_count),
            "tag": sorted_facet(tags_count),
            "runtime": sorted_facet(runtimes_count),
            "confidence": sorted_facet(confidence_count),
        })

    @app.get("/api/entity/{ref:path}")
    def api_entity(ref: str) -> JSONResponse:
        catalog = load_catalog(catalog_path)
        for entity in catalog.get("entities") or []:
            entity_ref = f"{entity['kind']}:{entity['metadata']['name']}"
            if entity_ref == ref:
                return JSONResponse({
                    "ref": entity_ref,
                    "kind": entity["kind"],
                    "name": entity["metadata"]["name"],
                    "metadata": entity.get("metadata") or {},
                    "spec": entity.get("spec") or {},
                    "evidence": entity.get("evidence") or [],
                    "confidence": entity.get("confidence"),
                })
        return JSONResponse({"error": "not_found", "ref": ref}, status_code=404)

    @app.get("/api/graph")
    def api_graph(
        kind: list[str] | None = Query(default=None),
        limit: int = 400,
        include_orphans: bool = False,
        center: str | None = None,
        depth: int = 1,
        edge_type: list[str] | None = Query(default=None),
        confidence: list[str] | None = Query(default=None),
    ) -> JSONResponse:
        """Return nodes + edges in a Sigma-friendly shape.

        - `kind` repeated → filter by entity kinds. Default: Component + Provider + Resource.
        - `limit` → cap to top-N nodes by total degree (in+out); `0` means no cap.
          Edges are pruned to those whose source and target are both in the kept set.
        - `include_orphans=False` (default) drops nodes with zero edges to/from another kept
          node — these otherwise form a useless visual halo in force-directed layouts.
        - `center` (optional) → return the ego-graph of this entity ref at `depth` hops.
          Overrides the kind filter (we include any kind in the ego-graph).
        """
        catalog = load_catalog(catalog_path)
        relations = catalog.get("relations") or []
        edge_types_filter = set(edge_type or [])
        confidence_filter = set(confidence or [])
        if edge_types_filter:
            relations = [r for r in relations if r.get("type") in edge_types_filter]
        if confidence_filter:
            relations = [r for r in relations if _confidence_allowed(r.get("confidence"), confidence_filter)]
        edge_total = len(relations)

        # Ego-graph mode: BFS from `center` for `depth` hops (any kind).
        if center:
            entities_by_ref = {f"{e['kind']}:{e['metadata']['name']}": e for e in (catalog.get("entities") or [])}
            adj_out: dict[str, list[dict[str, Any]]] = {}
            adj_in: dict[str, list[dict[str, Any]]] = {}
            for r in relations:
                adj_out.setdefault(r["from"], []).append(r)
                adj_in.setdefault(r["to"], []).append(r)
            visited: set[str] = set()
            frontier = [center]
            picked_edges: list[dict[str, Any]] = []
            seen_edges: set[tuple[str, str, str]] = set()
            for _ in range(max(depth, 1)):
                next_frontier: list[str] = []
                for node in frontier:
                    if node in visited:
                        continue
                    visited.add(node)
                    for r in adj_out.get(node, []) + adj_in.get(node, []):
                        key = (r["from"], r["type"], r["to"])
                        if key in seen_edges:
                            continue
                        seen_edges.add(key)
                        picked_edges.append(r)
                        other = r["to"] if r["from"] == node else r["from"]
                        if other not in visited:
                            next_frontier.append(other)
                frontier = next_frontier
            # Include the last-hop neighbours that we touched via edges even
            # though we didn't expand them — otherwise the returned subgraph
            # has edges pointing at nodes that aren't in the node list.
            for r in picked_edges:
                visited.add(r["from"])
                visited.add(r["to"])
            ego_entities = [entities_by_ref[r] for r in sorted(visited) if r in entities_by_ref]
            edges = []
            for i, relation in enumerate(picked_edges):
                edges.append({
                    "id": f"e{i}",
                    "source": relation["from"],
                    "target": relation["to"],
                    "type": relation["type"],
                    "confidence": relation.get("confidence"),
                    "properties": relation.get("properties") or {},
                })
            nodes = []
            for entity in ego_entities:
                ann = (entity.get("metadata") or {}).get("annotations") or {}
                ref = f"{entity['kind']}:{entity['metadata']['name']}"
                nodes.append({
                    "id": ref,
                    "label": entity["metadata"]["name"],
                    "kind": entity["kind"],
                    "system": (entity.get("spec") or {}).get("system") or "",
                    "tagline": ann.get("tagline") or "",
                    "confidence": entity.get("confidence") or "review",
                    "degree": sum(1 for e in picked_edges if e["from"] == ref or e["to"] == ref),
                    "is_center": ref == center,
                })
            return JSONResponse({
                "nodes": nodes,
                "edges": edges,
                "truncated": False,
                "node_total": len(nodes),
                "edge_total": len(edges),
                "include_orphans": True,
                "center": center,
                "depth": depth,
            })

        selected_kinds = list(dict.fromkeys(kind or ["Component", "Provider", "Resource"]))
        all_kinds = set(selected_kinds)
        entities = [e for e in (catalog.get("entities") or []) if e.get("kind") in all_kinds]
        node_total = len(entities)

        # Total degree (in + out), counted against entities of the visible kinds only.
        ref_for = lambda e: f"{e['kind']}:{e['metadata']['name']}"
        all_visible_refs = {ref_for(e) for e in entities}
        degree: dict[str, int] = {}
        for r in relations:
            if r["from"] in all_visible_refs and r["to"] in all_visible_refs:
                degree[r["from"]] = degree.get(r["from"], 0) + 1
                degree[r["to"]] = degree.get(r["to"], 0) + 1

        def entity_score(entity: dict[str, Any]) -> tuple[int, str, str]:
            ref = ref_for(entity)
            return (
                -degree.get(ref, 0),
                str(entity.get("metadata", {}).get("name") or "").lower(),
                ref,
            )

        kept = _balanced_entity_selection(
            entities,
            selected_kinds=selected_kinds,
            limit=limit,
            score_key=entity_score,
        )
        kept_refs = {ref_for(e) for e in kept}

        # Build edges between kept entities.
        edges: list[dict[str, Any]] = []
        connected: set[str] = set()
        for i, relation in enumerate(relations):
            if relation["from"] in kept_refs and relation["to"] in kept_refs:
                edges.append({
                    "id": f"e{i}",
                    "source": relation["from"],
                    "target": relation["to"],
                    "type": relation["type"],
                    "confidence": relation.get("confidence"),
                    "properties": relation.get("properties") or {},
                })
                connected.add(relation["from"])
                connected.add(relation["to"])

        # Drop orphans for a cleaner force-directed picture.
        if not include_orphans:
            kept = [e for e in kept if ref_for(e) in connected]

        nodes = []
        for entity in kept:
            ann = (entity.get("metadata") or {}).get("annotations") or {}
            nodes.append({
                "id": ref_for(entity),
                "label": entity["metadata"]["name"],
                "kind": entity["kind"],
                "system": (entity.get("spec") or {}).get("system") or "",
                "tagline": ann.get("tagline") or "",
                "confidence": entity.get("confidence") or "review",
                "degree": degree.get(ref_for(entity), 0),
            })
        return JSONResponse({
            "nodes": nodes,
            "edges": edges,
            "truncated": limit > 0 and len(kept) < node_total,
            "node_total": node_total,
            "edge_total": edge_total,
            "include_orphans": include_orphans,
        })

    @app.get("/api/operator/summary")
    def operator_summary() -> JSONResponse:
        catalog = load_catalog(catalog_path)
        catalog_dir = _repo_record_dir(catalog_path.parent)
        repo_records = _repo_run_records(catalog_dir)
        now = dt.datetime.now(dt.timezone.utc)

        cost_by_day: dict[str, dict[str, Any]] = {}
        stale_buckets = {"fresh": 0, "warm": 0, "aging": 0, "stale": 0, "unknown": 0}
        stale_queue: list[dict[str, Any]] = []
        verifier = {
            "validation_errors": 0,
            "evidence_quarantined": 0,
            "repos_with_validation_errors": 0,
            "repos_with_quarantined_evidence": 0,
        }
        for record in repo_records:
            extracted = _parse_dt(record.get("extracted_at"))
            if extracted:
                day = extracted.date().isoformat()
                day_row = cost_by_day.setdefault(day, {"day": day, "cost": 0.0, "repos": 0, "duration_seconds": 0.0})
                day_row["cost"] += record["cost"]
                day_row["repos"] += 1
                day_row["duration_seconds"] += record["duration_seconds"]
                age_hours = max((now - extracted).total_seconds() / 3600, 0)
                if age_hours <= 24:
                    bucket = "fresh"
                elif age_hours <= 72:
                    bucket = "warm"
                elif age_hours <= 168:
                    bucket = "aging"
                else:
                    bucket = "stale"
                stale_buckets[bucket] += 1
                if bucket == "stale":
                    stale_queue.append({
                        "repo": record["repo"],
                        "extracted_at": record["extracted_at"],
                        "age_hours": round(age_hours, 1),
                        "cost": record["cost"],
                        "status": record["status"],
                    })
            else:
                stale_buckets["unknown"] += 1
                stale_queue.append({
                    "repo": record["repo"],
                    "extracted_at": None,
                    "age_hours": None,
                    "cost": record["cost"],
                    "status": record["status"],
                })
            verifier["validation_errors"] += record["validation_errors"]
            verifier["evidence_quarantined"] += record["evidence_quarantined"]
            if record["validation_errors"]:
                verifier["repos_with_validation_errors"] += 1
            if record["evidence_quarantined"]:
                verifier["repos_with_quarantined_evidence"] += 1

        confidence_entities: dict[str, int] = {}
        for entity in catalog.get("entities") or []:
            value = entity.get("confidence") or "review"
            confidence_entities[value] = confidence_entities.get(value, 0) + 1
        confidence_relations: dict[str, int] = {}
        for relation in catalog.get("relations") or []:
            value = relation.get("confidence") or "review"
            confidence_relations[value] = confidence_relations.get(value, 0) + 1

        stale_queue.sort(key=lambda r: (r["age_hours"] is None, -(r["age_hours"] or 0), r["repo"]))
        trend = list(cost_by_day.values())
        trend.sort(key=lambda row: row["day"])
        for row in trend:
            row["cost"] = round(row["cost"], 4)
            row["duration_seconds"] = round(row["duration_seconds"], 1)

        return JSONResponse({
            "catalog": {
                "last_build_at": last_build_timestamp(catalog_path),
                "repos_indexed": (catalog.get("summary") or {}).get("repos_indexed") or len(repo_records),
                "entities": (catalog.get("summary") or {}).get("entities") or len(catalog.get("entities") or []),
                "relations": (catalog.get("summary") or {}).get("relations") or len(catalog.get("relations") or []),
            },
            "cost_trend": trend[-30:],
            "staleness": {
                "buckets": stale_buckets,
                "repos": stale_queue,
                "repo_total": len(stale_queue),
            },
            "verifier": {
                **verifier,
                "entity_confidence": confidence_entities,
                "relation_confidence": confidence_relations,
            },
        })

    @app.get("/api/communications")
    def api_communications(limit: int = 500, transport: str | None = None) -> JSONResponse:
        catalog = load_catalog(catalog_path)
        entities_by_ref = {
            f"{e['kind']}:{e['metadata']['name']}": e
            for e in (catalog.get("entities") or [])
            if e.get("kind") and e.get("metadata", {}).get("name")
        }
        flows: list[dict[str, Any]] = []
        for relation in catalog.get("relations") or []:
            if relation.get("type") != "communicatesWith":
                continue
            props = relation.get("properties") or {}
            transports = [str(t) for t in (props.get("transports") or []) if t]
            primary_transport = props.get("transport") or (transports[0] if transports else "")
            if transport and transport.lower() not in {t.lower() for t in transports + [primary_transport]}:
                continue
            source_entity = entities_by_ref.get(relation.get("from"))
            target_entity = entities_by_ref.get(relation.get("to"))
            flows.append({
                "source": relation.get("from"),
                "source_name": ((source_entity or {}).get("metadata") or {}).get("name") or relation.get("from"),
                "target": relation.get("to"),
                "target_name": ((target_entity or {}).get("metadata") or {}).get("name") or relation.get("to"),
                "endpoint": props.get("endpoint") or "",
                "endpoints": props.get("endpoints") or [],
                "transport": primary_transport,
                "transports": transports,
                "mechanism": props.get("mechanism") or "",
                "mechanisms": props.get("mechanisms") or [],
                "confidence": relation.get("confidence"),
                "evidence_count": len(relation.get("evidence") or []),
            })
        flows.sort(key=lambda f: (f["source_name"], f["target_name"], f["endpoint"]))
        return JSONResponse({"flows": flows[:limit], "count": len(flows), "limit": limit})

    @app.get("/api/triage.json")
    def api_triage() -> JSONResponse:
        catalog = load_catalog(catalog_path)
        decisions = read_jsonl(decisions_path)
        decided = latest_decisions(decisions)
        inbound = inbound_counts(catalog.get("relations") or [])
        out: list[dict[str, Any]] = []
        for entity in catalog.get("entities") or []:
            if entity.get("kind") != "Component":
                continue
            annotations = (entity.get("metadata") or {}).get("annotations") or {}
            if annotations.get("external") != "true":
                continue
            ref = f"Component:{entity['metadata']['name']}"
            if ref in decided:
                continue
            out.append({
                "ref": ref,
                "name": entity["metadata"]["name"],
                "aliases": annotations.get("aliases") or [],
                "inbound": inbound.get(ref, 0),
                "evidence": entity.get("evidence") or [],
            })
        out.sort(key=lambda r: (-r["inbound"], r["name"].lower()))
        return JSONResponse({"count": len(out), "components": out})

    @app.get("/api/triage/facts")
    def triage_facts(include_mixed: bool = True) -> JSONResponse:
        rows = disconfirmed_facts(catalog_path.parent / "catalog", decisions_path, include_mixed=include_mixed)
        assigned = sum(1 for row in rows if row.get("owner"))
        return JSONResponse({
            "count": len(rows),
            "assigned": assigned,
            "unassigned": len(rows) - assigned,
            "facts": rows,
        })

    @app.get("/api/crawl/runs")
    def crawl_runs(limit: int = Query(default=50, ge=1, le=500)) -> JSONResponse:
        """List recent scheduler runs.

        Each entry is the summary of a scheduler tick: trigger, start /
        finish timestamps, status, repos_changed count, and the run_id
        that points at the full event log (see /api/crawl/runs/<id>).
        """
        run_log_dir = (catalog_path.parent / "crawl_runs").resolve()
        if not run_log_dir.is_dir():
            runs = _extraction_activity_runs(catalog_path.parent, limit)
            return JSONResponse({"runs": runs, "total": len(runs), "source": "extractions"})
        repo_record_dir = _repo_record_dir(catalog_path.parent)
        entries: list[dict[str, Any]] = []
        for path in sorted(run_log_dir.glob("*.json"), reverse=True):
            try:
                doc = _enrich_activity_run_doc(json.loads(path.read_text(encoding="utf-8")), repo_record_dir)
            except (OSError, json.JSONDecodeError):
                continue
            entries.append(_activity_run_summary(doc, path))
            if len(entries) >= limit:
                break
        if not entries:
            entries = _extraction_activity_runs(catalog_path.parent, limit)
            return JSONResponse({"runs": entries, "total": len(entries), "source": "extractions"})
        return JSONResponse({"runs": entries, "total": len(entries), "source": "activity"})

    @app.get("/api/crawl/runs/{run_id}")
    def crawl_run_detail(run_id: str) -> JSONResponse:
        """Full event stream for one scheduler tick.

        Returns the raw run-log document including the full repos_changed
        list, per-event timeline, crawler stdout/stderr tails, and final
        status. Used by the dashboard's Activity drawer.
        """
        # Normalise to prevent path traversal.
        safe = "".join(c for c in run_id if c.isalnum() or c in "-_")
        if not safe:
            return JSONResponse({"error": "invalid_run_id"}, status_code=400)
        path = (catalog_path.parent / "crawl_runs" / f"{safe}.json").resolve()
        runs_dir = (catalog_path.parent / "crawl_runs").resolve()
        try:
            path.relative_to(runs_dir)
        except ValueError:
            return JSONResponse({"error": "invalid_run_id"}, status_code=400)
        if not path.is_file():
            detail = _extraction_activity_detail(catalog_path.parent, safe)
            if detail:
                return JSONResponse(detail)
            return JSONResponse({"error": "not_found", "run_id": safe}, status_code=404)
        try:
            doc = _enrich_activity_run_doc(
                json.loads(path.read_text(encoding="utf-8")),
                _repo_record_dir(catalog_path.parent),
            )
        except (OSError, json.JSONDecodeError):
            return JSONResponse({"error": "corrupt"}, status_code=500)
        return JSONResponse(doc)

    @app.get("/api/crawl/status")
    def crawl_status() -> JSONResponse:
        """Current scheduler state.

        Returns whether a tick is currently running (lock file present
        and PID alive), the most recent run summary, and the interval
        / budget configured.
        """
        lock_path = (catalog_path.parent / "crawl_lock").resolve()
        lock_info = _read_lock(lock_path)
        trigger_log = (catalog_path.parent / "crawl_trigger.log").resolve()
        data_dir = catalog_path.parent.resolve()
        managed = managed_scheduler_status(data_dir)
        external_scheduler = scheduler_status()
        if managed.get("running"):
            scheduler_payload = {**managed, "source": "dashboard"}
        elif external_scheduler.get("running"):
            scheduler_payload = {
                **managed,
                **external_scheduler,
                "source": "external",
                "managed": False,
            }
        else:
            scheduler_payload = {**managed, "source": "stopped"}
        run_log_dir = (catalog_path.parent / "crawl_runs").resolve()
        last_run = None
        if run_log_dir.is_dir():
            paths = sorted(run_log_dir.glob("*.json"), reverse=True)
            if paths:
                try:
                    doc = _enrich_activity_run_doc(
                        json.loads(paths[0].read_text(encoding="utf-8")),
                        _repo_record_dir(catalog_path.parent),
                    )
                    last_run = _activity_run_summary(doc, paths[0])
                except (OSError, json.JSONDecodeError):
                    pass
        if last_run is None:
            extraction_runs = _extraction_activity_runs(catalog_path.parent, 1)
            if extraction_runs:
                last_run = extraction_runs[0]
        active_log_path = str(trigger_log) if trigger_log.is_file() else None
        active_log_lines = read_lines(trigger_log)
        active_log_tail = active_log_lines[-2000:]
        if not active_log_lines:
            activity_log_path, activity_log_tail = latest_activity_log(data_dir, 2000)
            if activity_log_path:
                active_log_path = str(activity_log_path)
                active_log_lines = activity_log_tail
        active_run = _active_run_doc(
            data_dir=data_dir,
            catalog_dir=_repo_record_dir(catalog_path.parent),
            lock_info=lock_info,
            scheduler_payload=scheduler_payload,
            active_log_lines=active_log_lines,
        )
        return JSONResponse({
            "lock": lock_info,
            "scheduler": scheduler_payload,
            "crawler": crawler_status(),
            "active_run": active_run,
            "last_run": last_run,
            "interval_minutes": int(scheduler_payload.get("interval_minutes") or 360),
            "budget_usd": float(scheduler_payload.get("budget_usd") or 20.0),
            "workspace_root": os.environ.get("WORKSPACE_ROOT") or "",
            "workspace_config": os.environ.get("SERVICESCOUT_WORKSPACE_CONFIG") or str(HERE / "workspace.json"),
            "crawler_runtime": read_crawler_runtime_config(data_dir),
            "active_log_path": active_log_path,
            "active_log_tail": active_log_tail,
        })

    @app.post("/api/crawl/runtime")
    def update_crawler_runtime(body: CrawlerRuntimeBody) -> JSONResponse:
        """Update runtime crawler tuning for the next batch."""
        data_dir = catalog_path.parent.resolve()
        current = read_crawler_runtime_config(data_dir)
        parallelism = body.parallelism if body.parallelism is not None else int(current["parallelism"])
        batch_size = body.batch_size if body.batch_size is not None else int(current["batch_size"])
        return JSONResponse(write_crawler_runtime_config(
            data_dir,
            parallelism=parallelism,
            batch_size=batch_size,
        ))

    @app.post("/api/crawl/scheduler/start")
    def start_scheduler(
        interval_minutes: int = Form(360),
        budget_usd: float = Form(20.0),
    ) -> JSONResponse:
        """Start or update the dashboard-managed scheduler daemon."""
        data_dir = catalog_path.parent.resolve()
        interval_minutes = max(1, min(int(interval_minutes), 10080))
        budget_usd = max(0.01, min(float(budget_usd), 10000.0))

        managed = managed_scheduler_status(data_dir)
        managed_was_running = bool(managed.get("running"))
        external = scheduler_status() if not managed_was_running else {"running": False, "pid": None}
        if external.get("running"):
            return JSONResponse({
                "error": "external_scheduler_running",
                "scheduler": external,
                "detail": "A scheduler process is already running outside the dashboard controller.",
            }, status_code=409)

        workspace_root = Path(os.environ.get("WORKSPACE_ROOT") or "/workspace").resolve()
        if not workspace_root.is_dir():
            return JSONResponse({
                "error": "workspace_root_missing",
                "workspace_root": str(workspace_root),
                "setup_hint": "Set WORKSPACE_ROOT and mount it into the dashboard container.",
            }, status_code=400)

        paths = scheduler_control_paths(data_dir)
        write_scheduler_settings(data_dir, interval_minutes=interval_minutes, budget_usd=budget_usd)
        if managed.get("running"):
            try:
                os.kill(int(managed["pid"]), signal.SIGTERM)
            except (OSError, TypeError, ValueError):
                pass
            try:
                paths["pid"].unlink()
            except OSError:
                pass
        paths["log"].parent.mkdir(parents=True, exist_ok=True)
        cmd = scheduler_command(interval_minutes=interval_minutes, budget_usd=budget_usd)
        try:
            with paths["log"].open("ab") as fh:
                proc = subprocess.Popen(cmd, cwd=str(HERE), stdout=fh, stderr=subprocess.STDOUT)
            paths["pid"].write_text(str(proc.pid), encoding="utf-8")
        except OSError as exc:
            return JSONResponse({"error": "scheduler_start_failed", "detail": str(exc)}, status_code=500)
        return JSONResponse({"status": "started", "scheduler": managed_scheduler_status(data_dir)}, status_code=202)

    @app.post("/api/crawl/scheduler/stop")
    def stop_scheduler() -> JSONResponse:
        """Pause the dashboard-managed scheduler daemon."""
        data_dir = catalog_path.parent.resolve()
        paths = scheduler_control_paths(data_dir)
        managed = managed_scheduler_status(data_dir)
        pid = managed.get("pid")
        if pid:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except OSError:
                pass
        try:
            if paths["pid"].exists():
                paths["pid"].unlink()
        except OSError:
            pass
        stopped = managed_scheduler_status(data_dir)
        return JSONResponse({"status": "stopped", "scheduler": stopped})

    @app.post("/api/crawl/trigger")
    def trigger_crawl() -> JSONResponse:
        """Start one scheduler tick from the dashboard.

        The work runs out-of-process and writes the same run-log JSON as the
        daemon scheduler. This keeps the HTTP request short and makes the
        Activity page the source of truth for progress.
        """
        data_dir = catalog_path.parent.resolve()
        lock_path = (data_dir / "crawl_lock").resolve()
        lock_info = _read_lock(lock_path)
        if lock_info.get("held") and not lock_info.get("stale_or_corrupt"):
            return JSONResponse({"error": "crawl_busy", "lock": lock_info}, status_code=409)

        workspace_root = Path(os.environ.get("WORKSPACE_ROOT") or "/workspace").resolve()
        if not workspace_root.is_dir():
            return JSONResponse({
                "error": "workspace_root_missing",
                "workspace_root": str(workspace_root),
                "setup_hint": "Set WORKSPACE_ROOT and mount it into the dashboard container.",
            }, status_code=400)

        settings = read_scheduler_settings(data_dir)
        log_path = data_dir / "crawl_trigger.log"
        cmd = scheduler_command(
            interval_minutes=int(settings.get("interval_minutes") or 360),
            budget_usd=float(settings.get("budget_usd") or 20.0),
            trigger="manual",
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with log_path.open("ab") as fh:
                proc = subprocess.Popen(cmd, cwd=str(HERE), stdout=fh, stderr=subprocess.STDOUT)
        except OSError as exc:
            return JSONResponse({"error": "trigger_failed", "detail": str(exc)}, status_code=500)
        _audit(data_dir, "crawl_triggered", trigger="manual", pid=proc.pid)
        return JSONResponse({
            "status": "accepted",
            "pid": proc.pid,
            "log_path": str(log_path),
            "workspace_root": str(workspace_root),
        }, status_code=202)

    def start_forced_repo_crawl(repos: list[str]) -> JSONResponse:
        """Force extraction of selected repo-units in one scheduler tick."""
        unique_repos = list(dict.fromkeys(repo.strip() for repo in repos if repo.strip()))
        if not unique_repos:
            return JSONResponse({"error": "repo_required"}, status_code=400)
        if len(unique_repos) > 250:
            return JSONResponse({"error": "too_many_repos", "limit": 250}, status_code=400)

        data_dir = catalog_path.parent.resolve()
        lock_path = (data_dir / "crawl_lock").resolve()
        lock_info = _read_lock(lock_path)
        if lock_info.get("held") and not lock_info.get("stale_or_corrupt"):
            return JSONResponse({"error": "crawl_busy", "lock": lock_info}, status_code=409)

        workspace_root = Path(os.environ.get("WORKSPACE_ROOT") or "/workspace").resolve()
        if not workspace_root.is_dir():
            return JSONResponse({
                "error": "workspace_root_missing",
                "workspace_root": str(workspace_root),
                "setup_hint": "Set WORKSPACE_ROOT and mount it into the dashboard container.",
            }, status_code=400)

        settings = read_scheduler_settings(data_dir)
        log_path = data_dir / "crawl_trigger.log"
        cmd = scheduler_command(
            interval_minutes=int(settings.get("interval_minutes") or 360),
            budget_usd=float(settings.get("budget_usd") or 20.0),
            trigger="manual-reindex",
            force_repos=unique_repos,
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with log_path.open("ab") as fh:
                proc = subprocess.Popen(cmd, cwd=str(HERE), stdout=fh, stderr=subprocess.STDOUT)
        except OSError as exc:
            return JSONResponse({"error": "trigger_failed", "detail": str(exc)}, status_code=500)
        _audit(
            data_dir,
            "crawl_triggered_repo" if len(unique_repos) == 1 else "crawl_triggered_repos",
            trigger="manual-reindex",
            repo=unique_repos[0] if len(unique_repos) == 1 else None,
            repos=unique_repos,
            pid=proc.pid,
        )
        return JSONResponse({
            "status": "accepted",
            "pid": proc.pid,
            "repo": unique_repos[0] if len(unique_repos) == 1 else None,
            "repos": unique_repos,
            "log_path": str(log_path),
            "workspace_root": str(workspace_root),
        }, status_code=202)

    @app.post("/api/crawl/trigger/repo")
    def trigger_repo_crawl(repo: str = Query(..., min_length=1)) -> JSONResponse:
        """Force extraction of one repo/repo-unit from the dashboard."""
        return start_forced_repo_crawl([repo])

    @app.post("/api/crawl/trigger/repos")
    def trigger_repos_crawl(body: RepoReindexBody) -> JSONResponse:
        """Force extraction of multiple repo-units in a single Activity run."""
        return start_forced_repo_crawl(body.repos)

    @app.get("/api/triage/decisions")
    def triage_decisions(limit: int = Query(default=50, ge=1, le=500)) -> JSONResponse:
        decisions = read_jsonl(decisions_path)
        rows = decisions[-limit:][::-1]
        return JSONResponse({"decisions": rows, "total": len(decisions)})

    @app.post("/api/triage/facts/decide")
    def decide_fact(
        fact_id: str = Form(...), action: str = Form(...),
        owner: str = Form(""), due_date: str = Form(""),
        reviewer: str = Form(""), reason: str = Form(""),
    ) -> JSONResponse:
        if action not in {"assign_owner", "mark_corrected", "accept_risk", "false_positive"}:
            return JSONResponse({"error": "invalid_action"}, status_code=400)
        record: dict[str, Any] = {
            "type": "fact",
            "entity": fact_id,
            "action": action,
            "owner": owner.strip(),
            "due_date": due_date.strip(),
            "reviewer": reviewer.strip(),
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        if reason.strip():
            record["reason"] = reason.strip()
        append_jsonl(decisions_path, record)
        return JSONResponse({"status": "ok", "decision": record})

    @app.post("/triage/decide")
    def decide(
        entity: str = Form(...), action: str = Form(...),
        category: str = Form(""), repo: str = Form(""),
        into: str = Form(""), reviewer: str = Form(""), reason: str = Form(""),
    ) -> RedirectResponse:
        record: dict[str, Any] = {
            "entity": entity, "action": action, "reviewer": reviewer.strip(),
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        if reason.strip():
            record["reason"] = reason.strip()
        if action == "mark_external":
            record["category"] = category.strip() or "other"
        elif action == "link":
            record["repo"] = repo.strip()
        elif action == "merge":
            record["into"] = into.strip()
        elif action == "skip":
            pass
        else:
            return RedirectResponse(url="/triage", status_code=303)
        append_jsonl(decisions_path, record)
        return RedirectResponse(url="/triage", status_code=303)

    # ---- onboarding: GitHub PAT -> orgs/repos, workspace config (Phases 2-3) ----
    @app.post("/api/github/validate")
    def github_validate(body: GithubTokenBody) -> JSONResponse:
        """Validate a PAT and return the orgs it can read (for the picker)."""
        try:
            result = github_client.validate_token(body.token)
        except github_client.GithubError as exc:
            return JSONResponse({"error": "github_error", "detail": exc.message}, status_code=exc.status or 502)
        return JSONResponse(result)

    @app.post("/api/github/repos")
    def github_repos(body: GithubReposBody) -> JSONResponse:
        """List an org's non-archived repos, for journey-seed selection."""
        try:
            repos = github_client.list_org_repos(body.token, body.org)
        except github_client.GithubError as exc:
            return JSONResponse({"error": "github_error", "detail": exc.message}, status_code=exc.status or 502)
        return JSONResponse({"org": body.org, "repos": repos})

    @app.get("/api/workspace/config")
    def get_workspace_config() -> JSONResponse:
        cfg = load_workspace_config(_workspace_config_path())
        settings = read_scheduler_settings(catalog_path.parent.resolve())
        return JSONResponse({
            "orgs": cfg.get("orgs") or [],
            "seeds": cfg.get("seeds") or [],
            "scope": cfg.get("scope") or {},
            "budget_usd": settings.get("budget_usd"),
            "config_path": str(_workspace_config_path()),
        })

    @app.post("/api/workspace/config")
    def save_workspace_config(body: WorkspaceConfigBody) -> JSONResponse:
        cfg_path = _workspace_config_path()
        before = load_workspace_config(cfg_path)
        scope = dict(body.scope or {})
        if body.discover is not None:
            scope["discover"] = bool(body.discover)
        if body.max_discovery_rounds is not None:
            scope["max_discovery_rounds"] = int(body.max_discovery_rounds)
        saved = write_workspace_config(cfg_path, orgs=body.orgs, seeds=body.seeds, scope=scope)
        data_dir = catalog_path.parent.resolve()
        if body.budget_usd is not None and body.budget_usd > 0:
            settings = read_scheduler_settings(data_dir)
            write_scheduler_settings(
                data_dir,
                interval_minutes=int(settings.get("interval_minutes") or 360),
                budget_usd=float(body.budget_usd),
            )
        token_stored = _persist_github_token(body.token) if body.token else False
        _audit(
            data_dir, "workspace_config_saved",
            before={"orgs": before.get("orgs"), "seeds": before.get("seeds"), "scope": before.get("scope")},
            after={"orgs": saved.get("orgs"), "seeds": saved.get("seeds"), "scope": saved.get("scope")},
            budget_usd=body.budget_usd,
            token_stored=token_stored,  # the fact, never the token
        )
        return JSONResponse({
            "saved": {"orgs": saved.get("orgs"), "seeds": saved.get("seeds"), "scope": saved.get("scope")},
            "config_path": str(cfg_path),
            "token_stored": token_stored,
        })

    @app.get("/api/audit")
    def audit_log(limit: int = Query(default=50, ge=1, le=500)) -> JSONResponse:
        records = read_jsonl(catalog_path.parent.resolve() / "audit.jsonl")
        return JSONResponse({"entries": records[-limit:][::-1], "count": len(records)})

    # ---- frontend (React SPA) ----
    if FRONTEND_DIST.exists() and (FRONTEND_DIST / "index.html").exists():
        # Static files (JS/CSS/etc.) under /assets.
        app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")

        @app.get("/app-logo.png", response_model=None)
        @app.get("/favicon.png", response_model=None)
        @app.get("/favicon.svg", response_model=None)
        @app.get("/icons.svg", response_model=None)
        def frontend_root_asset(request: Request):
            asset_path = FRONTEND_DIST / request.url.path.lstrip("/")
            return FileResponse(str(asset_path))

        @app.get("/", response_model=None)
        @app.get("/{path:path}", response_model=None)
        def spa(request: Request, path: str = ""):
            # Don't intercept API paths — FastAPI dispatches matching routes
            # before falling through to this catch-all.
            if request.url.path.startswith("/api/") or request.url.path.startswith("/triage/"):
                return JSONResponse({"error": "not_found"}, status_code=404)
            return FileResponse(str(FRONTEND_DIST / "index.html"))
    else:
        @app.get("/", response_class=HTMLResponse)
        def missing_frontend() -> HTMLResponse:
            return HTMLResponse(
                """<html><body style="font-family: ui-sans-serif, system-ui; padding: 2rem; max-width: 600px;">
                <h1>ServiceScout dashboard</h1>
                <p>The React frontend hasn't been built yet.</p>
                <pre style="background: #f4f4f5; padding: 1rem; border-radius: 6px;">
cd frontend && npm install && npm run build</pre>
                <p>Then reload. The JSON APIs at <code>/api/*</code> are already live.</p>
                </body></html>""",
                status_code=200,
            )

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--extraction-log", type=Path, default=DEFAULT_EXTRACTION_LOG)
    parser.add_argument("--decisions", type=Path, default=DEFAULT_DECISIONS)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()

    import uvicorn
    app = create_app(catalog_path=args.catalog, extraction_log=args.extraction_log, decisions_path=args.decisions)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
