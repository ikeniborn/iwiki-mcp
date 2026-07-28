---
review:
  plan_hash: a470f200f2ab3acf
  last_run: 2026-07-28
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-07-28-search-pipeline-benchmark-intent.md
  spec: docs/superpowers/specs/2026-07-28-search-pipeline-benchmark-design.md
result_check:
  verdict: needs_work
  plan_hash: a470f200f2ab3acf
  last_run: 2026-07-28
---

# Search Retrieval Pareto Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox
> (`- [ ]`) syntax for tracking.

**Goal:** Improve preliminary wiki search quality with evidence-selected weighted RRF,
then reduce rerank latency only when a fixed smaller candidate batch passes the approved
quality gates.

**Architecture:** Extend the existing eval package with deterministic replay and one
bounded Pareto experiment. Production receives only two internal constants selected by
that evidence: one signal-weight map and, if it passes, one rerank batch limit. Public
`wiki_search`, models, chunking, index schema, safety guards, and read-only benchmark
behavior stay unchanged.

**Tech Stack:** Python 3.10+, existing `iwiki_mcp` retrieval/rerank modules, existing
`eval.search_pipeline` package, pytest, standard-library JSON/statistics, uv.

---

## Baseline Evidence

- Rerank-off aggregate: Recall@8 `0.907407`, MRR@8 `0.777778`, nDCG@8
  `0.686564`, intent coverage `0.944444`, mean latency `125.5 ms`.
- Rerank-on aggregate: Recall@8/MRR@8/intent coverage `1.0`, nDCG@8
  `0.982244`, mean latency `1206 ms`.
- Confirmed loss point: equal-weight RRF places two relevant semantic candidates at
  fused ranks 22 and 26 even though chunks, index, dimensions, signals, and hydration
  are valid.
- Confirmed latency point: rerank adds about `1.1 s` and dominates total latency.
- Current test baseline on 2026-07-28: `605 passed`, one unrelated failure because
  `docs/reports/iwiki-mcp-server-report.html` is absent. This plan does not create that
  unrelated report.

## Requirements And Closure Map

| ID | Requirement | Closed by |
| --- | --- | --- |
| R1 | Weighted RRF supports validated internal signal weights while omitted weights preserve equal RRF. | Task 1 |
| R2 | Exactly 12 reviewed live cases cover the six approved query classes. | Task 2 |
| R3 | Captured signal rankings deterministically select one passing weight map or no change. | Task 2 |
| R4 | Live evidence compares rerank batches 16, 24, and 32 with one warm-up and two measured passes. | Task 3 |
| R5 | Reports show selected/rejected settings, gate reasons, stage metrics, findings, and ranked backlog without secrets or paths. | Task 3 |
| R6 | Production retrieval uses selected weights only after every fusion quality gate passes. | Task 4 |
| R7 | Production rerank uses 16 or 24 only after quality and 25% p95 latency gates pass; otherwise 32 remains. | Task 5 |
| R8 | `wiki_search` arguments, result fields, fail-soft order, models, chunks, index schema, and read-only behavior stay stable. | Tasks 1, 3, 4, 5, 6 |
| R9 | English/Russian docs, iwiki, version metadata, and verification evidence match shipped behavior. | Task 6 |

## File Structure

- Modify `src/iwiki_mcp/engine/fusion.py`: optional validated RRF weights.
- Modify `src/iwiki_mcp/retrieval.py`: one evidence-selected production weight map.
- Modify `src/iwiki_mcp/server.py`: one evidence-selected rerank batch limit when the
  limit is 16 or 24.
- Modify `eval/search_pipeline/fixtures.py`: 12 reviewed cases and query-class labels.
- Create `eval/search_pipeline/selection.py`: deterministic fusion replay, quality gates,
  p95 calculation, and rerank batch selection.
- Modify `eval/search_pipeline/instrumentation.py`: eval-only fusion and rerank-batch
  overrides.
