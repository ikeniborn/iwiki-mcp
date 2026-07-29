from __future__ import annotations

from dataclasses import asdict
from dataclasses import is_dataclass
from dataclasses import replace
from datetime import datetime
from datetime import timezone
import math
from statistics import median
from typing import Iterable

from iwiki_mcp.base import resolve_binding
from iwiki_mcp.engine.config import Config

from .analyzer import analyze_trace
from .analyzer import ranked_backlog
from .envfile import safe_config_fingerprint
from .fixtures import BenchmarkCase
from .instrumentation import trace_query
from .metrics import intent_coverage_at_k
from .metrics import mrr_at_k
from .metrics import ndcg_at_k
from .metrics import recall_at_k
from .report import sanitize_evidence
from .selection import _confirmed_loss_records
from .selection import _fusion_gate_reasons
from .selection import _has_complete_case_mode_evidence
from .selection import _loss_findings
from .selection import _metrics
from .selection import _per_mode
from .selection import FusionCandidate
from .selection import RERANK_BATCHES
from .selection import select_fusion_candidate
from .selection import select_fusion_weights
from .selection import select_rerank_batch


_ROLLUP_FIELDS = (
    "recall_at_k",
    "mrr_at_k",
    "ndcg_at_k",
    "intent_coverage_at_k",
    "latency_ms",
)

_SAFE_CONFIG_FIELDS = {
    "bfs_top_k",
    "chat_model",
    "chunk_overlap",
    "chunk_size",
    "dimensions",
    "embed_model",
    "graph_depth",
    "rerank_enabled",
    "rerank_model",
    "score_threshold",
    "search_mode",
    "seed_threshold",
    "seed_top_k",
    "summary_max",
    "top_k",
    "write_seed_threshold",
}


def summarize_trace(case: BenchmarkCase, trace: dict) -> dict:
    metrics_input = trace.get("metrics_input", {})
    metric_case = replace(
        case,
        relevant=dict(metrics_input.get("relevant", case.relevant)),
        intents={
            name: list(values)
            for name, values in metrics_input.get("intents", case.intents).items()
        },
    )
    ranking = list(metrics_input.get("ranking", []))
    k = int(trace.get("k", case.k))
    return {
        "case_id": trace.get("case_id", case.case_id),
        "mode": trace.get("mode"),
        "k": k,
        "recall_at_k": recall_at_k(ranking, metric_case, k),
        "mrr_at_k": mrr_at_k(ranking, metric_case, k),
        "ndcg_at_k": ndcg_at_k(ranking, metric_case, k),
        "intent_coverage_at_k": intent_coverage_at_k(ranking, metric_case, k),
        "latency_ms": round(trace.get("latency", {}).get("total_ms", 0.0), 3),
    }


def _rollup(summaries: list[dict]) -> dict:
    if not summaries:
        return {
            "case_count": 0,
            **{field: 0.0 for field in _ROLLUP_FIELDS},
        }
    count = len(summaries)
    rollup = {"case_count": count}
    for field in _ROLLUP_FIELDS:
        rollup[field] = round(
            sum(float(summary.get(field, 0.0)) for summary in summaries) / count,
            6,
        )
    return rollup


def _case_payload(case: BenchmarkCase) -> dict:
    if is_dataclass(case):
        return asdict(case)
    return dict(case)


def _config_payload(cfg) -> dict:
    if isinstance(cfg, Config):
        return safe_config_fingerprint(cfg)
    if isinstance(cfg, dict):
        return {
            key: cfg[key]
            for key in sorted(cfg)
            if key in _SAFE_CONFIG_FIELDS and _is_json_scalar(cfg[key])
        }
    return {"type": type(cfg).__name__}


def _is_json_scalar(value) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _safe_error_type(exc: Exception) -> str:
    name = type(exc).__name__
    safe = "".join(char for char in name if char.isalnum() or char == "_")
    return safe or "Exception"


