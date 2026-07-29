from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from dataclasses import replace
from itertools import combinations
import json
import math

from iwiki_mcp.engine.fusion import fuse_ranked

from .analyzer import analyze_trace
from .fixtures import BenchmarkCase
from .fixtures import hard_negative_records
from .metrics import intent_coverage_at_k
from .metrics import mrr_at_k
from .metrics import ndcg_at_k
from .metrics import recall_at_k


PAGE_WEIGHTS = (0.025, 0.05, 0.1)
GRAPH_WEIGHTS = (0.01, 0.025, 0.05)
RERANK_BATCHES = (16, 24, 32)
_FINAL_RECOVERY_K = 8
_REQUIRED_FUSION_MODES = ("hybrid", "lexical", "semantic")
_RANKED_HIT_FIELDS = ("domain", "file", "heading", "chunk", "ordinal")
_DIRECT_SIGNALS = ("semantic_chunk", "lexical_section")
_BROAD_SIGNALS = ("semantic_page", "lexical_page", "graph_page")


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


def _candidate_key(candidate: FusionCandidate) -> str:
    return json.dumps(candidate.payload(), sort_keys=True, separators=(",", ":"))


def _is_positive_int(value) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and value > 0
    )


def _validated_candidate(candidate: FusionCandidate) -> FusionCandidate:
    if not isinstance(candidate, FusionCandidate):
        raise ValueError("candidate must be a FusionCandidate")
    if candidate.family not in {
        "baseline", "rrf_k", "direct_multiplier", "direct_quota",
        "fanout_cap", "pair",
    }:
        raise ValueError("candidate family is invalid")
    if not _is_positive_int(candidate.rrf_k):
        raise ValueError("candidate rrf_k must be a positive integer")
    if (
        isinstance(candidate.direct_multiplier, bool)
        or not isinstance(candidate.direct_multiplier, (int, float))
        or not math.isfinite(candidate.direct_multiplier)
        or candidate.direct_multiplier < 1.0
    ):
        raise ValueError("candidate direct_multiplier must be at least 1.0")
    if (
        isinstance(candidate.direct_quota, bool)
        or not isinstance(candidate.direct_quota, int)
        or not 0 <= candidate.direct_quota <= _FINAL_RECOVERY_K
    ):
        raise ValueError("candidate direct_quota must be between 0 and 8")
    if candidate.fanout_cap is not None and not _is_positive_int(candidate.fanout_cap):
        raise ValueError("candidate fanout_cap must be a positive integer")
    if (
        not isinstance(candidate.components, tuple)
        or not all(isinstance(item, str) and item for item in candidate.components)
    ):
        raise ValueError("candidate components are invalid")

    changed = sum((
        candidate.rrf_k != 60,
        candidate.direct_multiplier != 1.0,
        candidate.direct_quota != 0,
        candidate.fanout_cap is not None,
    ))
    expected_changes = {
        "baseline": 0,
        "rrf_k": 1,
        "direct_multiplier": 1,
        "direct_quota": 1,
        "fanout_cap": 1,
        "pair": 2,
    }[candidate.family]
    if changed != expected_changes:
        raise ValueError("candidate has unrelated fields")
    if candidate.family == "baseline" and candidate.components:
        raise ValueError("candidate baseline has components")
    if candidate.family != "pair" and candidate.components:
        raise ValueError("candidate family has components")
    if candidate.family == "pair" and len(candidate.components) != 2:
        raise ValueError("candidate pair must have two components")
    return candidate


def stage_a_candidates() -> list[FusionCandidate]:
    return [
        *(FusionCandidate(family="rrf_k", rrf_k=value) for value in (10, 20, 40)),
        *(
            FusionCandidate(family="direct_multiplier", direct_multiplier=value)
            for value in (1.25, 1.5, 2.0)
        ),
        *(
            FusionCandidate(family="direct_quota", direct_quota=value)
            for value in (1, 2, 3)
        ),
        *(FusionCandidate(family="fanout_cap", fanout_cap=value) for value in (1, 2, 4)),
    ]


def stage_b_candidates(winners) -> list[FusionCandidate]:
    family_winners = sorted(
        (_validated_candidate(candidate) for candidate in winners),
        key=_candidate_key,
    )
    candidates = []
    for first, second in combinations(family_winners, 2):
        if first.family == second.family:
            continue
        values = {
            "rrf_k": 60,
            "direct_multiplier": 1.0,
            "direct_quota": 0,
            "fanout_cap": None,
        }
        for candidate in (first, second):
            for name, default in tuple(values.items()):
                value = getattr(candidate, name)
                if value != default:
                    values[name] = value
        components = tuple(sorted((_candidate_key(first), _candidate_key(second))))
        candidates.append(FusionCandidate(
            family="pair",
            components=components,
            **values,
        ))
    return sorted(candidates, key=_candidate_key)[:6]


