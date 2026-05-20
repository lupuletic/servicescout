# Online Boutique Manual Verification

Date: 2026-05-20

Workspace: `online-boutique`

Source: `evals/workspaces/online-boutique/workspace/microservices-demo`

Catalog: `evals/workspaces/online-boutique/data/catalog.json`

## Summary

The focused monorepo extraction produced a source-audited catalog with:

- 47 entities, 108 relations
- 13 Components, 13 APIs, 6 Resources, 10 Providers
- 12 focused extraction units under one physical Git repository
- Eval: `10/10` catalog questions passed
- Audit: `150` facts, `124` pass, `26` review, `0` fail, `0` high findings

The first whole-monorepo extraction timed out after 20 minutes with an empty
catalog. The generic fix was to add explicit monorepo extraction units:
`repo_units[]` in the workspace config. Each unit runs from the physical repo
root but passes a `focus_path` to the extractor, allowing shared protos and
manifests to remain visible while keeping each LLM call bounded.

## Source Checks

| Area | Expected | Source evidence | Catalog result |
| --- | --- | --- | --- |
| Runnable units | Online Boutique services are deployable units under `src/*`. | `release/kubernetes-manifests.yaml`, `src/*/Dockerfile` | Components include `frontend`, `cartservice`, `checkoutservice`, `productcatalogservice`, `currencyservice`, `paymentservice`, `shippingservice`, `emailservice`, `recommendationservice`, `adservice`, `shoppingassistantservice`, and `loadgenerator`. |
| gRPC contracts | Shared proto defines Cart, Checkout, ProductCatalog, Payment, Shipping, Email, Currency, Recommendation, and Ad APIs. | `protos/demo.proto:23`, `:56`, `:71`, `:107`, `:140`, `:177`, `:199`, `:224`, `:243` | APIs are present and linked with `providesApi`; static proto merge added 10 operations. |
| Frontend checkout path | Frontend reads `CHECKOUT_SERVICE_ADDR` and invokes `PlaceOrder`. | `src/frontend/main.go:136`, `src/frontend/handlers.go:355` | `frontend -> checkoutservice` is `consumesApi` and trace now reaches checkout/payment. |
| Checkout dependencies | Checkout maps cart/product/currency/email/payment/shipping env vars and calls each service in `PlaceOrder`. | `src/checkoutservice/main.go:111-116`, `:325`, `:344`, `:370`, `:380`, `:387` | `checkoutservice` has outgoing edges to cart, product catalog, currency, payment, email, and shipping. |
| Cart storage | Default deployment uses Redis, with optional Spanner and AlloyDB stores. | `release/kubernetes-manifests.yaml:445-446`, `src/cartservice/src/Startup.cs:29-55` | `redis-cart`, `spanner-cart`, and `alloydb-cart` are Resources; Redis is not modeled as a Component. |
| Recommendation dependency | Recommendation service uses `PRODUCT_CATALOG_SERVICE_ADDR` and a ProductCatalog stub. | `src/recommendationservice/recommendation_server.py:131-136` | `recommendationservice -> productcatalogservice` is present. |
| Observability | Collector/profiler are runtime observability dependencies, not owned services. | `src/frontend/main.go:120`, `src/paymentservice/index.js:47`, `src/productcatalogservice/server.go:163` | Generic provider-duplicate demotion folds `opentelemetrycollector` placeholder into `Provider:OpenTelemetry-Collector`. |

## Generic Fixes From This Benchmark

- Monorepo extraction units with `focus_path`.
- Repo-unit identity now shortens to the final path segment, so `org/repo/service` becomes `service`.
- Component aliases from the extractor are preserved and indexed for dependency resolution.
- External component placeholders that suffix-match a sourced component are merged into the sourced component.
- External component placeholders that alias-match a Provider are demoted to Provider edges.
- Generic search exact-term boosting now ignores words like `service` so all Components are not boosted equally.
- Trace fanout default increased from 5 to 12 so high-fanout frontend/BFF nodes do not hide critical paths.

## Remaining Review Items

The 26 medium audit reviews are mostly evidence-verification limitations and
intentional review-confidence facts around optional cloud providers,
observability, and generated/deployment-derived edges. They are not high
severity blockers for a controlled private-estate indexing run, but they should
be reviewed before treating provider cost/blast-radius output as authoritative.
