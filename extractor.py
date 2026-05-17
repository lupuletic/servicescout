"""LLM-driven catalog extraction for one repo.

Runs Codex or Claude Code in non-interactive mode against a repo and emits a
Backstage-aligned JSON document (one Component graph per repo) into
data/catalog/<repo>.json. The output is validated against catalog_schema.json
before being written; invalid output is quarantined.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from repo_discovery import find_repos, load_workspace_config
from static_extractors import ast_crosscheck, calibrate, correction, snippet_verify


HERE = Path(__file__).parent
CATALOG_SCHEMA_PATH = HERE / "catalog_schema.json"
DEFAULT_OUTPUT_DIR = HERE / "data" / "catalog"
DEFAULT_QUARANTINE_DIR = HERE / "data" / "catalog_quarantine"
DEFAULT_CATALOG = HERE / "data" / "catalog.json"
RUNS_LEDGER = HERE / "data" / "extraction_runs.jsonl"
GLOSSARY_MAX_ENTRIES = 80


PROMPT_TEMPLATE = """You are extracting an architecture catalog from one source repository, in a shape
compatible with Spotify Backstage's software catalog (Components, APIs, Resources, Dependencies).
The output is consumed by coding agents that work across many repositories in an organization.
This prompt is intentionally domain-agnostic — apply it to any kind of service or codebase.

Repository: {repo_id}
Local path: {repo_path}

Return JSON conforming to the supplied schema (Backstage-aligned). Every fact must include file/line
evidence. Do not optimize for a specific ticket — extract reusable repo-level facts that help many
future prompts.

The catalog is used by coding agents to answer prompts like:
- which repos are relevant to this feature or change?
- what does this repo do, what does it expose, what does it call?
- which databases / queues / topics does it read/write/publish/consume?
- what downstream repos should I clone next?
- which repo branches on a given enum value or config flag (e.g. `productType=evoucher`)?
- what does a given business term mean inside this codebase?

INVESTIGATION (phased, do NOT read the whole repo):

Phase A - orient: identify purpose, language, runtime, deployable components from README,
build files (pom.xml, build.gradle, package.json, pyproject.toml), component-index.yaml,
altitude.yaml, helm/k8s manifests, Dockerfile, and the source tree shape. Decide which files
are likely authoritative for runtime facts.

