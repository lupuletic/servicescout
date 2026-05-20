"""Export catalog.json to Backstage-compatible YAML.

Produces:
  - data/backstage/<component>.yaml  (one Component per file)
  - data/backstage/api/<api>.yaml
  - data/backstage/resource/<resource>.yaml
  - data/backstage/all-entities.yaml  (multi-doc, for static catalog import)

Each YAML follows Backstage's v1alpha1 entity schema. Use these files as a seed
catalog when adopting Backstage, or as a verification target ("does our auto-
extracted catalog match a hand-written one?").
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


HERE = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = HERE / "data" / "catalog.json"
DEFAULT_OUTPUT_DIR = HERE / "data" / "backstage"


KIND_TO_BACKSTAGE = {
    "Component": "Component",
    "API": "API",
    "Resource": "Resource",
    "System": "System",
    "Domain": "Domain",
    "Group": "Group",
}


def to_backstage_entity(entity: dict[str, Any], relations: list[dict[str, Any]]) -> dict[str, Any]:
    kind = KIND_TO_BACKSTAGE.get(entity["kind"])
    if not kind:
        return {}
    meta = entity.get("metadata", {})
    spec_in = entity.get("spec", {})
    name = meta["name"]
    annotations = dict(meta.get("annotations") or {})
    if isinstance(annotations.get("source_repos"), list):
        annotations["github.com/source-repos"] = ",".join(annotations["source_repos"])
    if isinstance(annotations.get("aliases"), list):
        annotations["servicescout/aliases"] = ",".join(annotations["aliases"])
    for key in ("env_keys", "tables_or_collections", "operations"):
        if isinstance(annotations.get(key), list):
            annotations[f"servicescout/{key}"] = ",".join(str(v) for v in annotations[key])
    # Translate ServiceScout / community keys into Backstage's well-known
    # `backstage.io/*` form so the exported YAML works in real Backstage.
    # Our native catalog keeps the originals.
    src = annotations.get("github.com/source-location")
    if src and not annotations.get("backstage.io/source-location"):
        annotations["backstage.io/source-location"] = f"url:{src}/"
    if src and not annotations.get("backstage.io/view-url"):
        annotations["backstage.io/view-url"] = src
    if src and not annotations.get("backstage.io/edit-url"):
        annotations["backstage.io/edit-url"] = f"{src}/edit/main/catalog-info.yaml"
    annotations.setdefault("backstage.io/techdocs-ref", "dir:.")
    for k in ["source_repos", "aliases", "env_keys", "tables_or_collections", "operations"]:
        annotations.pop(k, None)

    backstage_meta = {
        "name": name,
        "annotations": annotations,
    }
    if meta.get("description"):
        backstage_meta["description"] = meta["description"]
    if meta.get("tags"):
        backstage_meta["tags"] = meta["tags"]
    if meta.get("title"):
        backstage_meta["title"] = meta["title"]

    # Backstage rejects lifecycle="unknown" — strip it when exporting.
    def _lifecycle(raw: str | None) -> str | None:
        if raw and raw in ("production", "experimental", "deprecated"):
            return raw
        return None

    spec: dict[str, Any] = {}
    if kind == "Component":
        spec = {
            "type": spec_in.get("type", "service"),
            "owner": spec_in.get("owner") or "unknown",
        }
        lc = _lifecycle(spec_in.get("lifecycle"))
        if lc:
            spec["lifecycle"] = lc
        if spec_in.get("system"):
            spec["system"] = spec_in["system"]
        if spec_in.get("domain"):
            spec["domain"] = spec_in["domain"]
        if spec_in.get("subcomponentOf"):
            spec["subcomponentOf"] = spec_in["subcomponentOf"]
        provides = [r["to"].split(":", 1)[1] for r in relations if r["from"] == f"Component:{name}" and r["type"] == "providesApi"]
        if provides:
            spec["providesApis"] = provides
        consumes = [r["to"].split(":", 1)[1] for r in relations if r["from"] == f"Component:{name}" and r["type"] == "consumesApi"]
        if consumes:
            spec["consumesApis"] = consumes
        depends = [r["to"] for r in relations if r["from"] == f"Component:{name}" and r["type"] in {"dependsOn", "readsResource", "writesResource", "producesMessage", "consumesMessage"}]
        if depends:
            spec["dependsOn"] = depends
    elif kind == "API":
        spec = {
            "type": spec_in.get("type", "openapi"),
            "owner": spec_in.get("owner") or "unknown",
        }
        lc = _lifecycle(spec_in.get("lifecycle"))
        if lc:
            spec["lifecycle"] = lc
        # `definition` is required by Backstage but we usually don't have the
        # actual schema text — omit the entity when we'd otherwise emit a stub.
        # Downstream consumers can filter for entities with `definition` set.
        defn = (entity.get("metadata", {}).get("annotations", {}) or {}).get("definition")
        if defn and defn != "stub":
            spec["definition"] = defn
        else:
            return {}  # skip — Backstage would reject this entity anyway
    elif kind == "Resource":
        spec = {
            "type": spec_in.get("type", "database"),
            "owner": spec_in.get("owner") or "unknown",
        }
    elif kind == "System":
        spec = {"owner": spec_in.get("owner") or "unknown"}
    elif kind == "Domain":
        spec = {"owner": spec_in.get("owner") or "unknown"}
    elif kind == "Group":
        # Backstage's Group schema has no `children` field — that's computed
        # from `partOf` relations. Only `type` is required.
        spec = {"type": spec_in.get("type", "team")}

    return {
        "apiVersion": "backstage.io/v1alpha1",
        "kind": kind,
        "metadata": backstage_meta,
        "spec": spec,
    }


def write_yaml(path: Path, payload: dict[str, Any] | Iterable[dict[str, Any]]) -> None:
    if yaml is None:
        raise SystemExit("PyYAML is required: pip install pyyaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, dict):
        path.write_text(yaml.safe_dump(payload, sort_keys=False, default_flow_style=False), encoding="utf-8")
    else:
        path.write_text(yaml.safe_dump_all(payload, sort_keys=False, default_flow_style=False), encoding="utf-8")


def export(catalog_path: Path, output_dir: Path) -> dict[str, int]:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    entities = catalog.get("entities") or []
    relations = catalog.get("relations") or []

    all_docs: list[dict[str, Any]] = []
    counts = {"Component": 0, "API": 0, "Resource": 0, "System": 0, "Domain": 0, "Group": 0}
    for entity in entities:
        backstage = to_backstage_entity(entity, relations)
        if not backstage:
            continue
        all_docs.append(backstage)
        kind = backstage["kind"]
        counts[kind] = counts.get(kind, 0) + 1
        subdir = output_dir
        if kind != "Component":
            subdir = output_dir / kind.lower()
        write_yaml(subdir / f"{backstage['metadata']['name']}.yaml", backstage)
    write_yaml(output_dir / "all-entities.yaml", all_docs)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    counts = export(args.catalog, args.output_dir)
    print(json.dumps({"output_dir": str(args.output_dir), "counts": counts}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
