---
name: servicescout
description: Translate a natural-language question into the set of GitHub repositories that are relevant to it by querying the local ServiceScout catalog over MCP, then clone / open those repos and read the actual code to answer. Use whenever the user asks about cross-repo architecture, request flows, service dependencies, "which repos are involved in X", "how does feature Y work end-to-end", "what database does service Z use", "what publishes/consumes queue W", "trace the login/checkout/payment/auth flow", or anything that requires understanding code across multiple services. ServiceScout is a semantic router for repos; the source code is the only authoritative answer.
---

# ServiceScout: a semantic router, not an answer layer

ServiceScout exposes a Backstage-shaped catalog of the org's services,
APIs, resources, and dependencies — built from source code by the
crawler — over the MCP server `servicescout`. **The catalog translates a
prompt into the most likely relevant repos; it does not tell you how the
code actually works.** Once it has pointed you at candidates, clone or
open those repos and follow the code path yourself: that is where the
real answer lives. For any non-trivial question, follow this two-step
workflow.

## Step 1: Discover with the KG

Use the MCP tools to identify candidate repos and (when the question spans
multiple services) the hop-by-hop path.

| Prompt shape | Tool |
|---|---|
| "which repos are relevant to <X>", "where does <Y> live" | `servicescout_search(query=...)` |
| "what does <service> do / call / own" | `servicescout_describe(entity=<name>)` |
| "what does <service> depend on" / "what does <service> publish" | `servicescout_neighbors(entity=<name>, direction="out")` |
| "who depends on / consumes / calls <service-or-resource>" | `servicescout_neighbors(entity=<name>, direction="in")` |
| "trace the <flow> end-to-end" / multi-hop business journey | `servicescout_trace(start=..., max_hops=6)` (use the `journey` skill) |
| "where in code is <X> wired to <Y>" | `servicescout_evidence(source=<X>, target=<Y>)` |
| "is the catalog up to date" / list all <kind> | `servicescout_status(list_entities=true, kind="Component")` |

### Interpreting `servicescout_search` results

- **Trust the top 3.** Each hit carries `score` (Reciprocal Rank Fusion of
  dense+lexical), `vector_score` (cosine, 0–1), and `lexical_score` (BM25-style
  IDF). The fusion is calibration-free; top-3 placement is reliable, position 4+
  is often noise. Top-3 hits also carry `matched_domain_attributes` and
  `matched_glossary` when a query term fired on a specific business term — those
  are the strongest signal a hit is the right repo.
- **Walk one hop with `servicescout_neighbors`** when a hit needs context. `direction="out"`
  shows what it calls / publishes / writes; `direction="in"` shows what depends
  on / consumes / calls it. For deeper or end-to-end traversal use `servicescout_trace`
  (the `journey` skill drives this loop).
- **Pay attention to entity kinds.** An `API:` result tells you what an API
  exposes, but a coding task usually starts from the owning `Component:`. If
  `servicescout_search` returns an API as top hit, immediately `servicescout_describe` its
  `exposed_by` Component or the matching `Component:` ref.

After step 1, you should have:
- A list of GitHub repos the question touches (`source_repos` on each
  Component, the `github.com/source-location` annotation).
- The directed edges between them (`consumesApi`, `dependsOn`,
  `readsResource`, `producesMessage`).
- File:line citations from `evidence` arrays.

## Step 2: Read the code in the repos the KG pointed at

The catalog summarises — it does not narrate the exact request flow. For
each candidate repo, open it locally (clone if needed) and follow the code:
client config → controller → service → outbound call → next repo. Prefer:

1. Files in `evidence` arrays from `servicescout_evidence` — these are the exact code
   sites the LLM extractor cited.
2. Annotation `github.com/source-location` on the entity — points at the
   repo's root on GitHub.
3. Repo paths under the configured workspace root (whatever
   `WORKSPACE_ROOT` is set to in `.env`, e.g. `~/servicescout-workspace/<repo>`)
   — these are already cloned locally if `source_repos` is populated.

Use `Read`, `Grep`, and `Glob` inside those repos. The KG's job ends once
you know which repos to look in; the answer comes from the code. Do NOT
state a request flow you haven't read end-to-end in source.

## Common cross-stack request shapes

These are architectural patterns to remember as candidate hops to look for
in the KG output, then verify in code — they are not promises the KG made:

- **User-facing request flow**: frontend / storefront / mobile app (cookie
  or header auth layer) → API gateway or GraphQL hub → domain service →
  data store. Always trace each hop from the calling repo's HTTP client
  config, not from imagination.
