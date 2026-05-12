---
name: journey
description: Trace an end-to-end business journey or user flow across multiple microservices by combining ServiceScout (via the servicescout MCP server) with direct code reading. Use whenever the user asks to trace a request flow, user journey, business process, or event chain that spans 2 or more services — e.g. "trace what happens when a user places an order", "follow the login flow end to end", "how does a refund get processed across services", "what services touch this kind of event". ServiceScout plans candidate hops; you verify each in source code and produce a chain with file:line evidence at every step.
---

# Journey tracing: ServiceScout plans, code confirms

A business journey or user flow often crosses many services — sometimes
2 or 3, sometimes 15 or 20 (especially when async messaging is involved).
ServiceScout can ENUMERATE candidate hops in milliseconds, but it cannot
tell you whether each hop is real and current. Your job is to verify
each hop in source code before stating it as fact.

## Workflow

### 0. Pick the right starting point — and aim for the full arc

Default to the architect's view: trace the journey end-to-end, from
where the work enters the system to where data lives or is handed off.
The complete shape is:

```
  entry point → orchestration / gateways → owning service(s) → persistence / external system
```

The starting point is wherever the work actually begins: a UI surface,
a scheduled job, a webhook, an integration partner, a message landing
on a queue, an admin tool. If your initial hits skip the entry point,
find it before tracing forward:

- `servicescout_neighbors(entity=<hub-or-gateway>, direction="in", edge_types=["consumesApi"])`
  surfaces every upstream caller of a hub — websites, mobile apps,
  internal tools, external partners — without you needing the
  enterprise's naming conventions.
- `servicescout_search` with a phrasing that names where the work originates
  (e.g. "scheduled job", "webhook", "admin tool") if the entry point
  isn't behind a known hub.

Stop short of the full arc only when the user has explicitly narrowed
the question ("just the database layer", "only the GraphQL contract").
Otherwise the user almost always wants the whole picture — the way a
new joiner would need it explained.

### 1. Plan with `servicescout_trace`

```
servicescout_trace(
  start="user clicks login button on the storefront",  # NL or entity ref
  end="kind:Resource",                                  # optional terminal
  max_hops=6,                                           # bump for very deep async chains
  include_async=True,
)
```

Returns ordered candidate hops with `branch_id`, `depth`, `edge_type`,
`evidence_count`, `confidence`, and `to_tagline`. Async messaging chains
(`producesMessage` → topic → `consumesMessage`) are followed automatically.

**Reading the plan:**
- `start_resolved` — what the KG decided your start point is. If wrong,
  rephrase or pass an explicit `Component:<name>` ref.
- `hops` — candidate transitions. NOT verified.
- `terminal_nodes` — where the trace stopped (end matched, max depth, or
  no outgoing edges).
- `hop_count` — if very high (>50), narrow with a more specific `end`
  condition or smaller `max_hops`/`fanout_per_node`.

### 2. Verify each hop in code

For every hop in the plan:

1. Call `servicescout_evidence(source=<from>, target=<to>)` to get file:line
   citations the LLM extractor recorded for that edge.
2. Open at least ONE of those files (cite the path:line) and confirm:
   - The HTTP call / queue publish / DB write is actually wired up.
   - The control-flow can reach that call site on this journey.
3. If you cannot confirm in code, mark the hop as **unconfirmed** and
   move on — do NOT silently accept it.

### 3. Compose a verified chain

Build a sequence of `{step, from, to, what_happens, evidence:[path:line], confirmed}`.
Each `what_happens` is one sentence in business or technical language
describing what the hop does (read it from the cited code, not from
your imagination).

### 4. Choose the output surface

Same verified chain, two presentations:

**Developer view** (default):
```
1. <storefront-component> (<your-org>/<storefront-repo>)
   → calls <graphql-hub> via GraphQL mutation `login(...)`
   → <your-org>/<storefront-repo>/src/api/auth.ts:42
2. <graphql-hub>
   → calls <account-service> via REST POST /v1/sessions
   → <your-org>/<graphql-hub-repo>/.../AccountClient.java:118
3. ...
```

**Product-manager view** (when the user asks in business language):
```
1. User clicks "Sign in" on the storefront.
2. Storefront asks the GraphQL hub to authenticate.
3. GraphQL hub delegates to the account service.
4. Account service verifies credentials and issues a session token.
5. ...
```

Always include a "verified in code" / "KG-only" annotation per step so
the user can tell what was actually confirmed vs what was inferred from
the catalog.

## When to use this skill vs `servicescout`

| Question shape | Skill |
|---|---|
| "Trace the login flow end to end" | journey |
| "How does a placed order get fulfilled all the way down to the warehouse?" | journey |
| "Which services consume the order.placed event?" | journey (async chain) |
| "Which repos handle the X domain?" | servicescout |
| "What does service Y do?" | servicescout |
| "Where is config key Z used?" | servicescout |

The two skills compose: when journey tracing surfaces an unfamiliar
service, fall back to `servicescout_describe` on it (from servicescout) to read its
tagline + capability_sheet before moving to code. For "who else consumes
this message / queue / API" sub-questions during a trace, use
`servicescout_neighbors(entity=<resource>, direction="in")` — much faster than
re-tracing from scratch.

## Async messaging chains

When the plan shows `producesMessage` followed by `consumesMessage`
hops:

- The producer fires a message; the consumer picks it up out-of-band.
- For each consumer hop, also `servicescout_evidence` from the message resource
  back to the consumer to find the listener / handler class.
- Time matters: in a synchronous trace the consumer follows
  immediately; in async flows the consumer may run minutes later.
- Treat each consumer as a separate branch of the journey, not a single
  linear chain.

## Honest failure modes

- **Unconfirmed hop**: the KG suggests A → B but you can't find the
  call in A's source. State this explicitly. Possible reasons: stale
  catalog, alias mismatch, refactor that removed the call, the edge
  was a low-confidence extraction.
- **Missing hop**: the journey is incomplete because the KG hasn't
  extracted a relevant service yet (it shows up in the code you read
  but isn't in the catalog). Name it in your answer and flag it as
  needing extraction — do NOT make up its name.
- **Cycle / fan-out explosion**: more than ~30 hops surfaced is usually
  noise. Narrow the trace with a tighter `end` condition (e.g.
  `kind:Resource` to stop at any datastore) or smaller `fanout_per_node`.

The goal is a trace the user can audit: every step links to a real
line of real code, and uncertainty is labelled rather than hidden.
