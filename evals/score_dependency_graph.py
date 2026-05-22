"""Score a ServiceScout catalog's dependency graph against a source-true gold.

This is the experiment yardstick: precision / recall / F-beta on the *edge set*,
measured against a hand-verified, source-grounded gold graph (not the README
diagram). Unlike validate_readme_architecture.py, this penalises hallucinated
edges (precision), not just missing ones (recall) — a confidently-wrong
dependency is the dangerous failure mode for a tool used "in the wild".

It also reports recall split by whether each gold edge is documented in the
README diagram. The gap between documented-edge recall and undocumented-edge
recall is a contamination probe: a model that only parrots the README scores
high on documented edges and low on undocumented ones; a model that genuinely
reads code scores high on both.

Pass several --catalog paths (e.g. N repeats of one config) to get mean +/- sd.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SERVICE_REL_TYPES = {"communicatesWith", "consumesApi", "dependsOn"}
RESOURCE_REL_TYPES = {"readsResource", "writesResource", "dependsOn"}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def tokens(text: str) -> list[str]:
    return [tok for tok in re.split(r"[^a-z0-9]+", text.lower()) if tok]


def strip_kind(ref: str) -> tuple[str, str]:
    """'Component:frontend' -> ('component', 'frontend'); bare -> ('', ref)."""
    if ":" in ref:
        kind, _, name = ref.partition(":")
        return kind.lower(), name
    return "", ref


@dataclass(frozen=True)
class Gold:
    components: set[str]
    service_edges: set[tuple[str, str]]
    resource_edges: set[tuple[str, str]]  # (component, resource_name)
    documented: set[tuple[str, str]]  # edges flagged in_readme_diagram
    resource_tokens: dict[str, list[str]]  # resource_name -> match tokens


def load_gold(path: Path) -> Gold:
    raw = load_json(path)
    components = {c["name"].lower() for c in raw["components"]}
    service_edges: set[tuple[str, str]] = set()
    resource_edges: set[tuple[str, str]] = set()
    documented: set[tuple[str, str]] = set()
    resource_tokens: dict[str, list[str]] = {}
    for edge in raw["edges"]:
        src, dst = edge["from"].lower(), edge["to"].lower()
        pair = (src, dst)
        if edge.get("kind") == "resource":
            resource_edges.add(pair)
            resource_tokens[dst] = tokens(dst)
        else:
            service_edges.add(pair)
        if edge.get("in_readme_diagram"):
            documented.add(pair)
    return Gold(components, service_edges, resource_edges, documented, resource_tokens)


def make_resolver(gold_components: set[str]):
    """Map a catalog component ref to its canonical gold name (or None).

    Handles the real-world naming drift we've seen:
      'Component:googlecloudplatform-microservices-demo-frontend' -> 'frontend'
      'Product Catalog Service'                                   -> 'productcatalogservice'
    """
    stripped_gold = {g: re.sub(r"[^a-z0-9]+", "", g.lower()) for g in gold_components}

    def resolve(ref: str) -> str | None:
        _, name = strip_kind(ref)
        low = name.lower()
        if low in gold_components:
            return low
        stripped = re.sub(r"[^a-z0-9]+", "", low)
        for gold_name, sg in stripped_gold.items():  # spaced/punctuated names
            if stripped == sg:
                return gold_name
        toks = set(tokens(name))
        token_hits = [g for g in gold_components if g in toks]
        if len(token_hits) == 1:
            return token_hits[0]
        # Prefixed drift ('...-demo-frontend') or embedded name; prefer the
        # longest gold name to avoid a short name matching inside a longer one.
        contained = sorted(
            (g for g, sg in stripped_gold.items() if sg and (stripped.endswith(sg) or sg in stripped)),
            key=len, reverse=True,
        )
        return contained[0] if contained else None

    return resolve


@dataclass
class Scored:
    catalog: str
    tp: set[tuple[str, str]]
    fn: set[tuple[str, str]]
    fp: set[tuple[str, str]]
    precision: float
    recall: float
    fbeta: float
    documented_recall: float
    undocumented_recall: float
    component_recall: float
    extra_components: list[str]


def predicted_edges(catalog: dict[str, Any], gold: Gold, resolve) -> tuple[set, set]:
    relations = catalog.get("relations") or []
    # API node -> provider canonical component
    api_provider: dict[str, str] = {}
    for rel in relations:
        if rel.get("type") == "providesApi":
            provider = resolve(rel.get("from", ""))
            if provider:
                api_provider[rel.get("to", "")] = provider

    service: set[tuple[str, str]] = set()
    resource: set[tuple[str, str]] = set()
    for rel in relations:
        rtype, frm, to = rel.get("type"), rel.get("from", ""), rel.get("to", "")
        src = resolve(frm)
        if not src:
            continue
        to_kind, to_name = strip_kind(to)
        if rtype in SERVICE_REL_TYPES:
            if to_kind == "component":
                dst = resolve(to)
            elif to_kind == "api":
                dst = api_provider.get(to)
            else:
                dst = None
            if dst and dst != src:
                service.add((src, dst))
        if rtype in RESOURCE_REL_TYPES and to_kind == "resource":
            resource.add((src, to_name.lower()))
    return service, resource


def match_resource(
    pred: set[tuple[str, str]], resource_edges: set[tuple[str, str]], resource_tokens: dict[str, list[str]]
) -> set[tuple[str, str]]:
    """A gold resource edge is hit if some predicted edge from the same component
    points at a resource whose name shares the gold resource's tokens."""
    found: set[tuple[str, str]] = set()
    for comp, res_name in resource_edges:
        want = set(resource_tokens[res_name])
        if any(p_comp == comp and want & set(tokens(p_res)) for p_comp, p_res in pred):
            found.add((comp, res_name))
    return found