def transform_candidate_signals(
    signals: dict[str, list[dict]],
    candidate: FusionCandidate,
) -> dict[str, list[dict]]:
    candidate = _validated_candidate(candidate)
    transformed = {}
    for signal, hits in signals.items():
        copied_hits = [dict(hit) for hit in hits]
        if candidate.fanout_cap is not None and signal in _BROAD_SIGNALS:
            seen_by_file = Counter()
            capped_hits = []
            for hit in copied_hits:
                file_identity = (hit["domain"], hit["file"])
                seen_by_file[file_identity] += 1
                if seen_by_file[file_identity] <= candidate.fanout_cap:
                    capped_hits.append(hit)
            copied_hits = capped_hits
        transformed[signal] = copied_hits
    return transformed


def _unique_identity_count(signals: dict[str, list[dict]]) -> int:
    return len({
        _identity(hit)
        for hits in signals.values()
        for hit in hits
    })


def _quota_ranking(
    fused: list[dict],
    direct_hits: list[dict],
    final_k: int,
) -> list[dict]:
    reserved = []
    reserved_identities = set()
    for hit in direct_hits:
        identity = _identity(hit)
        if identity not in reserved_identities:
            reserved_identities.add(identity)
            reserved.append(hit)
    reserved = reserved[:final_k]
    reserved_ids = {_identity(hit) for hit in reserved}
    top = list(fused[:final_k])
    top_ids = {_identity(hit) for hit in top}
    missing = [hit for hit in reserved if _identity(hit) not in top_ids]
    for hit in missing:
        for index in range(len(top) - 1, -1, -1):
            if _identity(top[index]) not in reserved_ids:
                del top[index]
                break
        top.append(hit)
    ordered = top + [
        hit for hit in fused
        if _identity(hit) not in {_identity(item) for item in top}
    ]
    return ordered


def fuse_candidate_signals(
    signals: dict[str, list[dict]],
    candidate: FusionCandidate,
    *,
    limit: int,
    final_k: int,
) -> list[dict]:
    candidate = _validated_candidate(candidate)
    transformed = transform_candidate_signals(signals, candidate)
    weights = {
        signal: candidate.direct_multiplier
        for signal in _DIRECT_SIGNALS
        if signal in transformed
    }
    fused = fuse_ranked(
        transformed,
        limit,
        weights,
        rrf_k=candidate.rrf_k,
    )
    if not candidate.direct_quota:
        return fused
    direct_signals = {
        signal: transformed[signal]
        for signal in _DIRECT_SIGNALS
        if signal in transformed
    }
    direct_hits = fuse_ranked(
        direct_signals,
        _unique_identity_count(direct_signals),
        weights,
        rrf_k=candidate.rrf_k,
    )[:candidate.direct_quota]
    return _quota_ranking(fused, direct_hits, final_k)


def replay_fusion_candidate(
    trace: dict,
    candidate: FusionCandidate,
) -> list[str] | None:
    signals = _ranked_signals(trace)
    k = trace.get("k")
    if signals is None or not _is_positive_int(k):
        return None
    fused = fuse_candidate_signals(
        signals,
        candidate,
        limit=_unique_identity_count(signals),
        final_k=k,
    )
    return [_identity(hit) for hit in fused]


def fusion_weight_grid() -> list[dict[str, float]]:
    return [
        {
            "semantic_chunk": 1.0,
            "lexical_section": 1.0,
            "semantic_page": page_weight,
            "lexical_page": page_weight,
            "graph_page": graph_weight,
        }
        for page_weight in PAGE_WEIGHTS
        for graph_weight in GRAPH_WEIGHTS
        if graph_weight <= page_weight
    ]


def _identity(hit: dict) -> str:
    return f"{hit['domain']}/{hit['file']}#{hit['heading']}:{hit['chunk']}"


def _ranked_signals(trace: dict) -> dict[str, list[dict]] | None:
    stages = trace.get("stages")
    if not isinstance(stages, dict):
        return None
    signals_stage = stages.get("signals")
    if not isinstance(signals_stage, dict):
        return None
    ranked = signals_stage.get("ranked")
    if not isinstance(ranked, dict):
        return None
    signals = {}
    for name, hits in ranked.items():
        if not isinstance(name, str) or not name or not isinstance(hits, list):
            return None
        signal_hits = []
        for hit in hits:
            if not isinstance(hit, dict) or set(hit) != set(_RANKED_HIT_FIELDS):
                return None
            if (
                not all(isinstance(hit[key], str) and hit[key] for key in (
                    "domain", "file", "heading",
                ))
                or any(
                    isinstance(hit[key], bool) or not isinstance(hit[key], int)
                    for key in ("chunk", "ordinal")
                )
            ):
                return None
            signal_hits.append({key: hit[key] for key in _RANKED_HIT_FIELDS})
        signals[name] = signal_hits
    return signals


