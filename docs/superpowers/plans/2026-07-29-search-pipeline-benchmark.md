---
review:
  plan_hash: 304b6790cbaa5d4e
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

# Reproducible Hard-Negative Cases Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox
> (`- [ ]`) syntax for tracking.

**Goal:** Replace historical rank-22/rank-26 gates with two reviewed, reproducible
hard-negative case contracts, then repeat the same bounded fusion experiment without
changing production search.

**Architecture:** Extend `BenchmarkCase` with typed hard-negative targets and derive
activation from the exact baseline trace used for candidate comparison. Selection
requires recovery only for active targets, fails closed for invalid contracts, and
returns diagnostic `needs_work` when fewer than two contracts are active. Reports expose
activation and recovery evidence; no candidate is applied to production.

**Tech Stack:** Python 3.10+, dataclasses, existing `eval.search_pipeline` fixtures,
analyzer, selector, reports, pytest, JSON/Markdown/HTML, uv.

---

## Evidence And Boundaries

- The 2026-07-28 and 2026-07-29 live baselines share eighteen exact
  `lost_after_fusion_topk` case/mode/identity records.
- The reviewed contracts use two distinct single-intent cases and source pages:
  `related-sections` / `semantic` /
  `iwiki-mcp/retrieval.md#Related sections:0`, and
  `stale-write-protection` / `lexical` /
  `iwiki-mcp/git-sync.md#Pre-write freshness guard:0`.
- Absolute fused rank is diagnostic evidence, not an acceptance invariant.
- At least two distinct contracts must be active in the baseline used for a selection
  decision. Fewer than two means `needs_work: hard_negative_evidence_incomplete`.
- Recall, intent coverage, nDCG, new-loss, deterministic tie, case-mode completeness,
  and replay-integrity gates remain unchanged.
- `retrieval.py`, `server.py`, public `wiki_search`, chunks, embeddings, index schema,
  and rerank budget remain unchanged.
- The benchmark remains read-only. Credentials are used only from the operator env file
  for the final live run and never enter reports or git.

## Requirements And Closure Map

| ID | Requirement | Closed by |
| --- | --- | --- |
| H1 | Hard-negative contracts are typed, reviewed, deterministic, and tied to relevant fixture identities. | Task 1 |
| H2 | Baseline validation classifies each contract as active, unavailable, or invalid without using absolute rank constants. | Task 1 |
| H3 | Candidate recovery applies only to active contracts; invalid and insufficient evidence fail closed. | Task 1 |
| H4 | JSON, Markdown, and HTML expose activation, baseline rank, mode, identity, and recovery safely. | Task 2 |
| H5 | Existing replay/live CLI, four candidate families, quality gates, API safety, and read-only behavior remain compatible. | Tasks 1–3 |
| H6 | Two deterministic replays and one fresh live run repeat the bounded experiment and record an evidence-backed decision. | Task 4 |

## File Structure

- Modify `eval/search_pipeline/fixtures.py`: typed `HardNegativeTarget` and two reviewed
  live-case contracts.
- Modify `eval/search_pipeline/selection.py`: contract validation, activation evidence,
  recovery gates, and removal of positional rank requirements.
- Modify `eval/search_pipeline/runner.py`: use the same baseline-derived hard-negative
  records for live confirmation instead of a separate rank-based check.
- Modify `eval/search_pipeline/report.py`: hard-negative evidence tables.
- Modify `tests/eval/test_search_pipeline_selection.py`: contract and selector tests.
- Modify `tests/eval/test_search_pipeline_report.py`: deterministic sanitized rendering.
- Modify `tests/eval/test_search_pipeline_runner.py`: unchanged CLI/live orchestration
  compatibility and decision propagation.
- Modify `README.md` and `docs/README.ru.md`: actual hard-negative gate semantics.
- Modify `pyproject.toml`, `src/iwiki_mcp/__init__.py`, and `uv.lock`: patch version
  `0.7.11` to `0.7.12`.
- Modify `docs/TODO.md` only through chain reconciliation.
- Update iwiki `reference/search-pipeline-benchmark` after behavior exists.

## Task 1: Add Reviewed Hard-Negative Contracts And Gates

**Closes:** H1, H2, H3, and selector parts of H5.

**Files:**
- Modify: `eval/search_pipeline/fixtures.py`
- Modify: `eval/search_pipeline/selection.py`
- Modify: `eval/search_pipeline/runner.py`
- Modify: `tests/eval/test_search_pipeline_selection.py`
- Modify: `tests/eval/test_search_pipeline_runner.py`