def fbeta_score(precision: float, recall: float, beta: float) -> float:
    if precision == 0 and recall == 0:
        return 0.0
    b2 = beta * beta
    denom = b2 * precision + recall
    return (1 + b2) * precision * recall / denom if denom else 0.0


def _score_core(
    label: str,
    pred_service: set[tuple[str, str]],
    pred_resource: set[tuple[str, str]],
    gold: Gold,
    beta: float,
    *,
    restrict_sources: set[str] | None,
    component_recall: float,
    extra: list[str],
) -> Scored:
    """Shared P/R/F-beta math. When restrict_sources is given, only gold edges
    (and predictions) whose source is in that set are scored — used when we
    extracted a subset of services and must not be penalised for the rest."""
    gold_service = gold.service_edges
    gold_resource = gold.resource_edges
    if restrict_sources is not None:
        gold_service = {(a, b) for a, b in gold_service if a in restrict_sources}
        gold_resource = {(a, b) for a, b in gold_resource if a in restrict_sources}

    pred_service_inscope = {
        (a, b) for a, b in pred_service
        if a in gold.components and b in gold.components
        and (restrict_sources is None or a in restrict_sources)
    }
    resource_tp = match_resource(pred_resource, gold_resource, gold.resource_tokens)

    gold_all = gold_service | gold_resource
    tp = (pred_service_inscope & gold_service) | resource_tp
    fn = gold_all - tp
    fp = pred_service_inscope - gold_service  # hallucinated service edges

    precision = len(tp) / (len(tp) + len(fp)) if (tp or fp) else 1.0
    recall = len(tp) / len(gold_all) if gold_all else 1.0
    fbeta = fbeta_score(precision, recall, beta)

    doc = gold.documented & gold_all
    undoc = gold_all - doc
    documented_recall = len(tp & doc) / len(doc) if doc else 1.0
    undocumented_recall = len(tp & undoc) / len(undoc) if undoc else 1.0

    return Scored(
        catalog=label, tp=tp, fn=fn, fp=fp,
        precision=precision, recall=recall, fbeta=fbeta,
        documented_recall=documented_recall, undocumented_recall=undocumented_recall,
        component_recall=component_recall, extra_components=extra,
    )


def score_one(catalog_path: Path, gold: Gold, beta: float) -> Scored:
    catalog = load_json(catalog_path)
    resolve = make_resolver(gold.components)
    pred_service, pred_resource = predicted_edges(catalog, gold, resolve)

    catalog_components = {
        resolve(f"Component:{(e.get('metadata') or {}).get('name', '')}")
        for e in catalog.get("entities") or []
        if e.get("kind") == "Component"
    }
    found = {c for c in catalog_components if c in gold.components}
    component_recall = len(found) / len(gold.components) if gold.components else 1.0
    extra = sorted(
        (e.get("metadata") or {}).get("name", "")
        for e in catalog.get("entities") or []
        if e.get("kind") == "Component" and resolve(f"Component:{(e.get('metadata') or {}).get('name','')}") is None
    )
    return _score_core(
        catalog_path.name, pred_service, pred_resource, gold, beta,
        restrict_sources=None, component_recall=component_recall, extra=extra,
    )