- Modify `eval/search_pipeline/runner.py`: bounded Pareto experiment orchestration.
- Modify `eval/search_pipeline/__main__.py`: explicit `--pareto` experiment mode.
- Modify `eval/search_pipeline/report.py`: selection evidence in Markdown/HTML.
- Modify `tests/engine/test_fusion.py`, `tests/test_retrieval.py`, and
  `tests/test_server_search.py`: production behavior tests.
- Create `tests/eval/test_search_pipeline_selection.py`: deterministic selector tests.
- Modify `tests/eval/test_search_pipeline_runner.py` and
  `tests/eval/test_search_pipeline_report.py`: experiment, safety, and report tests.
- Modify `README.md` and `docs/README.ru.md`: actual weighted fusion/rerank behavior and
  eval command.
- Modify `pyproject.toml`, `src/iwiki_mcp/__init__.py`, and `uv.lock`: patch version
  `0.7.9` to `0.7.10`.
- Modify `docs/TODO.md`: chain state only through `$check-chain`.

## Task 1: Add Weighted RRF Primitive

**Closes:** R1 and the fusion part of R8.

**Files:**
- Modify: `src/iwiki_mcp/engine/fusion.py`
- Modify: `tests/engine/test_fusion.py`

- [ ] **Step 1: Write failing weighted-RRF and validation tests**

Add tests proving lower page/discovery weights cannot outvote a rank-1 direct section,
missing weights remain `1.0`, deterministic ties remain stable, and invalid explicit
weights fail closed:

```python
def test_fuse_ranked_weights_direct_section_above_broad_page_fanout():
    signals = {
        "semantic_page": [_hit("noise", "N")],
        "graph_page": [_hit("noise", "N")],
        "semantic_chunk": [_hit("answer", "A")],
    }

    fused = fuse_ranked(
        signals,
        limit=2,
        signal_weights={
            "semantic_page": 0.05,
            "graph_page": 0.01,
            "semantic_chunk": 1.0,
        },
    )

    assert [hit["file"] for hit in fused] == ["answer", "noise"]


def test_fuse_ranked_missing_weight_preserves_equal_rrf_contribution():
    signals = {
        "semantic": [_hit("a", "A")],
        "lexical": [_hit("b", "B")],
    }

    fused = fuse_ranked(signals, 2, signal_weights={"semantic": 1.0})

    assert [hit["file"] for hit in fused] == ["a", "b"]
    assert fused[0]["score"] == fused[1]["score"]


@pytest.mark.parametrize("weight", [0.0, -1.0, float("inf"), float("nan"), "bad"])
def test_fuse_ranked_rejects_invalid_explicit_weight(weight):
    with pytest.raises(ValueError, match="signal weight"):
        fuse_ranked(
            {"semantic": [_hit("a", "A")]},
            1,
            signal_weights={"semantic": weight},
        )
```

- [ ] **Step 2: Run the tests and verify RED**

```bash
uv run pytest -q tests/engine/test_fusion.py
```

Expected: new tests fail because `fuse_ranked` does not accept `signal_weights`.

- [ ] **Step 3: Implement minimal weighted RRF**

Change the function contract to:

```python
def fuse_ranked(
    signals: dict[str, list[dict]],
    limit: int,
    signal_weights: dict[str, float] | None = None,
) -> list[dict]:
```

Resolve each contribution once per signal. Missing mapping entries use `1.0`; explicit
values are converted to float and rejected with `ValueError` when conversion fails, the
value is non-finite, or the value is not positive. Replace the current contribution with:

```python
weight = _signal_weight(signal, signal_weights)
fused["score"] += weight / (_RRF_K + rank)
```

Keep identity handling, duplicate suppression, tie ordering, and `limit <= 0` behavior
unchanged.

- [ ] **Step 4: Run focused fusion tests and verify GREEN**

```bash
uv run pytest -q tests/engine/test_fusion.py
```

