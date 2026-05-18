"""Per-system CLAUDE.md / AGENTS.md emitter (Epic #9 Tier 2 #5).

For each `system` in the catalog, emit a markdown brief that an AI
coding agent can drop into a multi-repo workspace as `CLAUDE.md`
(Claude Code) or `AGENTS.md` (Codex / other harnesses). The file
gives the agent:

  - a one-paragraph summary of the system
  - the component inventory with each Component's tagline, runtime,
    lifecycle, and source repo
  - the system's external APIs and resources
  - the dependency map across components within the system AND to
    other systems (one direction at a time, with edge kind)
  - a pointer to the ServiceScout MCP server for follow-up queries

The output is intentionally **factual**, not narrative: it's a map an
agent reads BEFORE exploring, so it knows which repos to clone and
which MCP queries to make. Generic prose is left to the agent's own
analysis after it has the map.

Phase A:
  Writes files to `data/claude_md/<system_slug>.md`.
  Run as a one-off:
    python export_claude_md.py
    python export_claude_md.py --system "Sock Shop"
    python export_claude_md.py --output-dir /tmp/agent-briefs

Phase B (deferred):
  PR-bot mode that opens PRs into each owning repo with the rendered
  brief at the repo root. Out of scope for this commit.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


HERE = Path(__file__).parent
DEFAULT_CATALOG = HERE / "data" / "catalog.json"
DEFAULT_OUTPUT_DIR = HERE / "data" / "claude_md"


def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", name.strip())
    return s.strip("-").lower() or "system"


def _component_block(ent: dict[str, Any]) -> str:
    name = ent["metadata"]["name"]
    md = ent.get("metadata", {})
    spec = ent.get("spec", {})
    ann = md.get("annotations") or {}
    tagline = ann.get("tagline") or md.get("description") or ""
    runtime = spec.get("runtime") or ""
    lifecycle = spec.get("lifecycle") or ""
    src = ann.get("source_repos") or []
    bullets = []
    if tagline:
        bullets.append(tagline.strip())
    if runtime or lifecycle:
        bits = []
        if runtime:
            bits.append(f"runtime: `{runtime}`")
        if lifecycle:
            bits.append(f"lifecycle: `{lifecycle}`")
        bullets.append(" · ".join(bits))
    if src:
        repo_links = ", ".join(f"`{r}`" for r in src[:4])
        bullets.append(f"source repos: {repo_links}")
    body = "\n  - " + "\n  - ".join(bullets) if bullets else ""
    return f"- **{name}**{body}"


def _api_block(ent: dict[str, Any]) -> str:
    name = ent["metadata"]["name"]
    spec = ent.get("spec", {})
    api_type = spec.get("type") or "unknown"
    exposed_by = spec.get("exposed_by") or ""
    ops_count = len(spec.get("operations") or [])
    lifecycle = spec.get("lifecycle") or ""
    bits = [f"type: `{api_type}`"]
    if exposed_by:
        bits.append(f"exposed by `{exposed_by}`")
    if ops_count:
        bits.append(f"{ops_count} operation(s)")
    if lifecycle:
        bits.append(f"lifecycle: `{lifecycle}`")
    return f"- **{name}** — " + " · ".join(bits)


def _resource_block(ent: dict[str, Any]) -> str:
    name = ent["metadata"]["name"]
    spec = ent.get("spec", {})
    rtype = spec.get("type") or "unknown"
    tech = spec.get("technology") or ""
    bits = [f"type: `{rtype}`"]
    if tech:
        bits.append(f"technology: `{tech}`")
    return f"- **{name}** — " + " · ".join(bits)


def _dependency_line(rel: dict[str, Any]) -> str:
    kind = rel.get("type") or rel.get("kind") or "dependsOn"
    proto = (rel.get("properties") or {}).get("protocol") or ""
    suffix = f" ({proto})" if proto else ""
    return f"  - `{rel.get('from')}` →`{kind}`→ `{rel.get('to')}`{suffix}"


def _system_summary_paragraph(system_name: str, components: list[dict[str, Any]]) -> str:
    if not components:
        return f"`{system_name}` has no components yet in the catalog."
    n = len(components)
    runtimes = sorted({(c.get("spec") or {}).get("runtime") for c in components if (c.get("spec") or {}).get("runtime")})
    repos = sorted({r for c in components for r in (c.get("metadata", {}).get("annotations") or {}).get("source_repos") or []})
    runtime_phrase = (
        f" Mixed runtime: {', '.join(f'`{r}`' for r in runtimes if r)}." if len(runtimes) > 1
        else (f" Runtime: `{next(iter(runtimes))}`." if runtimes else "")
    )
    repos_phrase = f" Backed by {len(repos)} repo(s)." if repos else ""
    return f"`{system_name}` has **{n} component(s)** in the catalog.{runtime_phrase}{repos_phrase}"


def render_system(
    system_name: str,
    components: list[dict[str, Any]],
    apis: list[dict[str, Any]],
    resources: list[dict[str, Any]],
    internal_relations: list[dict[str, Any]],
    outbound_relations: list[dict[str, Any]],
    inbound_relations: list[dict[str, Any]],
) -> str:
    parts: list[str] = []
    parts.append(f"# `{system_name}` — system brief")
    parts.append("")
    parts.append(
        "> Generated by ServiceScout. Drop this file in your multi-repo "
        "workspace as `CLAUDE.md` or `AGENTS.md` so an AI agent has the "
        "map before it starts exploring. Update by re-running "
        "`python export_claude_md.py` after each catalog rebuild."
    )
    parts.append("")
    parts.append(_system_summary_paragraph(system_name, components))
    parts.append("")

    parts.append("## Components")
    parts.append("")
    if components:
        for c in sorted(components, key=lambda e: e["metadata"]["name"].lower()):
            parts.append(_component_block(c))
    else:
        parts.append("_(none catalogued)_")
    parts.append("")

    parts.append("## APIs exposed by this system")
    parts.append("")
    if apis:
        for a in sorted(apis, key=lambda e: e["metadata"]["name"].lower()):
            parts.append(_api_block(a))
    else:
        parts.append("_(none catalogued)_")
    parts.append("")

    parts.append("## Resources owned or used by this system")
    parts.append("")
    if resources:
        for r in sorted(resources, key=lambda e: e["metadata"]["name"].lower()):
            parts.append(_resource_block(r))
    else:
        parts.append("_(none catalogued)_")
    parts.append("")

    parts.append("## Internal dependencies (within this system)")
    parts.append("")
    if internal_relations:
        for rel in internal_relations:
            parts.append(_dependency_line(rel))
    else:
        parts.append("_(no internal edges catalogued)_")
    parts.append("")

    parts.append("## Outbound dependencies (this system → other systems / providers)")
    parts.append("")
    if outbound_relations:
        for rel in outbound_relations:
            parts.append(_dependency_line(rel))
    else:
        parts.append("_(no outbound edges catalogued)_")
    parts.append("")

    parts.append("## Inbound dependencies (other systems → this system)")
    parts.append("")
    if inbound_relations:
        for rel in inbound_relations:
            parts.append(_dependency_line(rel))
    else:
        parts.append("_(no inbound edges catalogued)_")
    parts.append("")

    parts.append("## Asking ServiceScout for more")
    parts.append("")
    parts.append(
        "If you need to drill deeper than this map, query the ServiceScout "
        "MCP server. The MCP tools are designed to be used in this order:"
    )
    parts.append("")
    parts.append(
        "1. `servicescout_search(\"<question>\")` — hybrid retrieval over the catalog. "
        "Returns the top entities for a free-text question with score, tagline, source repos."
    )
    parts.append(
        "2. `servicescout_describe(<ref>)` — full record (metadata, spec, annotations, evidence) for one entity."
    )
    parts.append(
        "3. `servicescout_neighbors(<ref>, direction=\"out\"|\"in\"|\"both\")` — one-hop graph traversal."
    )
    parts.append(
        "4. `servicescout_trace(<start>, end=<end>)` — multi-hop journey planner with async chains."
    )
    parts.append(
        "5. `servicescout_evidence(<source>, <target>)` — file:line citations on an edge. "
        "Use this to VERIFY any hop before including it in an answer."
    )
    parts.append(
        "6. `servicescout_glossary(<term>)` — direct vocab lookup. Cheaper than `search` when "
        "you already know the term."
    )
    parts.append(
        "7. `servicescout_owners(<entity>)` — owner / lifecycle lookup. Cheaper than `describe` "
        "when you only need to know who to PR against."
    )
    parts.append("")
    parts.append(
        "Every hop in this map is a candidate. Confirm with `servicescout_evidence` and "
        "by reading the cited source files before including a fact in an answer."
    )
    parts.append("")
    return "\n".join(parts) + "\n"


def _split_relations(
    relations: list[dict[str, Any]],
    in_system_refs: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split relations into (internal, outbound, inbound) for one system."""
    internal: list[dict[str, Any]] = []
    outbound: list[dict[str, Any]] = []
    inbound: list[dict[str, Any]] = []
    for rel in relations:
        src = rel.get("from") or ""
        tgt = rel.get("to") or ""
        src_in = src in in_system_refs
        tgt_in = tgt in in_system_refs
        if src_in and tgt_in:
            internal.append(rel)
        elif src_in and not tgt_in:
            outbound.append(rel)
        elif tgt_in and not src_in:
            inbound.append(rel)
    return internal, outbound, inbound


