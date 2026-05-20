# Socks-Shop Scan Quality Review

Scan reviewed: clean socks-shop eval rebuild on 2026-05-19.
Updated after the generic reconciliation, audit-gate, dynamic dependency,
domain-normalization, and broker-modeling fixes on 2026-05-20.

Inputs inspected:

- `evals/data/catalog.json`
- `evals/data/extractions/*.json`
- `evals/data/extraction_runs.jsonl`
- `evals/runs/run_20260519_225840.json`
- pinned source under `evals/workspace/*`

External reference: Backstage's current software catalog docs describe
Component, API, and Resource as core software catalog entities, with Resources
covering infrastructure needed by a system:
https://backstage.io/docs/features/software-catalog/system-model and
https://backstage.io/docs/features/software-catalog/descriptor-format.

## Executive Verdict

The baseline scan was useful for agent routing and high-level service-flow
discovery, but it was not good enough to claim reliable Backstage-quality
catalog extraction.

After the current fixes, the structural catalog is materially cleaner: the
high-severity kind/provenance audit failures are gone, the catalog-tier eval is
18 / 18, and the catalog has zero Resource-as-Component or
Provider-as-Component duplicates. It is still not fully Backstage-quality
because dynamic dependency reconciliation and semantic confidence still need
more pipeline work.

What is strong:

- The extractor found the 9 cloned repositories and produced a connected
  catalog: 59 entities, 94 relations, 14 derived communication flows.
- Evidence validation is strong at the text level: 681 / 688 cited snippets
  matched the source files, with only 7 missing or partial snippets.
- The core service path is mostly recoverable:
  `front-end -> catalogue/carts/orders/user`, `orders -> payment/shipping`,
  `shipping -> shipping-task`, and `queue-master -> shipping-task`.
- The actual pinned-source async path is captured: shipping publishes to
  `shipping-task`; queue-master consumes it.

What is weak:

- Entity kind reconciliation is the biggest quality problem. Resources and
  providers are duplicated as Components because the catalog merge resolves
  dependency targets only through the Component alias index.
- Only 10 / 59 entities carry `source_repos`; APIs, Resources, Providers,
  Systems, and Domains generally lose source provenance in the merged catalog.
- Several inferred Components are not real deployable units in this workspace.
  Examples: `Address-Service`, `Card-Service`, `Customer-Service`,
  `Cart-Items-API`, `MongoDB-orders-data-store`, `shipping-task`,
  `shipping-task-exchange`, `Zipkin`, and `Google-Fonts`.
- API granularity is inconsistent. Some repos produce one service contract,
  while shipping emits separate APIs per route (`createShipping`,
  `getShippingById`, `listShipping`, `health`, `metrics`).
- Confidence is over-optimistic. Evidence presence is being treated as semantic
  correctness. A line can exist and still support the wrong entity kind or the
  wrong catalog abstraction.

## Run Metrics

| Metric | Value |
| --- | ---: |
| Repos indexed | 9 |
| Entities | 59 |
| Relations | 94 |
| Derived communication flows | 14 |
| Dropped for missing evidence | 0 |
| Catalog eval | 14 / 18 |
| Extractor estimated cost | $2.25 |
| Extractor wall time | ~42 minutes |
| Text evidence match | 681 / 688 |

Detailed extraction accounting from `evals/data/extraction_runs.jsonl`:

| Metric | Value |
| --- | ---: |
| Repositories extracted | 9 |
| Estimated LLM cost | $2.251965 |
| Wall time | 2,537.032 seconds |
| Input tokens | 5,234,225 |
| Billable input tokens | 431,025 |
| Cached input tokens | 4,803,200 |
| Output tokens | 348,546 |
| Reasoning output tokens | 244,989 |

Per-repo cost ranged from $0.151884 for `microservices-demo/load-test` to
$0.397058 for `microservices-demo/front-end`. The post-fix rebuilds used
`--skip-extraction`, so the deterministic merge/audit/eval iterations added no
new LLM spend.

## Post-Fix Rebuild

Rebuild command sequence:

```bash
./evals/build_eval_catalog.sh --skip-extraction
python build_kuzu.py --catalog evals/data/catalog.json --db evals/data/catalog.kuzu
python evals/runner.py --catalog evals/data/catalog.json --no-plots
python evals/report.py --current evals/runs/run_20260520_091528.json
python evals/audit_catalog.py --catalog evals/data/catalog.json --workspace evals/workspace --fail-on-high
```

Before and after:

| Metric | Baseline | After fix |
| --- | ---: | ---: |
| Entities | 59 | 44 |
| Components | 23 | 10 |
| APIs | 17 | 16 |
| Resources | 7 | 8 |
| Domains | 8 | 5 |
| Providers | 2 | 3 |
| Relations | 94 | 76 |
| Derived communication flows | 14 | 9 |
| Static API specs merged | 0 | 1 |
| Static API operations added | 0 | 9 |
| Derived broker resources | 0 | 5 |
| Demoted infrastructure Components | 0 | 1 |
| Normalized API fragments | 0 | 4 |
| Unresolved external Components | 13 | 5 |
| Catalog eval | 14 / 18 | 18 / 18 |
| Audited facts | 143 | 113 |
| Audit pass | 77 | 111 |
| Audit review | 19 | 2 |
| Audit fail | 47 | 0 |
| High-severity audit findings | 47 | 0 |
| Medium-severity audit findings | 19 | 2 |

The two remaining audit reviews are both partial-evidence checks on
`API:Operational-API` and `Component:orders -> API:Operational-API`.

The rebuild also demotes caller-supplied request-body URLs from service
Components to review-confidence dynamic API placeholders. For example,
`orders -> item.address/item.card/item.customer` now targets dynamic APIs, and
`orders -> item.items` resolves to `API:Items-API` rather than inventing a
`Cart-Items-API` service.

The rebuild also demotes infrastructure-only database/container-image
Components when a matching Resource already exists. For example, `user-db` is
now represented by `Resource:MongoDB-users-database`, and `Component:user`
depends on that Resource rather than on a database Component.

The rebuild enriches API operations from source API specs when a spec title
strongly matches an existing API. In this run it added the missing user service
`/cards`, `/cards/{id}`, `/addresses`, and `/addresses/{id}` style operations
without adding duplicate API entities.

The rebuild adds `Resource:rabbitmq` as an inferred broker resource linked from
RabbitMQ queue/topic resources. Queue/topic resources remain the precise
endpoints; the broker resource exists so blast-radius questions such as
"RabbitMQ goes down" can resolve affected publishers and consumers without
modeling RabbitMQ as a Provider or Component.

Domain labels now collapse obvious free-text drift: `ecommerce`,
`e-commerce / shopping carts`, and `e-commerce storefront` all normalize to
`Domain:e-commerce` with the original labels retained as aliases.

Route-level API fragments now collapse into a single contract when a component
emits many tiny one-route APIs. For example, `shipping` now exposes
`API:shipping-API` with five operations rather than five separate route APIs.

Quality gates now passing or review-only:

- No Resource-as-Component or Provider-as-Component duplicates.
- APIs and Resources carry source provenance.
- Resource relation target kinds are respected.
- Dynamic request-body URLs are not emitted as deployable Services.
- Infrastructure-only DB/cache images are not emitted as deployable Services
  when a matching Resource exists.
- Existing APIs are enriched from checked-in OpenAPI/Swagger specs when the
  match is unambiguous.
- Obvious free-text domain variants normalize to canonical Domain entities.
- RabbitMQ is modeled as a broker Resource, not as a Provider or Component.
- API granularity defaults to one contract when many route-level fragments are
  emitted for the same component.
- Relation endpoints exist.
- Cited evidence has no high-severity failures; two partial matches remain as
  medium review items.

The full per-fact audit is in `docs/socks-shop-catalog-audit.md`. The local
issue list is in `docs/socks-shop-quality-issues.md`.

Post-fix entity kinds:

| Kind | Count |
| --- | ---: |
| API | 16 |
| Component | 10 |
| Domain | 5 |
| Provider | 3 |
| Resource | 8 |
| System | 2 |

Post-fix relation types:

| Type | Count |
| --- | ---: |
| dependsOn | 16 |
| partOf | 16 |
| providesApi | 13 |
| readsResource | 5 |
| writesResource | 2 |
| consumesApi | 11 |
| consumesMessage | 2 |
| producesMessage | 2 |
| communicatesWith | 9 |

## What Worked Well

### 1. Source-grounded extraction mostly worked

The extractor cited real files and lines. The text verifier caught only a small
number of bad snippets:

- `front-end`: mixed evidence for `--domain`.
- `orders`: one partial evidence issue on `Operational API`.
- `user`: mixed evidence around `ZIPKIN`.

That is a good baseline. The problem is less hallucinated file references and
more semantic modeling.

### 2. Core service routing is useful

The catalog correctly surfaces the main services:

- `front-end`
- `catalogue`
- `carts`
- `orders`
- `payment`
- `shipping`
- `queue-master`
- `user`
- `load-test`

The service-flow graph is therefore useful for "which repos matter?" questions.
It is not yet a complete architecture graph.

### 3. The real async path is captured

The pinned source shows:

- `shipping` publishes to RabbitMQ queue/routing key `shipping-task` using
  `rabbitTemplate.convertAndSend("shipping-task", shipment)`.
- `queue-master` consumes `shipping-task` with
  `SimpleMessageListenerContainer` and `ShippingTaskHandler.handleMessage`.
- `orders` calls `shipping` over HTTP; it does not publish directly to
  RabbitMQ at these SHAs.

The extracted catalog captured `shipping -> Resource:shipping-task` as
`producesMessage` and `queue-master -> Resource:shipping-task` as
`consumesMessage`.

### 4. Domain attributes and glossary are rich

The run extracted 66 domain attributes and 136 glossary entries across the 9
repos. That gives search/retrieval more lexical hooks than basic dependency
graphs normally provide.

## What Was Bad

### 1. Resources and providers are duplicated as Components

Duplicate names across kinds:

| Name | Kinds emitted |
| --- | --- |
| `Google-Fonts` | Component, Provider |
| `Zipkin` | Component, Provider |
| `MongoDB-orders-data-store` | Component, Resource |
| `carts-db` | Component, Resource |
| `session-db` | Component, Resource |
| `shipping-task` | Component, Resource |
| `shipping-task-exchange` | Component, Resource |
| `shipping` | Component, Domain |

Root cause: `build_catalog.py` resolves deferred dependency targets through a
Component-only alias index. If a dependency says `target_kind=resource` or
`target_kind=provider`, the resolver can still miss the existing Resource or
Provider and create an external Component fallback.

This is why the graph feels like it is "just services": the flow graph is
derived from Component-to-Component edges, and the catalog has accidentally
promoted infrastructure/resources into Components.

### 2. Source provenance is too sparse

Merged catalog source attribution:

| Entity kind | With `source_repos` |
| --- | ---: |
| Component | 10 / 23 |
| API | 0 / 17 |
| Resource | 0 / 7 |
| Provider | 0 / 2 |

For agent use, this is a serious gap. If an agent lands on `API:Orders-API` or
`Resource:shipping-task`, it should immediately know which repo(s) to open.

### 3. Some inferred components are really payload links or endpoints

`orders` accepts caller-supplied links for customer, address, card, and cart
items. The scan converted those into separate Components:

- `Customer-Service`
- `Address-Service`
- `Card-Service`
- `Cart-Items-API`

The source supports "orders fetches these URIs", but it does not prove those
are standalone deployable services in this workspace. In this demo, several of
those URLs are better reconciled to `user` and `carts` API surfaces.

### 4. API entity granularity is inconsistent

Examples:

- `carts` emits `Cart-API`, `Items-API`, `Health-API`.
- `shipping` emits `listShipping`, `getShippingById`, `createShipping`,
  `health`, and `metrics` as separate APIs.
- `front-end` emits `Storefront-API`, `Account-API`, `Metrics-API`.

Backstage-style catalog quality needs a consistent rule. Usually this should
be one API entity per exposed contract, with operations attached under that
contract, unless the repo truly exposes multiple independently owned contracts.

### 5. Broker modeling is unresolved

The extractor correctly avoided emitting `Provider:RabbitMQ`, which is good:
RabbitMQ is transport, not a SaaS provider.

But user questions often say "RabbitMQ goes down". The current catalog has
RabbitMQ only as `Resource.spec.technology` on `shipping-task` and
`shipping-task-exchange`, so `Resource:RabbitMQ` eval expectations fail.

We need an explicit design choice:

- Keep brokers as transport only, then update evals and search expansion to
  treat "RabbitMQ" as a technology facet.
- Or create a broker-instance Resource such as `Resource:rabbitmq` and link
  queues/topics to it.

The post-fix catalog takes the second option: `Resource:rabbitmq` is derived
from queue/topic evidence and linked to publishers/consumers, while
`shipping-task` and `shipping-task-exchange` remain the precise messaging
resources.

### 6. Confidence labels are not semantic confidence

The catalog reports:

- Entities: 36 high, 23 medium.
- Relations: 75 high, 19 medium.

That looks better than the data deserves. Text evidence matching proves the
snippet exists; it does not prove that the target kind, canonical name, API
granularity, or ownership is right.

## Eval Interpretation

The baseline catalog-tier eval passed 14 / 18. The post-fix catalog-tier eval
passes 18 / 18.

The baseline failures split into two categories:

1. Stale or debatable eval expectations:
   - Some questions still expect `Resource:rabbitmq`.
   - Older comments expected `orders` to publish `shipping-task`, but the
     pinned source shows `orders` calls `shipping` over HTTP and `shipping`
     publishes the message.