def replay_fusion(trace: dict, weights: dict[str, float]) -> list[str] | None:
    signals = _ranked_signals(trace)
    if signals is None:
        return None
    limit = len({
        _identity(hit)
        for hits in signals.values()
        for hit in hits
    })
    return [_identity(hit) for hit in fuse_ranked(signals, limit, weights)]


def _metric_case(case: BenchmarkCase, trace: dict) -> BenchmarkCase:
    metrics_input = trace.get("metrics_input", {})
    return replace(
        case,
        relevant=dict(metrics_input.get("relevant", case.relevant)),
        intents={
            name: list(values)
            for name, values in metrics_input.get("intents", case.intents).items()
        },
    )


def _metrics(case: BenchmarkCase, trace: dict, ranking: list[str]) -> dict[str, float]:
    metric_case = _metric_case(case, trace)
    k = int(trace.get("k", case.k))
    return {
        "recall_at_k": recall_at_k(ranking, metric_case, k),
        "mrr_at_k": mrr_at_k(ranking, metric_case, k),
        "ndcg_at_k": ndcg_at_k(ranking, metric_case, k),
        "intent_coverage_at_k": intent_coverage_at_k(ranking, metric_case, k),
    }


def _mean_metrics(values: list[dict[str, float]]) -> dict[str, float]:
    if not values:
        return {
            "recall_at_k": 0.0,
            "mrr_at_k": 0.0,
            "ndcg_at_k": 0.0,
            "intent_coverage_at_k": 0.0,
        }
    return {
        name: round(sum(item[name] for item in values) / len(values), 6)
        for name in values[0]
    }


def _per_mode(
    records: list[tuple[str, dict[str, float]]],
) -> dict[str, dict[str, float]]:
    grouped: dict[str, list[dict[str, float]]] = {}
    for mode, metrics in records:
        grouped.setdefault(mode, []).append(metrics)
    return {mode: _mean_metrics(grouped[mode]) for mode in sorted(grouped)}


def _weights_key(weights: dict[str, float]) -> str:
    return json.dumps(weights, sort_keys=True, separators=(",", ":"))


def _replayed_trace(trace: dict, ranking: list[str]) -> dict:
    raw_stages = trace.get("stages")
    stages = dict(raw_stages) if isinstance(raw_stages, dict) else {}
    stages["fusion"] = {"candidate_identities": ranking}
    return {
        **trace,
        "stages": stages,
        "metrics_input": {
            **trace.get("metrics_input", {}),
            "ranking": ranking[:int(trace.get("k", 0))],
        },
    }


def _loss_findings(
    case: BenchmarkCase,
    trace: dict,
    ranking: list[str],
) -> set[tuple]:
    replay = _replayed_trace(trace, ranking)
    return {
        (finding["case_id"], finding["class"], finding["identity"])
        for finding in analyze_trace(case, replay)
        if finding["class"] == "lost_after_fusion_topk"
    }


def _has_complete_case_mode_evidence(cases, traces) -> bool:
    case_ids = [case.case_id for case in cases]
    expected_cases = set(case_ids)
    counts = Counter()

    for trace in traces:
        case_id = trace.get("case_id")
        mode = trace.get("mode")
        if case_id not in expected_cases or mode not in _REQUIRED_FUSION_MODES:
            return False
        counts[(case_id, mode)] += 1

    expected = {
        (case_id, mode)
        for case_id in expected_cases
        for mode in _REQUIRED_FUSION_MODES
    }
    return (
        bool(expected_cases)
        and len(case_ids) == len(expected_cases)
        and set(counts) == expected
        and all(count == 1 for count in counts.values())
    )


def _has_valid_trace_k(traces: list[dict]) -> bool:
    return all(
        isinstance(trace.get("k"), int)
        and not isinstance(trace.get("k"), bool)
        and trace["k"] > 0
        for trace in traces
    )