def _safe_error_category(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        return "invalid_query_input"
    if "legacy store layout" in str(exc).lower():
        return "legacy_store_layout"
    return "query_runtime_error"


def _safe_error_payload(exc: Exception) -> dict:
    category = _safe_error_category(exc)
    messages = {
        "invalid_query_input": "query input is invalid for benchmark execution",
        "legacy_store_layout": "legacy store layout requires migration before benchmark",
        "query_runtime_error": "query failed during benchmark execution",
    }
    return {
        "type": _safe_error_type(exc),
        "category": category,
        "message": messages[category],
    }


def _failed_trace(case: BenchmarkCase, mode: str, exc: Exception) -> dict:
    return {
        "case_id": case.case_id,
        "domain": case.domain,
        "mode": mode,
        "k": case.k,
        "status": "failed",
        "error": _safe_error_payload(exc),
    }


def _failed_finding(trace: dict) -> dict:
    error = trace["error"]
    return {
        "case_id": trace["case_id"],
        "class": "query_failed",
        "severity": "high",
        "mode": trace["mode"],
        "evidence": {
            "domain": trace["domain"],
            "error_type": error["type"],
            "error_category": error["category"],
        },
    }


def run_offline_traces(
    cfg,
    base: str,
    cases: Iterable[BenchmarkCase],
    modes: Iterable[str],
) -> dict:
    traces = []
    summaries = []
    findings = []
    case_list = sorted(list(cases), key=lambda item: item.case_id)
    mode_list = list(modes)

    for case in case_list:
        for mode in mode_list:
            trace = trace_query(
                cfg,
                base,
                case,
                mode=mode,
                rerank_enabled=False,
            )
            traces.append(trace)
            summaries.append(summarize_trace(case, trace))
            findings.extend(
                {**finding, "mode": mode}
                for finding in analyze_trace(case, trace)
            )

    return {
        "kind": "offline",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_settings": {
            "rerank_enabled": False,
        },
        "config": _config_payload(cfg),
        "modes": mode_list,
        "cases": [_case_payload(case) for case in case_list],
        "summary": {
            "rollup": _rollup(summaries),
            "cases": summaries,
        },
        "traces": traces,
        "findings": findings,
        "backlog": ranked_backlog(findings),
    }


def run_live_traces(
    cfg,
    domain: str,
    modes: Iterable[str],
    cases: Iterable[BenchmarkCase],
    *,
    base: str | None = None,
    latency_ceiling_ms: float | None = None,
    rerank_enabled: bool | None = None,
    fusion_weights: dict[str, float] | None = None,
    fusion_candidate: FusionCandidate | None = None,
    rerank_candidate_limit: int | None = None,
    sample_id: str | None = None,
) -> dict:
    wiki_base = base if base is not None else resolve_binding().base
    traces = []
    summaries = []
    findings = []
    case_list = sorted(
        [case for case in cases if case.domain == domain],
        key=lambda item: item.case_id,
    )
    mode_list = list(modes)
    effective_rerank_enabled = (
        bool(getattr(cfg, "rerank_model", ""))
        if rerank_enabled is None
        else rerank_enabled
    )

    for case in case_list:
        for mode in mode_list:
            try:
                trace_kwargs = {}
                if fusion_weights is not None:
                    trace_kwargs["fusion_weights"] = fusion_weights
                if fusion_candidate is not None:
                    trace_kwargs["fusion_candidate"] = fusion_candidate
                if rerank_candidate_limit is not None:
                    trace_kwargs["rerank_candidate_limit"] = rerank_candidate_limit
                trace = trace_query(
                    cfg,
                    wiki_base,
                    case,
                    mode=mode,
                    rerank_enabled=effective_rerank_enabled,
                    **trace_kwargs,
                )
            except Exception as exc:
                trace = _failed_trace(case, mode, exc)
                if sample_id is not None:
                    trace["sample_id"] = sample_id
                traces.append(trace)
                findings.append(_failed_finding(trace))
                continue
            trace = {**trace, "status": trace.get("status", "passed")}
            if sample_id is not None:
                trace["sample_id"] = sample_id
            traces.append(trace)
            summaries.append(summarize_trace(case, trace))
            findings.extend(
                {**finding, "mode": mode}
                for finding in analyze_trace(case, trace)
            )

    run_settings = {
        "domain": domain,
        "rerank_enabled": effective_rerank_enabled,
    }
    if latency_ceiling_ms is not None:
        run_settings["latency_ceiling_ms"] = latency_ceiling_ms

    return {
        "kind": "live",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "domain": domain,
        "run_settings": run_settings,
        "config": _config_payload(cfg),
        "modes": mode_list,
        "cases": [_case_payload(case) for case in case_list],
        "summary": {
            "rollup": _rollup(summaries),
            "cases": summaries,
        },
        "traces": traces,
        "findings": findings,
        "backlog": ranked_backlog(findings),
    }


