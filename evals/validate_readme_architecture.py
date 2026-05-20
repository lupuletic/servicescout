"""Validate an eval catalog against a workspace's README architecture contract.

This is a zero-LLM regression check. It compares the generated ServiceScout
catalog against a small, reviewed JSON contract extracted from the public
README architecture table/diagram for a benchmark workspace.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from workspace_paths import resolve_workspace


PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"


@dataclass(frozen=True)
class Finding:
    status: str
    check: str
    subject: str
    detail: str


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def entity_ref(entity: dict[str, Any]) -> str:
    return f"{entity.get('kind')}:{(entity.get('metadata') or {}).get('name')}"


def entity_text(entity: dict[str, Any]) -> str:
    meta = entity.get("metadata") or {}
    annotations = meta.get("annotations") or {}
    spec = entity.get("spec") or {}
    fields = [
        meta.get("name"),
        meta.get("description"),
        annotations.get("tagline"),
        annotations.get("capability_sheet"),
        spec.get("type"),
        spec.get("runtime"),
        " ".join(meta.get("tags") or []),
    ]
    return " ".join(str(value) for value in fields if value).lower()


def has_any_term(text: str, terms: list[str]) -> bool:
    return any(term.lower() in text for term in terms)


def github_repo_root(repo_id: str) -> str:
    parts = [part for part in repo_id.split("/") if part]
    if len(parts) < 2:
        return ""
    return "/".join(parts[:2])


def github_source_location(repo_id: str) -> str:
    parts = [part for part in repo_id.split("/") if part]
    if len(parts) < 2:
        return ""
    return f"https://github.com/{parts[0]}/{parts[1]}"


def validate_readme_mentions(readme_text: str, expected: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    lowered = readme_text.lower()
    for component in expected["expected_components"]:
        name = component["name"]
        status = PASS if name.lower() in lowered else FAIL
        findings.append(Finding(status, "readme_component_mentioned", name, "README names this service"))
    for flow in expected["expected_flows"]:
        # The README diagram is an image, so the text cannot prove every arrow.
        # We still assert both endpoint names are in the README service table.
        subject = f"{flow['from']} -> {flow['to']}"
        endpoints_present = flow["from"].lower() in lowered and flow["to"].lower() in lowered
        status = PASS if endpoints_present else WARN
        findings.append(Finding(status, "readme_flow_endpoints_mentioned", subject, flow["readme_basis"]))
    return findings


def validate_components(catalog: dict[str, Any], expected: dict[str, Any]) -> list[Finding]:
    entities = {entity_ref(entity): entity for entity in catalog.get("entities") or []}
    findings: list[Finding] = []
    for component in expected["expected_components"]:
        name = component["name"]
        ref = f"Component:{name}"
        entity = entities.get(ref)
        if not entity:
            findings.append(Finding(FAIL, "component_present", name, "README service missing from catalog"))
            continue
        findings.append(Finding(PASS, "component_present", name, "README service exists as Component"))

        spec_type = ((entity.get("spec") or {}).get("type") or "").lower()
        accepted_types = [value.lower() for value in component.get("accepted_types") or []]
        type_status = PASS if spec_type in accepted_types else WARN
        findings.append(Finding(type_status, "component_type", name, f"type={spec_type or '-'} expected one of {accepted_types}"))

        text = entity_text(entity)
        language_status = PASS if has_any_term(text, component.get("language_terms") or []) else WARN
        findings.append(Finding(language_status, "component_language", name, f"expected language terms {component.get('language_terms') or []}"))

        role_status = PASS if has_any_term(text, component.get("role_terms") or []) else WARN
        findings.append(Finding(role_status, "component_role", name, f"expected role terms {component.get('role_terms') or []}"))
    return findings


def validate_flows(catalog: dict[str, Any], expected: dict[str, Any]) -> list[Finding]:
    accepted_types = set(expected.get("accepted_flow_relation_types") or ["communicatesWith", "consumesApi"])
    relations = catalog.get("relations") or []
    findings: list[Finding] = []
    for flow in expected["expected_flows"]:
        source = f"Component:{flow['from']}"
        target = f"Component:{flow['to']}"
        matches = [
            relation for relation in relations
            if relation.get("from") == source
            and relation.get("to") == target
            and relation.get("type") in accepted_types
        ]
        if matches:
            relation_types = sorted({str(match.get("type")) for match in matches})
            findings.append(Finding(PASS, "readme_flow_present", f"{flow['from']} -> {flow['to']}", f"relations={relation_types}"))
        else:
            findings.append(Finding(FAIL, "readme_flow_present", f"{flow['from']} -> {flow['to']}", flow["readme_basis"]))
    return findings


def validate_resources(catalog: dict[str, Any], expected: dict[str, Any]) -> list[Finding]:
    entities = {entity_ref(entity): entity for entity in catalog.get("entities") or []}
    relations = catalog.get("relations") or []
    findings: list[Finding] = []
    for resource in expected.get("expected_resources") or []:
        ref = f"{resource['kind']}:{resource['name']}"
        entity = entities.get(ref)
        if not entity:
            findings.append(Finding(FAIL, "resource_present", resource["name"], "README/deployment resource missing from catalog"))
            continue
        text = entity_text(entity)
        term_status = PASS if has_any_term(text, resource.get("terms") or []) else WARN
        findings.append(Finding(term_status, "resource_terms", resource["name"], f"expected terms {resource.get('terms') or []}"))

        accepted = set(resource.get("accepted_relation_types") or [])
        source = f"Component:{resource['used_by']}"
        matches = [
            relation for relation in relations
            if relation.get("from") == source
            and relation.get("to") == ref
            and relation.get("type") in accepted
        ]
        status = PASS if matches else FAIL
        detail = f"{resource['used_by']} relations={sorted({m.get('type') for m in matches})}" if matches else f"missing {resource['used_by']} -> {resource['name']}"
        findings.append(Finding(status, "resource_used_by_expected_component", resource["name"], detail))
    return findings


def validate_extras(catalog: dict[str, Any], expected: dict[str, Any]) -> list[Finding]:
    expected_names = {component["name"] for component in expected["expected_components"]}
    allowed = {component["name"]: component["reason"] for component in expected.get("allowed_extra_components") or []}
    components = [
        entity for entity in catalog.get("entities") or []
        if entity.get("kind") == "Component"
    ]
    findings: list[Finding] = []
    for entity in sorted(components, key=lambda value: (value.get("metadata") or {}).get("name") or ""):
        name = (entity.get("metadata") or {}).get("name") or ""
        if name in expected_names:
            continue
        annotations = (entity.get("metadata") or {}).get("annotations") or {}
        if name in allowed:
            findings.append(Finding(PASS, "extra_component_allowed", name, allowed[name]))
        elif annotations.get("external") == "true":
            findings.append(Finding(WARN, "extra_component_external", name, "external/unresolved component outside README base architecture"))
        else:
            findings.append(Finding(FAIL, "extra_component_unexpected", name, "component is not in README base architecture or allowed extras"))
    return findings


def validate_source_repos(catalog: dict[str, Any], expected: dict[str, Any]) -> list[Finding]:
    source_root = expected.get("source_repo_root") or ""
    findings: list[Finding] = []
    for component in expected["expected_components"]:
        ref = f"Component:{component['name']}"
        entity = next(
            (
                candidate for candidate in catalog.get("entities") or []
                if candidate.get("kind") == "Component"
                and (candidate.get("metadata") or {}).get("name") == component["name"]
            ),
            None,
        )
        if not entity:
            continue
        annotations = (entity.get("metadata") or {}).get("annotations") or {}
        repos = annotations.get("source_repos") or []
        if not repos:
            findings.append(Finding(FAIL, "source_repo_present", ref, "missing source_repos annotation"))
            continue
        roots = sorted({github_repo_root(str(repo)) for repo in repos})
        status = PASS if roots == [source_root] else FAIL
        findings.append(Finding(status, "source_repo_root", ref, f"roots={roots} expected={source_root}"))

        expected_location = github_source_location(str(repos[0]))
        actual_location = str(annotations.get("github.com/source-location") or "")
        if actual_location == expected_location:
            findings.append(Finding(PASS, "source_location_precise", ref, expected_location))
        else:
            findings.append(Finding(WARN, "source_location_precise", ref, f"actual={actual_location or '-'} expected={expected_location}"))
    return findings


def summarise(findings: list[Finding]) -> dict[str, int]:
    return {
        PASS: sum(1 for finding in findings if finding.status == PASS),
        WARN: sum(1 for finding in findings if finding.status == WARN),
        FAIL: sum(1 for finding in findings if finding.status == FAIL),
    }


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def markdown_report(*, workspace: str, catalog_path: Path, expected_path: Path, findings: list[Finding]) -> str:
    summary = summarise(findings)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    lines = [
        "# Online Boutique README Architecture Validation",
        "",
        f"Generated: {now}",
        f"Workspace: `{workspace}`",
        f"Catalog: `{display_path(catalog_path)}`",
        f"Expectation: `{display_path(expected_path)}`",
        "",
        "## Summary",
        "",
        f"- PASS: {summary[PASS]}",
        f"- WARN: {summary[WARN]}",
        f"- FAIL: {summary[FAIL]}",
        "",
        "## Findings",
        "",
        "| Status | Check | Subject | Detail |",
        "| --- | --- | --- | --- |",
    ]
    for finding in findings:
        detail = re.sub(r"\s+", " ", finding.detail).replace("|", "\\|")
        subject = finding.subject.replace("|", "\\|")
        lines.append(f"| {finding.status} | `{finding.check}` | `{subject}` | {detail} |")
    lines.append("")
    return "\n".join(lines)


def run_validation(*, workspace: str, catalog_path: Path, expected_path: Path) -> tuple[dict[str, Any], str]:
    expected = load_json(expected_path)
    catalog = load_json(catalog_path)
    readme_path = expected_path.parent / expected.get("source_readme", "")
    readme_text = readme_path.read_text(encoding="utf-8") if readme_path.is_file() else ""

    findings: list[Finding] = []
    if readme_text:
        findings.extend(validate_readme_mentions(readme_text, expected))
    else:
        findings.append(Finding(WARN, "readme_available", str(readme_path), "README file not found; skipped README text checks"))
    findings.extend(validate_components(catalog, expected))
    findings.extend(validate_flows(catalog, expected))
    findings.extend(validate_resources(catalog, expected))
    findings.extend(validate_extras(catalog, expected))
    findings.extend(validate_source_repos(catalog, expected))

    summary = summarise(findings)
    payload = {
        "workspace": workspace,
        "catalog": str(catalog_path),
        "expectation": str(expected_path),
        "summary": summary,
        "findings": [finding.__dict__ for finding in findings],
    }
    md = markdown_report(workspace=workspace, catalog_path=catalog_path, expected_path=expected_path, findings=findings)
    return payload, md


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default="online-boutique", help="Eval workspace slug.")
    parser.add_argument("--catalog", type=Path, default=None, help="Catalog JSON path. Defaults to the workspace data catalog.")
    parser.add_argument("--expectation", type=Path, default=None, help="README architecture contract JSON.")
    parser.add_argument("--json-output", type=Path, default=None, help="Optional JSON report path.")
    parser.add_argument("--markdown-output", type=Path, default=None, help="Optional markdown report path.")
    parser.add_argument("--strict-warnings", action="store_true", help="Exit non-zero on warnings as well as failures.")
    args = parser.parse_args()

    ws = resolve_workspace(args.workspace)
    catalog_path = (args.catalog or (ws.data_dir / "catalog.json")).resolve()
    expected_path = (args.expectation or (ws.root / "readme_architecture.json")).resolve()
    payload, md = run_validation(workspace=ws.name, catalog_path=catalog_path, expected_path=expected_path)

    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(md, encoding="utf-8")

    print(json.dumps(payload["summary"], sort_keys=True))
    if payload["summary"][FAIL] > 0:
        return 1
    if args.strict_warnings and payload["summary"][WARN] > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
