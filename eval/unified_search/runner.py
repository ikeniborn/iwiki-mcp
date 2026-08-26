"""Deterministic comparison harness for the unregistered unified-search candidate."""

from __future__ import annotations

from collections import defaultdict
import json as json_module
from typing import Any, Callable, Iterable

from .agent import AgentRun, Post, run_agent_case
from .candidate import compose_unified_search
from .fixtures import UnifiedSearchCase


def _first_three_unique_seeds(code: dict[str, Any]) -> list[str]:
    if code.get("fresh") is not True or code.get("state") not in (None, "ready"):
        return []
    seeds: list[str] = []
    for row in code.get("results", []):
        entity_id = row.get("entity_id") if isinstance(row, dict) else None
        if isinstance(entity_id, str) and entity_id and entity_id not in seeds:
            seeds.append(entity_id)
            if len(seeds) == 3:
                break
    return seeds


def ideal_specialized_result(case: UnifiedSearchCase) -> dict[str, Any]:
    """Assemble ideal specialized response directly; never call candidate code."""
    wiki, code = case.wiki.as_dict(), case.code.as_dict()
    wiki_failed = "error" in wiki and not wiki.get("results")
    code_failed = "error" in code and not code.get("results")
    seeds = _first_three_unique_seeds(code)
    empty = lambda reason=None: {"degraded": reason is not None, "reason": reason}
    if not seeds:
        code_reason = "failed" if code_failed else None
        if not code_failed and (code.get("fresh") is not True or code.get("state") not in (None, "ready")):
            code_reason = code.get("state") or "not_fresh"
        pending = "not_run" if code_reason else None
        return {"wiki": wiki, "code": code, "associations": [], "context": {}, "degradation": {
            "wiki": empty("failed" if wiki_failed else None), "code": empty(code_reason),
            "context": empty(pending), "associations": empty(pending)}}
    context = case.context.as_dict()
    if context.get("revision") != code.get("revision"):
        reason = "revision_changed"
    elif context.get("fresh") is not True:
        reason = context.get("code") if isinstance(context.get("code"), str) else "not_fresh"
    else:
        reason = None
    if reason:
        context.pop("wiki_pages", None)
        for key in ("seeds", "nodes", "relations", "files"):
            context[key] = []
        return {"wiki": wiki, "code": code, "associations": [], "context": context, "degradation": {
            "wiki": empty("failed" if wiki_failed else None), "code": empty("failed" if code_failed else None),
            "context": empty(reason), "associations": empty(reason)}}
    associations = list(context.pop("wiki_pages", []))
    stale = bool(context.get("wiki_links_stale")) or "wiki_links_stale" in context.get("warnings", [])
    if stale:
        associations = []
    return {"wiki": wiki, "code": code, "associations": associations, "context": context, "degradation": {
        "wiki": empty("failed" if wiki_failed else None), "code": empty("failed" if code_failed else None),
        "context": empty(), "associations": empty("wiki_links_stale" if stale else None)}}


def raw_parity(case: UnifiedSearchCase) -> dict[str, Any]:
    expected = ideal_specialized_result(case)
    actual = compose_unified_search(wiki_call=case.wiki.as_dict, code_call=case.code.as_dict,
                                    context_call=lambda _seeds: case.context.as_dict())
    booleans = {
        "wiki_equal": actual["wiki"] == expected["wiki"],
        "code_equal": actual["code"] == expected["code"],
        "context_equal": actual["context"] == expected["context"],
        "context_without_wiki_pages_equal": actual["context"] == expected["context"],
        "context_excludes_wiki_pages": "wiki_pages" not in actual["context"],
        "associations_equal": actual["associations"] == expected["associations"],
        "degradation_describes_state": actual["degradation"] == expected["degradation"],
    }
    return {"case_id": case.id, "backend": case.backend_label, **booleans,
            "passed": all(booleans.values())}


def _expected_graph_state(case: UnifiedSearchCase) -> str:
    return "revision_changed" if case.id == "revision-mismatch" else case.expected_graph_state


