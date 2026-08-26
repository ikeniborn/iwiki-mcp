---
review:
  plan_hash: ade355c4e8c6112f
  last_run: 2026-08-26
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings: []
result_check:
  verdict: OK
  source: plan
  plan_hash: ade355c4e8c6112f
  last_run: 2026-08-26
  reviewed: true
  docs_checked: true
chain:
  intent: docs/superpowers/intents/2026-08-25-wiki-unified-search-intent.md
  spec: docs/superpowers/specs/2026-08-25-wiki-unified-search-design.md
---
# Wiki Unified Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decide with bounded paired evidence whether `wiki_unified_search` materially improves agent workflow correctness; register the public tool only after raw parity, aggregate superiority, per-scenario non-inferiority, and human approval.

**Architecture:** Keep the delivered eval-only callback composition unregistered and compare it with the specialized workflow through 20 transport-valid paired runs for every fixed scenario. Transport failures retry the whole pair; all other workflow failures score incorrect. Deterministic paired bootstrap bounds make correctness the registration gate, while tool-call count remains secondary evidence. A failed bounded gate ends in documented `do_not_implement`; a passed and human-approved gate permits the existing conditional production path.

**Tech Stack:** Python 3.10+, FastMCP, httpx OpenAI-compatible chat completions, existing SQLite/PostgreSQL code-graph readers, pytest/pytest-asyncio, standard-library dataclasses/JSON/hashlib, uv.

---

## Boundaries and success conditions

- The evaluation candidate stays under `eval/unified_search/`; it never imports or mutates the module-level FastMCP registry.
- Candidate and baseline consume the same captured specialized responses. No independent retrieval, reranking, score fusion, or global ordering is added.
- Raw parity is exact for Wiki, code, associations, and context. Degradation metadata may only describe the same source state.
- Workflow success requires raw parity, an aggregate one-sided 95% paired-bootstrap lower bound above zero, and every scenario lower bound above `-0.15`.
- Client-visible tool calls are recorded as secondary evidence. Call count and provider wall-clock latency do not authorize or block registration.
- A `failed_transport` arm excludes and retries its whole pair. Every other agent-loop failure scores incorrect. Sampling stops after 20 included pairs or 30 attempts per scenario.
- Missing configuration or a provider that cannot start tool calling produces preflight `blocked`. Every bounded sampling outcome is `implement` or `do_not_implement`.
- Registration requires a separate HUMAN CHECKPOINT after evidence exists. Plan approval does not approve registration.
- The unified request remains read-only. Mutation spies must observe zero Wiki writes, code indexing, publication, schema changes, or alternate-backend calls.
- Existing `wiki_search`, `wiki_code_search`, and `wiki_code_context` schemas and payloads remain unchanged.
- Each repository commit receives one patch version bump and keeps the established
  version quartet identical: `pyproject.toml`, `src/iwiki_mcp/__init__.py`, the
  `test_code_graph_benchmark_package_version` expectation in `tests/test_package.py`,
  and the editable package entry in `uv.lock`. Delivered foundation versions are
  `0.7.185`–`0.7.187`; approved intent/spec revision is `0.7.188`; this revised plan is
  `0.7.189`; active implementation starts at `0.7.190`.

## Requirement coverage

| Spec requirement | Plan task | Verification evidence |
| --- | --- | --- |
| R-001 evidence before registration | 1–4 | eval-only import/registry assertion, bounded paired report, checkpoint |
| R-002 shared primitives | 5 | specialized and unified handler spy tests over identical private primitives |
| R-003 full read-search filter union | 6 | exact generated FastMCP input-schema assertion |
| R-004 separate result blocks | 1, 6 | pure assembly and public handler response tests |
| R-005 automatic bounded context | 1, 6 | zero/one/three/more-than-three seed tests |
| R-006 revision coherence | 1, 7 | mismatch fixture preserves search blocks and clears dependent blocks |
| R-007 independent fail-soft branches | 1, 7 | full degradation-table parameterization |
| R-008 authorization/backend isolation | 7 | hosted grants, forbidden fields, backend-call spies |
| R-009 quality-first workflow decision | 2–4 | paired attempts, bootstrap bounds, bounded decision function |
| R-010 existing contract preservation | 5, 6, 9 | specialized schema/payload snapshots and full suite |
| R-011 read-only behavior | 1, 3, 7, 9 | mutation/storage spies and unchanged snapshot revisions |

## Decision flow

```text
eval-only candidate
  -> deterministic raw parity
  -> 20 transport-valid pairs per scenario (30-attempt cap)
  -> paired correctness bounds; tool calls reported secondarily
  -> bounded decision report
     -> do_not_implement: document retained specialized workflow; no production files
     -> implement: HUMAN CHECKPOINT
        -> approved: production primitives, registration, auth, docs
        -> rejected: document retained specialized workflow; no production files
```

## Task 1: Build the unregistered candidate composer