Expected: all fusion tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/iwiki_mcp/engine/fusion.py tests/engine/test_fusion.py
git commit -m "feat(search): support weighted rank fusion"
```

## Task 2: Add Reviewed Corpus And Deterministic Selection

**Closes:** R2, R3, and deterministic parts of R5/R8.

**Files:**
- Modify: `eval/search_pipeline/fixtures.py`
- Create: `eval/search_pipeline/selection.py`
- Create: `tests/eval/test_search_pipeline_selection.py`
- Modify: `tests/eval/test_search_pipeline_runner.py`

- [ ] **Step 1: Write failing corpus-contract tests**

Add an optional `query_class: str = "unspecified"` field to `BenchmarkCase`, then test
that `DEFAULT_LIVE_CASES` has 12 unique IDs and exactly two cases in each class:

```python
EXPECTED_CLASSES = {
    "exact_identifier",
    "semantic_paraphrase",
    "multi_intent",
    "repeated_heading",
    "graph_distractor",
    "competing_evidence",
}


def test_live_corpus_has_two_reviewed_cases_per_query_class():
    assert len(DEFAULT_LIVE_CASES) == 12
    assert len({case.case_id for case in DEFAULT_LIVE_CASES}) == 12
    counts = Counter(case.query_class for case in DEFAULT_LIVE_CASES)
    assert counts == {name: 2 for name in EXPECTED_CLASSES}
    assert all(case.relevant and case.intents and case.k == 8
               for case in DEFAULT_LIVE_CASES)
```

Use these reviewed cases and exact current section identities:

| Class | Case ID | Query | Graded relevance and intent groups |
| --- | --- | --- | --- |
| exact_identifier | `search-mode-api` | `IWIKI_SEARCH_MODE semantic lexical hybrid wiki_search mode enum` | `mcp-server.md#Tool surface:0` = 3 `[api]`; `retrieval.md#Hybrid search:0` = 2 `[retrieval]` |
| exact_identifier | `update-page-api` | `wiki_update_page heading new_body source description status` | `architecture.md#wiki_update_page transaction:0` = 3 `[transaction]`; `mcp-server.md#Tool surface:0` = 2 `[api]` |
| semantic_paraphrase | `stale-write-protection` | `prevent overwriting newer remote knowledge before changing a page` | `git-sync.md#Pre-write freshness guard:0` = 3 `[freshness]` |
| semantic_paraphrase | `binding-resolution` | `choose knowledge base and project domain from current workspace` | `base-binding.md#Resolving the binding:0` = 3 `[binding]`; `base-binding.md#Binding model:0` = 2 `[binding]` |
| multi_intent | `rerank-hydration` | `rerank candidates hydration stale provider top_n result fields` | `retrieval.md#Hybrid search:0` = 3 `[rerank]`; `retrieval.md#Result shape:0` = 2 `[shape]`; `mcp-server.md#Tool surface:0` = 2 `[api]` |
| multi_intent | `embedding-storage-config` | `configure embedding endpoint dimensions and persist vectors` | `installation.md#Required environment:0` = 3 `[config]`; `indexing.md#Embeddings client:0` = 3 `[config]`; `indexing.md#Vector store:0` = 2 `[storage]` |
| repeated_heading | `chunking` | `Markdown chunking summary section chunk repeated heading` | `indexing.md#Markdown chunking:0` = 3 `[chunking]` |
| repeated_heading | `okf-repeated-section` | `migrate apply OKF frontmatter repeated section chunks` | `okf-governance.md#Migrate and apply tools:0` = 3 `[migration]`; `okf-governance.md#Migrate and apply tools:1` = 3 `[migration]` |
| graph_distractor | `sync-locking` | `coordinate concurrent pull rebase push without repository races` | `git-sync.md#Inter-process locking:0` = 3 `[concurrency]`; `git-sync.md#Explicit sync:0` = 2 `[sync]` |
| graph_distractor | `related-sections` | `find neighboring knowledge through vectors links and backlinks` | `retrieval.md#Related sections:0` = 3 `[related]` |
| competing_evidence | `search-scope` | `project bound explicit domains scope resolution for wiki search` | `base-binding.md#Search scope:0` = 3 `[scope]`; `mcp-server.md#Tool surface:0` = 2 `[api]` |
| competing_evidence | `frontmatter-migration` | `derive type tags from source log then migrate metadata` | `okf-governance.md#Frontmatter assembly:0` = 3 `[assembly]`; `okf-governance.md#Migrate and apply tools:0` = 2 `[migration]` |