def _fusion_gate_reasons(
    modes: dict[str, dict[str, float]],
    baseline_modes: dict[str, dict[str, float]],
    findings: set[tuple],
    baseline_findings: set[tuple],
    trace_list: list[dict],
    rankings: list[list[str] | None],
    hard_negatives: list[dict],
    *,
    case_mode_evidence_complete: bool,
    replay_evidence_incomplete: bool,
    hard_negative_evidence_invalid: bool,
    hard_negative_evidence_incomplete: bool,
) -> list[str]:
    reasons = []
    if hard_negative_evidence_invalid:
        reasons.append("hard_negative_evidence_invalid")
    if not case_mode_evidence_complete:
        reasons.append("case_mode_evidence_incomplete")
    if replay_evidence_incomplete or any(ranking is None for ranking in rankings):
        reasons.append("replay_evidence_incomplete")
    if hard_negative_evidence_incomplete:
        reasons.append("hard_negative_evidence_incomplete")
    if any(
        modes[mode]["recall_at_k"] < baseline_modes[mode]["recall_at_k"]
        for mode in modes
    ):
        reasons.append("recall_regression")
    if any(
        modes[mode]["intent_coverage_at_k"]
        < baseline_modes[mode]["intent_coverage_at_k"]
        for mode in modes
    ):
        reasons.append("intent_coverage_regression")
    if any(
        modes[mode]["ndcg_at_k"] < baseline_modes[mode]["ndcg_at_k"] - 0.01
        for mode in modes
    ):
        reasons.append("ndcg_loss_exceeds_limit")
    if findings - baseline_findings:
        reasons.append("new_lost_after_fusion_top_k")
    active_targets = {
        (record["case_id"], record["mode"], record["identity"])
        for record in hard_negatives
        if record["state"] == "active"
    }
    for trace, ranking in zip(trace_list, rankings):
        required = {
            identity
            for case_id, mode, identity in active_targets
            if (case_id, mode) == (trace["case_id"], trace.get("mode", ""))
        }
        if required - set((ranking or [])[:trace["k"]]):
            reasons.append("confirmed_loss_not_recovered")
            break
    return sorted(set(reasons))


def _invalid_fusion_evidence(
    hard_negatives: list[dict],
    *,
    reason: str = "evidence_invalid",
) -> dict:
    candidates = [
        {
            "weights": dict(sorted(weights.items())),
            "weights_key": _weights_key(weights),
            "metrics": {"aggregate": _mean_metrics([]), "modes": {}},
            "passed": False,
            "reasons": [reason],
        }
        for weights in fusion_weight_grid()
    ]
    candidates.sort(key=lambda item: item["weights_key"])
    return {
        "status": "needs_work",
        "reason": reason,
        "weights": None,
        "hard_negatives": hard_negatives,
        "candidates": candidates,
    }


def _hard_negative_evidence(cases, traces) -> dict:
    records = hard_negative_records(cases, traces)
    active_records = [
        record for record in records if record["state"] == "active"
    ]
    invalid = any(record["state"] == "invalid" for record in records)
    incomplete = len(active_records) < 2
    return {
        "records": records,
        "active_records": active_records,
        "invalid": invalid,
        "incomplete": incomplete,
        "ready": not invalid and not incomplete,
    }