**Closes:** R-001, R-004, R-005, R-006, R-007, R-011; AC-001, AC-004–AC-007, AC-012.

**Files:**
- Create: `eval/unified_search/__init__.py`
- Create: `eval/unified_search/candidate.py`
- Create: `tests/eval/test_unified_search_candidate.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `tests/test_package.py`
- Modify: `uv.lock`

- [ ] **Step 1: Write failing response-assembly tests**

Create callable spies for Wiki search, code search, and code context. Cover fresh linked
results, duplicate entity IDs, zero code results, every non-fresh state, branch
exceptions, context truncation, `wiki_links_stale`, and revision mismatch.

```python
def test_candidate_uses_three_unique_ranked_seeds_and_separates_wiki_pages():
    calls = []
    result = compose_unified_search(
        wiki_call=lambda: {"results": [{"slug": "design"}]},
        code_call=lambda: {
            "state": "ready", "fresh": True, "revision": "r1",
            "results": [
                {"entity_id": "a"}, {"entity_id": "a"},
                {"entity_id": "b"}, {"entity_id": "c"},
                {"entity_id": "d"},
            ],
        },
        context_call=lambda seeds: calls.append(seeds) or {
            "fresh": True, "revision": "r1", "seeds": seeds,
            "nodes": [{"id": "a"}], "relations": [], "files": [],
            "wiki_pages": [{"slug": "design"}], "warnings": [],
            "limits": {"depth": 1}, "truncated": False,
        },
    )

    assert calls == [["a", "b", "c"]]
    assert result["associations"] == [{"slug": "design"}]
    assert "wiki_pages" not in result["context"]
    assert set(result) == {"wiki", "code", "associations", "context", "degradation"}
```

```python
def test_candidate_discards_context_from_another_revision():
    result = compose_unified_search(
        wiki_call=lambda: {"results": [{"slug": "design"}]},
        code_call=lambda: {
            "state": "ready", "fresh": True, "revision": "r1",
            "results": [{"entity_id": "a"}],
        },
        context_call=lambda seeds: {
            "fresh": True, "revision": "r2", "nodes": [{"id": "a"}],
            "relations": [], "files": [], "wiki_pages": [{"slug": "design"}],
        },
    )

    assert result["wiki"]["results"]
    assert result["code"]["results"]
    assert result["context"]["nodes"] == []
    assert result["associations"] == []
    assert result["degradation"]["context"]["reason"] == "revision_changed"
```

Run:

```bash
uv run pytest -q tests/eval/test_unified_search_candidate.py
```

Expected: collection fails because `eval.unified_search.candidate` does not exist.

- [ ] **Step 2: Implement one pure composer**

`compose_unified_search` accepts three zero/one-argument callables. It executes Wiki and
code independently, selects the first three unique non-empty `entity_id` values only
from a fresh code response, invokes context once, enforces matching revisions, moves
confirmed `wiki_pages` out of context, and derives four independent degradation entries.
It catches only branch exceptions already sanitized by adapters; it must not serialize
exception text.

```python
def compose_unified_search(*, wiki_call, code_call, context_call):
    wiki = _call_branch(wiki_call, branch="wiki")
    code = _call_branch(code_call, branch="code")
    seeds = _fresh_unique_seeds(code, limit=3)
    if not seeds:
        return _without_context(wiki, code)
    context = _call_context(lambda: context_call(seeds))
    return _assemble(wiki, code, context)
```

Keep empty context shape explicit and stable. Preserve source branch dictionaries by
copying before removing `wiki_pages`.

- [ ] **Step 3: Prove read-only and unregistered behavior**

Add tests that pass mutation/index/publication callables which fail if touched and assert
`wiki_unified_search` is absent from `iwiki_mcp.server.mcp` tool listing before and after
candidate use.

Run:

```bash
uv run pytest -q tests/eval/test_unified_search_candidate.py tests/codegraph/test_server_tools.py
```

Expected: all selected tests pass; public registry still excludes the candidate.

- [ ] **Step 4: Bump version and commit**

Set all four version metadata locations to `0.7.185`.

```bash
git add eval/unified_search/__init__.py eval/unified_search/candidate.py tests/eval/test_unified_search_candidate.py pyproject.toml src/iwiki_mcp/__init__.py tests/test_package.py uv.lock
git commit -m "test(search): add unregistered unified candidate"
```

## Task 2: Add fixed scenarios and a shared agent harness

**Closes:** R-001, R-009; establishes AC-009 and AC-010 evidence inputs.

**Files:**
- Create: `eval/unified_search/fixtures.py`
- Create: `eval/unified_search/agent.py`
- Create: `tests/eval/test_unified_search_agent.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `tests/test_package.py`
- Modify: `uv.lock`

- [ ] **Step 1: Define the exact scenario catalog**

