---
review:
  plan_hash: e9e179df42cb843a
  last_run: 2026-07-29
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
---

# Bounded Fusion Experiment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox
> (`- [ ]`) syntax for tracking.

**Goal:** Compare four bounded fusion families using deterministic replay and one
conditional read-only live confirmation, without changing production search behavior.

**Architecture:** Keep candidate generation and selection in `eval/search_pipeline`.
Extend the generic fusion primitive only with a default-compatible RRF constant, then
apply direct multipliers, direct quotas, and fan-out caps through eval-only candidate
descriptors. `--pareto` captures or loads baseline evidence, selects one candidate under
the fixed gates, and runs one rerank-disabled live confirmation only when replay passes.

**Tech Stack:** Python 3.10+, existing `iwiki_mcp.engine.fusion`, existing
`eval.search_pipeline` package, pytest, standard-library dataclasses/JSON, uv.

---

## Baseline And Boundaries

- Existing 36-trace live evidence: Recall@8 `0.699074`, MRR@8 `0.531779`, nDCG@8
  `0.477597`, intent coverage `0.712963`, mean latency `130.753306 ms`.
- Existing weighted experiment rejected all eight maps with
  `needs_work: no_passing_weight_map`; production constants stayed unchanged.
- The new experiment evaluates exactly twelve Stage A candidates and at most six Stage B
  pairs. It never evaluates triples or a Cartesian grid.
- Production `retrieval.py`, `server.py`, `wiki_search` schema, rerank batch, chunks,
  models, and index schema remain unchanged.
- Replay and live runs remain read-only. Credentials stay in the operator-created env
  file and never enter reports or commits.
- Current full-suite baseline is `659 passed` plus one unrelated pre-existing failure
  because `docs/reports/iwiki-mcp-server-report.html` is absent. Any additional failure
  blocks completion.

## Human Checkpoint

Stop after this plan passes review. Tasks 1–6 require a new explicit user instruction to
start implementation. Plan approval alone does not authorize code edits, test execution,
provider calls, or the live experiment.

## Requirements And Closure Map

| ID | Requirement | Closed by |
| --- | --- | --- |
| R1 | RRF constants 10/20/40 are replayable while omitted value preserves 60. | Task 1 |
| R2 | Direct multipliers, direct quotas, and fan-out caps follow the approved exact semantics. | Task 2 |
| R3 | Stage A has exactly 12 candidates; Stage B has only pairs of passing family winners and at most 6 candidates. | Task 2 |
| R4 | Existing quality gates and deterministic tie-breaks select one winner or `needs_work`. | Task 2 |
| R5 | One replay winner can be confirmed once across 12 cases × 3 modes; failures cannot validate it. | Task 3 |
| R6 | CLI supports credential-free replay and live-first confirmation without exposing secrets or writing the wiki base. | Task 4 |
| R7 | JSON, Markdown, and HTML expose all candidates, metrics, reasons, recovery state, and final decision deterministically. | Task 4 |
| R8 | Docs, wiki, version metadata, and verification evidence match shipped eval behavior. | Tasks 5–6 |
| R9 | Production search behavior and public API remain unchanged. | Tasks 1–6 |

## File Structure

- Modify `src/iwiki_mcp/engine/fusion.py`: optional validated `rrf_k=60` argument.
- Modify `eval/search_pipeline/selection.py`: candidate descriptor, transformations,
  Stage A/Stage B generation, quality gates, and deterministic selection.
- Modify `eval/search_pipeline/instrumentation.py`: eval-only candidate application to
  live signal rankings.
- Modify `eval/search_pipeline/runner.py`: replay-only and conditional live-confirmation
  orchestration.
- Modify `eval/search_pipeline/__main__.py`: optional `--replay-evidence` input.
- Modify `eval/search_pipeline/report.py`: bounded-candidate and live-confirmation tables.
- Modify `tests/engine/test_fusion.py`: RRF constant compatibility and validation.
- Modify `tests/eval/test_search_pipeline_selection.py`: four families, combinations,
  gates, malformed evidence, and determinism.
