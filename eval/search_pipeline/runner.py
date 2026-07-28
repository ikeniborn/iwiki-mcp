from __future__ import annotations

from dataclasses import asdict, is_dataclass, replace
from datetime import datetime, timezone
from typing import Iterable

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
    return {"repr": repr(cfg)}


def _is_json_scalar(value) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


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
            findings.extend(analyze_trace(case, trace))

    return {
        "kind": "offline",
        "timestamp": datetime.now(timezone.utc).isoformat(),
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
