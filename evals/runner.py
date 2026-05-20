"""Top-level eval entry point.

  python evals/runner.py                       # catalog tier only (fast, free)
  python evals/runner.py --agent               # also run agent tier (costs LLM tokens)
  python evals/runner.py --agent --no-judge    # agent tier without LLM judge
  python evals/runner.py --refresh-cache       # bust agent_eval response cache
  python evals/runner.py --catalog evals/data/catalog.json
  python evals/runner.py --workspace online-boutique

Catalog discovery: prefers --catalog, then the selected eval workspace's
catalog.json, then the project-level data/catalog.json. Falls back to Kuzu DB
if available.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

HERE = Path(__file__).resolve().parent

try:
    import yaml
except ImportError as exc:
    raise SystemExit("pip install pyyaml to run the eval suite") from exc

from servicescout.storage import make_backend  # noqa: E402

import catalog_eval  # noqa: E402
import agent_eval    # noqa: E402
import plots         # noqa: E402
from workspace_paths import resolve_workspace  # noqa: E402


def _resolve_catalog(arg_path: Path | None, arg_kuzu: Path | None, workspace_name: str) -> tuple[Path, Path | None]:
    """Return (catalog_json_path, kuzu_db_path). Either may not exist."""
    ws = resolve_workspace(workspace_name)
    candidates = []
    if arg_path:
        candidates.append(arg_path)
    candidates.append(ws.data_dir / "catalog.json")
    candidates.append(PROJECT_ROOT / "data" / "catalog.json")
    catalog_path = next((c for c in candidates if c.exists()), candidates[0])

    kuzu_candidates = []
    if arg_kuzu:
        kuzu_candidates.append(arg_kuzu)
    kuzu_candidates.extend([ws.data_dir / "catalog.kuzu", PROJECT_ROOT / "data" / "catalog.kuzu"])
    kuzu_path = next((c for c in kuzu_candidates if c.exists()), None)
    return catalog_path, kuzu_path


def _catalog_summary(backend) -> dict:
    status = backend.status()
    summary = dict(status.get("summary") or {})
    summary.setdefault("entities", status.get("entities"))
    summary.setdefault("relations", status.get("relations"))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default="sock-shop", help="Eval workspace slug.")
    parser.add_argument("--catalog", type=Path, default=None, help="Override catalog.json path.")
    parser.add_argument("--kuzu", type=Path, default=None, help="Override catalog.kuzu path.")
    parser.add_argument("--questions", type=Path, default=None)
    parser.add_argument("--agent", action="store_true", help="Run agent-tier eval (uses Anthropic API).")
    parser.add_argument("--no-judge", action="store_true", help="Skip LLM judge (still scores repo/keyword recall).")
    parser.add_argument("--refresh-cache", action="store_true", help="Bypass agent_eval response cache.")
    parser.add_argument("--model", default=agent_eval.DEFAULT_MODEL)
    parser.add_argument("--judge-model", default=agent_eval.DEFAULT_JUDGE_MODEL)
    parser.add_argument("--limit", type=int, default=None, help="Run only first N questions.")
    parser.add_argument("--output", type=Path, default=None, help="Override output JSON path.")
    parser.add_argument("--no-plots", action="store_true", help="Skip PNG generation.")
    args = parser.parse_args()

    workspace = resolve_workspace(args.workspace)
    questions_path = args.questions or workspace.questions
    catalog_path, kuzu_path = _resolve_catalog(args.catalog, args.kuzu, args.workspace)
    questions = (yaml.safe_load(questions_path.read_text()) or {}).get("questions") or []
    if args.limit:
        questions = questions[: args.limit]
    if not questions:
        print(f"no questions in {questions_path}", file=sys.stderr)
        return 1

    print(f"catalog: {catalog_path} (exists={catalog_path.exists()})  kuzu: {kuzu_path}")
    print(f"workspace: {workspace.name}  questions: {len(questions)}")

    if not catalog_path.exists() and (kuzu_path is None or not kuzu_path.exists()):
        print(f"\nERROR: No catalog at {catalog_path} (and no Kuzu DB at {kuzu_path}).", file=sys.stderr)
        print(f"Hint: run ./evals/setup.sh --workspace {workspace.name}, then ./evals/build_eval_catalog.sh --workspace {workspace.name}.", file=sys.stderr)
        return 2

    backend = make_backend("auto", catalog_path=catalog_path, kuzu_path=kuzu_path)

    run = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "workspace": workspace.name,
        "catalog_path": str(catalog_path),
        "kuzu_path": str(kuzu_path) if kuzu_path else None,
        "catalog_summary": _catalog_summary(backend),
        "model": args.model if args.agent else None,
        "judge_model": (args.judge_model if (args.agent and not args.no_judge) else None),
        "catalog_results": [],
        "agent_results": [],
    }

    print("\n--- catalog tier ---")
    run["catalog_results"] = catalog_eval.run_catalog_eval(backend, questions)
    passes = sum(1 for r in run["catalog_results"] if r.get("pass"))
    print(f"catalog: {passes}/{len(run['catalog_results'])} passed")

    if args.agent:
        print("\n--- agent tier (cold-start) ---")
        run["agent_results"] = agent_eval.run_agent_eval(
            backend, questions,
            model=args.model,
            judge_model=None if args.no_judge else args.judge_model,
            refresh_cache=args.refresh_cache,
        )
        delta = sum(r["delta"]["repo_recall"] for r in run["agent_results"]) / max(len(run["agent_results"]), 1)
        print(f"agent: mean repo-recall delta (treatment - baseline) = {delta:+.2f}")

    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = workspace.runs_dir
    out_dir.mkdir(exist_ok=True)
    out_path = args.output or (out_dir / f"run_{ts}.json")
    out_path.write_text(json.dumps(run, indent=2))
    print(f"\nwrote {out_path}")

    if not args.no_plots:
        plot_dir = out_path.parent / out_path.stem
        emitted = plots.plot_all(run, plot_dir)
        wrote = [p for p in emitted.values() if p]
        if wrote:
            print(f"plots: wrote {len(wrote)} PNG(s) to {plot_dir}/")

    print(f"render the report with: python evals/report.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
