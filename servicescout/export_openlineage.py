"""OpenLineage export.

Render the catalog's reads/writes-Resource edges as OpenLineage events.
OpenLineage is the de-facto open standard for data-lineage tracking
across modern data stacks (Airflow, dbt, Spark, Trino, Flink, Snowflake,
BigQuery). By exposing ServiceScout's catalog in that shape we get:

  - Field-level data lineage queries ("which services write the
    `customer_email` column? where does it flow next?") that data /
    privacy teams already know how to answer in OpenLineage tooling
    like Marquez, DataHub OpenLineage integration, etc.

  - GDPR / data-classification superpower — the catalog already
    captures which Components touch which Resources via reads /
    writes; this commit adds the field-level dimension (tables /
    collections / columns) on top of that.

Mapping (ServiceScout → OpenLineage):

  ServiceScout                           OpenLineage
  --------------------------------------  --------------------------------
  Component                               Job (job.name = component.name)
  Resource                                Dataset (datasetName =
                                           Resource.name; namespace =
                                           Resource.technology or
                                           Resource.host_or_instance)
  dependency[readsResource]               RunEvent with inputs[Dataset]
  dependency[writesResource]              RunEvent with outputs[Dataset]
  Resource.tables_or_collections          Each element → a fields[]
                                           entry on the Dataset's schema
                                           facet (name=table_or_collection,
                                           type="table").
  Resource.env_or_config_keys             Stored as a custom
                                           "servicescout_config" facet on
                                           the Dataset (useful for finding
                                           rotated credentials).

This is a one-shot exporter (`python export_openlineage.py`) — it
reads `data/catalog.json` and writes a directory of OpenLineage JSON
files under `data/openlineage/`. Posting them to a Marquez or other
collector is left to whoever runs this; the JSON files are
ready-to-POST per the OpenLineage v1.1.0 spec.

This is opt-in (the existing pipeline does not call this exporter).
Run it manually when you want to feed the catalog into a lineage
backend, or wire it into CI/CD for continuous lineage sync.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import uuid
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = HERE / "data" / "catalog.json"
DEFAULT_OUTPUT_DIR = HERE / "data" / "openlineage"

OL_PRODUCER = "https://github.com/servicescout/servicescout"
OL_SCHEMA_URL = (
    "https://openlineage.io/spec/2-0-2/OpenLineage.json"
    "#/$defs/RunEvent"
)


def _dataset_namespace(resource_entity: dict[str, Any]) -> str:
    """Pick a OpenLineage namespace for a Resource.

    Convention: prefer host_or_instance when set (e.g. a DB host),
    else the technology (e.g. `postgresql`), else `servicescout`.
    """
    md = resource_entity.get("metadata") or {}
    spec = resource_entity.get("spec") or {}
    host = (spec.get("host_or_instance") or "").strip()
    if host:
        return host
    tech = (spec.get("technology") or "").strip().lower()
    if tech:
        return tech
    return "servicescout"


def _dataset_name(resource_entity: dict[str, Any]) -> str:
    md = resource_entity.get("metadata") or {}
    spec = resource_entity.get("spec") or {}
    schema = (spec.get("database_or_schema") or "").strip()
    name = (md.get("name") or "").strip()
    if schema and name:
        return f"{schema}.{name}"
    return name


def _dataset_schema_facet(resource_entity: dict[str, Any]) -> dict[str, Any] | None:
    """OpenLineage SchemaDatasetFacet listing the tables / collections
    on this resource. Each table_or_collection becomes a top-level field
    of "type: table" — a slight repurposing of the schema facet that
    keeps the dataset-level grain.

    Returns None if no fields are present, to avoid emitting empty
    facets.
    """
    spec = resource_entity.get("spec") or {}
    tables = spec.get("tables_or_collections") or []
    if not tables:
        return None
    return {
        "_producer": OL_PRODUCER,
        "_schemaURL": (
            "https://openlineage.io/spec/facets/1-1-1/SchemaDatasetFacet.json"
            "#/$defs/SchemaDatasetFacet"
        ),
        "fields": [
            {"name": str(t), "type": "table"} for t in tables if t
        ],
    }


def _servicescout_config_facet(resource_entity: dict[str, Any]) -> dict[str, Any] | None:
    spec = resource_entity.get("spec") or {}
    keys = spec.get("env_or_config_keys") or []
    if not keys:
        return None
    return {
        "_producer": OL_PRODUCER,
        "_schemaURL": OL_PRODUCER + "/openlineage-extensions/servicescout-config.json",
        "config_keys": list(keys),
        "access": spec.get("access") or "",
        "confidence": resource_entity.get("confidence", "unknown"),
    }


def _build_dataset(resource_entity: dict[str, Any]) -> dict[str, Any]:
    dataset: dict[str, Any] = {
        "namespace": _dataset_namespace(resource_entity),
        "name": _dataset_name(resource_entity),
        "facets": {},
    }
    schema_facet = _dataset_schema_facet(resource_entity)
    if schema_facet:
        dataset["facets"]["schema"] = schema_facet
    config_facet = _servicescout_config_facet(resource_entity)
    if config_facet:
        dataset["facets"]["servicescout_config"] = config_facet
    return dataset


def _emit_run_event(
    component_entity: dict[str, Any],
    inputs: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    *,
    event_time: str,
) -> dict[str, Any]:
    """Build one OpenLineage COMPLETE RunEvent for a Component.

    Each Component gets one event; the event's inputs/outputs are the
    Resources it reads / writes.
    """
    md = component_entity.get("metadata") or {}
    spec = component_entity.get("spec") or {}
    annotations = md.get("annotations") or {}
    job_name = md.get("name") or "unknown"
    job_namespace = (spec.get("system") or annotations.get("system") or "servicescout").strip() or "servicescout"
    return {
        "eventType": "COMPLETE",
        "eventTime": event_time,
        "run": {
            "runId": str(uuid.uuid4()),
            "facets": {
                "servicescout_component": {
                    "_producer": OL_PRODUCER,
                    "_schemaURL": OL_PRODUCER + "/openlineage-extensions/servicescout-component.json",
                    "type": spec.get("type") or "",
                    "runtime": spec.get("runtime") or "",
                    "lifecycle": spec.get("lifecycle") or "",
                    "owner": annotations.get("owner") or spec.get("owner") or "unknown",
                    "tagline": annotations.get("tagline") or "",
                },
            },
        },
        "job": {
            "namespace": job_namespace,
            "name": job_name,
            "facets": {
                "documentation": {
                    "_producer": OL_PRODUCER,
                    "_schemaURL": (
                        "https://openlineage.io/spec/facets/1-0-2/DocumentationJobFacet.json"
                        "#/$defs/DocumentationJobFacet"
                    ),
                    "description": annotations.get("tagline") or md.get("description") or "",
                },
            },
        },
        "inputs": inputs,
        "outputs": outputs,
        "producer": OL_PRODUCER,
        "schemaURL": OL_SCHEMA_URL,
    }


def build_events(catalog: dict[str, Any], *, event_time: str | None = None) -> list[dict[str, Any]]:
    """Walk the catalog, render one OpenLineage RunEvent per Component
    that has at least one read / write edge.
    """
    if event_time is None:
        event_time = dt.datetime.now(dt.timezone.utc).isoformat()

    entities = catalog.get("entities") or []
    relations = catalog.get("relations") or []

    by_ref: dict[str, dict[str, Any]] = {
        f"{e['kind']}:{e['metadata']['name']}": e for e in entities
    }

    # Group reads / writes by Component (source).
    by_component_inputs: dict[str, list[dict[str, Any]]] = {}
    by_component_outputs: dict[str, list[dict[str, Any]]] = {}
    for rel in relations:
        kind = rel.get("type") or rel.get("kind") or ""
        if kind not in {"readsResource", "writesResource"}:
            continue
        src = rel.get("from") or ""
        tgt = rel.get("to") or ""
        target_entity = by_ref.get(tgt)
        if target_entity is None or target_entity.get("kind") != "Resource":
            continue
        dataset = _build_dataset(target_entity)
        bucket = by_component_inputs if kind == "readsResource" else by_component_outputs
        bucket.setdefault(src, []).append(dataset)

    events: list[dict[str, Any]] = []
    component_refs = sorted(set(by_component_inputs) | set(by_component_outputs))
    for ref in component_refs:
        entity = by_ref.get(ref)
        if entity is None or entity.get("kind") != "Component":
            continue
        events.append(_emit_run_event(
            entity,
            inputs=by_component_inputs.get(ref, []),
            outputs=by_component_outputs.get(ref, []),
            event_time=event_time,
        ))
    return events


def write_events(events: list[dict[str, Any]], output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for ev in events:
        name = ev["job"]["name"].replace("/", "-").replace(":", "-")
        path = output_dir / f"{name}.openlineage.json"
        path.write_text(json.dumps(ev, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--single-file", type=Path, default=None,
                        help="If set, write all events as a single JSON array to this file "
                             "(useful for bulk-posting to a Marquez collector).")
    args = parser.parse_args()
    if not args.catalog.exists():
        raise SystemExit(f"catalog not found at {args.catalog}")
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    events = build_events(catalog)
    if not events:
        print("no Components with reads/writes-Resource edges; nothing to emit.")
        return 0
    if args.single_file:
        args.single_file.parent.mkdir(parents=True, exist_ok=True)
        args.single_file.write_text(json.dumps(events, indent=2, sort_keys=True) + "\n",
                                    encoding="utf-8")
        print(f"wrote {len(events)} events to {args.single_file}")
    else:
        written = write_events(events, args.output_dir)
        print(f"wrote {len(written)} OpenLineage event(s):")
        for p in written[:10]:
            print(f"  {p}")
        if len(written) > 10:
            print(f"  ... and {len(written) - 10} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
