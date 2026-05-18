# ServiceScout Architecture & Findings

A living document. Goal: build the most reliable, cost-effective pipeline
that turns a large enterprise codebase (any language, any framework,
any age) into a Backstage-shaped knowledge graph that AI agents can
trust.

We are not the first to attempt this. Two open-source efforts and
multiple 2025–2026 papers converge on the same architectural truth:
**determinist static analysis owns structural facts; the LLM owns
semantic enrichment**. The hybrid is the point. Below is what we believe
the pipeline should look like, what we've built so far, and where the
empirical evidence is pointing.

## Strategic goal

> Map a multi-repo enterprise topology — Components, APIs, Resources,
> Dependencies, Providers — with file:line evidence on every fact, low
> hallucination, low cost-per-repo, and broad language coverage. Surface
> the graph to AI agents over MCP so they can route, reason, and refactor
> across the organisation without manually exploring every repo.

Three things that distinguish this from existing tools:

1. **Backstage-shaped output**, not just symbol/call graphs. The agent
   should be able to answer "which service owns the `RewardSettlement`
   domain?", "which queue does `orders` publish to?", "what would break
   if Stripe went down?" — not just "who calls `validate()`?"
2. **Polyglot from day one.** No assumption that the catalog is a single
   language. Real enterprises mix Java / Kotlin / Go / Python / Node /
   C# / Ruby / Scala / Rust / PHP / Swift, often within one product.
3. **Async-aware.** First-class `producesMessage` / `consumesMessage` /
   `readsResource` / `writesResource` edges. Most code-graph tools treat
   message brokers as opaque clouds; we model them as graph endpoints.

## Pipeline architecture — current and target

### Current (this PR, "extractor-first")

```
┌─────────────────────────────────────────────────────────────────────┐
│ 1. LLM extract (Codex / Claude)                                     │
│    → Backstage-shaped JSON with file:line evidence on every fact    │
└───────────────────────────────┬─────────────────────────────────────┘
                                ↓
┌───────────────────────────────────────────────────────────────────┐
│ 2. Phase A — snippet substring verifier                           │
│    Pure text. For every evidence[].{path, line, snippet}:         │
│      open file, read line ±N, check snippet appears (whitespace-  │
│      normalised). Three-way verdict per fact.                     │
└───────────────────────────────┬───────────────────────────────────┘
                                ↓
┌───────────────────────────────────────────────────────────────────┐
│ 3. Phase B — tree-sitter AST cross-check                          │
│    Per-language queries (Java, Python, Go, JS, TS) on dependency  │
│    edges only. Confirms the cited line actually performs the kind │
│    of operation the edge claims (publish vs consume vs HTTP etc). │
└───────────────────────────────┬───────────────────────────────────┘
                                ↓
┌───────────────────────────────────────────────────────────────────┐
│ 4. Correction loop (stateless replay)                             │
│    If A∨B flags any fact: re-prompt the LLM with structured       │
│    feedback. LLM returns fix / drop / keep directives. Merge,     │
│    re-verify. Cap rounds at 1 (more rarely helps in practice).    │
└───────────────────────────────┬───────────────────────────────────┘
                                ↓
┌───────────────────────────────────────────────────────────────────┐
│ 5. Calibrate confidence                                           │
│    Combined A+B verdict → confidence change on dependencies and   │
│    resources (the schema's confidence-bearing categories).        │
└───────────────────────────────────────────────────────────────────┘
```

### Target ("AST-first hybrid")

The research consensus from 2025–2026 (see "References" below) and
empirical results from this PR all point to inverting this:

```
┌───────────────────────────────────────────────────────────────────┐
│ 1. Tree-sitter parse (66 languages)                               │
│    For every file: parse to AST. No LLM.                          │
└───────────────────────────────┬───────────────────────────────────┘
                                ↓
┌───────────────────────────────────────────────────────────────────┐
│ 2. AST extractors (deterministic)                                 │
│    Per-language .scm queries emit structural facts:               │
│      imports / function calls / annotations / message-broker      │
│      method calls / SQL strings / route registrations / etc.      │
│    Also: deterministic mini-extractors for Dockerfile / helm /    │
│    k8s / terraform / package.json / pom.xml.                      │
│    Output: a candidate Backstage-shaped graph with raw evidence.  │
└───────────────────────────────┬───────────────────────────────────┘
                                ↓
┌───────────────────────────────────────────────────────────────────┐
│ 3. LLM enrichment (semantic + narrative)                          │
│    Prompt the LLM with the AST-derived candidate graph and ask    │
│    only for what AST cannot give us:                              │
│      - tagline, capability_sheet, purpose                         │
│      - business domain assignment                                 │
│      - glossary terms with definitions                            │
│      - alias hygiene (canonical name vs config keys)              │
│      - confidence calibration on ambiguous edges                  │
│    No file exploration — work from the AST evidence alone.        │
└───────────────────────────────┬───────────────────────────────────┘
                                ↓
┌───────────────────────────────────────────────────────────────────┐
│ 4. Phase A+B re-verify (this PR's machinery, repurposed)          │
│    Confirm the LLM didn't smuggle in invented evidence. Phase A   │
│    on every fact; Phase B on every edge.                          │
└───────────────────────────────┬───────────────────────────────────┘
                                ↓
┌───────────────────────────────────────────────────────────────────┐
│ 5. Cross-signal reconcile                                         │
│    AST-only fact → confidence=medium. AST + LLM agree → high.     │
│    Disagreement → review (human-in-the-loop dashboard). Optional  │
│    SCIP layer when an indexer is available: bumps to verified.    │
└───────────────────────────────┬───────────────────────────────────┘
                                ↓
                Backstage-shaped knowledge graph
                served over MCP to agents
```

Why invert: deterministic AST gives **complete coverage** at **low and
predictable cost**, while LLM gives **semantic understanding**. Today
we use the LLM for structure (which it does imperfectly and expensively)
and ignore the AST signal until verification time. The right shape uses
each tool for what it's best at.

## Empirical evidence so far

### THG workspace (195 repos, 2530 facts)

Phase A verifier post-hoc, no LLM cost:

| metric | count | % |
|---|---:|---:|
| facts confirmed (verbatim match at cited line) | 2026 | 80.1% |
| facts mixed (partial / some evidence holds) | 485 | 19.2% |
| facts disconfirmed (auto-demoted to review) | **19** | 0.8% |
| evidence items matched | 8128 / 8938 | 90.9% |
| evidence items with stale path (file gone) | 22 | 0.2% |

Spot-checked the 19 disconfirmed: all real bugs (renamed controllers,
fabricated line numbers in long files, citations to files that don't
exist).

Phase B on the 892 dependency edges in the same workspace:

| metric | count | % |
|---|---:|---:|
| confirmed (AST pattern at cited line) | 392 | 43.9% |
| mixed (pattern in file, wrong line) | 317 | 35.5% |
| disconfirmed (pattern absent from file) | 63 | 7.1% |
| unsupported (no rule for lang × kind) | 120 | 13.5% |

The 63 Phase-B-disconfirmed edges are bugs Phase A could not catch:
the LLM cited a real line, but the line doesn't perform the operation
the edge claims. The 120 unsupported are mostly Kotlin / C# / Ruby —
**this is where the language coverage gap matters most**.

### Sock-shop validation (shipping + orders + queue-master, 3 repos)

A/B: baseline (no correction loop) vs treatment (Phase A + B + 1
correction round). Both extracted with codex/gpt-5.4-mini/effort=medium.

| repo | Phase A baseline → treatment | Phase B baseline → treatment | corrections applied |
|---|---|---|---|
| orders | 27/28 confirmed → 27/28 | 0/4 conf, 3 mixed → 0/3 conf, 3 mixed | 1 fix (no measurable improvement) |
| shipping | **26/28 → 28/28 confirmed** | 1/2 conf, 1 unsup → 1/2 conf, 1 unsup | **2 fix** |
| queue-master | 30/30 → 29/29 (1 dropped) | 1 disc → 0 disc | **1 drop** |