Use immutable dataclasses for scenario ID, task prompt, backend label, Wiki/code/context
responses, expected fact IDs, expected graph-state claim, and whether meaning-plus-code
coordination is required. Include every approved scenario: linked; unassociated code;
Wiki-only; code-empty; missing; dirty; busy; stale; `wiki_links_stale`; truncation;
revision mismatch; Wiki embedding failure; Wiki rerank failure; code-reader failure;
invalid filters; out-of-scope domains; SQLite; PostgreSQL; hosted.

Fixture content uses synthetic slugs, paths, entity IDs, and facts. It contains no live
Wiki text, credentials, URLs, DSNs, or local paths.

- [ ] **Step 2: Write failing harness tests**

Mock the OpenAI-compatible `/chat/completions` transport. Prove baseline and candidate
arms receive the same model, system prompt, task prompt, scope label, fixture facts,
maximum rounds, and output rubric. Only tool schemas/callbacks differ.

```python
def test_agent_arms_share_prompt_model_and_rubric(fake_chat):
    baseline = run_agent_case(case=_case(), arm="baseline", model="fixture", post=fake_chat)
    candidate = run_agent_case(case=_case(), arm="candidate", model="fixture", post=fake_chat)

    assert baseline.environment_hash == candidate.environment_hash
    assert baseline.tool_names == ("wiki_search", "wiki_code_search", "wiki_code_context")
    assert candidate.tool_names == ("wiki_unified_search",)
```

Also cover unknown tool name, repeated tool call, max-round exhaustion, malformed JSON,
transport error, and secret sentinel redaction. The harness records exact statuses and
never decides registration; Task 3 excludes transport-failed pairs and scores every
other failed status as incorrect.

Run:

```bash
uv run pytest -q tests/eval/test_unified_search_agent.py
```

Expected: import/behavior failures before the harness exists.

- [ ] **Step 3: Implement the minimal tool-calling loop**

Use the configured OpenAI-compatible chat-completions endpoint and existing `httpx`
dependency. Do not add an SDK. The final assistant response must be strict JSON:

```json
{"fact_ids": ["wiki.design", "code.search"], "graph_state": "ready"}
```

Score only exact fixture IDs and graph-state claims. Record model ID, prompt/tool schema
hashes, bounded call trace, parsed answer, expected IDs, missing IDs, extra IDs, and
status. Never record authorization headers or raw exception details.

The baseline adapter returns fixture responses through three specialized tool callbacks.
The private candidate adapter calls `compose_unified_search` over those same callbacks.
Neither adapter calls FastMCP or persistent storage.

- [ ] **Step 4: Verify deterministic harness behavior under mocked model output**

```bash
uv run pytest -q tests/eval/test_unified_search_agent.py tests/eval/test_unified_search_candidate.py
```

Expected: all selected tests pass.

- [ ] **Step 5: Bump version and commit**

Set all four version metadata locations to `0.7.186`.

```bash
git add eval/unified_search/fixtures.py eval/unified_search/agent.py tests/eval/test_unified_search_agent.py pyproject.toml src/iwiki_mcp/__init__.py tests/test_package.py uv.lock
git commit -m "test(search): add unified workflow harness"
```

## Task 3: Revise comparison to paired quality gates

**Closes:** R-001, R-009, R-011; AC-001, AC-009, AC-010, AC-012.

**Files:**
- Modify: `eval/unified_search/runner.py`
- Modify: `eval/unified_search/report.py`
- Modify: `eval/unified_search/__main__.py`
- Modify: `tests/eval/test_unified_search_runner.py`
- Modify: `tests/eval/test_unified_search_report.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `tests/test_package.py`
- Modify: `uv.lock`

- [ ] **Step 1: Write failing paired-scheduler tests**

Replace the old non-OK-blocks-evidence test with exact pair semantics. Use scripted
`AgentRun` values so no external model is required.

First make the existing `_agent_run` helper mirror production success semantics:

```python
success = status == "ok" and graph_state == case.expected_graph_state and not missing
return AgentRun(
    arm, case.id, "test", "env", "prompt", "schema", trace,
    {"fact_ids": list(case.expected_fact_ids), "graph_state": graph_state},
    case.expected_fact_ids, case.expected_graph_state, tuple(missing), (),
    graph_state == case.expected_graph_state, success, status, 2,
    [] if messages is None else messages, tool_names,
)
```

```python
def test_transport_failure_retries_whole_pair(monkeypatch):
    scripted = iter([
        _agent_run(case, arm="baseline", status="failed_transport"),
        _agent_run(case, arm="candidate", status="ok"),
        _agent_run(case, arm="candidate", status="ok"),
        _agent_run(case, arm="baseline", status="ok"),
    ])
    monkeypatch.setattr(runner, "run_agent_case", lambda *args, **kwargs: next(scripted))
    result = runner.run_workflow([case], runs=1, model="fixture",
                                 post_factory=lambda *_: object())
    assert result["attempts"][0]["included"] is False
    assert result["attempts"][1]["included"] is True
    assert [row["arm"] for row in result["attempts"][1]["rows"]] == ["candidate", "baseline"]


