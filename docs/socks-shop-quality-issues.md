# Socks-Shop Catalog Quality Issues

This issue list is intentionally generic. The fixes should improve catalog
quality for any workspace, not just this public eval.

## Fixed in this pass

### QI-001: Resource and provider dependencies were reconciled as Components

- Severity: high
- Evidence: baseline audit found 47 high-severity findings, including
  `shipping-task`, `shipping-task-exchange`, `carts-db`, `session-db`,
  `Zipkin`, and `Google-Fonts` duplicated as Components.
- Root cause: deferred dependency resolution used a Component-biased alias
  lookup and created external Component fallbacks even when `target_kind` or
  relation type implied Resource or Provider.
- Fix: alias indexes are now maintained per entity kind; dependency resolution
  uses target kind preferences; unresolved Resource/API/Provider dependencies
  create typed review placeholders instead of Components.
- Acceptance criteria: `evals/audit_catalog.py --fail-on-high` reports zero
  high-severity findings on the rebuilt eval catalog.
- Regression test: `tests/test_catalog_reconciliation.py`.

### QI-002: API, Resource, and Provider entities lost source provenance

- Severity: high
- Evidence: baseline review showed API source provenance at 0/17, Resource at
  0/7, and Provider at 0/2.
- Root cause: only Component creation attached `metadata.annotations.source_repos`.
- Fix: API, Resource, and Provider constructors now attach source repos, and
  relevant relations carry `properties.source_repo` and `target_kind`.
- Acceptance criteria: all API and Resource entities in the eval catalog carry
  source provenance.
- Regression test: `tests/test_catalog_reconciliation.py`.

### QI-003: Relation evidence could be polluted by entity merges

- Severity: high
- Evidence: `Component:carts -> Provider:Zipkin` inherited evidence from other
  repos after `Provider:Zipkin` was merged.
- Root cause: relation evidence and entity evidence could share the same list
  object from an extraction payload.
- Fix: relation insertion now deep-copies relation payloads, and entity
  constructors copy evidence lists.
- Acceptance criteria: merging a shared Provider does not mutate already-added
  component-to-provider relation evidence.
- Regression test: `tests/test_catalog_reconciliation.py`.

### QI-004: Caller-supplied URLs were over-promoted to services

- Severity: high
- Evidence: `Address-Service`, `Card-Service`, `Customer-Service`, and
  `Cart-Items-API` appeared as service Components even though the source only
  showed `orders` fetching URLs supplied in the request body.
- Root cause: unresolved `consumesApi` dependencies defaulted to Component
  placeholders even when extraction notes said the concrete host/path was
  runtime supplied.
- Fix: caller/request-body supplied API dependencies now become
  review-confidence dynamic API placeholders. If an alias resolves to an
  existing API, the relation targets that API instead of inventing a service.
- Acceptance criteria: caller-supplied URLs are not emitted as deployable
  Components unless they reconcile to known workspace services or explicit
  config/deployment names.
- Regression test: `tests/test_catalog_reconciliation.py`.

### QI-005: RabbitMQ was not searchable as a blast-radius resource

- Severity: high
- Evidence: RabbitMQ appeared only as queue resource technology, so questions
  such as "If RabbitMQ goes down, what fails?" could not use `Resource:rabbitmq`
  as an entry point.
- Root cause: the catalog had no broker-resource facet over queue/topic
  resources.
- Fix: broker resources are now inferred from queue/topic technologies and
  linked from producers/consumers. RabbitMQ remains a Resource, not a Provider
  or Component.
- Acceptance criteria: `Resource:rabbitmq` exists, `shipping` publishes to it,
  `queue-master` consumes from it, and the RabbitMQ blast-radius eval passes.
- Regression test: `tests/test_catalog_reconciliation.py`.

### QI-006: Sock-shop async eval expectations were stale

- Severity: high
- Evidence: eval text expected `orders` to publish and `shipping` to consume
  RabbitMQ messages, but pinned source shows `orders -> shipping` over HTTP,
  `shipping` publishes `shipping-task`, and `queue-master` consumes it.
- Root cause: eval ground truth encoded an old mental model rather than source
  evidence.
- Fix: eval questions and canonical answers now match the pinned source.
- Acceptance criteria: async structural questions assert
  `shipping producesMessage shipping-task` and
  `queue-master consumesMessage shipping-task`.

### QI-007: Domain taxonomy had obvious free-text duplicates

- Severity: high
- Evidence: the eval catalog contained `e-commerce`, `ecommerce`,
  `e-commerce-shopping-carts`, and `e-commerce-storefront` as separate Domains.
- Root cause: per-repo LLM extraction emitted domain labels without a controlled
  vocabulary or merge-time normalization.
- Fix: obvious lexical variants normalize to one canonical Domain while the
  original labels are retained as aliases.
- Acceptance criteria: equivalent e-commerce labels collapse to
  `Domain:e-commerce`.
- Regression test: `tests/test_catalog_reconciliation.py`.

### QI-008: Concrete database host names were hidden behind verbose labels

- Severity: medium
- Evidence: orders emitted `Resource:MongoDB-orders-data-store`, while eval and
  source config use `orders-db`.
- Root cause: the merger trusted verbose LLM resource names even when a concrete
  host/config name was available.
- Fix: database/cache resources prefer concrete host/config labels when that is
  safe and does not collide with an existing component in the same repo.
- Acceptance criteria: orders emits `Resource:orders-db`.
- Regression test: `tests/test_catalog_reconciliation.py`.

