"""Walk a repo, dispatch each file to the right mini-extractor, return
all emitted facts.

Single-pass walk. Each mini-extractor declares its file predicate so the
walker doesn't need glob patterns for every format. The output is a flat
list of `Fact` records; the merger (see `merge_into_payload`) folds them
into a Backstage-shaped catalog payload using reconcile rules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from static_extractors.mini import Fact, api_specs, build_manifests, dockerfile, kubernetes


# Each entry: (predicate, extractor). Predicates are cheap (filename /
# suffix / first-2KB sniff); extractors run the real parse.
_PIPELINE: tuple[tuple[Callable[[Path], bool], Callable[[Path, Path], list[Fact]]], ...] = (
    (dockerfile.is_dockerfile, dockerfile.extract),
    (kubernetes.is_k8s_yaml, kubernetes.extract),
    (build_manifests.is_build_manifest, build_manifests.extract),
    # api_specs covers .proto / .graphql / openapi.yaml — its predicate
    # discriminates internally. We just always call it on the small set
    # of plausible files.
    (lambda p: api_specs.is_proto(p) or api_specs.is_graphql(p) or api_specs.is_openapi(p), api_specs.extract),
)


_SKIP_DIR_PARTS = frozenset(
    {".git", "node_modules", "target", "build", "dist", "out", "vendor",
     ".venv", "venv", "__pycache__",
     # Test/fixture directories — Dockerfiles and manifests here describe
     # the test harness, not the runtime topology. Excluding them prevents
     # `test/Dockerfile` (often FROM python:alpine) from masquerading as
     # the service's actual runtime.
     "test", "tests", "__tests__", "spec", "specs", "e2e",
     # Example / sample / mock directories often have placeholder configs.
     "example", "examples", "sample", "samples"}
)


def _should_skip(rel_path: Path) -> bool:
    return bool(set(rel_path.parts) & _SKIP_DIR_PARTS)


def extract_all(repo_root: Path) -> list[Fact]:
    """Run every mini-extractor across the repo. Returns a flat list of
    facts. Caller is responsible for merging into a payload."""
    repo_root = repo_root.resolve()
    out: list[Fact] = []
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(repo_root)
        except ValueError:
            continue
        if _should_skip(rel):
            continue
        for predicate, extractor in _PIPELINE:
            try:
                if predicate(path):
                    out.extend(extractor(path, repo_root))
            except Exception:  # noqa: BLE001
                # Mini-extractor failures don't abort the walk. A single
                # malformed YAML / pom shouldn't lose the rest.
                continue
    return out


# --------------------------------------------------------------------------- #
# Merge into a Backstage-shaped payload
# --------------------------------------------------------------------------- #


def merge_into_payload(payload: dict[str, Any], facts: list[Fact]) -> dict[str, Any]:
    """Fold mini-extractor facts into an LLM-extracted payload.

    Reconcile rules:

      - Same category + same `name` already present in payload  →  merge:
        - append the mini-extractor's evidence items (capped at 8 total
          per fact to avoid evidence-list bloat)
        - merge `tags` (unique set)
        - record `_emitted_by` rule names alongside any existing emitters
        - leave other LLM-supplied fields alone (taglines, notes, etc.)
      - New `name` in this category                              →  add the
        whole fact body.

    Returns a summary describing what was merged vs added.
    """
    summary = {"added": 0, "merged": 0, "by_category": {}}
    for fact in facts:
        cat = fact.category
        body = fact.body
        if cat not in payload or not isinstance(payload[cat], list):
            payload[cat] = []
        existing = next(
            (i for i, item in enumerate(payload[cat])
             if isinstance(item, dict) and item.get("name") == body.get("name")),
            None,
        )
        if existing is None:
            # Add new
            body.setdefault("_emitted_by", []).append(fact.rule)
            payload[cat].append(body)
            summary["added"] += 1
            summary["by_category"].setdefault(cat, {"added": 0, "merged": 0})["added"] += 1
        else:
            target = payload[cat][existing]
            # Merge evidence (capped).
            ev_existing = target.get("evidence") or []
            ev_new = body.get("evidence") or []
            target["evidence"] = (ev_existing + ev_new)[:8]
            # Merge tags.
            t_existing = set(target.get("tags") or [])
            t_new = set(body.get("tags") or [])
            target["tags"] = sorted(t_existing | t_new)
            target.setdefault("_emitted_by", []).append(fact.rule)
            summary["merged"] += 1
            summary["by_category"].setdefault(cat, {"added": 0, "merged": 0})["merged"] += 1
    return summary
