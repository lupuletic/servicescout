"""Matplotlib plots for an eval run.

Generates four PNGs from a single run JSON:

  1. catalog_pass_rate.png       — pass-rate per category (bar)
  2. catalog_metric_heatmap.png  — per-question metric scores (heatmap)
  3. agent_repo_recall.png       — baseline vs treatment repo_recall (grouped bar)
  4. agent_judge_scores.png      — baseline vs treatment judge axes (grouped bar)

Designed to be readable in both light and dark README contexts (no custom
backgrounds, no funky colormaps).
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from statistics import mean

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
except ImportError as exc:
    raise SystemExit("pip install matplotlib numpy to generate plots") from exc

from score import CATEGORIES, JUDGE_AXES


def _group_by_category(results: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        grouped[r.get("category", "uncategorised")].append(r)
    return dict(grouped)


def _ordered_categories(grouped: dict) -> list[str]:
    """Preserve our preferred order; append any unexpected categories at the end."""
    base = [c for c in CATEGORIES if c in grouped]
    extra = sorted(set(grouped) - set(CATEGORIES))
    return base + extra


# ----------------- plot 1: catalog pass-rate by category -----------------

def plot_catalog_pass_rate(run: dict, out_path: Path) -> Path | None:
    catalog_results = run.get("catalog_results") or []
    if not catalog_results:
        return None
    grouped = _group_by_category(catalog_results)
    cats = _ordered_categories(grouped)
    pass_rates = [sum(1 for r in grouped[c] if r.get("pass")) / len(grouped[c]) for c in cats]
    counts = [len(grouped[c]) for c in cats]

    fig, ax = plt.subplots(figsize=(7, 4))
    bars = ax.bar(cats, pass_rates, color="#3a8dde", edgecolor="#1e4e8c")
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("Pass rate")
    ax.set_title(f"Catalog tier — pass rate by category ({len(catalog_results)} questions)")
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    for bar, pr, n in zip(bars, pass_rates, counts):
        ax.text(bar.get_x() + bar.get_width() / 2, pr + 0.02, f"{pr:.0%}\n(n={n})",
                ha="center", va="bottom", fontsize=9)
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


# ----------------- plot 2: per-question metric heatmap -----------------

def plot_catalog_heatmap(run: dict, out_path: Path) -> Path | None:
    catalog_results = run.get("catalog_results") or []
    if not catalog_results:
        return None
    metric_keys = ["search_recall", "neighbors_recall", "trace_recall"]
    qids = [r["question_id"] for r in catalog_results]
    matrix = np.full((len(catalog_results), len(metric_keys)), np.nan)
    for i, r in enumerate(catalog_results):
        for j, k in enumerate(metric_keys):
            if k in r.get("metrics", {}):
                matrix[i, j] = r["metrics"][k]

    fig, ax = plt.subplots(figsize=(6, max(3, 0.35 * len(qids) + 1)))
    cmap = plt.get_cmap("RdYlGn")
    cmap.set_bad(color="#e0e0e0")
    im = ax.imshow(matrix, cmap=cmap, vmin=0, vmax=1, aspect="auto")
    ax.set_xticks(range(len(metric_keys)))
    ax.set_xticklabels([k.replace("_", "\n") for k in metric_keys], fontsize=9)
    ax.set_yticks(range(len(qids)))
    ax.set_yticklabels(qids, fontsize=8)
    ax.set_title("Catalog tier — per-question metric scores")
    # cell annotations
    for i in range(len(qids)):
        for j in range(len(metric_keys)):
            val = matrix[i, j]
            if np.isnan(val):
                txt = "—"
            else:
                txt = f"{val:.2f}"
            ax.text(j, i, txt, ha="center", va="center",
                    color="black" if (np.isnan(val) or val > 0.5) else "white", fontsize=8)
    cbar = fig.colorbar(im, ax=ax, fraction=0.04, pad=0.04)
    cbar.set_label("Recall", rotation=270, labelpad=12)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


# ----------------- plot 3: agent repo recall (baseline vs treatment) -----------------

def plot_agent_repo_recall(run: dict, out_path: Path) -> Path | None:
    agent_results = run.get("agent_results") or []
    if not agent_results:
        return None
    grouped = _group_by_category(agent_results)
    cats = _ordered_categories(grouped)
    baseline_vals = [mean(r["baseline"]["score"]["repo_recall"] for r in grouped[c]) for c in cats]
    treatment_vals = [mean(r["treatment"]["score"]["repo_recall"] for r in grouped[c]) for c in cats]

    x = np.arange(len(cats))
    width = 0.38

    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    b1 = ax.bar(x - width / 2, baseline_vals, width, label="Baseline (cold-start, no context)",
                color="#bbbbbb", edgecolor="#555555")
    b2 = ax.bar(x + width / 2, treatment_vals, width, label="With ServiceScout context",
                color="#3a8dde", edgecolor="#1e4e8c")
    ax.set_xticks(x)
    ax.set_xticklabels(cats)
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("Expected-repo recall")
    ax.set_title(f"Agent tier — repo recall, cold-start vs ServiceScout ({len(agent_results)} questions)")
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    for bar, v in list(zip(b1, baseline_vals)) + list(zip(b2, treatment_vals)):
        ax.text(bar.get_x() + bar.get_width() / 2, v + 0.02, f"{v:.0%}",
                ha="center", va="bottom", fontsize=9)
    ax.legend(loc="upper left", frameon=False, fontsize=9)
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


# ----------------- plot 4: judge scores -----------------

def plot_judge_scores(run: dict, out_path: Path) -> Path | None:
    agent_results = run.get("agent_results") or []
    judged = [r for r in agent_results if r["baseline"].get("judge") and r["treatment"].get("judge")]
    if not judged:
        return None

    def _avg(side: str, axis: str) -> float:
        vals = [r[side]["judge"].get(axis) for r in judged
                if isinstance(r[side]["judge"].get(axis), (int, float))]
        return mean(vals) if vals else float("nan")

    baseline_vals = [_avg("baseline", a) for a in JUDGE_AXES]
    treatment_vals = [_avg("treatment", a) for a in JUDGE_AXES]

    x = np.arange(len(JUDGE_AXES))
    width = 0.38

    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    ax.bar(x - width / 2, baseline_vals, width, label="Baseline",
           color="#bbbbbb", edgecolor="#555555")
    ax.bar(x + width / 2, treatment_vals, width, label="With ServiceScout",
           color="#3a8dde", edgecolor="#1e4e8c")
    ax.set_xticks(x)
    ax.set_xticklabels([a.replace("_", "\n") for a in JUDGE_AXES])
    ax.set_ylim(0, 5.5)
    ax.set_ylabel("Judge score (1-5)")
    ax.set_title(f"LLM-judged answer quality ({len(judged)} questions judged)")
    for i, (b, t) in enumerate(zip(baseline_vals, treatment_vals)):
        ax.text(i - width / 2, b + 0.1, f"{b:.2f}", ha="center", va="bottom", fontsize=9)
        ax.text(i + width / 2, t + 0.1, f"{t:.2f}", ha="center", va="bottom", fontsize=9)
    ax.legend(loc="upper left", frameon=False, fontsize=9)
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


# ----------------- entry -----------------

def plot_all(run: dict, out_dir: Path) -> dict[str, Path | None]:
    out_dir.mkdir(parents=True, exist_ok=True)
    return {
        "catalog_pass_rate": plot_catalog_pass_rate(run, out_dir / "catalog_pass_rate.png"),
        "catalog_heatmap": plot_catalog_heatmap(run, out_dir / "catalog_metric_heatmap.png"),
        "agent_repo_recall": plot_agent_repo_recall(run, out_dir / "agent_repo_recall.png"),
        "agent_judge_scores": plot_judge_scores(run, out_dir / "agent_judge_scores.png"),
    }


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True, help="Path to evals/runs/run_*.json")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="Output directory (default: same as the run file's parent)")
    args = parser.parse_args()
    run = json.loads(args.run.read_text())
    out_dir = args.out_dir or args.run.parent
    paths = plot_all(run, out_dir)
    for name, p in paths.items():
        print(f"{name}: {p if p else '(skipped — no data)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