def _candidate_from_payload(payload: dict | None) -> FusionCandidate | None:
    if not isinstance(payload, dict):
        return None
    try:
        return FusionCandidate(
            family=payload["family"],
            rrf_k=payload["rrf_k"],
            direct_multiplier=payload["direct_multiplier"],
            direct_quota=payload["direct_quota"],
            fanout_cap=payload["fanout_cap"],
            components=tuple(payload["components"]),
        )
    except (KeyError, TypeError):
        return None


def _live_fusion_gate(cases, baseline: dict, confirmation: dict) -> list[str]:
    case_list = list(cases)
    case_by_id = {case.case_id: case for case in case_list}
    baseline_traces = sorted(
        baseline["traces"], key=lambda trace: (trace.get("mode", ""), trace["case_id"]),
    )
    confirmation_traces = sorted(
        confirmation["traces"],
        key=lambda trace: (trace.get("mode", ""), trace["case_id"]),
    )
    baseline_complete = _has_complete_case_mode_evidence(case_list, baseline_traces)
    confirmation_complete = _has_complete_case_mode_evidence(
        case_list, confirmation_traces,
    )
    failed = any(
        trace.get("status", "passed") != "passed"
        for trace in [*baseline_traces, *confirmation_traces]
    )
    if not baseline_complete or not confirmation_complete or failed:
        return ["live_evidence_incomplete"]

    baseline_rankings = [
        list(trace.get("metrics_input", {}).get("ranking", []))
        for trace in baseline_traces
    ]
    confirmation_rankings = [
        list(trace.get("metrics_input", {}).get("ranking", []))
        for trace in confirmation_traces
    ]
    baseline_records = [
        (
            trace.get("mode", ""),
            _metrics(case_by_id[trace["case_id"]], trace, ranking),
        )
        for trace, ranking in zip(baseline_traces, baseline_rankings)
    ]
    confirmation_records = [
        (
            trace.get("mode", ""),
            _metrics(case_by_id[trace["case_id"]], trace, ranking),
        )
        for trace, ranking in zip(confirmation_traces, confirmation_rankings)
    ]
    confirmed_losses = set().union(*[
        _confirmed_loss_records(case_by_id[trace["case_id"]], trace)
        for trace in baseline_traces
    ]) if baseline_traces else set()
    baseline_findings = set().union(*[
        _loss_findings(case_by_id[trace["case_id"]], trace, ranking)
        for trace, ranking in zip(baseline_traces, baseline_rankings)
    ]) if baseline_traces else set()
    confirmation_findings = set().union(*[
        _loss_findings(case_by_id[trace["case_id"]], trace, ranking)
        for trace, ranking in zip(confirmation_traces, confirmation_rankings)
    ]) if confirmation_traces else set()
    confirmed_loss_evidence_incomplete = (
        not {22, 26}.issubset({record[2] for record in confirmed_losses})
        or len(confirmed_losses) < 2
    )
    return _fusion_gate_reasons(
        _per_mode(confirmation_records),
        _per_mode(baseline_records),
        confirmation_findings,
        baseline_findings,
        confirmation_traces,
        confirmation_rankings,
        confirmed_losses,
        case_mode_evidence_complete=True,
        replay_evidence_incomplete=False,
        confirmed_loss_evidence_incomplete=confirmed_loss_evidence_incomplete,
    )