- [ ] **Step 1: Write failing fixture-contract tests**

Add a frozen descriptor and an optional tuple on `BenchmarkCase`:

```python
@dataclass(frozen=True)
class HardNegativeTarget:
    identity: str
    mode: str


@dataclass(frozen=True)
class BenchmarkCase:
    # Existing fields stay unchanged.
    hard_negatives: tuple[HardNegativeTarget, ...] = ()
```

Assert the live corpus has exactly the two reviewed contracts, distinct identities,
valid required modes, and targets contained in each case's `relevant` mapping:

```python
def test_live_corpus_has_two_reviewed_hard_negative_contracts():
    contracts = sorted([
        (case.case_id, target.mode, target.identity)
        for case in DEFAULT_LIVE_CASES
        for target in case.hard_negatives
    ])
    assert contracts == [
        (
            "related-sections",
            "semantic",
            "iwiki-mcp/retrieval.md#Related sections:0",
        ),
        (
            "stale-write-protection",
            "lexical",
            "iwiki-mcp/git-sync.md#Pre-write freshness guard:0",
        ),
    ]
```

- [ ] **Step 2: Run fixture tests and verify RED**

```bash
uv run pytest -q tests/eval/test_search_pipeline_selection.py -k hard_negative
```

Expected: collection or import fails because `HardNegativeTarget` and
`BenchmarkCase.hard_negatives` do not exist.

- [ ] **Step 3: Implement the typed fixture contracts**

Add the descriptor and attach the exact contracts to `related-sections` and
`stale-write-protection`. Keep all existing queries, relevance grades, intents, and
`k=8` unchanged.

- [ ] **Step 4: Write failing activation and validation tests**

Cover these exact states through `select_fusion_candidate`:

Import `replace` from `dataclasses` and `HardNegativeTarget` from fixtures, then add:

```python
def _hard_negative_case(
    *,
    mode="semantic",
    identity="iwiki-mcp/mcp-server.md#Tool surface:0",
):
    case = _case(k=8)
    return replace(
        case,
        hard_negatives=(HardNegativeTarget(identity=identity, mode=mode),),
    )


def _trace_with_target_at_fused_rank(rank, *, mode="semantic"):
    target = "iwiki-mcp/mcp-server.md#Tool surface:0"
    noise = [
        f"iwiki-mcp/noise-{index}.md#Noise:0"
        for index in range(1, rank)
    ]
    identities = [*noise, target]
    return _trace(
        mode=mode,
        signals={"semantic_chunk": identities},
        ranking=identities[:8],
        k=8,
    )


def test_hard_negative_is_active_without_fixed_rank_dependency():
    decision = select_fusion_candidate(
        [_hard_negative_case(mode="semantic")],
        _all_modes(_trace_with_target_at_fused_rank(13)),
    )
    assert decision["hard_negatives"][0]["state"] == "active"
    assert decision["hard_negatives"][0]["baseline_rank"] == 13


def test_hard_negative_is_unavailable_when_target_is_inside_top_k():
    decision = select_fusion_candidate(
        [_hard_negative_case(mode="semantic")],
        _all_modes(_trace_with_target_at_fused_rank(4)),
    )
    assert decision["reason"] == "hard_negative_evidence_incomplete"
    assert decision["hard_negatives"][0]["state"] == "unavailable"


def test_hard_negative_contract_is_invalid_when_target_is_not_relevant():
    decision = select_fusion_candidate(
        [_hard_negative_case(identity="iwiki-mcp/missing.md#Nope:0")],
        _all_modes(_trace()),
    )
    assert decision["reason"] == "hard_negative_evidence_invalid"
    assert decision["hard_negatives"][0]["state"] == "invalid"
```

Also prove two active contracts can pass evidence completeness, candidate recovery checks
the exact active identities in final top-k, duplicate contracts fail closed, and neither
rank `22` nor `26` has special meaning. Add a runner test proving live confirmation uses
the same active records and has no positional-rank fallback.

- [ ] **Step 5: Run selector tests and verify RED**

```bash
uv run pytest -q tests/eval/test_search_pipeline_selection.py -k hard_negative
```

Expected: assertions fail because selection still uses `_CONFIRMED_LOSS_RANKS`.

- [ ] **Step 6: Implement baseline-derived activation**

