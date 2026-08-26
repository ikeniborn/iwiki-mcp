from __future__ import annotations

from eval.unified_search.fixtures import FIXED_CASES


def _agent_run(case, *, arm="baseline", status="ok", graph_state=None, missing=(), trace=(),
               messages=None, tool_names=()):
    from eval.unified_search.agent import AgentRun
    graph_state = case.expected_graph_state if graph_state is None else graph_state
    return AgentRun(arm, case.id, "test", "env", "prompt", "schema", trace,
        {"fact_ids": list(case.expected_fact_ids), "graph_state": graph_state},
        case.expected_fact_ids, case.expected_graph_state, tuple(missing), (),
        graph_state == case.expected_graph_state, graph_state == case.expected_graph_state and not missing,
        status, 2, [] if messages is None else messages, tool_names)


def test_raw_parity_covers_backend_matrix_and_degradation_cases():
    from eval.unified_search.runner import raw_parity

    rows = [raw_parity(case) for case in FIXED_CASES]

    assert {row["backend"] for row in rows} >= {"sqlite", "postgresql", "hosted"}
    assert all(row["passed"] for row in rows)
    assert {row["case_id"] for row in rows} >= {
        "code-graph-missing", "code-graph-dirty", "revision-mismatch",
        "wiki-links-stale", "context-truncated",
    }


def test_decision_gates_are_strict_and_independent():
    from eval.unified_search.runner import decide

    good = {
        "raw_parity": [{"passed": True}], "runs": 3, "required_runs": 3,
        "model": "test", "transport_configured": True, "malformed": False,
        "runner_blockers": [], "aggregates": {
            "candidate_correct_runs": 4, "baseline_correct_runs": 3,
            "candidate_total_runs": 4, "baseline_total_runs": 4,
            "candidate_correctness_ratio": 1.0, "baseline_correctness_ratio": 0.75,
            "coordinated_candidate_total_runs": 3, "coordinated_baseline_total_runs": 3,
            "coordinated_candidate_mean_calls": 1.0, "coordinated_baseline_mean_calls": 3.0,
            "per_case": {"a": {"candidate_successes": 3, "baseline_successes": 3,
                                "candidate_mean_calls": 1.0, "baseline_mean_calls": 3.0,
                                "coordinated": True}},
        },
    }
    assert decide(good)["decision"] == "implement"
    for key, value in (("raw_parity", [{"passed": False}]),):
        evidence = dict(good)
        evidence[key] = value
        assert decide(evidence)["decision"] == "do_not_implement"
    not_better = dict(good)
    not_better["aggregates"] = {"candidate_correct_runs": 3, "baseline_correct_runs": 3,
        "candidate_total_runs": 4, "baseline_total_runs": 4,
        "candidate_correctness_ratio": 0.75, "baseline_correctness_ratio": 0.75,
        "coordinated_candidate_total_runs": 3, "coordinated_baseline_total_runs": 3,
        "coordinated_candidate_mean_calls": 1.0, "coordinated_baseline_mean_calls": 3.0,
        "per_case": good["aggregates"]["per_case"]}
    assert decide(not_better)["decision"] == "do_not_implement"
    evidence = dict(good)
    evidence["aggregates"] = {**good["aggregates"], "per_case": {"a": {
        "candidate_successes": 2, "baseline_successes": 3,
        "candidate_mean_calls": 1.0, "baseline_mean_calls": 3.0, "coordinated": True}}}
    assert decide(evidence)["decision"] == "do_not_implement"
    for key, value in (("runs", 2), ("transport_configured", False), ("malformed", True),
                       ("public_registry_contains_tool", True), ("expected_run_results", 1)):
        evidence = dict(good)
        evidence[key] = value
        if key == "expected_run_results":
            evidence["run_results"] = []
        assert decide(evidence)["decision"] == "blocked"


def test_raw_parity_exposes_independent_context_booleans(monkeypatch):
    from eval.unified_search import runner
    case = FIXED_CASES[0]
    row = runner.raw_parity(case)
    assert row["context_without_wiki_pages_equal"]
    assert row["context_excludes_wiki_pages"]
    monkeypatch.setattr(runner, "compose_unified_search", lambda **_: {**runner.ideal_specialized_result(case), "context": {"wiki_pages": ["bad"]}})
    broken = runner.raw_parity(case)
    assert not broken["context_excludes_wiki_pages"]
    assert not broken["passed"]