- Modify `tests/eval/test_search_pipeline_runner.py`: replay/live orchestration and
  read-only behavior.
- Modify `tests/eval/test_search_pipeline_report.py`: deterministic sanitized rendering.
- Modify `README.md` and `docs/README.ru.md`: actual evidence-only command semantics.
- Modify `pyproject.toml`, `src/iwiki_mcp/__init__.py`, and `uv.lock`: patch version
  `0.7.10` to `0.7.11`.
- Modify `docs/TODO.md`: chain state through `$check-chain` only.

## Task 1: Add A Default-Compatible RRF Constant

**Closes:** R1 and the fusion-primitive part of R9.

**Files:**
- Modify: `src/iwiki_mcp/engine/fusion.py`
- Modify: `tests/engine/test_fusion.py`

- [ ] **Step 1: Write failing RRF constant tests**

Add tests proving explicit `60` is byte-equivalent to omission, a lower constant changes
rank sensitivity, and invalid values fail closed:

```python
def test_fuse_ranked_explicit_default_rrf_k_matches_omission():
    signals = {
        "semantic": [_hit("a", "A"), _hit("b", "B")],
        "lexical": [_hit("b", "B"), _hit("a", "A")],
    }
    assert fuse_ranked(signals, 2) == fuse_ranked(signals, 2, rrf_k=60)


def test_fuse_ranked_lower_rrf_k_increases_rank_difference():
    signals = {"semantic": [_hit("a", "A"), _hit("b", "B")]}
    default = fuse_ranked(signals, 2, rrf_k=60)
    sharper = fuse_ranked(signals, 2, rrf_k=10)
    assert sharper[0]["score"] - sharper[1]["score"] > (
        default[0]["score"] - default[1]["score"]
    )


@pytest.mark.parametrize("rrf_k", [0, -1, True, 1.5, "10"])
def test_fuse_ranked_rejects_invalid_rrf_k(rrf_k):
    with pytest.raises(ValueError, match="rrf_k"):
        fuse_ranked({"semantic": [_hit("a", "A")]}, 1, rrf_k=rrf_k)
```

- [ ] **Step 2: Run focused tests and verify RED**

```bash
uv run pytest -q tests/engine/test_fusion.py
```

Expected: new tests fail because `fuse_ranked` has no `rrf_k` argument.

- [ ] **Step 3: Implement the minimal optional argument**

Use this contract and keep every existing caller on the default:

```python
_RRF_K = 60


def _validated_rrf_k(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("rrf_k must be a positive integer")
    return value


def fuse_ranked(
    signals: dict[str, list[dict]],
    limit: int,
    signal_weights: dict[str, float] | None = None,
    *,
    rrf_k: int = _RRF_K,
) -> list[dict]:
    rrf_k = _validated_rrf_k(rrf_k)
    # Existing identity, duplicate, ordinal, and tie logic stays unchanged.
```

Replace only the contribution denominator with `rrf_k + rank`.

- [ ] **Step 4: Run fusion and retrieval compatibility tests**

```bash
uv run pytest -q tests/engine/test_fusion.py tests/test_retrieval.py
```

Expected: all tests pass; omitted `rrf_k` preserves existing production order.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/iwiki_mcp/engine/fusion.py tests/engine/test_fusion.py
git commit -m "feat(search): support configurable RRF constant"
```

## Task 2: Implement Four Eval Candidate Families And Selection

**Closes:** R2, R3, R4, and deterministic parts of R7/R9.

**Files:**
- Modify: `eval/search_pipeline/selection.py`
- Modify: `tests/eval/test_search_pipeline_selection.py`

- [ ] **Step 1: Write failing candidate-grid and transformation tests**

Define the expected descriptor and bounded grid in tests:

```python
BASELINE_FUSION = FusionCandidate()