Replace `_CONFIRMED_LOSS_RANKS` and `_confirmed_loss_records` with a public eval helper
`hard_negative_records(cases, traces)` returning
sorted sanitized records shaped as:

```python
{
    "case_id": case.case_id,
    "mode": target.mode,
    "identity": target.identity,
    "state": "active" | "unavailable" | "invalid",
    "baseline_rank": int | None,
}
```

Validation rules:

- invalid: mode outside `hybrid|lexical|semantic`, target absent from fixture relevance,
  duplicate `(case_id, mode, identity)`, missing matching trace, or malformed ranking;
- active: matching baseline finding is `lost_after_fusion_topk`, identity equals target,
  and baseline rank is strictly greater than that trace's `k`;
- unavailable: valid contract whose target is not a baseline post-top-k fusion loss.

Use `hard_negative_evidence_invalid` before completeness checks. Require at least two
distinct active records for candidate selection. Pass only active records into recovery
checks and require each exact identity in that matching candidate ranking's final `k`.
Preserve all other gate reasons and stable sorting.

In `runner.py`, replace the `_confirmed_loss_records` import and the independent
`{22, 26}` completeness calculation inside live gate evaluation. Call
`hard_negative_records(case_list, baseline_traces)`, reject invalid/incomplete states
with the same selector reasons, and pass the active `(case_id, mode, identity)` records
to `_fusion_gate_reasons`. This makes replay selection and live confirmation share one
contract implementation.

- [ ] **Step 7: Run all selection tests and verify GREEN**

```bash
uv run pytest -q tests/eval/test_search_pipeline_selection.py tests/eval/test_search_pipeline_runner.py
```

Expected: all current and new selector tests pass; legacy weight-map tests remain green.

- [ ] **Step 8: Commit Task 1**

```bash
git add eval/search_pipeline/fixtures.py eval/search_pipeline/selection.py eval/search_pipeline/runner.py tests/eval/test_search_pipeline_selection.py tests/eval/test_search_pipeline_runner.py
git commit -m "feat(eval): validate hard-negative retrieval cases"
```

## Task 2: Expose Hard-Negative Evidence In Reports

**Closes:** H4 and report parts of H5.

**Files:**
- Modify: `eval/search_pipeline/report.py`
- Modify: `tests/eval/test_search_pipeline_report.py`

- [ ] **Step 1: Write failing report and propagation tests**

Add bounded evidence containing one active and one unavailable target. Assert JSON keeps
the sorted structured records, Markdown contains `Hard-Negative Cases`, and HTML contains
the same case/mode/identity/state/rank values with escaping.

```python
def test_bounded_report_contains_hard_negative_activation_table():
    markdown = render_markdown_report(HARD_NEGATIVE_EVIDENCE)
    html = render_html_report(HARD_NEGATIVE_EVIDENCE)
    assert "Hard-Negative Cases" in markdown
    assert "related-sections" in markdown
    assert "active" in html
    assert "unavailable" in html
```

- [ ] **Step 2: Run report/runner tests and verify RED**

```bash
uv run pytest -q tests/eval/test_search_pipeline_report.py -k "hard_negative or bounded"
```

Expected: report heading and activation records are absent.

- [ ] **Step 3: Implement deterministic report tables**

Render `fusion_selection.hard_negatives` sorted by `(case_id, mode, identity)` with
columns `Case`, `Mode`, `Identity`, `State`, and `Baseline rank`. Reuse existing Markdown
and HTML escaping and `sanitize_evidence`; add no new persistence path or raw exception.
Keep Stage A, family winner, Stage B, and live-confirmation sections unchanged.

- [ ] **Step 4: Run all eval tests and verify GREEN**

```bash
uv run pytest -q tests/eval
```

Expected: all eval tests pass, including legacy report and replay/live orchestration.

- [ ] **Step 5: Commit Task 2**

```bash
git add eval/search_pipeline/report.py tests/eval/test_search_pipeline_report.py
git commit -m "feat(eval): report hard-negative evidence"
```

## Task 3: Document The Revised Gate And Bump Version

**Closes:** documentation/version parts of H5.

**Files:**
- Modify: `README.md`
- Modify: `docs/README.ru.md`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

- [ ] **Step 1: Update English and Russian behavior documentation**

Document baseline-derived activation, the two reviewed contracts, the minimum-two rule,
and the distinction between `hard_negative_evidence_invalid`,
`hard_negative_evidence_incomplete`, and a candidate quality rejection. State that
absolute ranks are diagnostic only and production remains unchanged.

