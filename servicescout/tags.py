"""Tag canonicalisation for catalog facets.

ServiceScout keeps raw tags in the catalog, but the UI needs stable facet
values. Built-in behaviour only removes spelling/casing drift. Workspace-
specific semantic merges come from a generated alias file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping


def normalize_tag(tag: object) -> str:
    """Return a generic display key for a tag.

    This deliberately does not encode a taxonomy. It lowercases and tidies
    separators so `Java`, `java`, and `JAVA` share a facet, while semantic
    choices such as `mssql` -> `sql-server` remain data-driven.
    """
    raw = str(tag or "").strip().lower()
    if not raw:
        return ""
    if ":" in raw:
        prefix, value = raw.split(":", 1)
        prefix = _slug(prefix)
        value = " ".join(value.split()) if prefix == "schedule" else _slug(value)
        return f"{prefix}:{value}" if value else prefix
    return _slug(raw)


def canonical_tag(tag: object, aliases: Mapping[str, str] | None = None) -> str:
    """Return the canonical facet value for ``tag``."""
    normal = normalize_tag(tag)
    if not normal:
        return ""
    aliases = aliases or {}
    exact = aliases.get(str(tag or "").strip())
    if exact:
        return normalize_tag(exact)
    mapped = aliases.get(normal)
    return normalize_tag(mapped) if mapped else normal


def canonical_tags(tags: Iterable[object] | None, aliases: Mapping[str, str] | None = None) -> list[str]:
    return sorted({value for tag in (tags or []) if (value := canonical_tag(tag, aliases))})


def load_tag_aliases(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    raw_aliases = payload.get("aliases") if isinstance(payload, dict) else None
    if not isinstance(raw_aliases, dict):
        return {}
    aliases: dict[str, str] = {}
    for raw, canonical in raw_aliases.items():
        canonical_value = normalize_tag(canonical)
        if not canonical_value:
            continue
        raw_text = str(raw or "").strip()
        raw_normal = normalize_tag(raw_text)
        if raw_text:
            aliases[raw_text] = canonical_value
        if raw_normal:
            aliases[raw_normal] = canonical_value
    return aliases


def _slug(value: str) -> str:
    value = re.sub(r"[._/\s]+", "-", value)
    value = re.sub(r"[^a-z0-9:+-]+", "-", value)
    return re.sub(r"-+", "-", value).strip("-")
