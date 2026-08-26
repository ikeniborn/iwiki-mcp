"""Deterministic comparison harness for the unregistered unified-search candidate."""

from __future__ import annotations

from collections import defaultdict
from functools import lru_cache
import hashlib
import json as json_module
import random
from typing import Any, Callable, Iterable

from .agent import AgentRun, Post, run_agent_case
from . import agent as agent_module
from .candidate import compose_unified_search
from .fixtures import FIXED_CASES, UnifiedSearchCase


REQUIRED_PAIRS = 20
MAX_ATTEMPTS = 30
NON_INFERIORITY_MARGIN = 0.15
BOOTSTRAP_SAMPLES = 50_000
BOOTSTRAP_SEED = 20260826
_METADATA_CACHE: dict[tuple[str, str, str], str] = {}


def _protocol() -> dict[str, Any]:
    return {"expected_case_ids": [case.id for case in FIXED_CASES],
            "required_pairs": REQUIRED_PAIRS, "max_attempts": MAX_ATTEMPTS,
            "non_inferiority_margin": NON_INFERIORITY_MARGIN,
            "bootstrap_samples": BOOTSTRAP_SAMPLES, "bootstrap_seed": BOOTSTRAP_SEED}


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
            "client_visible_calls": len(run.tool_trace),
            "required_fact_loss": len(run.missing_fact_ids), "tool_trace": [dict(item) for item in run.tool_trace],
            "result": dict(run.parsed_answer) if isinstance(run.parsed_answer, dict) else None,
            "prompt_hash": run.prompt_hash, "environment_hash": run.environment_hash,
            "shared_messages": list(run.shared_messages), "tool_schema_hash": run.tool_schema_hash,
            "declared_tool_names": list(run.declared_tool_names), "observed_context_seeds": list(observed_seeds) if observed_seeds is not None else None}


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
                             "candidate_success_rate": sum(item["success"] for item in candidate) / len(candidate) if candidate else 0.0,
                             "baseline_success_rate": sum(item["success"] for item in baseline) / len(baseline) if baseline else 0.0,
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


def paired_lower_bound(differences: Iterable[int | float], *, samples: int = BOOTSTRAP_SAMPLES,
                       seed: int = BOOTSTRAP_SEED) -> float:
    """Return deterministic one-sided 95% paired-bootstrap lower bound."""
    return _paired_lower_bound(tuple(float(value) for value in differences), samples, seed)


@lru_cache(maxsize=None)
def _paired_lower_bound(values: tuple[float, ...], samples: int, seed: int) -> float:
    if not values or samples <= 0:
        return 0.0
    generator = random.Random(seed)
    count = len(values)
    means = sorted(sum(values[generator.randrange(count)] for _ in range(count)) / count
                   for _ in range(samples))
    return means[max(0, int(samples * 0.05) - 1)]


