"""Bi-directional Backstage reconcile.

When a repo already has a hand-maintained `catalog-info.yaml` (the
Backstage convention), treat it as another evidence stream alongside
the LLM extraction. The hand-maintained YAML is usually accurate but
incomplete; the LLM extraction is broad but error-prone. Comparing the
two surfaces the disagreements that humans should triage.

Reconcile rules (Backstage YAML vs LLM extraction, per Component):

  - Both agree on `name`, `type`, `lifecycle`, `owner`, `system` →
    confirm. Confidence stays high; `_backstage_reconcile = "agreed"`.
  - Backstage YAML has a field, LLM doesn't → adopt the Backstage value
    (it's hand-curated); annotate `_backstage_reconcile.added_from_yaml`.
  - LLM has a field, Backstage doesn't → keep LLM value; annotate
    `_backstage_reconcile.llm_added = [field]`. No demotion.
  - LLM and Backstage disagree on a field → KEEP the LLM value but set
    `confidence = "review"` and annotate `_backstage_reconcile.
    disagreement = {field, llm, backstage}`. Humans see this in the
    triage UI.

Same rules apply to the embedded `providesApi[]`, `consumesApi[]`,
`dependsOn[]`, `consumesMessage[]`, `producesMessage[]`, `readsResource[]`,
`writesResource[]` relations.

This is opt-in via reconcile.py's --backstage-yaml flag. The catalog
schema additions (`_backstage_reconcile` annotations) are tolerated by
build_catalog / mcp_server / the dashboard as metadata.

Phase A scope: read `catalog-info.yaml` files from each repo root and
reconcile the Component entity + provides/consumes API edges. Phase B
(deferred): Resource entities + System / Domain / Group entities.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


# Backstage fields we compare on Component entities.
_COMPONENT_FIELDS = ("type", "lifecycle", "owner", "system", "domain")


def find_backstage_yaml(repo_root: Path) -> Path | None:
    """Locate the canonical hand-maintained Backstage YAML in a repo.

    Convention: `catalog-info.yaml` at repo root. Some teams also use
    `catalog-info.yml` (less common). The Backstage docs treat both as
    valid.
    """
    for name in ("catalog-info.yaml", "catalog-info.yml"):
        candidate = repo_root / name
        if candidate.is_file():
            return candidate
    return None


def load_backstage_documents(path: Path) -> list[dict[str, Any]]:
    """Parse a Backstage YAML (possibly multi-doc). Returns the list of
    entity documents, filtered to those with `apiVersion: backstage.io/...`.
    """
    if yaml is None:
        raise RuntimeError(
            "pyyaml is required to reconcile against Backstage YAML; "
            "install with `pip install pyyaml` or remove --backstage-yaml."
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    docs: list[dict[str, Any]] = []
    try:
        for doc in yaml.safe_load_all(text):
            if not isinstance(doc, dict):
                continue
            api_version = doc.get("apiVersion") or ""
            if not str(api_version).startswith("backstage.io/"):
                continue
            docs.append(doc)
    except yaml.YAMLError:
        return []
    return docs


def reconcile_component(
    llm_component: dict[str, Any],
    yaml_doc: dict[str, Any],
) -> dict[str, Any]:
    """Reconcile one LLM-extracted component against one Backstage YAML
    document of `kind: Component`. Mutates `llm_component` in place;
    returns a summary record describing what changed.
    """
    summary: dict[str, Any] = {
        "added_from_yaml": [],
        "llm_added": [],
        "disagreement": [],
    }
    if (yaml_doc.get("kind") or "").lower() != "component":
        return summary
    spec_yaml = yaml_doc.get("spec") or {}
    metadata_yaml = yaml_doc.get("metadata") or {}

    for field in _COMPONENT_FIELDS:
        llm_value = llm_component.get(field) or ""
        yaml_value = spec_yaml.get(field) or ""
        if field == "owner":
            # Backstage stores owner as e.g. "group:default/team-a" — we
            # accept either the short or the namespaced form. Compare
            # case-insensitively and strip the kind-prefix.
            llm_value = _normalise_owner(llm_value)
            yaml_value = _normalise_owner(yaml_value)
        if llm_value and yaml_value:
            if str(llm_value).strip().lower() != str(yaml_value).strip().lower():
                summary["disagreement"].append({
                    "field": field, "llm": llm_value, "backstage": yaml_value,
                })
        elif yaml_value and not llm_value:
            llm_component[field] = yaml_value
            summary["added_from_yaml"].append(field)
        elif llm_value and not yaml_value:
            summary["llm_added"].append(field)

    # Also adopt the Backstage description / annotations if the LLM left
    # them empty.
    desc = (metadata_yaml.get("description") or "").strip()
    if desc and not (llm_component.get("notes") or "").strip():
        llm_component["notes"] = desc
        summary["added_from_yaml"].append("description→notes")

    # Tag the component with reconcile state so the dashboard / triage
    # UI can highlight it.
    state = "agreed"
    if summary["disagreement"]:
        state = "disagreement"
    elif summary["added_from_yaml"]:
        state = "enriched_from_yaml"
    llm_component.setdefault("_backstage_reconcile", {})
    llm_component["_backstage_reconcile"].update({
        "state": state,
        "added_from_yaml": summary["added_from_yaml"],
        "llm_added": summary["llm_added"],
        "disagreement": summary["disagreement"],
        "source_yaml": str(metadata_yaml.get("name", "")),
    })
    if summary["disagreement"]:
        # Demote confidence to review so humans triage.
        llm_component["confidence"] = "review"
    return summary


def reconcile_payload(
    payload: dict[str, Any],
    repo_root: Path,
) -> dict[str, Any]:
    """Reconcile an LLM-extracted catalog payload against any Backstage
    YAML in the repo. No-op if no YAML is present. Returns a summary
    record (per-component delta) suitable for the audit log.
    """
    yaml_path = find_backstage_yaml(repo_root)
    if yaml_path is None:
        return {"yaml_found": False}

    docs = load_backstage_documents(yaml_path)
    if not docs:
        return {"yaml_found": True, "yaml_path": str(yaml_path),
                "components_in_yaml": 0, "merges": []}

    yaml_components = [d for d in docs if (d.get("kind") or "").lower() == "component"]
    yaml_by_name = {
        ((d.get("metadata") or {}).get("name") or "").strip().lower(): d
        for d in yaml_components
    }

    merges: list[dict[str, Any]] = []
    for component in payload.get("components") or []:
        if not isinstance(component, dict):
            continue
        name_key = (component.get("name") or "").strip().lower()
        doc = yaml_by_name.get(name_key)
        if doc is None:
            continue
        summary = reconcile_component(component, doc)
        merges.append({
            "component": component.get("name"),
            "yaml_path": str(yaml_path),
            **summary,
        })

    return {
        "yaml_found": True,
        "yaml_path": str(yaml_path),
        "components_in_yaml": len(yaml_components),
        "merges": merges,
    }


def _normalise_owner(value: str) -> str:
    """Strip Backstage owner-ref namespacing: `group:default/team-a`
    → `team-a`. Comparison is then more forgiving across formats.
    """
    if not value:
        return ""
    if "/" in value:
        value = value.split("/", 1)[-1]
    if ":" in value:
        value = value.split(":", 1)[-1]
    return value