def test_run_metrics_capture_seed_and_state_claim_categories():
    from eval.unified_search.runner import _run_metrics
    ready = FIXED_CASES[0]
    row = _run_metrics(_agent_run(ready), ready, observed_seeds=["wrong"])
    assert row["seed_mistakes"] == 1
    assert row["omitted_required_context_calls"] == 1
    assert row["required_fact_loss"] == 0
    stale = next(case for case in FIXED_CASES if case.id == "code-graph-stale")
    missing = next(case for case in FIXED_CASES if case.id == "code-graph-missing")
    revision = next(case for case in FIXED_CASES if case.id == "revision-mismatch")
    assert _run_metrics(_agent_run(stale, graph_state="ready"), stale)["stale_claim_errors"] == 1
    assert _run_metrics(_agent_run(missing, graph_state="ready"), missing)["missing_claim_errors"] == 1
    assert _run_metrics(_agent_run(revision, graph_state="wrong"), revision)["revision_changed_claim_errors"] == 1


def test_run_metrics_retains_safe_agent_prompt_schema_and_tool_metadata():
    from eval.unified_search.runner import _run_metrics
    case = FIXED_CASES[0]
    run = _agent_run(case, trace=({"name": "wiki_search", "call_id": "x"},),
                     messages=[{"role": "user", "content": "prompt"}], tool_names=("wiki_search",))
    row = _run_metrics(run, case)
    assert row["shared_messages"] == run.shared_messages
    assert row["tool_schema_hash"] == "schema"
    assert row["declared_tool_names"] == ["wiki_search"]
    assert "arguments" not in str(row["tool_trace"])


def test_revision_mismatch_requires_revision_changed_claim():
    from eval.unified_search.runner import _run_metrics
    revision = next(case for case in FIXED_CASES if case.id == "revision-mismatch")
    ready = _run_metrics(_agent_run(revision, graph_state="ready"), revision)
    changed = _run_metrics(_agent_run(revision, graph_state="revision_changed"), revision)
    assert not ready["graph_state_correct"]
    assert not ready["success"]
    assert changed["graph_state_correct"]
    assert changed["success"]


def test_workflow_revision_ready_claim_cannot_pass(monkeypatch):
    from eval.unified_search import runner
    revision = next(case for case in FIXED_CASES if case.id == "revision-mismatch")
    monkeypatch.setattr(runner, "run_agent_case", lambda case, arm, model, post: _agent_run(case, arm=arm, graph_state="ready"))
    evidence = runner.run_workflow([revision], runs=3, model="test", post_factory=lambda *_: object())
    assert all(not row["success"] for row in evidence["run_results"])
    assert all(row["revision_changed_claim_errors"] == 1 for row in evidence["run_results"])


def test_non_ok_agent_run_blocks_evidence(monkeypatch):
    from eval.unified_search import runner
    case = FIXED_CASES[0]
    monkeypatch.setattr(runner, "run_agent_case", lambda *args: _agent_run(case, status="failed_transport"))
    evidence = runner.run_workflow([case], runs=3, model="test", post_factory=lambda *_: object())
    assert "agent_run_invalid" in evidence["runner_blockers"]


def test_registry_invariant_is_explicit_and_blocks():
    from eval.unified_search.runner import build_evidence
    evidence = build_evidence([], runs=3, model="test", transport_configured=True,
                              public_registry_contains_tool=True)
    assert evidence["public_registry_contains_tool"] is True
    assert evidence["decision"] == "blocked"