2. Real catalog quality gaps that are fixed or captured in this pass:
   - `orders-db` is emitted as `MongoDB-orders-data-store`, so ground truth and
     catalog naming do not align. This now normalizes to `Resource:orders-db`.
   - RabbitMQ blast-radius traversal was weak because there was no broker node
     or broker technology expansion. This now resolves through
     `Resource:rabbitmq`.
   - Duplicate Resource-as-Component nodes polluted neighbors and flow edges.
     The post-fix audit has zero Resource-as-Component or Provider-as-Component
     duplicates.

## Recommended Quality Bar

Before treating this as Backstage-quality extraction, add these gates:

1. **No resource/provider component duplicates**
   - A name may not exist as both `Component` and `Resource` unless explicitly
     allowlisted.
   - A name may not exist as both `Component` and `Provider` unless explicitly
     allowlisted.

2. **Source provenance coverage**
   - 100% of API and Resource entities must carry `source_repos`.
   - 100% of relations extracted from repo code must carry `properties.source_repo`.

3. **Target-kind-aware resolution**
   - `target_kind=resource` must resolve to `Resource:*`, not `Component:*`.
   - `target_kind=provider` must resolve to `Provider:*`, not `Component:*`.
   - `target_kind=api` must resolve to `API:*`.
   - Only `target_kind=component` should create external Component fallbacks.

4. **Stable naming rules**
   - Database resources should prefer deployment/config names (`orders-db`,
     `carts-db`, `user-db`) with verbose labels as aliases.
   - Queue/topic resources should use the literal queue/topic names.
   - Systems/domains should be normalized by a controlled mapping, not free
     LLM wording.

5. **API granularity rules**
   - One API entity per contract by default.
   - Route-level detail belongs in `apis[].operations[]`.
   - Per-route APIs should require an explicit reason.

6. **Semantic confidence demotion**
   - Any unresolved target, target-kind mismatch, duplicate kind collision, or
     missing source repo should demote confidence to `review`.
   - "High" should mean both cited and semantically well-typed.

## Concrete Improvements

### Build Catalog Fixes

1. Build alias indexes per entity kind, not only for Components.
2. Resolve deferred dependencies using `target_kind`.
3. Stop creating external Components for resource/provider dependencies.
4. Attach `source_repos` to APIs, Resources, and Providers as they are created.
5. When a dependency duplicates a relation already created from `resources[]`,
   merge evidence/properties instead of adding a parallel Component fallback.

### Extractor Fixes

1. Tighten prompt rules:
   - databases, queues, topics, caches, and brokers are never Components.
   - providers are never Components.
   - caller-supplied URLs should be modeled as dynamic HTTP dependencies unless
     a concrete service identity is proven.
2. Add deterministic mini-extractors before the LLM:
   - Docker/Compose/Kubernetes: deployable Components and infra Resources.
   - OpenAPI/Swagger: API contracts and operations.
   - Spring Java: controllers, repositories, RabbitTemplate, listener
     containers, Mongo/JDBC config.
   - Node/Express: routes and outbound `request(...)`/endpoint config.
3. Feed deterministic findings to the LLM as constraints, not suggestions.

### Eval Fixes

1. Update RabbitMQ questions to match the chosen broker model.
2. Add structural assertions for:
   - no Resource-as-Component duplicates.
   - no Provider-as-Component duplicates.
   - API and Resource source provenance coverage.
   - no `protocol=unknown` when resource technology is known.
3. Keep the corrected async ground truth:
   - `orders -> shipping` via HTTP.
   - `shipping -> shipping-task` via RabbitMQ publish.
   - `queue-master -> shipping-task` via RabbitMQ consume.

### UI Fixes

1. Label the current graph as a **Service flow** view, not a general entity
   graph.
2. Add a separate **Catalog topology** view that shows APIs, Resources,
   Providers, Systems, and Domains even when they have no service-flow edge.
3. Show quality warnings in the Operator view:
   - duplicate entity names across kinds.
   - missing source provenance.
   - unresolved external components.
   - target-kind mismatches.

## Bottom Line

The LLM scan produced useful raw material and enough evidence to route agents
to the right repos for common service-flow questions. It did not produce a
clean, Backstage-grade catalog by itself.

The path forward is hybrid extraction:

- deterministic parsers establish typed facts and deployment topology;
- the LLM fills in summaries, domain vocabulary, and ambiguous dependency
  meaning;
- the merger enforces kind-aware reconciliation and quality gates;
- evals fail on catalog hygiene, not only on user-facing retrieval questions.

Until those gates exist, the product should be described as an evidence-backed
service intelligence prototype, not a reliable automated Backstage catalog.
