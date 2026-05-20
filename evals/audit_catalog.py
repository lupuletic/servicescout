"""Audit a ServiceScout catalog for source-grounded catalog hygiene.

This is intentionally generic: it does not encode sock-shop expected answers.
It checks structural promises that should hold for any workspace:

  python evals/audit_catalog.py --catalog evals/data/catalog.json \
    --workspace evals/workspace --output docs/catalog-audit.md

Use --fail-on-high in CI or local eval loops when high-severity hygiene
findings should fail the run.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from servicescout.build_catalog import canonical_key  # noqa: E402


CORE_ENTITY_KINDS = {"Component", "API", "Resource", "Provider"}
RESOURCE_RELATIONS = {"readsResource", "writesResource", "producesMessage", "consumesMessage"}
CODE_RELATIONS = RESOURCE_RELATIONS | {"dependsOn", "consumesApi"}
STRUCTURAL_RELATIONS = {"partOf", "providesApi", "communicatesWith"}
SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1, "info": 0, "-": -1}


def ref_kind(ref: str) -> str:
    return ref.split(":", 1)[0] if ":" in ref else ""


def ref_name(ref: str) -> str:
    return ref.split(":", 1)[1] if ":" in ref else ref


def entity_ref(entity: dict[str, Any]) -> str:
    return f"{entity.get('kind')}:{entity.get('metadata', {}).get('name')}"


def relation_ref(relation: dict[str, Any]) -> str:
    return f"{relation.get('from')} -[{relation.get('type')}]-> {relation.get('to')}"


def _annotations(entity: dict[str, Any]) -> dict[str, Any]:
    return entity.get("metadata", {}).get("annotations", {}) or {}


def _source_repos_for_entity(entity: dict[str, Any]) -> list[str]:
    repos = _annotations(entity).get("source_repos") or []
    if isinstance(repos, str):
        repos = [repos]
    return [str(repo) for repo in repos if repo]


def _source_repos_for_relation(relation: dict[str, Any]) -> list[str]:
    props = relation.get("properties") or {}
    repos = props.get("source_repos") or []
    if isinstance(repos, str):
        repos = [repos]
    repo = props.get("source_repo")
    if repo:
        repos = [*repos, repo]
    return sorted({str(repo) for repo in repos if repo})


def _repo_dir_name(repo_id: str) -> str:
    return repo_id.rstrip("/").split("/")[-1]


def _normalise_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _candidate_files(workspace: Path | None, evidence_path: str, source_repos: list[str]) -> list[Path]:
    if not workspace or not evidence_path or not workspace.exists():
        return []

    candidates: list[Path] = []
    bases: list[Path] = []
    for repo_id in source_repos:
        repo_dir = workspace / _repo_dir_name(repo_id)
        if repo_dir.exists():
            bases.append(repo_dir)
    if not bases:
        bases = [child for child in workspace.iterdir() if child.is_dir()]

    for base in bases:
        candidate = base / evidence_path
        if candidate.exists():
            candidates.append(candidate)
    direct = workspace / evidence_path
    if direct.exists():
        candidates.append(direct)
    if not candidates:
        # Explicit monorepo extraction units may cite paths relative to the
        # physical repo root while their logical source repo id names a service.
        # Fall back to a bounded recursive lookup so source audits still verify
        # evidence without hardcoding a benchmark layout.
        candidates.extend(path for path in workspace.glob(f"**/{evidence_path}") if path.exists())
    return candidates


def evidence_status(evidence: list[dict[str, Any]], source_repos: list[str], workspace: Path | None) -> tuple[str, str]:
    if not evidence:
        return "missing", "no cited evidence"
    if not workspace:
        return "not_checked", f"{len(evidence)} citation(s), workspace not provided"

    matched = 0
    checked = 0
    missing_paths = 0
    for item in evidence:
        path = str(item.get("path") or "")
        snippet = str(item.get("snippet") or "")
        files = _candidate_files(workspace, path, source_repos)
        if not files:
            missing_paths += 1
            continue
        checked += 1
        want = _normalise_text(snippet)
        if not want:
            continue
        for file_path in files:
            try:
                haystack = _normalise_text(file_path.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
            if want in haystack:
                matched += 1
                break

    if matched == len(evidence):
        return "matched", f"{matched}/{len(evidence)} citation(s) matched"
    if matched:
        return "partial", f"{matched}/{len(evidence)} citation(s) matched; {missing_paths} missing path(s)"
    if checked:
        return "unmatched", f"0/{len(evidence)} citation(s) matched"
    return "missing_paths", f"0/{len(evidence)} citation path(s) found"


def _worst_findings(findings: list[tuple[str, str, str]]) -> tuple[str, str, str]:
    if not findings:
        return "PASS", "-", "source-backed structural checks passed"
    severity, code, message = max(findings, key=lambda item: SEVERITY_RANK.get(item[0], -1))
    return "FAIL" if severity == "high" else "REVIEW", severity, f"{code}: {message}"


def _names_by_kind(entities: list[dict[str, Any]]) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = defaultdict(set)
    for entity in entities:
        name = entity.get("metadata", {}).get("name")
        kind = entity.get("kind")
        if name and kind:
            grouped[canonical_key(name)].add(kind)
    return grouped


def audit_entity(entity: dict[str, Any], names_by_kind: dict[str, set[str]], workspace: Path | None) -> dict[str, str]:
    ref = entity_ref(entity)
    kind = entity.get("kind") or ""
    name = entity.get("metadata", {}).get("name") or ""
    annotations = _annotations(entity)
    source_repos = _source_repos_for_entity(entity)
    evidence = entity.get("evidence") or []
    findings: list[tuple[str, str, str]] = []

    colliding_kinds = names_by_kind.get(canonical_key(name), set()) - {kind}
    if kind == "Component" and ("Resource" in colliding_kinds or "Provider" in colliding_kinds):
        bad = ", ".join(sorted(colliding_kinds & {"Resource", "Provider"}))
        findings.append(("high", "wrong-kind-duplicate", f"component name also emitted as {bad}"))

    if kind in {"API", "Resource"} and not source_repos:
        findings.append(("high", "missing-provenance", f"{kind} has no source_repos annotation"))
    elif kind == "Provider" and not source_repos:
        findings.append(("medium", "missing-provenance", "provider has no source_repos annotation"))

    is_external = str(annotations.get("external") or "").lower() == "true"
    evidence_kind, evidence_detail = evidence_status(evidence, source_repos, workspace)
    if evidence_kind in {"unmatched", "missing_paths"}:
        findings.append(("high", "bad-evidence", evidence_detail))
    elif evidence_kind == "partial":
        findings.append(("medium", "partial-evidence", evidence_detail))
    elif evidence_kind == "missing" and not is_external:
        findings.append(("medium", "missing-evidence", evidence_detail))

    verdict, severity, finding = _worst_findings(findings)
    if kind not in CORE_ENTITY_KINDS:
        verdict = "OUT_OF_SCOPE" if verdict == "PASS" else verdict

    return {
        "scope": "entity",
        "fact": ref,
        "expected": "typed entity with provenance and matching source evidence",
        "actual": f"{kind}; source_repos={source_repos or '-'}; evidence={len(evidence)}",
        "verdict": verdict,
        "severity": severity,
        "source_evidence": evidence_detail,
        "recommended_fix": finding,
    }


def audit_relation(relation: dict[str, Any], entity_refs: set[str], names_by_kind: dict[str, set[str]], workspace: Path | None) -> dict[str, str]:
    rel_type = relation.get("type") or ""
    target_ref = relation.get("to") or ""
    source_ref = relation.get("from") or ""
    props = relation.get("properties") or {}
    source_repos = _source_repos_for_relation(relation)
    evidence = relation.get("evidence") or []
    findings: list[tuple[str, str, str]] = []

    if source_ref not in entity_refs:
        findings.append(("high", "missing-endpoint", f"source endpoint does not exist: {source_ref}"))
    if target_ref not in entity_refs:
        findings.append(("high", "missing-endpoint", f"target endpoint does not exist: {target_ref}"))

    target_kind = ref_kind(target_ref)
    if rel_type in RESOURCE_RELATIONS and target_kind != "Resource":
        findings.append(("high", "target-kind-mismatch", f"{rel_type} target is {target_kind}, expected Resource"))
    if rel_type == "consumesApi" and target_kind not in {"Component", "API"}:
        findings.append(("high", "target-kind-mismatch", f"consumesApi target is {target_kind}, expected Component or API"))

    expected_target_kind = str(props.get("target_kind") or "").lower()
    if expected_target_kind and expected_target_kind != target_kind.lower():
        findings.append(("high", "target-kind-property-mismatch", f"property target_kind={expected_target_kind}, target ref kind={target_kind.lower()}"))

    if target_kind == "Component":
        colliding_kinds = names_by_kind.get(canonical_key(ref_name(target_ref)), set()) - {"Component"}
        if colliding_kinds & {"Resource", "Provider"}:
            bad = ", ".join(sorted(colliding_kinds & {"Resource", "Provider"}))
            findings.append(("high", "wrong-kind-target", f"relation points to Component but same name exists as {bad}"))

    if rel_type in CODE_RELATIONS and not source_repos:
        findings.append(("medium", "missing-relation-provenance", "code relation has no properties.source_repo"))

    evidence_kind, evidence_detail = evidence_status(evidence, source_repos, workspace)
    if evidence_kind in {"unmatched", "missing_paths"}:
        findings.append(("high", "bad-evidence", evidence_detail))
    elif evidence_kind == "partial":
        findings.append(("medium", "partial-evidence", evidence_detail))
    elif evidence_kind == "missing" and rel_type not in STRUCTURAL_RELATIONS:
        findings.append(("medium", "missing-evidence", evidence_detail))

    verdict, severity, finding = _worst_findings(findings)
    return {
        "scope": "relation",
        "fact": relation_ref(relation),
        "expected": "existing endpoints, correct direction, correct target kind, matching evidence",
        "actual": f"{rel_type}; source_repo={source_repos[0] if source_repos else '-'}; evidence={len(evidence)}",
        "verdict": verdict,
        "severity": severity,
        "source_evidence": evidence_detail,
        "recommended_fix": finding,
    }


def audit_catalog(catalog: dict[str, Any], workspace: Path | None = None) -> dict[str, Any]:
    entities = catalog.get("entities") or []
    relations = catalog.get("relations") or []
    names_by_kind = _names_by_kind(entities)
    entity_refs = {entity_ref(entity) for entity in entities}

    rows: list[dict[str, str]] = []
    for entity in entities:
        if entity.get("kind") in CORE_ENTITY_KINDS:
            rows.append(audit_entity(entity, names_by_kind, workspace))
    for relation in relations:
        rows.append(audit_relation(relation, entity_refs, names_by_kind, workspace))

    severity_counts = Counter(row["severity"] for row in rows)
    verdict_counts = Counter(row["verdict"] for row in rows)
    gate_failures = [row for row in rows if row["severity"] == "high"]

    return {
        "summary": {
            "facts_audited": len(rows),
            "entities_audited": sum(1 for e in entities if e.get("kind") in CORE_ENTITY_KINDS),
            "relations_audited": len(relations),
            "high_findings": severity_counts.get("high", 0),
            "medium_findings": severity_counts.get("medium", 0),
            "low_findings": severity_counts.get("low", 0),
            "pass": verdict_counts.get("PASS", 0),
            "review": verdict_counts.get("REVIEW", 0),
            "fail": verdict_counts.get("FAIL", 0),
        },
        "rows": rows,
        "gate_failures": gate_failures,
    }


def _escape_md(value: Any) -> str:
    text = str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(audit: dict[str, Any], catalog_path: Path, workspace: Path | None) -> str:
    summary = audit["summary"]
    lines = [
        "# Catalog Source Audit",
        "",
        f"- catalog: `{catalog_path}`",
        f"- workspace: `{workspace}`" if workspace else "- workspace: not checked",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    for key in ("facts_audited", "entities_audited", "relations_audited", "pass", "review", "fail", "high_findings", "medium_findings"):
        lines.append(f"| {key} | {summary.get(key, 0)} |")

    lines.extend([
        "",
        "## Quality Gates",
        "",
        "| Gate | Status |",
        "| --- | --- |",
    ])
    evidence_reviews = [
        row for row in audit["rows"]
        if "partial-evidence" in row["recommended_fix"] or "bad-evidence" in row["recommended_fix"]
    ]
    gates = [
        ("No Resource-as-Component or Provider-as-Component duplicates", "FAIL" if "wrong-kind" in " ".join(row["recommended_fix"] for row in audit["gate_failures"]) else "PASS"),
        ("APIs and Resources carry source provenance", "FAIL" if any(row["scope"] == "entity" and "missing-provenance" in row["recommended_fix"] and ("API:" in row["fact"] or "Resource:" in row["fact"]) for row in audit["gate_failures"]) else "PASS"),
        ("Resource relation target kinds are respected", "FAIL" if any("target-kind" in row["recommended_fix"] for row in audit["gate_failures"]) else "PASS"),
        ("Relation endpoints exist", "FAIL" if any("missing-endpoint" in row["recommended_fix"] for row in audit["gate_failures"]) else "PASS"),
        ("Cited evidence resolves in workspace", "REVIEW" if evidence_reviews else "PASS"),
    ]
    for gate, status in gates:
        lines.append(f"| {gate} | {status} |")

    lines.extend([
        "",
        "## Fact Audit",
        "",
        "| Scope | Fact | Verdict | Severity | Expected | Actual | Source evidence | Recommended fix |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ])
    for row in audit["rows"]:
        lines.append(
            "| "
            + " | ".join(
                _escape_md(row[key])
                for key in ("scope", "fact", "verdict", "severity", "expected", "actual", "source_evidence", "recommended_fix")
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=PROJECT_ROOT / "evals" / "data" / "catalog.json")
    parser.add_argument("--workspace", type=Path, default=PROJECT_ROOT / "evals" / "workspace")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--json-output", type=Path, default=None)
    parser.add_argument("--fail-on-high", action="store_true")
    args = parser.parse_args()

    catalog = json.loads(args.catalog.read_text())
    workspace = args.workspace if args.workspace and args.workspace.exists() else None
    audit = audit_catalog(catalog, workspace)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(render_markdown(audit, args.catalog, workspace))
        print(f"wrote {args.output}")
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(audit, indent=2))
        print(f"wrote {args.json_output}")

    summary = audit["summary"]
    print(
        f"audited {summary['facts_audited']} facts: "
        f"{summary['pass']} pass, {summary['review']} review, {summary['fail']} fail; "
        f"high={summary['high_findings']}, medium={summary['medium_findings']}"
    )
    if args.fail_on_high and summary["high_findings"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