def select_fusion_weights(cases, traces) -> dict:
    case_list = list(cases)
    all_traces = list(traces)
    hard_negative_evidence = _hard_negative_evidence(case_list, all_traces)
    hard_negatives = hard_negative_evidence["records"]
    if hard_negative_evidence["invalid"]:
        return _invalid_fusion_evidence(
            hard_negatives,
            reason="hard_negative_evidence_invalid",
        )
    if not _has_valid_trace_k(all_traces):
        return _invalid_fusion_evidence(hard_negatives)
    case_by_id = {case.case_id: case for case in case_list}
    case_mode_evidence_complete = _has_complete_case_mode_evidence(
        case_list, all_traces,
    )
    trace_list = sorted(
        (trace for trace in all_traces if trace.get("case_id") in case_by_id),
        key=lambda trace: (trace.get("mode", ""), trace["case_id"]),
    )
    baseline_rankings = [replay_fusion(trace, {}) for trace in trace_list]
    replay_evidence_incomplete = any(
        ranking is None for ranking in baseline_rankings
    )
    usable_baseline_rankings = [ranking or [] for ranking in baseline_rankings]
    baseline_records = [
        (
            trace.get("mode", ""),
            _metrics(case_by_id[trace["case_id"]], trace, ranking),
        )
        for trace, ranking in zip(trace_list, usable_baseline_rankings)
    ]
    baseline_modes = _per_mode(baseline_records)
    hard_negative_evidence_invalid = hard_negative_evidence["invalid"]
    hard_negative_evidence_incomplete = hard_negative_evidence["incomplete"]
    baseline_findings = (
        set().union(
            *[
                _loss_findings(case_by_id[trace["case_id"]], trace, ranking)
                for trace, ranking in zip(trace_list, usable_baseline_rankings)
            ]
        )
        if trace_list
        else set()
    )

    candidates = []
    for weights in fusion_weight_grid():
        rankings = [replay_fusion(trace, weights) for trace in trace_list]
        usable_rankings = [ranking or [] for ranking in rankings]
        records = [
            (
                trace.get("mode", ""),
                _metrics(case_by_id[trace["case_id"]], trace, ranking),
            )
            for trace, ranking in zip(trace_list, usable_rankings)
        ]
        modes = _per_mode(records)
        findings = (
            set().union(
                *[
                    _loss_findings(case_by_id[trace["case_id"]], trace, ranking)
                    for trace, ranking in zip(trace_list, usable_rankings)
                ]
            )
            if trace_list
            else set()
        )
        reasons = _fusion_gate_reasons(
            modes,
            baseline_modes,
            findings,
            baseline_findings,
            trace_list,
            rankings,
            hard_negatives,
            case_mode_evidence_complete=case_mode_evidence_complete,
            replay_evidence_incomplete=replay_evidence_incomplete,
            hard_negative_evidence_invalid=hard_negative_evidence_invalid,
            hard_negative_evidence_incomplete=hard_negative_evidence_incomplete,
        )
        candidates.append(
            {
                "weights": dict(sorted(weights.items())),
                "weights_key": _weights_key(weights),
                "metrics": {
                    "aggregate": _mean_metrics(
                        [metrics for _, metrics in records]
                    ),
                    "modes": modes,
                },
                "passed": not reasons,
                "reasons": reasons,
            }
        )

    candidates.sort(key=lambda item: item["weights_key"])
    if hard_negative_evidence_invalid:
        return {
            "status": "needs_work",
            "reason": "hard_negative_evidence_invalid",
            "weights": None,
            "hard_negatives": hard_negatives,
            "candidates": candidates,
        }
    if not case_mode_evidence_complete:
        return {
            "status": "needs_work",
            "reason": "case_mode_evidence_incomplete",
            "weights": None,
            "hard_negatives": hard_negatives,
            "candidates": candidates,
        }
    if replay_evidence_incomplete:
        return {
            "status": "needs_work",
            "reason": "replay_evidence_incomplete",
            "weights": None,
            "hard_negatives": hard_negatives,
            "candidates": candidates,
        }
    if hard_negative_evidence_incomplete:
        return {
            "status": "needs_work",
            "reason": "hard_negative_evidence_incomplete",
            "weights": None,
            "hard_negatives": hard_negatives,
            "candidates": candidates,
        }
    passing = [item for item in candidates if item["passed"]]
    if not passing:
        return {
            "status": "needs_work",
            "reason": "no_passing_weight_map",
            "weights": None,
            "hard_negatives": hard_negatives,
            "candidates": candidates,
        }
    selected = min(
        passing,
        key=lambda item: (
            -item["metrics"]["aggregate"]["ndcg_at_k"],
            -item["metrics"]["aggregate"]["mrr_at_k"],
            sum(abs(value - 1.0) for value in item["weights"].values()),
            item["weights_key"],
        ),
    )
    return {
        "status": "passed",
        "weights": selected["weights"],
        "weights_key": selected["weights_key"],
        "hard_negatives": hard_negatives,
        "candidates": candidates,
    }


def _candidate_families(candidate: FusionCandidate) -> list[str]:
    if candidate.family != "pair":
        return [candidate.family]
    return sorted(json.loads(component)["family"] for component in candidate.components)


def _candidate_transformations(candidate: FusionCandidate) -> int:
    return sum((
        candidate.rrf_k != 60,
        candidate.direct_multiplier != 1.0,
        candidate.direct_quota != 0,
        candidate.fanout_cap is not None,
    ))


def _evaluate_fusion_candidate(
    candidate: FusionCandidate,
    case_by_id: dict[str, BenchmarkCase],
    trace_list: list[dict],
    baseline_modes: dict[str, dict[str, float]],
    baseline_findings: set[tuple],
    hard_negatives: list[dict],
    *,
    case_mode_evidence_complete: bool,
    replay_evidence_incomplete: bool,
    hard_negative_evidence_invalid: bool,
    hard_negative_evidence_incomplete: bool,
) -> dict:
    rankings = [replay_fusion_candidate(trace, candidate) for trace in trace_list]
    usable_rankings = [ranking or [] for ranking in rankings]
    records = [
        (
            trace.get("mode", ""),
            _metrics(case_by_id[trace["case_id"]], trace, ranking),
        )
        for trace, ranking in zip(trace_list, usable_rankings)
    ]
    modes = _per_mode(records)
    findings = (
        set().union(*[
            _loss_findings(case_by_id[trace["case_id"]], trace, ranking)
            for trace, ranking in zip(trace_list, usable_rankings)
        ])
        if trace_list
        else set()
    )
    reasons = _fusion_gate_reasons(
        modes,
        baseline_modes,
        findings,
        baseline_findings,
        trace_list,
        rankings,
        hard_negatives,
        case_mode_evidence_complete=case_mode_evidence_complete,
        replay_evidence_incomplete=replay_evidence_incomplete,
        hard_negative_evidence_invalid=hard_negative_evidence_invalid,
        hard_negative_evidence_incomplete=hard_negative_evidence_incomplete,
    )
    return {
        "candidate": candidate.payload(),
        "candidate_key": _candidate_key(candidate),
        "families": _candidate_families(candidate),
        "metrics": {
            "aggregate": _mean_metrics([metrics for _, metrics in records]),
            "modes": modes,
        },
        "passed": not reasons,
        "reasons": reasons,
    }