Every identity is prefixed with `iwiki-mcp/` in fixture code. Queries must describe the
named behavior without copying the complete target heading for semantic-paraphrase and
graph-distractor cases.

- [ ] **Step 2: Write failing deterministic selector tests**

Import and test this eval-only API:

```python
from eval.search_pipeline.selection import (
    GRAPH_WEIGHTS,
    PAGE_WEIGHTS,
    RERANK_BATCHES,
    fusion_weight_grid,
    replay_fusion,
    select_fusion_weights,
    select_rerank_batch,
)

assert PAGE_WEIGHTS == (0.025, 0.05, 0.1)
assert GRAPH_WEIGHTS == (0.01, 0.025, 0.05)
assert RERANK_BATCHES == (16, 24, 32)
```

Tests must prove:

```python
def test_fusion_grid_is_bounded_and_deterministic():
    first = fusion_weight_grid()
    assert first == fusion_weight_grid()
    assert len(first) == 8
    assert all(item["graph_page"] <= item["semantic_page"] for item in first)


def test_selector_rejects_recall_regression_before_ndcg_optimization():
    decision = select_fusion_weights(CASES, TRACES)
    rejected = {item["weights_key"]: item["reasons"]
                for item in decision["candidates"] if not item["passed"]}
    assert "recall_regression" in rejected[REGRESSING_KEY]


def test_selector_output_is_byte_stable_for_same_captured_traces():
    first = json.dumps(
        select_fusion_weights(CASES, TRACES), sort_keys=True, separators=(",", ":")
    )
    second = json.dumps(
        select_fusion_weights(CASES, TRACES), sort_keys=True, separators=(",", ":")
    )
    assert first == second
```

Add separate cases proving rejection for per-mode nDCG loss over `0.01`, a new
`lost_after_fusion_top_k` finding, rerank quality regression, and p95 improvement below
25%.

- [ ] **Step 3: Run new tests and verify RED**

```bash
uv run pytest -q tests/eval/test_search_pipeline_selection.py tests/eval/test_search_pipeline_runner.py
```

Expected: imports/contracts fail because selection module and query classes do not exist.

- [ ] **Step 4: Implement the bounded selectors**

`fusion_weight_grid()` emits direct weights `1.0`, equal page weights for
`semantic_page`/`lexical_page`, and one graph weight, retaining only
`graph_weight <= page_weight`. `replay_fusion()` rebuilds minimal hit dictionaries from
the trace's existing `stages.signals.identities` and calls production
`fusion.fuse_ranked`; it does not duplicate RRF math.

`select_fusion_weights()` evaluates equal RRF plus the eight mappings. Gate order is:

1. per-mode Recall@8 and intent coverage must not decrease from equal RRF;
2. per-mode mean nDCG@8 loss must be `<= 0.01`;
3. no new `lost_after_fusion_top_k` finding;
4. the two confirmed identities previously at ranks 22 and 26 must enter top-8;
5. maximize aggregate nDCG@8, then MRR@8;
6. break remaining ties by the smallest sum of absolute deviations from `1.0`, then a
   serialized weight key.

`select_rerank_batch()` uses batch 32 as baseline. Compute p95 with nearest-rank
`ceil(0.95 * n) - 1`. Evaluate 16 then 24 and return the first batch with unchanged
Recall/intent coverage, mean nDCG loss `<= 0.01`, no new missing/worsened finding, and
p95 rerank improvement `>= 25%`. Otherwise return batch 32 with status `needs_work` and
reason `latency_gate_unresolved`.

Both selectors return sorted candidate records containing metrics, `passed`, and stable
reason codes. They never include query text, base paths, provider URLs, or secrets.

- [ ] **Step 5: Run selector and existing eval tests**