def run_bounded_fusion_experiment(
    cfg,
    domain: str,
    cases: Iterable[BenchmarkCase],
    *,
    base: str | None = None,
) -> dict:
    case_list = sorted(
        [case for case in cases if case.domain == domain],
        key=lambda case: case.case_id,
    )
    modes = ["hybrid", "lexical", "semantic"]
    baseline = run_live_traces(
        cfg,
        domain,
        modes,
        case_list,
        base=base,
        rerank_enabled=False,
    )
    selection = select_fusion_candidate(case_list, baseline["traces"])
    safe_baseline = {
        **baseline,
        "traces": [_safe_pareto_trace(trace) for trace in baseline["traces"]],
        "cases": _safe_pareto_cases(case_list),
    }
    evidence = {
        "kind": "live-bounded-fusion",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "domain": domain,
        "run_settings": {"rerank_enabled": False},
        "config": _config_payload(cfg),
        "cases": _safe_pareto_cases(case_list),
        "baseline": safe_baseline,
        "fusion_selection": selection,
    }
    candidate = _candidate_from_payload(selection.get("candidate"))
    if selection.get("status") != "passed" or candidate is None:
        evidence["decision"] = {
            "status": "needs_work",
            "reason": selection.get("reason", "fusion_selection_failed"),
            "candidate": None,
        }
        return evidence

    confirmation = run_live_traces(
        cfg,
        domain,
        modes,
        case_list,
        base=base,
        rerank_enabled=False,
        fusion_candidate=candidate,
    )
    safe_confirmation = {
        **confirmation,
        "traces": [_safe_pareto_trace(trace) for trace in confirmation["traces"]],
        "cases": _safe_pareto_cases(case_list),
    }
    reasons = _live_fusion_gate(case_list, baseline, confirmation)
    evidence["live_confirmation"] = safe_confirmation
    evidence["decision"] = {
        "status": "validated_candidate" if not reasons else "needs_work",
        "reason": None if not reasons else reasons[0],
        "candidate": candidate.payload(),
    }
    return evidence


def run_bounded_fusion_replay(
    domain: str,
    cases: Iterable[BenchmarkCase],
    traces: Iterable[dict],
) -> dict:
    case_list = sorted(
        [case for case in cases if case.domain == domain],
        key=lambda case: case.case_id,
    )
    trace_list = sanitize_evidence({"traces": list(traces)})["traces"]
    selection = select_fusion_candidate(case_list, trace_list)
    case_by_id = {case.case_id: case for case in case_list}
    summaries = [
        summarize_trace(case_by_id[trace["case_id"]], trace)
        for trace in trace_list
        if (
            trace.get("status", "passed") == "passed"
            and trace.get("case_id") in case_by_id
            and isinstance(trace.get("k"), int)
            and not isinstance(trace.get("k"), bool)
            and trace["k"] > 0
        )
    ]
    baseline = {
        "kind": "replay-baseline",
        "run_settings": {"rerank_enabled": False},
        "cases": _safe_pareto_cases(case_list),
        "summary": {"rollup": _rollup(summaries), "cases": summaries},
        "traces": trace_list,
        "findings": [],
        "backlog": [],
    }
    selected = selection.get("candidate")
    if selection.get("status") == "passed" and selected is not None:
        decision = {
            "status": "pending_live_confirmation",
            "reason": "replay_candidate_selected",
            "candidate": selected,
        }
    else:
        decision = {
            "status": "needs_work",
            "reason": selection.get("reason", "fusion_selection_failed"),
            "candidate": None,
        }
    return {
        "kind": "replay-bounded-fusion",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "domain": domain,
        "run_settings": {"rerank_enabled": False, "replay_only": True},
        "cases": _safe_pareto_cases(case_list),
        "baseline": baseline,
        "fusion_selection": selection,
        "live_confirmation": {"status": "not_run", "reason": "replay_only"},
        "summary": baseline["summary"],
        "traces": trace_list,
        "findings": [],
        "backlog": [],
        "decision": decision,
    }


def _safe_pareto_trace(trace: dict) -> dict:
    return {key: value for key, value in trace.items() if key != "query"}


def _safe_pareto_cases(cases: Iterable[BenchmarkCase]) -> list[dict]:
    return [
        {
            "case_id": case.case_id,
            "domain": case.domain,
            "query_class": case.query_class,
            "k": case.k,
            "relevant": dict(case.relevant),
            "intents": {
                name: list(values) for name, values in case.intents.items()
            },
        }
        for case in cases
    ]