def _candidate_selection_key(record: dict) -> tuple:
    return (
        -record["metrics"]["aggregate"]["ndcg_at_k"],
        -record["metrics"]["aggregate"]["mrr_at_k"],
        _candidate_transformations(FusionCandidate(
            family=record["candidate"]["family"],
            rrf_k=record["candidate"]["rrf_k"],
            direct_multiplier=record["candidate"]["direct_multiplier"],
            direct_quota=record["candidate"]["direct_quota"],
            fanout_cap=record["candidate"]["fanout_cap"],
            components=tuple(record["candidate"]["components"]),
        )),
        record["candidate_key"],
    )


def select_fusion_candidate(cases, traces) -> dict:
    case_list = list(cases)
    all_traces = list(traces)
    stage_a = stage_a_candidates()
    hard_negative_evidence = _hard_negative_evidence(case_list, all_traces)
    hard_negatives = hard_negative_evidence["records"]
    if hard_negative_evidence["invalid"] or not _has_valid_trace_k(all_traces):
        reason = (
            "hard_negative_evidence_invalid"
            if hard_negative_evidence["invalid"]
            else "evidence_invalid"
        )
        records = [
            {
                "candidate": candidate.payload(),
                "candidate_key": _candidate_key(candidate),
                "families": [candidate.family],
                "metrics": {"aggregate": _mean_metrics([]), "modes": {}},
                "passed": False,
                "reasons": [reason],
            }
            for candidate in stage_a
        ]
        return {
            "status": "needs_work",
            "reason": reason,
            "candidate": None,
            "baseline": {"metrics": {"aggregate": _mean_metrics([]), "modes": {}}},
            "hard_negatives": hard_negatives,
            "stage_a": records,
            "family_winners": [],
            "family_rejections": sorted({item.family for item in stage_a}),
            "stage_b": [],
        }

    case_by_id = {case.case_id: case for case in case_list}
    case_mode_evidence_complete = _has_complete_case_mode_evidence(
        case_list, all_traces,
    )
    trace_list = sorted(
        (trace for trace in all_traces if trace.get("case_id") in case_by_id),
        key=lambda trace: (trace.get("mode", ""), trace["case_id"]),
    )
    baseline_candidate = FusionCandidate()
    baseline_rankings = [
        replay_fusion_candidate(trace, baseline_candidate)
        for trace in trace_list
    ]
    replay_evidence_incomplete = any(
        ranking is None for ranking in baseline_rankings
    )
    usable_baseline_rankings = [ranking or [] for ranking in baseline_rankings]
    baseline_records = [
        (
            trace.get("mode", ""),
            _metrics(case_by_id[trace["case_id"]], trace, ranking),
        )
        for trace, ranking in zip(trace_list, usable_baseline_rankings)
    ]
    baseline_metrics = {
        "aggregate": _mean_metrics([metrics for _, metrics in baseline_records]),
        "modes": _per_mode(baseline_records),
    }
    hard_negative_evidence_invalid = hard_negative_evidence["invalid"]
    hard_negative_evidence_incomplete = hard_negative_evidence["incomplete"]
    baseline_findings = (
        set().union(*[
            _loss_findings(case_by_id[trace["case_id"]], trace, ranking)
            for trace, ranking in zip(trace_list, usable_baseline_rankings)
        ])
        if trace_list
        else set()
    )

    evaluation_args = {
        "case_mode_evidence_complete": case_mode_evidence_complete,
        "replay_evidence_incomplete": replay_evidence_incomplete,
        "hard_negative_evidence_invalid": hard_negative_evidence_invalid,
        "hard_negative_evidence_incomplete": hard_negative_evidence_incomplete,
    }
    stage_a_records = [
        _evaluate_fusion_candidate(
            candidate,
            case_by_id,
            trace_list,
            baseline_metrics["modes"],
            baseline_findings,
            hard_negatives,
            **evaluation_args,
        )
        for candidate in stage_a
    ]
    stage_a_records.sort(key=lambda item: item["candidate_key"])
    winners = []
    family_rejections = []
    for family in sorted({candidate.family for candidate in stage_a}):
        passing = [
            item for item in stage_a_records
            if item["candidate"]["family"] == family and item["passed"]
        ]
        if not passing:
            family_rejections.append(family)
            continue
        winners.append(min(passing, key=_candidate_selection_key))
    winners.sort(key=lambda item: item["candidate_key"])
    stage_b_records = [
        _evaluate_fusion_candidate(
            candidate,
            case_by_id,
            trace_list,
            baseline_metrics["modes"],
            baseline_findings,
            hard_negatives,
            **evaluation_args,
        )
        for candidate in stage_b_candidates([
            FusionCandidate(
                family=item["candidate"]["family"],
                rrf_k=item["candidate"]["rrf_k"],
                direct_multiplier=item["candidate"]["direct_multiplier"],
                direct_quota=item["candidate"]["direct_quota"],
                fanout_cap=item["candidate"]["fanout_cap"],
                components=tuple(item["candidate"]["components"]),
            )
            for item in winners
        ])
    ]
    stage_b_records.sort(key=lambda item: item["candidate_key"])
    invalid_reason = next((reason for reason, failed in (
        ("hard_negative_evidence_invalid", hard_negative_evidence_invalid),
        ("case_mode_evidence_incomplete", not case_mode_evidence_complete),
        ("replay_evidence_incomplete", replay_evidence_incomplete),
        ("hard_negative_evidence_incomplete", hard_negative_evidence_incomplete),
    ) if failed), None)
    passing = [
        item for item in [*stage_a_records, *stage_b_records]
        if item["passed"]
    ]
    selected = min(passing, key=_candidate_selection_key) if passing else None
    return {
        "status": "passed" if selected is not None and invalid_reason is None else "needs_work",
        "reason": invalid_reason or (
            None if selected is not None else "no_passing_fusion_candidate"
        ),
        "candidate": selected["candidate"] if selected is not None and invalid_reason is None else None,
        "baseline": {"metrics": baseline_metrics},
        "hard_negatives": hard_negatives,
        "stage_a": stage_a_records,
        "family_winners": winners,
        "family_rejections": family_rejections,
        "stage_b": stage_b_records,
    }