def test_non_transport_failures_are_included_as_incorrect(monkeypatch):
    monkeypatch.setattr(runner, "run_agent_case",
                        lambda case, arm, *args, **kwargs:
                        _agent_run(case, arm=arm, status="failed_max_rounds"))
    result = runner.run_workflow([case], runs=1, model="fixture",
                                 post_factory=lambda *_: object())
    assert result["attempts"][0]["included"] is True
    assert all(row["success"] is False for row in result["run_results"])
```

Also prove: 20 included pairs are collected per scenario; attempts stop at 30; arm order
alternates; one-arm transport failure excludes both rows; an exhausted cap is recorded
without `blocked`; seed spies and bounded traces still work.

Run:

```bash
uv run pytest -q tests/eval/test_unified_search_runner.py
```

Expected: new tests fail against the three-run flat scheduler.

- [ ] **Step 2: Write failing paired-bootstrap and decision tests**

Define deterministic tests around a pure helper and the final decision surface:

```python
def _complete_evidence(*, aggregate_lower_bound=0.01,
                       scenario_lower_bounds=None,
                       candidate_mean_calls=2.0,
                       baseline_mean_calls=3.0):
    bounds = {case.id: 0.0 for case in FIXED_CASES}
    bounds.update(scenario_lower_bounds or {})
    return {
        "model": "test",
        "transport_configured": True,
        "public_registry_contains_tool": False,
        "raw_parity": [{"passed": True}],
        "sampling": {
            "required_pairs": 20,
            "max_attempts": 30,
            "complete": True,
            "attempt_cap_exhausted": False,
        },
        "quality": {
            "aggregate_lower_bound": aggregate_lower_bound,
            "scenario_lower_bounds": bounds,
            "non_inferiority_margin": 0.15,
        },
        "tool_calls": {
            "candidate_mean": candidate_mean_calls,
            "baseline_mean": baseline_mean_calls,
        },
    }


def test_paired_lower_bound_is_deterministic():
    differences = [1] * 12 + [0] * 8
    first = paired_lower_bound(differences, samples=50_000, seed=20260826)
    assert first == paired_lower_bound(differences, samples=50_000, seed=20260826)
    assert first > 0


def test_quality_gate_ignores_call_count():
    evidence = _complete_evidence(aggregate_lower_bound=0.01,
                                  scenario_lower_bounds={case.id: -0.14 for case in FIXED_CASES},
                                  candidate_mean_calls=9.0, baseline_mean_calls=2.0)
    assert decide(evidence)["decision"] == "implement"


def test_any_scenario_below_margin_is_do_not_implement():
    evidence = _complete_evidence(aggregate_lower_bound=0.01,
                                  scenario_lower_bounds={"wiki-only": -0.151})
    assert decide(evidence)["decision"] == "do_not_implement"