def _run_metrics(run: AgentRun, case: UnifiedSearchCase,
                 observed_seeds: list[str] | None = None) -> dict[str, Any]:
    names = [entry.get("name") for entry in run.tool_trace]
    requires_context = bool(_first_three_unique_seeds(case.code.as_dict()))
    context_name = "wiki_unified_search" if run.arm == "candidate" else "wiki_code_context"
    omitted_context = int(requires_context and context_name not in names)
    expected_seeds = _first_three_unique_seeds(case.code.as_dict())
    seed_mistakes = int(requires_context and observed_seeds != expected_seeds)
    parsed = run.parsed_answer or {}
    actual_facts = tuple(parsed.get("fact_ids", ())) if isinstance(parsed.get("fact_ids"), list) and all(isinstance(item, str) for item in parsed["fact_ids"]) else ()
    expected_facts = case.expected_fact_ids
    missing = tuple(item for index, item in enumerate(expected_facts) if index >= len(actual_facts) or actual_facts[index] != item)
    extra = tuple(item for index, item in enumerate(actual_facts) if index >= len(expected_facts) or expected_facts[index] != item)
    expected_graph = _expected_graph_state(case)
    graph_correct = bool(parsed and parsed.get("graph_state") == expected_graph)
    success = run.status == "ok" and not missing and not extra and graph_correct
    graph_mismatch = int(not graph_correct)
    stale_claim_errors = int(graph_mismatch and expected_graph == "stale")
    missing_claim_errors = int(graph_mismatch and expected_graph == "missing")
    revision_changed_claim_errors = int(graph_mismatch and case.id == "revision-mismatch")
    return {"arm": run.arm, "case_id": case.id, "status": run.status, "correctness": success,
            "success": success, "missing_fact_ids": list(missing),
            "extra_fact_ids": list(extra), "graph_state_correct": graph_correct,
            "seed_mistakes": seed_mistakes, "omitted_required_context_calls": omitted_context,
            "stale_claim_errors": stale_claim_errors, "missing_claim_errors": missing_claim_errors,
            "revision_changed_claim_errors": revision_changed_claim_errors,
            "stale_missing_revision_claim_errors": stale_claim_errors + missing_claim_errors + revision_changed_claim_errors,
            "client_visible_calls": run.client_visible_call_count,
            "required_fact_loss": len(run.missing_fact_ids), "tool_trace": [dict(item) for item in run.tool_trace],
            "result": dict(run.parsed_answer) if isinstance(run.parsed_answer, dict) else None,
            "prompt_hash": run.prompt_hash, "environment_hash": run.environment_hash,
            "shared_messages": list(run.shared_messages), "tool_schema_hash": run.tool_schema_hash,
            "declared_tool_names": list(run.declared_tool_names)}


def _aggregate(rows: list[dict[str, Any]], cases: Iterable[UnifiedSearchCase],
               expected_runs: int) -> dict[str, Any]:
    case_list = list(cases)
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        grouped[row["case_id"]][row["arm"]].append(row)
    per_case = {}
    for case in sorted(case_list, key=lambda item: item.id):
        arms = grouped[case.id]
        candidate, baseline = arms["candidate"], arms["baseline"]
        mean = lambda values: sum(item["client_visible_calls"] for item in values) / len(values) if values else 0.0
        per_case[case.id] = {"candidate_successes": sum(item["success"] for item in candidate),
                             "baseline_successes": sum(item["success"] for item in baseline),
                             "candidate_mean_calls": mean(candidate), "baseline_mean_calls": mean(baseline),
                             "coordinated": case.coordinated_meaning_code}
    candidate = [row for row in rows if row["arm"] == "candidate"]
    baseline = [row for row in rows if row["arm"] == "baseline"]
    coordinated_ids = {case.id for case in case_list if case.coordinated_meaning_code}
    coordinated_candidate = [row for row in candidate if row["case_id"] in coordinated_ids]
    coordinated_baseline = [row for row in baseline if row["case_id"] in coordinated_ids]
    mean = lambda values: sum(row["client_visible_calls"] for row in values) / len(values) if values else 0.0
    candidate_correct = sum(row["correctness"] for row in candidate)
    baseline_correct = sum(row["correctness"] for row in baseline)
    candidate_total = len(candidate)
    baseline_total = len(baseline)
    expected_total = len(case_list) * max(0, expected_runs)
    return {"candidate_correct_runs": candidate_correct, "baseline_correct_runs": baseline_correct,
            "candidate_total_runs": candidate_total, "baseline_total_runs": baseline_total,
            "candidate_expected_runs": expected_total, "baseline_expected_runs": expected_total,
            "candidate_correctness_ratio": candidate_correct / expected_total if expected_total else 0.0,
            "baseline_correctness_ratio": baseline_correct / expected_total if expected_total else 0.0,
            "coordinated_candidate_total_runs": len(coordinated_candidate),
            "coordinated_baseline_total_runs": len(coordinated_baseline),
            "coordinated_candidate_mean_calls": mean(coordinated_candidate),
            "coordinated_baseline_mean_calls": mean(coordinated_baseline), "per_case": per_case}


def _candidate_seeds(case: UnifiedSearchCase) -> list[str] | None:
    observed: list[str] | None = None
    def context_call(seeds: list[str]) -> dict[str, Any]:
        nonlocal observed
        observed = list(seeds[:3])
        return case.context.as_dict()
    compose_unified_search(wiki_call=case.wiki.as_dict, code_call=case.code.as_dict,
                           context_call=context_call)
    return observed


def _capture_context_seeds(post: Post, observed: list[list[str]]) -> Post:
    def wrapped(path: str, *, json: dict[str, Any]) -> dict[str, Any]:
        response = post(path, json=json)
        try:
            tool_calls = response["choices"][0]["message"].get("tool_calls") or []
            for call in tool_calls:
                function = call.get("function", {})
                if function.get("name") == "wiki_code_context":
                    args = json_module.loads(function.get("arguments", ""))
                    seeds = args.get("seeds") if isinstance(args, dict) else None
                    if isinstance(seeds, list) and all(isinstance(seed, str) for seed in seeds):
                        observed.append(list(seeds[:3]))
        except (KeyError, IndexError, TypeError, ValueError, json_module.JSONDecodeError):
            pass
        return response
    return wrapped