def _finding_keys(
    cases: dict[str, BenchmarkCase],
    traces: list[dict],
) -> set[tuple]:
    keys = set()
    for trace in traces:
        case = cases.get(trace.get("case_id"))
        if case is None:
            continue
        for finding in analyze_trace(case, trace):
            if (
                finding["class"] == "rerank_worsened_order"
                or finding["class"].startswith("missing_")
            ):
                keys.add(
                    (finding["case_id"], finding["class"], finding["identity"])
                )
    return keys


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def _rerank_latency_samples(traces: list[dict]) -> tuple[list[float], bool]:
    samples = []
    for trace in traces:
        latency = trace.get("latency")
        if not isinstance(latency, dict) or "rerank_ms" not in latency:
            return [], False
        sample = latency["rerank_ms"]
        if isinstance(sample, bool) or not isinstance(sample, (int, float)):
            return [], False
        sample = float(sample)
        if not math.isfinite(sample) or sample < 0.0:
            return [], False
        samples.append(sample)
    return samples, True


def _has_valid_rerank_measurements(traces: list[dict]) -> bool:
    return all(
        trace.get("status") == "passed"
        and trace.get("sample_id") is not None
        for trace in traces
    )


def _case_mode_samples(traces: list[dict]) -> Counter[tuple[str, str, object]]:
    counts: dict[tuple[str, str], int] = {}
    samples = Counter()
    for trace in traces:
        case_mode = (trace.get("case_id", ""), trace.get("mode", ""))
        sample_id = trace.get("sample_id")
        if sample_id is None:
            sample_id = counts.get(case_mode, 0)
            counts[case_mode] = sample_id + 1
        samples[(*case_mode, sample_id)] += 1
    return samples


def _has_duplicate_measured_sample(traces: list[dict]) -> bool:
    return any(count > 1 for count in _case_mode_samples(traces).values())


def _has_valid_rerank_case_mode_evidence(case_by_id, traces: list[dict]) -> bool:
    return all(
        trace.get("case_id") in case_by_id and trace.get("mode") == "hybrid"
        for trace in traces
    )


