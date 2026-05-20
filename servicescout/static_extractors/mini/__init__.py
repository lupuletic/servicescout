"""Mini-extractors — language-agnostic by construction.

Each module here parses an *interchange format* (Dockerfile, Kubernetes
YAML, Helm values, build manifest, OpenAPI / gRPC / GraphQL spec) and
emits Backstage-shaped facts with file:line evidence. These work
identically across any source-code language because they read
configuration files, not code.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Fact:
    """One emission from a mini-extractor.

    `category` is a catalog_schema.json top-level key:
      "components" | "apis" | "resources" | "dependencies" |
      "providers" | "domain_attributes" | "glossary"

    `body` is the Backstage-shaped object for that category, already
    populated with evidence.
    """
    category: str
    body: dict[str, Any]
    rule: str = ""