def test_stage_a_grid_has_exactly_three_candidates_per_family():
    candidates = stage_a_candidates()
    assert len(candidates) == 12
    assert Counter(item.family for item in candidates) == {
        "rrf_k": 3,
        "direct_multiplier": 3,
        "direct_quota": 3,
        "fanout_cap": 3,
    }
    assert candidates == stage_a_candidates()


def test_fanout_cap_is_independent_per_broad_signal():
    signals = transform_candidate_signals(
        _ranked_signals(_trace_with_page_fanout()),
        FusionCandidate(family="fanout_cap", fanout_cap=1),
    )
    assert [_identity(hit) for hit in signals["semantic_page"]] == [
        "iwiki-mcp/page.md#A:0"
    ]
    assert [_identity(hit) for hit in signals["lexical_page"]] == [
        "iwiki-mcp/page.md#A:0"
    ]
    assert [_identity(hit) for hit in signals["semantic_chunk"]] == [
        "iwiki-mcp/direct.md#Answer:0"
    ]


def test_direct_quota_promotes_direct_identity_into_last_reserved_slot():
    ranking = replay_fusion_candidate(
        _trace_with_direct_target_at_rank_22(),
        FusionCandidate(family="direct_quota", direct_quota=1),
    )
    assert ranking[7] == "iwiki-mcp/answer.md#Target:0"
    assert ranking[:7] == _baseline_ranking()[:7]
```

Add parameterized invalid-descriptor tests for non-positive `rrf_k`, direct multiplier
below `1.0`, quota outside `0..8`, fan-out cap below `1`, unknown family, and descriptors
that set unrelated fields for a single-family Stage A candidate.

- [ ] **Step 2: Run transformation tests and verify RED**

```bash
uv run pytest -q tests/eval/test_search_pipeline_selection.py -k "stage_a or candidate or quota or fanout"
```

Expected: imports fail because the bounded candidate API does not exist.

- [ ] **Step 3: Add the immutable candidate descriptor and exact grids**

Implement:

```python
@dataclass(frozen=True)
class FusionCandidate:
    family: str = "baseline"
    rrf_k: int = 60
    direct_multiplier: float = 1.0
    direct_quota: int = 0
    fanout_cap: int | None = None
    components: tuple[str, ...] = ()

    def payload(self) -> dict:
        return {
            "family": self.family,
            "rrf_k": self.rrf_k,
            "direct_multiplier": self.direct_multiplier,
            "direct_quota": self.direct_quota,
            "fanout_cap": self.fanout_cap,
            "components": list(self.components),
        }


def stage_a_candidates() -> list[FusionCandidate]:
    return [
        *(FusionCandidate(family="rrf_k", rrf_k=value)
          for value in (10, 20, 40)),
        *(FusionCandidate(family="direct_multiplier", direct_multiplier=value)
          for value in (1.25, 1.5, 2.0)),
        *(FusionCandidate(family="direct_quota", direct_quota=value)
          for value in (1, 2, 3)),
        *(FusionCandidate(family="fanout_cap", fanout_cap=value)
          for value in (1, 2, 4)),
    ]
```

Validate descriptors before replay. Serialized keys use sorted compact JSON from
`payload()`.

- [ ] **Step 4: Implement eval-only transformations**

Add constants for direct and broad signals, cap each broad signal independently by
`(domain, file)`, call `fuse_ranked` with candidate `rrf_k` and direct weights, then
apply quota to the final `k` window:

```python
_DIRECT_SIGNALS = ("semantic_chunk", "lexical_section")
_BROAD_SIGNALS = ("semantic_page", "lexical_page", "graph_page")


def replay_fusion_candidate(trace: dict, candidate: FusionCandidate) -> list[str] | None:
    signals = _ranked_signals(trace)
    if signals is None:
        return None
    fused = fuse_candidate_signals(
        signals,
        candidate,
        limit=_unique_identity_count(signals),
        final_k=int(trace["k"]),
    )
    return [_identity(hit) for hit in fused]