def select_rerank_batch(cases, batch_runs) -> dict:
    case_by_id = {case.case_id: case for case in cases}
    runs = {
        int(batch): sorted(
            list(traces),
            key=lambda trace: (
                trace.get("mode", ""),
                trace.get("case_id", ""),
            ),
        )
        for batch, traces in batch_runs.items()
    }
    baseline = runs.get(32, [])
    latency_samples = {
        batch: _rerank_latency_samples(traces)
        for batch, traces in runs.items()
    }
    latency_evidence_valid = all(valid for _, valid in latency_samples.values())
    measurement_evidence_valid = all(
        _has_valid_rerank_measurements(traces)
        for traces in runs.values()
    )
    rerank_evidence_valid = (
        latency_evidence_valid and measurement_evidence_valid
    )
    case_mode_evidence_valid = all(
        _has_valid_rerank_case_mode_evidence(case_by_id, traces)
        for traces in runs.values()
    )
    duplicate_measured_sample = any(
        _has_duplicate_measured_sample(traces)
        for traces in runs.values()
    )
    baseline_records = [
        (
            trace.get("mode", ""),
            _metrics(
                case_by_id[trace["case_id"]],
                trace,
                list(trace.get("metrics_input", {}).get("ranking", [])),
            ),
        )
        for trace in baseline
        if trace.get("case_id") in case_by_id
    ]
    baseline_modes = _per_mode(baseline_records)
    baseline_findings = _finding_keys(case_by_id, baseline)
    baseline_samples = _case_mode_samples(baseline)
    baseline_p95 = (
        _p95(latency_samples.get(32, ([], True))[0])
        if rerank_evidence_valid
        else None
    )

    candidates = []
    for batch in RERANK_BATCHES:
        traces = runs.get(batch, [])
        records = [
            (
                trace.get("mode", ""),
                _metrics(
                    case_by_id[trace["case_id"]],
                    trace,
                    list(trace.get("metrics_input", {}).get("ranking", [])),
                ),
            )
            for trace in traces
            if trace.get("case_id") in case_by_id
        ]
        modes = _per_mode(records)
        p95 = (
            _p95(latency_samples.get(batch, ([], True))[0])
            if rerank_evidence_valid
            else None
        )
        improvement = (
            (baseline_p95 - p95) / baseline_p95 * 100.0
            if baseline_p95 is not None and p95 is not None and baseline_p95
            else 0.0
        )
        reasons = []
        if not rerank_evidence_valid:
            reasons.append("invalid_rerank_evidence")
        if not case_mode_evidence_valid:
            reasons.append("case_mode_sample_mismatch")
        if duplicate_measured_sample:
            reasons.append("duplicate_measured_sample")
        if batch != 32:
            if _case_mode_samples(traces) != baseline_samples:
                reasons.append("case_mode_sample_mismatch")
            if set(modes) != set(baseline_modes):
                reasons.append("missing_baseline_mode")
            if any(
                modes[mode]["recall_at_k"]
                < baseline_modes.get(mode, {}).get("recall_at_k", 0.0)
                for mode in modes
            ):
                reasons.append("recall_regression")
            if any(
                modes[mode]["intent_coverage_at_k"]
                < baseline_modes.get(mode, {}).get("intent_coverage_at_k", 0.0)
                for mode in modes
            ):
                reasons.append("intent_coverage_regression")
            if any(
                modes[mode]["ndcg_at_k"]
                < baseline_modes.get(mode, {}).get("ndcg_at_k", 0.0) - 0.01
                for mode in modes
            ):
                reasons.append("ndcg_loss_exceeds_limit")
            if _finding_keys(case_by_id, traces) - baseline_findings:
                reasons.append("new_rerank_or_missing_candidate_finding")
            if improvement < 25.0:
                reasons.append("p95_improvement_below_25_percent")
        candidates.append(
            {
                "batch": batch,
                "metrics": {
                    "aggregate": _mean_metrics([value for _, value in records]),
                    "modes": modes,
                },
                "p95_rerank_ms": round(p95, 6) if p95 is not None else None,
                "p95_improvement_percent": round(improvement, 6),
                "passed": not reasons,
                "reasons": sorted(reasons),
            }
        )

    if duplicate_measured_sample:
        return {
            "status": "needs_work",
            "batch": None,
            "reason": "duplicate_measured_sample",
            "candidates": candidates,
        }
    for candidate in candidates:
        if candidate["batch"] in (16, 24) and candidate["passed"]:
            return {
                "status": "passed",
                "batch": candidate["batch"],
                "candidates": candidates,
            }
    reason = (
        "invalid_rerank_evidence"
        if not rerank_evidence_valid
        else "latency_gate_unresolved"
    )
    return {
        "status": "needs_work",
        "batch": 32,
        "reason": reason,
        "candidates": candidates,
    }
