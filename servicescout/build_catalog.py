"""Merge per-repo catalog extractions into a single Backstage-aligned catalog.

Inputs:
  data/catalog/*.json  (one per repo, produced by extractor.py)
  seeds/*.json         (optional, additive architecture facts)

Output:
  data/catalog.json

The catalog uses Backstage's node kinds: Component, API, Resource, System,
Domain, Group. Relations: ownedBy, partOf, dependsOn, providesApi, consumesApi,
communicatesWith, hasPart. Every node and edge carries provenance (source repo,
evidence, run id) and a confidence label.

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
import copy
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

from servicescout.repo_discovery import find_repos, load_workspace_config
from servicescout.static_extractors.mini import runner as mini_runner


HERE = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG_DIR = HERE / "data" / "catalog"
DEFAULT_OUTPUT = HERE / "data" / "catalog.json"
DEFAULT_SEEDS_DIR = HERE / "seeds"


SUFFIXES_TO_STRIP = ["serviceapi", "apiservice", "service", "api", "backend", "frontend", "client", "gateway"]
COMMUNICATION_RELATION = "communicatesWith"
BROKER_TECHNOLOGIES = {
    "rabbitmq": "RabbitMQ",
    "kafka": "Kafka",
    "activemq": "ActiveMQ",
    "nats": "NATS",
    "redisstreams": "Redis Streams",
    "kinesis": "Kinesis",
    "pubsub": "Pub/Sub",
    "servicebus": "Service Bus",
}
GENERIC_BROKER_ENDPOINTS = set(BROKER_TECHNOLOGIES) | {
    "active-mq",
    "active mq",
    "pub-sub",
    "pub/sub",
    "google pubsub",
    "google pub/sub",
    "message-broker",
    "message broker",
    "broker",
    "queue",
    "topic",
}
GENERIC_BROKER_ENDPOINT_KEYS = {
    re.sub(r"[^a-z0-9]+", "", value.lower())
    for value in GENERIC_BROKER_ENDPOINTS
}

# Deterministic kind order for resolve_alias when no explicit kinds are given.
# Services (Components/APIs) are preferred targets over Resources/Providers.
_ALIAS_RESOLUTION_KIND_ORDER = ["Component", "API", "Provider", "Resource", "System", "Domain", "Group"]


def canonical_key(name: str) -> str:
    if not name:
        return ""
    key = re.sub(r"[^a-z0-9]+", "", name.lower())
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


def normalize_domain_label(label: str) -> str:
    """Collapse obvious spelling/punctuation drift in repo-level domains.

    Domain ownership is organization-specific, so this deliberately stays
    conservative: it fixes lexical variants of the same parent domain and
    leaves unrelated labels alone for an org-level reconciliation pass.
    """
    raw = (label or "").strip()
    if not raw:
        return ""
    lowered = re.sub(r"[_/]+", " ", raw.lower())
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered).strip()
    if not lowered:
        return ""
    compact = lowered.replace(" ", "")
    if compact == "ecommerce" or lowered.startswith("e commerce ") or lowered.startswith("ecommerce "):
        return "e-commerce"
    return canonical_name(lowered.replace(" ", "-"))


class Catalog:
    def __init__(self) -> None:
        self.entities: dict[str, dict[str, Any]] = {}
        self.relations: list[dict[str, Any]] = []
        self._relation_keys: set[str] = set()
        # kind -> {canonical_key -> (priority, ref)}. Priority breaks ties so a
        # canonical key resolves deterministically regardless of load order:
        #   3 = a real (non-external) entity's own name   (strongest claim)
        #   2 = an external/placeholder entity's own name
        #   1 = an alias / endpoint key
        # A higher-priority claim reclaims a key from a lower one; equal-priority
        # collisions keep the first (sorted-filename load order → deterministic).
        self._alias_index: dict[str, dict[str, tuple[int, str]]] = {}

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

    def _claim_key(self, kind: str, key: str, ref: str, *, priority: int) -> None:
        if not key:
            return
        index = self._alias_index.setdefault(kind, {})
        existing = index.get(key)
        # Overwrite only on a strictly higher-priority claim; equal/lower keeps
        # the incumbent so the result is independent of insertion order.
        if existing is None or priority > existing[0]:
            index[key] = (priority, ref)

    def _index_aliases(self, entity: dict[str, Any]) -> None:
        kind = entity["kind"]
        name = entity["metadata"]["name"]
        ref = entity_ref(entity)
        annotations = entity["metadata"].setdefault("annotations", {})
        is_external = str(annotations.get("external") or "").lower() == "true"
        # A real entity's own name outranks a placeholder's name, which outranks
        # any alias — so the real `payments-api` reclaims the `payment` key even
        # if some component's alias grabbed it first.
        self._claim_key(kind, canonical_key(name), ref, priority=2 if is_external else 3)
        aliases = annotations.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [a.strip() for a in aliases.split(",") if a.strip()]
        if kind == "Resource":
            aliases = [
                *aliases,
                *list(annotations.get("env_keys") or []),
                annotations.get("datasource_url") or "",
                (entity.get("spec") or {}).get("host") or "",
            ]
        for alias in aliases:
            key = canonical_key(alias) or host_to_key(alias)
            self._claim_key(kind, key, ref, priority=1)

    def add_relation(self, relation: dict[str, Any]) -> None:
        relation = copy.deepcopy(relation)
        key = relation_key(relation)
        if key in self._relation_keys:
            existing = next(r for r in self.relations if relation_key(r) == key)
            merge_relation(existing, relation)
            return
        self._relation_keys.add(key)
        self.relations.append(relation)

    def resolve_alias(self, label: str, kinds: Iterable[str] | None = None) -> str | None:
        key = canonical_key(label) or host_to_key(label)
        if not key:
            return None
        if kinds is not None:
            search_kinds: list[str] = list(kinds)
        else:
            # Deterministic order so resolution doesn't depend on which repo
            # happened to be processed first (dict-insertion order).
            present = self._alias_index.keys()
            search_kinds = [k for k in _ALIAS_RESOLUTION_KIND_ORDER if k in present]
            search_kinds += sorted(k for k in present if k not in _ALIAS_RESOLUTION_KIND_ORDER)
        for kind in search_kinds:
            entry = self._alias_index.get(kind, {}).get(key)
            if entry:
                return entry[1]
        return None

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
        if key in {"aliases", "endpoints", "transports", "mechanisms", "via_edges"}:
            existing = set(props_into.get(key) or [])
            new = value if isinstance(value, list) else [value]
            props_into[key] = sorted(existing | set(new))
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
        github_url = github_source_location(repo_id)
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
        "evidence": list(evidence or []),
        "confidence": "high" if not external else "medium",
    }


def make_api(name: str, *, type_: str, exposed_by: str, description: str, evidence: list[dict[str, Any]], operations: list[dict[str, Any]], repo_id: str | None = None, aliases: list[str] | None = None) -> dict[str, Any]:
    annotations: dict[str, Any] = {"exposed_by": exposed_by, "operations": operations or []}
    if repo_id:
        annotations["source_repos"] = [repo_id]
    if aliases:
        annotations["aliases"] = sorted({a for a in aliases if a})
    return {
        "kind": "API",
        "metadata": {
            "name": canonical_name(name),
            "description": description,
            "annotations": annotations,
        },
        "spec": {"type": type_, "lifecycle": "unknown", "owner": "unknown"},
        "evidence": list(evidence or []),
        "confidence": "high",
    }


def make_provider(name: str, *, category: str, description: str, aliases: list[str], evidence: list[dict[str, Any]], repo_id: str | None = None) -> dict[str, Any]:
    annotations: dict[str, Any] = {"external": "true"}
    if repo_id:
        annotations["source_repos"] = [repo_id]
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
        "evidence": list(evidence or []),
        "confidence": "high",
    }


def make_resource(name: str, *, type_: str, technology: str, host: str, description: str, evidence: list[dict[str, Any]], used_by: str, messaging_pattern: str | None, subscribes_to: str | None, datasource_url: str | None, env_keys: list[str], tables: list[str], access: str, confidence: str, repo_id: str | None = None, aliases: list[str] | None = None) -> dict[str, Any]:
    annotations: dict[str, Any] = {}
    if repo_id:
        annotations["source_repos"] = [repo_id]
    if aliases:
        annotations["aliases"] = sorted({a for a in aliases if a})
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
        "evidence": list(evidence or []),
        "confidence": confidence or "medium",
    }


def canonical_name(name: str) -> str:
    if not name:
        return "unknown"
    name = name.strip()
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name)
    name = re.sub(r"-+", "-", name).strip("-")
    return name or "unknown"


def _host_label(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    match = re.search(r"\$\{[^:}]+:([^}]+)\}", value)
    if match:
        value = match.group(1)
    value = re.sub(r"^[a-z][a-z0-9+.-]*://", "", value, flags=re.IGNORECASE)
    value = value.split("/", 1)[0].split(":", 1)[0]
    return canonical_name(value)


def preferred_resource_name(resource: dict[str, Any], existing_components: set[str] | None = None) -> str:
    raw_name = resource.get("name") or "unknown-resource"
    current = canonical_name(raw_name)
    host_name = _host_label(resource.get("host_or_instance") or resource.get("datasource_url") or "")
    if not host_name:
        return current
    if existing_components and host_name in existing_components:
        return current
    key = canonical_key(current)
    generic_markers = ("database", "datastore", "datastore", "data", "store", "mongodb", "mysql", "postgres", "redis")
    if any(marker in key for marker in generic_markers):
        return host_name
    return current


def _api_match_key(name: str) -> str:
    tokens = [
        token for token in re.split(r"[^a-z0-9]+", (name or "").lower())
        if token and token not in {"api", "apis", "rest", "http", "https", "openapi", "swagger", "resource", "resources", "service", "services", "contract", "and"}
    ]
    return "".join(tokens)


def _merge_operations(target: dict[str, Any], operations: Iterable[dict[str, Any]]) -> int:
    existing = target.setdefault("operations", [])
    seen = {
        (str(op.get("method") or ""), str(op.get("path") or "") or str(op.get("name") or ""))
        for op in existing
        if isinstance(op, dict)
    }
    added = 0
    for op in operations:
        if not isinstance(op, dict):
            continue
        sig = (str(op.get("method") or ""), str(op.get("path") or "") or str(op.get("name") or ""))
        if sig in seen:
            continue
        existing.append(op)
        seen.add(sig)
        added += 1
    return added


def merge_static_api_operations(repo_payload: dict[str, Any], repo_root: Path) -> dict[str, int]:
    """Enrich LLM APIs with operations from exact source API specs.

    This deliberately only merges into an existing API with a strong name match.
    It does not add new APIs, because unmatched spec titles are often broad
    umbrella specs that would duplicate contract-level APIs.
    """
    if not repo_root.exists():
        return {"facts": 0, "merged_apis": 0, "operations_added": 0}
    try:
        facts = mini_runner.extract_all(repo_root)
    except Exception:  # noqa: BLE001
        return {"facts": 0, "merged_apis": 0, "operations_added": 0}
    apis = repo_payload.get("apis") or []
    api_by_key: dict[str, list[dict[str, Any]]] = {}
    for api in apis:
        if isinstance(api, dict):
            api_by_key.setdefault(_api_match_key(str(api.get("name") or "")), []).append(api)

    merged_apis = 0
    operations_added = 0
    for fact in facts:
        if fact.category != "apis":
            continue
        body = fact.body
        candidates = api_by_key.get(_api_match_key(str(body.get("name") or ""))) or []
        if len(candidates) != 1:
            continue
        target = candidates[0]
        added = _merge_operations(target, body.get("operations") or [])
        if added:
            operations_added += added
            merged_apis += 1
        evidence = target.setdefault("evidence", [])
        _merge_evidence(evidence, body.get("evidence") or [])
        emitters = target.setdefault("_emitted_by", [])
        if fact.rule not in emitters:
            emitters.append(fact.rule)
    return {"facts": len([f for f in facts if f.category == "apis"]), "merged_apis": merged_apis, "operations_added": operations_added}


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
    raw_domain = (repo_meta.get("domain") or "").strip()
    domain = normalize_domain_label(raw_domain)
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
            aliases=component.get("aliases") or [],
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
            if raw_domain and raw_domain != domain:
                annotations["original_domain_label"] = raw_domain
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
                    "metadata": {
                        "name": canonical_name(domain),
                        "annotations": {"aliases": [raw_domain]} if raw_domain and raw_domain != domain else {},
                    },
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
            repo_id=repo_id,
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
                "source_repo": repo_id,
                "target_kind": "provider",
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
            repo_id=repo_id,
            aliases=list(api.get("aliases") or []),
        )
        catalog.upsert(api_entity)
        catalog.add_relation({
            "from": f"Component:{main_component_name}",
            "type": "providesApi",
            "to": entity_ref(api_entity),
            "evidence": api.get("evidence") or [],
            "properties": {"source_repo": repo_id, "target_kind": "api"},
            "confidence": "high",
        })

    resources = repo_payload.get("resources") or []
    for resource in resources:
        res_name = preferred_resource_name(resource, created_component_names)
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
            repo_id=repo_id,
            aliases=list(resource.get("aliases") or []),
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
            "properties": {
                "access": resource.get("access") or "unknown",
                "source_repo": repo_id,
                "target_kind": "resource",
            },
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
    return repo_id.rstrip("/").split("/")[-1] if "/" in repo_id else repo_id


def github_source_location(repo_id: str) -> str:
    parts = [part for part in repo_id.strip("/").split("/") if part]
    if len(parts) < 2:
        return ""
    return f"https://github.com/{parts[0]}/{parts[1]}"


def add_seed_platform(catalog: Catalog, seed: dict[str, Any]) -> None:
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


def _preferred_target_kinds(dep: dict[str, Any]) -> list[str]:
    if _is_dynamic_api_dependency(dep):
        return ["API", "Component"]
    explicit = canonical_key(str(dep.get("target_kind") or ""))
    if explicit == "resource":
        return ["Resource"]
    if explicit == "provider":
        return ["Provider"]
    if explicit == "api":
        return ["API"]
    if explicit == "component":
        return ["Component"]
    if explicit == "external":
        if dep.get("kind") == "consumesApi":
            return ["Component", "API", "Provider"]
        return ["Provider"]
    relation_kind = dep.get("kind") or ""
    if relation_kind in {"producesMessage", "consumesMessage", "readsResource", "writesResource"}:
        return ["Resource"]
    if relation_kind == "consumesApi":
        return ["Component", "API"]
    return ["Component", "Provider", "Resource", "API"]


def _dependency_relation_confidence(dep: dict[str, Any], target: dict[str, Any] | None) -> str:
    confidence = dep.get("confidence") or "medium"
    if target and target.get("confidence"):
        confidence = _confidence_floor(confidence, target.get("confidence"))
    if _is_dynamic_api_dependency(dep):
        confidence = _confidence_floor(confidence, "review")
    explicit = canonical_key(str(dep.get("target_kind") or ""))
    protocol = canonical_key(str(dep.get("protocol") or ""))
    if explicit == "external" and dep.get("kind") == "dependsOn" and protocol in {"", "unknown"}:
        confidence = _confidence_floor(confidence, "review")
    return confidence


def _is_dynamic_api_dependency(dep: dict[str, Any]) -> bool:
    if dep.get("kind") != "consumesApi":
        return False
    text = " ".join(
        str(dep.get(key) or "")
        for key in ("notes", "operation_or_usage", "target_label")
    ).lower()
    aliases = " ".join(str(alias) for alias in dep.get("aliases") or []).lower()
    dynamic_markers = (
        "caller-supplied",
        "request body",
        "runtime-supplied",
        "runtime supplied",
        "user-supplied",
        "user supplied",
    )
    return any(marker in text for marker in dynamic_markers) or any(alias.startswith("item.") for alias in aliases.split())


def _make_unresolved_target(dep: dict[str, Any], repo_id: str) -> dict[str, Any]:
    name = canonical_name(dep["target_label"])
    aliases = dep.get("aliases") or []
    evidence = dep.get("evidence") or []
    target_kind = (_preferred_target_kinds(dep) or ["Component"])[0]
    if target_kind == "Resource":
        resource_type = "queue" if dep.get("kind") in {"producesMessage", "consumesMessage"} else "unknown"
        return make_resource(
            name,
            type_=resource_type,
            technology=dep.get("protocol") if dep.get("protocol") != "unknown" else "",
            host="",
            description=f"Unresolved resource dependency: {dep['target_label']}",
            evidence=evidence,
            used_by=dep.get("source") or "",
            messaging_pattern=None,
            subscribes_to=None,
            datasource_url=None,
            env_keys=dep.get("env_keys") or [],
            tables=[],
            access="unknown",
            confidence="review",
            repo_id=repo_id,
            aliases=aliases,
        )
    if target_kind == "Provider":
        return make_provider(
            name,
            category="other",
            description=f"Unresolved provider dependency: {dep['target_label']}",
            aliases=aliases,
            evidence=evidence,
            repo_id=repo_id,
        ) | {"confidence": "review"}
    if target_kind == "API":
        api_entity = make_api(
            name,
            type_="dynamic-http" if _is_dynamic_api_dependency(dep) else dep.get("protocol") or "unknown",
            exposed_by="runtime-supplied" if _is_dynamic_api_dependency(dep) else "unknown",
            description=f"Unresolved API dependency: {dep['target_label']}",
            evidence=evidence,
            operations=[],
            repo_id=repo_id,
            aliases=aliases,
        ) | {"confidence": "review"}
        if _is_dynamic_api_dependency(dep):
            annotations = api_entity["metadata"].setdefault("annotations", {})
            annotations["dynamic_dependency"] = "true"
        return api_entity
    return make_component(
        name,
        repo_id=None,
        type_="service",
        system=None,
        domain=None,
        owner=None,
        lifecycle="unknown",
        description=f"External or unresolved: {dep['target_label']}",
        tags=["external"],
        aliases=aliases,
        evidence=evidence,
        external=True,
    )


def resolve_deferred_dependencies(catalog: Catalog, deferred_by_repo: dict[str, list[dict[str, Any]]]) -> int:
    unresolved = 0
    for repo_id, deferred in deferred_by_repo.items():
        for dep in deferred:
            source_ref = f"Component:{dep['source']}"
            preferred_kinds = _preferred_target_kinds(dep)
            target_ref = catalog.resolve_alias(dep["target_label"], preferred_kinds)
            if target_ref is None:
                for alias in dep["aliases"]:
                    target_ref = catalog.resolve_alias(alias, preferred_kinds)
                    if target_ref:
                        break
            if target_ref is None:
                external = catalog.upsert(_make_unresolved_target(dep, repo_id))
                target_ref = entity_ref(external)
                unresolved += 1
            target_entity = catalog.entities.get(target_ref)
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
                    "target_kind": _ref_kind(target_ref).lower(),
                },
                "confidence": _dependency_relation_confidence(dep, target_entity),
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


def filter_payload_evidence_required(payload: dict[str, Any]) -> int:
    """Payload-level twin of filter_evidence_required, re-applied after the
    derivation passes (normalize_api_granularity etc.) that can introduce
    high-confidence edges lacking evidence — so the invariant holds on the
    final written catalog, not just on the pre-derivation Catalog."""
    relations = payload.get("relations") or []
    kept = [r for r in relations if not (r.get("confidence") == "high" and not r.get("evidence"))]
    dropped = len(relations) - len(kept)
    payload["relations"] = kept
    return dropped


def _ref_kind(ref: str) -> str:
    return ref.split(":", 1)[0] if ":" in ref else ""


def _ref_name(ref: str) -> str:
    return ref.split(":", 1)[1] if ":" in ref else ref


def _entity_by_ref(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        f"{entity.get('kind')}:{entity.get('metadata', {}).get('name')}": entity
        for entity in (payload.get("entities") or [])
        if entity.get("kind") and entity.get("metadata", {}).get("name")
    }


def _broker_name_for_resource(entity: dict[str, Any]) -> str:
    spec = entity.get("spec") or {}
    annotations = entity.get("metadata", {}).get("annotations", {}) or {}
    candidates = [
        str(spec.get("technology") or ""),
        str(spec.get("type") or ""),
        str(annotations.get("messaging_pattern") or ""),
        str(annotations.get("datasource_url") or ""),
    ]
    for candidate in candidates:
        key = canonical_key(candidate)
        for marker, name in BROKER_TECHNOLOGIES.items():
            if marker in key:
                return name
    return ""


def _merge_evidence(into: list[dict[str, Any]], evidence: Iterable[dict[str, Any]]) -> None:
    seen = {(ev.get("path"), ev.get("line"), ev.get("snippet")) for ev in into}
    for ev in evidence:
        sig = (ev.get("path"), ev.get("line"), ev.get("snippet"))
        if sig not in seen:
            into.append(ev)
            seen.add(sig)


def _upsert_payload_entity(payload: dict[str, Any], entity: dict[str, Any]) -> dict[str, Any]:
    entities = payload.setdefault("entities", [])
    ref = entity_ref(entity)
    for existing in entities:
        if entity_ref(existing) == ref:
            merge_entity(existing, entity)
            return existing
    entities.append(entity)
    return entity


def _upsert_payload_relation(payload: dict[str, Any], relation: dict[str, Any]) -> bool:
    relations = payload.setdefault("relations", [])
    for existing in relations:
        if relation_key(existing) == relation_key(relation):
            merge_relation(existing, relation)
            return False
    relations.append(relation)
    return True


def derive_broker_resources(payload: dict[str, Any]) -> int:
    """Add broker-instance Resources as a facet over queue/topic resources.

    Queue/topic resources remain the precise endpoints. The broker resource lets
    blast-radius questions such as "RabbitMQ is down" resolve without turning
    broker products into Providers or Components.
    """
    entities = _entity_by_ref(payload)
    added = 0
    broker_refs_by_resource: dict[str, str] = {}

    for resource_ref, entity in list(entities.items()):
        if _ref_kind(resource_ref) != "Resource":
            continue
        broker_name = _broker_name_for_resource(entity)
        if not broker_name:
            continue
        broker_ref = f"Resource:{canonical_name(broker_name.lower())}"
        annotations = entity.get("metadata", {}).get("annotations", {}) or {}
        source_repos = annotations.get("source_repos") or []
        if isinstance(source_repos, str):
            source_repos = [source_repos]
        broker_entity = {
            "kind": "Resource",
            "metadata": {
                "name": canonical_name(broker_name.lower()),
                "description": f"{broker_name} broker instance inferred from queue/topic resources.",
                "annotations": {
                    "source_repos": sorted({str(repo) for repo in source_repos if repo}),
                    "aliases": [
                        broker_name,
                        broker_name.lower(),
                        canonical_name(broker_name),
                        f"{broker_name} broker",
                        "async",
                        "asynchronous",
                        "message broker",
                        "queue broker",
                    ],
                    "inferred_broker": "true",
                },
            },
            "spec": {
                "type": "broker",
                "technology": broker_name,
                "host": (entity.get("spec") or {}).get("host") or broker_name.lower(),
                "access": "publish-consume",
                "owner": "unknown",
            },
            "evidence": list(entity.get("evidence") or []),
            "confidence": "medium",
        }
        _upsert_payload_entity(payload, broker_entity)
        broker_refs_by_resource[resource_ref] = broker_ref
        added += int(broker_ref not in entities)
        entities[broker_ref] = broker_entity

        _upsert_payload_relation(payload, {
            "from": resource_ref,
            "type": "dependsOn",
            "to": broker_ref,
            "evidence": entity.get("evidence") or [],
            "properties": {
                "via": "broker-technology",
                "source_repo": source_repos[0] if source_repos else "",
                "source_repos": sorted({str(repo) for repo in source_repos if repo}),
                "target_kind": "resource",
            },
            "confidence": "medium",
        })

    if not broker_refs_by_resource:
        return added

    for relation in list(payload.get("relations") or []):
        rtype = relation.get("type")
        target = relation.get("to")
        if rtype not in {"producesMessage", "consumesMessage", "dependsOn"}:
            continue
        broker_ref = broker_refs_by_resource.get(target)
        if not broker_ref:
            continue
        source = relation.get("from")
        if _ref_kind(source) != "Component":
            continue
        props = dict(relation.get("properties") or {})
        props.update({
            "via": "broker-resource",
            "via_resource": target,
            "target_kind": "resource",
            "protocol": props.get("protocol") or _broker_name_for_resource(entities.get(target, {})),
        })
        added += int(_upsert_payload_relation(payload, {
            "from": source,
            "type": rtype,
            "to": broker_ref,
            "evidence": relation.get("evidence") or [],
            "properties": props,
            "confidence": _confidence_floor(relation.get("confidence"), "medium"),
        }))
    return added


def normalize_api_granularity(payload: dict[str, Any], *, min_fragments: int = 4) -> int:
    """Collapse route-level API fragments into one contract per component.

    The LLM sometimes emits one API entity per route. Backstage-style catalogs
    are more useful when the contract is the API entity and routes are
    operations. This conservative pass only merges components with many tiny
    API fragments, leaving smaller sets of intentionally separate contracts
    alone.
    """
    entities = payload.get("entities") or []
    relations = payload.get("relations") or []
    apis_by_provider: dict[str, list[dict[str, Any]]] = {}
    for entity in entities:
        if entity.get("kind") != "API":
            continue
        annotations = entity.get("metadata", {}).get("annotations", {}) or {}
        provider = annotations.get("exposed_by") or ""
        if provider:
            apis_by_provider.setdefault(str(provider), []).append(entity)

    collapsed = 0
    for provider, apis in sorted(apis_by_provider.items()):
        if len(apis) < min_fragments:
            continue
        if any(len((api.get("metadata", {}).get("annotations", {}) or {}).get("operations") or []) > 2 for api in apis):
            continue
        merged_name = canonical_name(f"{provider}-API")
        merged_ref = f"API:{merged_name}"
        old_refs = {entity_ref(api) for api in apis}
        operations: list[dict[str, Any]] = []
        op_seen: set[str] = set()
        evidence: list[dict[str, Any]] = []
        source_repos: set[str] = set()
        aliases: set[str] = set()
        descriptions: list[str] = []
        types: set[str] = set()
        confidence = "high"
        for api in apis:
            meta = api.get("metadata", {}) or {}
            annotations = meta.get("annotations", {}) or {}
            aliases.add(str(meta.get("name") or ""))
            aliases.update(str(alias) for alias in (annotations.get("aliases") or []) if alias)
            for repo in annotations.get("source_repos") or []:
                if repo:
                    source_repos.add(str(repo))
            for op in annotations.get("operations") or []:
                if not isinstance(op, dict):
                    continue
                sig = f"{op.get('method','')} {op.get('path','')} {op.get('name','')}"
                if sig not in op_seen:
                    operations.append(op)
                    op_seen.add(sig)
            _merge_evidence(evidence, api.get("evidence") or [])
            if meta.get("description"):
                descriptions.append(str(meta["description"]))
            if (api.get("spec") or {}).get("type"):
                types.add(str((api.get("spec") or {}).get("type")))
            confidence = _confidence_floor(confidence, api.get("confidence"))

        merged_api = {
            "kind": "API",
            "metadata": {
                "name": merged_name,
                "description": " ".join(descriptions[:3]),
                "annotations": {
                    "exposed_by": provider,
                    "operations": operations,
                    "source_repos": sorted(source_repos),
                    "aliases": sorted(alias for alias in aliases if alias and alias != merged_name),
                    "granularity_normalized": "true",
                },
            },
            "spec": {
                "type": sorted(types)[0] if types else "rest",
                "lifecycle": "unknown",
                "owner": "unknown",
            },
            "evidence": evidence,
            "confidence": confidence or "medium",
        }
        payload["entities"] = [e for e in payload.get("entities") or [] if entity_ref(e) not in old_refs]
        _upsert_payload_entity(payload, merged_api)

        new_relations: list[dict[str, Any]] = []
        added_provider_relation = False
        for relation in relations:
            if relation.get("type") == "providesApi" and relation.get("to") in old_refs:
                if not added_provider_relation:
                    merged_relation = dict(relation)
                    merged_relation["to"] = merged_ref
                    merged_relation["evidence"] = evidence
                    merged_relation["properties"] = {
                        **(relation.get("properties") or {}),
                        "target_kind": "api",
                        "granularity_normalized": True,
                    }
                    new_relations.append(merged_relation)
                    added_provider_relation = True
                continue
            if relation.get("from") in old_refs:
                relation = {**relation, "from": merged_ref}
            if relation.get("to") in old_refs:
                relation = {**relation, "to": merged_ref}
            new_relations.append(relation)
        payload["relations"] = []
        for relation in new_relations:
            _upsert_payload_relation(payload, relation)
        relations = payload.get("relations") or []
        collapsed += len(apis) - 1
    return collapsed


_INFRA_COMPONENT_TAGS = {
    "database",
    "datastore",
    "db",
    "mongodb",
    "mongo",
    "mysql",
    "postgres",
    "postgresql",
    "redis",
    "cache",
    "seed-data",
}


def _resource_match_keys(entity: dict[str, Any]) -> set[str]:
    meta = entity.get("metadata", {}) or {}
    spec = entity.get("spec", {}) or {}
    annotations = meta.get("annotations", {}) or {}
    keys = {
        canonical_key(str(meta.get("name") or "")),
        host_to_key(str(spec.get("host") or "")),
        host_to_key(str(annotations.get("datasource_url") or "")),
    }
    for alias in annotations.get("aliases") or []:
        keys.add(canonical_key(str(alias)) or host_to_key(str(alias)))
    return {key for key in keys if key}


def demote_infrastructure_components(payload: dict[str, Any]) -> int:
    """Remove DB/cache-only Components when the same thing is already a Resource.

    Some repos build a database image with seed data. That is source-backed, but
    for catalog navigation it is infrastructure, not a runnable application
    Component. This pass only demotes conservative cases that have a matching
    Resource host/name and no API/message behavior.
    """
    entities = payload.get("entities") or []
    relations = payload.get("relations") or []
    resource_by_key: dict[str, str] = {}
    resources_by_ref = {
        entity_ref(entity): entity
        for entity in entities
        if entity.get("kind") == "Resource"
    }
    for ref, resource in resources_by_ref.items():
        for key in _resource_match_keys(resource):
            resource_by_key.setdefault(key, ref)

    relation_counts: dict[str, dict[str, int]] = {}
    for relation in relations:
        for side in ("from", "to"):
            ref = relation.get(side)
            if ref:
                counts = relation_counts.setdefault(ref, {})
                counts[relation.get("type") or ""] = counts.get(relation.get("type") or "", 0) + 1

    demotions: dict[str, str] = {}
    for entity in entities:
        if entity.get("kind") != "Component":
            continue
        ref = entity_ref(entity)
        meta = entity.get("metadata", {}) or {}
        spec = entity.get("spec", {}) or {}
        tags = {canonical_key(str(tag)) for tag in (meta.get("tags") or [])}
        text = " ".join([
            str(meta.get("name") or ""),
            str(meta.get("description") or ""),
            str(spec.get("type") or ""),
            *[str(tag) for tag in (meta.get("tags") or [])],
        ]).lower()
        looks_infra = bool(tags & _INFRA_COMPONENT_TAGS) or any(marker in text for marker in ("database", "mongodb", "mysql", "postgres", "redis", "seed data"))
        if not looks_infra:
            continue
        counts = relation_counts.get(ref, {})
        behavior_edges = sum(counts.get(kind, 0) for kind in ("providesApi", "consumesApi", "producesMessage", "consumesMessage", "communicatesWith"))
        if behavior_edges:
            continue
        match_key = canonical_key(str(meta.get("name") or ""))
        target_resource = resource_by_key.get(match_key)
        if target_resource:
            demotions[ref] = target_resource

    if not demotions:
        return 0

    payload["entities"] = [entity for entity in entities if entity_ref(entity) not in demotions]
    rewritten: list[dict[str, Any]] = []
    for relation in relations:
        source = relation.get("from")
        target = relation.get("to")
        if source in demotions:
            continue
        if target in demotions:
            if relation.get("type") == "partOf":
                continue
            relation = copy.deepcopy(relation)
            target_resource = demotions[target]
            relation["to"] = target_resource
            props = relation.setdefault("properties", {})
            props["target_kind"] = "resource"
            props["demoted_from_component"] = target
            relation["confidence"] = _confidence_floor(relation.get("confidence"), (resources_by_ref.get(target_resource) or {}).get("confidence"))
        rewritten.append(relation)

    deduped: dict[str, dict[str, Any]] = {}
    for relation in rewritten:
        key = relation_key(relation)
        if key in deduped:
            merge_relation(deduped[key], relation)
        else:
            deduped[key] = relation
    payload["relations"] = list(deduped.values())
    return len(demotions)


def _resource_endpoint(resource_ref: str, entity: dict[str, Any] | None) -> str:
    """Return the protocol-agnostic shared endpoint represented by a resource.

    Subscription resources normalize back to their shared topic/stream when the
    extractor captured that link; direct queues/files/tables remain keyed by
    their own resource name.
    """
    if not entity:
        return resource_ref.split(":", 1)[-1]
    meta = entity.get("metadata", {}) or {}
    annotations = meta.get("annotations", {}) or {}
    explicit = annotations.get("subscribes_to")
    if explicit:
        return str(explicit)
    name = meta.get("name") or resource_ref.split(":", 1)[-1]
    marker = ".VirtualTopic."
    if name.startswith("Consumer.") and marker in name:
        return "VirtualTopic." + name.split(marker, 1)[1]
    return name


def _is_generic_broker_endpoint(endpoint: str) -> bool:
    key = re.sub(r"[^a-z0-9]+", "", endpoint.lower())
    return key in GENERIC_BROKER_ENDPOINT_KEYS or key in BROKER_TECHNOLOGIES


def _transport_for(ref: str, entity: dict[str, Any] | None, relation: dict[str, Any]) -> str:
    props = relation.get("properties") or {}
    if props.get("protocol") and props["protocol"] != "unknown":
        return str(props["protocol"])
    spec = (entity or {}).get("spec", {}) or {}
    for key in ("technology", "type", "category"):
        value = spec.get(key)
        if value:
            return str(value)
    return _ref_kind(ref).lower() or "unknown"


def _operation_or_endpoint(relation: dict[str, Any], fallback: str) -> str:
    props = relation.get("properties") or {}
    for key in ("operation_or_usage", "message_or_event_name", "endpoint"):
        value = props.get(key)
        if value:
            return str(value)
    return fallback


def _edge_sig(relation: dict[str, Any]) -> str:
    return f"{relation.get('from')}|{relation.get('type')}|{relation.get('to')}"


def _confidence_floor(*values: str | None) -> str:
    present = [v for v in values if v]
    if not present:
        return "medium"
    return min(present, key=lambda v: CONFIDENCE_RANK.get(v, -1))


def _merge_property_list(props: dict[str, Any], key: str, values: Iterable[str]) -> None:
    existing = {str(v) for v in (props.get(key) or []) if v}
    existing.update(str(v) for v in values if v)
    if existing:
        props[key] = sorted(existing)


def _add_communication_relation(
    relations: list[dict[str, Any]],
    index: dict[tuple[str, str, str], dict[str, Any]],
    *,
    source: str,
    target: str,
    endpoint: str,
    transport: str,
    mechanism: str,
    confidence: str,
    evidence: list[dict[str, Any]],
    via_edges: list[str],
) -> bool:
    if not source or not target or source == target:
        return False
    if _ref_kind(source) != "Component" or _ref_kind(target) != "Component":
        return False
    key = (source, COMMUNICATION_RELATION, target)
    relation = index.get(key)
    created = False
    if relation is None:
        relation = {
            "from": source,
            "type": COMMUNICATION_RELATION,
            "to": target,
            "evidence": [],
            "confidence": confidence or "medium",
            "properties": {
                "derived": True,
                "endpoint": endpoint,
                "transport": transport,
                "mechanism": mechanism,
                "endpoints": [],
                "transports": [],
                "mechanisms": [],
                "via_edges": [],
            },
        }
        relations.append(relation)
        index[key] = relation
        created = True
    props = relation.setdefault("properties", {})
    _merge_property_list(props, "endpoints", [endpoint])
    _merge_property_list(props, "transports", [transport])
    _merge_property_list(props, "mechanisms", [mechanism])
    _merge_property_list(props, "via_edges", via_edges)
    props.setdefault("endpoint", endpoint)
    props.setdefault("transport", transport)
    props.setdefault("mechanism", mechanism)

    seen_evidence = {(ev.get("path"), ev.get("line"), ev.get("snippet")) for ev in relation.setdefault("evidence", [])}
    for ev in evidence:
        sig = (ev.get("path"), ev.get("line"), ev.get("snippet"))
        if sig not in seen_evidence:
            relation["evidence"].append(ev)
            seen_evidence.add(sig)
    relation["confidence"] = _confidence_floor(relation.get("confidence"), confidence)
    return created


def derive_communication_flows(payload: dict[str, Any]) -> int:
    """Derive service-to-service communication flows from lower-level catalog facts.

    The derived edge is intentionally protocol-agnostic. It keeps APIs,
    resources, and raw dependency edges intact, while adding a `communicatesWith`
    shortcut that agents and humans can traverse without knowing whether the
    underlying mechanism was HTTP, GraphQL, a broker topic, a direct queue, or a
    shared handoff resource.
    """
    entities = _entity_by_ref(payload)
    relations = payload.setdefault("relations", [])
    relation_index: dict[tuple[str, str, str], dict[str, Any]] = {
        (r.get("from"), r.get("type"), r.get("to")): r
        for r in relations
        if r.get("from") and r.get("type") and r.get("to")
    }
    existing_communication_keys = {
        key for key, rel in relation_index.items()
        if rel.get("type") == COMMUNICATION_RELATION
    }

    api_providers: dict[str, list[dict[str, Any]]] = {}
    producers_by_endpoint: dict[str, list[dict[str, Any]]] = {}
    consumers_by_endpoint: dict[str, list[dict[str, Any]]] = {}
    writers_by_resource: dict[str, list[dict[str, Any]]] = {}
    readers_by_resource: dict[str, list[dict[str, Any]]] = {}

    for relation in relations:
        rtype = relation.get("type")
        target = relation.get("to")
        if rtype == "providesApi" and _ref_kind(target) == "API":
            api_providers.setdefault(target, []).append(relation)
        elif rtype == "producesMessage":
            if _ref_kind(target) == "Resource":
                endpoint = _resource_endpoint(target, entities.get(target))
                producers_by_endpoint.setdefault(endpoint, []).append(relation)
        elif rtype == "consumesMessage":
            if _ref_kind(target) == "Resource":
                endpoint = _resource_endpoint(target, entities.get(target))
                consumers_by_endpoint.setdefault(endpoint, []).append(relation)
        elif rtype == "writesResource" and _ref_kind(target) == "Resource":
            writers_by_resource.setdefault(target, []).append(relation)
        elif rtype == "readsResource" and _ref_kind(target) == "Resource":
            readers_by_resource.setdefault(target, []).append(relation)

    added = 0

    # Synchronous API-like calls.
    for relation in list(relations):
        if relation.get("type") != "consumesApi":
            continue
        source = relation.get("from")
        target = relation.get("to")
        target_kind = _ref_kind(target)
        if target_kind == "Component":
            endpoint = _operation_or_endpoint(relation, target)
            transport = _transport_for(target, entities.get(target), relation)
            added += int(_add_communication_relation(
                relations,
                relation_index,
                source=source,
                target=target,
                endpoint=endpoint,
                transport=transport,
                mechanism="api-call",
                confidence=relation.get("confidence") or "medium",
                evidence=relation.get("evidence") or [],
                via_edges=[_edge_sig(relation)],
            ))
        elif target_kind == "API":
            api_entity = entities.get(target)
            endpoint = _operation_or_endpoint(relation, target)
            transport = _transport_for(target, api_entity, relation)
            for provider_relation in api_providers.get(target, []):
                added += int(_add_communication_relation(
                    relations,
                    relation_index,
                    source=source,
                    target=provider_relation.get("from"),
                    endpoint=endpoint,
                    transport=transport,
                    mechanism="api-call",
                    confidence=_confidence_floor(relation.get("confidence"), provider_relation.get("confidence")),
                    evidence=(relation.get("evidence") or []) + (provider_relation.get("evidence") or []),
                    via_edges=[_edge_sig(relation), _edge_sig(provider_relation)],
                ))

    # Component-target async dependencies emitted directly by the extractor.
    for relation in list(relations):
        rtype = relation.get("type")
        source = relation.get("from")
        target = relation.get("to")
        if rtype == "producesMessage" and _ref_kind(target) == "Component":
            added += int(_add_communication_relation(
                relations,
                relation_index,
                source=source,
                target=target,
                endpoint=_operation_or_endpoint(relation, target),
                transport=_transport_for(target, entities.get(target), relation),
                mechanism="async-message",
                confidence=relation.get("confidence") or "medium",
                evidence=relation.get("evidence") or [],
                via_edges=[_edge_sig(relation)],
            ))
        elif rtype == "consumesMessage" and _ref_kind(target) == "Component":
            added += int(_add_communication_relation(
                relations,
                relation_index,
                source=target,
                target=source,
                endpoint=_operation_or_endpoint(relation, target),
                transport=_transport_for(target, entities.get(target), relation),
                mechanism="async-message",
                confidence=relation.get("confidence") or "medium",
                evidence=relation.get("evidence") or [],
                via_edges=[_edge_sig(relation)],
            ))

    # Broker/topic/queue/event-stream style rendezvous resources.
    for endpoint in sorted(set(producers_by_endpoint) & set(consumers_by_endpoint)):
        if _is_generic_broker_endpoint(endpoint):
            continue
        for producer in producers_by_endpoint[endpoint]:
            producer_resource = entities.get(producer.get("to"))
            for consumer in consumers_by_endpoint[endpoint]:
                transport = _transport_for(producer.get("to"), producer_resource, producer)
                if transport == "unknown":
                    transport = _transport_for(consumer.get("to"), entities.get(consumer.get("to")), consumer)
                added += int(_add_communication_relation(
                    relations,
                    relation_index,
                    source=producer.get("from"),
                    target=consumer.get("from"),
                    endpoint=endpoint,
                    transport=transport,
                    mechanism="async-message",
                    confidence=_confidence_floor(producer.get("confidence"), consumer.get("confidence")),
                    evidence=(producer.get("evidence") or []) + (consumer.get("evidence") or []),
                    via_edges=[_edge_sig(producer), _edge_sig(consumer)],
                ))

    # Shared handoff resources: DB outbox/inbox tables, object prefixes, files,
    # caches, etc. These are lower confidence because read/write does not always
    # mean intentional service communication.
    for resource_ref in sorted(set(writers_by_resource) & set(readers_by_resource)):
        resource = entities.get(resource_ref)
        endpoint = _resource_endpoint(resource_ref, resource)
        for writer in writers_by_resource[resource_ref]:
            for reader in readers_by_resource[resource_ref]:
                added += int(_add_communication_relation(
                    relations,
                    relation_index,
                    source=writer.get("from"),
                    target=reader.get("from"),
                    endpoint=endpoint,
                    transport=_transport_for(resource_ref, resource, writer),
                    mechanism="shared-resource",
                    confidence="low",
                    evidence=(writer.get("evidence") or []) + (reader.get("evidence") or []),
                    via_edges=[_edge_sig(writer), _edge_sig(reader)],
                ))

    # Return only newly-created flow pairs, not endpoint merges into existing
    # communication edges.
    return len({
        key for key, rel in relation_index.items()
        if rel.get("type") == COMMUNICATION_RELATION and key not in existing_communication_keys
    })


def refresh_summary_counts(payload: dict[str, Any]) -> None:
    counts: dict[str, int] = {}
    for entity in payload.get("entities") or []:
        kind = entity.get("kind")
        if kind:
            counts[kind] = counts.get(kind, 0) + 1
    edge_counts: dict[str, int] = {}
    for relation in payload.get("relations") or []:
        rtype = relation.get("type")
        if rtype:
            edge_counts[rtype] = edge_counts.get(rtype, 0) + 1
    payload.setdefault("summary", {})["node_kinds"] = counts
    payload.setdefault("summary", {})["relation_types"] = edge_counts
    payload.setdefault("summary", {})["entities"] = sum(counts.values())
    payload.setdefault("summary", {})["relations"] = sum(edge_counts.values())


def demote_provider_duplicate_components(payload: dict[str, Any]) -> int:
    """Fold external placeholder Components into matching Providers.

    Extractors sometimes emit an observability/SaaS endpoint as a Component when
    one repo calls it like a service, while another repo correctly emits the
    same runtime dependency as a Provider. If the placeholder Component has no
    source repo and matches a Provider by name or alias, the Provider is the
    more specific entity kind.
    """
    entities = payload.get("entities") or []
    provider_by_key: dict[str, str] = {}
    entities_by_ref = {f"{e.get('kind')}:{e.get('metadata', {}).get('name')}": e for e in entities}

    def keys_for(entity: dict[str, Any]) -> set[str]:
        meta = entity.get("metadata") or {}
        annotations = meta.get("annotations") or {}
        values = [meta.get("name") or "", *list(annotations.get("aliases") or [])]
        return {canonical_key(v) or host_to_key(v) for v in values if v}

    for entity in entities:
        if entity.get("kind") != "Provider":
            continue
        ref = f"Provider:{entity.get('metadata', {}).get('name')}"
        for key in keys_for(entity):
            if key:
                provider_by_key.setdefault(key, ref)

    remap: dict[str, str] = {}
    for entity in entities:
        if entity.get("kind") != "Component":
            continue
        annotations = entity.get("metadata", {}).get("annotations") or {}
        if annotations.get("source_repos"):
            continue
        if str(annotations.get("external") or "").lower() != "true":
            continue
        provider_ref = next((provider_by_key.get(key) for key in keys_for(entity) if provider_by_key.get(key)), None)
        if provider_ref:
            remap[f"Component:{entity.get('metadata', {}).get('name')}"] = provider_ref
            provider = entities_by_ref.get(provider_ref)
            if provider:
                merge_entity(provider, entity)
                provider["kind"] = "Provider"

    if not remap:
        return 0

    payload["entities"] = [
        entity for entity in entities
        if f"{entity.get('kind')}:{entity.get('metadata', {}).get('name')}" not in remap
    ]
    deduped: dict[str, dict[str, Any]] = {}
    for relation in payload.get("relations") or []:
        relation = copy.deepcopy(relation)
        if relation.get("from") in remap:
            continue
        if relation.get("to") in remap:
            relation["to"] = remap[relation["to"]]
            if relation.get("type") in {"consumesApi", COMMUNICATION_RELATION}:
                relation["type"] = "dependsOn"
            relation.setdefault("properties", {})["target_kind"] = "provider"
            relation["confidence"] = "review"
        key = relation_key(relation)
        if key in deduped:
            merge_relation(deduped[key], relation)
        else:
            deduped[key] = relation
    payload["relations"] = list(deduped.values())
    return len(remap)


def merge_suffix_duplicate_components(payload: dict[str, Any]) -> int:
    """Merge external component placeholders into sourced components by suffix.

    Monorepo callers often refer to `repo-service` while the extracted service
    unit is just `service`. If the longer component has no source repo and the
    shorter one does, the shorter sourced component is canonical.
    """
    entities = payload.get("entities") or []
    sourced: dict[str, tuple[str, str]] = {}
    entities_by_ref = {f"{e.get('kind')}:{e.get('metadata', {}).get('name')}": e for e in entities}
    for entity in entities:
        if entity.get("kind") != "Component":
            continue
        annotations = entity.get("metadata", {}).get("annotations") or {}
        if not annotations.get("source_repos"):
            continue
        name = entity.get("metadata", {}).get("name") or ""
        sourced[canonical_key(name)] = (canonical_name(name).lower(), f"Component:{name}")

    remap: dict[str, str] = {}
    for entity in entities:
        if entity.get("kind") != "Component":
            continue
        annotations = entity.get("metadata", {}).get("annotations") or {}
        if annotations.get("source_repos"):
            continue
        if str(annotations.get("external") or "").lower() != "true":
            continue
        name = entity.get("metadata", {}).get("name") or ""
        name_key = canonical_key(name)
        name_slug = canonical_name(name).lower()
        target_ref = None
        for key, (slug, ref) in sourced.items():
            if name_key != key and name_slug.endswith(f"-{slug}"):
                target_ref = ref
                break
        if not target_ref:
            aliases = annotations.get("aliases") or []
            alias_keys = {canonical_key(alias) or host_to_key(alias) for alias in aliases if alias}
            target_ref = next((ref for key, (_slug, ref) in sourced.items() if key in alias_keys), None)
        if target_ref:
            source_ref = f"Component:{name}"
            remap[source_ref] = target_ref
            target = entities_by_ref.get(target_ref)
            if target:
                merge_entity(target, entity)

    if not remap:
        return 0

    payload["entities"] = [
        entity for entity in entities
        if f"{entity.get('kind')}:{entity.get('metadata', {}).get('name')}" not in remap
    ]
    deduped: dict[str, dict[str, Any]] = {}
    for relation in payload.get("relations") or []:
        relation = copy.deepcopy(relation)
        if relation.get("from") in remap:
            relation["from"] = remap[relation["from"]]
        if relation.get("to") in remap:
            relation["to"] = remap[relation["to"]]
            relation.setdefault("properties", {})["target_kind"] = "component"
        if relation.get("from") == relation.get("to"):
            continue
        key = relation_key(relation)
        if key in deduped:
            merge_relation(deduped[key], relation)
        else:
            deduped[key] = relation
    payload["relations"] = list(deduped.values())
    return len(remap)


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
                annotations.setdefault("github.com/source-location", github_source_location(repo))
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
    cloned_repos = find_repos(
        root,
        workspace_config.get("orgs") or [],
        workspace_config.get("excluded_repos") or [],
        workspace_config.get("repo_units") or [],
    )
    cloned_ids = {r["id"] for r in cloned_repos}

    deferred_by_repo: dict[str, list[dict[str, Any]]] = {}
    repo_count = 0
    repos_by_id = {repo["id"]: repo for repo in cloned_repos}
    static_api_specs_merged = 0
    static_api_operations_added = 0
    for payload in load_catalog_dir(catalog_dir):
        repo_id = payload.get("repo", {}).get("id") or "unknown"
        repo_root = Path((repos_by_id.get(repo_id) or {}).get("absolute_path") or root / short_repo_name(repo_id))
        static_api_summary = merge_static_api_operations(payload, repo_root)
        if static_api_summary.get("operations_added"):
            payload.setdefault("_meta", {})["static_api_specs"] = static_api_summary
            static_api_specs_merged += int(static_api_summary.get("merged_apis") or 0)
            static_api_operations_added += int(static_api_summary.get("operations_added") or 0)
        result = add_repo(catalog, payload)
        deferred_by_repo[repo_id] = result["deferred_dependencies"]
        repo_count += 1

    for seed in load_seeds(seeds_dir):
        if "frontends" in seed or "native_apps" in seed:
            add_seed_platform(catalog, seed)
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
    payload["summary"]["static_api_specs_merged"] = static_api_specs_merged
    payload["summary"]["static_api_operations_added"] = static_api_operations_added

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

    demoted_infra_components = demote_infrastructure_components(payload)
    payload["summary"]["demoted_infrastructure_components"] = demoted_infra_components
    merged_suffix_components = merge_suffix_duplicate_components(payload)
    payload["summary"]["merged_suffix_duplicate_components"] = merged_suffix_components
    demoted_provider_components = demote_provider_duplicate_components(payload)
    payload["summary"]["demoted_provider_duplicate_components"] = demoted_provider_components
    normalized_api_fragments = normalize_api_granularity(payload)
    payload["summary"]["normalized_api_fragments"] = normalized_api_fragments
    derived_broker_resources = derive_broker_resources(payload)
    payload["summary"]["derived_broker_resources"] = derived_broker_resources
    derived_communication_flows = derive_communication_flows(payload)
    payload["summary"]["derived_communication_flows"] = derived_communication_flows
    # Re-enforce "high-confidence edges must carry evidence" on the final graph:
    # the derivation passes above can mint high edges (e.g. merged providesApi)
    # that lack evidence, which the earlier Catalog-level filter never saw.
    payload["summary"]["dropped_for_missing_evidence"] = (
        payload["summary"].get("dropped_for_missing_evidence", 0)
        + filter_payload_evidence_required(payload)
    )
    refresh_summary_counts(payload)

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