def _case_seed(case_id: str) -> int:
    digest = hashlib.sha256(f"{BOOTSTRAP_SEED}:{case_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _failure_counts(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {"baseline": {}, "candidate": {}}
    for row in rows:
        status = row.get("status", "failed_runner")
        if status != "ok":
            arm = row.get("arm", "unknown")
            arm_counts = counts.setdefault(arm, {})
            arm_counts[status] = arm_counts.get(status, 0) + 1
    return {arm: dict(sorted(statuses.items())) for arm, statuses in sorted(counts.items())}


def _quality(rows: list[dict[str, Any]], cases: Iterable[UnifiedSearchCase]) -> dict[str, Any]:
    differences: dict[str, list[int]] = defaultdict(list)
    by_attempt: dict[tuple[str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        by_attempt[(row["case_id"], row["attempt"])][row["arm"]] = row
    for (case_id, _attempt), arms in by_attempt.items():
        if "candidate" in arms and "baseline" in arms:
            differences[case_id].append(int(arms["candidate"]["success"]) - int(arms["baseline"]["success"]))
    scenario = {case.id: paired_lower_bound(differences[case.id], seed=_case_seed(case.id))
                for case in sorted(cases, key=lambda item: item.id)}
    aggregate_differences = [value for case_id in sorted(differences) for value in differences[case_id]]
    return {"aggregate_lower_bound": paired_lower_bound(aggregate_differences),
            "scenario_lower_bounds": scenario,
            "non_inferiority_margin": NON_INFERIORITY_MARGIN,
            "bootstrap_samples": BOOTSTRAP_SAMPLES, "bootstrap_seed": BOOTSTRAP_SEED}


def _tool_calls(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    included = [item for item in attempts if item["included"]]
    excluded = [item for item in attempts if not item["included"]]

    def calls(items: list[dict[str, Any]], arm: str) -> list[int]:
        return [next(row["client_visible_calls"] for row in item["rows"] if row["arm"] == arm)
                for item in items]

    candidate, baseline = calls(included, "candidate"), calls(included, "baseline")
    excluded_candidate, excluded_baseline = calls(excluded, "candidate"), calls(excluded, "baseline")
    mean = lambda values: sum(values) / len(values) if values else 0.0
    reasons: dict[str, int] = {}
    for item in excluded:
        reason = item["exclusion_reason"] or "unknown"
        reasons[reason] = reasons.get(reason, 0) + 1
    return {"included_pair_candidate_mean": mean(candidate), "included_pair_baseline_mean": mean(baseline),
            "included_pair_mean_difference": mean([left - right for left, right in zip(candidate, baseline)]),
            "excluded_attempt_calls": {"candidate_total": sum(excluded_candidate), "baseline_total": sum(excluded_baseline),
                                       "candidate_mean": mean(excluded_candidate), "baseline_mean": mean(excluded_baseline),
                                       "reasons": dict(sorted(reasons.items()))}}


def _row_integrity(row: dict[str, Any], case: UnifiedSearchCase) -> bool:
    status = row.get("status")
    allowed_statuses = {"ok", "blocked_cycle", "failed_final_json", "failed_invalid_arguments", "failed_malformed_arguments", "failed_max_rounds", "failed_response", "failed_tool", "failed_tool_envelope", "failed_tool_limit", "failed_tool_output", "failed_transport", "failed_unknown_tool", "failed_runner"}
    if status not in allowed_statuses:
        return False
    result = row.get("result")
    if status != "ok":
        if result is not None:
            return False
        facts: list[str] = []
        graph_value = None
    else:
        if not isinstance(result, dict) or set(result) != {"fact_ids", "graph_state"}:
            return False
        facts = result.get("fact_ids")
        graph_value = result.get("graph_state")
        if not isinstance(facts, list) or not all(isinstance(fact, str) for fact in facts) or not isinstance(graph_value, str):
            return False
    expected = case.expected_fact_ids
    actual = tuple(facts)
    missing = tuple(item for index, item in enumerate(expected) if index >= len(actual) or actual[index] != item)
    extra = tuple(item for index, item in enumerate(actual) if index >= len(expected) or expected[index] != item)
    graph_correct = graph_value == _expected_graph_state(case)
    success = status == "ok" and not missing and not extra and graph_correct
    names = [entry.get("name") for entry in row.get("tool_trace", [])] if isinstance(row.get("tool_trace"), list) else []
    expected_names = ["wiki_search", "wiki_code_search", "wiki_code_context"] if row.get("arm") == "baseline" else ["wiki_unified_search"]
    trace = row.get("tool_trace")
    if not isinstance(trace, list) or any(not isinstance(entry, dict) or set(entry) != {"name", "call_id"} or not isinstance(entry.get("name"), str) or not entry["name"] or not isinstance(entry.get("call_id"), str) or not entry["call_id"] for entry in trace):
        return False
    if row.get("declared_tool_names") != expected_names or any(name not in expected_names for name in names):
        return False
    context_name = "wiki_code_context" if row["arm"] == "baseline" else "wiki_unified_search"
    omitted = int(bool(_first_three_unique_seeds(case.code.as_dict())) and context_name not in names)
    graph_mismatch = int(not graph_correct)
    expected_fields = {
        "missing_fact_ids": list(missing), "extra_fact_ids": list(extra), "graph_state_correct": graph_correct,
        "success": success, "correctness": success, "omitted_required_context_calls": omitted,
        "stale_claim_errors": int(graph_mismatch and _expected_graph_state(case) == "stale"),
        "missing_claim_errors": int(graph_mismatch and _expected_graph_state(case) == "missing"),
        "revision_changed_claim_errors": int(graph_mismatch and case.id == "revision-mismatch"),
        "required_fact_loss": len(missing),
    }
    expected_fields["stale_missing_revision_claim_errors"] = (expected_fields["stale_claim_errors"] + expected_fields["missing_claim_errors"] + expected_fields["revision_changed_claim_errors"])
    if any(row.get(key) != value for key, value in expected_fields.items()):
        return False
    observed = row.get("observed_context_seeds")
    expected_seeds = _first_three_unique_seeds(case.code.as_dict())
    expected_seed_mistakes = int(bool(expected_seeds) and observed != expected_seeds)
    return (observed is None or isinstance(observed, list) and len(observed) <= 3 and all(isinstance(seed, str) for seed in observed)) and (
            isinstance(row.get("seed_mistakes"), int) and row["seed_mistakes"] == expected_seed_mistakes and
            isinstance(row.get("client_visible_calls"), int) and row["client_visible_calls"] == len(trace) and
            0 <= row["client_visible_calls"] <= (12 if row["arm"] == "baseline" else 4) and
            isinstance(row.get("prompt_hash"), str) and bool(row["prompt_hash"]) and
            isinstance(row.get("environment_hash"), str) and bool(row["environment_hash"]) and
            isinstance(row.get("shared_messages"), list) and isinstance(row.get("tool_schema_hash"), str) and bool(row["tool_schema_hash"]))


def _expected_metadata(case: UnifiedSearchCase, arm: str, model: str) -> dict[str, Any]:
    key = (case.id, arm, model)
    if key not in _METADATA_CACHE:
        def fail_post(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("metadata probe")
        probe = agent_module.run_agent_case(case, arm, model, fail_post)
        metadata = {"environment_hash": probe.environment_hash, "prompt_hash": probe.prompt_hash,
                    "tool_schema_hash": probe.tool_schema_hash, "shared_messages": probe.shared_messages,
                    "declared_tool_names": list(probe.declared_tool_names)}
        _METADATA_CACHE[key] = json_module.dumps(metadata, sort_keys=True, separators=(",", ":"))
    return json_module.loads(_METADATA_CACHE[key])


def _failed_runner_metrics(case: UnifiedSearchCase, arm: str, model: str) -> dict[str, Any]:
    metadata = _expected_metadata(case, arm, model)
    expected_graph = _expected_graph_state(case)
    missing = list(case.expected_fact_ids)
    names: list[str] = []
    context_name = "wiki_code_context" if arm == "baseline" else "wiki_unified_search"
    omitted = int(bool(_first_three_unique_seeds(case.code.as_dict())) and context_name not in names)
    expected_seeds = _first_three_unique_seeds(case.code.as_dict())
    return {"arm": arm, "case_id": case.id, "status": "failed_runner", "correctness": False, "success": False,
            "missing_fact_ids": missing, "extra_fact_ids": [], "graph_state_correct": False,
            "seed_mistakes": int(bool(expected_seeds)), "observed_context_seeds": None,
            "omitted_required_context_calls": omitted,
            "stale_claim_errors": int(expected_graph == "stale"), "missing_claim_errors": int(expected_graph == "missing"),
            "revision_changed_claim_errors": int(case.id == "revision-mismatch"),
            "stale_missing_revision_claim_errors": int(expected_graph == "stale") + int(expected_graph == "missing") + int(case.id == "revision-mismatch"),
            "client_visible_calls": 0, "required_fact_loss": len(missing), "tool_trace": [], "result": None, **metadata}


def _retained_integrity(evidence: dict[str, Any], expected_ids: list[str]) -> bool:
    try:
        attempts = evidence.get("attempts")
        if not isinstance(attempts, list):
            return False
        by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in attempts:
            if not isinstance(item, dict) or item.get("case_id") not in expected_ids:
                return False
            by_case[item["case_id"]].append(item)
        if set(by_case) != set(expected_ids):
            return False
        included: list[dict[str, Any]] = []
        exclusion_reasons: dict[str, int] = {}
        per_case: dict[str, dict[str, int]] = {}
        all_rows: list[dict[str, Any]] = []
        cases_by_id = {case.id: case for case in FIXED_CASES}
        for case_id in expected_ids:
            items = by_case[case_id]
            numbers = [item.get("attempt") for item in items]
            if not (REQUIRED_PAIRS <= len(items) <= MAX_ATTEMPTS and numbers == list(range(1, len(items) + 1))):
                return False
            included_count = excluded_count = 0
            for item in items:
                order = ["baseline", "candidate"] if item["attempt"] % 2 else ["candidate", "baseline"]
                rows = item.get("rows")
                if item.get("arm_order") != order or not isinstance(rows, list) or len(rows) != 2 or not all(isinstance(row, dict) for row in rows):
                    return False
                if [row.get("arm") for row in rows] != order:
                    return False
                for row in rows:
                    if row.get("case_id") != case_id or row.get("attempt") != item["attempt"] or not isinstance(row.get("status"), str):
                        return False
                    if not _row_integrity(row, cases_by_id[case_id]) or row.get("run") != included_count + 1:
                        return False
                    metadata = _expected_metadata(cases_by_id[case_id], row["arm"], evidence["model"])
                    if any(row.get(key) != value for key, value in metadata.items()):
                        return False
                if (rows[0].get("prompt_hash") != rows[1].get("prompt_hash") or rows[0].get("environment_hash") != rows[1].get("environment_hash") or
                        rows[0].get("shared_messages") != rows[1].get("shared_messages")):
                    return False
                transport_failed = any(row["status"] == "failed_transport" for row in rows)
                expected_included = not transport_failed
                expected_reason = None if expected_included else "failed_transport"
                if item.get("included") is not expected_included or item.get("exclusion_reason") != expected_reason:
                    return False
                if any(row.get("included") is not expected_included or row.get("exclusion_reason") != expected_reason for row in rows):
                    return False
                all_rows.extend(rows)
                if expected_included:
                    included_count += 1
                    included.extend(rows)
                    if included_count == REQUIRED_PAIRS and item is not items[-1]:
                        return False
                else:
                    excluded_count += 1
                    exclusion_reasons["failed_transport"] = exclusion_reasons.get("failed_transport", 0) + 1
            if included_count != REQUIRED_PAIRS:
                return False
            per_case[case_id] = {"total_attempts": len(items), "included_pairs": included_count, "excluded_pairs": excluded_count,
                                 "exclusion_reasons": {"failed_transport": excluded_count} if excluded_count else {}}
        expected_counts = {"total_attempts": len(attempts), "included_pairs": len(included) // 2,
                           "excluded_pairs": len(attempts) - len(included) // 2, "per_case": per_case,
                           "exclusion_reasons": dict(sorted(exclusion_reasons.items()))}
        if evidence.get("attempt_counts") != expected_counts:
            return False
        sampling = evidence.get("sampling")
        if not isinstance(sampling, dict) or sampling.get("per_case_included_pairs") != {case_id: REQUIRED_PAIRS for case_id in expected_ids}:
            return False
        results = evidence.get("run_results")
        if not isinstance(results, list) or not all(isinstance(row, dict) for row in results):
            return False
        def canonical(row: dict[str, Any]) -> str:
            return json_module.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if sorted(canonical(row) for row in results) != sorted(canonical(row) for row in included):
            return False
        if (sorted({row["prompt_hash"] for row in included}) != evidence.get("shared_prompt_hashes") or
                sorted({row["environment_hash"] for row in included}) != evidence.get("shared_environment_hashes")):
            return False
        return (_aggregate(included, FIXED_CASES, REQUIRED_PAIRS) == evidence.get("aggregates") and
                _quality(included, FIXED_CASES) == evidence.get("quality") and
                _failure_counts(all_rows) == evidence.get("workflow_failure_counts") and
                _tool_calls(attempts) == evidence.get("tool_calls"))
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


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
    included_rows: list[dict[str, Any]] = []
    attempt_rows: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    for case in case_list:
        included = 0
        for attempt_index in range(1, MAX_ATTEMPTS + 1):
            if included >= runs:
                break
            order = ("baseline", "candidate") if attempt_index % 2 else ("candidate", "baseline")
            pair_rows: list[dict[str, Any]] = []
            for arm in order:
                try:
                    observed: list[list[str]] = []
                    post = post_factory(case, arm, attempt_index - 1)
                    if arm == "baseline":
                        post = _capture_context_seeds(post, observed)
                    run = run_agent_case(case, arm, model, post)
                    seeds = _candidate_seeds(case) if arm == "candidate" else (observed[-1] if observed else None)
                    row = _run_metrics(run, case, seeds)
                except Exception:
                    row = _failed_runner_metrics(case, arm, model)
                row["attempt"] = attempt_index
                row["run"] = included + 1
                pair_rows.append(row)
            excluded = any(row["status"] == "failed_transport" for row in pair_rows)
            reason = "failed_transport" if excluded else None
            for row in pair_rows:
                row["included"] = not excluded
                row["exclusion_reason"] = reason
            attempts.append({"case_id": case.id, "attempt": attempt_index, "arm_order": list(order),
                             "included": not excluded, "exclusion_reason": reason, "rows": pair_rows})
            attempt_rows.extend(pair_rows)
            if excluded:
                continue
            included_rows.extend(pair_rows)
            included += 1
    included_rows.sort(key=lambda row: (row["case_id"], row["attempt"], row["arm"]))
    attempts.sort(key=lambda item: (item["case_id"], item["attempt"]))
    per_case_included_pairs = {case.id: sum(item["included"] for item in attempts if item["case_id"] == case.id)
                               for case in case_list}
    complete = all(count >= runs for count in per_case_included_pairs.values())
    sampling = {"required_pairs": runs, "max_attempts": MAX_ATTEMPTS, "complete": complete,
                "attempt_cap_exhausted": not complete, "per_case_included_pairs": per_case_included_pairs}
    exclusion_reasons: dict[str, int] = {}
    for item in attempts:
        reason = item["exclusion_reason"]
        if reason:
            exclusion_reasons[reason] = exclusion_reasons.get(reason, 0) + 1
    attempt_counts = {"total_attempts": len(attempts), "included_pairs": sum(item["included"] for item in attempts),
                      "excluded_pairs": sum(not item["included"] for item in attempts),
                      "per_case": {case.id: {"total_attempts": sum(item["case_id"] == case.id for item in attempts),
                                              "included_pairs": per_case_included_pairs[case.id],
                                              "excluded_pairs": sum(item["case_id"] == case.id and not item["included"] for item in attempts),
                                              "exclusion_reasons": {"failed_transport": sum(item["case_id"] == case.id and item["exclusion_reason"] == "failed_transport" for item in attempts)} if any(item["case_id"] == case.id and item["exclusion_reason"] == "failed_transport" for item in attempts) else {}}
                                   for case in case_list},
                      "exclusion_reasons": dict(sorted(exclusion_reasons.items()))}
    return {"attempts": attempts, "run_results": included_rows,
            "aggregates": _aggregate(included_rows, case_list, runs),
            "sampling": sampling, "attempt_counts": attempt_counts, "quality": _quality(included_rows, case_list),
            "workflow_failure_counts": _failure_counts(attempt_rows), "tool_calls": _tool_calls(attempts),
            "runner_blockers": []}


def decide(evidence: dict[str, Any]) -> dict[str, Any]:
    attempts = evidence.get("attempts") if isinstance(evidence, dict) else None
    sampling_started = isinstance(attempts, list) and bool(attempts)
    prerequisites = (isinstance(evidence, dict) and isinstance(evidence.get("model"), str) and bool(evidence["model"]) and
                     evidence.get("transport_configured") is True and evidence.get("tool_calling_available") is True and
                     evidence.get("preflight") == {"available": True, "status": "supported"})
    if not prerequisites:
        return {"decision": "do_not_implement" if sampling_started else "blocked", "blocker": "preflight_unavailable" if not sampling_started else None, "gates": {}}
    try:
        protocol = evidence.get("protocol")
        sampling = evidence.get("sampling")
        quality = evidence.get("quality")
        expected_ids = _protocol()["expected_case_ids"]
        protocol_ok = protocol == _protocol()
        expected_parity = sorted((raw_parity(case) for case in FIXED_CASES), key=lambda row: row["case_id"])
        raw_ok = evidence.get("raw_parity") == expected_parity
        sampling_ok = (isinstance(sampling, dict) and sampling.get("required_pairs") == REQUIRED_PAIRS and sampling.get("max_attempts") == MAX_ATTEMPTS and
                       sampling.get("complete") is True and sampling.get("attempt_cap_exhausted") is False and
                       sampling.get("per_case_included_pairs") == {case_id: REQUIRED_PAIRS for case_id in expected_ids})
        quality_metadata_ok = (isinstance(quality, dict) and quality.get("non_inferiority_margin") == NON_INFERIORITY_MARGIN and
                               quality.get("bootstrap_samples") == BOOTSTRAP_SAMPLES and quality.get("bootstrap_seed") == BOOTSTRAP_SEED and
                               sorted(quality.get("scenario_lower_bounds", {})) == sorted(expected_ids))
        aggregate_ok = isinstance(quality, dict) and quality.get("aggregate_lower_bound", 0.0) > 0.0
        scenarios = quality.get("scenario_lower_bounds", {}) if isinstance(quality, dict) else {}
        scenario_ok = quality_metadata_ok and all(bound > -NON_INFERIORITY_MARGIN for bound in scenarios.values())
        retained_ok = _retained_integrity(evidence, expected_ids)
        registry_ok = evidence.get("public_registry_contains_tool") is False
    except (AttributeError, KeyError, TypeError, ValueError):
        protocol_ok = raw_ok = sampling_ok = aggregate_ok = scenario_ok = retained_ok = registry_ok = False
    ready = protocol_ok and raw_ok and registry_ok and sampling_ok and retained_ok and aggregate_ok and scenario_ok
    return {"decision": "implement" if ready else "do_not_implement", "blocker": None,
            "gates": {"preflight": True, "protocol": protocol_ok, "retained_attempts": retained_ok, "raw_parity": raw_ok, "sampling_complete": sampling_ok,
                      "aggregate_lower_bound": aggregate_ok, "scenario_lower_bounds": scenario_ok, "registry_unregistered": registry_ok}}


def build_evidence(cases: Iterable[UnifiedSearchCase], *, runs: int, model: str | None,
                   transport_configured: bool, post_factory: Callable[[UnifiedSearchCase, str, int], Post] | None = None,
                   public_registry_contains_tool: bool = False, tool_calling_available: bool = True,
                   preflight: dict[str, Any] | None = None) -> dict[str, Any]:
    case_list = sorted(cases, key=lambda item: item.id)
    evidence: dict[str, Any] = {"model": model or "", "runs": runs, "protocol": _protocol(),
        "transport_configured": transport_configured, "tool_calling_available": tool_calling_available,
        "preflight": dict(preflight or {"available": tool_calling_available, "status": "not_run"}),
        "public_registry_contains_tool": public_registry_contains_tool, "raw_parity": [raw_parity(case) for case in case_list],
        "shared_prompt_hashes": [], "shared_environment_hashes": [], "registry_state": {"public_registry_contains_tool": public_registry_contains_tool}}
    valid_preflight = bool(model) and transport_configured is True and tool_calling_available is True and preflight == {"available": True, "status": "supported"}
    if post_factory is None or not valid_preflight:
        evidence.update({"attempts": [], "attempt_counts": {"total_attempts": 0, "included_pairs": 0, "excluded_pairs": 0, "per_case": {}, "exclusion_reasons": {}}, "run_results": [], "aggregates": {"candidate_correct_runs": 0, "baseline_correct_runs": 0, "per_case": {}},
                         "sampling": {"required_pairs": runs, "max_attempts": MAX_ATTEMPTS, "complete": False, "attempt_cap_exhausted": False},
                         "quality": {"aggregate_lower_bound": 0.0, "scenario_lower_bounds": {}, "non_inferiority_margin": NON_INFERIORITY_MARGIN, "bootstrap_samples": BOOTSTRAP_SAMPLES, "bootstrap_seed": BOOTSTRAP_SEED},
                         "workflow_failure_counts": {"baseline": {}, "candidate": {}}, "tool_calls": {"baseline": 0, "candidate": 0}, "runner_blockers": []})
    else:
        evidence.update(run_workflow(case_list, runs=runs, model=model or "", post_factory=post_factory))
        evidence["shared_prompt_hashes"] = sorted({row["prompt_hash"] for row in evidence["run_results"]})
        evidence["shared_environment_hashes"] = sorted({row["environment_hash"] for row in evidence["run_results"]})
    evidence.update(decide(evidence))
    return evidence