```

Add boundary tests for equality at `-0.15` and `0`, raw-parity failure, registry already
containing the tool, attempt-cap exhaustion, missing config preflight, and provider
tool-calling preflight failure. Only the two preflight cases return `blocked`.

- [ ] **Step 3: Implement the minimum paired scheduler and statistics**

Keep constants private and fixed:

```python
REQUIRED_PAIRS = 20
MAX_ATTEMPTS = 30
NON_INFERIORITY_MARGIN = 0.15
BOOTSTRAP_SAMPLES = 50_000
BOOTSTRAP_SEED = 20260826
```

For each sorted case, run both arms for every attempt even when the first arm has a
transport failure. Alternate order between `("baseline", "candidate")` and
`("candidate", "baseline")`. Store every sanitized attempt. Include neither row when
either status is `failed_transport`; otherwise include both rows and score each existing
`success` boolean. Stop after 20 included pairs or 30 attempts.

Implement `paired_lower_bound(differences, *, samples, seed)` with a local
`random.Random` instance, paired resampling with replacement, sorted bootstrap means,
and the 5th-percentile value. Derive stable per-scenario RNG seeds from the fixed seed
plus case ID; use the fixed seed for the aggregate 340-pair vector. Report per-scenario
and aggregate lower bounds, success rates, included/excluded counts, workflow-failure
counts, and secondary included-pair tool-call means.

- [ ] **Step 4: Revise reports and CLI contract**

Keep atomic sanitized JSON/Markdown writes. Add attempt totals, exclusions,
per-scenario bounds, aggregate bound, margin, bootstrap sample count/seed, workflow
failure counts, and secondary calls. Remove old `correct_run_count`,
`per_scenario_success`, `coordinated_call_mean`, and `malformed` decision gates.

CLI remains:

```text
--output-dir PATH
--runs 20
--model MODEL
```

Set the default and only accepted `--runs` value to `20`. An explicit `--model` remains
sufficient; `IWIKI_CHAT_MODEL` is only its optional default. Missing endpoint/key/model
or tool-incompatible preflight exits `2`; bounded `do_not_implement` exits `1`;
`implement` exits `0`. No outcome changes the real registry.

Run:

```bash
uv run python -m eval.unified_search --help
uv run pytest -q tests/eval/test_unified_search_runner.py tests/eval/test_unified_search_report.py
```

Expected: help documents the fixed 20-pair protocol; selected tests pass.

- [ ] **Step 5: Verify no public registration or mutation**

```bash
uv run pytest -q tests/eval/test_unified_search_candidate.py tests/eval/test_unified_search_agent.py tests/eval/test_unified_search_runner.py tests/eval/test_unified_search_report.py tests/codegraph/test_server_tools.py tests/test_package.py
```

Expected: all selected tests pass; tool list omits `wiki_unified_search`.

- [ ] **Step 6: Bump version and commit**

Set all four version metadata locations to `0.7.190`.

```bash
git add eval/unified_search/runner.py eval/unified_search/report.py eval/unified_search/__main__.py tests/eval/test_unified_search_runner.py tests/eval/test_unified_search_report.py pyproject.toml src/iwiki_mcp/__init__.py tests/test_package.py uv.lock
git commit -m "test(search): add paired unified quality gate"
```

## Task 4: Run the comparison and stop at the registration checkpoint

**Closes:** R-001, R-009; AC-009, AC-010.

**Files:**
- Create: `docs/superpowers/evidence/wiki-unified-search-evaluation.json`
- Create: `docs/superpowers/evidence/wiki-unified-search-evaluation.md`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `tests/test_package.py`
- Modify: `uv.lock`

- [ ] **Step 1: Verify the clean deterministic evaluation surface**

```bash
uv run pytest -q tests/eval/test_unified_search_candidate.py tests/eval/test_unified_search_agent.py tests/eval/test_unified_search_runner.py tests/eval/test_unified_search_report.py
```

Expected: all selected tests pass.

- [ ] **Step 2: Run the repeated workflow comparison**

```bash
uv run python -m eval.unified_search --output-dir docs/superpowers/evidence --runs 20 --model ollama-qwen3-5-cloud
```

Expected: both evidence files exist. Exit `0` means raw parity, aggregate superiority,
and all 17 non-inferiority bounds pass. Exit `1` means bounded evidence recommends
`do_not_implement`. Exit `2` is permitted only before sampling for missing configuration
or unsupported tool calling; repair preflight and rerun without changing decision rules.

- [ ] **Step 3: Inspect evidence invariants**

```bash
uv run python -m json.tool docs/superpowers/evidence/wiki-unified-search-evaluation.json >/dev/null
rg -n 'decision|raw_parity|aggregate_lower_bound|scenario_lower_bounds|included_pairs|excluded_pairs|workflow_failure_counts|tool_calls|public_registry_contains_tool' docs/superpowers/evidence/wiki-unified-search-evaluation.md
rg -n 'Authorization|Bearer |api[_-]?key|password|postgres(ql)?://|/home/' docs/superpowers/evidence/wiki-unified-search-evaluation.json docs/superpowers/evidence/wiki-unified-search-evaluation.md
```

Expected: JSON parses; every scenario has 20 included pairs or an explicit attempt-cap
failure; confidence bounds, secondary calls, workflow failures, and registry state
appear; secret/path scan returns no matches.

- [ ] **Step 4: Bump version and commit evidence**

Set all four version metadata locations to `0.7.191`.

```bash
git add docs/superpowers/evidence/wiki-unified-search-evaluation.json docs/superpowers/evidence/wiki-unified-search-evaluation.md pyproject.toml src/iwiki_mcp/__init__.py tests/test_package.py uv.lock
git commit -m "docs(eval): record unified search evidence"
```

- [ ] **Step 5: HUMAN CHECKPOINT — present the recorded outcome**

Stop execution. Present raw parity, aggregate lower bound, all per-scenario lower bounds,
included/excluded attempt counts, workflow failures, secondary call-count change, model,
and report links. Ask the user to accept recorded `implement` or `do_not_implement`
outcome.

- preflight `blocked`: repair only configuration or provider tool-calling availability,
  rerun Task 4, and remain at this checkpoint.
- `do_not_implement`, or user rejection of `implement`: execute Task 4A only.
- `implement` plus explicit user approval: execute Tasks 5–9. Do not create production
  module, registration, auth, or public docs before that approval.

## Task 4A: Close a do-not-implement decision

**Closes:** R-001, R-009, R-010; AC-010, AC-011.

**Files:**
- Modify: `README.md`
- Modify: `docs/README.ru.md`
- Modify: `docs/architecture.md`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `tests/test_package.py`
- Modify: `uv.lock`
- Wiki update: existing bound page describing daily agent search workflow

- [ ] **Step 1: Document the retained specialized workflow**

State that `wiki_unified_search` remains intentionally unregistered, link the evidence,
and show the supported sequence `wiki_search` → `wiki_code_search` →
`wiki_code_context`. Do not add the candidate to tool matrices or public schemas.

- [ ] **Step 2: Add/adjust documentation contract tests if existing tests cover tool lists**

Assert docs name the retained sequence and do not claim public availability.

- [ ] **Step 3: Update bound iwiki workflow documentation**

Read current page immediately before its section mutation, pass `expected_revision` and
`expected_section_hash`, then run `wiki_lint`. Do not call Git-only `wiki_sync` on hosted
PostgreSQL.

- [ ] **Step 4: Bump version, verify, and commit**

Set all four version metadata locations to `0.7.192`.

```bash
uv run pytest -q tests/test_package.py tests/codegraph/test_server_tools.py tests/eval
uv run pytest -q
git add README.md docs/README.ru.md docs/architecture.md pyproject.toml src/iwiki_mcp/__init__.py tests/test_package.py uv.lock
git commit -m "docs(search): retain specialized wiki code workflow"
```

Expected: focused and full suites pass; real registry still excludes the unified tool.
Then continue directly to Task 9 result reconciliation, skipping Tasks 5–8.

## Task 5: Factor shared production read primitives

**Gate:** Execute only after evidence says `implement` and user explicitly approves registration.

**Closes:** R-002, R-010; AC-002, AC-011.

**Files:**
- Modify: `src/iwiki_mcp/server.py`
- Create: `src/iwiki_mcp/unified_search.py`
- Modify: `eval/unified_search/candidate.py`
- Create: `tests/test_unified_search.py`
- Modify: `tests/test_server_search.py`
- Modify: `tests/codegraph/test_server_tools.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `tests/test_package.py`
- Modify: `uv.lock`