### QI-009: Search under-ranked exact broker/resource aliases

- Severity: medium
- Evidence: `Resource:rabbitmq` existed and graph traversal worked, but vague
  async questions did not rank it into the top catalog search results.
- Root cause: search treated exact aliases/technology matches the same as
  ordinary lexical text and did not expand async/message vocabulary.
- Fix: search now boosts exact name/alias/technology matches and expands
  async/message/broker query terms.
- Acceptance criteria: the async catalog eval passes without increasing graph
  false positives.
- Regression test: `tests/test_storage_search.py`.

### QI-010: API granularity was inconsistent

- Severity: high
- Evidence: some repos emit one API per contract; `shipping` emits separate API
  entities for route-level operations such as `createShipping` and
  `getShippingById`.
- Root cause: extractor guidance does not yet enforce a stable contract-vs-route
  rule.
- Fix: the merger now collapses many tiny route-level API fragments from one
  component into one contract API with operations attached.
- Acceptance criteria: shipping produces `API:shipping-API` with route
  operations under annotations.
- Regression test: `tests/test_catalog_reconciliation.py`.

### QI-011: External infrastructure was still modeled as a Component

- Severity: medium
- Evidence: manual source review found `Component:Docker-Engine` from the
  queue-master repo. The evidence was real code in `DockerSpawner`, but the
  active `ShippingTaskHandler` only has `docker.init()` and `docker.spawn()`
  as commented-out calls.
- Root cause: unresolved `target_kind=external` dependencies defaulted to a
  Component placeholder, and relation confidence did not account for uncertain
  external infrastructure targets.
- Fix: unresolved non-API external dependencies now become review-confidence
  Providers, and uncertain target confidence pulls the relation confidence down.
- Acceptance criteria: Docker Engine is not emitted as a deployable Component;
  the queue-master relation is review-confidence and targets
  `Provider:Docker-Engine`.
- Regression test: `tests/test_catalog_reconciliation.py`.

### QI-012: Infrastructure-only database image was modeled as a Component

- Severity: medium
- Evidence: manual review found `Component:user-db` even though the source is a
  MongoDB seed/container image and the actual runtime dependency is the
  `user-db:27017` MongoDB resource.
- Root cause: repo extraction treated every Docker image as a Component even
  when it was infrastructure with no API/message/application behavior.
- Fix: infrastructure-only Components with DB/cache/seed markers and a matching
  Resource host/name are demoted to the Resource. Incoming dependency edges are
  rewired to the Resource.
- Acceptance criteria: `Component:user-db` is absent; `Component:user` depends
  on `Resource:MongoDB-users-database`.
- Regression test: `tests/test_catalog_reconciliation.py`.

### QI-013: Checked-in API specs were not filling missed operations

- Severity: medium
- Evidence: manual review found `User-API` did not include `/cards`,
  `/cards/{id}`, `/addresses`, or `/addresses/{id}` even though they are present
  in `user/apispec/user.json`.
- Root cause: the LLM operation list was treated as complete; source API specs
  were not merged into the catalog build.
- Fix: the build step now enriches existing APIs from OpenAPI/Swagger specs
  when the spec title strongly matches one extracted API. Unmatched broad specs
  are not added, avoiding duplicate API entities.
- Acceptance criteria: `User-API` includes the card/address operations from the
  checked-in spec, and the catalog still has no duplicate broad user API.
- Regression test: `tests/test_catalog_reconciliation.py`.

## Still Open

### QI-014: Dynamic API dependencies need better reconciliation to known services

- Severity: medium
- Evidence: `Address-Service`, `Card-Service`, and `Customer-Service` are now
  correctly not services, but they still do not reconcile to `user` API
  surfaces without cross-request reasoning.
- Root cause: the merger does not yet trace HATEOAS/request-body links across
  producers and consumers.
- Proposed fix: add a reconciliation/verifier pass that can connect
  caller-supplied links to known APIs when another repo constructs those links.
- Acceptance criteria: dynamic APIs either resolve to known service APIs with
  evidence or remain explicit review-confidence dynamic dependencies.

### QI-015: Confidence still needs semantic calibration

- Severity: medium
- Evidence: text evidence can match while the catalog abstraction is still
  debatable, for example dynamic dependency identity or dormant helper code.
- Root cause: confidence is mostly extraction confidence, not verified semantic
  correctness.
- Current mitigation: unresolved external infrastructure, dynamic dependencies,
  duplicate resource/component cases, and inconsistent API granularity are now
  demoted or normalized. A full semantic verifier is still needed.
- Acceptance criteria: high confidence means evidence exists and the entity
  kind, canonicalization, direction, and granularity pass structural checks.

### QI-016: Domain taxonomy still needs org-level governance

- Severity: medium
- Evidence: obvious lexical variants now collapse, but arbitrary enterprise
  domain ownership still cannot be inferred reliably from one repo at a time.
- Root cause: domain labels need workspace seeds, existing catalog descriptors,
  CODEOWNERS/team maps, or organization-level reconciliation.
- Proposed fix: domains should come from workspace seeds, CODEOWNERS/team maps,
  existing Backstage descriptors, or a separate organization-level
  reconciliation pass. Per-repo free-text domains should be review confidence
  or normalized to a parent domain.
- Acceptance criteria: equivalent labels collapse to one canonical Domain, and
  sub-domain/capability labels are represented deliberately rather than by
  punctuation/spelling drift.
