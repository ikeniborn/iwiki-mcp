---
review:
  spec_hash: 09970ba1b952343e
  last_run: 2026-07-29
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

**Date:** 2026-07-29
**Status:** approved

## Acceptance (from intent)

- Per-stage metrics remain visible for every involved search pipeline stage.
- Bottlenecks and algorithm changes are supported by concrete evidence.
- Reports retain a ranked, evidence-backed backlog of unresolved bottlenecks.
- Search modes and the four approved fusion families can be compared; sanitized chunk
  and model fingerprints preserve cross-run comparison while rerank-budget evaluation
  remains explicitly deferred.
- API keys and provider details remain safe: no key is persisted, printed, or written
  into benchmark artifacts.
- The public `wiki_search` API and response shape remain stable.
- The benchmark and production read path do not write to the wiki base.
- Live results remain timestamped evidence rather than deterministic CI assertions.
- Chunk defaults, embedding and rerank models, and the index schema remain unchanged.

## Evidence And Problem Statement

The first twelve-case Pareto run completed 36 rerank-disabled traces across `hybrid`,
`lexical`, and `semantic`. It measured Recall@8 `0.699074`, MRR@8 `0.531779`, nDCG@8
`0.477597`, intent coverage `0.712963`, and mean latency `130.753306 ms`. The backlog
contained 19 `lost_after_fusion_topk` findings.

All eight tested page/graph weight maps failed the fixed quality gates. Every map
introduced at least one new top-k fusion loss, and four maps also failed to recover the
confirmed losses at fused ranks 22 and 26. The result was
`needs_work: no_passing_weight_map`; the rerank batch matrix did not run and production
fusion and rerank constants remained unchanged.

This evidence rejects global suppression of page and graph signals. It does not yet
distinguish four narrower explanations: RRF rank smoothing may be too strong; direct
section evidence may need bounded positive support; direct candidates may need a small
top-k reserve; or correlated page/graph fan-out may overcount sections from one page.
The next experiment compares those explanations without changing production.

No current evidence identifies chunk creation, stale index records, embedding
dimensions, or hydration as the cause of the confirmed rank-22/rank-26 losses. Those
components remain observable in the report but are not changed by this experiment.

## Scope

This cycle is evidence-only. It replays the four approved fusion families against the
already captured complete signal rankings, selects at most one replay candidate, and
performs one rerank-disabled live confirmation only if replay passes. It does not apply
the candidate to production.

In scope:

- compare RRF constants `10`, `20`, and `40` against baseline `60`;
- compare direct-section multipliers `1.25`, `1.5`, and `2.0`;
- compare direct-candidate quotas `1`, `2`, and `3` within final top-8;
- compare broad-signal per-page fan-out caps `1`, `2`, and `4`;
- test at most six pairwise combinations formed from passing family winners;
- preserve deterministic JSON, Markdown, and HTML evidence for every acceptance or
  rejection reason;
- run a single live confirmation of the replay winner across all twelve cases and three
  modes.

Out of scope:

- production retrieval or server constants;
- rerank candidate-budget evaluation;
- query classification, dynamic routing, or learned fusion;
- changes to chunks, embeddings, models, index schema, hydration, or result shape;
- generated-answer evaluation;
- wiki writes, migrations, or reindexing during benchmark execution.

## Candidate Families

### RRF Rank Constant

Generic RRF contribution becomes `1 / (rrf_k + rank)`. Baseline remains `rrf_k=60`.
Replay tests `10`, `20`, and `40`; smaller values make rank differences within each
signal more important. Identity merging, duplicate suppression, deterministic tie
ordering, and candidate limits remain unchanged.

### Direct-Section Multiplier

With `rrf_k=60`, contributions from `semantic_chunk` and `lexical_section` use a fixed
multiplier from `{1.25, 1.5, 2.0}`. All page and graph contributions remain `1.0`.
This is the positive counterpart to the rejected page/graph suppression and preserves
discovery evidence at its baseline strength.

### Direct-Candidate Quota

Replay builds both the ordinary baseline order and a direct-only RRF order from
`semantic_chunk` and `lexical_section`. For quota `q` in `{1, 2, 3}`, the final top-8
must contain the leading `q` unique direct identities when that many exist. Missing
reserved identities replace the lowest baseline-ranked non-reserved identities. The
retained baseline candidates keep their relative order; promoted direct candidates keep
their direct-only order. Candidates below top-8 remain in baseline order for evidence
and fail-soft comparison.

### Correlated Fan-Out Cap

Before ordinary RRF, each broad signal (`semantic_page`, `lexical_page`, `graph_page`)
retains at most `1`, `2`, or `4` identities per `(domain, file)`, in original signal
rank order. Direct section signals are not capped. Caps apply independently per broad
signal so one signal cannot erase evidence from another.

## Bounded Combination Procedure

Stage A evaluates each non-baseline parameter independently, for twelve replay
candidates total. Each family winner is the highest-quality candidate from that family
that passes every gate. A family with no passing member contributes no winner.