- [ ] **Step 1: Capture specialized-handler behavior before refactoring**

Add spy tests for resolved binding count, Wiki config/candidate/rerank calls, SQLite code
runtime calls, PostgreSQL reader calls, language validation, and existing error payloads.
Run them before implementation and retain expected payload snapshots.

```bash
uv run pytest -q tests/test_server_search.py tests/codegraph/test_server_tools.py
```

Expected: baseline tests pass.

- [ ] **Step 2: Write failing shared-primitive delegation tests**

Tests monkeypatch `_wiki_search_read`, `_wiki_code_search_bound`, and
`_wiki_code_context_bound`; specialized handlers must delegate to the matching primitive
without response changes. Unified tests call the same primitives through injected
callables.

- [ ] **Step 3: Extract the minimum private primitives**

In `server.py`, keep structural validation and binding at handler boundaries. Move only
post-binding existing behavior into:

```python
async def _wiki_search_read(*, binding, query, scope, mode, domains, k,
                            threshold, type_filter, tags): ...

async def _wiki_code_search_bound(*, binding, query, kinds, path,
                                  languages, limit): ...

async def _wiki_code_context_bound(*, binding, seeds, direction="both", depth=1,
                                   include_source=False, include_wiki=True): ...
```

Specialized public signatures, validation order, status fields, warnings, results, and
errors stay byte-equivalent for the tested inputs.

- [ ] **Step 4: Create the production pure orchestrator**

Move only proven composition logic from `eval/unified_search/candidate.py` to
`src/iwiki_mcp/unified_search.py`. Make the eval candidate import and invoke that pure
function after the checkpoint; do not retain two implementations.

- [ ] **Step 5: Verify specialized contracts and shared use**

```bash
uv run pytest -q tests/test_server_search.py tests/codegraph/test_server_tools.py tests/test_unified_search.py tests/eval/test_unified_search_candidate.py
```

Expected: all selected tests pass; specialized payloads unchanged.

- [ ] **Step 6: Bump version and commit**

Set all four version metadata locations to `0.7.192`.

```bash
git add src/iwiki_mcp/server.py src/iwiki_mcp/unified_search.py eval/unified_search/candidate.py tests/test_unified_search.py tests/test_server_search.py tests/codegraph/test_server_tools.py tests/eval/test_unified_search_candidate.py pyproject.toml src/iwiki_mcp/__init__.py tests/test_package.py uv.lock
git commit -m "refactor(search): share wiki code read primitives"
```

## Task 6: Add the exact public handler and FastMCP schema

**Closes:** R-003, R-004, R-005, R-010; AC-003–AC-005, AC-011.