```bash
uv run pytest -q tests/eval/test_search_pipeline_selection.py tests/eval/test_search_pipeline_metrics.py tests/eval/test_search_pipeline_analyzer.py tests/eval/test_search_pipeline_runner.py
```

Expected: all selected tests pass and repeated selector JSON is identical.

- [ ] **Step 6: Commit Task 2**

```bash
git add eval/search_pipeline/fixtures.py eval/search_pipeline/selection.py tests/eval/test_search_pipeline_selection.py tests/eval/test_search_pipeline_runner.py
git commit -m "feat(eval): select bounded search settings"
```

## Task 3: Run One Pareto Experiment Through The Existing CLI

**Closes:** R4, R5, and eval/read-only parts of R8.

**Files:**
- Modify: `eval/search_pipeline/instrumentation.py`
- Modify: `eval/search_pipeline/runner.py`
- Modify: `eval/search_pipeline/__main__.py`
- Modify: `eval/search_pipeline/report.py`
- Modify: `tests/eval/test_search_pipeline_runner.py`
- Modify: `tests/eval/test_search_pipeline_report.py`

- [ ] **Step 1: Write failing instrumentation and orchestration tests**

Extend `trace_query()` with eval-only keyword arguments:

```python
def trace_query(
    cfg: Config,
    base: str,
    case: BenchmarkCase,
    mode: str,
    rerank_enabled: bool,
    *,
    fusion_weights: dict[str, float] | None = None,
    rerank_candidate_limit: int | None = None,
) -> dict:
```

Tests assert the fusion override is passed to `fuse_ranked`, only the leading requested
rerank batch is hydrated/submitted, the complete fused pool remains in evidence, and
fallback still returns original fused top-k. Add an orchestration test with stub traces:

```python
def test_pareto_experiment_runs_required_matrix_and_excludes_warmups(monkeypatch):
    evidence = run_pareto_experiment(CFG, "iwiki-mcp", CASES, base="/wiki")

    assert evidence["kind"] == "live-pareto"
    assert evidence["baseline"]["modes"] == ["hybrid", "lexical", "semantic"]
    assert set(evidence["rerank_batches"]) == {"16", "24", "32"}
    assert all(run["sample_count"] == 24
               for run in evidence["rerank_batches"].values())
    assert evidence["run_settings"]["warmup_passes"] == 1
    assert evidence["run_settings"]["measured_passes"] == 2
```

Also assert invalid `--pareto` mode subsets exit 2 before provider calls and sanitized
errors contain no env values.

- [ ] **Step 2: Run focused eval tests and verify RED**

```bash
uv run pytest -q tests/eval/test_search_pipeline_runner.py tests/eval/test_search_pipeline_report.py
```

Expected: failures for missing overrides, `run_pareto_experiment`, and `--pareto`.

- [ ] **Step 3: Implement instrumentation overrides**

Pass `fusion_weights` directly to `fusion.fuse_ranked`. For rerank-enabled traces, use:

```python
rerank_pool = fused
if rerank_candidate_limit is not None:
    effective_limit = max(case.k, rerank_candidate_limit)
    rerank_pool = fused[:effective_limit]
hydrated = retrieval.hydrate_candidates(
    cfg,
    base,
    [dict(candidate) for candidate in rerank_pool],
    page_cache,
)
```

The fusion stage still records all 32 candidates. Hydration records the actual batch.
Unscored candidates are appended from the complete `fused` list. A rerank failure uses
`fused[:case.k]` exactly as before.

- [ ] **Step 4: Implement `run_pareto_experiment`**

Add one runner entry point that:

1. captures rerank-off traces for 12 cases in `hybrid`, `lexical`, and `semantic`;
2. calls `select_fusion_weights` on those captured signals;
3. stops with `decision.status = needs_work` when no fusion mapping passes;
4. for a passing mapping, runs hybrid batches 16, 24, and 32;
5. excludes one full warm-up pass per batch;
6. retains two measured passes, exactly 24 samples per batch;
7. calls `select_rerank_batch` and emits one production recommendation;
8. merges only evidence-supported unresolved findings into `ranked_backlog`.

