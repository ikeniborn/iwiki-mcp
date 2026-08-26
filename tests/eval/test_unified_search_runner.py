from __future__ import annotations

from eval.unified_search.fixtures import FIXED_CASES


def _agent_run(case, *, arm="baseline", status="ok", graph_state=None, missing=(), trace=(),
               messages=None, tool_names=()):
    from eval.unified_search.agent import AgentRun
    graph_state = ("revision_changed" if case.id == "revision-mismatch" else case.expected_graph_state) if graph_state is None else graph_state
    facts = [fact for fact in case.expected_fact_ids if fact not in missing]
    if not tool_names:
        tool_names = ("wiki_search", "wiki_code_search", "wiki_code_context") if arm == "baseline" else ("wiki_unified_search",)
    if not trace:
        trace = tuple({"name": name, "call_id": f"{arm}-{name}"} for name in tool_names)
    return AgentRun(arm, case.id, "test", "env", "prompt", "schema", trace,
        {"fact_ids": facts, "graph_state": graph_state},
        case.expected_fact_ids, case.expected_graph_state, tuple(missing), (),
        graph_state == case.expected_graph_state, status == "ok" and graph_state == case.expected_graph_state and not missing,
        status, 2, [] if messages is None else messages, tool_names)


def _complete_protocol_evidence():
    from eval.unified_search.runner import BOOTSTRAP_SAMPLES, BOOTSTRAP_SEED, MAX_ATTEMPTS, NON_INFERIORITY_MARGIN, REQUIRED_PAIRS, _aggregate, _expected_graph_state, _expected_metadata, _failure_counts, _first_three_unique_seeds, _quality, _tool_calls, raw_parity

    ids = [case.id for case in FIXED_CASES]
    attempts = []
    rows = []
    cases_by_id = {case.id: case for case in FIXED_CASES}
    for case_id in ids:
        for attempt in range(1, REQUIRED_PAIRS + 1):
            order = ["baseline", "candidate"] if attempt % 2 else ["candidate", "baseline"]
            case = cases_by_id[case_id]
            pair = []
            for arm in order:
                facts = list(case.expected_fact_ids) if arm == "candidate" else ["wrong"]
                expected = case.expected_fact_ids
                actual = tuple(facts)
                missing = [item for index, item in enumerate(expected) if index >= len(actual) or actual[index] != item]
                extra = [item for index, item in enumerate(actual) if index >= len(expected) or expected[index] != item]
                graph = _expected_graph_state(case)
                success = not missing and not extra
                names = ["wiki_unified_search"] if arm == "candidate" else ["wiki_search", "wiki_code_search", "wiki_code_context"]
                pair.append({"case_id": case_id, "attempt": attempt, "run": attempt, "arm": arm, "status": "ok", "success": success,
                             "correctness": success, "client_visible_calls": 1 if arm == "candidate" else 3,
                             "result": {"fact_ids": facts, "graph_state": graph}, "missing_fact_ids": missing, "extra_fact_ids": extra,
                             "graph_state_correct": True, "observed_context_seeds": _first_three_unique_seeds(case.code.as_dict()), "seed_mistakes": 0, "omitted_required_context_calls": 0,
                             "stale_claim_errors": 0, "missing_claim_errors": 0, "revision_changed_claim_errors": 0,
                             "stale_missing_revision_claim_errors": 0, "required_fact_loss": len(missing),
                             "tool_trace": [{"name": name, "call_id": f"{arm}-{name}"} for name in names],
                             "prompt_hash": "prompt", "environment_hash": "environment", "shared_messages": [{"role": "user", "content": "shared"}],
                             "tool_schema_hash": f"schema-{arm}", "declared_tool_names": names,
                             "included": True, "exclusion_reason": None})
            attempts.append({"case_id": case_id, "attempt": attempt, "arm_order": order, "included": True,
                             "exclusion_reason": None, "rows": pair})
            rows.extend(pair)
    evidence = {"model": "test", "transport_configured": True, "tool_calling_available": True,
            "preflight": {"available": True, "status": "supported"},
            "public_registry_contains_tool": False,
            "protocol": {"expected_case_ids": ids, "required_pairs": REQUIRED_PAIRS, "max_attempts": MAX_ATTEMPTS,
                         "non_inferiority_margin": NON_INFERIORITY_MARGIN, "bootstrap_samples": BOOTSTRAP_SAMPLES,
                         "bootstrap_seed": BOOTSTRAP_SEED},
            "raw_parity": sorted((raw_parity(case) for case in FIXED_CASES), key=lambda row: row["case_id"]),
            "sampling": {"required_pairs": REQUIRED_PAIRS, "max_attempts": MAX_ATTEMPTS, "complete": True,
                         "attempt_cap_exhausted": False, "per_case_included_pairs": {case_id: REQUIRED_PAIRS for case_id in ids}},
            "attempts": attempts, "run_results": rows,
            "attempt_counts": {"total_attempts": len(attempts), "included_pairs": len(attempts), "excluded_pairs": 0,
                               "per_case": {case_id: {"total_attempts": REQUIRED_PAIRS, "included_pairs": REQUIRED_PAIRS, "excluded_pairs": 0, "exclusion_reasons": {}} for case_id in ids},
                               "exclusion_reasons": {}}}
    evidence["aggregates"] = _aggregate(rows, FIXED_CASES, REQUIRED_PAIRS)
    evidence["quality"] = _quality(rows, FIXED_CASES)
    evidence["workflow_failure_counts"] = _failure_counts(rows)
    evidence["tool_calls"] = _tool_calls(attempts)
    for row in rows:
        row.update(_expected_metadata(cases_by_id[row["case_id"]], row["arm"], "test"))
    evidence["shared_prompt_hashes"] = sorted({row["prompt_hash"] for row in rows})
    evidence["shared_environment_hashes"] = sorted({row["environment_hash"] for row in rows})
    return evidence