**Files:**
- Modify: `src/iwiki_mcp/server.py`
- Modify: `tests/test_unified_search.py`
- Modify: `tests/codegraph/test_server_tools.py`
- Modify: `tests/test_package.py`
- Modify: `tests/test_mcp_smoke.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

- [ ] **Step 1: Write failing exact-schema tests**

Inspect the real FastMCP tool schema. Assert the input properties equal exactly:

```python
{
    "query", "scope", "mode", "domains", "k", "threshold", "type", "tags",
    "kinds", "path", "languages", "limit",
}
```

Assert exclusion of `intent`, `heading`, `seeds`, `direction`, `relations`,
`include_source`, `include_wiki`, and all node/file/byte budgets.

- [ ] **Step 2: Write failing public response and call-bound tests**

Cover structural validation before binding; one binding resolution; independent Wiki
and code calls; no separate status call; fixed context arguments; five exact top-level
blocks; unchanged ranks; no combined score/order; zero context calls for fresh zero-hit
code; one context call for any positive hit count.

- [ ] **Step 3: Implement and register `wiki_unified_search`**

The handler validates shared query and static filters first, resolves binding once, then
constructs async adapters around the three private primitives. Wiki and code execute
sequentially and independently. Snapshot-dependent language errors remain a code-branch
degradation and do not remove Wiki results.

- [ ] **Step 4: Verify real registry and specialized schemas**

```bash
uv run pytest -q tests/test_unified_search.py tests/codegraph/test_server_tools.py tests/test_package.py tests/test_mcp_smoke.py
```

Expected: unified tool appears once with exact schema; specialized schemas remain
unchanged.

- [ ] **Step 5: Bump version and commit**

Set all four version metadata locations to `0.7.193`.

```bash
git add src/iwiki_mcp/server.py tests/test_unified_search.py tests/codegraph/test_server_tools.py tests/test_package.py tests/test_mcp_smoke.py pyproject.toml src/iwiki_mcp/__init__.py uv.lock
git commit -m "feat(search): add unified search tool"
```

## Task 7: Enforce hosted authorization, degradation parity, and backend isolation

**Closes:** R-006, R-007, R-008, R-011; AC-006–AC-008, AC-012.

**Files:**
- Modify: `src/iwiki_mcp/http.py`
- Modify: `tests/test_unified_search.py`
- Modify: `tests/codegraph/test_runtime.py`
- Modify: `tests/postgres/test_code_graph_reader.py`
- Modify: `tests/postgres/test_http.py`
- Modify: `tests/postgres/test_tool_matrix.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `tests/test_package.py`
- Modify: `uv.lock`

- [ ] **Step 1: Write failing combined-authorization tests**

Hosted tests reject missing primary-domain read grant, any unauthorized requested Wiki
domain, caller `iwiki_id`, and singular `domain`. Authorized primary plus all requested
Wiki domains reaches handler once. Auth rejection occurs before any Wiki/code reader.

- [ ] **Step 2: Implement one combined read authorization branch**

Add `wiki_unified_search` to an explicit combined-read set. Reuse existing primary
code-domain check and requested-Wiki-domain grant logic; do not treat it only as a code
tool and return early. Do not broaden absent domains beyond the resolved bound read set.

- [ ] **Step 3: Parameterize every degradation row**

Test Wiki failure, code missing/dirty/busy/stale/failed, fresh zero hits, context failure,
revision mismatch, `wiki_links_stale`, truncation, Wiki rerank failure, invalid language,
and code-reader exception. Assertions cover retained independent blocks, exact reasons,
empty dependent fields, and absence of exception/credential/path leakage.

- [ ] **Step 4: Prove overlapping SQLite/PostgreSQL/hosted behavior**

Use existing runtime and PostgreSQL reader fixtures. Inject a search/context activation
change to force revision mismatch. Assert no SQLite-to-PostgreSQL, PostgreSQL-to-SQLite,
or hosted-to-local fallback call. Snapshot revisions and Wiki page revisions remain
unchanged after requests.

- [ ] **Step 5: Verify authorization and backend matrix**

```bash
uv run pytest -q tests/test_unified_search.py tests/codegraph/test_runtime.py tests/postgres/test_code_graph_reader.py tests/postgres/test_http.py tests/postgres/test_tool_matrix.py
```

Expected: all selected tests pass; unauthorized calls perform zero retrieval; authorized
calls preserve backend-specific states without fallback.

- [ ] **Step 6: Bump version and commit**

Set all four version metadata locations to `0.7.194`.

```bash
git add src/iwiki_mcp/http.py tests/test_unified_search.py tests/codegraph/test_runtime.py tests/postgres/test_code_graph_reader.py tests/postgres/test_http.py tests/postgres/test_tool_matrix.py pyproject.toml src/iwiki_mcp/__init__.py tests/test_package.py uv.lock
git commit -m "feat(http): authorize unified search reads"
```

## Task 8: Document the approved public contract and update iwiki

**Closes:** R-001, R-003–R-011; supports AC-003, AC-008, AC-010–AC-012.

