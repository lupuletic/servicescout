"""Render per-run JSON results into a markdown summary + diff against a baseline.

Two report modes:

  per-run:  one-off summary of the most recent run, written to runs/<ts>.md
  diff:     compares two runs (default: latest vs baselines/catalog_baseline.json)
            and highlights regressions / improvements per question.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent))
from score import JUDGE_AXES  # noqa: E402


def _by_category(results: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for r in results:
        grouped.setdefault(r.get("category", "uncategorised"), []).append(r)
    return grouped


def _judge_axis_vals(results: list[dict], side: str, axis: str) -> list[float]:
    out = []
    for r in results:
        j = r[side].get("judge")
        if j and isinstance(j.get(axis), (int, float)):
            out.append(j[axis])
    return out


def render_run(run: dict, out_path: Path) -> Path:
    lines: list[str] = []
    lines.append(f"# Eval run — {run.get('timestamp','?')}")
    lines.append("")
    lines.append(f"- catalog: `{run.get('catalog_path')}`")
    lines.append(f"- catalog entities: **{run.get('catalog_summary', {}).get('entities', '?')}**, "
                 f"relations: **{run.get('catalog_summary', {}).get('relations', '?')}**")
    lines.append(f"- questions evaluated: **{len(run.get('catalog_results', []))}** catalog, "
                 f"**{len(run.get('agent_results', []))}** agent")
    lines.append("")

    catalog_results = run.get("catalog_results") or []
    if catalog_results:
        lines.append("## Catalog tier")
        passes = sum(1 for r in catalog_results if r.get("pass"))
        lines.append(f"- overall pass-rate: **{passes}/{len(catalog_results)} = {passes/len(catalog_results):.0%}**")
        lines.append("")
        lines.append("| category | pass-rate | search_recall | neighbors_recall | trace_recall |")
        lines.append("|---|---|---|---|---|")
        for cat, items in _by_category(catalog_results).items():
            pr = sum(1 for r in items if r.get("pass")) / len(items)
            sr = mean([r["metrics"].get("search_recall", 0.0) for r in items if "search_recall" in r["metrics"]] or [float("nan")])
            nr = mean([r["metrics"].get("neighbors_recall", 0.0) for r in items if "neighbors_recall" in r["metrics"]] or [float("nan")])
            tr = mean([r["metrics"].get("trace_recall", 0.0) for r in items if "trace_recall" in r["metrics"]] or [float("nan")])
            lines.append(f"| {cat} | {pr:.0%} | {sr:.2f} | {nr:.2f} | {tr:.2f} |")
        lines.append("")
        lines.append("### Per-question detail")
        lines.append("| id | pass | metrics |")
        lines.append("|---|---|---|")
        for r in catalog_results:
            mark = "✔" if r.get("pass") else "✘"
            m = ", ".join(f"{k}={v:.2f}" for k, v in r["metrics"].items())
            lines.append(f"| {r['question_id']} | {mark} | {m} |")
        lines.append("")

    agent_results = run.get("agent_results") or []
    if agent_results:
        lines.append("## Agent tier (cold-start scenario)")
        lines.append("")
        lines.append("| category | baseline repo-recall | treatment repo-recall | Δ |")
        lines.append("|---|---|---|---|")
        for cat, items in _by_category(agent_results).items():
            base = mean(r["baseline"]["score"]["repo_recall"] for r in items)
            treat = mean(r["treatment"]["score"]["repo_recall"] for r in items)
            arrow = "↑" if treat > base else ("↓" if treat < base else "—")
            lines.append(f"| {cat} | {base:.2f} | {treat:.2f} | {arrow} {treat-base:+.2f} |")
        lines.append("")
        any_judged = any(r["treatment"].get("judge") for r in agent_results)
        if any_judged:
            lines.append("### Judge scores (1-5, higher better)")
            lines.append("| metric | baseline | treatment | Δ |")
            lines.append("|---|---|---|---|")
            for axis in JUDGE_AXES:
                base_vals = _judge_axis_vals(agent_results, "baseline", axis)
                treat_vals = _judge_axis_vals(agent_results, "treatment", axis)
                if not base_vals or not treat_vals:
                    continue
                b, t = mean(base_vals), mean(treat_vals)
                lines.append(f"| {axis} | {b:.2f} | {t:.2f} | {t-b:+.2f} |")
            lines.append("")

    out_path.write_text("\n".join(lines))
    return out_path


def render_diff(current: dict, baseline: dict, out_path: Path) -> Path:
    lines = [f"# Eval diff — {current.get('timestamp')} vs baseline {baseline.get('timestamp')}", ""]
    cur_by_id = {r["question_id"]: r for r in current.get("catalog_results", [])}
    base_by_id = {r["question_id"]: r for r in baseline.get("catalog_results", [])}
    all_ids = sorted(set(cur_by_id) | set(base_by_id))

    regressions: list[str] = []
    improvements: list[str] = []
    for qid in all_ids:
        c = cur_by_id.get(qid)
        b = base_by_id.get(qid)
        if not c or not b:
            continue
        if c["pass"] and not b["pass"]:
            improvements.append(f"- ✅ {qid}: now passes")
        elif b["pass"] and not c["pass"]:
            regressions.append(f"- ❌ {qid}: regressed")

    lines.append(f"- {len(improvements)} improvement(s), {len(regressions)} regression(s)")
    lines.append("")
    if regressions:
        lines.append("## Regressions")
        lines.extend(regressions)
        lines.append("")
    if improvements:
        lines.append("## Improvements")
        lines.extend(improvements)
        lines.append("")

    out_path.write_text("\n".join(lines))
    return out_path


def latest_run(runs_dir: Path) -> Path | None:
    files = sorted(runs_dir.glob("run_*.json"))
    return files[-1] if files else None


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", type=Path, default=Path(__file__).resolve().parent / "runs")
    parser.add_argument("--baseline", type=Path, default=Path(__file__).resolve().parent / "baselines" / "catalog_baseline.json")
    parser.add_argument("--current", type=Path, default=None)
    args = parser.parse_args()

    current_path = args.current or latest_run(args.runs_dir)
    if not current_path or not current_path.exists():
        print("no current run found; run python evals/runner.py first")
        return 1
    current = json.loads(current_path.read_text())
    md_path = current_path.with_suffix(".md")
    render_run(current, md_path)
    print(f"wrote {md_path}")
    if args.baseline.exists():
        baseline = json.loads(args.baseline.read_text())
        diff_path = current_path.with_name(current_path.stem + "__vs_baseline.md")
        render_diff(current, baseline, diff_path)
        print(f"wrote {diff_path}")
    else:
        print(f"no baseline at {args.baseline} — promote a clean run with: cp {current_path} {args.baseline}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
