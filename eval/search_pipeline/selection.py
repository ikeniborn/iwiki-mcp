from __future__ import annotations

from collections import Counter
from dataclasses import replace
import json
import math

from iwiki_mcp.engine.fusion import fuse_ranked

from .analyzer import analyze_trace
from .fixtures import BenchmarkCase
from .metrics import intent_coverage_at_k
from .metrics import mrr_at_k
from .metrics import ndcg_at_k
from .metrics import recall_at_k


PAGE_WEIGHTS = (0.025, 0.05, 0.1)
GRAPH_WEIGHTS = (0.01, 0.025, 0.05)
RERANK_BATCHES = (16, 24, 32)
_CONFIRMED_LOSS_RANKS = (22, 26)
_FINAL_RECOVERY_K = 8
_REQUIRED_FUSION_MODES = ("hybrid", "lexical", "semantic")
_RANKED_HIT_FIELDS = ("domain", "file", "heading", "chunk", "ordinal")


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
    ranked = trace.get("stages", {}).get("signals", {}).get("ranked")
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
    stages = dict(trace.get("stages", {}))
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


def _confirmed_loss_records(
    case: BenchmarkCase,
    trace: dict,
) -> set[tuple[str, str, int, str]]:
    return {
        (
            finding["case_id"],
            trace.get("mode", ""),
            finding["evidence"]["candidate_rank"],
            finding["identity"],
        )
        for finding in analyze_trace(case, trace)
        if (
            finding["class"] == "lost_after_fusion_topk"
            and finding["evidence"].get("candidate_rank")
            in _CONFIRMED_LOSS_RANKS
        )
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


def _invalid_fusion_evidence() -> dict:
    candidates = [
        {
            "weights": dict(sorted(weights.items())),
            "weights_key": _weights_key(weights),
            "metrics": {"aggregate": _mean_metrics([]), "modes": {}},
            "passed": False,
            "reasons": ["evidence_invalid"],
        }
        for weights in fusion_weight_grid()
    ]
    candidates.sort(key=lambda item: item["weights_key"])
    return {
        "status": "needs_work",
        "reason": "evidence_invalid",
        "weights": None,
        "candidates": candidates,
    }


def select_fusion_weights(cases, traces) -> dict:
    case_list = list(cases)
    all_traces = list(traces)
    if not _has_valid_trace_k(all_traces):
        return _invalid_fusion_evidence()
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
    confirmed_loss_records = [
        _confirmed_loss_records(case_by_id[trace["case_id"]], trace)
        for trace in trace_list
    ]
    confirmed_losses = set().union(*confirmed_loss_records) if trace_list else set()
    evidence_incomplete = (
        not set(_CONFIRMED_LOSS_RANKS).issubset(
            {record[2] for record in confirmed_losses}
        )
        or len(confirmed_losses) < len(_CONFIRMED_LOSS_RANKS)
    )
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
        reasons = []
        if not case_mode_evidence_complete:
            reasons.append("case_mode_evidence_incomplete")
        if replay_evidence_incomplete or any(ranking is None for ranking in rankings):
            reasons.append("replay_evidence_incomplete")
        if evidence_incomplete:
            reasons.append("confirmed_loss_evidence_incomplete")
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
        if findings - baseline_findings:
            reasons.append("new_lost_after_fusion_top_k")
        for trace, ranking in zip(trace_list, usable_rankings):
            confirmed = {
                record[3]
                for record in confirmed_losses
                if record[:2] == (trace["case_id"], trace.get("mode", ""))
            }
            if confirmed - set(ranking[:_FINAL_RECOVERY_K]):
                reasons.append("confirmed_loss_not_recovered")
                break
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
                "reasons": sorted(set(reasons)),
            }
        )

    candidates.sort(key=lambda item: item["weights_key"])
    if not case_mode_evidence_complete:
        return {
            "status": "needs_work",
            "reason": "case_mode_evidence_incomplete",
            "weights": None,
            "candidates": candidates,
        }
    if replay_evidence_incomplete:
        return {
            "status": "needs_work",
            "reason": "replay_evidence_incomplete",
            "weights": None,
            "candidates": candidates,
        }
    if evidence_incomplete:
        return {
            "status": "needs_work",
            "reason": "confirmed_loss_evidence_incomplete",
            "weights": None,
            "candidates": candidates,
        }
    passing = [item for item in candidates if item["passed"]]
    if not passing:
        return {
            "status": "needs_work",
            "reason": "no_passing_weight_map",
            "weights": None,
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
        "candidates": candidates,
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