**Files:**
- Modify: `README.md`
- Modify: `docs/README.ru.md`
- Modify: `docs/architecture.md`
- Modify: `src/iwiki_mcp/resources.py` only if its existing agent tool guidance lists search tools
- Modify: `tests/test_package.py`
- Modify: `tests/test_resources.py` only if `resources.py` changes
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`
- Wiki update: existing tool-surface and daily-workflow pages in bound `iwiki-mcp` domain

- [ ] **Step 1: Document request, response, and selection boundary**

List exact fields, five response blocks, three-seed/depth-one/source-free behavior,
revision mismatch, `wiki_links_stale`, truncation, and branch degradation. State no score
fusion, no write intent, no manual traversal, no backend fallback, and specialized tools
remain supported for advanced work.

- [ ] **Step 2: Record evidence-backed registration rationale**

Link the committed report and state model/run count plus all passed gates without claiming
deterministic model behavior beyond the recorded environment.

- [ ] **Step 3: Update bound iwiki pages with CAS**

Read each current page/heading immediately before mutation. Pass current
`expected_revision` and `expected_section_hash`; re-read after a conflict. Run
`wiki_lint`. Hosted PostgreSQL writes are durable; do not call `wiki_sync`.

- [ ] **Step 4: Verify docs and public schema agree**

```bash
uv run pytest -q tests/test_package.py tests/test_unified_search.py
rg -n 'wiki_unified_search|revision_changed|wiki_links_stale' README.md docs/README.ru.md docs/architecture.md
```

Expected: docs tests pass; all three docs contain contract/state guidance.

- [ ] **Step 5: Bump version and commit**

Set all four version metadata locations to `0.7.195`.

```bash
git add README.md docs/README.ru.md docs/architecture.md src/iwiki_mcp/resources.py tests/test_package.py tests/test_resources.py pyproject.toml src/iwiki_mcp/__init__.py uv.lock
git commit -m "docs(search): document unified wiki code search"
```

Stage only actually changed documentation tests and `resources.py`; do not include
unrelated files.

## Task 9: Final verification and chain result reconciliation

**Closes:** all requirements; AC-001–AC-012.

**Files:**
- Modify only files required by a reproduced verification failure
- Update: iwiki task page and task history through `task-ledger`

- [ ] **Step 1: Run branch-specific focused verification**

For `do_not_implement`:

```bash
uv run pytest -q tests/eval tests/codegraph/test_server_tools.py tests/test_package.py
```

Expected: evaluation tests pass; registry excludes `wiki_unified_search`.

For approved `implement`:

```bash
uv run pytest -q tests/eval tests/test_unified_search.py tests/test_server_search.py tests/codegraph/test_server_tools.py tests/codegraph/test_runtime.py tests/postgres/test_code_graph_reader.py tests/postgres/test_http.py tests/postgres/test_tool_matrix.py tests/test_package.py tests/test_mcp_smoke.py
```

Expected: focused suite passes; registry contains one exact unified schema; specialized
contracts pass.

- [ ] **Step 2: Run complete repository verification**

```bash
uv run pytest -q
uv run iwiki-mcp --help
git diff --check
```

Expected: full suite passes, console help exits `0`, and diff check has no output.

- [ ] **Step 3: Re-run evidence integrity checks**

```bash
uv run python -m json.tool docs/superpowers/evidence/wiki-unified-search-evaluation.json >/dev/null
rg -n 'aggregate_lower_bound|scenario_lower_bounds|included_pairs|excluded_pairs|workflow_failure_counts|tool_calls' docs/superpowers/evidence/wiki-unified-search-evaluation.json docs/superpowers/evidence/wiki-unified-search-evaluation.md
rg -n 'Authorization|Bearer |api[_-]?key|password|postgres(ql)?://|/home/' docs/superpowers/evidence/wiki-unified-search-evaluation.json docs/superpowers/evidence/wiki-unified-search-evaluation.md
```

Expected: JSON parses, quality-first statistics and attempt evidence exist, and the
secret/path scan returns no matches.

- [ ] **Step 4: Refresh code graph when production Python symbols changed**

Call `wiki_code_status`. If the active local MCP server has the checkout, call
`wiki_code_index`; if hosted HTTP reports `source_unavailable`, record that optional
context state and do not treat it as a verification blocker.

- [ ] **Step 5: Lint iwiki and reconcile the chain result**

Run `wiki_lint`, record task evidence and lifecycle through `task-ledger`, then execute
`$check-chain result docs/superpowers/plans/2026-08-26-wiki-unified-search.md`. Repair any
`needs_work` finding at its owning stage and rerun. `OK` is required before branch
finishing.

- [ ] **Step 6: Review branch scope and prepare PR handoff**

```bash
git status --short
git log --oneline master..HEAD
git diff --stat master...HEAD
```

Expected: only plan, eval/evidence, selected decision branch, version bumps, tests, and
authorized docs appear. Use `superpowers:requesting-code-review`, then
`superpowers:finishing-a-development-branch`; use `git-workflow` for PR creation.

## Self-review checklist

- Every R-001–R-011 maps to task and executable evidence.
- Every AC-001–AC-012 has a focused assertion before full-suite verification.
- Evaluation remains unregistered through HUMAN CHECKPOINT.
- `do_not_implement` path creates no production module, registry, auth, or public-tool docs.
- `implement` path has exact request fields and five response blocks from approved spec.
- No task changes ranking, storage, schema, frontmatter, publication, specialized public contracts, or backend selection.
- Model-dependent evidence records environment and repeated runs; unit tests remain deterministic.
- No placeholder, unresolved design decision, or implementation-time discretionary API choice remains.