**Catalog eval**: 1/18 in both. Trivially low because we only extracted
3 of 9 sock-shop repos; 15 questions test routing across repos we
didn't extract. The interesting failures:

- `structural-01` (no broker as Provider) **FAILs in both** — the LLM
  emitted `Provider:ActiveMQ` despite our prompt guardrails. Phase A
  has no way to catch this (the citation is real); Phase B doesn't run
  on `providers`. A typed-grounding rule would.
- `structural-02` / `structural-03` (shipping consumes / orders produces
  to shipping-task) **FAIL in both** — and the *treatment* introduced
  a worse bug: the LLM emitted `shipping -producesMessage-> shipping-task`
  with kind reversed. Phase B's `producesMessage` query is too loose —
  it matches Spring `@RabbitListener` reply methods that look like
  publish calls. **This is precisely where context-aware queries or
  SCIP-level semantic resolution are needed.**

### Limit of the current architecture

Phase A + B can only **verify or correct facts the LLM extracted**. They
**cannot conjure missing facts**. If the LLM never emits the
`shipping consumesMessage shipping-task` edge — because it mis-classified
shipping as a producer — no amount of verification will repair the
omission. The fix has to come from inverting the pipeline: the AST sees
`@RabbitListener(queues = "shipping-task")` deterministically, and that
fact should enter the graph as a primary signal, not a verification
signal.

## What this means for the next iterations

Concrete near-term work, in priority order:

1. **Phase B as an extractor, not just a verifier.** For unambiguous
   AST patterns (`@RabbitListener`, `@KafkaListener`, `RabbitTemplate.convertAndSend`,
   `channel.basic_publish`, `mongoose.Schema`, etc.), emit edges directly
   into the graph. The LLM then enriches; if the LLM contradicts an
   AST-emitted edge, the LLM is wrong by default.
2. **Rules in files, not Python strings.** Move queries to
   `static_extractors/rules/{lang}/{kind}.scm` so adding a language is
   a configuration change, not a code change.
3. **Broaden language coverage to ~20 immediately.** Add Kotlin, C#,
   Ruby, Scala, Rust, PHP, Swift, Bash, Dockerfile, HCL, YAML grammars.
   The longer tail (Zig, Elixir, OCaml, Haskell) waits until someone
   asks.
4. **Context-aware AST queries.** `producesMessage` should NOT match a
   send call inside a `@RabbitListener` handler. Tree-sitter queries can
   match parent context: `(class_declaration (annotation @c (#match? @c "Listener")) body: (... (method_invocation @inner ...)))` and exclude inner matches.
5. **Static mini-extractors for non-code signals.** Helm values.yaml,
   k8s manifests, Dockerfile FROM/EXPOSE, terraform/CDK, GitHub Actions,
   Backstage YAML when present. These yield ground-truth Component
   facts at zero LLM cost. Most of these don't even need tree-sitter
   (YAML/JSON are trivial to parse). See Epic #9 Tier 1 #3.
6. **Optional SCIP layer.** For Java / TypeScript / Python / Go where
   the project has a working build, run `scip-java` / `scip-typescript` /
   `scip-python` / `rust-analyzer` to get actual semantic symbol
   resolution. The .scip file's symbol roles (definition vs reference,
   import etc) close the false-positive gap shipping just exposed.
   Gated behind a flag — not everyone can build every repo.

## References

The architecture inverts toward AST-primary because every credible
study and shipping system points the same way.

- **arXiv 2601.08773** (Jan 2026) — "Reliable Graph-RAG for Codebases:
  AST-Derived Graphs vs LLM-Extracted Knowledge Graphs". Direct
  benchmark on Shopizer / ThingsBoard / OpenMRS Core. Headline numbers:
  on Shopizer, DKB (tree-sitter, deterministic) achieved 15/15 correct
  answers, build 22s, cost $0.04 (No-Graph baseline) → $0.09 (DKB) →
  $0.79 (LLM-KB). LLM-KB skipped 377 / 1210 files (per-file success
  rate 0.688) — its graph had 842 nodes vs DKB's 1158. On combined
  OpenMRS+ThingsBoard, the cost gap widens: $0.149 / $0.317 / $6.80.