Keep existing `run_live_traces()` behavior compatible. Add explicit optional overrides
rather than reading experiment settings from environment variables.

- [ ] **Step 5: Add `--pareto` and report sections**

`--pareto` selects `run_pareto_experiment`; without it, the existing live benchmark path
is unchanged. Pareto mode requires the default three-mode set. JSON gains bounded
`fusion_selection`, `rerank_batches`, and `decision` objects. Markdown/HTML gain compact
tables for weights, gate failures, batch quality, p50/p95, and final recommendation.

Do not render base paths, provider URLs, raw provider payloads, auth headers, keys, or raw
exceptions. Preserve sorted JSON keys and stable ordering of deterministic arrays.

- [ ] **Step 6: Run eval safety/report tests and CLI help**

```bash
uv run pytest -q tests/eval/test_search_pipeline_envfile.py tests/eval/test_search_pipeline_selection.py tests/eval/test_search_pipeline_runner.py tests/eval/test_search_pipeline_report.py
uv run python -m eval.search_pipeline --help
```

Expected: tests pass; help lists `--pareto`, `--env-file`, `--domain`, and `--out`.

- [ ] **Step 7: Commit Task 3**

```bash
git add eval/search_pipeline/instrumentation.py eval/search_pipeline/runner.py eval/search_pipeline/__main__.py eval/search_pipeline/report.py tests/eval/test_search_pipeline_runner.py tests/eval/test_search_pipeline_report.py
git commit -m "feat(eval): run Pareto search experiment"
```

## Task 4: Evidence Gate And Production Fusion

**Closes:** R6 and fusion-related parts of R8.

**Files:**
- Modify: `src/iwiki_mcp/retrieval.py`
- Modify: `tests/test_retrieval.py`
- Evidence only: operator-provided `tmp/creds.env` and a private `/tmp` output directory

- [ ] **Step 1: Run the read-only live Pareto experiment**

HUMAN CHECKPOINT satisfied by the already operator-provided `tmp/creds.env`; do not
print, copy, commit, or include that file in output.

```bash
evidence_dir="$(mktemp -d /tmp/.private/altuser/iwiki-search-pareto.XXXXXX)"
uv run python -m eval.search_pipeline --domain iwiki-mcp --out "$evidence_dir" --env-file tmp/creds.env --pareto
printf '%s\n' "$evidence_dir"
```

Expected: 12 cases, 36 rerank-off baseline traces, 24 measured samples for each rerank
batch, no failed queries, no secret-bearing output, and a deterministic fusion decision.

- [ ] **Step 2: Enforce the automatic fusion gate**

Continue only when JSON says `fusion_selection.status = selected`, no Recall/intent
regression exists, per-mode nDCG loss is `<= 0.01`, no new lost-after-fusion finding
exists, and the two confirmed identities enter top-8 without rerank. If any condition
fails, leave production fusion unchanged, record `needs_work`, and stop Tasks 4-5 rather
than weakening a gate.

- [ ] **Step 3: Write a failing retrieval wiring test**

Monkeypatch `retrieval.fusion.fuse_ranked`, call `prepare_read_candidates`, and assert the
provided `signal_weights` exactly match the selected JSON map. Also retain the existing
public field assertion:

```python
assert set(result) == {
    "domain", "file", "heading", "chunk", "score", "hit", "source"
}
```

- [ ] **Step 4: Run the retrieval test and verify RED**

```bash
uv run pytest -q tests/test_retrieval.py
```

Expected: the new wiring assertion fails because production retrieval still calls equal
RRF.

- [ ] **Step 5: Apply the selected fixed map**

Add a private module constant containing the exact selector output. The only permitted
values are direct `1.0`, page one of `0.025/0.05/0.1`, and graph one of
`0.01/0.025/0.05`. Pass it as `signal_weights` from
`prepare_read_candidates`. Do not add config fields, environment variables, or API
arguments.

- [ ] **Step 6: Verify retrieval behavior and rerank-off quality**

