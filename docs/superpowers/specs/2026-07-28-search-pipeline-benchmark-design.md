---
review:
  spec_hash: a17e3b5aa06987aa
  last_run: 2026-07-28
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-07-28-search-pipeline-benchmark-intent.md
---

# Design: search-pipeline-benchmark

**Date:** 2026-07-28
**Status:** approved

## Acceptance (from intent)

- Per-stage metrics remain visible for every involved search pipeline stage.
- Bottlenecks and algorithm changes are supported by concrete evidence.
- Reports retain a ranked, evidence-backed backlog of unresolved bottlenecks.
- Search modes, fusion settings, and rerank candidate budgets can be compared; sanitized
  chunk and model fingerprints preserve cross-run comparison of those settings.
- API keys and provider details remain safe: no key is persisted, printed, or written
  into benchmark artifacts.
- The public `wiki_search` API and response shape remain stable.
- The benchmark and production read path do not write to the wiki base.
- Live results remain timestamped evidence rather than deterministic CI assertions.
- Chunk defaults, embedding and rerank models, and the index schema remain unchanged.

## Evidence And Problem Statement

The live benchmark completed nine traces over three labeled cases in `hybrid`,
`lexical`, and `semantic` modes. Without reranking, lexical produced mean nDCG@8
`0.873` at `25 ms`, hybrid produced `0.829` at `182 ms`, and semantic produced
`0.358` at `170 ms`. Semantic Recall@8 was `0.722`.

The missing semantic results were present in valid chunks, the current index, semantic
signals, and the 32-candidate fused pool. Equal-weight RRF placed the relevant sections
at candidate ranks 22 and 26, outside final top-8. Broad page and graph signals emit
every section from discovered pages and can outvote high-ranked direct section matches.
No evidence identifies chunking, stale records, embedding dimensions, or hydration as a
quality bottleneck.

Reranking raised aggregate nDCG@8 from `0.687` to `0.982` and brought Recall@8, MRR@8,
and intent coverage to `1.0`, but added approximately `1.1 s` per query. The server sends
up to 32 hydrated documents while the provider returns final top-k scores. The design
must improve preliminary fusion first, then reduce the rerank document batch only when
quality evidence permits it.

## Scope

This follow-up applies one Pareto strategy: weighted reciprocal rank fusion followed by
a fixed, quality-gated rerank candidate budget. It extends the existing benchmark only
as needed to select and verify those two internal constants.

The user explicitly approved this production follow-up under the existing intent's
proposal-first autonomy rule and requested that no new intent artifact be created.

In scope:

- expand the labeled live set from three to twelve compact, reviewed cases;
- replay captured signal rankings through candidate RRF weights;
- select fixed internal weights for direct section, page, and graph signals;
- evaluate rerank batches of 16, 24, and 32 candidates after fusion calibration;
- apply the smallest batch that passes all quality gates;
- rerun live A/B evidence with rerank enabled and disabled.

Out of scope:

- query classification or dynamic routing;
- public weight, mode-policy, or candidate-budget configuration;
- changes to chunking, embeddings, model selection, index schema, or result shape;
- generated-answer evaluation;
- writes, migrations, or reindexing during benchmark execution.

## Production Design

### Weighted RRF

`engine.fusion.fuse_ranked` accepts an optional internal signal-weight mapping. Each
unique signal contribution becomes `weight / (60 + rank)`. Missing weights default to
`1.0`, preserving existing behavior for callers that do not supply a mapping.

`retrieval.prepare_read_candidates` supplies one fixed selected mapping. Signals belong
to three evidence classes:

- direct section evidence: `semantic_chunk`, `lexical_section`;
- page evidence: `semantic_page`, `lexical_page`;
- discovery evidence: `graph_page`.

Direct evidence has fixed weight `1.0`. The eval selection procedure chooses page weight
from `{0.025, 0.05, 0.1}` and graph weight from `{0.01, 0.025, 0.05}`, constrained by
`graph <= page`. Equal-weight RRF remains the explicit baseline. This eight-mapping grid
is intentionally small and includes mappings that already eliminate the two observed
losses in replay; the twelve-case corpus determines whether they generalize.

### Bounded Rerank Batch

Retrieval continues to produce the 32-candidate safety pool. The selected rerank batch
limit applies only before hydration/provider submission; it does not shrink preliminary
retrieval evidence or fail-soft fallback coverage.

`server.wiki_search` hydrates at most the selected leading 16, 24, or 32 candidates for
reranking. Successful provider scores remain first, and all unscored, stale, or
unhydrated preliminary candidates continue in original order before final top-k. A
reranker failure returns the unchanged preliminary order and current sanitized metadata.

No new public argument, response field, or environment variable is introduced.

## Selection Procedure

### Labeled Corpus

`eval/search_pipeline/fixtures.py` contains exactly twelve reviewed cases covering:

- exact identifiers and API names;
- semantic paraphrases with little lexical overlap;
- multi-intent requests;
- long or repeated-heading sections;
- graph-adjacent distractors;
- competing page-level and direct-section evidence.

Every case declares graded relevant identities and intent groups. Cases are deterministic
inputs; live provider output remains non-deterministic evidence.

### Fusion Weight Selection

The eval layer replays recorded per-signal identity rankings through a bounded weight
grid. The direct weight remains `1.0`; only the bounded page and graph values defined
above vary. Selection is
lexicographic and trust-first:

1. reject mappings that reduce Recall@8 or intent coverage versus equal-weight RRF;
2. reject mappings that reduce mean nDCG@8 by more than `0.01` in any compared mode;
3. reject mappings that introduce a new lost-after-fusion finding;
4. maximize mean nDCG@8 across all cases and modes;
5. break ties by MRR@8, then by the mapping closest to equal weights.

The selected mapping must also eliminate the two benchmark-confirmed top-k losses. If no
mapping passes, production fusion remains unchanged and the result is reported as
`needs_work` rather than weakening a gate.

### Rerank Batch Selection

After the fusion mapping passes, live runs compare batches 16, 24, and 32 in the default
hybrid mode using the same queries, provider configuration, and final `k`. Each batch
gets one excluded warm-up followed by two measured passes over all twelve cases, yielding
24 latency samples. Select the smallest batch satisfying:

- Recall@8 and intent coverage do not decrease from batch 32;
- mean nDCG@8 decreases by no more than `0.01`;
- no new rerank-worsened-order or missing-candidate finding appears;
- p95 rerank latency improves by at least `25%` versus batch 32.

If neither 16 nor 24 passes, production keeps 32. This preserves trust but leaves the
latency bottleneck unresolved, so the latency part of the final result remains
`needs_work`; the implementation cannot claim the complete follow-up as accepted.

## Data Flow

1. Live benchmark tracing records signal identity order, the fused pool, final ranking,
   quality metrics, and per-stage latency without wiki writes.
2. Offline replay applies the bounded fusion weight grid to recorded signal orders and
   emits one deterministic winning mapping or no-change result.
3. The selected mapping is applied to production candidate fusion and verified by unit
   and integration tests.
4. Live rerank runs compare candidate batches 16, 24, and 32 after the fusion fix.
5. The smallest passing fixed batch is applied before hydration and provider submission.
6. Final reports compare baseline, fusion-only, and fusion-plus-rerank results.

## Metrics And Evidence

Quality gates use Recall@8, MRR@8, nDCG@8, intent coverage, and bottleneck findings.
Latency evidence records embedding, signal, fusion, hydration, rerank, and total timings,
plus p50 and p95 over each batch setting. Reports include the selected weights and batch
size, rejected alternatives with gate failures, source mix, hydration counts, and
sanitized model/config fingerprints. They retain a ranked backlog containing only
bottlenecks that remain supported by the current evidence.

Live evidence must not include provider URLs, keys, authorization data, raw requests,
raw responses, or local base paths.

## Credential And Env Handling

If live benchmark credentials are needed, they come only from an operator-provided env
file. The operator creates and fills the file outside the tool, then tells the agent it
is ready. The benchmark may accept the path with `--env-file`, load it at runtime, and
must not create, commit, print, copy, or include that file in evidence.

The CLI checks that the env file path is not inside the planned evidence output and warns
if it appears tracked by git. It records only safe presence/config fingerprints: which
logical providers were enabled, model names when safe to expose, and timing/count data.
Authorization headers, API keys, raw provider requests, raw provider responses, and raw
secret-bearing errors are never written.

## Error Handling And Safety

- Missing live configuration produces a controlled error with required variable names but
  no values.
- Rerank fallback is recorded as `applied=false` and does not fail the whole benchmark.
- A failed query/domain case is marked failed while the remaining cases continue.
- The benchmark does not call wiki write/update/delete/index tools in live mode.
- Every report labels live measurements as timestamped, non-deterministic evidence.
- Missing fusion weights default to equal contribution in generic fusion callers.
  Non-finite or non-positive explicit weights are rejected; production retrieval uses
  only reviewed module constants.
- A rerank batch that fails any quality gate cannot become the production constant.
- Existing descriptor-based path safety, index-hash validation, and fail-soft rerank
  behavior remain unchanged.

## Testing

Unit tests cover weighted RRF math, deterministic ties, missing-weight compatibility,
weight-grid gate ordering, and rerank-batch selection. Retrieval tests prove direct
section evidence can outrank broad page/graph fan-out without changing public result
fields. Server tests prove only the selected leading batch is hydrated/submitted while
partial, stale, unhydrated, and fail-soft candidates preserve preliminary order.

Benchmark regression tests retain existing metric, report, sanitization, env-file,
read-only, and bottleneck-classification coverage. The twelve labeled cases are reviewed
fixtures, not assertions over live provider scores in CI.

Final verification includes focused fusion/retrieval/rerank/server/eval tests, the full
test suite, CLI help, one read-only live A/B matrix, secret scans of reports, and wiki
documentation/lint after production behavior changes.

## Acceptance Criteria

- Twelve reviewed cases cover all six named query classes.
- The selected fixed fusion weights introduce no Recall@8 or intent-coverage regression
  and no new lost-after-fusion finding versus equal-weight RRF.
- The confirmed candidates at ranks 22 and 26 enter final top-8 without reranking.
- Mean nDCG@8 does not decrease by more than `0.01` in any compared mode.
- A batch of 16 or 24 is applied only when it improves p95 rerank latency by at least
  `25%` while satisfying every quality gate; otherwise 32 is retained and the latency
  outcome is reported as `needs_work`.
- `wiki_search` arguments, public fields, rerank metadata, and fail-soft behavior remain
  unchanged.
- Benchmark runs remain read-only and reports contain no keys, provider URLs, raw
  provider payloads, or local base paths.
- Chunk defaults, embedding/rerank models, and index schema remain unchanged.
- Replaying the same captured signal rankings produces identical selected weights,
  rejection reasons, and deterministic JSON metrics; live timing fields remain
  timestamped evidence.
