"""Phase B as extractor — emit Backstage-shaped edges directly from AST.

Where `ast_crosscheck` *verifies* the LLM's claims, this module *emits*
them. It walks a repo's source tree with tree-sitter, finds unambiguous
patterns (listener annotations, publish-call APIs with literal queue/topic
arguments), and produces dependency edges and resource entities with
file:line evidence — the same shape the LLM produces.

This is the deterministic, low-cost, complete-coverage signal. The
research consensus (arXiv 2601.08773) shows AST-derived KGs beat
LLM-extracted KGs on both correctness and completeness at ~10× lower
cost. ServiceScout reconciles both: AST emits structural facts; LLM
emits semantic facts (taglines, capability sheets, business domain);
the reconciler combines both with verifier feedback.

Currently supports: Java + Python + Go + JavaScript/TypeScript message
broker producers and consumers (RabbitMQ / Kafka / generic queue+topic).
HTTP-call extraction and DB-call extraction are out of scope for v1;
those edges are higher false-positive risk and benefit more from LLM
semantic enrichment ("which service is `client.get(url)` actually
calling?").

Output shape: a partial catalog payload with `resources[]` and
`dependencies[]`, plus per-fact provenance under `_emitted_by` so the
reconciler can tell AST-emitted from LLM-emitted facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from tree_sitter import Node, Parser, Query, QueryCursor

from static_extractors.ast_crosscheck import (
    _get_language,
    language_for_path,
)


# --------------------------------------------------------------------------- #
# Extraction rules
# --------------------------------------------------------------------------- #
# Each rule produces (kind, broker_hint, queue_or_topic_name, evidence).
# Tree-sitter queries are written to capture both the call site and the
# literal string argument so we can attribute the edge to a real
# queue/topic name, not "unknown".

JAVA_EXTRACT_RULES = {
    # `@RabbitListener(queues = "foo")` / `@KafkaListener(topics = "bar")`
    # / `@JmsListener(destination = "baz")` annotations on methods.
    # Tighten the inner element_value_pair to only match the parameters
    # that are actually queue/topic names — not adjacent params like
    # `containerFactory`, `concurrency`, `groupId`, etc.
    "annotation_consumer": r"""
        (annotation
          name: (identifier) @ann_name
          (#match? @ann_name "(Rabbit|Kafka|Jms|Stream)Listener$")
          arguments: (annotation_argument_list
            (element_value_pair
              key: (identifier) @kw
              (#match? @kw "^(destination|destinations|queues|queue|topics|topic|value)$")
              value: (string_literal (string_fragment) @queue)))) @hit
    """,
    # `rabbitTemplate.convertAndSend("queue-name", payload)` or any
    # `.convertAndSend(STRING, ...)` / `.send(STRING, ...)` / `.publish(STRING, ...)`
    # where the first arg is a literal string. The string IS the queue/topic.
    "method_producer_with_literal": r"""
        (method_invocation
          name: (identifier) @method
          (#match? @method "^(convertAndSend|send|publish|basicPublish|sendMessage)$")
          arguments: (argument_list . (string_literal (string_fragment) @queue))) @hit
    """,
    # Programmatic listener container binding:
    #   container.setQueueNames("foo");
    # or `new Queue("foo", false)` + binding.
    "programmatic_consumer": r"""
        (method_invocation
          name: (identifier) @method
          (#match? @method "^(setQueueNames|setQueueName|addQueueNames)$")
          arguments: (argument_list . (string_literal (string_fragment) @queue))) @hit
    """,
}

PYTHON_EXTRACT_RULES = {
    # channel.basic_publish(routing_key="rk", ...) — the queue name comes
    # from a keyword arg (the python convention). Tree-sitter-python wraps
    # kwargs as (keyword_argument name: ... value: ...).
    "method_producer_kwarg_routing": r"""
        (call
          function: (attribute
            attribute: (identifier) @method
            (#match? @method "^(basic_publish|publish|send|send_message|produce)$"))
          arguments: (argument_list
            (keyword_argument
              name: (identifier) @kw
              (#match? @kw "^(routing_key|topic|queue|destination|key)$")
              value: (string (string_content) @queue)))) @hit
    """,
    # channel.basic_publish("ex", "rk", body=...) — positional, first string is exchange.
    # Less reliable signal (could be exchange not queue) but still indicates a
    # producer; we extract the literal anyway.
    "method_producer_positional": r"""
        (call
          function: (attribute
            attribute: (identifier) @method
            (#match? @method "^(publish|send|send_message|produce)$"))
          arguments: (argument_list . (string (string_content) @queue) .)) @hit
    """,
    # channel.basic_consume(queue='shipping-task', on_message_callback=...)
    "method_consumer": r"""
        (call
          function: (attribute
            attribute: (identifier) @method
            (#match? @method "^(basic_consume|consume|subscribe|listen)$"))
          arguments: (argument_list
            (keyword_argument
              name: (identifier) @kw
              (#match? @kw "^(queue|topic|destination|key)$")
              value: (string (string_content) @queue)))) @hit
    """,
}

GO_EXTRACT_RULES = {
    # ch.Publish("exchange", "routing-key", ...) etc.
    "method_producer": r"""
        (call_expression
          function: (selector_expression
            field: (field_identifier) @method
            (#match? @method "^(Publish|PublishWithContext|SendMessage|Send|Produce)$"))
          arguments: (argument_list . (interpreted_string_literal) @queue)) @hit
    """,
    # ch.Consume("queue", "consumer", ...)
    "method_consumer": r"""
        (call_expression
          function: (selector_expression
            field: (field_identifier) @method
            (#match? @method "^(Consume|Subscribe|ReceiveMessage|Receive)$"))
          arguments: (argument_list . (interpreted_string_literal) @queue)) @hit
    """,
}

JS_EXTRACT_RULES = {
    # JS messaging patterns are deliberately tight because the same method
    # names (.publish/.send/.emit/.on/.subscribe) are used pervasively for
    # DOM events, EventEmitters, RxJS, and jQuery — all of which would
    # produce false-positive Backstage edges. Restrict to verbs unlikely
    # to appear in DOM/event-handler code. For brokers, the import-gating
    # in `_should_run_rules` is the primary defence.
    "method_producer": r"""
        (call_expression
          function: (member_expression
            property: (property_identifier) @method
            (#match? @method "^(publish|sendMessage|produce|basicPublish|sendToQueue)$"))
          arguments: (arguments . (string (string_fragment) @queue))) @hit
    """,
    "method_consumer": r"""
        (call_expression
          function: (member_expression
            property: (property_identifier) @method
            (#match? @method "^(subscribe|consume|basicConsume|consumeFromQueue)$"))
          arguments: (arguments . (string (string_fragment) @queue))) @hit
    """,
}


# Maps (language, rule_name) → (kind, edge_kind)
# kind is for naming the rule; edge_kind is the dependency.kind value emitted.
_EDGE_KIND_BY_RULE: dict[str, str] = {
    "annotation_consumer": "consumesMessage",
    "method_producer_with_literal": "producesMessage",
    "method_producer": "producesMessage",
    "method_producer_kwarg_routing": "producesMessage",
    "method_producer_positional": "producesMessage",
    "programmatic_consumer": "consumesMessage",
    "method_consumer": "consumesMessage",
}


_RULES_BY_LANG: dict[str, dict[str, str]] = {
    "java": JAVA_EXTRACT_RULES,
    "python": PYTHON_EXTRACT_RULES,
    "go": GO_EXTRACT_RULES,
    "javascript": JS_EXTRACT_RULES,
    "typescript": JS_EXTRACT_RULES,
    "tsx": JS_EXTRACT_RULES,
}


@lru_cache(maxsize=128)
def _compile_query(language: str, rule_name: str) -> Query | None:
    rules = _RULES_BY_LANG.get(language)
    if not rules:
        return None
    src = rules.get(rule_name)
    if not src:
        return None
    return Query(_get_language(language), src)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass
class AstEdge:
    kind: str
    queue_or_topic: str
    path: str
    line: int
    snippet: str
    rule: str
    language: str


# Files we never want to extract from — tests, fixtures, generated code.
_SKIP_DIR_PARTS = frozenset(
    {".git", "node_modules", "target", "build", "dist", "out", "vendor", ".venv", "venv", "__pycache__"}
)
_TEST_DIR_PARTS = frozenset({"test", "tests", "spec", "specs", "__tests__"})


def _should_skip(rel_path: Path) -> bool:
    parts = set(rel_path.parts)
    if parts & _SKIP_DIR_PARTS:
        return True
    # Test directories are skipped — listener annotations in test code
    # don't represent runtime topology.
    if parts & _TEST_DIR_PARTS:
        return True
    return False


def _walk_source_files(repo_root: Path) -> Iterable[Path]:
    repo_root = repo_root.resolve()
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(repo_root)
        except ValueError:
            continue
        if _should_skip(rel):
            continue
        lang = language_for_path(str(rel))
        if lang is None:
            continue
        yield path


# --------------------------------------------------------------------------- #
# Import-gating
# --------------------------------------------------------------------------- #
# Files that don't import a known message-broker library will not emit
# any messaging edges. This is the primary false-positive filter:
# `.send()` / `.publish()` / `.subscribe()` are too generic on their own
# (used for DOM events, EventEmitters, HTTP clients), so we require a
# library import as proof of intent. This is the same approach Sourcegraph
# SCIP / Moderne LST use at semantic-resolution time.

_BROKER_HINTS = {
    "java": (
        "org.springframework.amqp", "org.springframework.jms",
        "org.apache.kafka", "javax.jms", "jakarta.jms",
        "com.rabbitmq.client", "io.nats", "com.amazonaws.services.sqs",
        "com.amazonaws.services.sns", "software.amazon.awssdk.services.sqs",
        "software.amazon.awssdk.services.sns", "com.azure.messaging",
        "com.google.cloud.pubsub", "io.confluent.kafka",
        "reactor.kafka", "spring.cloud.stream",
    ),
    "python": (
        "pika", "kafka", "confluent_kafka", "aio_pika", "aiokafka",
        "kombu", "celery", "redis", "rsmq", "nats", "stomp",
        "boto3.client", "google.cloud.pubsub", "azure.servicebus",
    ),
    "go": (
        "github.com/streadway/amqp", "github.com/rabbitmq/amqp091-go",
        "github.com/Shopify/sarama", "github.com/confluentinc/confluent-kafka-go",
        "github.com/nats-io/nats.go", "github.com/aws/aws-sdk-go",
        "cloud.google.com/go/pubsub",
    ),
    "javascript": (
        "amqplib", "kafkajs", "@azure/service-bus", "@aws-sdk/client-sqs",
        "@aws-sdk/client-sns", "bull", "bullmq", "node-rdkafka",
        "@google-cloud/pubsub", "rascal", "ioredis", "rabbit.js",
        "nats", "stomp",
    ),
    "typescript": (
        "amqplib", "kafkajs", "@azure/service-bus", "@aws-sdk/client-sqs",
        "@aws-sdk/client-sns", "bull", "bullmq", "node-rdkafka",
        "@google-cloud/pubsub", "rascal", "ioredis", "rabbit.js",
        "nats", "stomp",
    ),
}
# tsx shares the typescript hints.
_BROKER_HINTS["tsx"] = _BROKER_HINTS["typescript"]


def _has_broker_import(src: bytes, language: str) -> bool:
    """Cheap pre-filter: does this file's text contain a known broker
    import string? Cheap text scan, not AST traversal — false negatives
    only matter when a real broker hint isn't in our list (then we miss
    a real edge, but we don't emit a false positive). False positives
    here only matter if a broker name happens to appear in a comment or
    string — exceedingly rare in practice.
    """
    hints = _BROKER_HINTS.get(language, ())
    if not hints:
        return False
    needle = src[:8192]  # imports are at file head; scan only the first 8KB
    for h in hints:
        if h.encode("utf-8") in needle:
            return True
    return False


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def extract_messaging_edges(
    repo_root: Path, *, component_name: str
) -> list[AstEdge]:
    """Walk the repo, run all extraction rules, return AstEdge records.

    The `component_name` is the Backstage Component this code belongs to
    — used as the `source` on emitted edges. The target on each edge is
    `Resource:<queue_or_topic>`.

    Caller is responsible for merging these edges into a catalog payload
    (see `merge_into_payload` below).
    """
    repo_root = repo_root.resolve()
    edges: list[AstEdge] = []
    for path in _walk_source_files(repo_root):
        rel_path = path.relative_to(repo_root)
        lang = language_for_path(str(rel_path)) or ""
        rules = _RULES_BY_LANG.get(lang)
        if not rules:
            continue
        try:
            src = path.read_bytes()
        except OSError:
            continue
        # Skip files that don't import a known broker library. Big false-
        # positive guard — without this, every `.publish()` in a frontend
        # repo gets misread as a producer.
        if not _has_broker_import(src, lang):
            continue
        parser = Parser(_get_language(lang))
        tree = parser.parse(src)
        for rule_name in rules:
            query = _compile_query(lang, rule_name)
            if query is None:
                continue
            cursor = QueryCursor(query)
            matches = cursor.matches(tree.root_node)
            for _match_id, captures in matches:
                hit_nodes = captures.get("hit") or []
                queue_nodes = captures.get("queue") or []
                if isinstance(hit_nodes, Node):
                    hit_nodes = [hit_nodes]
                if isinstance(queue_nodes, Node):
                    queue_nodes = [queue_nodes]
                for hit_n, queue_n in zip(hit_nodes, queue_nodes):
                    queue_name = queue_n.text.decode("utf-8", errors="replace")
                    if not queue_name:
                        continue
                    line = hit_n.start_point[0] + 1
                    # Snippet: the cited line's actual content (trimmed).
                    snippet = _line_text(src, line).strip()[:200]
                    edges.append(
                        AstEdge(
                            kind=_EDGE_KIND_BY_RULE[rule_name],
                            queue_or_topic=queue_name,
                            path=str(rel_path),
                            line=line,
                            snippet=snippet,
                            rule=rule_name,
                            language=lang,
                        )
                    )
    return edges


def _line_text(src: bytes, line: int) -> str:
    lines = src.split(b"\n")
    if 1 <= line <= len(lines):
        try:
            return lines[line - 1].decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            return ""
    return ""


# --------------------------------------------------------------------------- #
# Merging into a catalog payload
# --------------------------------------------------------------------------- #


def merge_into_payload(
    payload: dict[str, Any],
    edges: list[AstEdge],
    *,
    component_name: str,
) -> dict[str, Any]:
    """Merge AST-emitted edges into an LLM-extracted payload.

    Reconcile rules:

      - If the LLM already has an edge with the same (source, target, kind):
        leave it alone, but record the AST confirmation under `_emitted_by`.
      - If the LLM has an edge with the same (source, target) but a
        DIFFERENT kind (e.g. shipping direction reversed): the AST wins
        on `kind`, the LLM's evidence is preserved, and `confidence` is
        set to `review` with a note explaining the disagreement.
      - If the LLM has no edge for this (source, target):
        add a new edge with the AST evidence and `confidence: high`
        (AST is deterministic; the cited line literally performs the
        operation).
      - Resource entities: ensure a Resource exists for each queue/topic
        named by AST-emitted edges. If the LLM didn't emit one, add a
        minimal Resource node with `type: queue` (we can't determine
        queue vs topic from the AST alone; the LLM can refine later).

    Returns a summary record describing what changed.
    """
    summary: dict[str, Any] = {"added": [], "kind_corrected": [], "confirmed": [], "resources_added": []}
    deps = payload.setdefault("dependencies", [])
    resources = payload.setdefault("resources", [])

    existing_resource_names = {r.get("name") for r in resources if isinstance(r, dict)}

    # Index existing deps by (source, target) so we can detect duplicates
    # and kind mismatches.
    by_pair: dict[tuple[str, str], list[int]] = {}
    for idx, d in enumerate(deps):
        if not isinstance(d, dict):
            continue
        by_pair.setdefault((d.get("source") or "", d.get("target") or ""), []).append(idx)

    # Group AST edges by (target, kind) to consolidate multiple hits to
    # the same queue into one edge with multiple evidence items.
    grouped: dict[tuple[str, str], list[AstEdge]] = {}
    for e in edges:
        grouped.setdefault((e.queue_or_topic, e.kind), []).append(e)

    for (target, kind), group in grouped.items():
        evidence = [
            {"path": e.path, "line": e.line, "snippet": e.snippet}
            for e in group
        ]
        emitted_by = sorted({f"{e.rule}({e.language})" for e in group})

        existing_indices = by_pair.get((component_name, target), [])
        same_kind_match = next(
            (i for i in existing_indices if deps[i].get("kind") == kind), None
        )
        different_kind_match = next(
            (i for i in existing_indices if deps[i].get("kind") != kind), None
        )

        if same_kind_match is not None:
            d = deps[same_kind_match]
            d.setdefault("_emitted_by", []).extend(emitted_by)
            # Add AST-derived evidence (preserve LLM's existing evidence).
            d["evidence"] = (d.get("evidence") or []) + evidence
            summary["confirmed"].append(
                {"source": component_name, "target": target, "kind": kind}
            )
        elif different_kind_match is not None:
            d = deps[different_kind_match]
            old_kind = d.get("kind")
            d["kind"] = kind
            d.setdefault("notes", "")
            d["notes"] = (
                (d["notes"] + " ") if d["notes"] else ""
            ) + f"AST disagrees with LLM: LLM said {old_kind}, AST says {kind}. AST wins."
            d["confidence"] = "review"
            d["evidence"] = (d.get("evidence") or []) + evidence
            d.setdefault("_emitted_by", []).extend(emitted_by)
            summary["kind_corrected"].append(
                {
                    "source": component_name,
                    "target": target,
                    "before_kind": old_kind,
                    "after_kind": kind,
                }
            )
        else:
            new_dep = {
                "source": component_name,
                "target": target,
                "target_kind": "resource",
                "kind": kind,
                "protocol": "amqp",  # default; LLM can refine on next round
                "operation_or_usage": "",
                "message_or_event_name": target,
                "env_or_config_keys": [],
                "aliases": [],
                "confidence": "high",
                "notes": "Emitted by deterministic AST extractor.",
                "evidence": evidence,
                "_emitted_by": emitted_by,
            }
            deps.append(new_dep)
            summary["added"].append(
                {"source": component_name, "target": target, "kind": kind}
            )

        if target not in existing_resource_names:
            new_resource = {
                "name": target,
                "type": "queue",  # default — LLM can refine to topic / virtual-topic / stream
                "technology": "",
                "host_or_instance": "",
                "database_or_schema": "",
                "tables_or_collections": [],
                "access": "consume" if kind == "consumesMessage" else "publish",
                "env_or_config_keys": [],
                "used_by": component_name,
                "messaging_pattern": "broker-queue",
                "subscribes_to": "",
                "datasource_url": "",
                "confidence": "high",
                "notes": "Emitted by deterministic AST extractor.",
                "evidence": evidence,
                "_emitted_by": emitted_by,
            }
            resources.append(new_resource)
            existing_resource_names.add(target)
            summary["resources_added"].append({"name": target})

    return summary
