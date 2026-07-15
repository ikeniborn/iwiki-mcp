---
review:
  intent_hash: 8da5f9ad64cca29d
  last_run: 2026-07-15
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings: []
---

# Intent: configurable-search-mode-api

**Date:** 2026-07-15
**Status:** approved

## Objective
Make the search API use one user-facing terminology (`hybrid`, `lexical`,
`semantic`), allow the server to select its default search mode from the
environment, and improve retrieval quality with lexical page seeds, a broader
chunk candidate pool, fused preliminary scoring, and an optional LiteLLM
reranker. The change addresses the current rejection of `mode="semantic"` and
extends the retrieval pipeline described in the supplied reranker quality
diagram without moving answer generation into the MCP server.

## Desired Outcomes
- A caller can use `hybrid`, `lexical`, or `semantic`; `semantic` visibly means
  semantic retrieval implemented with vectors, and the former public `vector`
  mode is rejected.
- A server operator can choose the default search mode through
  `IWIKI_SEARCH_MODE`, while an explicit `wiki_search(mode=...)` value takes
  precedence.
- Search candidates combine semantic page-description seeds, lexical page
  seeds, graph-expanded pages, and chunk-level candidates before final top-k
  selection.
- When `IWIKI_RERANK_MODEL` is set, the server reranks the prepared candidate
  chunks through the authenticated LiteLLM `/v1/rerank` path; when it is unset
  or the call fails, search still returns the preliminary ranking.
- The MCP smoke test detects registration or input-schema regressions across
  the complete public tool surface, including the optional search mode.
- English, Russian, template, MCP-resource, and iwiki search documentation use
  the same terminology and describe the behavior that clients actually see.

## Health Metrics
- With `IWIKI_RERANK_MODEL` unset, the fixed evaluation corpus has no loss in
  relevant-hit recall at the existing top-k compared with the pre-change
  baseline.
- The selected candidate/scoring configuration measurably improves a named
  top-k retrieval-quality metric on the fixed evaluation corpus before it is
  accepted.
- Existing calls that omit `mode` continue to use hybrid search when
  `IWIKI_SEARCH_MODE` is unset.
- Reranker network calls use the existing 60-second model-request timeout;
  timeout, transport, HTTP, or malformed-response failures return the
  preliminary ranking rather than an MCP error.
- The full pytest suite, flake8 check, CLI smoke, live searches in all three
  canonical modes, and full MCP tool/schema smoke complete successfully.

## Strategic Context
- Interacts with: `wiki_search`, retrieval/index records, search configuration,
  LiteLLM virtual-key authentication, the framework-host `/v1/rerank` route,
  MCP clients, evaluation fixtures, public README files, agent templates, MCP
  authoring resources, and the `iwiki-mcp` documentation domain.
- Priority trade-off: retrieval quality first, then bounded model-request
  latency, then model-call cost.

## Constraints
### Steering (behavioral guidance)
- Tune `rerankerTopN` and preliminary scoring weights only from recorded
  baseline/evaluation evidence; prefer the smallest pipeline that produces a
  measurable quality improvement.
- Keep the user-facing explanation semantic: vectors are the implementation of
  semantic search, not a separate public search concept.
- Preserve the preliminary ordering as the fallback whenever optional
  reranking is unavailable.
- Keep documentation corrections surgical and tied to audited, testable
  behavior.

### Hard (architectural enforcement)
- Public read-search modes are exactly `hybrid`, `lexical`, and `semantic`;
  `vector` is removed rather than retained as an alias.
- `IWIKI_SEARCH_MODE` supplies only the omitted-mode default; an explicit mode
  argument wins. With neither set, the default is `hybrid`.
- Reranking is enabled only by a non-empty `IWIKI_RERANK_MODEL`; no separate
  `wiki_search` rerank-mode argument or boolean enable variable is added.
- Reranker requests reuse `IWIKI_LLM_BASE_URL` and `IWIKI_LLM_KEY`, call the
  LiteLLM `/v1/rerank` endpoint with the configured model, and receive the
  query plus prepared candidate chunks rather than the full corpus.
- Reranker requests use the existing 60-second model-request timeout and fail
  soft to the preliminary ranking.
- `wiki_search(intent="write")` remains outside read-mode and reranker
  selection and preserves its existing target-location behavior.
- Answer generation, query expansion, citation gating, and unrelated audited
  defects such as project-relative stale-source resolution are out of scope.
- Every repository change includes the required version bump; the breaking
  public mode rename uses a minor version bump.

## Autonomy Zones
- Full autonomy (reversible, low risk): add focused tests, correct documentation
  drift, choose internal names, and make surgical implementation changes that
  preserve the approved API and fallback contracts.
- Guarded (log + confidence threshold): tune candidate-pool sizes and scoring
  weights from recorded evaluation evidence; accept a configuration only when
  the chosen top-k quality metric improves without baseline recall loss when
  reranking is disabled.
- Proposal-first (needs approval): change the three canonical modes, add a new
  external dependency, change LiteLLM endpoint/authentication, expand scope to
  query expansion or answer/citation generation, or change write-intent search.
- No autonomy (human only): expose or rotate credentials, mutate framework-host
  LiteLLM/Lemonade deployment, or accept a quality/compatibility regression.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions is
> marked HUMAN CHECKPOINT in the plan.

## Stop Rules
- Halt if: the LiteLLM rerank request/response contract cannot be established
  from project documentation and focused tests, or canonical mode selection
  cannot be represented unambiguously in the MCP input schema.
- Escalate if: quality tuning cannot improve the selected top-k metric without
  reducing baseline recall, the implementation requires a new provider or
  dependency, or live verification would mutate the framework deployment.
- Done when: all three canonical modes work through the MCP interface, omitted
  mode follows the documented environment/default precedence, optional
  reranking demonstrably reorders a controlled candidate set and falls back on
  failure, the fixed evaluation records an accepted quality improvement, the
  complete tool/schema smoke and repository verification suite pass, and
  repository plus iwiki documentation agree with the observed API.