def write_per_system(
    catalog: dict[str, Any],
    output_dir: Path,
    *,
    only_system: str | None = None,
) -> list[Path]:
    """Render and write one markdown file per system. Returns the list
    of paths written.
    """
    entities = catalog.get("entities") or []
    relations = catalog.get("relations") or []

    by_system: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in entities:
        system = (e.get("spec") or {}).get("system") or ""
        if not system:
            continue
        if only_system and system != only_system:
            continue
        by_system[system].append(e)

    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for system_name, members in sorted(by_system.items()):
        refs_in_system = {f"{m['kind']}:{m['metadata']['name']}" for m in members}
        components = [m for m in members if m["kind"] == "Component"]
        apis = [m for m in members if m["kind"] == "API"]
        resources = [m for m in members if m["kind"] == "Resource"]

        # Include API / Resource entities reachable from a Component of
        # this system, even if the entity's own `system` field is empty
        # (typical — only Components carry the system tag explicitly).
        # We follow `providesApi`, `readsResource`, `writesResource`,
        # `producesMessage`, `consumesMessage` edges out of in-system
        # Components.
        component_refs = {f"Component:{c['metadata']['name']}" for c in components}
        api_refs: set[str] = set()
        resource_refs: set[str] = set()
        for rel in relations:
            src = rel.get("from") or ""
            tgt = rel.get("to") or ""
            kind = rel.get("type") or rel.get("kind") or ""
            if src not in component_refs:
                continue
            if tgt.startswith("API:") and kind in {"providesApi", "exposesApi"}:
                api_refs.add(tgt)
            elif tgt.startswith("Resource:") and kind in {
                "readsResource", "writesResource",
                "producesMessage", "consumesMessage",
                "ownsResource", "usesResource",
            }:
                resource_refs.add(tgt)
        ents_by_ref = {f"{e['kind']}:{e['metadata']['name']}": e for e in entities}
        for r in api_refs:
            e = ents_by_ref.get(r)
            if e and e not in apis:
                apis.append(e)
                refs_in_system.add(r)
        for r in resource_refs:
            e = ents_by_ref.get(r)
            if e and e not in resources:
                resources.append(e)
                refs_in_system.add(r)

        internal, outbound, inbound = _split_relations(relations, refs_in_system)
        text = render_system(
            system_name,
            components, apis, resources,
            internal, outbound, inbound,
        )
        path = output_dir / f"{_slug(system_name)}.md"
        path.write_text(text, encoding="utf-8")
        written.append(path)

    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--system", default=None,
                        help="Only emit the markdown for this system. Default: all systems.")
    args = parser.parse_args()

    if not args.catalog.exists():
        raise SystemExit(f"catalog not found at {args.catalog}")
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))

    written = write_per_system(catalog, args.output_dir, only_system=args.system)
    if not written:
        if args.system:
            print(f"no entities found for system {args.system!r}")
            return 1
        print(
            "no entities had a `system` field. Set repo.system on your extractions "
            "(see `extractor.py` PROMPT_TEMPLATE) or pass --system to override."
        )
        return 1
    print(f"wrote {len(written)} system brief(s):")
    for p in written:
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