def test_raw_parity_covers_backend_matrix_and_degradation_cases():
    from eval.unified_search.runner import raw_parity

    rows = [raw_parity(case) for case in FIXED_CASES]

    assert {row["backend"] for row in rows} >= {"sqlite", "postgresql", "hosted"}
    assert all(row["passed"] for row in rows)
    assert {row["case_id"] for row in rows} >= {
        "code-graph-missing", "code-graph-dirty", "revision-mismatch",
        "wiki-links-stale", "context-truncated",
    }


def test_decision_gates_use_quality_bounds_not_legacy_counts_or_calls():
    from eval.unified_search.runner import decide

    good = _complete_protocol_evidence()
    assert decide(good)["decision"] == "implement"
    for key, value in (("raw_parity", [{"passed": False}]), ("public_registry_contains_tool", True)):
        evidence = dict(good)
        evidence[key] = value
        assert decide(evidence)["decision"] == "do_not_implement"
    for key, value in (("transport_configured", False), ("model", "")):
        evidence = dict(good)
        evidence[key] = value
        assert decide(evidence)["decision"] == "do_not_implement"
    assert decide({**good, "preflight": {"available": False, "status": "failed_response"}})["decision"] == "do_not_implement"


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
    row = _run_metrics(_agent_run(ready, trace=({"name": "wiki_search", "call_id": "x"},)), ready, observed_seeds=["wrong"])
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
    assert row["client_visible_calls"] == 1


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


def test_transport_failure_excludes_pair_and_exhaustion_is_not_blocked(monkeypatch):
    from eval.unified_search import runner
    case = FIXED_CASES[0]
    monkeypatch.setattr(runner, "run_agent_case", lambda _case, arm, *_: _agent_run(case, arm=arm, status="failed_transport"))
    evidence = runner.run_workflow([case], runs=1, model="test", post_factory=lambda *_: object())
    assert not evidence["run_results"]
    assert evidence["sampling"]["attempt_cap_exhausted"]
    assert evidence["workflow_failure_counts"]["baseline"]["failed_transport"] == 30


def test_registry_invariant_is_explicit_and_rejects_after_preflight():
    from eval.unified_search.runner import build_evidence
    evidence = build_evidence([], runs=3, model="test", transport_configured=True,
                              public_registry_contains_tool=True)
    assert evidence["public_registry_contains_tool"] is True
    assert evidence["decision"] == "blocked"