```bash
uv run pytest -q tests/engine/test_fusion.py tests/test_retrieval.py tests/test_retrieval_facets.py tests/test_server_search.py
```

Expected: tests pass; public fields and search modes remain unchanged.

- [ ] **Step 7: Commit Task 4**

```bash
git add src/iwiki_mcp/retrieval.py tests/test_retrieval.py
git commit -m "fix(search): weight direct retrieval evidence"
```

## Task 5: Evidence-Gated Production Rerank Batch

**Closes:** R7 and rerank-related parts of R8.

**Files:**
- Modify only when batch 16 or 24 passes: `src/iwiki_mcp/server.py`
- Modify only when batch 16 or 24 passes: `tests/test_server_search.py`

- [ ] **Step 1: Enforce the automatic rerank gate**

Read `decision.rerank_batch` from Task 4 evidence. Continue with server edits only for
16 or 24 and only when quality gates pass plus p95 rerank latency improves at least 25%
versus 32. If decision is 32, retain current production behavior and record
`latency_gate_unresolved`; do not claim the full follow-up accepted.

- [ ] **Step 2: Write failing bounded-batch server tests**

For a passing 16/24 decision, generate 32 preliminary candidates and assert hydration
receives only the selected budget for `k=8`. Add an explicit `k` above the budget and
assert hydration receives `max(k, budget)` candidates. Verify a scored candidate is
promoted, all unscored preliminary candidates keep original order, provider failure
returns unchanged top-k, and response keys/metadata are unchanged.

```python
assert hydrated_input == preliminary[:server._RERANK_CANDIDATE_LIMIT]
assert set(output) == {"results", "rerank"}
assert all("text" not in item for item in output["results"])
```

- [ ] **Step 3: Run server tests and verify RED**

```bash
uv run pytest -q tests/test_server_search.py
```

Expected: bounded hydration assertion fails because all candidates are currently
hydrated.

- [ ] **Step 4: Apply the passing literal batch**

If decision is 16, add `_RERANK_CANDIDATE_LIMIT = 16`; if decision is 24, add
`_RERANK_CANDIDATE_LIMIT = 24`. Preserve an explicit larger public `k` and slice
candidates only for hydration/provider submission:

```python
rerank_budget = max(requested_top_k, _RERANK_CANDIDATE_LIMIT)
rerank_candidates = candidates[:rerank_budget]
hydrated = retrieval.hydrate_candidates(
    cfg, bind.base, rerank_candidates, page_cache=page_cache
)
```

Keep `candidates` unchanged for fail-soft fallback and unscored append. Keep provider
`top_n=requested_top_k` and existing sanitized metadata.

- [ ] **Step 5: Run server and reranker regression tests**

```bash
uv run pytest -q tests/test_server_search.py tests/eval/test_reranker_experiment.py tests/eval/test_search_pipeline_runner.py
```

Expected: tests pass; bounded provider input and complete fallback order are proven.

- [ ] **Step 6: Commit Task 5 when a smaller batch passed**

```bash
git add src/iwiki_mcp/server.py tests/test_server_search.py
git commit -m "perf(search): bound rerank candidate batch"
```

If 32 remains, skip this commit and preserve the evidence-backed `needs_work` latency
outcome.

## Task 6: Documentation, Version, And Final Verification

**Closes:** R5, R8, and R9.