Phase B - discover: use targeted `rg` searches with caps (`-m 20`, `head -n`, `sed -n '1,200p'`)
or Grep tool. Useful search terms:
  HTTP/REST:    RestTemplate, WebClient, FeignClient, HttpClient, requests, axios, fetch, httpx,
                baseUrl, baseURL, RestClient, @FeignClient, OpenFeign
  GraphQL:      graphql, GraphQLClient, useQuery, useMutation, gql`, ApolloClient, urql
  Messaging:    publish, subscribe, producer, consumer, broker, queue, topic, stream,
                exchange, routingKey, consumerGroup, deadLetter, dlq, outbox, inbox,
                @KafkaListener, KafkaTemplate, kafka, kinesis, EventHub, EventBridge,
                SqsClient, SnsClient, sqs, sns, PubSubTemplate, @PubSubListener,
                google.pubsub, ServiceBus, EventGrid, RabbitTemplate, RabbitMQ, amqp,
                JmsTemplate, JmsListener, @JmsListener, ActiveMQ, activemq, amq, Camel,
                CamelContext, from(\"activemq:\"), from(\"jms:\"), VirtualTopic, Consumer.,
                nats, RedisStream, XREAD, XADD, webhook
  Datastores:   datasource, jdbc, r2dbc, JdbcTemplate, JpaRepository, CrudRepository, @Entity,
                @Table, @Document, MongoTemplate, MongoRepository, CouchbaseTemplate, RedisTemplate,
                ElasticsearchOperations, opensearch, ElasticsearchClient, RestHighLevelClient,
                flyway, liquibase, db.changelog, V*__*.sql, CREATE TABLE, ALTER TABLE,
                postgres, postgresql, mysql, mariadb, oracle, sqlserver, mongo, mongodb,
                redis, lettuce, jedis, couchbase, bigquery, spanner, dynamodb, gcs, s3
  Auth:         oauth, jwt, keycloak, opaque, cookie, session
  Config:       application.properties, application.yml, application-*.yml, k8s/env/*.yaml,
                altitude.yaml, component-index.yaml, helm values, .env, environment variables

Phase B.5 - mine domain attributes and glossary (HIGH VALUE for cross-repo routing):

  B.5a — domain_attributes: every identifier the code uses to make a runtime decision
  is a discriminator. A decision means the value of this identifier changes which
  code path runs (branching), which downstream is called, which message is published,
  which field is written. Language and framework do not matter — the property is
  semantic, not syntactic. For each discriminator capture:
    * `attribute` — the name as it appears in code.
    * `values[]` — every distinct value the code branches on. Include values that
      appear only once.
    * `meaning` — one line, grounded in the code, describing when each value is set
      and what the service does for each.
    * `evidence[]` — at least one file:line for the branching site.
  Skip identifiers whose value never changes behaviour (pure structural fields like
  id/version/timestamp, pure transport metadata) unless the service explicitly
  branches on them.

  B.5b — glossary: 10-20 terms a new engineer would need defined to understand THIS
  service. Prefer terms specific to the codebase over generic tech terms (HTTP, REST,
  JSON, microservice, container, etc. are not glossary material). For each:
    * `term` — the literal token as it appears in code or docs.
    * `definition` — one line, grounded in this repo's code.
    * `synonyms[]` — equivalent names used in code or messaging (config keys, host
      labels, marketing labels, generated client class names).
    * `evidence[]` — at least one file:line.

  B.5c — COMPLETENESS SELF-TEST. Before you emit the JSON, run this self-test. Do
  not skip it. The self-test defines completeness by OUTCOME — not by which files
  you read or which patterns you grep. The catalog you are producing feeds a hybrid
  BM25 + embedding retriever; "complete" means the retriever can surface this repo
  for any reasonable question that mentions a term the code actually branches on.

    1. ROUTING TEST. For each cluster of behaviour you found, ask:
       "If a future user asked a question that mentions a product code, status name,
       customer segment, integration partner, fulfilment type, feature flag, route
       segment, message type, column value, or any other business term that this
       repo *actually branches on* in its source code, would my `tagline`,
       `glossary`, `domain_attributes`, or `capability_sheet` contain enough lexical
       AND semantic signal for the retriever to surface this repo?" If no, the
       extraction is incomplete.

    2. PRESUPPOSED-OMISSION SWEEP. Assume you missed at least one discriminator.
       Re-scan the repo: anywhere the code makes a decision based on the value of a
       string, enum, identifier, header, flag, configuration key, feature name,
       route segment, message type, or column value is a discriminator. The literal
       string values matter — they are what a future query will mention. List every
       such value you find, then confirm each appears verbatim (or as an obvious
       synonym) somewhere in your output. If you find values not yet captured, add
       them to `glossary` and/or `domain_attributes`.

    3. ADVERSARIAL READ. Adopt the role of a skeptical reviewer who has NOT read
       the code. Read only your output. Name three plausible user questions this
       repo should be the right answer to. For each, check the terms in the
       question against your output. If any term in those questions is not
       anywhere in your output, fix the gap before emitting.

  You are done with B.5 only when steps 1–3 all pass on the same draft. The model
  must converge on its own; do not stop early just because you have hit the entry
  caps below.

Phase C - reconcile: a config key, hostname, URL path, generated client class, and human label
may all name the same dependency. Pick one clear `target` (e.g. "Account Service" or
"account-service") and put hostnames/config keys/client classes into `aliases`. Group repeated
tenant configs into one dependency with representative evidence.

Phase D - emit: return JSON conforming to the schema. CAPS: ≤25 components, ≤15 apis,
≤30 resources, ≤40 dependencies, ≤25 providers, ≤30 domain_attributes, ≤25 glossary entries.
Prefer high-confidence runtime facts.

OUTPUT GUIDANCE BY ENTITY:

* repo: one summary with purpose, language, runtime, and 1-4 evidence files (README, main entry).
  Set `system` to the platform/system/product this repo belongs to if obvious; otherwise empty.
  Set `domain` to the higher-level business area; empty string if unsure.
  Set `owner` from CODEOWNERS, component-index/catalog-info.yaml, README, or repo description if
  discoverable; otherwise empty string. Look in `.github/CODEOWNERS`, `CODEOWNERS`,
  `catalog-info.yaml`, `component-index.yaml`, package.json `author`/`maintainers`, pom.xml
  `<developers>`, and the project's main team annotation. Prefer team slugs (e.g.
  `@org/team-payments` or `team-payments`) over individual GitHub handles.
  Set `lifecycle` to one of: production, experimental, deprecated, unknown.

  Also populate:
  * `repo.tagline` — ONE LINE, ≤160 chars. Name the primary domain and one or two
    distinguishing capabilities. Be specific (this is read by routers and rerankers).
    Good: "Spring Boot worker that triggers email/SMS workflows on product events".
    Bad:  "Backend service that handles workflow logic".
  * `repo.capability_sheet` — a ≤600-word markdown summary read by coding agents AFTER
    routing. Sections (h2): Purpose, Public surface (REST/GraphQL/queues), Domain
    entities owned, Important business rules, Persistence, External dependencies.
    Cite file paths inline. This is what a new joiner would read first.

* components[]: deployable units inside this repo. For a simple service, one component named
  after the repo with type=service is fine. For monorepos or repos containing multiple apps,
  list each (worker, cron, frontend, api). For each component:
    * `type` (coarse, Backstage-aligned): one of service, website, mobile-app, worker, cron,
      library, function, consumer, producer, unknown.
    * `runtime` (the shape it actually runs as — Backstage's `spec.type` is too coarse for
      agent reasoning): long-running | cron | lambda | consumer | batch | library | frontend
      | mobile. Use empty string if undiscoverable.
      Mapping hints: a Spring Boot HTTP app → long-running; a Cron `@Scheduled` job → cron;
      a Camel/JMS `@JmsListener` worker → consumer; a one-shot data pipeline → batch.
    * `lifecycle`: one of production | experimental | deprecated, or empty if unknown
      (Backstage rejects "unknown" — leave blank).
    * `subcomponent_of`: only set when this Component is a published library that ships
      AS PART OF another service (e.g. `account-service-client-interface` belongs to
      `account-service`). Name = the parent's canonical name. Empty string otherwise.
    * `environments[]`: deployment environments / regions from k8s manifests, helm values,
      terraform, CI workflows, or env-var defaults. Examples: `["prod"]`, `["prod","staging"]`,
      `["gb1","us1"]`, `["eu-west-2","us-east-1"]`. Empty array if undiscoverable.

* apis[]: APIs this repo EXPOSES (not calls). REST endpoints, GraphQL schemas, gRPC services.
  For REST: include 3-8 representative operations (method+path) — do not list every endpoint.

* resources[]: datastores AND messaging destinations this repo USES.
  Databases — extract one resource per distinct logical datasource (separate buckets/schemas count
    as separate resources). Fill: technology (Postgres, MySQL, Oracle, Couchbase, Mongo, Redis,
    Elasticsearch, BigQuery, Spanner...), host_or_instance (env-template OK), database_or_schema,
    tables_or_collections[] (top tables/collections/buckets from entities, migrations, or
    repository classes — cap at 12), access (read/write/read-write), env_or_config_keys[],
    datasource_url (template form, e.g. `jdbc:postgresql://${{db.host}}:5432/${{db.name}}`).
  Communication endpoints — capture the shared contract/destination explicitly:
    * HTTP/REST/GraphQL/gRPC/SOAP surfaces belong in `apis[]`; callers belong in
      `dependencies[]` with protocol and operation_or_usage.
    * Queue/topic/stream/event-bus destinations belong in `resources[]`, with
      technology set to the concrete transport (Kafka, RabbitMQ, ActiveMQ,
      AWS SQS/SNS/EventBridge, Google Pub/Sub, Azure Service Bus/Event Grid,
      Kinesis, NATS, Redis Streams, etc.). The broker product itself (e.g.
      "RabbitMQ", "Kafka") is the *transport*, not an entity — never list
      it in `providers[]` and never emit a `dependsOn` edge targeting the
      broker. The specific queue/topic/stream IS the entity.
    * For direct queues, set type=queue and access=publish, consume, or
      publish-consume as appropriate.
    * For pub/sub, event streams, and virtual-topic/subscription patterns, set
      type=topic, stream, subscription, virtual-topic, or consumer-queue as
      appropriate; set `subscribes_to` to the shared topic/stream when the
      consumed destination is a subscription/consumer-specific endpoint.
    * For webhook receivers, expose the callback route in `apis[]`; for webhook
      senders, add a dependency with protocol=webhook and the callback/config key.
    * For file/object-storage or database handoffs, model the shared bucket,
      prefix, table, outbox, or inbox as a resource and set read/write access.
    * Always capture env_or_config_keys that name the endpoint, broker, stream,
      subscription, queue, callback URL, bucket/prefix, or integration table.
  Cap at 30. Group near-identical per-tenant destinations into one resource and explain in notes.

