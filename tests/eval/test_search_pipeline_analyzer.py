from eval.search_pipeline.analyzer import analyze_trace, ranked_backlog
from eval.search_pipeline.fixtures import BenchmarkCase


def _case(*, relevant=None, k=2):
    return BenchmarkCase(
        case_id="case-a",
        domain="eval",
        query="needle",
        relevant=relevant or {"eval/guide/auth.md#Rotation:0": 3},
        k=k,
    )


def _trace(
    *,
    ranking=None,
    relevant=None,
    candidates=None,
    hydrated=None,
    hydration_requested=0,
    rerank=None,
):
    ranking = ranking if ranking is not None else []
    relevant = relevant or {"eval/guide/auth.md#Rotation:0": 3}
    candidates = candidates if candidates is not None else []
    hydrated = hydrated if hydrated is not None else candidates
    return {
        "metrics_input": {
            "ranking": ranking,
            "relevant": relevant,
        },
        "stages": {
            "fusion": {
                "candidate_identities": candidates,
            },
            "hydration": {
                "requested": hydration_requested,
                "hydrated": len(hydrated),
                "dropped": max(hydration_requested - len(hydrated), 0),
                "hydrated_identities": hydrated,
            },
            "rerank": rerank or {"applied": False},
        },
    }


def test_analyze_trace_reports_missing_relevant_identity_from_candidate_pool():
    case = _case()
    trace = _trace(
        ranking=["eval/guide/other.md#Overview:0"],
        candidates=["eval/guide/other.md#Overview:0"],
    )

    findings = analyze_trace(case, trace)

    assert findings == [
        {
            "case_id": "case-a",
            "class": "missing_from_candidate_pool",
            "severity": "high",
            "identity": "eval/guide/auth.md#Rotation:0",
            "evidence": {
                "candidate_count": 1,
                "relevance_grade": 3,
            },
        },
    ]


def test_analyze_trace_reports_relevant_identity_lost_after_fusion_topk():
    identity = "eval/guide/auth.md#Rotation:0"
    case = _case()
    trace = _trace(
        ranking=["eval/guide/other.md#Overview:0"],
        candidates=["eval/guide/other.md#Overview:0", identity],
        hydrated=["eval/guide/other.md#Overview:0", identity],
        hydration_requested=2,
    )

    findings = analyze_trace(case, trace)

    assert findings == [
        {
            "case_id": "case-a",
            "class": "lost_after_fusion_topk",
            "severity": "medium",
            "identity": identity,
            "evidence": {
                "candidate_rank": 2,
                "ranking_count": 1,
                "relevance_grade": 3,
            },
        },
    ]


def test_analyze_trace_reports_hydration_drop_only_when_requested():
    identity = "eval/guide/auth.md#Rotation:0"
    case = _case()
    dropped_trace = _trace(
        ranking=[identity],
        candidates=[identity],
        hydrated=[],
        hydration_requested=1,
    )
    unrequested_trace = _trace(
        ranking=[identity],
        candidates=[identity],
        hydrated=[],
        hydration_requested=0,
    )

    assert analyze_trace(case, dropped_trace) == [
        {
            "case_id": "case-a",
            "class": "hydration_drop",
            "severity": "medium",
            "identity": identity,
            "evidence": {
                "hydration_requested": 1,
                "hydration_hydrated": 0,
                "hydration_dropped": 1,
                "relevance_grade": 3,
            },
        },
    ]
    assert analyze_trace(case, unrequested_trace) == []


def test_analyze_trace_reports_rerank_worsened_order_only_when_applied():
    better = "eval/guide/auth.md#Rotation:0"
    worse = "eval/guide/backup.md#Rotation:0"
    case = _case(relevant={better: 3, worse: 2})
    trace = _trace(
        ranking=["eval/guide/other.md#Overview:0", worse, better],
        relevant=case.relevant,
        candidates=[better, worse, "eval/guide/other.md#Overview:0"],
        rerank={"applied": True, "scored_count": 3},
    )
    disabled_trace = _trace(
        ranking=["eval/guide/other.md#Overview:0", worse, better],
        relevant=case.relevant,
        candidates=[better, worse, "eval/guide/other.md#Overview:0"],
        rerank={"applied": False},
    )

    assert analyze_trace(case, trace) == [
        {
            "case_id": "case-a",
            "class": "rerank_worsened_order",
            "severity": "medium",
            "identity": better,
            "evidence": {
                "fusion_best_rank": 1,
                "ranking_best_rank": 2,
                "ranking_identity": worse,
                "rerank": {"applied": True, "scored_count": 3},
            },
        },
    ]
    assert analyze_trace(case, disabled_trace) == []


def test_analyze_trace_reports_unknown_quality_loss_when_unexplained():
    identity = "eval/guide/auth.md#Rotation:0"
    case = _case()
    trace = {
        "metrics_input": {
            "ranking": [],
            "relevant": case.relevant,
        },
        "stages": {},
    }

    findings = analyze_trace(case, trace)

    assert findings == [
        {
            "case_id": "case-a",
            "class": "unknown_quality_loss",
            "severity": "low",
            "identity": identity,
            "evidence": {
                "ranking_count": 0,
                "candidate_count": 0,
                "selected_relevant_count": 0,
                "relevance_grade": 3,
            },
        },
    ]


def test_ranked_backlog_groups_counts_and_sorts_deterministically():
    findings = [
        {
            "case_id": "case-c",
            "class": "unknown_quality_loss",
            "severity": "low",
            "identity": "c",
            "evidence": {},
        },
        {
            "case_id": "case-a",
            "class": "lost_after_fusion_topk",
            "severity": "medium",
            "identity": "a",
            "evidence": {},
        },
        {
            "case_id": "case-b",
            "class": "hydration_drop",
            "severity": "medium",
            "identity": "b",
            "evidence": {},
        },
        {
            "case_id": "case-d",
            "class": "missing_from_candidate_pool",
            "severity": "high",
            "identity": "d",
            "evidence": {},
        },
        {
            "case_id": "case-e",
            "class": "hydration_drop",
            "severity": "medium",
            "identity": "e",
            "evidence": {},
        },
    ]

    assert ranked_backlog(findings) == [
        {
            "class": "missing_from_candidate_pool",
            "count": 1,
            "severity": "high",
        },
        {
            "class": "hydration_drop",
            "count": 2,
            "severity": "medium",
        },
        {
            "class": "lost_after_fusion_topk",
            "count": 1,
            "severity": "medium",
        },
        {
            "class": "unknown_quality_loss",
            "count": 1,
            "severity": "low",
        },
    ]


def test_ranked_backlog_keeps_stable_counts_with_duplicate_findings():
    finding = {
        "case_id": "case-a",
        "class": "hydration_drop",
        "severity": "medium",
        "identity": "eval/guide/auth.md#Rotation:0",
        "evidence": {},
    }

    assert ranked_backlog([finding, dict(finding)]) == [
        {
            "class": "hydration_drop",
            "count": 2,
            "severity": "medium",
        },
    ]
