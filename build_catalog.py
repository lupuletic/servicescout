"""Merge per-repo catalog extractions into a single Backstage-aligned catalog.

Inputs:
  data/catalog/*.json  (one per repo, produced by extractor.py)
  seeds/*.json         (optional, additive architecture facts)

Output:
  data/catalog.json

The catalog uses Backstage's node kinds: Component, API, Resource, System,
Domain, Group. Relations: ownedBy, partOf, dependsOn, providesApi, consumesApi,
hasPart. Every node and edge carries provenance (source repo, evidence, run id)
and a confidence label.

Service-identity reconciliation:
- A Component is keyed by its canonical name (lowercased, hyphens, suffixes like
  api/service/fe/backend stripped when the stem is ≥3 chars).
- Hostnames, config keys, generated client classes are attached as
  `metadata.annotations.aliases[]` on the Component, not as separate nodes.
- A dependsOn edge resolves its `target` to the Component that owns the alias,
  if any; otherwise the edge target becomes an `external` Component.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

from repo_discovery import find_repos, load_workspace_config


HERE = Path(__file__).parent
DEFAULT_CATALOG_DIR = HERE / "data" / "catalog"
DEFAULT_OUTPUT = HERE / "data" / "catalog.json"
DEFAULT_SEEDS_DIR = HERE / "seeds"


SUFFIXES_TO_STRIP = ["serviceapi", "apiservice", "service", "api", "backend", "frontend", "client", "gateway"]
SUFFIX_ALLOWLIST = {"paymentinterface", "userinterface"}


def canonical_key(name: str) -> str:
    if not name:
        return ""
    key = re.sub(r"[^a-z0-9]+", "", name.lower())
    if key in SUFFIX_ALLOWLIST:
        return key
    changed = True
    while changed:
        changed = False
        for suffix in SUFFIXES_TO_STRIP:
            if key.endswith(suffix) and len(key) - len(suffix) >= 3:
                key = key[: -len(suffix)]
                changed = True
                break
    return key


def host_to_key(host: str) -> str:
    if not host:
        return ""
    host = host.lower()
    host = re.sub(r"^https?://", "", host)
    host = host.split("/", 1)[0]
    host = host.split(":", 1)[0]
    label = host.split(".", 1)[0]
    return canonical_key(label)


class Catalog:
    def __init__(self) -> None:
        self.entities: dict[str, dict[str, Any]] = {}
        self.relations: list[dict[str, Any]] = []
        self._relation_keys: set[str] = set()
        self._alias_index: dict[str, str] = {}

    def upsert(self, entity: dict[str, Any]) -> dict[str, Any]:
        key = entity_ref(entity)
        existing = self.entities.get(key)
        if existing is None:
            self.entities[key] = entity
            self._index_aliases(entity)
            return entity
        merge_entity(existing, entity)
        self._index_aliases(existing)
        return existing

    def _index_aliases(self, entity: dict[str, Any]) -> None:
        if entity["kind"] != "Component":
            return
        name = entity["metadata"]["name"]
        ref = entity_ref(entity)
        self._alias_index.setdefault(canonical_key(name), ref)
        annotations = entity["metadata"].setdefault("annotations", {})
        aliases = annotations.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [a.strip() for a in aliases.split(",") if a.strip()]
        for alias in aliases:
            key = canonical_key(alias) or host_to_key(alias)
            if key:
                self._alias_index.setdefault(key, ref)

    def add_relation(self, relation: dict[str, Any]) -> None:
        key = relation_key(relation)
        if key in self._relation_keys:
            existing = next(r for r in self.relations if relation_key(r) == key)
            merge_relation(existing, relation)
            return
        self._relation_keys.add(key)
        self.relations.append(relation)

    def resolve_alias(self, label: str) -> str | None:
        key = canonical_key(label) or host_to_key(label)
        return self._alias_index.get(key)

    def to_json(self) -> dict[str, Any]:
        entities_sorted = [self.entities[k] for k in sorted(self.entities)]
        relations_sorted = sorted(self.relations, key=lambda r: (r["from"], r["type"], r["to"]))
        counts: dict[str, int] = {}
        for entity in entities_sorted:
            counts[entity["kind"]] = counts.get(entity["kind"], 0) + 1
        edge_counts: dict[str, int] = {}
        for relation in relations_sorted:
            edge_counts[relation["type"]] = edge_counts.get(relation["type"], 0) + 1
        return {
            "schema_version": "catalog-v1",
            "summary": {"node_kinds": counts, "relation_types": edge_counts, "entities": len(entities_sorted), "relations": len(relations_sorted)},
            "entities": entities_sorted,
            "relations": relations_sorted,
        }


def entity_ref(entity: dict[str, Any]) -> str:
    return f"{entity['kind']}:{entity['metadata']['name']}"


def relation_key(relation: dict[str, Any]) -> str:
    return f"{relation['from']}|{relation['type']}|{relation['to']}"


def merge_entity(into: dict[str, Any], other: dict[str, Any]) -> None:
    meta_into = into.setdefault("metadata", {})
    meta_other = other.get("metadata", {})
    for key in ("description", "title"):
        if not meta_into.get(key) and meta_other.get(key):
            meta_into[key] = meta_other[key]
    tags = set(meta_into.get("tags") or []) | set(meta_other.get("tags") or [])
    if tags:
        meta_into["tags"] = sorted(tags)
    annotations_into = meta_into.setdefault("annotations", {})
    for key, value in (meta_other.get("annotations") or {}).items():
        if key == "aliases":
            existing = annotations_into.get("aliases") or []
            if isinstance(existing, str):
                existing = [a.strip() for a in existing.split(",") if a.strip()]
            new = value if isinstance(value, list) else [a.strip() for a in str(value).split(",") if a.strip()]
            annotations_into["aliases"] = sorted(set(existing) | set(new))
        elif key == "source_repos":
            existing = set(annotations_into.get("source_repos") or [])
            new = value if isinstance(value, list) else [value]
            annotations_into["source_repos"] = sorted(existing | set(new))
        else:
            annotations_into.setdefault(key, value)
    spec_into = into.setdefault("spec", {})
    for key, value in (other.get("spec") or {}).items():
        if key in {"domain_attributes", "glossary"} and isinstance(value, list):
            dedup_field = "attribute" if key == "domain_attributes" else "term"
            existing = spec_into.get(key) or []
            seen = {item.get(dedup_field) for item in existing if isinstance(item, dict)}
            for item in value:
                if not isinstance(item, dict):
                    continue
                ident = item.get(dedup_field)
                if ident and ident not in seen:
                    existing.append(item)
                    seen.add(ident)
            spec_into[key] = existing
        elif key in spec_into and isinstance(spec_into[key], list) and isinstance(value, list):
            try:
                spec_into[key] = sorted(set(spec_into[key]) | set(value))
            except TypeError:
                spec_into[key] = spec_into[key] + [v for v in value if v not in spec_into[key]]
        else:
            spec_into.setdefault(key, value)
    evidence_into = into.setdefault("evidence", [])
    seen = {(e.get("path"), e.get("line")) for e in evidence_into}
    for ev in other.get("evidence") or []:
        sig = (ev.get("path"), ev.get("line"))
        if sig not in seen:
            evidence_into.append(ev)
            seen.add(sig)
    if other.get("confidence"):
        into["confidence"] = pick_higher_confidence(into.get("confidence"), other["confidence"])


def merge_relation(into: dict[str, Any], other: dict[str, Any]) -> None:
    evidence_into = into.setdefault("evidence", [])
    seen = {(e.get("path"), e.get("line")) for e in evidence_into}
    for ev in other.get("evidence") or []:
        sig = (ev.get("path"), ev.get("line"))
        if sig not in seen:
            evidence_into.append(ev)
            seen.add(sig)
    props_into = into.setdefault("properties", {})
    for key, value in (other.get("properties") or {}).items():
        if key == "aliases":
            existing = set(props_into.get("aliases") or [])
            new = value if isinstance(value, list) else [value]
            props_into["aliases"] = sorted(existing | set(new))
        else:
            props_into.setdefault(key, value)
    if other.get("confidence"):
        into["confidence"] = pick_higher_confidence(into.get("confidence"), other["confidence"])


CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1, "review": 0, None: -1, "": -1}


def pick_higher_confidence(a: str | None, b: str | None) -> str:
    return a if CONFIDENCE_RANK.get(a, -1) >= CONFIDENCE_RANK.get(b, -1) else b  # type: ignore[return-value]


def make_component(name: str, *, repo_id: str | None, type_: str, system: str | None, domain: str | None, owner: str | None, lifecycle: str | None, description: str, tags: list[str], aliases: list[str], evidence: list[dict[str, Any]], runtime: str = "", subcomponent_of: str = "", external: bool = False) -> dict[str, Any]:
    annotations: dict[str, Any] = {}
    if repo_id:
        annotations["source_repos"] = [repo_id]
        # `github.com/source-location` is the community convention (used by
        # multiple tools, not Backstage-specific) so we keep it. Anything
        # ServiceScout-specific goes under `servicescout/*`. The Backstage
        # exporter translates our keys to `backstage.io/*` when emitting YAML.
        github_url = f"https://github.com/{repo_id}"
        annotations.setdefault("github.com/source-location", github_url)
    if aliases:
        annotations["aliases"] = sorted({a for a in aliases if a})
    if external:
        annotations["external"] = "true"
    spec: dict[str, Any] = {"type": type_, "lifecycle": lifecycle or "unknown", "owner": owner or "unknown"}
    if system:
        spec["system"] = system
    if domain:
        spec["domain"] = domain
    if runtime:
        spec["runtime"] = runtime
    if subcomponent_of:
        spec["subcomponentOf"] = subcomponent_of
    return {
        "kind": "Component",
        "metadata": {
            "name": canonical_name(name),
            "description": description,
            "tags": sorted(set(tags)),
            "annotations": annotations,
        },
        "spec": spec,
        "evidence": evidence or [],
        "confidence": "high" if not external else "medium",
    }


def make_api(name: str, *, type_: str, exposed_by: str, description: str, evidence: list[dict[str, Any]], operations: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "kind": "API",
        "metadata": {
            "name": canonical_name(name),
            "description": description,
            "annotations": {"exposed_by": exposed_by, "operations": operations or []},
        },
        "spec": {"type": type_, "lifecycle": "unknown", "owner": "unknown"},
        "evidence": evidence or [],
        "confidence": "high",
    }


def make_provider(name: str, *, category: str, description: str, aliases: list[str], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    annotations: dict[str, Any] = {"external": "true"}
    if aliases:
        annotations["aliases"] = sorted({a for a in aliases if a})
    return {
        "kind": "Provider",
        "metadata": {
            "name": canonical_name(name),
            "description": description,
            "annotations": annotations,
        },
        "spec": {
            "type": "provider",
            "category": category or "other",
            "owner": "external",
            "lifecycle": "production",
        },
        "evidence": evidence or [],
        "confidence": "high",
    }


def make_resource(name: str, *, type_: str, technology: str, host: str, description: str, evidence: list[dict[str, Any]], used_by: str, messaging_pattern: str | None, subscribes_to: str | None, datasource_url: str | None, env_keys: list[str], tables: list[str], access: str, confidence: str) -> dict[str, Any]:
    annotations: dict[str, Any] = {}
    if env_keys:
        annotations["env_keys"] = sorted(set(env_keys))
    if tables:
        annotations["tables_or_collections"] = sorted(set(tables))
    if subscribes_to:
        annotations["subscribes_to"] = subscribes_to
    if datasource_url:
        annotations["datasource_url"] = datasource_url
    if messaging_pattern:
        annotations["messaging_pattern"] = messaging_pattern
    if used_by:
        annotations["used_by"] = used_by
    return {
        "kind": "Resource",
        "metadata": {
            "name": canonical_name(name),
            "description": description,
            "annotations": annotations,
        },
        "spec": {
            "type": type_,
            "technology": technology,
            "host": host,
            "access": access,
            "owner": "unknown",
        },
        "evidence": evidence or [],
        "confidence": confidence or "medium",
    }


def canonical_name(name: str) -> str:
    if not name:
        return "unknown"
    name = name.strip()
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name)
    name = re.sub(r"-+", "-", name).strip("-")
    return name or "unknown"


_CODEOWNERS_LOCATIONS = ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS")
_TEAM_PATTERN = re.compile(r"@([\w./-]+/[\w./-]+|[\w./-]+)")


def _owner_from_codeowners(repo_id: str) -> str:
    """Deterministic fallback: parse the first non-comment, non-blank line of
    CODEOWNERS and return the first @-mention. Reads the repo from disk via
    WORKSPACE_ROOT + the repo's short name.

    Returns empty string when no CODEOWNERS or no parsable owner.
    """
    workspace_root = os.environ.get("WORKSPACE_ROOT") or os.environ.get("CONTEXT_GRAPH_WORKSPACE")
    if not workspace_root or not repo_id:
        return ""
    repo_dir = Path(workspace_root) / short_repo_name(repo_id)
    if not repo_dir.exists():
        return ""
    for candidate in _CODEOWNERS_LOCATIONS:
        codeowners = repo_dir / candidate
        if not codeowners.exists():
            continue
        try:
            for line in codeowners.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                match = _TEAM_PATTERN.search(line)
                if match:
                    return match.group(1)
        except OSError:
            continue
    return ""


def add_repo(catalog: Catalog, repo_payload: dict[str, Any]) -> dict[str, Any]:
    repo_meta = repo_payload.get("repo", {})
    repo_id = repo_meta.get("id") or ""
    system = (repo_meta.get("system") or "").strip()
    domain = (repo_meta.get("domain") or "").strip()
    owner = (repo_meta.get("owner") or "").strip()
    lifecycle = (repo_meta.get("lifecycle") or "unknown").strip() or "unknown"
    tagline = (repo_meta.get("tagline") or "").strip()
    capability_sheet = (repo_meta.get("capability_sheet") or "").strip()
    summary = repo_meta.get("summary") or {}
    description = summary.get("purpose") or summary.get("notes") or ""
    evidence = summary.get("evidence") or []
    tags = [t for t in [summary.get("primary_language"), summary.get("runtime")] if t]
    domain_attributes = repo_payload.get("domain_attributes") or []
    glossary = repo_payload.get("glossary") or []

    providers_in_repo = repo_payload.get("providers") or []
    components = repo_payload.get("components") or []
    if not components:
        components = [{"name": short_repo_name(repo_id), "type": "service", "notes": description, "evidence": evidence, "tags": tags, "environments": []}]

    # Owner fallback: if the extractor didn't set owner, try CODEOWNERS from disk.
    if not owner:
        owner = _owner_from_codeowners(repo_id) or owner

    canonical_repo_name = canonical_name(short_repo_name(repo_id))
    main_component_name = canonical_repo_name
    created_component_names: set[str] = set()

    for index, component in enumerate(components):
        comp_name = canonical_name(component.get("name") or main_component_name)
        if index == 0:
            comp_name = main_component_name
        created_component_names.add(comp_name)
        # Per-component lifecycle wins over repo-level; fall back to repo's.
        comp_lifecycle = (component.get("lifecycle") or "").strip()
        effective_lifecycle = comp_lifecycle or lifecycle
        if effective_lifecycle == "":
            effective_lifecycle = "unknown"
        component_entity = make_component(
            comp_name,
            repo_id=repo_id,
            type_=component.get("type") or "service",
            system=system or None,
            domain=domain or None,
            owner=owner or None,
            lifecycle=effective_lifecycle,
            description=component.get("notes") or description,
            tags=tags + (component.get("tags") or []),
            aliases=[],
            evidence=component.get("evidence") or evidence,
            runtime=(component.get("runtime") or "").strip(),
            subcomponent_of=(component.get("subcomponent_of") or "").strip(),
        )
        environments = component.get("environments") or []
        if index == 0:
            annotations = component_entity["metadata"].setdefault("annotations", {})
            if tagline:
                annotations["tagline"] = tagline
            if capability_sheet:
                annotations["capability_sheet"] = capability_sheet
            spec = component_entity.setdefault("spec", {})
            if domain_attributes:
                spec["domain_attributes"] = domain_attributes
            if glossary:
                spec["glossary"] = glossary
        if environments:
            component_entity.setdefault("spec", {})["environments"] = sorted({str(e) for e in environments if e})
        catalog.upsert(component_entity)
        # If this component declares itself a subcomponent of another, emit
        # the well-known `subcomponentOf` relation so dependency walks pick
        # it up. The target may not exist yet — that's fine, it'll resolve
        # at deferred-resolution time.
        subcomp_of = (component.get("subcomponent_of") or "").strip()
        if subcomp_of:
            parent_name = canonical_name(subcomp_of)
            if parent_name != comp_name:
                catalog.add_relation({
                    "from": entity_ref(component_entity),
                    "type": "subcomponentOf",
                    "to": f"Component:{parent_name}",
                    "evidence": component.get("evidence") or [],
                    "properties": {"via": "subcomponent_of"},
                    "confidence": "high",
                })
        if system:
            system_entity = catalog.upsert({
                "kind": "System",
                "metadata": {"name": canonical_name(system), "annotations": {}},
                "spec": {"owner": owner or "unknown"},
                "evidence": [],
                "confidence": "medium",
            })
            catalog.add_relation({"from": entity_ref(component_entity), "type": "partOf", "to": entity_ref(system_entity), "evidence": [], "properties": {}, "confidence": "medium"})
            if domain:
                domain_entity = catalog.upsert({
                    "kind": "Domain",
                    "metadata": {"name": canonical_name(domain), "annotations": {}},
                    "spec": {"owner": owner or "unknown"},
                    "evidence": [],
                    "confidence": "medium",
                })
                catalog.add_relation({"from": entity_ref(system_entity), "type": "partOf", "to": entity_ref(domain_entity), "evidence": [], "properties": {}, "confidence": "medium"})
        if owner:
            group_entity = catalog.upsert({
                "kind": "Group",
                "metadata": {"name": canonical_name(owner), "annotations": {}},
                "spec": {"type": "team"},
                "evidence": [],
                "confidence": "medium",
            })
            catalog.add_relation({"from": entity_ref(component_entity), "type": "ownedBy", "to": entity_ref(group_entity), "evidence": [], "properties": {}, "confidence": "medium"})

    for provider in providers_in_repo:
        prov_name = provider.get("name") or ""
        if not prov_name:
            continue
        prov_entity = make_provider(
            prov_name,
            category=provider.get("category") or "other",
            description=provider.get("description") or "",
            aliases=list(provider.get("aliases") or []),
            evidence=provider.get("evidence") or [],
        )
        catalog.upsert(prov_entity)
        catalog.add_relation({
            "from": f"Component:{main_component_name}",
            "type": "dependsOn",
            "to": entity_ref(prov_entity),
            "evidence": provider.get("evidence") or [],
            "properties": {
                "via": "provider",
                "category": provider.get("category") or "other",
            },
            "confidence": "high",
        })

    apis = repo_payload.get("apis") or []
    for api in apis:
        api_name = canonical_name(api.get("name") or "unknown-api")
        api_entity = make_api(
            api_name,
            type_=api.get("type") or "rest",
            exposed_by=main_component_name,
            description=api.get("notes") or "",
            evidence=api.get("evidence") or [],
            operations=api.get("operations") or [],
        )
        catalog.upsert(api_entity)
        catalog.add_relation({
            "from": f"Component:{main_component_name}",
            "type": "providesApi",
            "to": entity_ref(api_entity),
            "evidence": api.get("evidence") or [],
            "properties": {},
            "confidence": "high",
        })

    resources = repo_payload.get("resources") or []
    for resource in resources:
        res_name = canonical_name(resource.get("name") or "unknown-resource")
        res_entity = make_resource(
            res_name,
            type_=resource.get("type") or "unknown",
            technology=resource.get("technology") or "",
            host=resource.get("host_or_instance") or "",
            description=resource.get("notes") or "",
            evidence=resource.get("evidence") or [],
            used_by=resource.get("used_by") or main_component_name,
            messaging_pattern=resource.get("messaging_pattern"),
            subscribes_to=resource.get("subscribes_to"),
            datasource_url=resource.get("datasource_url"),
            env_keys=resource.get("env_or_config_keys") or [],
            tables=resource.get("tables_or_collections") or [],
            access=resource.get("access") or "unknown",
            confidence=resource.get("confidence") or "medium",
        )
        catalog.upsert(res_entity)
        kind_map = {
            "read": "readsResource",
            "write": "writesResource",
            "read-write": "readsResource",
            "publish": "producesMessage",
            "consume": "consumesMessage",
            "publish-consume": "consumesMessage",
        }
        relation_type = kind_map.get(resource.get("access") or "", "dependsOn")
        catalog.add_relation({
            "from": f"Component:{main_component_name}",
            "type": relation_type,
            "to": entity_ref(res_entity),
            "evidence": resource.get("evidence") or [],
            "properties": {"access": resource.get("access") or "unknown"},
            "confidence": resource.get("confidence") or "medium",
        })

    dependencies = repo_payload.get("dependencies") or []
    deferred: list[dict[str, Any]] = []
    for dep in dependencies:
        raw_source = canonical_name(dep.get("source") or main_component_name)
        source = raw_source if raw_source in created_component_names else main_component_name
        target_label = dep.get("target") or ""
        if not target_label:
            continue
        aliases = list(dep.get("aliases") or []) + [target_label]
        deferred.append({
            "source": source,
            "target_label": target_label,
            "aliases": aliases,
            "kind": dep.get("kind") or "dependsOn",
            "target_kind": dep.get("target_kind") or "component",
            "protocol": dep.get("protocol") or "unknown",
            "operation_or_usage": dep.get("operation_or_usage") or "",
            "message_or_event_name": dep.get("message_or_event_name") or "",
            "env_keys": dep.get("env_or_config_keys") or [],
            "confidence": dep.get("confidence") or "medium",
            "evidence": dep.get("evidence") or [],
            "notes": dep.get("notes") or "",
        })

    return {"main_component": main_component_name, "deferred_dependencies": deferred}


def short_repo_name(repo_id: str) -> str:
    return repo_id.split("/", 1)[-1] if "/" in repo_id else repo_id


def add_seed_ecomm(catalog: Catalog, seed: dict[str, Any]) -> None:
    system_name = canonical_name(seed.get("platform") or "platform")
    system_entity = catalog.upsert({
        "kind": "System",
        "metadata": {"name": system_name, "annotations": {}},
        "spec": {"owner": "unknown"},
        "evidence": [],
        "confidence": "medium",
    })
    hub = seed.get("hub_repo")
    if hub:
        hub_name = canonical_name(short_repo_name(hub))
        hub_entity = catalog.upsert(make_component(
            hub_name,
            repo_id=hub,
            type_="service",
            system=system_name,
            domain=None,
            owner=None,
            lifecycle="production",
            description=f"Hub component from seed {system_name}",
            tags=["hub"],
            aliases=[],
            evidence=[],
        ))
        catalog.add_relation({"from": entity_ref(hub_entity), "type": "partOf", "to": entity_ref(system_entity), "evidence": [], "properties": {}, "confidence": "medium"})
    for frontend in seed.get("frontends") or []:
        repo = frontend.get("storefront_repo")
        if not repo:
            continue
        comp_name = canonical_name(short_repo_name(repo))
        comp_entity = catalog.upsert(make_component(
            comp_name,
            repo_id=repo,
            type_="website",
            system=system_name,
            domain=None,
            owner=None,
            lifecycle="production",
            description=f"{frontend.get('name')} storefront",
            tags=["storefront"],
            aliases=[],
            evidence=[],
        ))
        catalog.add_relation({"from": entity_ref(comp_entity), "type": "partOf", "to": entity_ref(system_entity), "evidence": [], "properties": {}, "confidence": "medium"})
        if hub:
            catalog.add_relation({
                "from": entity_ref(comp_entity),
                "type": "dependsOn",
                "to": f"Component:{canonical_name(short_repo_name(hub))}",
                "evidence": [],
                "properties": {"via": "platform-seed"},
                "confidence": "high",
            })
    for app in seed.get("native_apps") or []:
        repo = app.get("app_repo")
        if not repo:
            continue
        comp_name = canonical_name(short_repo_name(repo))
        comp_entity = catalog.upsert(make_component(
            comp_name,
            repo_id=repo,
            type_="mobile-app",
            system=system_name,
            domain=None,
            owner=None,
            lifecycle="production",
            description=f"{app.get('name')} native app",
            tags=["native-app"],
            aliases=[],
            evidence=[],
        ))
        catalog.add_relation({"from": entity_ref(comp_entity), "type": "partOf", "to": entity_ref(system_entity), "evidence": [], "properties": {}, "confidence": "medium"})
        if hub:
            catalog.add_relation({
                "from": entity_ref(comp_entity),
                "type": "dependsOn",
                "to": f"Component:{canonical_name(short_repo_name(hub))}",
                "evidence": [],
                "properties": {"via": "platform-seed"},
                "confidence": "high",
            })


def add_seed_service_map(catalog: Catalog, seed: dict[str, Any], cloned_repo_ids: set[str]) -> None:
    source_repo = seed.get("source_repo")
    if source_repo:
        source_name = canonical_name(short_repo_name(source_repo))
        catalog.upsert(make_component(
            source_name,
            repo_id=source_repo,
            type_="service",
            system=None,
            domain=None,
            owner=None,
            lifecycle="production",
            description=f"Seed source component for {source_repo}",
            tags=[],
            aliases=[],
            evidence=[],
        ))
    for entry in seed.get("resolved") or []:
        config_name = entry.get("config_name")
        repo = entry.get("repo")
        if not config_name or not repo:
            continue
        repo_short = short_repo_name(repo)
        comp_name = canonical_name(repo_short)
        comp_entity = catalog.upsert(make_component(
            comp_name,
            repo_id=repo,
            type_="service",
            system=None,
            domain=None,
            owner=None,
            lifecycle="production",
            description=f"Seed-resolved service ({config_name})",
            tags=[],
            aliases=[config_name],
            evidence=[],
            external=repo not in cloned_repo_ids,
        ))
        if source_repo:
            catalog.add_relation({
                "from": f"Component:{canonical_name(short_repo_name(source_repo))}",
                "type": "dependsOn",
                "to": entity_ref(comp_entity),
                "evidence": [{"path": "seeds/service-map.json", "line": 1, "snippet": f"config_name: {config_name}"}],
                "properties": {"via": "service-map-seed", "config_name": config_name},
                "confidence": entry.get("confidence") or "review",
            })


def resolve_deferred_dependencies(catalog: Catalog, deferred_by_repo: dict[str, list[dict[str, Any]]]) -> int:
    unresolved = 0
    for repo_id, deferred in deferred_by_repo.items():
        for dep in deferred:
            source_ref = f"Component:{dep['source']}"
            target_ref = catalog.resolve_alias(dep["target_label"])
            if target_ref is None:
                for alias in dep["aliases"]:
                    target_ref = catalog.resolve_alias(alias)
                    if target_ref:
                        break
            if target_ref is None:
                external_name = canonical_name(dep["target_label"])
                external = catalog.upsert(make_component(
                    external_name,
                    repo_id=None,
                    type_="service",
                    system=None,
                    domain=None,
                    owner=None,
                    lifecycle="unknown",
                    description=f"External or unresolved: {dep['target_label']}",
                    tags=["external"],
                    aliases=dep["aliases"],
                    evidence=dep["evidence"],
                    external=True,
                ))
                target_ref = entity_ref(external)
                unresolved += 1
            relation_type_map = {
                "dependsOn": "dependsOn",
                "consumesApi": "consumesApi",
                "producesMessage": "producesMessage",
                "consumesMessage": "consumesMessage",
                "readsResource": "readsResource",
                "writesResource": "writesResource",
            }
            catalog.add_relation({
                "from": source_ref,
                "type": relation_type_map.get(dep["kind"], "dependsOn"),
                "to": target_ref,
                "evidence": dep["evidence"],
                "properties": {
                    "protocol": dep["protocol"],
                    "operation_or_usage": dep["operation_or_usage"],
                    "message_or_event_name": dep["message_or_event_name"],
                    "env_keys": dep["env_keys"],
                    "aliases": sorted(set(dep["aliases"])),
                    "source_repo": repo_id,
                },
                "confidence": dep["confidence"],
            })
    return unresolved


def filter_evidence_required(catalog: Catalog) -> int:
    dropped = 0
    kept: list[dict[str, Any]] = []
    for relation in catalog.relations:
        if relation.get("confidence") == "high" and not relation.get("evidence"):
            dropped += 1
            continue
        kept.append(relation)
    catalog.relations = kept
    catalog._relation_keys = {relation_key(r) for r in kept}
    return dropped


def load_catalog_dir(catalog_dir: Path) -> Iterable[dict[str, Any]]:
    if not catalog_dir.exists():
        return []
    payloads = []
    for path in sorted(catalog_dir.glob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"WARN: could not parse {path}: {exc}", file=sys.stderr)
            continue
        if isinstance(payload, dict) and "components" in payload:
            payloads.append(payload)
    return payloads


def load_seeds(seeds_dir: Path) -> list[dict[str, Any]]:
    if not seeds_dir.exists():
        return []
    out = []
    for path in sorted(seeds_dir.glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def apply_triage_decisions(catalog: dict[str, Any], decisions: list[dict[str, Any]]) -> dict[str, int]:
    """Apply the newest-per-entity decision from data/triage_decisions.jsonl.

    Deterministic, idempotent. Decision actions:

    - `mark_external`  → flag the entity as external + set category.
    - `link`           → attach the entity to a real GitHub repo.
    - `merge`          → fold the entity into another, re-pointing edges + folding
                         aliases/evidence/tags. Uses reconcile.apply_merges.
    - `skip`           → no-op; hides the row from /triage.

    Manual KG corrections (event-sourced, applied on top of LLM extraction):

    - `set_tagline`     → override `annotations.tagline`.
    - `set_owner`       → override `spec.owner` and emit ownedBy relation.
    - `set_lifecycle`   → override `spec.lifecycle`.
    - `add_alias`       → append to `annotations.aliases`.
    - `remove_alias`    → remove from `annotations.aliases`.
    - `add_dependency`  → emit a new dependsOn / consumesApi / etc. edge.
    - `remove_dependency` → drop a specific edge.
    """
    from reconcile import apply_merges  # local import to avoid a circular dep at module load.

    entities_by_ref = {f"{e['kind']}:{e['metadata']['name']}": e for e in catalog.get("entities") or []}
    latest: dict[str, dict[str, Any]] = {}
    for record in decisions:
        ref = record.get("entity")
        if ref:
            latest[ref] = record

    counts = {
        "mark_external": 0, "link": 0, "merge": 0, "skip": 0,
        "set_tagline": 0, "set_owner": 0, "set_lifecycle": 0,
        "add_alias": 0, "remove_alias": 0,
        "add_dependency": 0, "remove_dependency": 0,
        "missing": 0, "edges_rerouted": 0, "edges_dropped": 0,
    }
    merge_proposals: list[dict[str, Any]] = []
    added_relations: list[dict[str, Any]] = []
    removed_relations: list[tuple[str, str, str]] = []  # (from, type, to)

    for ref, decision in latest.items():
        entity = entities_by_ref.get(ref)
        action = decision.get("action")
        if entity is None and action not in {"merge", "add_dependency", "remove_dependency"}:
            counts["missing"] += 1
            print(f"INFO: triage decision targets missing entity {ref} (action={action})", file=sys.stderr)
            continue
        if entity is not None:
            annotations = entity.setdefault("metadata", {}).setdefault("annotations", {})
            spec = entity.setdefault("spec", {})
        else:
            annotations = {}
            spec = {}

        if action == "mark_external":
            annotations["external"] = "true"
            annotations["category"] = decision.get("category") or "other"
            counts["mark_external"] += 1
        elif action == "link":
            repo = decision.get("repo") or ""
            if repo:
                annotations["source_repos"] = [repo]
                annotations.setdefault("github.com/source-location", f"https://github.com/{repo}")
            annotations.pop("external", None)
            annotations["linked_via_triage"] = "true"
            counts["link"] += 1
        elif action == "merge":
            target = (decision.get("into") or "").strip()
            if not target or target == ref:
                print(f"WARN: merge {ref} → {target!r} skipped (invalid target)", file=sys.stderr)
                continue
            if ref not in entities_by_ref:
                counts["merge"] += 1
                continue  # already merged in a previous run
            if target not in entities_by_ref:
                print(f"WARN: merge {ref} → {target} skipped (target not in catalog)", file=sys.stderr)
                continue
            merge_proposals.append({
                "from": ref,
                "into": target,
                "matching_keys": ["triage-decision"],
                "reason": f"triage: {(decision.get('reason') or '')[:200]}",
            })
            counts["merge"] += 1
        elif action == "skip":
            counts["skip"] += 1
        elif action == "set_tagline":
            new_value = (decision.get("value") or "").strip()
            if new_value:
                annotations["tagline"] = new_value
                annotations["tagline_source"] = "triage"
                counts["set_tagline"] += 1
        elif action == "set_owner":
            new_value = (decision.get("value") or "").strip()
            if new_value:
                spec["owner"] = new_value
                spec["owner_source"] = "triage"
                counts["set_owner"] += 1
        elif action == "set_lifecycle":
            new_value = (decision.get("value") or "").strip()
            if new_value in {"production", "experimental", "deprecated"}:
                spec["lifecycle"] = new_value
                counts["set_lifecycle"] += 1
            else:
                print(f"WARN: set_lifecycle {new_value!r} for {ref} not in production|experimental|deprecated", file=sys.stderr)
        elif action == "add_alias":
            alias = (decision.get("value") or "").strip()
            if alias:
                existing = list(annotations.get("aliases") or [])
                if alias not in existing:
                    existing.append(alias)
                    annotations["aliases"] = sorted(set(existing))
                    counts["add_alias"] += 1
        elif action == "remove_alias":
            alias = (decision.get("value") or "").strip()
            if alias:
                existing = list(annotations.get("aliases") or [])
                if alias in existing:
                    existing.remove(alias)
                    annotations["aliases"] = existing
                    counts["remove_alias"] += 1
        elif action == "add_dependency":
            target = (decision.get("target") or "").strip()
            rel_type = (decision.get("relation_type") or "dependsOn").strip()
            reason = decision.get("reason") or "triage-added"
            if not target:
                print(f"WARN: add_dependency on {ref} skipped (no target)", file=sys.stderr)
                continue
            added_relations.append({
                "from": ref,
                "type": rel_type,
                "to": target,
                "evidence": [],
                "confidence": "review",
                "properties": {"source": "triage", "reason": reason[:160]},
            })
            counts["add_dependency"] += 1
        elif action == "remove_dependency":
            target = (decision.get("target") or "").strip()
            rel_type = (decision.get("relation_type") or "").strip()
            if not target:
                print(f"WARN: remove_dependency on {ref} skipped (no target)", file=sys.stderr)
                continue
            removed_relations.append((ref, rel_type, target))
            counts["remove_dependency"] += 1
        else:
            print(f"WARN: unknown triage action {action!r} for {ref}", file=sys.stderr)

    # Apply graph mutations after the per-entity loop.
    if merge_proposals:
        result = apply_merges(catalog, merge_proposals)
        counts["edges_rerouted"] = int(result.get("edges_rerouted") or 0)
        counts["edges_dropped"] = int(result.get("edges_dropped") or 0)

    if removed_relations:
        kept = []
        for rel in catalog.get("relations") or []:
            drop = False
            for (f, rtype, t) in removed_relations:
                if rel["from"] == f and rel["to"] == t and (not rtype or rel["type"] == rtype):
                    drop = True
                    break
            if not drop:
                kept.append(rel)
        catalog["relations"] = kept

    if added_relations:
        existing_keys = {(r["from"], r["type"], r["to"]) for r in catalog.get("relations") or []}
        for new_rel in added_relations:
            k = (new_rel["from"], new_rel["type"], new_rel["to"])
            if k not in existing_keys:
                catalog.setdefault("relations", []).append(new_rel)
                existing_keys.add(k)

    return counts


def write_catalog(output_path: Path, payload: dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    os.replace(tmp, output_path)


def build(
    root: Path,
    catalog_dir: Path,
    seeds_dir: Path,
    output_path: Path,
    workspace_config: dict[str, Any],
) -> dict[str, Any]:
    catalog = Catalog()
    cloned_repos = find_repos(root, workspace_config.get("orgs") or [], workspace_config.get("excluded_repos") or [])
    cloned_ids = {r["id"] for r in cloned_repos}

    deferred_by_repo: dict[str, list[dict[str, Any]]] = {}
    repo_count = 0
    for payload in load_catalog_dir(catalog_dir):
        result = add_repo(catalog, payload)
        deferred_by_repo[payload.get("repo", {}).get("id") or "unknown"] = result["deferred_dependencies"]
        repo_count += 1

    for seed in load_seeds(seeds_dir):
        if "frontends" in seed or "native_apps" in seed:
            add_seed_ecomm(catalog, seed)
        elif "resolved" in seed:
            add_seed_service_map(catalog, seed, cloned_ids)

    unresolved = resolve_deferred_dependencies(catalog, deferred_by_repo)
    evidence_dropped = filter_evidence_required(catalog)

    payload = catalog.to_json()

    # Preserve embeddings from the previous catalog so a rebuild doesn't force
    # a re-embed cycle. embed_catalog.py is still the source of truth — it
    # refreshes vectors when the underlying entity content changes.
    if output_path.exists():
        try:
            prior = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prior = None
        if isinstance(prior, dict):
            prior_by_ref = {
                f"{e.get('kind','')}:{e.get('metadata',{}).get('name','')}": e
                for e in (prior.get("entities") or [])
            }
            kept = 0
            for entity in payload.get("entities") or []:
                ref = f"{entity.get('kind','')}:{entity.get('metadata',{}).get('name','')}"
                old = prior_by_ref.get(ref)
                if old and old.get("embedding") and not entity.get("embedding"):
                    entity["embedding"] = old["embedding"]
                    kept += 1
            if kept:
                payload.setdefault("summary", {})["embeddings_preserved"] = kept

    payload["summary"]["repos_indexed"] = repo_count
    payload["summary"]["unresolved_external_components"] = unresolved
    payload["summary"]["dropped_for_missing_evidence"] = evidence_dropped
    payload["summary"]["cloned_repos_count"] = len(cloned_ids)

    decisions_path = output_path.parent / "triage_decisions.jsonl"
    if decisions_path.exists():
        decisions: list[dict[str, Any]] = []
        for line in decisions_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                decisions.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if decisions:
            triage_counts = apply_triage_decisions(payload, decisions)
            payload["summary"]["triage_applied"] = triage_counts

    write_catalog(output_path, payload)
    return payload["summary"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--catalog-dir", type=Path, default=DEFAULT_CATALOG_DIR)
    parser.add_argument("--seeds-dir", type=Path, default=DEFAULT_SEEDS_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workspace", type=Path, default=HERE / "workspace.json")
    args = parser.parse_args()

    workspace = load_workspace_config(args.workspace)
    summary = build(args.root.resolve(), args.catalog_dir, args.seeds_dir, args.output, workspace)
    print(json.dumps({"output": str(args.output), "summary": summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