def test_workflow_captures_only_baseline_context_seeds():
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


def test_aggregate_retains_secondary_call_metrics_without_decision_gate():
    from eval.unified_search.runner import _aggregate
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


def test_paired_lower_bound_is_deterministic_and_uses_a_one_sided_percentile():
    from eval.unified_search.runner import paired_lower_bound

    assert paired_lower_bound([1, 1, 1], samples=100, seed=7) == 1.0
    assert paired_lower_bound([0, 1, -1], samples=100, seed=7) == paired_lower_bound(
        [0, 1, -1], samples=100, seed=7
    )


def test_workflow_retries_only_transport_pairs_and_keeps_every_attempt(monkeypatch):
    from eval.unified_search import runner

    case = FIXED_CASES[0]
    calls = []

    def fake_run(case, arm, model, post):
        calls.append(arm)
        status = "failed_transport" if len(calls) <= 2 else "ok"
        return _agent_run(case, arm=arm, status=status)

    monkeypatch.setattr(runner, "run_agent_case", fake_run)
    evidence = runner.run_workflow([case], runs=1, model="test", post_factory=lambda *_: object())

    assert calls == ["baseline", "candidate", "candidate", "baseline"]
    assert len(evidence["attempts"]) == 2
    assert evidence["attempts"][0]["included"] is False
    assert evidence["attempts"][0]["exclusion_reason"] == "failed_transport"
    assert len(evidence["run_results"]) == 2
    assert evidence["sampling"] == {
        "required_pairs": 1, "max_attempts": 30, "complete": True,
        "attempt_cap_exhausted": False, "per_case_included_pairs": {case.id: 1},
    }
    assert evidence["attempt_counts"] == {"total_attempts": 2, "included_pairs": 1, "excluded_pairs": 1,
                                          "per_case": {case.id: {"total_attempts": 2, "included_pairs": 1, "excluded_pairs": 1, "exclusion_reasons": {"failed_transport": 1}}},
                                          "exclusion_reasons": {"failed_transport": 1}}


def test_decide_uses_pairwise_bounds_and_preflight_only_blocks():
    from eval.unified_search.runner import decide

    complete = _complete_protocol_evidence()
    assert decide(complete)["decision"] == "implement"
    assert decide({**complete, "quality": {"aggregate_lower_bound": 0.0, "scenario_lower_bounds": {"a": 0.0}}})["decision"] == "do_not_implement"
    assert decide({**complete, "transport_configured": False})["decision"] == "do_not_implement"


def test_decide_requires_unchanged_fixed_registration_protocol():
    from eval.unified_search.fixtures import FIXED_CASES
    from eval.unified_search.runner import decide

    ids = [case.id for case in FIXED_CASES]
    evidence = _complete_protocol_evidence()
    assert decide(evidence)["decision"] == "implement"
    assert decide({**evidence, "sampling": {**evidence["sampling"], "per_case_included_pairs": {ids[0]: 20}}})["decision"] == "do_not_implement"
    assert decide({**evidence, "quality": {**evidence["quality"], "non_inferiority_margin": 0.14}})["decision"] == "do_not_implement"


def test_decide_rejects_forged_protocol_without_retained_attempts():
    from eval.unified_search.runner import decide

    evidence = _complete_protocol_evidence()
    evidence.update({"preflight": {"available": True, "status": "supported"}, "attempts": [],
                     "attempt_counts": {"total_attempts": 0, "included_pairs": 0, "excluded_pairs": 0,
                                        "per_case": {}, "exclusion_reasons": {}}, "run_results": []})
    assert decide(evidence)["decision"] == "do_not_implement"


def test_decide_rejects_attempt_number_gaps_and_contradictory_rows():
    from eval.unified_search.runner import _retained_integrity

    evidence = _complete_protocol_evidence()
    evidence["attempts"][19]["attempt"] = 22
    for row in evidence["attempts"][19]["rows"]:
        row["attempt"] = 22
    assert not _retained_integrity(evidence, evidence["protocol"]["expected_case_ids"])