```

`transform_candidate_signals(signals, candidate)` validates the descriptor and applies
only the broad-signal fan-out cap to copied hit lists. `fuse_candidate_signals` then
applies `rrf_k`, direct weights, and hit-dict quota ordering. Replay and live tracing call
these same helpers; neither mutates source signals or trace evidence.

For quota `q`, take the first `q` direct-only identities as the reserved set. Keep any
reserved identity already present in baseline top-k at its original position. For each
missing reserved identity, remove the lowest baseline-ranked top-k identity that is not
reserved and append the missing identity in direct-only order. Append the remaining
baseline identities afterward without duplicates. This puts only promoted identities in
the last reserved top-k positions and preserves relative order elsewhere.

- [ ] **Step 5: Write failing Stage A/Stage B and gate tests**

Tests must prove:

```python
def test_stage_b_contains_only_pairs_of_passing_family_winners():
    pairs = stage_b_candidates(PASSING_FAMILY_WINNERS)
    assert len(pairs) == 6
    assert all(item.family == "pair" for item in pairs)
    assert all(len(item.components) == 2 for item in pairs)
    assert pairs == stage_b_candidates(PASSING_FAMILY_WINNERS)


def test_selector_never_combines_rejected_family_winner():
    decision = select_fusion_candidate(CASES, TRACES)
    rejected = set(decision["family_rejections"])
    assert all(
        not rejected.intersection(item["families"])
        for item in decision["stage_b"]
    )


def test_selector_returns_needs_work_when_no_candidate_passes():
    decision = select_fusion_candidate(CASES, REGRESSING_TRACES)
    assert decision["status"] == "needs_work"
    assert decision["reason"] == "no_passing_fusion_candidate"
    assert decision["candidate"] is None
```

Retain regression tests for exact 36-case-mode parity, rank-22/rank-26 evidence,
Recall/intent coverage, per-mode nDCG `0.01`, new case-level fusion loss, malformed ranks
and ordinals, deterministic JSON, and existing `select_fusion_weights` behavior.

- [ ] **Step 6: Implement shared gate evaluation and bounded selection**

Refactor existing metric/gate logic into one helper used by the legacy weight selector
and new candidate selector. The new result shape is:

```python
{
    "status": "passed" | "needs_work",
    "reason": "no_passing_fusion_candidate" | None,
    "candidate": candidate.payload() | None,
    "baseline": {"metrics": baseline_metrics},
    "stage_a": stage_a_records,
    "family_winners": family_winner_records,
    "stage_b": stage_b_records,
}
```

Choose family winners and final winner by aggregate nDCG, aggregate MRR, fewer
transformations, then stable serialized key. Never generate more than six pairs.

- [ ] **Step 7: Run selection tests**

```bash
uv run pytest -q tests/eval/test_search_pipeline_selection.py tests/eval/test_search_pipeline_metrics.py tests/eval/test_search_pipeline_analyzer.py
```

Expected: all tests pass; identical inputs produce byte-identical selector JSON.

- [ ] **Step 8: Commit Task 2**

```bash
git add eval/search_pipeline/selection.py tests/eval/test_search_pipeline_selection.py
git commit -m "feat(eval): compare bounded fusion candidates"
```

## Task 3: Add One Conditional Live Confirmation

**Closes:** R5 and live/read-only parts of R9.

**Files:**
- Modify: `eval/search_pipeline/instrumentation.py`
- Modify: `eval/search_pipeline/runner.py`
- Modify: `tests/eval/test_search_pipeline_runner.py`

- [ ] **Step 1: Write failing instrumentation tests**

Extend `trace_query` and `run_live_traces` with one eval-only descriptor:

```python
def test_trace_query_applies_eval_candidate_without_changing_default(monkeypatch):
    candidate = FusionCandidate(family="rrf_k", rrf_k=20)
    candidate_trace = trace_query(
        CFG, BASE, CASE, "hybrid", False, fusion_candidate=candidate
    )
    default_trace = trace_query(CFG, BASE, CASE, "hybrid", False)
    assert candidate_trace["stages"]["fusion"]["candidate"] == candidate.payload()
    assert "candidate" not in default_trace["stages"]["fusion"]