def test_workflow_captures_only_baseline_context_seeds():
    from eval.unified_search.agent import run_agent_case
    from eval.unified_search import runner
    import json

    case = FIXED_CASES[0]
    def response(content=None, tool_calls=None):
        return {"id": "id", "object": "chat.completion", "created": 1, "model": "test",
                "choices": [{"index": 0, "finish_reason": "tool_calls" if tool_calls else "stop",
                "message": {"role": "assistant", "content": content, "tool_calls": tool_calls}}]}
    def call(name, arguments):
        return {"id": name, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
    class Post:
        def __init__(self, values): self.values = values
        def __call__(self, path, *, json): return self.values.pop(0)
    def factory(_case, arm, _run):
        final = response('{"fact_ids":["wiki-policy","code-policy","association-policy"],"graph_state":"ready"}')
        if arm == "baseline":
            return Post([response(tool_calls=[call("wiki_search", {"query": "private"})]),
                         response(tool_calls=[call("wiki_code_search", {"query": "private"})]),
                         response(tool_calls=[call("wiki_code_context", {"seeds": ["wrong"]})]), final])
        return Post([response(tool_calls=[call("wiki_unified_search", {"query": "private"})]), final])
    evidence = runner.run_workflow([case], runs=3, model="test", post_factory=factory)
    baseline_rows = [row for row in evidence["run_results"] if row["arm"] == "baseline"]
    candidate_rows = [row for row in evidence["run_results"] if row["arm"] == "candidate"]
    assert all(row["seed_mistakes"] == 1 for row in baseline_rows)
    assert all(row["seed_mistakes"] == 0 for row in candidate_rows)
    assert "private" not in str(baseline_rows)


def test_workflow_captures_candidate_context_seeds_from_prior_tool_result():
    from eval.unified_search import runner
    import json
    case = FIXED_CASES[0]
    def response(content=None, tool_calls=None):
        return {"id": "id", "object": "chat.completion", "created": 1, "model": "test",
                "choices": [{"index": 0, "finish_reason": "tool_calls" if tool_calls else "stop",
                "message": {"role": "assistant", "content": content, "tool_calls": tool_calls}}]}
    class Post:
        def __init__(self): self.count = 0
        def __call__(self, path, *, json):
            self.count += 1
            if self.count == 1:
                return response(tool_calls=[{"id": "u", "type": "function", "function": {"name": "wiki_unified_search", "arguments": "{\"query\": \"private\"}"}}])
            return response('{"fact_ids":["wiki-policy","code-policy","association-policy"],"graph_state":"ready"}')
    evidence = runner.run_workflow([case], runs=3, model="test", post_factory=lambda *_: Post())
    rows = [row for row in evidence["run_results"] if row["arm"] == "candidate"]
    assert all(row["seed_mistakes"] == 0 for row in rows)
    assert "private" not in str(rows)


def test_candidate_seed_spy_catches_wrong_composer_input(monkeypatch):
    from eval.unified_search import runner
    case = FIXED_CASES[0]
    def wrong_composer(*, wiki_call, code_call, context_call):
        wiki_call()
        code_call()
        return context_call(["wrong"])
    monkeypatch.setattr(runner, "compose_unified_search", wrong_composer)
    assert runner._candidate_seeds(case) == ["wrong"]
    assert runner._run_metrics(_agent_run(case, arm="candidate"), case, ["wrong"])["seed_mistakes"] == 1


def test_aggregate_publishes_ratios_and_combines_coordinated_call_means():
    from eval.unified_search.runner import _aggregate, decide
    coordinated = FIXED_CASES[0]
    other = next(case for case in FIXED_CASES if case.id == "sqlite-postgres-hosted-labels")
    rows = [
        {"case_id": coordinated.id, "arm": "candidate", "correctness": True, "success": True, "client_visible_calls": 4},
        {"case_id": coordinated.id, "arm": "baseline", "correctness": True, "success": True, "client_visible_calls": 3},
        {"case_id": other.id, "arm": "candidate", "correctness": True, "success": True, "client_visible_calls": 1},
        {"case_id": other.id, "arm": "baseline", "correctness": True, "success": True, "client_visible_calls": 7},
    ]
    aggregates = _aggregate(rows, [coordinated, other], expected_runs=1)
    assert aggregates["candidate_correctness_ratio"] == 1.0
    assert aggregates["baseline_correctness_ratio"] == 1.0
    assert aggregates["coordinated_candidate_mean_calls"] == 2.5
    assert aggregates["coordinated_baseline_mean_calls"] == 5.0
    evidence = {"raw_parity": [{"passed": True}], "runs": 3, "required_runs": 3,
                "model": "test", "transport_configured": True, "malformed": False,
                "runner_blockers": [], "aggregates": aggregates}
    assert decide(evidence)["gates"]["coordinated_call_mean"]


def test_aggregate_correctness_ratio_uses_expected_not_observed_rows():
    from eval.unified_search.runner import _aggregate
    case = FIXED_CASES[0]
    rows = [{"case_id": case.id, "arm": "candidate", "correctness": True,
             "success": True, "client_visible_calls": 1}]
    aggregates = _aggregate(rows, [case], expected_runs=2)
    assert aggregates["candidate_total_runs"] == 1
    assert aggregates["candidate_expected_runs"] == 2
    assert aggregates["candidate_correctness_ratio"] == 0.5


def test_workflow_aggregates_every_run_not_best_of(monkeypatch):
    from eval.unified_search import runner
    from eval.unified_search.agent import AgentRun

    case = FIXED_CASES[0]
    calls = []
    candidate_calls = []
    def fake_run(case, arm, model, post):
        calls.append((arm, case.id))
        if arm == "candidate":
            candidate_calls.append(arm)
        success = not (arm == "candidate" and len(candidate_calls) == 2)
        facts = list(case.expected_fact_ids) if success else []
        return AgentRun(arm, case.id, model, "env", "prompt", "schema", (),
            {"fact_ids": facts, "graph_state": case.expected_graph_state},
            case.expected_fact_ids, case.expected_graph_state, () if success else case.expected_fact_ids, (), True, success,
            "ok", 1 if arm == "candidate" else 3, [], ())
    monkeypatch.setattr(runner, "run_agent_case", fake_run)
    evidence = runner.run_workflow([case], runs=3, model="test", post_factory=lambda *_: object())
    assert len(evidence["run_results"]) == 6
    assert evidence["aggregates"]["per_case"][case.id]["candidate_successes"] == 2
    assert evidence["aggregates"]["candidate_correct_runs"] == 2