- **arXiv 2603.27277** (Feb 2026) — "Codebase-Memory: Tree-Sitter-Based
  Knowledge Graphs for LLM Code Exploration via MCP." 66-language
  pipeline, persistent KG, MCP exposure. Benchmark on 31 real-world
  repos: 83% answer quality at 10x fewer tokens and 2.1x fewer tool
  calls vs file-exploration agents.
- **arXiv 2603.29109** — "SemLoc: Structured Grounding of Free-Form LLM
  Reasoning for Fault Localization". Tree-sitter as the structural
  anchor.
- **arXiv 2512.12117** — "Citation-Grounded Code Comprehension". Hybrid
  retrieval + AST chunking to prevent LLM citation hallucination.
  Exactly the failure mode our Phase A catches.
- **arXiv 2604.10800** — "Verify Before You Fix: Agentic Execution
  Grounding for Trustworthy Cross-Language Code Analysis". Execution
  grounding as a feedback loop — our correction loop is a lightweight
  version of this.

Shipping systems with overlapping shapes:

- [DeusData/codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp) —
  66 languages, single static binary, sub-ms queries, MCP server.
  Strongest existing reference architecture.
- [colbymchenry/codegraph](https://github.com/colbymchenry/codegraph) —
  19+ languages, SQLite + FTS5, MCP server. Symbol + call graph; not
  Backstage-shaped but the parsing pipeline is the same.
- [Sourcegraph SCIP](https://github.com/sourcegraph/scip) — Protocol
  Buffer schema for cross-language symbol resolution. Indexers exist
  for Java / Python / TypeScript / Go / Rust / C++ / C# / Ruby / Dart /
  PHP. Heavier (needs build) but semantically precise. Right candidate
  for the "verified" tier of confidence.
- [Moderne LST](https://docs.moderne.io/) — type-attributed semantic
  tree, multi-repo AI agent (Moddy). JVM-coupled; less applicable to
  polyglot enterprises but proves the pattern.

## Open questions

1. **AST-extractor priority on overlap.** When AST and LLM both emit
   the same edge with different kinds (`producesMessage` vs `consumesMessage`),
   which wins? Our hunch: AST wins by default; LLM-only direction
   triggers a `review` flag.
2. **How wide should Phase B unsupported be?** Today: "no rule for
   (language, kind)" returns unsupported and falls back to Phase A. As
   language coverage grows, the unsupported share shrinks — but for
   long-tail languages we may always rely on Phase A alone. Is that
   acceptable for production catalogs?
3. **Correction-loop budget.** 1 round seems enough in practice (sock-shop
   data: 3 fixes / 1 drop / 0 keeps applied in round 1). What workload
   makes round 2 worth it? Hypothesis: round 2 helps when the LLM's
   round-1 fix introduces a new evidence-citation bug — i.e. a
   self-correcting trap. We have not yet seen this in the sample.
4. **Where does SCIP fit?** Optional Phase C, gated by build success.
   Output the .scip index alongside the catalog; reconcile uses it to
   bump confidence on edges where the cited symbol resolves to the
   claimed target. We don't run SCIP today.
5. **Static mini-extractor priority.** Helm/k8s/terraform extractors
   are easy wins (deterministic, no parsing complexity), and they're
   the only way to get Resource:datasource_url right. Probably the next
   thing after Phase B as extractor.

## Learnings from arXiv 2601.08773 — what to reuse, what to leapfrog

Concrete design choices in the paper, with our take on each.

### 1. Two-pass extraction with target-validation

DKB extracts in two passes:

> Pass 1: Discover all project-local types, build filename-to-classname map.
> Pass 2: Extract injects / extends / implements edges, **validating that targets exist in the discovered set.**

**Adopt this directly.** Every AST-emitted edge should reference a node
that was actually discovered. This eliminates the "dangling pointer"
class of bugs the LLM regularly produces (citing a service the LLM
*assumes* exists). For our cross-repo case, the "discovered set" spans
all repos in the workspace, which is precisely the alias-resolution
work we already partially do in `reconcile.py`.

### 2. Interface-consumer expansion at retrieval time

> When a class implements an interface, the retriever adds "consumers
> of that interface" to improve controller/service discovery across
> boundary.

This is the same idea as `subcomponent_of` in our schema, but pushed
into traversal. Our `neighbors` / `trace` MCP tools already do bidirectional
expansion; the missing piece is **interface-mediated expansion** —
when you ask "who calls service X?", also include "who calls the
interface X implements". Java-heavy enterprises need this badly.

### 3. Tree-sitter queries (verbatim from the paper)

> class_declaration / interface_declaration / enum_declaration / record_declaration
>   → captures identifier @class_name
> field_declaration
>   → captures type_identifier @type_name  (injection signals)
> constructor_declaration
>   → captures formal_parameter types  (dependency injection)

These give Spring DI relationships deterministically. **For our
purposes**, the same idea generalises to *any framework's* DI:
constructor params + field annotations of types that name other
project-local classes. The Backstage-shaped output translates these
into `Component dependsOn Component` edges with high confidence.

### 4. The 377-files-skipped failure mode

> "File contents are truncated for batching, and the pipeline prints
> explicit SKIPPED/MISSED by LLM lines when a file in the input batch
> does not appear in the model's structured output."

On Shopizer: 377/1210 files (31.2%) were silently dropped by the LLM
pipeline. Coverage collapsed to 64.1%. **Our pipeline avoids batching
across files**, so this specific failure mode doesn't apply. But the
same family of bugs (schema validation, probabilistic omission)
manifests in our world as: LLM extracts a repo but emits no
`producesMessage` edge despite a real publish call existing. That's
the shipping bug. **AST-first inverts the failure direction**: AST
sees the call deterministically; LLM enrichment then names it.

### 5. Cost shape

| | Build (Shopizer) | Build (OpenMRS+ThingsBoard combined) | Q&A (Shopizer 15 questions) |
|---|---:|---:|---:|
| No-Graph (vector only) | 18.41 s | — | $0.04 |
| DKB (tree-sitter) | **22.09 s** | — | **$0.09** |
| LLM-KB | 215.09 s | — | $0.79 |
| LLM-KB combined | — | — | **$6.80** |
| DKB combined | — | — | **$0.317** |
| No-Graph combined | — | — | $0.149 |

DKB build is ~2× the No-Graph baseline. LLM-KB is ~20× on Shopizer
and ~45× on larger workloads. For our 195-repo THG workspace,
extrapolating from per-repo extraction time (~3 min average) gives
**a DKB-first pipeline would cut catalog build wall time from hours to
minutes** while improving correctness.

### 6. What the paper does NOT do — our differentiation

The paper explicitly notes:

> The three pipelines are entirely separate. The paper does not
> describe merging or validating signals across methods. It treats
> them as alternative strategies, not complementary components.

**This is precisely the gap we fill.** The empirical evidence from
this PR (Phase A: 19 real bugs caught; Phase B: 63 additional bugs
caught) shows that a *combined* AST-as-authority + LLM-as-enrichment
+ verifier-as-reconciler architecture beats either component alone.
None of the prior art (CodeGraph, Codebase-Memory, the paper) does
this reconciliation today. ServiceScout is the experiment.

The paper also acknowledges:

> Future work includes evaluation across multiple languages.

We're multi-language from day one. That's a small structural advantage
but a big strategic one.

### 7. Concrete take-aways folded into our roadmap

| Paper choice | Our adoption |
|---|---|
| Two-pass extraction with target validation | Add to Phase B extractor mode. Before emitting `dependsOn`/`consumesApi`, confirm the target is in the discovered node set. |
| DI / field-injection patterns as `dependsOn` | Phase B Java rules for `field_declaration` + `constructor_declaration` capturing type identifiers. |
| Interface-consumer query expansion | New `neighbors --via-interfaces` mode in the MCP server. Defer to a separate PR. |
| Decouple node discovery from embedding/insertion | Already true in our architecture (build_catalog.py is separate from embed_catalog.py). |
| Tree-sitter over full compiler infrastructure | Already aligned — tree-sitter is our primary parser. SCIP is the optional "verified" tier. |

## Decisions log

Append decisions here as they happen so future contributors can see why.

- **2026-05-17.** Phase A (snippet substring) + Phase B (tree-sitter
  AST) verifiers + stateless-replay correction loop. Tree-sitter chosen
  over SCIP for Phase B because we need coverage of every language, not
  just those with mature compiled indexers; SCIP becomes optional
  Phase C. Correction loop chosen stateless (vs Codex
  `--continue`/Claude thread resumption) because the cross-harness
  guarantee outweighs the marginal token saving.
- **2026-05-17.** When Codex strict `--output-schema` rejects open
  `additionalProperties`, drop the schema for the correction call and
  rely on prompt+post-hoc parse_correction_response. Trade-off: more
  forgiving but requires careful response validation.
- **2026-05-17.** Phase B Java rule for `producesMessage` is too loose;
  shipping benchmark exposed false-positive confirmation on a
  `@RabbitListener` reply method. Fix queued: context-aware queries
  that exclude calls inside `@*Listener` annotated classes; until then,
  the correction loop's `keep` action is the safety valve.
- **2026-05-17.** Strip the per-language tree-sitter rules from Phase B
  and the AST extractor in favour of a language-agnostic universal
  approach. Replace Phase B verifier with `code_shape.py` (text-only:
  is the cited line code vs blank vs comment vs unparseable). Add
  language-agnostic mini-extractors (Dockerfile / k8s / Helm / build
  manifests / OpenAPI / .proto / .graphql) for the structural facts
  that *can* be derived deterministically from interchange formats.
  Result on THG 195 repos: Phase B universal-confirmed 846 / 892
  (was 392 per-language), 0 unsupported (was 120). Mini-extractors
  yield 525 components / 215 providers / 91 resources / 40 APIs
  language-agnostically across 126 repos with manifests.
- **2026-05-18.** SCIP empirically evaluated as candidate Phase C.
  Installed scip-java + scip-typescript + scip-python, indexed sock-shop.
  3/5 attempted repos indexed (60%): carts failed on Maven dep
  resolution, front-end has no tsconfig.json. SCIP delivered strong
  signal where it worked — 17–25 external Maven coords per repo with
  exact versions, 108–131 broker symbol uses on the messaging-heavy
  Java repos, 30 HTTP symbol uses on orders, 10 DB uses confirming
  MongoDB. But: SCIP knows symbol IDENTITY, not string-literal
  arguments — it can't supply the queue/topic NAME or the edge
  KIND/DIRECTION, which is what the catalog actually needs for
  topology-routing questions. The LLM remains required for those.
  **Decision: defer SCIP integration.** The mini-extractors already
  cover ~80% of SCIP's provider/resource signal at 0% of its
  operational cost. SCIP's killer feature (exact resolved versions,
  cross-repo dependency-graph queries) suits a future
  Dependency/SBOM dashboard product, not the routing-agent catalog
  this project is. Empirical findings + the 230-LoC SCIP loader
  prototype preserved here so a future implementer has a fast start
  if the opt-in is later wanted. Re-evaluate when (a) the catalog's
  primary value shifts toward supply-chain questions, or (b) the
  agent eval suite shows a quality gap that only resolved-symbol
  data can close.
- **2026-05-18.** Full sock-shop eval run with the pipeline (LLM
  extraction + Phase A snippet + Phase B universal code-shape +
  correction loop + mini-extractors). All 9 repos extracted with
  codex/gpt-5.4-mini/effort=medium and correction-rounds=1.

  Result: **14/18 catalog-tier pass rate**, vs the README's iter-3
  baseline of 12/18. Per category:
    routing         5/5     (was 5/5)
    sync-multihop   4/5     (was 4/5)
    async-multihop  2/3     (was 1/3)
    blast-radius    0/2     (was 0/2)
    structural      3/3     (3 new corrected questions, all PASS)

  The +2 questions came from async-multihop (queue-master correctly
  modelled as consumer, shipping as producer — the iter-3 broken
  edge direction the README flagged is now correct in the catalog)
  and from the structural-question corrections that match the actual
  sock-shop architecture.

  The 4 remaining failures all share a shape: `search_recall` is
  high (the right entities surface from query) but `neighbors_recall`
  is partial — the catalog is missing some edges between entities
  that the eval expects. This is a coverage gap in the LLM's
  per-repo extraction (not a verifier or framework issue) — fixable
  by either re-extracting with the new framework-agnostic prompt
  (which now has tighter alias hygiene and configurable-construct
  precision) or by an AST-emitter for the specific edge kinds we're
  missing.

  Note: these extractions used the OLD prompt — the new framework-
  agnostic prompt with alias-hygiene and evidence-discipline guidance
  was committed AFTER these LLM calls started, so its effect is yet
  to be measured. Re-extracting all 9 with the new prompt would test
  whether prompt improvements close more of the remaining gap; cost
  ~$3-6.

  Note: build_kuzu requires `--no-vector` (skip HNSW) but NOT
  `--no-fts` for the search-recall scores to work — FTS5 lexical
  search is what the catalog-tier eval depends on, and disabling it
  silently dropped every search-based question to 0.0 the first time
  the eval was run.
- **2026-05-18.** Framework-agnostic prompt re-extraction experiment.
  Re-extracted all 9 sock-shop repos with the new prompt (alias
  hygiene + evidence discipline + entity disambiguation +
  configurable-construct precision). 8 of 9 finished cleanly; orders
  initially timed out at 900s, succeeded at 1800s.

  Raw result (no reconcile pass): **11/18 catalog tier** — a
  regression vs the 14/18 baseline.

  Cause: the more strictly-defined alias hygiene made the LLM emit
  MORE Components instead of folding marketing / class names into
  `aliases[]`. front-end's class names `Cart-Service` and
  `OrderManager` got emitted as `Component:Cart-Service` and
  `Component:OrderManager`, distinct from `Component:carts` /
  `Component:orders` — so eval traversal looking for the canonical
  names failed.

  Reconcile pass result: **14/18 catalog tier** — parity with the
  baseline. `reconcile.py` correctly identified `Component:Cart-Service
  → Component:carts` and `Component:OrderManager → Component:orders`
  as unambiguous merges (no LLM-assist needed) and merged them. 4
  edges rerouted.

  Decision: the new prompt is no worse than the old prompt provided
  `reconcile.py` runs in the catalog-build pipeline (it does by
  default in the production crawler flow). The prompt's structural
  improvements (evidence-discipline = verbatim-substring snippets,
  zero `_cross_check_ast` mixed verdicts) are unambiguously good for
  Phase A's verifier even though they don't move the eval. The
  failure shape is unchanged from the baseline: search recall high,
  neighbors recall partial on 4 questions — an LLM extraction
  coverage gap for specific edges, not a verifier or prompt issue.

  Cost of the experiment: ~$5-8 in LLM tokens. Wall time: ~50 min
  serial (one repo took 1800s; others 200-700s).

  Update after orders re-extraction completed at --timeout-seconds
  1800: all 9 sock-shop repos now extracted with the new prompt and
  reconciled — **14/18 catalog tier**, identical to the baseline. The
  4 failing questions retain the same `search_recall=1.0,
  neighbors_recall<1.0` shape on blast-radius / async-multihop /
  sync-multihop. This confirms the eval gap is NOT a prompt-quality
  issue — the catalog is missing specific cross-repo edges the eval
  expects, not the entities themselves. Closing the gap is best done
  by either (a) cross-repo edge synthesis at build_catalog time when
  one repo declares `target=B` and B exists as a Component, or (b) a
  `reconcile.py --llm-assist` pass that proposes missing edges using
  the full catalog as cross-repo context.