def test_trace_query_rejects_candidate_and_legacy_weights_together():
    with pytest.raises(ValueError, match="fusion override"):
        trace_query(
            CFG,
            BASE,
            CASE,
            "hybrid",
            False,
            fusion_weights={"semantic_chunk": 1.0},
            fusion_candidate=FusionCandidate(family="rrf_k", rrf_k=20),
        )
```

Also prove the complete transformed fused pool stays in evidence, hydration remains
unchanged, rerank remains disabled, and no migration/write helper is called.

- [ ] **Step 2: Write failing orchestration tests**

```python
def test_experiment_stops_before_live_confirmation_when_replay_needs_work(monkeypatch):
    monkeypatch.setattr(runner, "select_fusion_candidate", lambda *_: {
        "status": "needs_work",
        "reason": "no_passing_fusion_candidate",
        "candidate": None,
        "stage_a": [],
        "stage_b": [],
    })
    evidence = runner.run_bounded_fusion_experiment({}, "iwiki-mcp", CASES, base=BASE)
    assert evidence["decision"]["status"] == "needs_work"
    assert "live_confirmation" not in evidence


def test_experiment_runs_exactly_one_winner_confirmation(monkeypatch):
    evidence = runner.run_bounded_fusion_experiment({}, "iwiki-mcp", CASES, base=BASE)
    assert evidence["baseline"]["summary"]["rollup"]["case_count"] == 36
    assert evidence["live_confirmation"]["summary"]["rollup"]["case_count"] == 36
    assert evidence["decision"]["status"] in {"validated_candidate", "needs_work"}
    assert CONFIRMATION_CANDIDATES == [evidence["fusion_selection"]["candidate"]]
```

Add cases for one failed live sample, missing case/mode, live Recall/intent regression,
nDCG loss, and new fusion finding. Every case must return `needs_work`.

- [ ] **Step 3: Run runner tests and verify RED**

```bash
uv run pytest -q tests/eval/test_search_pipeline_runner.py -k "fusion_candidate or bounded_fusion"
```

Expected: missing argument and runner API failures.

- [ ] **Step 4: Implement eval-only live candidate application**

Call Task 2's shared `fuse_candidate_signals` helper with the live raw signal dict, so
replay and live tracing use exactly the same algorithm. Add:

```python
def trace_query(
    cfg: Config,
    base: str,
    case: BenchmarkCase,
    mode: str,
    rerank_enabled: bool,
    *,
    fusion_weights: dict[str, float] | None = None,
    fusion_candidate: FusionCandidate | None = None,
    rerank_candidate_limit: int | None = None,
) -> dict:
```

Reject simultaneous old/new overrides. Default calls continue through existing
equal-weight `rrf_k=60` fusion. Candidate traces record only the sanitized descriptor.

- [ ] **Step 5: Implement runner orchestration and live gates**

`run_bounded_fusion_experiment` captures one rerank-disabled 12×3 baseline, calls
`select_fusion_candidate`, and runs one 12×3 winner confirmation only after replay
passes. Reuse the same gate helper on live identities. Return:

```python
{
    "kind": "live-bounded-fusion",
    "baseline": safe_baseline,
    "fusion_selection": selection,
    "live_confirmation": safe_confirmation,
    "decision": {
        "status": "validated_candidate" | "needs_work",
        "reason": reason,
        "candidate": selected_payload_or_none,
    },
}
```

The function never calls rerank and never applies a production constant.

- [ ] **Step 6: Run focused instrumentation and runner tests**

```bash
uv run pytest -q tests/eval/test_search_pipeline_runner.py tests/eval/test_search_pipeline_selection.py
```

Expected: all tests pass; live validation uses exactly one candidate run.

- [ ] **Step 7: Commit Task 3**

```bash
git add eval/search_pipeline/instrumentation.py eval/search_pipeline/runner.py tests/eval/test_search_pipeline_runner.py
git commit -m "feat(eval): confirm one bounded fusion candidate"
```

## Task 4: Add Replay CLI And Complete Reports

**Closes:** R6, R7, and report-safety parts of R9.

**Files:**
- Modify: `eval/search_pipeline/__main__.py`
- Modify: `eval/search_pipeline/runner.py`
- Modify: `eval/search_pipeline/report.py`
- Modify: `tests/eval/test_search_pipeline_runner.py`
- Modify: `tests/eval/test_search_pipeline_report.py`

- [ ] **Step 1: Write failing replay CLI tests**

```python
def test_cli_replays_evidence_without_loading_provider_config(tmp_path, monkeypatch):
    evidence_path = tmp_path / "baseline.json"
    evidence_path.write_text(json.dumps(BASELINE_EVIDENCE), encoding="utf-8")
    monkeypatch.setattr(Config, "from_env", _fail_if_called)
    result = main([
        "--out", str(tmp_path / "report"),
        "--pareto",
        "--replay-evidence", str(evidence_path),
    ])
    assert result == 0


