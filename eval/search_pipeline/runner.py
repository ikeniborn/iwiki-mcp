from __future__ import annotations

from dataclasses import asdict, is_dataclass, replace
from datetime import datetime, timezone
from typing import Iterable

from iwiki_mcp.base import resolve_binding
from iwiki_mcp.engine.config import Config

from .analyzer import analyze_trace, ranked_backlog
from .envfile import safe_config_fingerprint
from .fixtures import BenchmarkCase
from .instrumentation import trace_query
from .metrics import (
    intent_coverage_at_k,
    mrr_at_k,
    ndcg_at_k,
    recall_at_k,
)


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
    rerank_enabled = bool(getattr(cfg, "rerank_model", ""))

    for case in case_list:
        for mode in mode_list:
            try:
                trace = trace_query(
                    cfg,
                    wiki_base,
                    case,
                    mode=mode,
                    rerank_enabled=rerank_enabled,
                )
            except Exception as exc:
                trace = _failed_trace(case, mode, exc)
                traces.append(trace)
                findings.append(_failed_finding(trace))
                continue
            trace = {**trace, "status": trace.get("status", "passed")}
            traces.append(trace)
            summaries.append(summarize_trace(case, trace))
            findings.extend(
                {**finding, "mode": mode}
                for finding in analyze_trace(case, trace)
            )

    run_settings = {
        "domain": domain,
        "rerank_enabled": rerank_enabled,
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
