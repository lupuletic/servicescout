"""Dockerfile mini-extractor.

A Dockerfile is the most universal "container intent" signal: language-
agnostic, present in essentially every deployable service, and
extractable with a regex pass (no Dockerfile-specific parser needed).

Facts emitted:

  - Provider hint for the base image registry (FROM docker.io/library/...
    → Provider:DockerHub; FROM gcr.io/... → Provider:GCR; etc.)
  - Component runtime hint (FROM openjdk → java; FROM node → node; etc.)
  - Resource hint when ENV / ARG references suggest a managed dep
    (DB URL, broker URL, S3 bucket name pattern).

All facts carry the Dockerfile path:line evidence. The LLM and the
correction loop downstream decide what to keep.
"""

from __future__ import annotations

import re
from pathlib import Path

from servicescout.static_extractors.mini import Fact


# Generic Dockerfile lexicon. Case-insensitive directive names; comments
# stripped before parse.
_DIRECTIVE_RE = re.compile(r"^\s*(?P<name>[A-Za-z]+)\s+(?P<value>.+?)\s*$")


def _strip_comment(line: str) -> str:
    # `#` starts a comment unless inside a quoted string — Dockerfiles
    # don't have such strings at the directive level.
    h = line.find("#")
    return line[:h] if h >= 0 else line


def _base_image_provider(image_ref: str) -> tuple[str | None, str | None]:
    """Return (provider_name, image_short) for a `FROM image:tag` arg.

    Examples:
      openjdk:11           → (None, "openjdk")          (DockerHub library)
      python:3.12-slim     → (None, "python")
      gcr.io/foo/bar       → ("GCR", "gcr.io/foo/bar")
      quay.io/foo/bar      → ("Quay", "quay.io/foo/bar")
      ghcr.io/foo/bar      → ("GitHub Container Registry", "ghcr.io/foo/bar")
      mcr.microsoft.com/.. → ("Microsoft Container Registry", ...)
      <account>.dkr.ecr.<region>.amazonaws.com/foo
                           → ("Amazon ECR", ...)
    """
    bare = image_ref.split("@", 1)[0]
    short = bare.split(":", 1)[0]
    if short.startswith("gcr.io/") or short.startswith("us.gcr.io/") or short.startswith("eu.gcr.io/") or short.startswith("asia.gcr.io/"):
        return "GCR", short
    if short.startswith("quay.io/"):
        return "Quay", short
    if short.startswith("ghcr.io/"):
        return "GitHub Container Registry", short
    if short.startswith("mcr.microsoft.com/"):
        return "Microsoft Container Registry", short
    if ".dkr.ecr." in short and ".amazonaws.com/" in short:
        return "Amazon ECR", short
    # Default: Docker Hub library or org image
    return None, short


# Coarse runtime classifier from base image. Deliberately not exhaustive.
# When in doubt, return empty — LLM enrichment fills the gap.
_RUNTIME_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("openjdk", "java"),
    ("eclipse-temurin", "java"),
    ("amazoncorretto", "java"),
    ("zulu-openjdk", "java"),
    ("maven", "java"),
    ("gradle", "java"),
    ("python", "python"),
    ("python-slim", "python"),
    ("node", "node"),
    ("nodejs", "node"),
    ("nginx", "nginx"),
    ("golang", "go"),
    ("rust", "rust"),
    ("ruby", "ruby"),
    ("dotnet", "dotnet"),
    ("mcr.microsoft.com/dotnet", "dotnet"),
    ("php", "php"),
    ("alpine", ""),  # too generic — leave empty
    ("debian", ""),
    ("ubuntu", ""),
    ("scratch", ""),
)


