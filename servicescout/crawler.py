"""Recursive crawl: discover repos, extract stale, rebuild catalog, embed, repeat.

Single entry point for the full pipeline. Stops when the frontier is empty,
the depth budget is exhausted, the USD budget is hit, or a max wall-clock is
reached. Stateless across runs apart from data/catalog/*.json files and the
extraction ledger.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from servicescout.repo_discovery import find_repos, load_workspace_config
from servicescout.build_catalog import build as build_catalog, canonical_key, host_to_key


HERE = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG_DIR = HERE / "data" / "catalog"
DEFAULT_CATALOG = HERE / "data" / "catalog.json"
DEFAULT_STATE = HERE / "data" / "crawler_state.json"


def is_stale(repo: dict[str, Any], catalog_dir: Path, max_age_hours: float) -> str | None:
    name = repo["name"]
    path = catalog_dir / f"{name}.json"
    if not path.exists():
        return "missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "corrupt"
    meta = payload.get("_meta", {})
    if meta.get("commit") and repo["commit"] and meta["commit"] != repo["commit"]:
        return "commit_changed"
    extracted_at = meta.get("extracted_at")
    if not extracted_at:
        return "no_timestamp"
    try:
        when = dt.datetime.fromisoformat(extracted_at)
    except ValueError:
        return "bad_timestamp"
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    age = dt.datetime.now(dt.timezone.utc) - when
    if age.total_seconds() > max_age_hours * 3600:
        return "stale"
    return None


def repo_total_cost(catalog_dir: Path) -> float:
    total = 0.0
    for path in catalog_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        cost = (((payload.get("_meta") or {}).get("run") or {}).get("cost") or {}).get("estimated_usd")
        if isinstance(cost, (int, float)):
            total += float(cost)
    return round(total, 4)


def run_extractor(
    repo: dict[str, Any],
    *,
    provider: str,
    model: str | None,
    effort: str,
    timeout_seconds: int,
    catalog_dir: Path,
    workspace_path: Path,
    stream_logs: bool,
) -> dict[str, Any]:
    cmd = [
        sys.executable,
        "-m",
        "servicescout.extractor",
        repo["name"],
        "--root",
        str(Path(repo["absolute_path"]).parent),
        "--provider",
        provider,
        "--effort",
        effort,
        "--timeout-seconds",
        str(timeout_seconds),
        "--output-dir",
        str(catalog_dir),
        "--workspace",
        str(workspace_path),
    ]
    if model:
        cmd.extend(["--model", model])
    if stream_logs:
        cmd.append("--stream-logs")
    started = time.monotonic()
    completed = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    duration = round(time.monotonic() - started, 2)
    payload: dict[str, Any] = {"stdout_tail": completed.stdout[-2000:]}
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith("EXTRACTOR_RESULT "):
            try:
                payload = json.loads(line[len("EXTRACTOR_RESULT "):])
            except json.JSONDecodeError:
                pass
            break
    payload["repo"] = repo["id"]
    payload["returncode"] = completed.returncode
    payload["duration_seconds"] = duration
    return payload


def emit(event: dict[str, Any]) -> None:
    print(json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat(), **event}, sort_keys=True), flush=True)


def list_org_repos(org: str, timeout_seconds: int = 60) -> list[dict[str, str]]:
    """List all repos in a GitHub org via `gh repo list`."""
    cmd = ["gh", "repo", "list", org, "--json", "name,nameWithOwner,description,isArchived", "--limit", "1000"]
    try:
        result = subprocess.run(cmd, text=True, capture_output=True, check=False, timeout=timeout_seconds)
        if result.returncode != 0:
            emit({"event": "gh_repo_list_failed", "org": org, "stderr_tail": result.stderr[-300:]})
            return []
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        emit({"event": "gh_repo_list_error", "org": org, "error": str(exc)})
        return []


def build_org_repo_index(orgs: list[str]) -> dict[str, str]:
    """Return {canonical_key(repo_name): 'org/name'} across all configured orgs.
    Skips archived repos.
    """
    index: dict[str, str] = {}
    for org in orgs:
        repos = list_org_repos(org)
        for repo in repos:
            if repo.get("isArchived"):
                continue
            name = repo.get("name") or ""
            full = repo.get("nameWithOwner") or ""
            if not name or not full:
                continue
            key = canonical_key(name)
            if key and key not in index:
                index[key] = full
        emit({"event": "org_scanned", "org": org, "repos_found": len(repos)})
    return index


def find_missing_repos(
    catalog_path: Path,
    org_repo_index: dict[str, str],
    cloned_ids: set[str],
    endpoint_role_suffixes: list[str] | None = None,
) -> list[dict[str, str]]:
    """Identify catalog hints that match uncloned repos in the configured orgs.

    Component names/aliases are the highest-confidence hints. Communication
    endpoints (API names, route paths, queue/topic/stream/resource names) are
    also useful for recursive discovery because a consumer may reveal the
    missing producer before the producer repo has been cloned.
    """
    if not catalog_path.exists():
        return []
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    candidates: dict[str, dict[str, str]] = {}

    def add_candidate(label: str, *, matched_via: str, matched_from: str, allow_namespace_roles: bool = False) -> None:
        if not label:
            return
        keys = [(canonical_key(label), "canonical"), (host_to_key(label), "host")]
        keys.extend(
            (key, source)
            for key, source in communication_endpoint_keys(
                label,
                allow_namespace_roles=allow_namespace_roles,
                role_suffixes=endpoint_role_suffixes or [],
            )
        )
        for key, key_source in keys:
            if not key:
                continue
            match = org_repo_index.get(key)
            if match and match not in cloned_ids and match not in candidates:
                candidates[match] = {
                    "repo": match,
                    "matched_label": label,
                    "matched_via": matched_via,
                    "matched_from": matched_from,
                    "matched_key": key,
                    "matched_key_source": key_source,
                }
                return

    for entity in catalog.get("entities") or []:
        kind = entity.get("kind")
        meta = entity.get("metadata", {})
        annotations = meta.get("annotations", {})
        name = meta.get("name") or ""
        ref = f"{kind}:{name}" if kind and name else name
        if kind == "Component":
            is_external = annotations.get("external") == "true"
            already_resolved = bool(annotations.get("source_repos"))
            if not is_external and already_resolved:
                continue
            candidate_labels = [name] + list(annotations.get("aliases") or [])
            for label in candidate_labels:
                add_candidate(label, matched_via=name, matched_from="component_identity")
        elif kind == "API":
            add_candidate(name, matched_via=ref, matched_from="communication_endpoint")
            for operation in annotations.get("operations") or []:
                if not isinstance(operation, dict):
                    continue
                add_candidate(operation.get("path") or "", matched_via=ref, matched_from="communication_endpoint")
                add_candidate(operation.get("name") or "", matched_via=ref, matched_from="communication_endpoint")
        elif kind == "Resource":
            labels = [name]
            for key in ("subscribes_to", "datasource_url"):
                value = annotations.get(key)
                if value:
                    labels.append(str(value))
            labels.extend(str(v) for v in (annotations.get("env_keys") or []) if v)
            for label in labels:
                add_candidate(
                    label,
                    matched_via=ref,
                    matched_from="communication_endpoint",
                    allow_namespace_roles=True,
                )
    return list(candidates.values())


def communication_endpoint_keys(
    label: str,
    *,
    allow_namespace_roles: bool,
    role_suffixes: list[str],
) -> list[tuple[str, str]]:
    """Candidate repo keys from protocol-agnostic endpoint strings.

    This is deliberately conservative. Exact canonical/host matching happens in
    `find_missing_repos`; this helper adds endpoint-aware namespace keys, such
    as a business-domain prefix from a route, topic, stream, queue, or webhook
    name. Namespace-role expansion is only enabled when the workspace config
    provides role suffixes.
    """
    if not label:
        return []
    raw = label.strip()
    lowered = raw.lower()
    out: list[tuple[str, str]] = []

    # Strip URL scheme/host path noise into tokens but keep dotted endpoint
    # names useful for stream/topic-style conventions.
    endpoint = re.sub(r"^https?://", "", lowered)
    endpoint = endpoint.split("?", 1)[0]
    endpoint = endpoint.split("#", 1)[0]
    endpoint = endpoint.replace(":", ".").replace("/", ".")
    parts = [p for p in re.split(r"[^a-z0-9]+", endpoint) if p]

    if "virtualtopic" in parts:
        idx = parts.index("virtualtopic")
        parts = parts[idx + 1:]
    elif parts[:1] == ["consumer"] and len(parts) > 2:
        parts = parts[2:]

    ignored = {
        "consumer", "producer", "publisher", "subscriber", "subscription",
        "queue", "topic", "stream", "event", "events", "exchange", "routing",
        "key", "v1", "v2", "v3", "api", "http", "https", "com", "org", "net",
    }
    meaningful = [p for p in parts if len(p) >= 3 and p not in ignored and not p.isdigit()]
    for token in meaningful[:3]:
        key = canonical_key(token)
        if key:
            out.append((key, "endpoint_namespace"))

    if allow_namespace_roles and meaningful and role_suffixes:
        namespace = canonical_key(meaningful[0])
        for suffix in role_suffixes:
            suffix_key = canonical_key(suffix)
            if suffix_key:
                out.append((f"{namespace}{suffix_key}", "endpoint_namespace_role"))

    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in out:
        if item not in seen:
            deduped.append(item)
            seen.add(item)
    return deduped


def clone_repo(repo_full_name: str, workspace_root: Path, timeout_seconds: int = 300) -> tuple[bool, str]:
    short = repo_full_name.split("/", 1)[1]
    dest = workspace_root / short
    if dest.exists():
        return False, "already_exists"
    cmd = ["gh", "repo", "clone", repo_full_name, str(dest)]
    try:
        result = subprocess.run(cmd, text=True, capture_output=True, check=False, timeout=timeout_seconds)
        if result.returncode == 0:
            return True, ""
        return False, (result.stderr or result.stdout)[-300:]
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_state(path: Path) -> dict[str, Any] | None:
    """Read crawler_state.json. Returns None if file is missing or
    corrupt — caller decides how to react.
    """
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# The fields of the crawler invocation that, if changed between runs,
# make resume-from-state unsafe. Examples: switching workspaces, changing
# the LLM model (output schema may differ), narrowing the repo allowlist.
# Changes to schedule-related fields like batch_size or max_age_hours are
# fine and not validated.
_RESUME_CRITICAL_FIELDS = ("root", "workspace_path", "provider", "model", "repos_filter")


def state_matches_inputs(state: dict[str, Any], inputs: dict[str, Any]) -> tuple[bool, str]:
    """Validate that a prior state file is compatible with the current
    invocation. Returns (compatible, reason).

    `repos_filter` matches as a SET (order-insensitive). All other fields
    are compared by string equality of their JSON repr.
    """
    if not isinstance(state, dict):
        return False, "state file is not a JSON object"
    if "inputs" not in state:
        return False, "state file is missing the `inputs` section (state was written by an older crawler version)"
    saved = state.get("inputs")
    if not isinstance(saved, dict):
        return False, "state file `inputs` section is malformed (expected JSON object)"
    for field in _RESUME_CRITICAL_FIELDS:
        prev = saved.get(field)
        curr = inputs.get(field)
        if field == "repos_filter":
            if (sorted(prev) if isinstance(prev, list) else prev) != (sorted(curr) if isinstance(curr, list) else curr):
                return False, f"{field} changed: {prev!r} → {curr!r}"
        elif prev != curr:
            return False, f"{field} changed: {prev!r} → {curr!r}"
    return True, ""


def state_completed_repos(state: dict[str, Any]) -> set[str]:
    """Return the set of repo names that were successfully extracted in
    a prior run (according to the state file). Used by --resume to skip
    work that has already happened. Failures are NOT included — they
    get retried by default.
    """
    out: set[str] = set()
    for batch in (state.get("batches") or []):
        for r in (batch.get("results") or []):
            if not isinstance(r, dict):
                continue
            if (r.get("status") or "").lower() in {"ok", "success", "completed"}:
                name = r.get("name") or r.get("id")
                if name:
                    out.add(name)
    return out


def crawl(
    *,
    root: Path,
    catalog_dir: Path,
    catalog_output: Path,
    workspace_path: Path,
    seeds_dir: Path,
    state_path: Path,
    provider: str,
    model: str | None,
    effort: str,
    timeout_seconds: int,
    max_age_hours: float,
    parallelism: int,
    batch_size: int,
    max_batches: int,
    budget_usd: float,
    repos_filter: list[str] | None,
    embed: bool,
    build_kuzu: bool,
    stream_logs: bool,
    discover: bool,
    max_discovery_rounds: int,
    reconcile_after_build: bool,
    reconcile_llm: bool,
    resume: bool = False,
) -> dict[str, Any]:
    workspace = load_workspace_config(workspace_path)
    repos = find_repos(
        root,
        workspace.get("orgs") or [],
        workspace.get("excluded_repos") or [],
        workspace.get("repo_units") or [],
    )
    if repos_filter:
        wanted = set(repos_filter)
        repos = [r for r in repos if r["name"] in wanted or r["id"] in wanted]

    org_repo_index: dict[str, str] = {}
    if discover:
        emit({"event": "discovery_index_start", "orgs": workspace.get("orgs") or []})
        org_repo_index = build_org_repo_index(workspace.get("orgs") or [])
        emit({"event": "discovery_index_done", "repos_indexed": len(org_repo_index)})

    inputs = {
        "root": str(root),
        "workspace_path": str(workspace_path),
        "provider": provider,
        "model": model,
        "repos_filter": list(repos_filter) if repos_filter else None,
    }

    resumed_from: dict[str, Any] | None = None
    if resume:
        prior = load_state(state_path)
        if prior is None:
            raise SystemExit(
                f"--resume specified but no usable state file at {state_path}. "
                "Run without --resume to start fresh."
            )
        ok, reason = state_matches_inputs(prior, inputs)
        if not ok:
            raise SystemExit(
                f"--resume rejected: state file at {state_path} is incompatible. {reason}. "
                "Either reconcile the inputs or delete the state file to start fresh."
            )
        completed = state_completed_repos(prior)
        skipped_for_resume = [r for r in repos if r["name"] in completed]
        repos = [r for r in repos if r["name"] not in completed]
        resumed_from = {
            "started_at": prior.get("started_at"),
            "batches_done": len(prior.get("batches") or []),
            "skipped_completed": [r["name"] for r in skipped_for_resume],
            "remaining": len(repos),
        }
        emit({"event": "resume_decision", **resumed_from})

    state: dict[str, Any] = {
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "root": str(root),
        "workspace_repos": len(repos),
        "batches": [],
        "discovery_rounds": [],
        # Persist the inputs so a future --resume can validate compatibility.
        "inputs": inputs,
        "resumed_from": resumed_from,
    }
    emit({"event": "crawl_start", "workspace_repos": len(repos), "discover": discover, "resume": resume})

    discovery_rounds = 0

    batches_done = 0
    while batches_done < max_batches:
        stale: list[tuple[dict[str, Any], str]] = []
        for repo in repos:
            reason = is_stale(repo, catalog_dir, max_age_hours)
            if reason:
                stale.append((repo, reason))
        if not stale:
            emit({"event": "frontier_empty"})
            if not discover or discovery_rounds >= max_discovery_rounds:
                break
            cloned_ids = {r["id"] for r in repos}
            missing = find_missing_repos(
                catalog_output,
                org_repo_index,
                cloned_ids,
                list(workspace.get("communication_discovery_role_suffixes") or []),
            )
            if not missing:
                emit({"event": "discovery_no_new_repos"})
                break
            emit({"event": "discovery_round_start", "round": discovery_rounds + 1, "candidates": len(missing)})
            cloned_now = []
            for candidate in missing:
                ok, err = clone_repo(candidate["repo"], root)
                emit({"event": "repo_cloned" if ok else "repo_clone_failed", **candidate, "error": err if not ok else ""})
                if ok:
                    cloned_now.append(candidate["repo"])
            state["discovery_rounds"].append({"round": discovery_rounds + 1, "cloned": cloned_now, "candidates": len(missing)})
            discovery_rounds += 1
            if not cloned_now:
                emit({"event": "discovery_round_no_clones"})
                break
            repos = find_repos(
                root,
                workspace.get("orgs") or [],
                workspace.get("excluded_repos") or [],
                workspace.get("repo_units") or [],
            )
            emit({"event": "discovery_round_done", "workspace_repos_now": len(repos)})
            continue

        current_total = repo_total_cost(catalog_dir)
        if current_total >= budget_usd:
            emit({"event": "budget_exhausted", "spent": current_total, "budget": budget_usd})
            break

        batch = stale[:batch_size]
        emit({"event": "batch_start", "n": len(batch), "spent_so_far": current_total})
        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=parallelism) as executor:
            futures = {
                executor.submit(
                    run_extractor,
                    repo,
                    provider=provider,
                    model=model,
                    effort=effort,
                    timeout_seconds=timeout_seconds,
                    catalog_dir=catalog_dir,
                    workspace_path=workspace_path,
                    stream_logs=stream_logs,
                ): repo
                for repo, _ in batch
            }
            for future in as_completed(futures):
                repo = futures[future]
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001
                    result = {"repo": repo["id"], "status": "error", "error": str(exc)}
                results.append(result)
                emit({"event": "repo_done", "repo": result["repo"], "status": result.get("status")})

        state["batches"].append({"results": results, "completed_at": dt.datetime.now(dt.timezone.utc).isoformat()})
        write_state(state_path, state)

        emit({"event": "build_catalog_start"})
        summary = build_catalog(root, catalog_dir, seeds_dir, catalog_output, workspace)
        emit({"event": "build_catalog_done", **summary})

        if reconcile_after_build:
            reconcile_args = [sys.executable, "-m", "servicescout.reconcile", "--catalog", str(catalog_output)]
            if reconcile_llm:
                reconcile_args.append("--llm-assist")
            emit({"event": "reconcile_start", "llm_assist": reconcile_llm})
            rc = subprocess.run(reconcile_args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            emit({"event": "reconcile_done", "returncode": rc.returncode, "stdout_tail": rc.stdout[-1500:]})

        batches_done += 1

    if embed:
        emit({"event": "embed_start"})
        embed_cmd = [sys.executable, "-m", "servicescout.embed_catalog", "--catalog", str(catalog_output)]
        completed = subprocess.run(embed_cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        emit({"event": "embed_done", "returncode": completed.returncode, "tail": completed.stdout[-1000:]})

    if build_kuzu:
        emit({"event": "build_kuzu_start"})
        kuzu_cmd = [
            sys.executable, "-m", "servicescout.build_kuzu",
            "--catalog", str(catalog_output),
            "--db", str(catalog_output.parent / "catalog.kuzu"),
        ]
        completed = subprocess.run(kuzu_cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        emit({"event": "build_kuzu_done", "returncode": completed.returncode, "tail": completed.stdout[-800:]})

    state["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    state["total_cost_usd"] = repo_total_cost(catalog_dir)
    write_state(state_path, state)
    emit({"event": "crawl_done", "spent": state["total_cost_usd"]})
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--catalog-dir", type=Path, default=DEFAULT_CATALOG_DIR)
    parser.add_argument("--catalog-output", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--workspace", type=Path, default=HERE / "workspace.json")
    parser.add_argument("--seeds-dir", type=Path, default=HERE / "seeds")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--provider", choices=("codex", "claude"), default="codex")
    parser.add_argument("--model", default=None)
    parser.add_argument("--effort", default="high")
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    parser.add_argument("--max-age-hours", type=float, default=24.0)
    parser.add_argument("--parallelism", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--max-batches", type=int, default=999, help="Safety cap; default is effectively unbounded. Real termination should be frontier_empty + discovery exhausted, or budget.")
    parser.add_argument("--budget-usd", type=float, default=200.0, help="Hard stop on cumulative LLM spend. Default 200 is sized for a medium-large enterprise crawl.")
    parser.add_argument("--repos", nargs="*", default=None, help="Limit crawl to specific repo names/ids")
    parser.add_argument("--embed", action="store_true", help="Run embed_catalog.py after extraction")
    parser.add_argument("--build-kuzu", action="store_true", help="Run build_kuzu.py after --embed to populate data/catalog.kuzu (the MCP server's primary backend).")
    parser.add_argument("--stream-logs", action="store_true")
    parser.add_argument("--discover", action="store_true", help="Search configured GH orgs for repos that the catalog references but are not cloned; clone and extract them.")
    parser.add_argument("--max-discovery-rounds", type=int, default=10, help="Maximum rounds of discover→clone→extract before stopping. Each round indexes the configured orgs, finds unresolved Components that match an uncloned repo, clones them, then re-enters extraction.")
    parser.add_argument("--reconcile", action="store_true", help="Run reconcile.py after each build_catalog (collapse external duplicates).")
    parser.add_argument("--reconcile-llm", action="store_true", help="Use --llm-assist on reconcile.py (LLM-judgment merges for ambiguous duplicates).")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from a prior run: read the state file at --state and skip repos that were "
             "successfully extracted there (failed ones are retried). Validates that the saved "
             "inputs (root / workspace / provider / model / repos filter) match the current "
             "invocation; aborts with a clear message if not. Without --resume the crawler "
             "starts fresh and writes a new state file.",
    )
    args = parser.parse_args()
    crawl(
        root=args.root.resolve(),
        catalog_dir=args.catalog_dir,
        catalog_output=args.catalog_output,
        workspace_path=args.workspace,
        seeds_dir=args.seeds_dir,
        state_path=args.state,
        provider=args.provider,
        model=args.model,
        effort=args.effort,
        timeout_seconds=args.timeout_seconds,
        max_age_hours=args.max_age_hours,
        parallelism=args.parallelism,
        batch_size=args.batch_size,
        max_batches=args.max_batches,
        budget_usd=args.budget_usd,
        repos_filter=args.repos,
        embed=args.embed,
        build_kuzu=args.build_kuzu,
        stream_logs=args.stream_logs,
        discover=args.discover,
        max_discovery_rounds=args.max_discovery_rounds,
        reconcile_after_build=args.reconcile,
        reconcile_llm=args.reconcile_llm,
        resume=args.resume,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
