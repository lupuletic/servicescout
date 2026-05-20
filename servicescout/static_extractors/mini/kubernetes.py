"""Kubernetes / Helm mini-extractor.

Parses a k8s manifest or Helm template (which is a parameterised k8s
manifest). Surfaces:

  - kind=Service               → Component (service-port surface)
  - kind=Deployment/StatefulSet/DaemonSet
                                 → Component (deployable, with replicas,
                                    container env vars, container ports)
  - kind=Ingress                → API (REST surface; rules become operations)
  - kind=CronJob                → Component (type=cron, schedule attribute)
  - kind=ConfigMap/Secret       → Resource hint (env_or_config_keys)
  - kind=Job                    → Component (type=batch)

We deliberately ignore `apiVersion` specifics — emitting a fact per
manifest is more useful than trying to schema-validate. The catalog
reconciler can dedupe.

Multi-document YAML (separated by `---`) is supported via yaml.safe_load_all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from servicescout.static_extractors.mini import Fact


_K8S_KINDS = {
    "Service", "Deployment", "StatefulSet", "DaemonSet", "Ingress",
    "CronJob", "Job", "ConfigMap", "Secret", "Pod", "ReplicaSet",
    "HorizontalPodAutoscaler", "VirtualService", "Gateway",
}


def is_k8s_yaml(path: Path) -> bool:
    if path.suffix.lower() not in {".yaml", ".yml"}:
        return False
    # Cheap pre-check: does the file mention apiVersion + kind near the top?
    # We don't fully parse here — that's in extract(). Most non-k8s YAML
    # (CI configs, helm Chart.yaml, etc.) won't have both, but some will
    # (Helm Chart.yaml has `apiVersion: v2`). We tolerate false positives;
    # extract() returns [] if there's no recognised kind.
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:2048]
    except OSError:
        return False
    return "apiVersion:" in head and "kind:" in head


def extract(path: Path, repo_root: Path) -> list[Fact]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    try:
        rel_path = str(path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return []

    facts: list[Fact] = []
    try:
        documents = list(yaml.safe_load_all(text))
    except yaml.YAMLError:
        # Helm templates with Go templating expressions {{ .Values.foo }}
        # are not always parseable raw. Skip — the LLM will see them.
        return []

    for doc in documents:
        if not isinstance(doc, dict):
            continue
        kind = (doc.get("kind") or "").strip()
        if kind not in _K8S_KINDS:
            continue
        metadata = doc.get("metadata") or {}
        name = (metadata.get("name") or "").strip() if isinstance(metadata, dict) else ""

        if kind in {"Deployment", "StatefulSet", "DaemonSet"}:
            facts.append(_component_from_workload(rel_path, doc, name, kind))
        elif kind == "Service":
            facts.append(_component_from_service(rel_path, doc, name))
        elif kind == "Ingress":
            facts.append(_api_from_ingress(rel_path, doc, name))
        elif kind == "CronJob":
            facts.append(_component_from_cronjob(rel_path, doc, name))
        elif kind == "Job":
            facts.append(_component_from_job(rel_path, doc, name))
        elif kind in {"ConfigMap", "Secret"}:
            facts.append(_resource_from_config(rel_path, doc, name, kind))
    return facts


# --------------------------------------------------------------------------- #
# Per-kind builders
# --------------------------------------------------------------------------- #


def _evidence(path: str) -> list[dict[str, Any]]:
    # We don't track per-field line numbers (yaml lib doesn't surface
    # them by default). Cite line 1 of the file as a conservative anchor.
    return [{"path": path, "line": 1, "snippet": ""}]


def _component_from_workload(path: str, doc: dict, name: str, kind: str) -> Fact:
    spec = doc.get("spec") or {}
    replicas = spec.get("replicas")
    template = (spec.get("template") or {}).get("spec") or {}
    containers = template.get("containers") or []
    container_images = []
    container_ports: list[str] = []
    env_keys: list[str] = []
    if isinstance(containers, list):
        for c in containers:
            if not isinstance(c, dict):
                continue
            if c.get("image"):
                container_images.append(c["image"])
            for p in (c.get("ports") or []):
                if isinstance(p, dict) and p.get("containerPort") is not None:
                    container_ports.append(str(p["containerPort"]))
            for e in (c.get("env") or []):
                if isinstance(e, dict) and e.get("name"):
                    env_keys.append(e["name"])
    return Fact(
        category="components",
        rule=f"k8s.{kind}",
        body={
            "name": name or path,
            "type": "service",
            "runtime": "long-running",
            "lifecycle": "",
            "subcomponent_of": "",
            "notes": (
                f"k8s {kind} with {replicas} replicas. Images: {', '.join(container_images) or 'n/a'}."
                if replicas is not None else
                f"k8s {kind}. Images: {', '.join(container_images) or 'n/a'}."
            ),
            "tags": [f"port:{p}" for p in container_ports]
                    + [f"image:{i}" for i in container_images]
                    + [f"env:{k}" for k in env_keys],
            "environments": [],
            "evidence": _evidence(path),
        },
    )


def _component_from_service(path: str, doc: dict, name: str) -> Fact:
    spec = doc.get("spec") or {}
    ports = spec.get("ports") or []
    port_strs = []
    if isinstance(ports, list):
        for p in ports:
            if isinstance(p, dict) and p.get("port") is not None:
                port_strs.append(str(p["port"]))
    return Fact(
        category="components",
        rule="k8s.Service",
        body={
            "name": name or path,
            "type": "service",
            "runtime": "long-running",
            "lifecycle": "",
            "subcomponent_of": "",
            "notes": f"k8s Service exposing ports: {', '.join(port_strs) or 'n/a'}.",
            "tags": [f"port:{p}" for p in port_strs] + ["k8s:service"],
            "environments": [],
            "evidence": _evidence(path),
        },
    )


def _api_from_ingress(path: str, doc: dict, name: str) -> Fact:
    spec = doc.get("spec") or {}
    operations: list[dict[str, Any]] = []
    rules = spec.get("rules") or []
    if isinstance(rules, list):
        for r in rules:
            if not isinstance(r, dict):
                continue
            host = r.get("host") or ""
            http = r.get("http") or {}
            paths = (http.get("paths") or []) if isinstance(http, dict) else []
            if isinstance(paths, list):
                for p in paths:
                    if not isinstance(p, dict):
                        continue
                    operations.append({
                        "name": f"{host} {p.get('path', '/')}".strip(),
                        "method": "GET",
                        "path": p.get("path") or "/",
                    })
    return Fact(
        category="apis",
        rule="k8s.Ingress",
        body={
            "name": name or path,
            "type": "rest",
            "exposed_by": name or path,
            "operations": operations,
            "notes": f"REST surface from k8s Ingress {name}.",
            "evidence": _evidence(path),
        },
    )


def _component_from_cronjob(path: str, doc: dict, name: str) -> Fact:
    spec = doc.get("spec") or {}
    schedule = (spec.get("schedule") or "").strip()
    suspend = spec.get("suspend", False)
    job_tmpl_spec = ((spec.get("jobTemplate") or {}).get("spec") or {}).get("template", {}).get("spec") or {}
    containers = job_tmpl_spec.get("containers") or []
    container_images = [c.get("image") for c in containers if isinstance(c, dict) and c.get("image")]
    return Fact(
        category="components",
        rule="k8s.CronJob",
        body={
            "name": name or path,
            "type": "cron",
            "runtime": "cron",
            "lifecycle": "deprecated" if suspend else "",
            "subcomponent_of": "",
            "notes": f"k8s CronJob, schedule: {schedule or 'n/a'}. Images: {', '.join(container_images) or 'n/a'}.",
            "tags": [f"schedule:{schedule}"] if schedule else [],
            "environments": [],
            "evidence": _evidence(path),
        },
    )


def _component_from_job(path: str, doc: dict, name: str) -> Fact:
    return Fact(
        category="components",
        rule="k8s.Job",
        body={
            "name": name or path,
            "type": "function",
            "runtime": "batch",
            "lifecycle": "",
            "subcomponent_of": "",
            "notes": "k8s Job.",
            "tags": ["k8s:job"],
            "environments": [],
            "evidence": _evidence(path),
        },
    )


def _resource_from_config(path: str, doc: dict, name: str, kind: str) -> Fact:
    data = doc.get("data") or {}
    keys = list(data.keys()) if isinstance(data, dict) else []
    return Fact(
        category="resources",
        rule=f"k8s.{kind}",
        body={
            "name": name or path,
            "type": "config-store",
            "technology": "kubernetes",
            "host_or_instance": "",
            "database_or_schema": "",
            "tables_or_collections": [],
            "access": "read",
            "env_or_config_keys": keys,
            "used_by": "",
            "messaging_pattern": "",
            "subscribes_to": "",
            "datasource_url": "",
            "confidence": "high",
            "notes": f"k8s {kind} with {len(keys)} keys.",
            "evidence": _evidence(path),
        },
    )