* dependencies[]: edges this repo creates to OTHER entities. Cap at 40.
  Use `kind`:
    - dependsOn (general repo-to-repo / service-to-service / repo-to-provider)
    - consumesApi (calls a typed API surface of another service)
    - producesMessage — this repo WRITES messages TO a queue/topic/stream.
      `target` is the queue/topic Resource name; `target_kind=resource`.
      Classify by ROLE, not by which client class is imported. A producer
      invokes a send/publish operation from inside a request handler, a
      scheduled task, an event handler, a CLI command, or a one-off
      startup step. Confirm by finding the actual call site for the
      send/publish/emit/put operation in this repo's code paths —
      regardless of language or framework.
    - consumesMessage — this repo READS messages FROM a queue/topic/stream.
      `target` is the queue/topic Resource name; `target_kind=resource`.
      A consumer registers a handler that runs whenever a message arrives,
      OR runs an indefinite polling loop, OR exposes a subscription
      callback. Any of: framework-managed listener bindings, an
      explicit subscribe/consume/receive call in main(), or a worker
      that blocks on a queue read. A module that merely instantiates a
      broker client without invoking its send/publish operations is NOT
      a producer — disambiguate by inspecting actual call sites in the
      runtime path.
      If a repo does BOTH (e.g. a worker that consumes queue A and
      publishes to queue B), emit both edges with distinct targets.
    - readsResource / writesResource (only when the target is an externally-owned shared
      resource, not this repo's own DB)
  Pick the clearest `target` label; put hostnames, config keys, generated client class names
  into `aliases[]`. Set `target_kind` so the build pipeline can route it
  (`component`, `api`, `resource`, `provider`, `external`, `unknown`). When the target is a
  third-party SaaS / cloud / payment / identity provider, set `target_kind=provider` and make
  sure the same provider also appears in the `providers[]` array.

* providers[]: third-party services this repo depends on — distinct from `resources[]` (which
  are owned dependencies like the repo's own DB). Examples: payment processors (Stripe, Adyen,
  Braintree), identity / auth (Auth0, Okta, Cognito), cloud services (AWS S3, GCS, Azure Blob),
  comms (Twilio, SendGrid), analytics (Segment, Snowflake, BigQuery), observability (Datadog,
  Sentry, NewRelic), CDN (Cloudflare, Fastly), search (Algolia, Elasticsearch SaaS).
  For each: `name` (canonical brand), `category` (one of: saas, cloud, payment, identity,
  analytics, messaging, observability, cdn, search, ai-ml, logistics, tax, fraud,
  communications, other), `description` (one line, what this repo uses it for),
  `aliases[]` (SDK class names, hostnames, config keys, marketing labels — these collapse
  variant references to the same provider), and `evidence[]`. Cap at 25.
  Skip development-only tooling (linters, formatters, test runners). Include only runtime
  third-party dependencies.

* domain_attributes[]: emit from Phase B.5a. ≤30 entries. Every entry needs ≥1 evidence.
  Skip purely structural fields. Prefer attributes whose VALUE changes what the service
  does, not just what it stores. If the repo has none (e.g. a pure library or transport
  proxy), emit an empty array.

* glossary[]: emit from Phase B.5b. Target 10-20 entries. Every entry needs ≥1 evidence.
  Skip generic tech terms. Focus on what's specific to THIS service's domain. If the
  repo has no domain-specific vocabulary (rare), emit an empty array.

EVIDENCE RULES:
  - Every component, api, resource, and dependency MUST have ≥1 evidence item.
  - path must be relative to the repo root (not absolute, no `..`).
  - line is an integer ≥1.
  - snippet is a SHORT extract (≤200 chars).
  - If you cannot prove a fact with evidence, omit it or mark confidence=review.

OUT OF SCOPE:
  - Lock files, package registries, generated symbols, screenshots, translation files.
  - Test-only mock services (unless they identify the real dependency).
  - Library/build-time dependencies that are not used at runtime.
  - Per-tenant duplication when the pattern is the same.

Return ONLY JSON matching the schema. Use empty strings or empty arrays for missing fields.

KNOWN COMPONENTS IN THIS ORG (use these canonical names when referring to them as dependencies):
{glossary}

When this repo depends on a service in the glossary, use the canonical name on the LEFT as the
`target`, and put the labels you actually saw in source (config keys, hostnames, client classes,
service-as-marketed names) into `aliases[]`. Do NOT invent a new name for a service that is already
in the glossary. If you genuinely cannot tell whether a dependency is a known component or
something new, prefer the glossary entry whose aliases or canonical name most closely matches
what you saw in source.
"""


CODEX_PRICING_PER_1M: dict[str, dict[str, Any]] = {
    "gpt-5.4-mini": {"input": 0.75, "cached_input": 0.075, "output": 4.50, "currency": "USD"},
    "gpt-5.4": {"input": 5.00, "cached_input": 0.50, "output": 25.00, "currency": "USD"},
}


def load_schema() -> dict[str, Any]:
    return json.loads(CATALOG_SCHEMA_PATH.read_text(encoding="utf-8"))


def load_glossary(catalog_path: Path = DEFAULT_CATALOG, max_entries: int = GLOSSARY_MAX_ENTRIES) -> str:
    if not catalog_path.exists():
        return "  (no existing catalog yet — you are the first extraction)"
    try:
        payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "  (existing catalog could not be parsed; using empty glossary)"
    components = [e for e in payload.get("entities") or [] if e.get("kind") == "Component"]
    components.sort(
        key=lambda e: (
            0 if e.get("metadata", {}).get("annotations", {}).get("source_repos") else 1,
            e["metadata"].get("name") or "",
        )
    )
    lines: list[str] = []
    for entity in components[:max_entries]:
        meta = entity.get("metadata", {})
        name = meta.get("name") or ""
        aliases = sorted({a for a in (meta.get("annotations", {}).get("aliases") or []) if a})
        description = (meta.get("description") or "")[:80]
        bits = [f"  - {name}"]
        if aliases:
            bits.append(f"  aliases: {', '.join(aliases[:6])}")
        if description:
            bits.append(f"  ({description})")
        lines.append(" ".join(bits))
    if not lines:
        return "  (catalog has no Components yet)"
    return "\n".join(lines)


def build_prompt(repo: dict[str, Any], catalog_path: Path = DEFAULT_CATALOG) -> str:
    glossary = load_glossary(catalog_path)
    return PROMPT_TEMPLATE.format(
        repo_id=repo["id"],
        repo_path=repo["absolute_path"],
        glossary=glossary,
    )


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def estimate_cost(model: str | None, usage: dict[str, Any]) -> dict[str, Any] | None:
    if not model or model not in CODEX_PRICING_PER_1M:
        billable_total = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        return {
            "estimated_usd": round(billable_total * 5.0 / 1_000_000, 6),
            "currency": "USD",
            "pricing_note": f"Fallback pricing — model {model!r} not in table; assumed $5/1M tokens.",
            "fallback": True,
        }
    pricing = CODEX_PRICING_PER_1M[model]
    input_tokens = int(usage.get("input_tokens") or 0)
    cached_tokens = int(usage.get("cached_input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    billable_input = max(input_tokens - cached_tokens, 0)
    cost = (
        billable_input * pricing["input"]
        + cached_tokens * pricing["cached_input"]
        + output_tokens * pricing["output"]
    ) / 1_000_000
    return {
        "estimated_usd": round(cost, 6),
        "currency": pricing["currency"],
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "billable_input_tokens": billable_input,
        "output_tokens": output_tokens,
        "fallback": False,
    }


def _terminate_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait(timeout=5)


def _read_json_if_valid(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _summarize_codex_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    event = payload.get("type") or ""
    item = payload.get("item")
    if event in {"thread.started", "turn.started", "turn.completed"}:
        out: dict[str, Any] = {"event": event.replace(".", "_")}
        if isinstance(payload.get("usage"), dict):
            out["usage"] = payload["usage"]
        return out
    if isinstance(item, dict):
        return {"event": event.replace(".", "_"), "item_type": item.get("type", ""), "status": item.get("status", "")}
    return None


def codex_extract(
    repo: dict[str, Any],
    model: str | None,
    effort: str,
    timeout_seconds: int,
    post_result_grace: int,
    stream_logs: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    codex = shutil.which("codex")
    if not codex:
        raise SystemExit("codex CLI was not found on PATH")
    work_dir = DEFAULT_OUTPUT_DIR / ".scratch"
    work_dir.mkdir(parents=True, exist_ok=True)
    schema_path = work_dir / f".{repo['name']}.schema.json"
    result_path = work_dir / f".{repo['name']}.result.json"
    schema_path.write_text(json.dumps(load_schema(), indent=2) + "\n", encoding="utf-8")
    if result_path.exists():
        result_path.unlink()

    prompt = build_prompt(repo)
    cmd = [
        codex,
        "exec",
        "--ephemeral",
        "-c",
        f'model_reasoning_effort="{effort}"',
        "--cd",
        repo["absolute_path"],
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(result_path),
    ]
    if stream_logs:
        cmd.append("--json")
    if model:
        cmd.extend(["--model", model])
    cmd.append(prompt)

    started = time.monotonic()
    last_usage: dict[str, Any] | None = None
    returncode = 0

    if stream_logs:
        process = subprocess.Popen(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, bufsize=1)
        assert process.stdout is not None
        deadline = started + timeout_seconds
        result_seen_at: float | None = None
        try:
            while True:
                now = time.monotonic()
                if process.poll() is not None:
                    for line in process.stdout.readlines():
                        summary = _summarize_codex_line(line)
                        if summary and isinstance(summary.get("usage"), dict):
                            last_usage = summary["usage"]
                        if summary:
                            print(json.dumps(summary), flush=True)
                    returncode = process.returncode or 0
                    break
                payload = _read_json_if_valid(result_path)
                if payload is not None and result_seen_at is None:
                    result_seen_at = now
                if result_seen_at is not None and now - result_seen_at >= post_result_grace:
                    _terminate_group(process)
                    returncode = 0
                    break
                if now >= deadline:
                    if _read_json_if_valid(result_path) is not None:
                        _terminate_group(process)
                        returncode = 0
                        break
                    _terminate_group(process)
                    raise SystemExit(f"codex timed out after {timeout_seconds}s for {repo['id']}")
                wait_seconds = min(1.0, max(0.0, deadline - now))
                ready, _, _ = select.select([process.stdout], [], [], wait_seconds)
                if not ready:
                    continue
                line = process.stdout.readline()
                if not line:
                    continue
                summary = _summarize_codex_line(line)
                if summary and isinstance(summary.get("usage"), dict):
                    last_usage = summary["usage"]
                if summary:
                    print(json.dumps(summary), flush=True)
        except KeyboardInterrupt:
            _terminate_group(process)
            raise
    else:
        try:
            completed = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout_seconds)
            returncode = completed.returncode or 0
        except subprocess.TimeoutExpired as exc:
            payload = _read_json_if_valid(result_path)
            if payload is None:
                raise SystemExit(f"codex timed out after {timeout_seconds}s for {repo['id']}") from exc

    payload = _read_json_if_valid(result_path)
    if payload is None:
        raise SystemExit(f"codex did not write a usable JSON result for {repo['id']} (rc={returncode})")
    run = {
        "provider": "codex",
        "model": model or "default",
        "effort": effort,
        "duration_seconds": round(time.monotonic() - started, 3),
        "returncode": returncode,
        "usage": last_usage or {},
        "cost": estimate_cost(model, last_usage or {}),
    }
    return payload, run


def claude_extract(
    repo: dict[str, Any],
    model: str,
    effort: str,
    max_budget_usd: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    claude = shutil.which("claude")
    if not claude:
        raise SystemExit("claude CLI was not found on PATH")
    prompt = build_prompt(repo)
    cmd = [
        claude,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(load_schema(), separators=(",", ":")),
        "--permission-mode",
        "dontAsk",
        "--model",
        model,
        "--effort",
        effort,
        "--allowedTools",
        "Read,Grep,Glob,Bash(rg *)",
        "--add-dir",
        repo["absolute_path"],
    ]
    if max_budget_usd:
        cmd.extend(["--max-budget-usd", max_budget_usd])
    started = time.monotonic()
    completed = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout[:1000]
        raise SystemExit(detail or f"claude exited with {completed.returncode}")
    raw = json.loads(completed.stdout)
    if isinstance(raw, dict) and isinstance(raw.get("structured_output"), dict):
        payload = raw["structured_output"]
    elif isinstance(raw, dict) and isinstance(raw.get("result"), str):
        payload = json.loads(raw["result"])
    else:
        payload = raw
    run = {
        "provider": "claude",
        "model": model,
        "effort": effort,
        "duration_seconds": round(time.monotonic() - started, 3),
        "returncode": 0,
        "cost": {"estimated_usd": raw.get("total_cost_usd") if isinstance(raw, dict) else None, "currency": "USD"},
    }
    return payload, run


def codex_correct(
    repo: dict[str, Any],
    prompt: str,
    model: str | None,
    effort: str,
    timeout_seconds: int = 600,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Single targeted correction call to Codex with the correction schema.

    Smaller / shorter than `codex_extract` — no stream-log handling, tighter
    timeout, and the supplied prompt+schema instead of the main extraction
    prompt and catalog schema. Used by the Phase A+B correction loop.
    """
    codex = shutil.which("codex")
    if not codex:
        raise SystemExit("codex CLI was not found on PATH")
    work_dir = DEFAULT_OUTPUT_DIR / ".scratch"
    work_dir.mkdir(parents=True, exist_ok=True)
    schema_path = work_dir / f".{repo['name']}.correction.schema.json"
    result_path = work_dir / f".{repo['name']}.correction.result.json"
    schema_path.write_text(
        json.dumps(correction.CORRECTION_SCHEMA, indent=2) + "\n", encoding="utf-8"
    )
    if result_path.exists():
        result_path.unlink()
    cmd = [
        codex,
        "exec",
        "--ephemeral",
        "-c",
        f'model_reasoning_effort="{effort}"',
        "--cd",
        repo["absolute_path"],
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(result_path),
    ]
    if model:
        cmd.extend(["--model", model])
    cmd.append(prompt)
    started = time.monotonic()
    completed = subprocess.run(
        cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout_seconds
    )
    payload = _read_json_if_valid(result_path)
    if payload is None:
        raise RuntimeError(
            f"codex correction returned no usable JSON for {repo['id']} (rc={completed.returncode})"
        )
    run = {
        "provider": "codex",
        "model": model or "default",
        "effort": effort,
        "duration_seconds": round(time.monotonic() - started, 3),
        "returncode": completed.returncode,
        "kind": "correction",
    }
    return payload, run


def claude_correct(
    repo: dict[str, Any],
    prompt: str,
    model: str,
    effort: str,
    max_budget_usd: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Single targeted correction call to Claude with the correction schema."""
    claude = shutil.which("claude")
    if not claude:
        raise SystemExit("claude CLI was not found on PATH")
    cmd = [
        claude,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(correction.CORRECTION_SCHEMA, separators=(",", ":")),
        "--permission-mode",
        "dontAsk",
        "--model",
        model,
        "--effort",
        effort,
        "--allowedTools",
        "Read,Grep,Glob,Bash(rg *)",
        "--add-dir",
        repo["absolute_path"],
    ]
    if max_budget_usd:
        cmd.extend(["--max-budget-usd", max_budget_usd])
    started = time.monotonic()
    completed = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout[:1000]
        raise RuntimeError(detail or f"claude correction exited with {completed.returncode}")
    raw = json.loads(completed.stdout)
    if isinstance(raw, dict) and isinstance(raw.get("structured_output"), dict):
        payload = raw["structured_output"]
    elif isinstance(raw, dict) and isinstance(raw.get("result"), str):
        payload = json.loads(raw["result"])
    else:
        payload = raw
    run = {
        "provider": "claude",
        "model": model,
        "effort": effort,
        "duration_seconds": round(time.monotonic() - started, 3),
        "returncode": 0,
        "kind": "correction",
    }
    return payload, run


def validate_against_schema(payload: dict[str, Any]) -> list[str]:
    try:
        import jsonschema
    except ImportError:
        return []
    schema = load_schema()
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(payload), key=lambda e: e.path)
    return [f"{'/'.join(str(p) for p in e.path)}: {e.message}" for e in errors[:20]]


def normalize_payload(payload: dict[str, Any], repo: dict[str, Any]) -> dict[str, Any]:
    payload.setdefault("repo", {})
    if isinstance(payload["repo"], dict):
        payload["repo"].setdefault("id", repo["id"])
        payload["repo"].setdefault("tagline", "")
        payload["repo"].setdefault("capability_sheet", "")
    payload.setdefault("components", [])
    payload.setdefault("apis", [])
    payload.setdefault("resources", [])
    payload.setdefault("dependencies", [])
    payload.setdefault("providers", [])
    payload.setdefault("domain_attributes", [])
    payload.setdefault("glossary", [])
    # Backfill the Backstage-aligned fields on components for older catalog JSONs.
    for component in payload.get("components") or []:
        if isinstance(component, dict):
            component.setdefault("environments", [])
            component.setdefault("runtime", "")
            component.setdefault("lifecycle", "")
            component.setdefault("subcomponent_of", "")
            # Normalise lifecycle: Backstage doesn't allow "unknown".
            if component.get("lifecycle") in ("unknown", None):
                component["lifecycle"] = ""
    # We keep repo.lifecycle's "unknown" value as-is for internal use; the
    # export_backstage step strips it when emitting Backstage YAML.
    return payload


def confine_evidence_paths(payload: dict[str, Any], repo_root: Path) -> int:
    repo_root = repo_root.resolve()
    quarantined = 0

    def _scrub(items: list[dict[str, Any]]) -> None:
        nonlocal quarantined
        for item in items:
            evidence = item.get("evidence") or []
            kept = []
            for ev in evidence:
                raw_path = (ev.get("path") or "").lstrip("/")
                if not raw_path or ".." in Path(raw_path).parts:
                    quarantined += 1
                    continue
                try:
                    resolved = (repo_root / raw_path).resolve()
                    resolved.relative_to(repo_root)
                except (ValueError, OSError):
                    quarantined += 1
                    continue
                kept.append(ev)
            item["evidence"] = kept

    if isinstance(payload.get("repo"), dict) and isinstance(payload["repo"].get("summary"), dict):
        _scrub([payload["repo"]["summary"]])
    for key in ("components", "apis", "resources", "dependencies"):
        _scrub(payload.get(key) or [])
    return quarantined


def write_output(output_dir: Path, payload: dict[str, Any]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    repo_id = payload["repo"]["id"]
    name = repo_id.split("/", 1)[-1]
    path = output_dir / f"{name}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def append_run_ledger(record: dict[str, Any]) -> Path:
    RUNS_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with RUNS_LEDGER.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return RUNS_LEDGER


def _correction_call(
    repo: dict[str, Any],
    prompt: str,
    *,
    provider: str,
    model: str | None,
    effort: str,
    timeout_seconds: int,
    max_budget_usd: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if provider == "claude":
        return claude_correct(repo, prompt, model or "sonnet", effort, max_budget_usd)
    return codex_correct(repo, prompt, model, effort, timeout_seconds=max(300, timeout_seconds // 2))


def run_for_repo(
    repo: dict[str, Any],
    *,
    provider: str,
    model: str | None,
    effort: str,
    timeout_seconds: int,
    post_result_grace: int,
    stream_logs: bool,
    max_budget_usd: str | None,
    output_dir: Path,
    correction_rounds: int = 1,
) -> dict[str, Any]:
    if provider == "claude":
        payload, run = claude_extract(repo, model or "sonnet", effort, max_budget_usd)
    else:
        payload, run = codex_extract(repo, model, effort, timeout_seconds, post_result_grace, stream_logs)
    payload = normalize_payload(payload, repo)
    repo_root = Path(repo["absolute_path"])
    quarantined = confine_evidence_paths(payload, repo_root)
    errors = validate_against_schema(payload)
    cross_check = None
    correction_runs: list[dict[str, Any]] = []
    if not errors:
        # Phase A (snippet substring) + Phase B (tree-sitter AST) cross-checks
        # on the LLM's evidence. Phase A asks "is the snippet at the cited
        # line?"; Phase B asks "does the cited line actually do what the
        # edge claims it does?". The calibrator combines both verdicts into
        # confidence changes on `dependencies` and `resources`, and annotates
        # every fact with `_cross_check` (and `_cross_check_ast` for deps).
        for round_idx in range(max(0, correction_rounds)):
            report_a = snippet_verify.verify_payload(payload, repo_root)
            report_b = ast_crosscheck.verify_payload(payload, repo_root)
            problems = calibrate.collect_problems(payload, report_a, report_b)
            if not problems:
                break
            try:
                prompt = correction.build_correction_prompt(
                    repo["id"], repo["absolute_path"], problems
                )
                raw, c_run = _correction_call(
                    repo,
                    prompt,
                    provider=provider,
                    model=model,
                    effort=effort,
                    timeout_seconds=timeout_seconds,
                    max_budget_usd=max_budget_usd,
                )
                directives = correction.parse_correction_response(raw)
                apply_summary = correction.apply_corrections(payload, directives)
                # Re-quarantine in case corrections introduced bad paths.
                quarantined += confine_evidence_paths(payload, repo_root)
                round_errors = validate_against_schema(payload)
                correction_runs.append(
                    {
                        "round": round_idx + 1,
                        "problems_input": len(problems),
                        "directives_returned": len(directives),
                        "applied": apply_summary,
                        "run": c_run,
                        "post_correction_validation_errors": round_errors,
                    }
                )
                if round_errors:
                    errors = round_errors
                    break
            except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
                correction_runs.append(
                    {"round": round_idx + 1, "problems_input": len(problems), "error": str(exc)}
                )
                break

        if not errors:
            report_a = snippet_verify.verify_payload(payload, repo_root)
            report_b = ast_crosscheck.verify_payload(payload, repo_root)
            cross_check = calibrate.apply(payload, report_a, report_b)
    payload["_meta"] = {
        "provider": provider,
        "model": run["model"],
        "effort": effort,
        "commit": repo["commit"],
        "extracted_at": utc_now_iso(),
        "schema": "catalog-v1",
        "evidence_quarantined": quarantined,
        "validation_errors": errors,
        "cross_check": cross_check,
        "correction_runs": correction_runs,
        "run": run,
    }
    if errors:
        target_dir = DEFAULT_QUARANTINE_DIR
        target_dir.mkdir(parents=True, exist_ok=True)
        out = write_output(target_dir, payload)
        append_run_ledger({"repo": repo["id"], "status": "quarantined", "errors": errors, **run})
        return {"output": str(out), "status": "quarantined", "errors": errors, "run": run}
    out = write_output(output_dir, payload)
    append_run_ledger({"repo": repo["id"], "status": "ok", **run})
    return {
        "output": str(out),
        "status": "ok",
        "components": len(payload["components"]),
        "apis": len(payload["apis"]),
        "resources": len(payload["resources"]),
        "dependencies": len(payload["dependencies"]),
        "run": run,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", help="Repo name or org/repo id")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--provider", choices=("codex", "claude"), default="codex")
    parser.add_argument("--model", default=None)
    parser.add_argument("--effort", default="high")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--post-result-grace-seconds", type=int, default=15)
    parser.add_argument("--stream-logs", action="store_true")
    parser.add_argument("--max-budget-usd", default=None)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--workspace", type=Path, default=HERE / "workspace.json")
    parser.add_argument(
        "--correction-rounds",
        type=int,
        default=1,
        help="Phase A+B verifier correction rounds to run after extraction. "
             "0 disables the correction loop; values >2 rarely help in practice.",
    )
    args = parser.parse_args()

    root = args.root.resolve()
    workspace = load_workspace_config(args.workspace)
    repos = find_repos(root, workspace["orgs"], workspace["excluded_repos"])
    selected = next((r for r in repos if args.repo in {r["id"], r["name"]}), None)
    if selected is None:
        raise SystemExit(f"Repo {args.repo!r} not found under {root}")

    result = run_for_repo(
        selected,
        provider=args.provider,
        model=args.model,
        effort=args.effort,
        timeout_seconds=args.timeout_seconds,
        post_result_grace=args.post_result_grace_seconds,
        stream_logs=args.stream_logs,
        max_budget_usd=args.max_budget_usd,
        output_dir=args.output_dir if args.output_dir.is_absolute() else (HERE / args.output_dir),
        correction_rounds=args.correction_rounds,
    )
    print("EXTRACTOR_RESULT " + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