def _runtime_hint(image_short: str) -> str:
    lower = image_short.lower()
    for prefix, runtime in _RUNTIME_BY_PREFIX:
        if lower == prefix or lower.startswith(prefix + ":") or lower.startswith(prefix + "/"):
            return runtime
    # Fallback: tokenize on common separators and look for known runtime
    # tokens. Catches custom registry images like `weaveworksdemos/msd-java`
    # or `mycorp/internal-node-runtime`.
    tokens = set()
    for chunk in lower.replace("/", "-").replace(":", "-").split("-"):
        if chunk:
            tokens.add(chunk)
    for tok in ("java", "jdk", "jre", "openjdk"):
        if tok in tokens:
            return "java"
    for tok in ("node", "nodejs"):
        if tok in tokens:
            return "node"
    for tok in ("python", "py"):
        if tok in tokens:
            return "python"
    for tok in ("golang", "go"):
        if tok in tokens:
            return "go"
    for tok in ("ruby",):
        if tok in tokens:
            return "ruby"
    for tok in ("dotnet", "aspnet"):
        if tok in tokens:
            return "dotnet"
    return ""


def extract(path: Path, repo_root: Path) -> list[Fact]:
    """Parse a Dockerfile. `path` must point at a Dockerfile-shaped file
    (Dockerfile, *.Dockerfile, Containerfile). `repo_root` is used to
    compute the relative evidence path.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    try:
        rel_path = str(path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return []

    facts: list[Fact] = []
    providers_seen: dict[str, dict] = {}

    for line_idx, raw_line in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw_line).strip()
        if not line:
            continue
        m = _DIRECTIVE_RE.match(line)
        if not m:
            continue
        name = m.group("name").upper()
        value = m.group("value").strip()

        if name == "FROM":
            # Strip optional `AS stage`
            image_ref = value.split(" AS ", 1)[0].split(" as ", 1)[0].strip()
            provider, image_short = _base_image_provider(image_ref)
            runtime = _runtime_hint(image_short)
            if provider and provider not in providers_seen:
                providers_seen[provider] = {
                    "name": provider,
                    "category": "cloud",
                    "description": f"Container image registry hosting {image_short}.",
                    "aliases": [image_short.split("/", 1)[0]] if "/" in image_short else [],
                    "evidence": [
                        {"path": rel_path, "line": line_idx, "snippet": raw_line.strip()[:200]}
                    ],
                }
            if runtime:
                # Emit a Component runtime hint. The LLM normally produces
                # the Component too — the merge step will reconcile.
                facts.append(
                    Fact(
                        category="components",
                        rule=f"dockerfile.FROM→runtime:{runtime}",
                        body={
                            "name": repo_root.name,
                            "type": "service",
                            "runtime": "long-running",
                            "lifecycle": "",
                            "subcomponent_of": "",
                            "notes": f"Runtime inferred from Dockerfile base image: {image_short}.",
                            "tags": [runtime] if runtime else [],
                            "environments": [],
                            "evidence": [
                                {"path": rel_path, "line": line_idx, "snippet": raw_line.strip()[:200]}
                            ],
                        },
                    )
                )

        elif name == "EXPOSE":
            # Component-listening port hint. Schema doesn't have a dedicated
            # ports field; surface as a tag for now ("port:8080").
            ports = [p.strip().split("/", 1)[0] for p in value.split() if p.strip()]
            if ports:
                facts.append(
                    Fact(
                        category="components",
                        rule="dockerfile.EXPOSE",
                        body={
                            "name": repo_root.name,
                            "type": "service",
                            "runtime": "long-running",
                            "lifecycle": "",
                            "subcomponent_of": "",
                            "notes": f"Listens on {', '.join(ports)} per Dockerfile EXPOSE.",
                            "tags": [f"port:{p}" for p in ports],
                            "environments": [],
                            "evidence": [
                                {"path": rel_path, "line": line_idx, "snippet": raw_line.strip()[:200]}
                            ],
                        },
                    )
                )

    for body in providers_seen.values():
        facts.append(Fact(category="providers", body=body, rule="dockerfile.FROM→provider"))

    return facts


def is_dockerfile(path: Path) -> bool:
    name = path.name
    if name == "Dockerfile" or name == "Containerfile":
        return True
    return name.lower().endswith(".dockerfile")