- **Auth / login**: frontend → identity / SSO service → user / profile
  service → DB. There is usually a separate token-issuance hop.
- **Checkout / commerce-shaped flows**: frontend → BFF / orchestrator →
  many fan-out domains (inventory, payments, shipping, order, fraud, mq).
- **Async messaging**: capture `producesMessage` →
  `Resource(virtual-topic|topic|queue|stream)` → reverse-lookup
  `consumesMessage` to find the consumer service.
- **GraphQL hubs / BFFs**: when a single GraphQL or BFF service appears in
  many top results, treat it as a near-certain hop for any user-facing
  question, then confirm by reading the caller's HTTP client config.

## Identity & disambiguation

Entities are named `<kind>:<name>` (a ref). The same name may appear under
multiple kinds (the `Component` and the `API` it exposes, for example); pass
the ref form when you need to disambiguate. Aliases on a Component include
hostnames, config keys, generated client class names, and marketing labels
— the fuzzy match handles most user phrasings.

## Never guess GitHub org / repo from disk path

The workspace layout may be flat (`<workspace>/<repo>/`) — the on-disk
directory does NOT encode the org. The catalog's `source_repos` annotation is
the authoritative `org/repo` value (derived from `git remote get-url origin`).
When you need to cite a repo URL, call `servicescout_describe` and read
`metadata.annotations.source_repos` or
`metadata.annotations.github.com/source-location`. Never infer the org from
a parent directory or a sibling repo. If the catalog doesn't have the entity,
say so — do not fabricate an `<org>/<repo>` pair from a hunch.

## Default to the end-to-end picture

Answer the way an architect, engineer, or product manager would explain
the system to a new joiner: the WHOLE arc, from where the request or
event enters, through every service it touches, to where data is finally
persisted or handed off to an external system. Don't stop at the first
hop that answers the user's literal question — they almost always want
the full story even when they phrase it narrowly.

The shape of a complete trace:

```
  entry point → orchestration / gateways → owning service(s) → persistence / external system
```

- **Entry point** is wherever the work begins: a UI surface (website,
  mobile app, embedded widget), a scheduled job, a webhook from an
  external partner, a message landing on a queue, an admin tool, etc.
- **Orchestration / gateways** are the hubs, BFFs, API gateways, or
  GraphQL servers that route the request inwards.
- **Owning service(s)** are the backends that hold the business logic
  for the domain in the question.
- **Persistence / external system** is the source of truth: a database,
  a message broker handoff, a third-party API, a search index.

Narrow the scope only when the user has explicitly limited the question
("just the database schema", "only the GraphQL contract", "what does
service X publish"). When in doubt, draw the full arc and let the user
prune.

How to find missing ends of the arc when your initial hits cover only
part of it:

- **Missing entry point:** find what calls into a backend hub by running
  `servicescout_neighbors(entity=<hub>, direction="in", edge_types=["consumesApi"])`.
  This surfaces every upstream — websites, mobile apps, internal tools,
  external partners — without you needing to know the enterprise's
  naming conventions.
- **Missing downstream:** `servicescout_neighbors(entity=<service>, direction="out")`
  or `servicescout_trace(start=<service>, end="kind:Resource")` to walk until a
  datastore or external system.
- **Missing the data store:** `servicescout_search` with phrases like "where is X
  persisted" or follow `readsResource` / `writesResource` edges off the
  owning service.

## Frontend vs backend services with similar names

When the same product area has both a UI service and a backend service
(for example, an `<area>` website and an `<area>-service` API), they are
SEPARATE Components in the catalog and usually live in different GitHub
orgs / repos. Always `servicescout_describe` both when the user asks about a
domain — confirm the distinction in your response. The `spec.type` field
disambiguates: `website` or `mobile-app` for the UI tier, `service` for the
backend tier. Read each repo before stating which one handles a given
piece of functionality (login pages, for instance, are often served by the
UI tier even though authentication itself happens behind the backend).

## When NOT to use this

- The user has already named the file they're working on → just open it.
- Pure language / syntax questions → don't call MCP at all.
- "What did I just change" → use git, not the catalog.

## Failure mode to avoid

Do not synthesise a flow only from KG output. The catalog can be incomplete
or carry a wrong edge (low-confidence seed facts, ambiguous duplicates that
reconcile hasn't merged yet). Treat the KG result as a list of repos to
investigate, not as a description of the system. Always cross-check by
reading at least one file per hop before stating "X calls Y for the purpose
of Z." The goal of this tool is to save you from grep-ing across an entire
enterprise; the answer still comes from the code.