def run_workflow(cases: Iterable[UnifiedSearchCase], *, runs: int, model: str,
                 post_factory: Callable[[UnifiedSearchCase, str, int], Post]) -> dict[str, Any]:
    case_list = sorted(cases, key=lambda item: item.id)
    rows: list[dict[str, Any]] = []
    blockers: list[str] = []
    if runs < 3:
        blockers.append("runs_must_be_at_least_3")
    for run_index in range(max(0, runs)):
        for case in case_list:
            for arm in ("baseline", "candidate"):
                try:
                    observed: list[list[str]] = []
                    post = post_factory(case, arm, run_index)
                    if arm == "baseline":
                        post = _capture_context_seeds(post, observed)
                    run = run_agent_case(case, arm, model, post)
                    seeds = _candidate_seeds(case) if arm == "candidate" else (observed[-1] if observed else None)
                    row = _run_metrics(run, case, seeds)
                    row["run"] = run_index + 1
                    rows.append(row)
                    if run.status != "ok":
                        blockers.append("agent_run_invalid")
                except Exception:
                    blockers.append("runner_failure")
    rows.sort(key=lambda row: (row["case_id"], row["run"], row["arm"]))
    return {"run_results": rows, "aggregates": _aggregate(rows, case_list, runs), "runner_blockers": sorted(set(blockers))}


def decide(evidence: dict[str, Any]) -> dict[str, Any]:
    blockers = list(evidence.get("runner_blockers", []))
    required = int(evidence.get("required_runs", 3))
    expected_rows = evidence.get("expected_run_results")
    incomplete_rows = expected_rows is not None and len(evidence.get("run_results", [])) != expected_rows
    if (int(evidence.get("runs", 0)) < required or incomplete_rows or not evidence.get("model") or
            not evidence.get("transport_configured") or evidence.get("malformed") or
            evidence.get("public_registry_contains_tool") or blockers):
        return {"decision": "blocked", "blocker": "incomplete_or_invalid_evidence", "gates": {
            "raw_parity": False, "correct_run_count": False,
            "per_scenario_success": False, "coordinated_call_mean": False}}
    parity = evidence.get("raw_parity", [])
    aggregates = evidence.get("aggregates", {})
    cases = aggregates.get("per_case", {})
    raw_ok = bool(parity) and all(row.get("passed") for row in parity)
    count_ok = aggregates.get("candidate_correctness_ratio", 0.0) > aggregates.get("baseline_correctness_ratio", 0.0)
    scenario_ok = bool(cases) and all(row.get("candidate_successes", 0) >= row.get("baseline_successes", 0) for row in cases.values())
    calls_ok = (aggregates.get("coordinated_candidate_total_runs", 0) > 0 and
                aggregates.get("coordinated_baseline_total_runs", 0) > 0 and
                aggregates.get("coordinated_candidate_mean_calls", 0) < aggregates.get("coordinated_baseline_mean_calls", 0))
    gates = {"raw_parity": raw_ok, "correct_run_count": count_ok,
             "per_scenario_success": scenario_ok, "coordinated_call_mean": calls_ok}
    return {"decision": "implement" if all(gates.values()) else "do_not_implement", "blocker": None, "gates": gates}


def build_evidence(cases: Iterable[UnifiedSearchCase], *, runs: int, model: str | None,
                   transport_configured: bool, post_factory: Callable[[UnifiedSearchCase, str, int], Post] | None = None,
                   public_registry_contains_tool: bool = False) -> dict[str, Any]:
    case_list = sorted(cases, key=lambda item: item.id)
    evidence: dict[str, Any] = {"model": model or "", "runs": runs, "required_runs": 3,
        "transport_configured": transport_configured, "malformed": False,
        "public_registry_contains_tool": public_registry_contains_tool, "raw_parity": [raw_parity(case) for case in case_list],
        "shared_prompt_hashes": [], "shared_environment_hashes": [],
        "expected_run_results": len(case_list) * 2 * max(0, runs)}
    if post_factory is None:
        evidence.update({"run_results": [], "aggregates": {"candidate_correct_runs": 0, "baseline_correct_runs": 0, "per_case": {}}, "runner_blockers": ["transport_not_configured"]})
    else:
        evidence.update(run_workflow(case_list, runs=runs, model=model or "", post_factory=post_factory))
        evidence["malformed"] = any(row["status"] != "ok" for row in evidence["run_results"])
        evidence["shared_prompt_hashes"] = sorted({row["prompt_hash"] for row in evidence["run_results"]})
        evidence["shared_environment_hashes"] = sorted({row["environment_hash"] for row in evidence["run_results"]})
    evidence.update(decide(evidence))
    return evidence