def _batch_evidence(run: dict) -> dict:
    traces = run["traces"]
    rerank_samples = []
    for trace in traces:
        latency = trace.get("latency")
        sample = latency.get("rerank_ms") if isinstance(latency, dict) else None
        if (
            trace.get("status") != "passed"
            or trace.get("sample_id") is None
            or isinstance(sample, bool)
            or not isinstance(sample, (int, float))
            or not math.isfinite(sample)
            or sample < 0.0
        ):
            rerank_samples = None
            break
        rerank_samples.append(float(sample))
    return {
        "sample_count": len(traces),
        "p50_rerank_ms": (
            round(median(rerank_samples), 6)
            if rerank_samples is not None and rerank_samples
            else None
        ),
        "p95_rerank_ms": (
            sorted(rerank_samples)[
                max(0, int(len(rerank_samples) * 0.95 + 0.999999) - 1)
            ]
            if rerank_samples is not None and rerank_samples
            else None
        ),
        "summary": run["summary"],
        "findings": run["findings"],
        "traces": [_safe_pareto_trace(trace) for trace in traces],
    }


def run_pareto_experiment(
    cfg,
    domain: str,
    cases: Iterable[BenchmarkCase],
    *,
    base: str | None = None,
) -> dict:
    case_list = sorted(
        [case for case in cases if case.domain == domain],
        key=lambda case: case.case_id,
    )
    modes = ["hybrid", "lexical", "semantic"]
    baseline = run_live_traces(
        cfg,
        domain,
        modes,
        case_list,
        base=base,
        rerank_enabled=False,
    )
    fusion_selection = select_fusion_weights(case_list, baseline["traces"])
    safe_baseline = {
        **baseline,
        "traces": [_safe_pareto_trace(trace) for trace in baseline["traces"]],
        "cases": _safe_pareto_cases(case_list),
    }
    evidence = {
        "kind": "live-pareto",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "domain": domain,
        "run_settings": {
            "warmup_passes": 1,
            "measured_passes": 2,
            "baseline_rerank_enabled": False,
        },
        "config": _config_payload(cfg),
        "cases": _safe_pareto_cases(case_list),
        "baseline": safe_baseline,
        "fusion_selection": fusion_selection,
        "summary": safe_baseline["summary"],
        "traces": safe_baseline["traces"],
        "findings": safe_baseline["findings"],
        "backlog": safe_baseline["backlog"],
    }
    if fusion_selection.get("status") != "passed":
        evidence["decision"] = {
            "status": "needs_work",
            "reason": fusion_selection.get("reason", "fusion_selection_failed"),
        }
        return evidence

    weights = fusion_selection["weights"]
    batch_runs = {}
    batch_evidence = {}
    all_findings = list(safe_baseline["findings"])
    for batch in RERANK_BATCHES:
        run_live_traces(
            cfg,
            domain,
            ["hybrid"],
            case_list,
            base=base,
            rerank_enabled=True,
            fusion_weights=weights,
            rerank_candidate_limit=batch,
            sample_id=f"batch-{batch}-warmup",
        )
        measured_traces = []
        measured_summaries = []
        measured_findings = []
        for pass_index in range(2):
            measured = run_live_traces(
                cfg,
                domain,
                ["hybrid"],
                case_list,
                base=base,
                rerank_enabled=True,
                fusion_weights=weights,
                rerank_candidate_limit=batch,
                sample_id=f"batch-{batch}-pass-{pass_index + 1}",
            )
            measured_traces.extend(measured["traces"])
            measured_summaries.extend(measured["summary"]["cases"])
            measured_findings.extend(measured["findings"])
        batch_run = {
            "traces": measured_traces,
            "summary": {
                "rollup": _rollup(measured_summaries),
                "cases": measured_summaries,
            },
            "findings": measured_findings,
        }
        batch_runs[batch] = measured_traces
        batch_evidence[str(batch)] = _batch_evidence(batch_run)
        all_findings.extend(measured_findings)

    decision = select_rerank_batch(case_list, batch_runs)
    evidence.update({
        "rerank_batches": batch_evidence,
        "decision": decision,
        "findings": all_findings,
        "backlog": ranked_backlog(all_findings),
    })
    return evidence