- [ ] **Step 2: Bump package version to 0.7.12**

Update `pyproject.toml` and `src/iwiki_mcp/__init__.py`, then refresh only local lock
metadata:

```bash
uv lock
uv run python -c "import iwiki_mcp; assert iwiki_mcp.__version__ == '0.7.12'"
```

Expected: version assertion passes and dependency versions do not drift.

- [ ] **Step 3: Update iwiki and lint after behavior exists**

Update `iwiki-mcp/reference/search-pipeline-benchmark` through `wiki_update_page` with
the reviewed contracts and baseline-derived gate. Run `wiki_lint`; changed benchmark
sources must not be stale and broken refs must be empty. Do not reindex or write wiki
during benchmark execution.

- [ ] **Step 4: Verify docs and commit Task 3**

```bash
uv run python -m eval.search_pipeline --help
git diff --check
git add README.md docs/README.ru.md pyproject.toml src/iwiki_mcp/__init__.py uv.lock
git commit -m "docs(eval): document reproducible hard-negative gates"
```

## Task 4: Repeat Replay And Live Bounded Evidence

**Closes:** H6 and final compatibility parts of H5.

**Files:**
- Modify: `docs/TODO.md` only through `$check-chain result`
- Evidence: private `/tmp` directories outside git

- [ ] **Step 1: Run focused and compatibility verification**

```bash
uv run pytest -q tests/eval tests/engine/test_fusion.py
uv run pytest -q tests/test_retrieval.py tests/test_server_search.py
uv run python -m compileall -q src eval
```

Expected: all commands pass.

- [ ] **Step 2: Run full-suite comparison**

```bash
uv run pytest -q
```

Expected: no new failures. The only accepted baseline failure is
`tests/test_resources.py::test_repository_server_report_lists_current_search_modes_and_tool_surface`
for the absent `docs/reports/iwiki-mcp-server-report.html`; every other failure blocks
the live experiment.

- [ ] **Step 3: Replay both retained live baselines**

Run replay against the retained 2026-07-28 evidence and the latest 2026-07-29 evidence.
For each input, run replay twice into separate `mktemp -d` directories, normalize only
`timestamp`, `output_path`, and `out_dir` through the existing `jq -S walk(...)` command,
and require `cmp` exit `0`. Both inputs must report the two reviewed contracts as active.

- [ ] **Step 4: Run one fresh read-only live experiment**

```bash
fusion_output_dir="$(mktemp -d /tmp/iwiki-hard-negative-live.XXXXXX)"
uv run python -m eval.search_pipeline --domain iwiki-mcp --out "$fusion_output_dir" --env-file tmp/creds.env --pareto
```

Expected: 36 baseline traces, 12 Stage A candidates, exactly two reviewed hard-negative
records, and either a bounded candidate decision or an exact diagnostic reason. If fewer
than two contracts are active, retain `needs_work` and do not weaken the minimum-two
rule.

- [ ] **Step 5: Verify safety and production immutability**

Compare git status and production-base state before/after. Scan JSON/Markdown/HTML for
forbidden keys and markers:

```bash
if grep -RE '"(query|provider_url|authorization|api_key|env_file|base_path)"|Bearer |https?://|tmp/creds\.env|/home/' "$fusion_output_dir"; then exit 1; fi
```

Expected: no report matches, no wiki-base writes, and no changes to `retrieval.py` or
`server.py`.

- [ ] **Step 6: Reconcile result without applying production changes**

Run `$check-chain result docs/superpowers/plans/2026-07-29-search-pipeline-benchmark.md`.
Close the TODO row when implementation and evidence are complete. Record separately
whether the benchmark decision is `validated_candidate`, candidate-quality
`needs_work`, or hard-negative evidence `needs_work`. Any production application remains
a new user-approved task.

## Stop Rules

- Stop on malformed or duplicate contracts, fewer than two active targets, secret/path
  leakage, wiki-base mutation, public API drift, or any new full-suite failure.
- Do not replace unavailable targets from a single live run; contract changes require
  evidence from at least two independent baselines and a reviewed plan update.
- Do not weaken existing quality gates to select a candidate.
- Do not edit production retrieval or server behavior in this plan.

## Expected Result

The evaluator can distinguish an unavailable hard-negative corpus from a rejected
fusion candidate. Two reviewed targets are validated from the same baseline used for
selection, candidate recovery no longer depends on historical absolute ranks, reports
show reproducible evidence, and the bounded experiment is repeated without changing
production search.