**Files:**
- Modify: `README.md`
- Modify: `docs/README.ru.md`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`
- Modify through chain gate: `docs/TODO.md`
- Update through iwiki MCP: `retrieval`, `mcp-server`, and
  `reference/search-pipeline-benchmark`

- [ ] **Step 1: Update repository documentation**

Document weighted RRF as an internal fixed policy, the actual rerank batch only when it
changed, preserved fail-soft behavior, and the reproducible Pareto command. English and
Russian docs must describe the same behavior. Do not include private output paths,
provider URLs, or credentials.

- [ ] **Step 2: Bump patch version**

Set `pyproject.toml`, `src/iwiki_mcp/__init__.py`, and the local package stanza in
`uv.lock` from `0.7.9` to `0.7.10`.

```bash
uv lock
uv run pytest -q tests/test_package.py
```

Expected: lock metadata is current and package/distribution versions both equal
`0.7.10`.

- [ ] **Step 3: Run focused verification**

```bash
uv run pytest -q tests/engine/test_fusion.py tests/test_retrieval.py tests/test_retrieval_facets.py tests/test_server_search.py tests/eval/test_search_pipeline_selection.py tests/eval/test_search_pipeline_metrics.py tests/eval/test_search_pipeline_analyzer.py tests/eval/test_search_pipeline_envfile.py tests/eval/test_search_pipeline_runner.py tests/eval/test_search_pipeline_report.py tests/test_package.py
uv run flake8 src/iwiki_mcp eval/search_pipeline tests/engine/test_fusion.py tests/test_retrieval.py tests/test_server_search.py tests/eval/test_search_pipeline_selection.py tests/eval/test_search_pipeline_runner.py tests/eval/test_search_pipeline_report.py
uv run python -m eval.search_pipeline --help
git diff --check
```

Expected: all focused tests pass, flake8 is clean, CLI help succeeds, diff check is
clean.

- [ ] **Step 4: Run full-suite comparison against baseline**

```bash
uv run pytest -q
uv run pytest -q -k "not test_repository_server_report_lists_current_search_modes_and_tool_surface"
```

Expected: no new failure. The first command may retain only the known missing
`docs/reports/iwiki-mcp-server-report.html` failure recorded before implementation; the
second command must pass completely. Any additional failure blocks completion.

- [ ] **Step 5: Rerun final live evidence and safety checks**

Rerun the Task 4 command after production constants are applied. Confirm report decision
matches source constants, all 12 cases complete, no API/model/chunk/index setting drift
exists, and evidence contains no provider URL, local base path, auth header, raw payload,
or env-file value. Compare production-base git HEAD/status before and after without
writing to it.

- [ ] **Step 6: Update iwiki through MCP and lint**

Use `wiki_update_page` for existing sections in `retrieval`, `mcp-server`, and
`reference/search-pipeline-benchmark`. Describe only behavior actually shipped. Then run
`wiki_lint`; broken refs, stale pages, or contradictory text block result completion.

- [ ] **Step 7: Run result reconciliation**

Run `$check-chain result docs/superpowers/plans/2026-07-28-search-pipeline-benchmark.md`.
Expected: every implemented plan step maps to diff/test/live evidence; a retained batch
32 leaves latency result `needs_work` rather than being reported as fixed.

- [ ] **Step 8: Commit final metadata and docs**

```bash
git add README.md docs/README.ru.md pyproject.toml src/iwiki_mcp/__init__.py uv.lock docs/TODO.md docs/superpowers/plans/2026-07-28-search-pipeline-benchmark.md
git commit -m "docs(search): record Pareto retrieval policy"
```

Do not stage `.gitignore`, `tmp/creds.env`, private evidence, generated caches, or any
unrelated file.

## Execution Stop Rules

- Stop before production fusion changes when no weight map passes every quality gate.
- Keep rerank batch 32 and mark latency `needs_work` when neither 16 nor 24 passes.
- Stop on any secret/path leakage, wiki-base write, public API/response change, new full
  suite failure, model/chunk/index drift, or failed read-only verification.
- Never weaken a metric gate to obtain a production change.

## Expected Result

- Preliminary fusion no longer loses the two confirmed relevant sections and does not
  regress Recall/intent coverage or per-mode nDCG beyond the approved tolerance.
- Rerank latency decreases only if live evidence proves a smaller fixed batch preserves
  trust; otherwise the current batch remains and the unresolved bottleneck is explicit.
- Reports retain per-stage metrics, evidence-backed bottlenecks, rejected alternatives,
  ranked backlog, deterministic replay, and safe live fingerprints.
- Public `wiki_search`, keys, wiki-base write behavior, chunking, embeddings, models, and
  index schema remain unchanged.
