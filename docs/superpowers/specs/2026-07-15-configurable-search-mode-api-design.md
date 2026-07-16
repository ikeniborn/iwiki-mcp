---
review:
  spec_hash: acadde912de598ab
  last_run: 2026-07-16
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-07-15-configurable-search-mode-api-intent.md
  spec: null
---

# Configurable Semantic Search and Reranking — Design

**Date:** 2026-07-15
**Status:** approved
**Topic:** `configurable-search-mode-api`
**Intent:** `docs/superpowers/intents/2026-07-15-configurable-search-mode-api-intent.md`

## 1. Purpose and scope

The read-search API currently exposes `hybrid`, `vector`, and `lexical`, even
though vector similarity is the implementation technique for semantic search.
It also hard-codes `hybrid` as the function default and merges vector and
lexical hits only after each path has already truncated its own result list.
This design makes `semantic` the canonical public term, makes the omitted-mode
default configurable, broadens the preliminary candidate set, and adds an
optional LiteLLM reranking stage.

The read path changes. `wiki_search(intent="write")` retains its existing
summary-seed, graph-expand, exact-heading, section-rank algorithm. Query
expansion, answer generation, citation validation, deployment mutation, and
the audited project-relative stale-source bug are separate work.

## 2. Acceptance (from intent)

### Desired outcomes

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

### Done when

All three canonical modes work through the MCP interface, omitted mode follows
the documented environment/default precedence, optional reranking demonstrably
reorders a controlled candidate set and falls back on failure, the fixed
evaluation records an accepted quality improvement, the complete tool/schema
smoke and repository verification suite pass, and repository plus iwiki
documentation agree with the observed API.

## 3. Public API and configuration

### R1 — Canonical modes

`wiki_search` accepts exactly `hybrid`, `lexical`, and `semantic` for read
search. Its Python signature uses an optional `Literal` so FastMCP publishes an
optional enum in the input schema. The old `vector` value is rejected; it is
not a compatibility alias. Semantic hits use `hit: "semantic"`; a hit present
in semantic and lexical signals uses `hit: "both"`.

Acceptance: MCP schema lists the three canonical values and omits `vector`;
focused calls prove each value works and `vector` returns an allowed-values
validation response.

### R2 — Default-mode precedence

`Config.search_mode` loads `IWIKI_SEARCH_MODE`, trimming whitespace and
lowercasing it. The environment default is `hybrid`. `wiki_search(mode=None)`
resolves `explicit mode -> Config.search_mode -> hybrid`. Explicit `hybrid`
therefore overrides an environment default of `lexical` or `semantic`.

Acceptance: focused tests cover absent env, every valid env value, explicit
override, whitespace/case normalization, and invalid env/explicit values.

### R3 — Reranker configuration

`Config.rerank_model` loads optional `IWIKI_RERANK_MODEL`. A non-empty model
enables reranking globally for read search; an empty or absent value disables
it. There is no `wiki_search` rerank flag and no separate boolean environment
variable. The reranker reuses `IWIKI_LLM_BASE_URL` and `IWIKI_LLM_KEY`.

Acceptance: config tests prove presence enables the call and absence makes the
read path perform no reranker HTTP request.

### R4 — Result contract

Result entries retain `domain`, `file`, `heading`, `chunk`, `score`, `hit`, and
`source`. `hit` is `semantic`, `lexical`, or `both`. `source` describes how the
candidate entered the pool: `seed`, `graph`, `global`, or `lexical`. Reranking
may replace `score` with the reranker relevance score but does not rewrite
`hit` or `source`.

When reranking is configured, `wiki_search` adds top-level metadata:

```json
{
  "results": [],
  "rerank": {"applied": false, "warning": "reranker unavailable"}
}
```

Successful reranking returns `{"applied": true}`. No model name, credential,
URL, response body, or raw exception is exposed. With reranking disabled, the
existing top-level shape remains `{"results": [...]}`.

Acceptance: serialization tests cover all hit/source values, successful
metadata, sanitized fallback metadata, and the unchanged disabled shape.

## 4. Retrieval architecture

### R5 — Independent ranking signals

The read pipeline builds ranked lists before truncating to final top-k:

1. Semantic page ranking scores `kind="summary"` description records.
2. Lexical page ranking aggregates positive section-term hits per page.
3. Global semantic chunk ranking scores all eligible `kind="section"` records.
4. Graph expansion begins from the union of mode-appropriate semantic and
   lexical page seeds and records seed origin, graph distance, and deterministic
   discovery order.
5. Candidate chunks are drawn from seed/graph pages plus global semantic and
   lexical section hits.

`semantic` uses signals 1, 3, and semantic-seeded graph expansion. `lexical`
uses signals 2, lexical sections, and lexical-seeded graph expansion and never
calls embeddings. `hybrid` uses every signal. Facet filters apply before seed,
graph-candidate, global-chunk, and lexical aggregation decisions.

Acceptance: unit tests isolate every signal, verify mode selection, prove
lexical mode performs no embedding request, and prove facets cannot re-enter
through another candidate path.

### R6 — Reciprocal Rank Fusion

A new framework-free `engine/fusion.py` accepts named ranked lists and merges
them by `(domain, file, heading, chunk)` using Reciprocal Rank Fusion:

```text
rrf(candidate) = sum(1 / (60 + rank_in_signal))
```

The graph list ranks seed pages before graph pages, then shorter distance,
seed rank, file, section ordinal, and chunk. Candidate chunks inherit that page
rank as one signal; semantic chunk similarity and lexical section frequency
remain separate signals. RRF removes the need to normalize incomparable cosine
and term-frequency scales. Ties are deterministic by domain, file, ordinal,
and chunk.

The candidate ceiling is an internal evaluated constant rather than a new
public setting. It must be at least final `top_k`; the accepted value is
recorded with eval evidence. This avoids adding configuration before the fixed
corpus demonstrates a need for it.

Acceptance: pure unit tests cover fusion, duplicates, missing signals, stable
ties, graph-distance ordering, and a candidate ceiling smaller than top-k.

### R7 — Candidate text hydration

The JSONL vector-store schema remains unchanged. Before reranking, selected
records are mapped back to the current Markdown and passed through the existing
`chunk_markdown` function with current chunk settings. The tuple
`(file, heading, chunk)` selects the exact embedded text. Missing or changed
chunks are omitted from the reranker request and retain preliminary order.

Acceptance: tests prove exact long-section sub-chunk hydration, no frontmatter
or unrelated-section leakage, and safe behavior when a page changes after its
index was built.

### R8 — Write-intent isolation

`locate_target` retains its current vector implementation and higher
`write_seed_threshold`. It does not use `IWIKI_SEARCH_MODE`, lexical seeds,
RRF, global chunks, or reranking. Internal vector terminology may remain in
this implementation where it describes mechanics rather than public modes.

Acceptance: existing write-intent tests pass unchanged in observable behavior,
and focused tests prove configured reranking is not called.

## 5. LiteLLM reranker boundary

### R9 — Request contract

A new `engine/rerank.py` sends one batch request to
`${Config.base_url}/rerank`, which resolves to LiteLLM `/v1/rerank` when the
configured base ends in `/v1`. The JSON body contains:

```text
model: IWIKI_RERANK_MODEL
query: original query
documents: hydrated candidate chunk texts
top_n: len(documents)
```

`top_n` equals the number of hydrated candidates sent, capped by the evaluated
internal candidate ceiling. Authentication uses the existing Bearer key. The
implementation accepts LiteLLM result rows containing integer `index` and
finite numeric `relevance_score`.

Acceptance: mocked-HTTP tests assert exact URL, headers, payload, timeout, and
response-to-candidate mapping for the framework-documented LiteLLM contract.

### R10 — Timeout and fail-soft behavior

The reranker uses the existing model-request timeout of 60 seconds. Timeout,
transport failure, HTTP error, malformed JSON, duplicate/out-of-range indices,
or invalid scores never fail an otherwise successful search. Valid returned
rows come first by descending relevance score; missing candidates follow in
their preliminary order. A fully invalid response leaves the entire
preliminary order unchanged and returns sanitized warning metadata.

Retrieval and embedding errors remain visible through the existing MCP error
path; the reranker fallback must not catch or relabel them.

Acceptance: one test per failure class proves stable fallback and sanitized
metadata, while an embedding failure still produces the existing retrieval
error.

## 6. Evaluation and verification

### R11 — Fixed offline evaluation

