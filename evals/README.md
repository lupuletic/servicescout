# ServiceScout evals

A small eval harness that measures one thing: does ServiceScout actually help an
AI agent (or a human) understand a multi-service codebase it doesn't have cloned?

It runs against two public, pinned benchmarks:

- **Weaveworks sock-shop** (default) — 9 polyglot microservices in separate
  repos, with an HTTP + RabbitMQ async messaging chain. 18 questions.
- **Google Online Boutique** (`microservices-demo`) — an 11-service monorepo
  that exercises focused per-service extraction. 10 questions. Lives under
  `evals/workspaces/online-boutique/`.

## Quickstart

```bash
# 1. clone the sock-shop repos at pinned SHAs
./evals/setup.sh --workspace sock-shop

# 2. build a catalog from the cloned repos (costs LLM tokens once)
./evals/build_eval_catalog.sh --workspace sock-shop

# 3. build the graph DB that MCP/search uses
python -m servicescout.build_kuzu --catalog evals/data/catalog.json --db evals/data/catalog.kuzu

# 4. catalog tier — free, fast, no LLM calls. Run on every change.
python evals/runner.py --workspace sock-shop

# 5. agent tier — paid; needs ANTHROPIC_API_KEY
python evals/runner.py --workspace sock-shop --agent

# 6. render the markdown report (+ diff vs the promoted baseline)
python evals/report.py --runs-dir evals/runs
```

Swap `--workspace online-boutique` to run the second benchmark with the same
commands. To explore a catalog in the UI without touching your real `./data`,
point Compose at the eval data:

```bash
docker compose --env-file .env.socks-shop.example up -d mcp dashboard   # http://127.0.0.1:8790
```

## The two tiers

**Catalog tier** grades the graph itself, with zero LLM calls — it drives the
same `Backend` interface the MCP server uses over a pre-built `catalog.json`.
Each question runs up to three checks; it **passes** only when every applicable
check is 1.0:

- `search_recall` — does `backend.search()`'s top-K include the expected entity? (routing)
- `neighbors_recall` — are the expected graph neighbours present? (edge correctness, esp. async)
- `trace_recall` — does `backend.trace()`'s multi-hop plan visit the expected nodes? (journey planning)

Because it's free and instant, run it on every change. The `structural`
questions assert invariants the catalog must always hold (e.g. a message broker
is transport, not an entity) and catch bugs no user-facing question would.

**Agent tier** grades user-facing value with a cold-start comparison, two trials
per question with the same model:

- **baseline** — no context, no tools, no repos (an engineer dropped into a new
  org without the right repos cloned).
- **treatment** — same prompt plus a context blob from `servicescout_search` +
  `servicescout_trace`.

Each answer is scored on `repo_recall` (expected repos mentioned),
`keyword_hit`, and an optional LLM `judge` (`correctness`, `specificity`,
`completeness`, `hallucination_risk`, 1–5, rubric in `judge_prompt.txt`; pass
`--no-judge` to skip). Answers are cached by `(question_id, trial, model,
prompt_hash)`, so re-running with no changes costs nothing.

## Picking the right speed

You almost never need to re-extract all repos. Match the work to the change:

| Speed | Command | Cost | When |
|---|---|---|---|
| **Re-eval only** | `python evals/runner.py --workspace sock-shop` | $0, seconds | Retrieval / scoring / RRF change — extractor untouched. |
| **Targeted re-extract** | `./evals/build_eval_catalog.sh --workspace online-boutique --only frontend,checkoutservice` then re-eval | ~$0.15–0.50/unit | Extractor-prompt change affecting a known cluster. |
| **Full sweep** | `./evals/build_eval_catalog.sh --workspace sock-shop` + `runner.py --agent` | varies, 30–80 min | Release-candidate validation; promote to baseline. |

**Cross-repo gotcha:** when a targeted re-extract touches a messaging cluster
(producers + consumers + broker), re-extract the *whole cluster* together —
otherwise you get half-complete chains (a `producesMessage` with no matching
`consumesMessage`).

## Workflow

The point is to make "did this change help?" a 5-second question:

1. Promote a clean run as the baseline:
   `cp evals/runs/run_<ts>.json evals/baselines/catalog_baseline.json` and commit it.
2. Make your change.
3. Re-extract only what changed, then `python evals/runner.py`.
4. `python evals/report.py` — read the diff in `runs/run_*__vs_baseline.md`.
5. Green → promote the new baseline. Regressed → investigate.

After any Online Boutique extraction, also run the architecture check, which
compares the catalog against the project's published 11-service architecture:

```bash
python evals/validate_readme_architecture.py --workspace online-boutique --strict-warnings
```

## Layout

```
evals/
├── workspace.json          # 9-repo workspace spec            (committed)
├── workspace.lock.json     # pinned SHAs from setup.sh         (committed)
├── questions.yaml          # questions + ground truth          (committed)
├── judge_prompt.txt        # LLM-judge rubric                  (committed)
├── setup.sh                # clone repos + pin SHAs
├── build_eval_catalog.sh   # run the extractor over the workspace
├── runner.py               # entry point for both tiers
├── catalog_eval.py         # catalog tier (drives the Backend)
├── agent_eval.py           # agent tier (cold-start comparison + caching)
├── score.py / report.py    # scoring helpers + markdown report
├── workspaces/             # additional benchmarks (online-boutique)
├── workspace/  data/  runs/  cache/   (git-ignored generated state)
└── baselines/              # promoted golden runs              (committed)
```

## Why sock-shop

- **Multi-repo by design** — each service is its own Git repo, like a real org;
  most demos are monorepos.
- **Async chain** — `orders → RabbitMQ → shipping + queue-master` is the
  canonical `producesMessage → consumesMessage` pattern ServiceScout traverses.
- **Polyglot** — Go, Java/Spring, Node.js, Python; extraction can't be
  language-specific.
- **Frozen** — pinned SHAs keep the ground truth stable.

## Known limits

- The agent tier uses **direct Anthropic API calls** with a pre-computed context
  blob, not a real subprocess agent invoking MCP tools. It tests the *value of
  the context*, not the *quality of tool use*. Swapping in `claude --print` with
  the MCP server registered is the natural next step.
- The judge is a single Claude call against a fixed rubric; pin the judge model
  in CI and treat the rubric as code.
- Question counts are small (18 / 10). Trust *deltas* over absolute scores.
