"""API-spec mini-extractor.

OpenAPI (Swagger), Protocol Buffers, and GraphQL schemas all declare
service surfaces in a language-agnostic interchange format. Parsing
them yields API entities directly, no source-code traversal required.

Output:

  - OpenAPI yaml/json with `openapi:` or `swagger:` top-level
    → API entity with type=openapi, operations[] from `paths`.
  - `*.proto` files with `service Foo { rpc Bar(...) returns (...); }`
    → API entity with type=grpc, operations[] from each rpc.
  - `*.graphql` / `*.gql` schema with `type Query { ... }`
    / `type Mutation { ... }`
    → API entity with type=graphql, operations[] from field declarations.

Catalog entities are emitted with file:line evidence pointing at the
spec source. The LLM enrichment downstream adds tagline / domain.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from static_extractors.mini import Fact


# --------------------------------------------------------------------------- #
# OpenAPI
# --------------------------------------------------------------------------- #


def is_openapi(path: Path) -> bool:
    if path.suffix.lower() not in {".yaml", ".yml", ".json"}:
        return False
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:512]
    except OSError:
        return False
    return ("openapi:" in head) or ('"openapi":' in head) or ("swagger:" in head) or ('"swagger":' in head)


def _load(text: str, ext: str) -> dict | None:
    try:
        if ext == ".json":
            data = json.loads(text)
        else:
            data = yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _extract_openapi(path: Path, repo_root: Path) -> list[Fact]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        rel_path = str(path.resolve().relative_to(repo_root.resolve()))
    except (OSError, ValueError):
        return []
    data = _load(text, path.suffix.lower())
    if not data:
        return []
    info = data.get("info") or {}
    title = (info.get("title") or path.stem).strip() if isinstance(info, dict) else path.stem
    paths_section = data.get("paths") or {}
    operations: list[dict[str, Any]] = []
    if isinstance(paths_section, dict):
        for url_path, methods_obj in paths_section.items():
            if not isinstance(methods_obj, dict):
                continue
            for method, op in methods_obj.items():
                if method.lower() not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                    continue
                op_name = (op.get("operationId") or op.get("summary") or f"{method.upper()} {url_path}") if isinstance(op, dict) else f"{method.upper()} {url_path}"
                operations.append({
                    "name": op_name,
                    "method": method.upper(),
                    "path": url_path,
                })
    return [
        Fact(
            category="apis",
            rule="api_specs.openapi",
            body={
                "name": title,
                "type": "openapi",
                "exposed_by": "",
                "operations": operations,
                "notes": f"OpenAPI spec at {rel_path}.",
                "evidence": [{"path": rel_path, "line": 1, "snippet": text.splitlines()[0][:200] if text else ""}],
            },
        )
    ]


# --------------------------------------------------------------------------- #
# Protocol Buffers
# --------------------------------------------------------------------------- #


_PROTO_SERVICE_RE = re.compile(r"^\s*service\s+(\w+)\s*\{", re.MULTILINE)
_PROTO_RPC_RE = re.compile(r"^\s*rpc\s+(\w+)\s*\(\s*(\w+)\s*\)\s*returns\s*\(\s*(\w+)\s*\)\s*;?", re.MULTILINE)


def is_proto(path: Path) -> bool:
    return path.suffix.lower() == ".proto"


def _extract_proto(path: Path, repo_root: Path) -> list[Fact]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        rel_path = str(path.resolve().relative_to(repo_root.resolve()))
    except (OSError, ValueError):
        return []
    facts: list[Fact] = []
    services = list(_PROTO_SERVICE_RE.finditer(text))
    if not services:
        return []
    for i, svc in enumerate(services):
        svc_name = svc.group(1)
        svc_line = text.count("\n", 0, svc.start()) + 1
        # Body delimited by `{` … `}`. We don't fully balance braces; in
        # practice each service block contains only rpc declarations.
        end = services[i + 1].start() if i + 1 < len(services) else len(text)
        body = text[svc.end():end]
        operations: list[dict[str, Any]] = []
        for rpc in _PROTO_RPC_RE.finditer(body):
            operations.append({
                "name": rpc.group(1),
                "method": "RPC",
                "path": f"/{svc_name}/{rpc.group(1)}",
            })
        facts.append(
            Fact(
                category="apis",
                rule="api_specs.proto",
                body={
                    "name": svc_name,
                    "type": "grpc",
                    "exposed_by": "",
                    "operations": operations,
                    "notes": f"gRPC service from {rel_path}.",
                    "evidence": [{"path": rel_path, "line": svc_line, "snippet": f"service {svc_name} {{ ... }}"}],
                },
            )
        )
    return facts


# --------------------------------------------------------------------------- #
# GraphQL
# --------------------------------------------------------------------------- #


_GQL_TYPE_RE = re.compile(r"^\s*type\s+(Query|Mutation|Subscription)\s*\{", re.MULTILINE)
_GQL_FIELD_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?:\([^)]*\))?\s*:\s*[\w\[\]!]+", re.MULTILINE)


def is_graphql(path: Path) -> bool:
    return path.suffix.lower() in {".graphql", ".gql"}


def _extract_graphql(path: Path, repo_root: Path) -> list[Fact]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        rel_path = str(path.resolve().relative_to(repo_root.resolve()))
    except (OSError, ValueError):
        return []
    blocks = list(_GQL_TYPE_RE.finditer(text))
    if not blocks:
        return []
    operations: list[dict[str, Any]] = []
    for i, m in enumerate(blocks):
        op_type = m.group(1)  # Query / Mutation / Subscription
        end = blocks[i + 1].start() if i + 1 < len(blocks) else len(text)
        body = text[m.end():end]
        # Stop at the closing `}` of the block, not the file end.
        brace_close = body.find("}")
        if brace_close >= 0:
            body = body[:brace_close]
        for f in _GQL_FIELD_RE.finditer(body):
            field_name = f.group(1)
            operations.append({
                "name": field_name,
                "method": op_type.upper(),
                "path": f"/{op_type.lower()}/{field_name}",
            })
    return [
        Fact(
            category="apis",
            rule="api_specs.graphql",
            body={
                "name": path.stem,
                "type": "graphql",
                "exposed_by": "",
                "operations": operations,
                "notes": f"GraphQL schema at {rel_path}.",
                "evidence": [{"path": rel_path, "line": 1, "snippet": text.splitlines()[0][:200] if text else ""}],
            },
        )
    ]


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #


def extract(path: Path, repo_root: Path) -> list[Fact]:
    if is_proto(path):
        return _extract_proto(path, repo_root)
    if is_graphql(path):
        return _extract_graphql(path, repo_root)
    if is_openapi(path):
        return _extract_openapi(path, repo_root)
    return []
