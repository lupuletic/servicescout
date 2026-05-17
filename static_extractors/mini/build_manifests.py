"""Build-manifest mini-extractor.

Reads package.json / pom.xml / go.mod / requirements.txt / pyproject.toml /
Cargo.toml / Gemfile / Pipfile and emits:

  - Provider hints for *significant* third-party SDKs (Stripe, Twilio,
    Datadog, AWS SDKs, etc.) — NOT every transitive dep.
  - Resource hints for known client libraries (PostgreSQL driver →
    Resource hint for type=database, technology=PostgreSQL).
  - Component runtime / framework tags (spring-boot, fastapi, express,
    etc.).

Format-specific parsers, but the *output* shape is the same.

Why not emit every dep as a `dependsOn` edge? Backstage's dependency
edges are for service-to-service runtime topology; cataloguing every
transitive library would explode the graph and obscure the topology
agents need. We surface a curated set of indicators instead — the
ones that name a *runtime concern* rather than a build-time helper.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from static_extractors.mini import Fact


# Each entry: (substring_match, provider_name, category, description)
# The match is on the *package coordinate string* — so it works for
# every package manager regardless of namespacing.
_PROVIDER_HINTS: tuple[tuple[str, str, str, str], ...] = (
    # Cloud
    ("aws-sdk", "AWS", "cloud", "AWS SDK dependency."),
    ("amazonaws", "AWS", "cloud", "AWS SDK dependency."),
    ("software.amazon.awssdk", "AWS", "cloud", "AWS SDK v2 dependency."),
    ("google-cloud", "GCP", "cloud", "Google Cloud SDK dependency."),
    ("@google-cloud", "GCP", "cloud", "Google Cloud SDK dependency."),
    ("azure-", "Azure", "cloud", "Azure SDK dependency."),
    ("@azure/", "Azure", "cloud", "Azure SDK dependency."),
    # Payments
    ("stripe", "Stripe", "payment", "Stripe SDK dependency."),
    ("braintree", "Braintree", "payment", "Braintree SDK dependency."),
    ("adyen", "Adyen", "payment", "Adyen SDK dependency."),
    ("paypal", "PayPal", "payment", "PayPal SDK dependency."),
    # Communications
    ("twilio", "Twilio", "communications", "Twilio SDK dependency."),
    ("sendgrid", "SendGrid", "communications", "SendGrid SDK dependency."),
    ("mailchimp", "Mailchimp", "communications", "Mailchimp SDK dependency."),
    # Identity
    ("auth0", "Auth0", "identity", "Auth0 SDK dependency."),
    ("okta", "Okta", "identity", "Okta SDK dependency."),
    # Observability
    ("datadog", "Datadog", "observability", "Datadog SDK / agent dependency."),
    ("newrelic", "New Relic", "observability", "New Relic SDK dependency."),
    ("sentry", "Sentry", "observability", "Sentry SDK dependency."),
    ("honeycomb", "Honeycomb", "observability", "Honeycomb SDK dependency."),
    ("@sentry/", "Sentry", "observability", "Sentry SDK dependency."),
    ("opentelemetry", "OpenTelemetry", "observability", "OpenTelemetry SDK dependency."),
    ("zipkin", "Zipkin", "observability", "Zipkin tracer dependency."),
    # Search / analytics
    ("elasticsearch", "Elasticsearch", "search", "Elasticsearch client dependency."),
    ("opensearch", "OpenSearch", "search", "OpenSearch client dependency."),
    ("algolia", "Algolia", "search", "Algolia client dependency."),
    # Feature flags
    ("launchdarkly", "LaunchDarkly", "other", "LaunchDarkly SDK dependency."),
    ("optimizely", "Optimizely", "other", "Optimizely SDK dependency."),
    # AI/ML
    ("openai", "OpenAI", "ai-ml", "OpenAI SDK dependency."),
    ("anthropic", "Anthropic", "ai-ml", "Anthropic SDK dependency."),
    ("cohere", "Cohere", "ai-ml", "Cohere SDK dependency."),
)

# Resource hints — DB / cache / queue clients → Resource entity hint.
# Tuples: (match, technology, type)
_RESOURCE_HINTS: tuple[tuple[str, str, str], ...] = (
    ("postgresql", "PostgreSQL", "database"),
    ("postgres", "PostgreSQL", "database"),
    ("pg", "PostgreSQL", "database"),
    ("psycopg", "PostgreSQL", "database"),
    ("mysql", "MySQL", "database"),
    ("mariadb", "MariaDB", "database"),
    ("mongodb", "MongoDB", "database"),
    ("mongoose", "MongoDB", "database"),
    ("pymongo", "MongoDB", "database"),
    ("couchbase", "Couchbase", "database"),
    ("cassandra", "Cassandra", "database"),
    ("dynamodb", "DynamoDB", "database"),
    ("redis", "Redis", "cache"),
    ("ioredis", "Redis", "cache"),
    ("memcached", "Memcached", "cache"),
    ("rabbitmq", "RabbitMQ", "queue"),
    ("amqp", "RabbitMQ", "queue"),
    ("pika", "RabbitMQ", "queue"),
    ("amqplib", "RabbitMQ", "queue"),
    ("kafka", "Kafka", "topic"),
    ("kafkajs", "Kafka", "topic"),
    ("confluent-kafka", "Kafka", "topic"),
    ("sarama", "Kafka", "topic"),
    ("nats", "NATS", "topic"),
    ("activemq", "ActiveMQ", "queue"),
    ("artemis", "ActiveMQ Artemis", "queue"),
)

# Runtime / framework tags. (match, runtime_name_or_tag)
_FRAMEWORK_HINTS: tuple[tuple[str, str], ...] = (
    ("spring-boot", "spring-boot"),
    ("springframework", "spring"),
    ("express", "express"),
    ("fastapi", "fastapi"),
    ("flask", "flask"),
    ("django", "django"),
    ("nestjs", "nestjs"),
    ("@nestjs/", "nestjs"),
    ("nextjs", "nextjs"),
    ("next", "nextjs"),
    ("gin-gonic", "gin"),
    ("gorilla/mux", "gorilla"),
    ("aspnet", "aspnet"),
    ("rails", "rails"),
    ("sinatra", "sinatra"),
    ("ktor", "ktor"),
    ("axum", "axum"),
    ("actix", "actix"),
)


def _coord_matches(coord_lc: str, needle: str) -> bool:
    """Match a curated needle against a package coordinate.

    Short bare names (no separators) need to match against the WHOLE
    coord or a name boundary, so the 2-char `pg` package gets a hit while
    `stripeg-foo` doesn't. Multi-char namespaced needles (`@sentry/`,
    `aws-sdk`, `software.amazon.awssdk`) use simple substring search
    because their separators give enough context to avoid collisions.
    """
    if any(sep in needle for sep in ("/", "@", ".", "-")):
        return needle in coord_lc
    # Bare name: match if it's the whole coord, or appears at a separator.
    if coord_lc == needle:
        return True
    if any(coord_lc.startswith(needle + sep) for sep in ("-", "/", ":", "_")):
        return True
    if any((sep + needle) in coord_lc for sep in ("/", ":")):
        return True
    return False


def _emit_for_dep(rel_path: str, line: int, snippet: str, dep_coord: str) -> Iterable[Fact]:
    """Yield Fact records for one dependency coordinate."""
    coord_lc = dep_coord.lower()
    # Providers
    seen_provider: set[str] = set()
    for needle, provider, category, descr in _PROVIDER_HINTS:
        if _coord_matches(coord_lc, needle) and provider not in seen_provider:
            seen_provider.add(provider)
            yield Fact(
                category="providers",
                rule="build_manifest.provider_hint",
                body={
                    "name": provider,
                    "category": category,
                    "description": descr,
                    "aliases": [dep_coord],
                    "evidence": [{"path": rel_path, "line": line, "snippet": snippet[:200]}],
                },
            )
    # Resources
    seen_resource: set[str] = set()
    for needle, tech, rtype in _RESOURCE_HINTS:
        if _coord_matches(coord_lc, needle) and tech not in seen_resource:
            seen_resource.add(tech)
            yield Fact(
                category="resources",
                rule="build_manifest.resource_hint",
                body={
                    "name": tech.lower(),
                    "type": rtype,
                    "technology": tech,
                    "host_or_instance": "",
                    "database_or_schema": "",
                    "tables_or_collections": [],
                    "access": "read-write" if rtype in {"database", "cache"} else "publish-consume",
                    "env_or_config_keys": [],
                    "used_by": "",
                    "messaging_pattern": "broker-queue" if rtype == "queue"
                        else ("broker-topic" if rtype == "topic" else ""),
                    "subscribes_to": "",
                    "datasource_url": "",
                    "confidence": "medium",
                    "notes": f"{tech} client library is present in build manifest.",
                    "evidence": [{"path": rel_path, "line": line, "snippet": snippet[:200]}],
                },
            )


def _coords_from_package_json(text: str) -> list[tuple[int, str, str]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    out: list[tuple[int, str, str]] = []
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        deps = data.get(section) or {}
        if not isinstance(deps, dict):
            continue
        for name in deps:
            # We don't know the line number from json.loads alone, so cite
            # line 1 (the file). Good enough for evidence; the LLM can refine.
            out.append((1, name, f'"{name}": "{deps[name]}"'))
    return out


_POM_DEP_RE = re.compile(
    r"<dependency>\s*<groupId>([^<]+)</groupId>\s*<artifactId>([^<]+)</artifactId>",
    re.DOTALL,
)


def _coords_from_pom_xml(text: str) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    # Cheap: enumerate matches and find the line number by counting
    # newlines up to the match position.
    for m in _POM_DEP_RE.finditer(text):
        group_id = m.group(1).strip()
        artifact = m.group(2).strip()
        line = text.count("\n", 0, m.start()) + 1
        snippet = f"<dependency><groupId>{group_id}</groupId><artifactId>{artifact}</artifactId>...</dependency>"
        out.append((line, f"{group_id}:{artifact}", snippet))
    return out


_GOMOD_REQUIRE_RE = re.compile(r"^\s*([\w./\-]+)\s+v?\d", re.MULTILINE)


def _coords_from_go_mod(text: str) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    inside_block = False
    for idx, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("require (") or stripped == "require (":
            inside_block = True
            continue
        if inside_block and stripped == ")":
            inside_block = False
            continue
        target = stripped if inside_block else (stripped[len("require "):] if stripped.startswith("require ") else "")
        if not target:
            continue
        # Module path is the first whitespace-separated token.
        parts = target.split()
        if not parts:
            continue
        module = parts[0]
        if "/" not in module:
            continue
        out.append((idx, module, stripped))
    return out


def _coords_from_requirements_txt(text: str) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            continue
        # name[extras]==version style. Take name only.
        name = re.split(r"[<>=!~\s\[]", stripped, 1)[0].strip()
        if name:
            out.append((idx, name, stripped))
    return out


def _coords_from_pyproject(text: str) -> list[tuple[int, str, str]]:
    # Tiny TOML scrape — just look for lines under [tool.poetry.dependencies]
    # / [project] dependencies = [ "name>=..." ]. We don't pull in the toml
    # parser because pyproject.toml shapes vary widely; regex catches both
    # common forms.
    out: list[tuple[int, str, str]] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("["):
            continue
        m = re.match(r'^"?([A-Za-z0-9_\-.]+)"?\s*[=:]\s*["\^~>=<].*$', stripped)
        if m:
            out.append((idx, m.group(1), stripped))
            continue
        m2 = re.match(r'^"([A-Za-z0-9_\-.]+)(\[.*\])?[=<>!~\s].*"\s*,?\s*$', stripped)
        if m2:
            out.append((idx, m2.group(1), stripped))
    return out


_GEMFILE_RE = re.compile(r"^\s*gem\s+['\"]([A-Za-z0-9_\-]+)['\"]")


def _coords_from_gemfile(text: str) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        m = _GEMFILE_RE.match(line)
        if m:
            out.append((idx, m.group(1), line.strip()))
    return out


_CARGO_DEPS_RE = re.compile(r'^\s*\[(?:dependencies|dev-dependencies|build-dependencies)\]', re.MULTILINE)


def _coords_from_cargo(text: str) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    # Scan after each [dependencies] header until the next [section] line.
    sections = list(_CARGO_DEPS_RE.finditer(text))
    if not sections:
        return out
    for i, m in enumerate(sections):
        section_start = text.count("\n", 0, m.start()) + 1
        next_section = sections[i + 1].start() if i + 1 < len(sections) else len(text)
        block = text[m.end():next_section]
        for line_offset, raw in enumerate(block.splitlines(), start=1):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("["):
                continue
            name_match = re.match(r'^([A-Za-z0-9_\-]+)\s*=', stripped)
            if name_match:
                out.append((section_start + line_offset, name_match.group(1), stripped))
    return out


_DISPATCH = {
    "package.json": _coords_from_package_json,
    "pom.xml": _coords_from_pom_xml,
    "go.mod": _coords_from_go_mod,
    "requirements.txt": _coords_from_requirements_txt,
    "pyproject.toml": _coords_from_pyproject,
    "Gemfile": _coords_from_gemfile,
    "Cargo.toml": _coords_from_cargo,
}


def is_build_manifest(path: Path) -> bool:
    return path.name in _DISPATCH


def extract(path: Path, repo_root: Path) -> list[Fact]:
    parser = _DISPATCH.get(path.name)
    if parser is None:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    try:
        rel_path = str(path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return []
    coords = parser(text)
    facts: list[Fact] = []
    seen_provider_names: set[str] = set()
    seen_resource_names: set[str] = set()
    framework_tags: list[tuple[str, int, str, str]] = []  # (tag, line, coord, snippet)
    for line, coord, snippet in coords:
        for fact in _emit_for_dep(rel_path, line, snippet, coord):
            # Dedupe: each provider/resource emitted once per manifest, with
            # all matching coords listed under aliases / evidence merged.
            name = fact.body.get("name")
            if fact.category == "providers" and name in seen_provider_names:
                continue
            if fact.category == "resources" and name in seen_resource_names:
                continue
            if fact.category == "providers":
                seen_provider_names.add(name)
            else:
                seen_resource_names.add(name)
            facts.append(fact)
        coord_lc = coord.lower()
        for needle, tag in _FRAMEWORK_HINTS:
            if needle in coord_lc:
                framework_tags.append((tag, line, coord, snippet))
    if framework_tags:
        # One synthetic Component update with framework tags. Real
        # Component name comes from elsewhere (Dockerfile / k8s / LLM);
        # the merge step assigns to the right component.
        tags = sorted({t[0] for t in framework_tags})
        first_line = framework_tags[0][1]
        first_snippet = framework_tags[0][3]
        facts.append(
            Fact(
                category="components",
                rule="build_manifest.framework_hint",
                body={
                    "name": repo_root.name,
                    "type": "service",
                    "runtime": "long-running",
                    "lifecycle": "",
                    "subcomponent_of": "",
                    "notes": f"Framework(s) inferred from build manifest: {', '.join(tags)}.",
                    "tags": tags,
                    "environments": [],
                    "evidence": [{"path": rel_path, "line": first_line, "snippet": first_snippet[:200]}],
                },
            )
        )
    return facts