def test_cli_rejects_replay_file_inside_output_directory(tmp_path):
    output = tmp_path / "report"
    evidence_path = output / "baseline.json"
    result = main([
        "--out", str(output),
        "--pareto",
        "--replay-evidence", str(evidence_path),
    ])
    assert result == 2
```

Add invalid JSON/schema/path tests. Error text may contain the safe option name but not
file contents, provider values, env-file paths, or resolved wiki-base paths.

- [ ] **Step 2: Write failing report tests**

Assert Markdown and HTML include all twelve Stage A rows, family winners, no more than
six Stage B rows, exact rejection reasons, confirmed-loss recovery, live confirmation,
and final decision. Assert escaping of candidate/reason strings and deterministic sorted
JSON. Recursively assert that persisted objects contain no exact `query`, `provider_url`,
`authorization`, `api_key`, `env_file`, or `base_path` keys. Sentinel secret, URL,
exception, env-file, and local-path values must be absent from all three report formats.

```python
def test_bounded_report_contains_all_selection_stages():
    markdown = render_markdown_report(BOUNDED_EVIDENCE)
    html = render_html_report(BOUNDED_EVIDENCE)
    assert "Stage A Candidates" in markdown
    assert "Family Winners" in markdown
    assert "Stage B Pairs" in markdown
    assert "Live Confirmation" in markdown
    assert "Stage A Candidates" in html
    assert "no_passing_fusion_candidate" in html
