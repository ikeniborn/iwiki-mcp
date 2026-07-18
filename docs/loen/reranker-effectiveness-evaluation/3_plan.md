# Plan

Topic: `reranker-effectiveness-evaluation`

Approval status: **approved by user on 2026-07-18**

## Bounded Plan

1. Build a sanitized, identity-preserving fixture from the supplied call and
   capture missing baseline evidence -> verify: no credentials or private
   metadata are present; every candidate has a stable ID; copied log truncation
   is explicitly marked and excluded from live-input conclusions.
2. Create graded relevance judgments for the seed query plus a small stratified
   query set -> verify: each query has documented intent, 0-3 judgments, and at
   least one relevant candidate; ambiguous judgments are flagged for human
   review.
3. Implement a reproducible evaluation harness -> verify: deterministic metric
   tests cover Recall@k, MRR@k, nDCG@k, Precision@k, stable ties, partial
   responses, and latency/payload aggregation.
4. Measure current behavior -> verify: evidence records RRF-only order, current
   32-candidate rerank order/scores, response coverage, request bytes, p50/p95
   latency, errors, and model/deployment identity without secrets.
5. Compare bounded alternatives -> verify: evaluate candidate pools 8, 16, 24,
   and 32; raw versus natural-language intent query; bounded query decomposition
   for multi-intent cases; plain chunks versus stable heading/file context; and
   `top_n` aligned with evaluated final output versus full scoring.
6. Select the Pareto frontier -> verify: report quality, recall, latency, payload,
   and reliability deltas against current behavior. Prefer the simplest
   non-dominated variant; do not average away query-class regressions.
7. Apply at most one focused auto-fix when gates pass -> verify: the chosen
   variant does not regress Recall@k, MRR@k, or nDCG@k on the accepted dataset;
   improves at least one primary quality metric or materially reduces p95
   latency/payload; preserves fail-soft behavior and public response shape.
8. Run focused and full verification, update rerank documentation and the bound
   wiki if functionality changed, then record keep/fix/revert/handoff -> verify:
   all commands exit zero, evidence is stored under `evidence/`, wiki lint has no
   new findings, and `7_result.md` states the supported conclusion.

## Experiment Matrix

| Dimension | Variants |
|---|---|
| Candidate pool | 8, 16, 24, 32 |
| Query form | raw, natural-language intent, bounded multi-intent decomposition |
| Document form | chunk text, file + heading + chunk text |
| Provider return size | final top-k, full candidate count |
| Baseline | RRF only, current rerank contract |

Cross-product execution is bounded by 200 local model requests. Screening may
eliminate dominated variants before repeated latency runs.

## Primary Metrics

- Quality: Recall@5/8/10, MRR@5/8/10, nDCG@5/8/10, Precision@5
- Efficiency: request bytes, candidate count, p50/p95 latency
- Reliability: timeout/error rate, valid-score coverage, duplicate/missing rows
- Diagnostics: relevant/non-relevant score separation and per-query-class deltas

## Auto-Fix Policy

- Change only mutable-scope paths.
- Apply no fix when evidence is incomplete or variants trade quality for cost
  without a clearly approved operating point.
- Accept a fix only when deterministic tests pass and live evidence, when
  available, shows no primary-metric regression.
- Prefer a request-policy or representation change over model replacement unless
  the evaluated model itself is demonstrably dominated and replacement is
  separately approved.
- Never merge, release, push, deploy, rotate credentials, or edit external
  services.

## Verifier

Focused verifier after the harness exists:

```bash
uv run pytest -q tests/eval tests/engine/test_rerank.py tests/test_server_search.py
```

Repository verifier after any product change:

```bash
uv run pytest -q
uv run flake8 src tests eval
uv run iwiki-mcp --help
```

Experiment evidence target:

```text
docs/loen/reranker-effectiveness-evaluation/evidence/latest-test.json
```

## Budget

- Maximum passes: 5
- Maximum local reranker requests: 200
- One focused product fix maximum before re-evaluation
- Manual trigger; no unattended recurrence

## Stop Conditions

- A Pareto-efficient recommendation is supported by complete evidence and all
  verifiers pass.
- No tested variant dominates current behavior; publish report-only result.
- Five passes or 200 local model calls are consumed.
- Protected scope would be required.
- Live model is unavailable after bounded retries.
- Full untruncated candidate text or sufficient relevance judgments cannot be
  obtained.

## Handoff Conditions

- Model/deployment replacement is indicated.
- Public API, index schema, embedding model, or write-intent behavior must change.
- Live infrastructure or secrets must be modified.
- Quality/latency tradeoff needs an owner-selected operating point.
- Any verifier regression remains after the bounded repair budget.

## Rollback Policy

Before a product edit, preserve the focused diff and baseline evidence. If any
quality gate fails, reverse only changes introduced by that pass, rerun the
baseline verifier, retain failed-attempt evidence, and hand off. Never use
destructive Git reset or overwrite unrelated user changes.

## Terminal Condition

Finish only with reproducible evidence, a selected approach or explicit
no-change conclusion, passing required checks, and a completed `7_result.md`.
The user approved this plan on 2026-07-18. The runner may execute this exact
plan under the constraints in `loop.yaml`.