Stage B evaluates pairwise combinations of passing family winners only. Four family
winners produce at most six unordered pairs. It does not evaluate triples, a full
Cartesian grid, query-specific parameters, or parameters selected from rejected family
members. This keeps the search space inspectable and limits overfitting to twelve cases.

The final replay winner is selected across passing Stage A candidates and Stage B pairs:

1. maximize mean nDCG@8 across all cases and modes;
2. break ties by mean MRR@8;
3. then prefer fewer transformations;
4. then use stable family and numeric parameter ordering.

If no candidate passes, selection returns `needs_work: no_passing_fusion_candidate`.

## Quality Gates

Every candidate is compared with equal-weight `rrf_k=60` replay over the exact same
complete case-mode matrix. A candidate is rejected when any condition holds:

- Recall@8 decreases in any compared mode;
- intent coverage decreases in any compared mode;
- mean nDCG@8 decreases by more than `0.01` in any compared mode;
- any new case-level `lost_after_fusion_topk` finding appears;
- either confirmed rank-22 or rank-26 loss is not recovered in fixed final top-8;
- replay evidence is missing, duplicated, malformed, or contains unknown cases, modes,
  signals, identities, ranks, or ordinals.

MRR@8 is a ranking objective and tie-breaker, not a regression gate. Gates are not
weakened when no candidate passes.

## Replay And Live Data Flow

1. Load the sanitized signal rankings from a complete baseline evidence file.
2. Validate exact parity with the twelve reviewed cases and three required modes.
3. Replay Stage A and record metrics, findings, and rejection reasons.
4. Replay at most six Stage B pairs from passing family winners.
5. Select one deterministic winner or return `needs_work`.
6. When a winner exists, capture a fresh rerank-disabled live baseline and winner run
   over the same twelve cases and modes.
7. Apply the same quality gates to live identities and metrics.
8. Emit `validated_candidate` only if replay and live gates both pass. Otherwise emit
   `needs_work`; production remains unchanged in both outcomes.

Live confirmation may vary because embeddings/provider execution is non-deterministic.
Replay over a fixed evidence file must be byte-stable apart from explicitly excluded
timestamps and output paths.

## Reports And Safety

JSON, Markdown, and standalone HTML report:

- baseline rollups and per-mode quality;
- all Stage A candidates grouped by family;
- family winners and all Stage B pairs;
- metric deltas and exact gate rejection reasons;
- recovery state for the two confirmed losses;
- live confirmation results when performed;
- final status, chosen candidate, and evidence-backed backlog.

Reports contain only sanitized case IDs, query classes, signal names, candidate
identities, parameters, metrics, counts, findings, and timings. They exclude query text,
provider URLs, keys, authorization values, raw payloads, raw exceptions, env-file paths,
and local wiki-base paths.

The command loads credentials only from current process environment or the existing
operator-created env file. It does not create, modify, print, copy, or persist that file.
Replay requires no provider credentials. Both replay and live confirmation remain
read-only and refuse store layouts that would require migration.

## Error Handling

- Invalid candidate parameters fail before replay and cannot enter reports as passing.
- Incomplete baseline evidence returns a sanitized `needs_work` decision rather than a
  partial comparison.
- A live query failure marks that case-mode sample failed and rejects live validation;
  remaining samples continue for diagnostic evidence.
- A candidate implementation exception is reduced to a safe error category; raw error
  text is not persisted.
- No replay or live outcome automatically changes production constants.

## Testing

Unit tests cover each family transformation, boundary values, stable ordering,
deterministic ties, malformed inputs, Stage A family selection, bounded Stage B pair
generation, gate ordering, and final tie-breaks. Regression tests prove the existing
weighted-map selector and ordinary benchmark mode remain compatible.

Runner and report tests cover replay-only operation without credentials, conditional
single-candidate live confirmation, query failure handling, read-only guards, exact
case-mode parity, deterministic JSON, escaped HTML, and secret/path sanitization. Live
provider measurements remain evidence artifacts and never become CI assertions.

Final verification includes focused eval and fusion tests, the full test suite, CLI
help, deterministic replay twice against one evidence file, one read-only live
confirmation when a replay winner exists, report secret scans, and wiki documentation
and lint updates for shipped benchmark behavior.

## Acceptance Criteria

- Exactly twelve Stage A candidates are evaluated from the four fixed families.
- Stage B evaluates only unordered pairs of passing family winners and never more than
  six combinations.
- Every accepted candidate passes all fixed quality gates over the complete 36-trace
  replay matrix.
- Replay selection and rejection reasons are deterministic for identical evidence.
- A live confirmation runs only for one replay winner and cannot validate a candidate
  when any live quality gate or sample-completeness check fails.
- Production fusion, rerank budget, public `wiki_search` API, response fields, chunks,
  models, index schema, and fail-soft behavior remain unchanged.
- Benchmark runs do not write to the wiki base and reports contain no secrets, provider
  URLs, raw provider payloads, env-file paths, or local base paths.
- A passing result reports one `validated_candidate`; a non-passing result reports
  `needs_work` with exact evidence and does not weaken gates.