The network-free hierarchical harness is expanded beyond its current two easy
queries. Fixtures cover semantic-only phrasing, an exact identifier, a page
reachable through a real link, a lexical seed, an unrelated high-similarity
distractor, a semantic/lexical duplicate, and a relevant global chunk outside
the page-seed graph.

Before implementation, the current pipeline records baseline `recall@k` and
`MRR@k`. The new preliminary pipeline is accepted only when `MRR@k` increases
and `recall@k` does not decrease. A deterministic fake reranker separately
proves reranking can improve ordering; live model output is not a unit-test
gate.

Acceptance: the evidence file records corpus, k, baseline, candidate ceiling,
new preliminary metrics, fake-reranker metrics, and the selected configuration.

### R12 — MCP and repository verification

The subprocess MCP smoke enumerates all 18 registered tools and inspects the
`wiki_search` input schema for optional `mode` with the three canonical enum
values. Focused server tests cover configuration precedence and top-level
reranker metadata. Final verification runs full pytest, flake8, CLI help, and
read-only live searches in all three modes. Live reranker verification runs
only when already-configured credentials/model permit it and does not mutate
the framework deployment.

Acceptance: every mandatory local command exits zero; live checks record their
result or a precise external blocker without weakening local acceptance.

## 7. Documentation and release

### R13 — Documentation consistency

Update `README.md`, `docs/README.ru.md`, the standalone server report, agent
templates, the MCP authoring resource, and the complete bound `iwiki-mcp`
retrieval/tool-surface pages. Besides the
new mode/config/reranker behavior, fix audited contradictions within this
scope: pure lexical hits use `source="lexical"`; descriptions seed pages rather
than prefixing section vectors; graph-reachable pages need no description to
enter through graph expansion; `wiki_remediation_plan` and the complete tool
surface are documented; templates describe `wiki_update_page`; reserved
`index.md`/`log.md` wording matches type-directory behavior.

The project-relative stale-source bug is recorded as a separate follow-up and
is not silently fixed in this branch.

Acceptance: repository searches find no public `vector` mode claim, both
languages describe the same tools and configuration, and `wiki_lint` has no
broken/stale findings introduced by the update.

### R14 — Release level

Bump `pyproject.toml` from `0.6.10` to `0.7.1`. The new `0.7` minor line
communicates the intentional breaking rename from `vector` to `semantic` while
the patch component records the documentation-consistency remediation completed
before branch integration.

Acceptance: package metadata, CLI import, and documentation report `0.7.1`.

## 8. Risks and mitigations

- **Small eval corpus overfits fusion decisions.** Use RRF with one conventional
  constant, expand behaviorally distinct fixtures, and require recall guardrails.
- **Global chunk search increases CPU work.** Reuse the already-loaded records
  and one query embedding; cap candidates before text hydration.
- **Reranker delays requests.** Keep it opt-in through model presence, make one
  batch request, use the shared 60-second timeout, and fail soft.
- **Live Markdown diverges from its index.** Hydrate by exact tuple, omit stale
  candidates from reranking, and preserve preliminary order.
- **Breaking mode rename surprises clients.** Publish the enum in MCP schema,
  return allowed values for `vector`, document the minor release, and verify all
  bundled callers/templates.
- **Fallback hides provider failures.** Return sanitized `rerank` metadata when
  configured while keeping results usable.

## 9. File boundaries

- `src/iwiki_mcp/engine/config.py` — search/reranker configuration.
- `src/iwiki_mcp/engine/hier.py` — ranked graph metadata for read retrieval.
- `src/iwiki_mcp/engine/fusion.py` — pure RRF.
- `src/iwiki_mcp/engine/rerank.py` — LiteLLM request and validation.
- `src/iwiki_mcp/retrieval.py` — signal orchestration and candidate hydration.
- `src/iwiki_mcp/server.py` — public mode resolution and response metadata.
- `eval/hierarchical/`, `tests/` — baseline, quality, unit, integration, and MCP
  schema coverage.
- `README.md`, `docs/README.ru.md`, `docs/reports/iwiki-mcp-server-report.html`,
  `templates/`, `src/iwiki_mcp/resources.py`, and the `iwiki-mcp` domain —
  user-facing documentation.
- `pyproject.toml` — release version.