```

- [ ] **Step 3: Run CLI/report tests and verify RED**

```bash
uv run pytest -q tests/eval/test_search_pipeline_runner.py tests/eval/test_search_pipeline_report.py
```

Expected: failures for missing `--replay-evidence` and bounded report sections.

- [ ] **Step 4: Implement replay-only loading**

Add `--replay-evidence PATH`, valid only with `--pareto`. Resolve and validate the input
before `Config.from_env`. Load JSON through `json.load`, require `traces` or
`baseline.traces`, and pass sanitized traces to `run_bounded_fusion_replay`. Replay mode
never resolves a wiki binding or initializes provider configuration.

- [ ] **Step 5: Route live `--pareto` to the new experiment**

Without `--replay-evidence`, preserve existing env-file safety checks, load config, and
run `run_bounded_fusion_experiment`. Remove rerank-batch execution from this CLI path;
keep legacy selector functions tested and importable for prior evidence compatibility.

- [ ] **Step 6: Render bounded evidence deterministically**

Add separate rendering helpers for Stage A, family winners, Stage B, live confirmation,
and recommendation. Use sorted candidate keys and existing `_md_cell`/`_html_cell`
escaping. `write_reports` retains sorted JSON and the same three output files.

- [ ] **Step 7: Run all eval tests**

```bash
uv run pytest -q tests/eval
```

Expected: all eval tests pass, including legacy report and weighted-selector regression
coverage.

- [ ] **Step 8: Commit Task 4**

```bash
git add eval/search_pipeline/__main__.py eval/search_pipeline/runner.py eval/search_pipeline/report.py tests/eval/test_search_pipeline_runner.py tests/eval/test_search_pipeline_report.py
git commit -m "feat(eval): report bounded fusion evidence"
```

## Task 5: Document The Evidence-Only Workflow And Bump Version

**Closes:** documentation/version parts of R8 and R9.

**Files:**
- Modify: `README.md`
- Modify: `docs/README.ru.md`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

- [ ] **Step 1: Update English and Russian command documentation**

Document both commands:

```bash
uv run python -m eval.search_pipeline --out ./fusion-replay --pareto --replay-evidence /path/to/search-pipeline-benchmark.json
uv run python -m eval.search_pipeline --domain iwiki-mcp --out ./fusion-live --env-file /path/to/operator.env --pareto
```

State that replay needs no credentials, live mode runs one winner confirmation only when
replay passes, no result changes production, and rerank-budget evaluation is deferred.

- [ ] **Step 2: Bump package version to 0.7.11**

Change `pyproject.toml` and `src/iwiki_mcp/__init__.py` from `0.7.10` to `0.7.11`, then
refresh the lock:

```bash
uv lock
```

Expected: only the local package version changes in `uv.lock`.

- [ ] **Step 3: Verify docs, CLI help, and package metadata**

```bash
uv run python -m eval.search_pipeline --help
uv run python -c "import iwiki_mcp; assert iwiki_mcp.__version__ == '0.7.11'"
git diff --check
```

Expected: help lists `--replay-evidence`; metadata assertion and diff check pass.

- [ ] **Step 4: Update iwiki after behavior exists**

Use iwiki MCP tools to update `iwiki-mcp/reference/search-pipeline-benchmark`, describing
the four families, replay-only mode, conditional live confirmation, and unchanged
production behavior. Run `wiki_lint`; broken and stale lists must be empty. Existing
advisory orphan entries do not block this task.

- [ ] **Step 5: Commit Task 5**

```bash
git add README.md docs/README.ru.md pyproject.toml src/iwiki_mcp/__init__.py uv.lock
git commit -m "docs(eval): document bounded fusion benchmark"
```

## Task 6: Verify Determinism And Run Real Evidence

**Closes:** R8 and the observable experiment outcome. This task ends at evidence; it
does not change production.

**Files:**
- Modify: `docs/TODO.md` through `$check-chain result` only
- Evidence output: operator-selected private directory outside git and outside
  `tmp/creds.env`

- [ ] **Step 1: Run focused and compatibility tests**

```bash
uv run pytest -q tests/engine/test_fusion.py tests/eval
uv run pytest -q tests/test_retrieval.py tests/test_server_search.py
uv run python -m compileall -q src eval
```

Expected: all commands pass.

- [ ] **Step 2: Run the full suite and compare with baseline**

```bash
uv run pytest -q
```

Expected: no new failures. The sole accepted baseline failure, when still present, is
`tests/test_resources.py::test_repository_server_report_lists_current_search_modes_and_tool_surface`
because `docs/reports/iwiki-mcp-server-report.html` is absent. Any other failure blocks
the experiment.

- [ ] **Step 3: Prove replay determinism twice**

```bash
uv run python -m eval.search_pipeline --out /tmp/iwiki-fusion-replay-a --pareto --replay-evidence /tmp/.private/altuser/iwiki-search-pareto.qVDiNI/search-pipeline-benchmark.json
uv run python -m eval.search_pipeline --out /tmp/iwiki-fusion-replay-b --pareto --replay-evidence /tmp/.private/altuser/iwiki-search-pareto.qVDiNI/search-pipeline-benchmark.json
```

Compare normalized JSON after removing only timestamps and output-path fields. Expected:
candidate records, metrics, reasons, family winners, pairs, and decision are identical.
If the source evidence path no longer exists, use the latest sanitized baseline JSON
produced by the benchmark; do not recreate or copy credentials.

Use these exact comparison commands after the two runs:

```bash
jq -S 'walk(if type == "object" then del(.timestamp, .output_path, .out_dir) else . end)' /tmp/iwiki-fusion-replay-a/search-pipeline-benchmark.json > /tmp/iwiki-fusion-replay-a.normalized.json
jq -S 'walk(if type == "object" then del(.timestamp, .output_path, .out_dir) else . end)' /tmp/iwiki-fusion-replay-b/search-pipeline-benchmark.json > /tmp/iwiki-fusion-replay-b.normalized.json
cmp /tmp/iwiki-fusion-replay-a.normalized.json /tmp/iwiki-fusion-replay-b.normalized.json
```

Expected: `cmp` exits `0` with no output.

- [ ] **Step 4: Run one read-only live-first experiment**

Create a private output directory and run:

```bash
fusion_output_dir="$(mktemp -d /tmp/iwiki-fusion-live.XXXXXX)"
uv run python -m eval.search_pipeline --domain iwiki-mcp --out "$fusion_output_dir" --env-file tmp/creds.env --pareto
```

Expected: 36 baseline samples, exactly 12 Stage A candidates, no more than 6 Stage B
pairs, and either `needs_work` with exact reasons or one 36-sample live confirmation.
No wiki-base files change during the command.

- [ ] **Step 5: Scan reports for secret and path leakage**

Inspect JSON/Markdown/HTML for forbidden keys and path/provider markers without printing
secret values:

```bash
if grep -RE '"(query|provider_url|authorization|api_key|env_file|base_path)"|Bearer |https?://|tmp/creds\.env|/home/' "$fusion_output_dir"; then exit 1; fi
```

Expected: no matches and exit `0`. Sentinel-based tests cover literal configured secret
values without reading or printing operator credentials. Any match blocks completion and
requires a report-sanitization fix before rerun.

- [ ] **Step 6: Record evidence without applying production changes**

Summarize the winning or rejected families, metric deltas, confirmed-loss recovery, new
losses, and live status. If `validated_candidate` exists, stop and request a separate
production-change approval. If no candidate passes, keep `needs_work` and rank the
remaining evidence-backed bottlenecks. Do not modify `retrieval.py` or `server.py`.

- [ ] **Step 7: Run result reconciliation**

Run `$check-chain result docs/superpowers/plans/2026-07-28-search-pipeline-benchmark.md`.
Expected: chain result is `OK` when every implementation step is complete even when the
benchmark decision is `needs_work`. TODO notes the benchmark decision separately and
does not claim a production fix.

## Plan Summary

This plan implements only an eval experiment. It adds one default-compatible fusion
parameter, evaluates four approved candidate families, limits combinations, validates
one replay winner live, and produces sanitized evidence. It closes uncertainty about
which bounded fusion strategy survives current gates; it does not promise that a winner
exists and does not change production search.

## Verification Summary

- Unit evidence: fusion constant, four transformations, exact grid sizes, gates, stable
  ties, malformed inputs, and legacy-selector compatibility.
- Integration evidence: replay-only CLI without config, live-first 12×3 baseline,
  exactly one conditional candidate confirmation, read-only guards, and report safety.
- Real evidence: deterministic replay twice plus one operator-credential live run.
- Human checkpoint: any production application remains a separate proposal after this
  plan finishes.
