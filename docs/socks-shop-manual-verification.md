# Socks-Shop Manual Verification

Date: 2026-05-20

This is a manual source review of the generated sock-shop catalog after the
quality fixes. It is intentionally separate from the automated eval so that
green test status is not treated as proof by itself.

## Objective

Verify that the current harness output for sock-shop is source-grounded,
organization-agnostic, and good enough to use as the baseline pipeline for
indexing larger workspaces.

Success criteria:

- Components are real runnable software units, not databases, queues, or
  third-party infrastructure mislabeled as services.
- APIs are contracts or runtime-supplied API placeholders, not fake services.
- Resources represent databases, caches, queues, topics, and brokers.
- Relations have the right direction and target kind.
- Uncertain dynamic/dormant facts are review-confidence, not high-confidence.
- No private organization-specific logic or labels are required.

## Latest Harness Result

Artifacts:

- Catalog: `evals/data/catalog.json`
- Kuzu DB: `evals/data/catalog.kuzu`
- Eval run: `evals/runs/run_20260520_091528.json`
- Audit: `docs/socks-shop-catalog-audit.md`

Metrics:

| Metric | Value |
| --- | ---: |
| Repos | 9 |
| Entities | 44 |
| Relations | 76 |
| Components | 10 |
| APIs | 16 |
| Resources | 8 |
| Providers | 3 |
| Domains | 5 |
| Systems | 2 |
| Catalog eval | 18 / 18 |
| Source audit | 111 pass / 2 review / 0 fail |
| High-severity audit findings | 0 |
| Static API operations added | 9 |
| Unit tests | 174 passing |

## Manual Checks

### Components

Accepted Components:

- `front-end`: Express storefront and API facade.
- `catalogue`: Go catalogue HTTP service.
- `carts`: Spring Boot cart service.
- `orders`: Spring Boot order service.
- `payment`: Go payment authorization service.
- `shipping`: Spring Boot shipping service.
- `queue-master`: Spring Boot queue consumer/management service.
- `user`: Go user-account service.
- `load-test`: Locust workload runner.
- `microservices-demo-app`: external app target used by the load-test repo.

Fixed during manual review:

- `Docker-Engine` was not a runnable service in this workspace. It is now
  `Provider:Docker-Engine` with review confidence.
- `user-db` was a MongoDB seed/container image, not application code. It is now
  represented by `Resource:MongoDB-users-database`; the old Component was
  removed and `Component:user` points to the Resource.

### APIs

The service APIs line up with source routes/specs:

- `front-end`: account, storefront, and metrics surfaces.
- `catalogue`: catalogue HTTP API.
- `carts`: cart, item, and health APIs.
- `orders`: orders API plus operational health/metrics.
- `payment`: `/paymentAuth`, health, and metrics.
- `shipping`: one normalized `shipping-API` with route operations.
- `queue-master`: health and Prometheus management endpoints.
- `user`: checked-in user API plus router endpoints.

Fixed during manual review:

- `User-API` was missing several operations present in `apispec/user.json`,
  including `/cards`, `/cards/{id}`, `/addresses`, and `/addresses/{id}`.
  The merger now enriches matching APIs from checked-in OpenAPI/Swagger specs
  without adding duplicate broad-spec API entities.

Dynamic order dependencies are now explicit review-confidence API placeholders:

- `Address-Service`
- `Card-Service`
- `Customer-Service`

`orders -> Items-API` is also review-confidence because the URL is
caller-supplied, even though it resolves to the carts item API.

### Resources

Resources match the source-level infrastructure:

- `catalogue-db`: MySQL catalogue database.
- `carts-db`: MongoDB carts database.
- `orders-db`: MongoDB orders database.
- `MongoDB-users-database`: MongoDB user database at `user-db:27017`.
- `session-db`: Redis session store used by front-end when `SESSION_REDIS` is
  enabled.
- `shipping-task`: RabbitMQ queue.
- `shipping-task-exchange`: RabbitMQ topic exchange.
- `rabbitmq`: inferred broker Resource for blast-radius queries.

### Relations

Manually checked source paths:

- `front-end -> catalogue/carts/orders/user`: source URLs in
  `front-end/api/endpoints.js`.
- `orders -> payment`: `OrdersConfigurationProperties.paymentUri()`.
- `orders -> shipping`: `OrdersConfigurationProperties.shippingUri()`.
- `shipping -> shipping-task`: `RabbitTemplate.convertAndSend("shipping-task",
  shipment)`.
- `queue-master -> shipping-task`: `SimpleMessageListenerContainer` listens on
  `shipping-task`.
- Zipkin provider dependencies: source config/imports across service repos.
- Database/cache dependencies: application properties, compose files, and
  health checks reference the expected hosts.

The async path now matches the pinned code:

`orders` calls `shipping` over HTTP; `shipping` publishes `shipping-task`;
`queue-master` consumes `shipping-task`.

## Remaining Review Items

These are not treated as hallucinations, but they are not fully resolved:

- Dynamic HATEOAS/order payload URLs for address/card/customer remain explicit
  review APIs because the pipeline does not yet reconcile those links back to
  the user service across request-producing repos.
- `Provider:Docker-Engine` is review-confidence because the source contains a
  Docker spawner class, but the active handler comments out the calls.
- Domain/System labels are useful but not enterprise-grade. They need
  workspace seeds, Backstage descriptors, CODEOWNERS/team maps, or organization
  taxonomy to be authoritative.

## Conclusion

The current sock-shop output is now good enough as a baseline indexing pipeline:
service-flow, dependency, API, resource, provider, and RabbitMQ blast-radius
queries are source-grounded and pass the harness.

It is not a perfect truth engine. The correct operating model for new
enterprise workspaces is to run this pipeline, fail on high-severity audit
findings, manually inspect review facts, add organization-agnostic reconciliation
rules where defects repeat, and use workspace seeds only for facts that cannot
be inferred safely from code.