def test_retained_integrity_recomputes_success_from_raw_result():
    from eval.unified_search.runner import _retained_integrity

    evidence = _complete_protocol_evidence()
    candidate = next(row for row in evidence["attempts"][0]["rows"] if row["arm"] == "candidate")
    candidate["result"] = {}
    assert not _retained_integrity(evidence, evidence["protocol"]["expected_case_ids"])


def test_retained_integrity_requires_result_for_ok_status():
    from eval.unified_search.runner import _retained_integrity

    evidence = _complete_protocol_evidence()
    candidate = next(row for row in evidence["attempts"][0]["rows"] if row["arm"] == "candidate")
    candidate.update({"result": None, "success": False, "correctness": False})
    assert not _retained_integrity(evidence, evidence["protocol"]["expected_case_ids"])


def test_scheduler_collects_exactly_twenty_pairs_for_every_fixed_case(monkeypatch):
    from eval.unified_search import runner

    monkeypatch.setattr(runner, "run_agent_case", lambda case, arm, *_: _agent_run(case, arm=arm))
    monkeypatch.setattr(runner, "_quality", lambda *_: {"aggregate_lower_bound": 1.0, "scenario_lower_bounds": {},
                                                           "non_inferiority_margin": 0.15, "bootstrap_samples": 50_000,
                                                           "bootstrap_seed": 20260826})
    evidence = runner.run_workflow(FIXED_CASES, runs=20, model="test", post_factory=lambda *_: object())

    assert evidence["attempt_counts"]["total_attempts"] == len(FIXED_CASES) * 20
    assert evidence["attempt_counts"]["included_pairs"] == len(FIXED_CASES) * 20
    assert all(count == 20 for count in evidence["sampling"]["per_case_included_pairs"].values())
    assert all(counts["exclusion_reasons"] == {} for counts in evidence["attempt_counts"]["per_case"].values())


def test_build_evidence_reaches_implement_from_full_scheduler(monkeypatch):
    from eval.unified_search import runner

    monkeypatch.setattr(runner, "run_agent_case", lambda case, arm, *_: _agent_run(case, arm=arm))
    monkeypatch.setattr(runner, "_expected_metadata", lambda _case, arm, _model: {
        "environment_hash": "env", "prompt_hash": "prompt", "tool_schema_hash": "schema", "shared_messages": [],
        "declared_tool_names": ["wiki_search", "wiki_code_search", "wiki_code_context"] if arm == "baseline" else ["wiki_unified_search"],
    })
    monkeypatch.setattr(runner, "paired_lower_bound", lambda *_args, **_kwargs: 1.0)
    evidence = runner.build_evidence(FIXED_CASES, runs=20, model="test", transport_configured=True,
                                     tool_calling_available=True, preflight={"available": True, "status": "supported"},
                                     post_factory=lambda *_: object(), public_registry_contains_tool=False)

    assert evidence["decision"] == "implement"


def test_runner_exception_creates_complete_included_failure_rows(monkeypatch):
    from eval.unified_search import runner

    case = FIXED_CASES[0]
    monkeypatch.setattr(runner, "run_agent_case", lambda *_: (_ for _ in ()).throw(RuntimeError("runner")))
    evidence = runner.run_workflow([case], runs=1, model="test", post_factory=lambda *_: object())
    assert evidence["attempts"][0]["included"] is True
    assert all(row["status"] == "failed_runner" and not row["success"] for row in evidence["attempts"][0]["rows"])
    assert evidence["workflow_failure_counts"]["baseline"]["failed_runner"] == 1
    assert evidence["workflow_failure_counts"]["candidate"]["failed_runner"] == 1


def test_failed_response_with_no_tool_calls_is_a_valid_included_row():
    from eval.unified_search.runner import _failed_runner_metrics, _row_integrity

    row = _failed_runner_metrics(FIXED_CASES[0], "candidate", "test")
    row["status"] = "failed_response"
    assert _row_integrity(row, FIXED_CASES[0])
