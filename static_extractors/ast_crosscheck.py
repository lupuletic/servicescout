"""Phase B — tree-sitter AST cross-check.

For every LLM-extracted **edge** (entry in `payload["dependencies"]`), open
the cited file, parse it with tree-sitter, and check that the structural
pattern matching the edge's `kind` actually appears at (or near) the cited
line. Where Phase A asks "is the cited snippet present at that line?",
Phase B asks "is the cited line actually doing what the edge claims it is
doing?".

Example: an LLM produces `orders -producesMessage-> shipping-task` citing
`OrdersController.java:50`. Phase A confirms the snippet `@PostMapping(...)`
appears there. Phase B checks for a publish call pattern at line 50 —
finds only a route registration → returns `disconfirmed`. The two phases
combined catch the case where the line is right but the semantics are
wrong.

Languages: Java, Python, JavaScript, TypeScript, Go (matches sock-shop
shape: orders/shipping/carts/queue-master = Java; payment/catalogue/user
= Go; front-end = Node; load-test = Python).

Verdicts per dependency edge:
  - confirmed:    rule matched within ±SEARCH_WINDOW lines of cited line.
  - mixed:        rule matched somewhere in the file, but not in the
                  cited line's window (file is right, line is wrong).
  - disconfirmed: rule did not match anywhere in the file.
  - unsupported:  no rule for this (language, kind) pair, or file
                  language not recognised. Treated as "no signal" — the
                  calibrator falls back to Phase A's verdict alone.

Resources, components, APIs, providers, domain_attributes, glossary
entries are out of scope for Phase B. Their evidence is best validated
by Phase A's substring check; structural patterns for those would
require parsing helm/k8s/IaC, which is the (deferred) Tier-1-item-#3
work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import tree_sitter_go as ts_go
import tree_sitter_java as ts_java
import tree_sitter_javascript as ts_js
import tree_sitter_python as ts_py
import tree_sitter_typescript as ts_ts
from tree_sitter import Language, Node, Parser, Query, QueryCursor


SEARCH_WINDOW_BEFORE = 3
SEARCH_WINDOW_AFTER = 8


# --------------------------------------------------------------------------- #
# Language registry
# --------------------------------------------------------------------------- #


_LANG_BY_EXT = {
    "java": "java",
    "py": "python",
    "go": "go",
    "js": "javascript",
    "jsx": "javascript",
    "mjs": "javascript",
    "cjs": "javascript",
    "ts": "typescript",
    "tsx": "tsx",
}


@lru_cache(maxsize=8)
def _get_language(name: str) -> Language:
    if name == "java":
        return Language(ts_java.language())
    if name == "python":
        return Language(ts_py.language())
    if name == "go":
        return Language(ts_go.language())
    if name == "javascript":
        return Language(ts_js.language())
    if name == "typescript":
        return Language(ts_ts.language_typescript())
    if name == "tsx":
        return Language(ts_ts.language_tsx())
    raise ValueError(f"unknown language: {name}")


def language_for_path(path: str) -> str | None:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return _LANG_BY_EXT.get(ext)


# --------------------------------------------------------------------------- #
# Rule library
# --------------------------------------------------------------------------- #
# Each rule is a tree-sitter query. The query just needs to MATCH; we don't
# care about which capture fires. The names below correspond to entries in
# the `dependencies[].kind` enum from catalog_schema.json.
#
# Rules are intentionally inclusive — we want to confirm the *category* of
# operation, not pin down the exact API. False positives at this stage are
# preferable to false negatives (the calibrator treats Phase B as a *boost*
# signal, not an authority).


JAVA_RULES = {
    # Async publish — RabbitMQ, Kafka, JMS, ActiveMQ, AMQP, SQS/SNS,
    # plus generic *.publish/*.send/*.basicPublish patterns.
    "producesMessage": r"""
        [
          (annotation name: (identifier) @n
            (#match? @n "^(RabbitProducer|MessageMapping|EventListener)$"))
          (method_invocation
            name: (identifier) @m
            (#match? @m "^(send|sendAndReceive|convertAndSend|convertAndSendAsync|basicPublish|publish|publishEvent|emit|push|enqueue|sendMessage)$"))
          (method_invocation
            object: (_) @obj
            (#match? @obj "(?i)(template|producer|broker|publisher|channel|client|emitter|bus|amqp|jms|kafka|rabbit|sns|sqs|eventbridge)$"))
        ] @hit
    """,
    # Async consume — listener annotations + manual consume calls
    "consumesMessage": r"""
        [
          (annotation name: (identifier) @n
            (#match? @n "Listener$|^Subscribe$|^EventHandler$|^StreamListener$|^RabbitHandler$|^MessageMapping$"))
          (marker_annotation name: (identifier) @n2
            (#match? @n2 "Listener$|^RabbitHandler$"))
          (method_invocation
            name: (identifier) @m
            (#match? @m "^(receive|consume|poll|basicConsume|onMessage|subscribe|listen)$"))
        ] @hit
    """,
    # HTTP outbound call
    "consumesApi": r"""
        [
          (annotation name: (identifier) @n
            (#match? @n "^(FeignClient|HttpExchange|RestClient)$"))
          (method_invocation
            name: (identifier) @m
            (#match? @m "^(getForObject|getForEntity|postForObject|postForEntity|put|patch|delete|exchange|execute|retrieve|toBodilessEntity|toBody|toFlux|toMono|build|send|sendAsync|newCall|fetch|call)$"))
          (method_invocation
            object: (_) @obj
            (#match? @obj "(?i)(resttemplate|webclient|httpclient|okhttp|client|feign|api|service)$"))
        ] @hit
    """,
    # Reads a persisted resource (DB, cache, document store)
    "readsResource": r"""
        [
          (annotation name: (identifier) @n
            (#match? @n "^(Repository|Entity|Table|Document|RedisHash|Cacheable|Query|Select)$"))
          (method_invocation
            name: (identifier) @m
            (#match? @m "^(find|findAll|findBy|findOne|get|select|read|query|fetch|load|exists|count|stream|page|search)"))
        ] @hit
    """,
    # Writes a persisted resource
    "writesResource": r"""
        [
          (annotation name: (identifier) @n
            (#match? @n "^(Repository|Entity|Table|Document|Modifying|Transactional|CacheEvict|CachePut|Insert|Update|Delete)$"))
          (method_invocation
            name: (identifier) @m
            (#match? @m "^(save|saveAll|insert|update|delete|deleteAll|put|set|persist|merge|create|upsert|store|write|drop|truncate|flush)"))
        ] @hit
    """,
    # Import / dependency edge — for dependsOn we just want any import.
    "dependsOn": r"""
        (import_declaration) @hit
    """,
}


PYTHON_RULES = {
    "producesMessage": r"""
        [
          (call function:
            (attribute attribute: (identifier) @m
              (#match? @m "^(basic_publish|publish|send|send_message|emit|put_nowait|put|produce|enqueue|push)$")))
          (call function:
            (identifier) @f
            (#match? @f "^(publish|emit|produce)$"))
        ] @hit
    """,
    "consumesMessage": r"""
        [
          (call function:
            (attribute attribute: (identifier) @m
              (#match? @m "^(basic_consume|consume|subscribe|poll|on_message|receive|recv|get|listen|run_forever)$")))
          (decorator (call function:
            (attribute attribute: (identifier) @d
              (#match? @d "(?i)(consumer|subscriber|listener|handler|task|on_event)$"))))
          (decorator (call function:
            (identifier) @d2
            (#match? @d2 "(?i)^(task|listener|consumer|subscriber)$")))
        ] @hit
    """,
    "consumesApi": r"""
        [
          (call function:
            (attribute attribute: (identifier) @m
              (#match? @m "^(get|post|put|patch|delete|head|request|fetch|send|execute|call|invoke)$")))
          (call function:
            (identifier) @f
            (#match? @f "^(get|post|put|patch|delete|fetch|request)$"))
        ] @hit
    """,
    "readsResource": r"""
        [
          (call function:
            (attribute attribute: (identifier) @m
              (#match? @m "^(find|find_one|find_all|fetchone|fetchall|fetchmany|execute|query|select|get|read|load|exists|count|aggregate)$")))
        ] @hit
    """,
    "writesResource": r"""
        [
          (call function:
            (attribute attribute: (identifier) @m
              (#match? @m "^(insert|insert_one|insert_many|update|update_one|update_many|delete|delete_one|delete_many|save|set|put|create|drop|commit|execute|merge|upsert|replace_one)$")))
        ] @hit
    """,
    "dependsOn": r"""
        [
          (import_statement) @hit
          (import_from_statement) @hit
        ]
    """,
}


GO_RULES = {
    "producesMessage": r"""
        (call_expression
          function: (selector_expression
            field: (field_identifier) @m
            (#match? @m "^(Publish|PublishWithContext|SendMessage|Send|Produce|Push|Enqueue|Emit|PublishMessage|BasicPublish|Put)$"))) @hit
    """,
    "consumesMessage": r"""
        (call_expression
          function: (selector_expression
            field: (field_identifier) @m
            (#match? @m "^(Consume|Subscribe|Receive|ReceiveMessage|ReceiveMessages|Poll|OnMessage|Listen|HandleFunc|HandleMessage)$"))) @hit
    """,
    "consumesApi": r"""
        (call_expression
          function: (selector_expression
            field: (field_identifier) @m
            (#match? @m "^(Get|Post|Put|Patch|Delete|Do|Send|Call|Invoke|NewRequest|RequestWithContext)$"))) @hit
    """,
    "readsResource": r"""
        (call_expression
          function: (selector_expression
            field: (field_identifier) @m
            (#match? @m "^(Query|QueryRow|QueryContext|Find|FindOne|Get|Scan|Read|Select|Fetch|Load|Exists|Count)$"))) @hit
    """,
    "writesResource": r"""
        (call_expression
          function: (selector_expression
            field: (field_identifier) @m
            (#match? @m "^(Exec|ExecContext|Insert|InsertOne|InsertMany|Update|UpdateOne|UpdateMany|Delete|DeleteOne|DeleteMany|Save|Set|Put|Drop|Commit|Upsert|Create)$"))) @hit
    """,
    "dependsOn": r"""
        (import_declaration) @hit
    """,
}


# JavaScript/TypeScript share the same syntax shape — reuse identical
# queries. Tree-sitter parses both with the same grammar family.
JS_RULES = {
    "producesMessage": r"""
        (call_expression
          function: (member_expression
            property: (property_identifier) @m
            (#match? @m "^(publish|send|sendMessage|emit|push|enqueue|produce|basicPublish|publishMessage|broadcast)$"))) @hit
    """,
    "consumesMessage": r"""
        (call_expression
          function: (member_expression
            property: (property_identifier) @m
            (#match? @m "^(consume|subscribe|on|onMessage|receive|listen|handle|run|process)$"))) @hit
    """,
    "consumesApi": r"""
        [
          (call_expression
            function: (identifier) @f
            (#match? @f "^(fetch|axios|request|got|superagent|undici)$"))
          (call_expression
            function: (member_expression
              property: (property_identifier) @m
              (#match? @m "^(get|post|put|patch|delete|head|request|send|fetch|call)$")))
        ] @hit
    """,
    "readsResource": r"""
        (call_expression
          function: (member_expression
            property: (property_identifier) @m
            (#match? @m "^(find|findOne|findById|findAll|get|select|query|read|fetch|aggregate|count|exists)$"))) @hit
    """,
    "writesResource": r"""
        (call_expression
          function: (member_expression
            property: (property_identifier) @m
            (#match? @m "^(insert|insertOne|insertMany|update|updateOne|updateMany|delete|deleteOne|deleteMany|save|set|put|create|upsert|replaceOne|drop)$"))) @hit
    """,
    "dependsOn": r"""
        [
          (import_statement) @hit
          (lexical_declaration (variable_declarator value: (call_expression function: (identifier) @r (#eq? @r "require")))) @hit2
        ]
    """,
}


_RULES_BY_LANG: dict[str, dict[str, str]] = {
    "java": JAVA_RULES,
    "python": PYTHON_RULES,
    "go": GO_RULES,
    "javascript": JS_RULES,
    "typescript": JS_RULES,
    "tsx": JS_RULES,
}


@lru_cache(maxsize=64)
def _get_query(language: str, kind: str) -> Query | None:
    rules = _RULES_BY_LANG.get(language)
    if not rules:
        return None
    text = rules.get(kind)
    if not text:
        return None
    return Query(_get_language(language), text)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=512)
def _parse(repo_root_str: str, rel_path: str) -> tuple[bytes, Any, str] | None:
    """Return (source_bytes, tree.root_node, language_name) or None.

    Cached to avoid re-parsing each file once per evidence item that cites
    it. Per-process cache; cleared on `clear_cache()` for tests.
    """
    repo_root = Path(repo_root_str)
    candidate = repo_root / rel_path
    try:
        resolved = candidate.resolve()
        resolved.relative_to(repo_root.resolve())
    except (ValueError, OSError):
        return None
    if not resolved.is_file():
        return None
    lang_name = language_for_path(rel_path)
    if lang_name is None:
        return None
    try:
        src = resolved.read_bytes()
    except OSError:
        return None
    parser = Parser(_get_language(lang_name))
    tree = parser.parse(src)
    return src, tree.root_node, lang_name


def clear_cache() -> None:
    _parse.cache_clear()
    _get_query.cache_clear()


# --------------------------------------------------------------------------- #
# Reusable types
# --------------------------------------------------------------------------- #


@dataclass
class AstEvidenceCheck:
    path: str
    line: int
    status: str
    reason: str = ""


@dataclass
class AstFactCheck:
    index: int
    label: str
    kind: str
    evidence_total: int = 0
    confirmed: int = 0
    mixed: int = 0
    disconfirmed: int = 0
    unsupported: int = 0
    details: list[AstEvidenceCheck] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if self.evidence_total == 0:
            return "empty"
        if self.unsupported == self.evidence_total:
            return "unsupported"
        if self.confirmed > 0 and self.disconfirmed == 0:
            return "confirmed"
        if self.confirmed > 0 and self.disconfirmed > 0:
            return "mixed"
        if self.mixed > 0 and self.disconfirmed == 0:
            return "mixed"
        # No confirmation, only mixed/disconfirmed → disconfirmed.
        if self.confirmed == 0 and (self.disconfirmed > 0 or self.mixed > 0):
            return "disconfirmed"
        return "mixed"


@dataclass
class AstReport:
    facts: list[AstFactCheck] = field(default_factory=list)

    @property
    def total_facts(self) -> int:
        return len(self.facts)

    @property
    def confirmed_facts(self) -> int:
        return sum(1 for f in self.facts if f.verdict == "confirmed")

    @property
    def disconfirmed_facts(self) -> int:
        return sum(1 for f in self.facts if f.verdict == "disconfirmed")

    @property
    def mixed_facts(self) -> int:
        return sum(1 for f in self.facts if f.verdict == "mixed")

    @property
    def unsupported_facts(self) -> int:
        return sum(1 for f in self.facts if f.verdict == "unsupported")

    def to_dict(self) -> dict[str, Any]:
        return {
            "totals": {
                "facts": self.total_facts,
                "facts_confirmed": self.confirmed_facts,
                "facts_mixed": self.mixed_facts,
                "facts_disconfirmed": self.disconfirmed_facts,
                "facts_unsupported": self.unsupported_facts,
            },
            "facts": [
                {
                    "index": f.index,
                    "label": f.label,
                    "kind": f.kind,
                    "verdict": f.verdict,
                    "evidence_total": f.evidence_total,
                    "confirmed": f.confirmed,
                    "mixed": f.mixed,
                    "disconfirmed": f.disconfirmed,
                    "unsupported": f.unsupported,
                    "details": [
                        {"path": d.path, "line": d.line, "status": d.status, "reason": d.reason}
                        for d in f.details
                    ],
                }
                for f in self.facts
            ],
        }


# --------------------------------------------------------------------------- #
# Per-evidence check
# --------------------------------------------------------------------------- #


def _node_in_window(node: Node, line: int) -> bool:
    start = node.start_point[0] + 1  # 0-indexed → 1-indexed
    end = node.end_point[0] + 1
    return (line - SEARCH_WINDOW_BEFORE) <= start <= (line + SEARCH_WINDOW_AFTER) or (
        start <= line <= end
    )


def _check_one_evidence(
    repo_root: Path, rel_path: str, line: int, kind: str
) -> AstEvidenceCheck:
    parsed = _parse(str(repo_root), rel_path)
    if parsed is None:
        return AstEvidenceCheck(rel_path, line, "unsupported", "file unparseable or unknown language")
    src, root, lang_name = parsed
    query = _get_query(lang_name, kind)
    if query is None:
        return AstEvidenceCheck(rel_path, line, "unsupported", f"no rule for ({lang_name}, {kind})")

    cursor = QueryCursor(query)
    matches = cursor.matches(root)
    if not matches:
        return AstEvidenceCheck(rel_path, line, "disconfirmed", "no AST match in file")

    in_window = False
    elsewhere = False
    for _match_id, captures in matches:
        for _cap_name, nodes in captures.items():
            node_list = nodes if isinstance(nodes, list) else [nodes]
            for node in node_list:
                if _node_in_window(node, line):
                    in_window = True
                    break
                else:
                    elsewhere = True
            if in_window:
                break
        if in_window:
            break

    if in_window:
        return AstEvidenceCheck(rel_path, line, "confirmed")
    if elsewhere:
        return AstEvidenceCheck(rel_path, line, "mixed", "pattern present in file but not near cited line")
    return AstEvidenceCheck(rel_path, line, "disconfirmed", "no AST match in file")


def _label_for_dep(dep: dict[str, Any]) -> str:
    src = dep.get("source", "?")
    tgt = dep.get("target", "?")
    kind = dep.get("kind", "?")
    return f"{src} -{kind}-> {tgt}"


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def verify_payload(payload: dict[str, Any], repo_root: Path) -> AstReport:
    """Run Phase B AST cross-check over all `dependencies` in a payload.

    Only `dependencies[]` are checked. Other categories are out of scope
    for Phase B; their grounding rests on Phase A.
    """
    facts: list[AstFactCheck] = []
    for idx, dep in enumerate(payload.get("dependencies") or []):
        if not isinstance(dep, dict):
            continue
        kind = dep.get("kind") or ""
        check = AstFactCheck(index=idx, label=_label_for_dep(dep), kind=kind)
        for ev in dep.get("evidence") or []:
            if not isinstance(ev, dict):
                continue
            raw_path = (ev.get("path") or "").lstrip("/")
            line = ev.get("line")
            if not raw_path or not isinstance(line, int) or line <= 0:
                check.evidence_total += 1
                check.unsupported += 1
                check.details.append(
                    AstEvidenceCheck(raw_path, line or 0, "unsupported", "invalid evidence shape")
                )
                continue
            check.evidence_total += 1
            result = _check_one_evidence(repo_root, raw_path, line, kind)
            check.details.append(result)
            if result.status == "confirmed":
                check.confirmed += 1
            elif result.status == "mixed":
                check.mixed += 1
            elif result.status == "unsupported":
                check.unsupported += 1
            else:
                check.disconfirmed += 1
        facts.append(check)
    return AstReport(facts=facts)
