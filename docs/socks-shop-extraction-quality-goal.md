# Goal Prompt: Sock-Shop Extraction Quality

```text
Implement the end-to-end ServiceScout extraction-quality pipeline properly, using the public sock-shop eval harness as the validation target.

Primary objective:
Make ServiceScout produce a source-grounded, high-accuracy sock-shop catalog end to end, while keeping the architecture generic, open-source friendly, self-contained, and suitable for large enterprise orgs. This is not just a design exercise: implement the highest-value fixes, run the harness, measure quality/cost/time, and iterate until the sock-shop output is materially trustworthy.

Context:
The current pipeline is useful but not yet reliable enough. We have seen false Service nodes, caller-supplied URLs modeled as services, noisy free-text domains, inconsistent API granularity, weak RabbitMQ/broker modeling, missing or weak provenance, and confidence labels that sometimes mean "snippet exists" rather than "fact is semantically correct."

Use existing issue work as prior art:
- Issue #9 explored AST/tree-sitter, but the stronger direction appears to be universal code-shape verification plus deterministic mini-extractors, rather than fragile per-language tree-sitter rules.
- Static mini-extractors already exist or are planned for Dockerfile, k8s/Helm, build manifests, OpenAPI/proto/graphql, and provider/resource hints.
- The framework-agnostic prompt rewrite, harness abstraction, verifier/correction loop, Context7-style resolver/retrieval ideas, claude-context-style semantic code search, and Backstage-inspired catalog modeling should be considered where they improve measured quality.

Implementation goal:
Build a practical extraction architecture with this default shape unless evidence suggests otherwise:
1. Deterministic/static facts first.
2. One LLM repo pass for semantic interpretation.
3. Verifier/correction for trust and confidence calibration.
4. Targeted second-pass LLM correction only for review/high-risk facts.
5. Full multi-call LLM chains only if evals show the quality lift justifies cost and runtime.

Concrete work:
- Improve static extraction and reconciliation so deployable services, APIs, resources, providers, systems, domains, owners, lifecycle, and relations are typed correctly.
- Prevent dynamic/caller-supplied URLs from becoming deployable Services unless corroborated by config/deployment/API evidence.
- Normalize or demote noisy domain labels such as spelling/punctuation variants.
- Fix API granularity so APIs default to one contract entity with operations attached.
- Decide and implement a clear RabbitMQ/broker model that supports blast-radius questions.
- Improve confidence semantics so high confidence means cited and semantically well typed.
- Add or improve verifier/correction logic for evidence, target kind, endpoint existence, relation direction, and source provenance.
- Add targeted LLM correction or re-extraction only where deterministic checks fail or facts are ambiguous.
- Improve the UI/catalog presentation only where needed to make the validated data understandable.

Validation harness:
Use the existing sock-shop eval harness as the source of truth:
- Fresh setup/rebuild when needed.
- Build `evals/data/catalog.json`.
- Build Kuzu.
- Run catalog evals.
- Run source-vs-catalog audit.
- Manually inspect high-risk facts in `evals/workspace`.
- Record before/after metrics.

Measure:
- catalog eval pass rate;
- source-vs-catalog audit failures;
- false Components/Services;
- false Resources/Providers/APIs;
- dynamic dependency handling;
- API granularity correctness;
- RabbitMQ/broker and queue modeling quality;
- domain/system/owner quality;
- provenance coverage;
- evidence match rate;
- confidence calibration;
- extraction wall time;
- estimated cost per repo;
- tokens per repo;
- complexity and maintenance burden.

Quality gates:
- No Resource-as-Component or Provider-as-Component duplicates unless explicitly allowlisted.
- Caller-supplied/dynamic URLs must not become deployable Services unless corroborated.
- APIs and Resources must carry source provenance.
- `target_kind` must be respected during reconciliation.
- Relation endpoints must exist and directions must be correct.
- API granularity should default to one API per contract, with operations attached.
- Domain labels must be normalized or marked review.
- Confidence must mean semantic correctness, not just snippet existence.
- Eval ground truth must match pinned source.

Deliverables:
- Implemented code changes for the chosen pipeline improvements.
- Regression tests for each fixed defect class.
- Rebuilt sock-shop catalog and Kuzu DB.
- Updated eval run and audit report.
- Before/after quality, cost, and runtime comparison.
- Local issue list for any remaining confirmed defects.
- Final architecture note explaining what was implemented, why, what was rejected, and when to use cheap vs balanced vs exhaustive extraction modes.

Completion criteria:
- Sock-shop can be rebuilt and evaluated end to end from the repo harness.
- Every extracted Component/API/Resource/Provider/relation is audited or explicitly marked out of scope.
- High-severity extraction defects are fixed or captured with acceptance criteria.
- The resulting sock-shop catalog is materially trustworthy for service-flow, dependency, API, resource, provider, and blast-radius questions.
- The implemented approach remains generic and does not hardcode sock-shop or any private organization.
```