def score_extractions(extraction_paths: list[Path], gold: Gold, beta: float, *, label: str | None = None) -> Scored:
    """Score raw per-service extractor outputs (dependencies[]) directly,
    isolating extraction quality from the merge step. Gold is restricted to the
    services actually extracted, so unextracted callers aren't counted missing.
    """
    resolve = make_resolver(gold.components)
    pred_service: set[tuple[str, str]] = set()
    pred_resource: set[tuple[str, str]] = set()
    sources: set[str] = set()
    for path in extraction_paths:
        ex = load_json(path)
        own = resolve((ex.get("repo") or {}).get("id", ""))
        if own:
            sources.add(own)
        for dep in ex.get("dependencies") or []:
            src = resolve(dep.get("source", "")) or own
            if not src:
                continue
            if (dep.get("target_kind") or "").lower() == "resource":
                pred_resource.add((src, (dep.get("target") or "").lower()))
            else:
                dst = resolve(dep.get("target", ""))
                if dst and dst != src:
                    pred_service.add((src, dst))
    return _score_core(
        label or f"{len(extraction_paths)} extractions", pred_service, pred_resource, gold, beta,
        restrict_sources=sources or None, component_recall=1.0, extra=[],
    )


def fmt_edges(edges: Iterable[tuple[str, str]]) -> str:
    items = sorted(f"{a}->{b}" for a, b in edges)
    return ", ".join(items) if items else "(none)"


def report(scores: list[Scored], gold: Gold, beta: float) -> str:
    lines: list[str] = []
    n_gold = len(gold.service_edges | gold.resource_edges)
    lines.append(f"Gold edges: {n_gold} ({len(gold.documented)} documented, {n_gold - len(gold.documented)} undocumented)")
    lines.append(f"F-beta beta={beta} (beta<1 weights precision; beta>1 weights recall)")
    lines.append("")
    for s in scores:
        lines.append(f"## {s.catalog}")
        lines.append(f"  precision {s.precision:.3f}  recall {s.recall:.3f}  F{beta:g} {s.fbeta:.3f}")
        lines.append(f"  documented-edge recall   {s.documented_recall:.3f}")
        lines.append(f"  undocumented-edge recall {s.undocumented_recall:.3f}   <- contamination probe")
        lines.append(f"  component recall {s.component_recall:.3f}")
        lines.append(f"  MISSED (false neg): {fmt_edges(s.fn)}")
        lines.append(f"  HALLUCINATED (false pos): {fmt_edges(s.fp)}")
        if s.extra_components:
            lines.append(f"  extra components (out of scope): {', '.join(s.extra_components)}")
        lines.append("")
    if len(scores) > 1:
        def stat(vals: list[float]) -> str:
            return f"{statistics.mean(vals):.3f} +/- {statistics.pstdev(vals):.3f}"
        lines.append("## aggregate (mean +/- stdev over %d runs)" % len(scores))
        lines.append(f"  precision {stat([s.precision for s in scores])}")
        lines.append(f"  recall    {stat([s.recall for s in scores])}")
        lines.append(f"  F{beta:g}      {stat([s.fbeta for s in scores])}")
        lines.append(f"  undocumented-edge recall {stat([s.undocumented_recall for s in scores])}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, nargs="+", required=True, help="One or more catalog JSON paths (repeat for N runs of a config).")
    parser.add_argument("--gold", type=Path, default=Path(__file__).parent / "workspaces/online-boutique/gold_architecture.json")
    parser.add_argument("--beta", type=float, default=1.0, help="F-beta beta. <1 penalises hallucinated edges harder (recommended for 'in the wild').")
    parser.add_argument("--json-output", type=Path, default=None)
    args = parser.parse_args()

    gold = load_gold(args.gold)
    scores = [score_one(c, gold, args.beta) for c in args.catalog]
    print(report(scores, gold, args.beta))

    if args.json_output:
        payload = [
            {
                "catalog": s.catalog, "precision": s.precision, "recall": s.recall, "fbeta": s.fbeta,
                "documented_recall": s.documented_recall, "undocumented_recall": s.undocumented_recall,
                "component_recall": s.component_recall,
                "missed": sorted(f"{a}->{b}" for a, b in s.fn),
                "hallucinated": sorted(f"{a}->{b}" for a, b in s.fp),
            }
            for s in scores
        ]
        args.json_output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
