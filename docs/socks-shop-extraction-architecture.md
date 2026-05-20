# Socks-Shop Extraction Architecture Note

This note records the implementation shape after the 2026-05-20 sock-shop
quality pass. The goal is a generic, open-source extraction pipeline that works
out of the box on a new workspace, with sock-shop as the public accuracy
harness.

## Current Balanced Pipeline

The default mode is:

1. Crawl or mount repositories into a workspace.
2. Run one source-grounded extractor pass per repo.
3. Preserve static/code-shape facts from the extractor payload.
4. Merge through deterministic reconciliation:
   - kind-aware entity resolution;
   - source provenance on APIs, Resources, Providers, and relations;
   - dynamic request-body URL demotion to review API placeholders;
   - database/cache resources prefer concrete host/config labels when safe;
   - obvious domain-label drift is normalized;
   - route-level API fragments collapse to a contract API;
   - broker resources are inferred from queue/topic technologies.
5. Build JSON and Kuzu catalogs.
6. Run catalog evals and source-audit gates.
7. Record local quality issues instead of silently accepting ambiguous facts.

This keeps the expensive part to one LLM pass per repo, then uses cheap
deterministic checks to improve correctness. The current sock-shop rebuild used
`--skip-extraction`, so all fixes after the baseline extraction added no new
LLM cost.

## Validation Snapshot

Latest sock-shop artifacts:

- Catalog: `evals/data/catalog.json`
- Kuzu DB: `evals/data/catalog.kuzu`
- Eval run: `evals/runs/run_20260520_091528.json`
- Audit report: `docs/socks-shop-catalog-audit.md`
- Manual verification: `docs/socks-shop-manual-verification.md`
- Issue log: `docs/socks-shop-quality-issues.md`

Latest metrics:

| Metric | Value |
| --- | ---: |
| Repositories | 9 |
| Entities | 44 |
| Relations | 76 |
| Components | 10 |
| APIs | 16 |
| Resources | 8 |
| Providers | 3 |
| Domains | 5 |
| Catalog eval | 18 / 18 |
| Source audit | 111 pass / 2 review / 0 fail |
| High-severity audit findings | 0 |
| Static API specs merged | 1 |
| Static API operations added | 9 |
| Unit tests | 174 passing |

Baseline extractor accounting:

| Metric | Value |
| --- | ---: |
| Estimated LLM cost | $2.251965 |
| Wall time | 2,537.032 seconds |
| Input tokens | 5,234,225 |
| Billable input tokens | 431,025 |
| Cached input tokens | 4,803,200 |
| Output tokens | 348,546 |
| Reasoning output tokens | 244,989 |

## Modes

Cheap mode:

- Reuse existing extractions with `--skip-extraction`.
- Run deterministic merge, Kuzu build, eval, and source audit.
- Cost: no new LLM spend.
- Use for regression checks, UI iteration, and merge-rule development.

Balanced mode:

- One LLM extractor pass per repo plus deterministic reconciliation and audit.
- Current sock-shop cost: about $2.25 and 42 minutes for 9 repos with
  `gpt-5.4-mini` at medium effort.
- Use as the default for a new workspace.

Exhaustive mode:

- Run the balanced pipeline first.
- Target only review/high-risk facts for additional LLM or agentic correction.
- Useful for enterprise onboarding, but not yet the default because the current
  high-severity sock-shop defects were fixed with deterministic reconciliation.

## Implemented Decisions

- Backstage-like kinds remain generic: Component, API, Resource, Provider,
  System, and Domain.
- Resources and Providers are never reconciled through Component fallbacks.
- Caller-supplied URLs are not deployable services unless another source
  grounds them as such.
- Infrastructure-only database/cache seed images are demoted to matching
  Resources when they have no application behavior of their own.
- Existing APIs are enriched from checked-in OpenAPI/Swagger specs when the
  title strongly matches the extracted API, without adding duplicate APIs.
- RabbitMQ is modeled as a Resource broker derived from queue/topic evidence.
- Queue/topic resources remain precise endpoints; the broker is an additional
  blast-radius facet.
- API contracts are preferred over many one-route API entities when a component
  emits route fragments.
- Domain normalization is intentionally conservative and records aliases so a
  future organization taxonomy can replace free-text labels.

## Deferred Work

- Dynamic API reconciliation: HATEOAS/request-body links should be connected to
  known service APIs when another repo constructs those links.
- Semantic confidence: high confidence should mean evidence, kind,
  directionality, canonicalization, and granularity all pass verification.
- Domain governance: production-grade domains should come from catalog seeds,
  CODEOWNERS/team maps, existing descriptors, or an org-level reconciliation
  pass.
- Tree-sitter/AST work: keep exploring it as a verifier/code-shape signal, but
  do not make language-specific AST extractors the default until they beat the
  generic pipeline on quality, cost, and maintenance.

## Completion Standard

Sock-shop is acceptable for the current goal when:

- the catalog eval passes all questions;
- source audit has no high-severity findings;
- every Component/API/Resource/Provider/relation is either source-backed,
  deterministic-derived from source-backed evidence, or explicitly marked as a
  review item;
- local issues capture the remaining medium-risk gaps;
- the implementation contains no private organization branding.
