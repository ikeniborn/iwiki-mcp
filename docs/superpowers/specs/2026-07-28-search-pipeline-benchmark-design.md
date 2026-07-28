---
review:
  spec_hash: b5bf9cc7db7a6009
  last_run: 2026-07-28
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-07-28-search-pipeline-benchmark-intent.md
---

# Design: search-pipeline-benchmark

**Date:** 2026-07-28
**Status:** approved

## Acceptance (from intent)

- Per-stage metrics are visible for every involved search pipeline stage.
- Bottlenecks are listed with concrete evidence.
- The output includes a ranked backlog of follow-up fixes or experiments.
- Search modes, chunk settings, and model settings can be compared.
- API keys and provider details remain safe: no key is persisted, printed, or written
  into benchmark artifacts.
- The public `wiki_search` API and response shape remain stable.
- Latency ceiling does not degrade unless a separate explicit decision approves the
  trade-off.
- The benchmark does not write to the wiki base unless a separate explicit action is
  requested.
- Benchmark results are reproducible from committed fixtures, judgments, and commands.

## Scope

The benchmark is live-first. Its primary output comes from read-only runs against real
iwiki domains and the currently configured provider stack. Offline deterministic
fixtures remain in scope only as regression guards for the harness, metrics, report
schema, and bottleneck classifier. The benchmark evaluates returned search context, not
LLM-generated answers.

The live benchmark measures the current search pipeline without changing default
behavior: chunk and index shape, semantic embedding lookup, lexical hits, graph
expansion, Reciprocal Rank Fusion, hydration, and optional reranking. Live evidence is
timestamped, labeled as non-deterministic, and includes a sanitized configuration
fingerprint rather than secrets or raw provider payloads.

Out of scope: changing `wiki_search` API or response shape, changing default embedding or
rerank models, changing chunk defaults, changing the index schema, writing to the wiki
base, and treating live-provider output as a deterministic CI assertion.

## Architecture

Add a new `eval/search_pipeline/` package:

- `fixtures.py` defines live benchmark cases: domains, queries, `k`, modes, and expected
  relevant section identities. It also defines a small synthetic offline fixture used by
  tests.
- `metrics.py` implements `Recall@k`, `MRR@k`, `nDCG@k`, intent coverage, source mix,
  and latency summaries.
- `instrumentation.py` exposes read-only probes around retrieval stages. The probes
  gather signal and candidate counts, per-stage timings, hydration counts, stale/drop
  counts, and rerank metadata without changing public `wiki_search` output.
- `runner.py` executes live cases through the local server/retrieval path using the
  active iwiki binding and `Config.load()`. It can compare `hybrid`, `lexical`, and
  `semantic`, and it can compare rerank-on versus rerank-off only when this can be done
  without changing persisted configuration or writing to the wiki base.
- `analyzer.py` classifies likely bottlenecks from stage evidence and generates a ranked
  backlog.
- `report.py` writes sanitized JSON evidence plus a compact Markdown and/or standalone
  HTML report.
- `__main__.py` provides the CLI:
  `uv run python -m eval.search_pipeline --domain iwiki-mcp --out docs/...`.

The implementation keeps the benchmark as an eval tool, not a production API. Production
search behavior remains inside `iwiki_mcp.retrieval` and `iwiki_mcp.server`.

## Data Flow

1. The CLI loads the benchmark case list and live configuration. If a benchmark-specific
   credential file is required, the operator supplies it explicitly with `--env-file`.
2. For each case, the runner executes the selected mode/settings matrix. The initial
   matrix includes `hybrid`, `lexical`, and `semantic`; optional rerank comparisons are
   recorded only when credentials and model settings are available safely.
3. Instrumentation records final result identities and scores, stage timings, signal
   counts, candidate counts, source mix, hydration outcomes, rerank metadata, and
   sanitized config fingerprints.
4. Metrics compare final and intermediate identities against expected relevant sections.
5. The analyzer assigns bottleneck classes:
   - relevant section missing from chunks or index;
   - relevant section exists in chunks/index but never enters the candidate pool;
   - relevant section appears before fusion but is lost after fusion or top-k;
   - relevant section is dropped during hydration because it is stale, unsafe, missing,
     or unhydrated;
   - rerank improves or worsens ordering;
   - final context contains excessive judged noise.
6. The report writes JSON evidence and a concise Markdown/HTML summary with metrics,
   bottlenecks, and ranked follow-up tasks.

## Metrics

Quality metrics:

- `Recall@k`: fraction of expected relevant section identities represented in top-k
  context.
- `MRR@k`: reciprocal rank of the first relevant section.
- `nDCG@k`: graded ranking quality when a case supplies relevance grades.
- Intent coverage: distinct expected intents represented in the top-k context.

Pipeline metrics:

- candidates per stage and mode;
- source mix across `semantic`, `lexical`, `both`, `seed`, `graph`, `global`, and
  `lexical`;
- hydration requested/scored/dropped counts;
- rerank applied/fallback state and scored count;
- total and per-stage latency.

Chunk/index metrics:

- page, section, and chunk counts;
- repeated heading count;
- chunk length distribution;
- relevant-section chunk identity coverage.

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

## Testing

Unit tests cover metric math, report schema, sanitization, credential-file guards, and
bottleneck classification. Offline integration tests use a synthetic mini-vault to prove
the harness can identify at least these cases: relevant section missing from chunks,
relevant section lost before top-k, and rerank improving or degrading order.

CLI smoke tests cover `--help` and controlled failure without live credentials. Live
benchmark execution is manual and env-gated, not a CI requirement. Verification should
include focused eval tests and `tests/test_package.py`. Full `uv run pytest -q` remains a
desired final check, but the plan must account for current unrelated repository failures
if they still exist.
