from collections import Counter
from copy import deepcopy
import json

import pytest

from eval.search_pipeline.fixtures import BenchmarkCase
from eval.search_pipeline.fixtures import DEFAULT_LIVE_CASES
from eval.search_pipeline.selection import fusion_weight_grid
from eval.search_pipeline.selection import FusionCandidate
from eval.search_pipeline.selection import GRAPH_WEIGHTS
from eval.search_pipeline.selection import _identity
from eval.search_pipeline.selection import PAGE_WEIGHTS
from eval.search_pipeline.selection import replay_fusion
from eval.search_pipeline.selection import replay_fusion_candidate
from eval.search_pipeline.selection import RERANK_BATCHES
from eval.search_pipeline.selection import select_fusion_weights
from eval.search_pipeline.selection import select_fusion_candidate
from eval.search_pipeline.selection import select_rerank_batch
from eval.search_pipeline.selection import stage_a_candidates
from eval.search_pipeline.selection import stage_b_candidates
from eval.search_pipeline.selection import transform_candidate_signals
from iwiki_mcp.engine.fusion import fuse_ranked


def _case(*, k=2):
    return BenchmarkCase(
        case_id="case-a",
        domain="iwiki-mcp",
        query="needle",
        relevant={"iwiki-mcp/mcp-server.md#Tool surface:0": 3},
        intents={"api": ["iwiki-mcp/mcp-server.md#Tool surface:0"]},
        k=k,
    )


def _second_case(*, k=2):
    return BenchmarkCase(
        case_id="case-b",
        domain="iwiki-mcp",
        query="other needle",
        relevant={"iwiki-mcp/other.md#Target:0": 3},
        intents={"other": ["iwiki-mcp/other.md#Target:0"]},
        k=k,
    )


def _ranked(signals):
    ranked = {}
    for name, identities in signals.items():
        ranked[name] = []
        for ordinal, value in enumerate(identities):
            location, heading_chunk = value.split("#", 1)
            domain, file_name = location.split("/", 1)
            heading, chunk = heading_chunk.rsplit(":", 1)
            ranked[name].append({
                "domain": domain,
                "file": file_name,
                "heading": heading,
                "chunk": int(chunk),
                "ordinal": ordinal,
            })
    return ranked


def _trace(*, mode="hybrid", signals=None, ranking=None, rerank_ms=100.0, k=2):
    answer = "iwiki-mcp/mcp-server.md#Tool surface:0"
    noise = "iwiki-mcp/retrieval.md#Hybrid search:0"
    signals = signals or {
        "semantic_chunk": [answer],
        "lexical_section": [noise],
        "semantic_page": [noise],
        "lexical_page": [noise],
        "graph_page": [noise],
    }
    ranking = ranking or [answer, noise]
    return {
        "case_id": "case-a",
        "mode": mode,
        "k": k,
        "stages": {
            "signals": {"identities": signals, "ranked": _ranked(signals)},
            "fusion": {"candidate_identities": ranking},
            "hydration": {"requested": 2, "hydrated_identities": ranking},
            "rerank": {"applied": False},
        },
        "latency": {"rerank_ms": rerank_ms},
        "metrics_input": {
            "ranking": ranking,
            "relevant": {answer: 3},
            "intents": {"api": [answer]},
        },
    }


def _measured(traces):
    for sample_id, trace in enumerate(traces, 1):
        trace["sample_id"] = sample_id
        trace["status"] = "passed"
    return traces


def _all_modes(trace):
    return [
        {**deepcopy(trace), "mode": mode}
        for mode in ("hybrid", "lexical", "semantic")
    ]


def _fanout_trace(*, k=8):
    direct_target = "iwiki-mcp/answer.md#Target:0"
    fanout = [
        f"iwiki-mcp/page.md#Noise {index}:{0}"
        for index in range(8)
    ]
    return _trace(
        k=k,
        signals={
            "semantic_chunk": [direct_target],
            "semantic_page": [
                "iwiki-mcp/page.md#A:0",
                "iwiki-mcp/page.md#B:0",
                "iwiki-mcp/other.md#C:0",
            ],
            "lexical_page": [
                "iwiki-mcp/page.md#A:0",
                "iwiki-mcp/page.md#B:0",
                "iwiki-mcp/other.md#C:0",
            ],
            "graph_page": fanout,
        },
    )


def test_stage_a_grid_has_exactly_three_candidates_per_family():
    candidates = stage_a_candidates()

    assert len(candidates) == 12
    assert Counter(item.family for item in candidates) == {
        "rrf_k": 3,
        "direct_multiplier": 3,
        "direct_quota": 3,
        "fanout_cap": 3,
    }
    assert candidates == stage_a_candidates()


def test_candidate_descriptor_is_frozen_and_serializes_stably():
    candidate = FusionCandidate(family="rrf_k", rrf_k=20)

    with pytest.raises(AttributeError):
        candidate.rrf_k = 10

    assert candidate.payload() == {
        "family": "rrf_k",
        "rrf_k": 20,
        "direct_multiplier": 1.0,
        "direct_quota": 0,
        "fanout_cap": None,
        "components": [],
    }


@pytest.mark.parametrize(
    "candidate",
    (
        FusionCandidate(family="rrf_k", rrf_k=0),
        FusionCandidate(family="direct_multiplier", direct_multiplier=0.99),
        FusionCandidate(family="direct_quota", direct_quota=9),
        FusionCandidate(family="fanout_cap", fanout_cap=0),
        FusionCandidate(family="unknown"),
        FusionCandidate(family="rrf_k", rrf_k=20, direct_quota=1),
    ),
)
def test_candidate_transform_rejects_invalid_descriptors(candidate):
    with pytest.raises(ValueError, match="candidate"):
        transform_candidate_signals(_ranked(_trace()["stages"]["signals"]["identities"]), candidate)


def test_fanout_cap_is_independent_per_broad_signal_and_preserves_order():
    trace = _fanout_trace()
    signals = transform_candidate_signals(
        trace["stages"]["signals"]["ranked"],
        FusionCandidate(family="fanout_cap", fanout_cap=1),
    )

    assert [_identity(hit) for hit in signals["semantic_page"]] == [
        "iwiki-mcp/page.md#A:0",
        "iwiki-mcp/other.md#C:0",
    ]
    assert [_identity(hit) for hit in signals["lexical_page"]] == [
        "iwiki-mcp/page.md#A:0",
        "iwiki-mcp/other.md#C:0",
    ]
    assert [_identity(hit) for hit in signals["semantic_chunk"]] == [
        "iwiki-mcp/answer.md#Target:0",
    ]
    assert trace["stages"]["signals"]["ranked"]["semantic_page"][1]["heading"] == "B"


def test_direct_quota_promotes_missing_direct_identity_into_last_reserved_slot():
    target = "iwiki-mcp/answer.md#Target:0"
    broad_hits = [
        f"iwiki-mcp/page.md#Noise {index}:0"
        for index in range(8)
    ]
    trace = _trace(
        k=8,
        signals={
            "semantic_chunk": [target],
            "semantic_page": broad_hits,
            "lexical_page": broad_hits,
            "graph_page": broad_hits,
        },
    )
    baseline = replay_fusion_candidate(trace, FusionCandidate())
    ranking = replay_fusion_candidate(
        trace,
        FusionCandidate(family="direct_quota", direct_quota=1),
    )

    assert target not in baseline[:8]
    assert ranking[7] == target
    assert ranking[:7] == baseline[:7]


def test_stage_b_contains_only_unordered_pairs_of_passing_family_winners():
    winners = [
        FusionCandidate(family="rrf_k", rrf_k=20),
        FusionCandidate(family="direct_multiplier", direct_multiplier=1.5),
        FusionCandidate(family="direct_quota", direct_quota=2),
        FusionCandidate(family="fanout_cap", fanout_cap=2),
    ]

    pairs = stage_b_candidates(winners)

    assert len(pairs) == 6
    assert all(item.family == "pair" for item in pairs)
    assert all(len(item.components) == 2 for item in pairs)
    assert pairs == stage_b_candidates(list(reversed(winners)))


def test_candidate_selector_never_combines_rejected_family_winner():
    traces = _all_modes(_trace())

    decision = select_fusion_candidate([_case()], traces)

    rejected = set(decision["family_rejections"])
    assert len(decision["stage_a"]) == 12
    assert len(decision["stage_b"]) <= 6
    assert all(
        not rejected.intersection(item["families"])
        for item in decision["stage_b"]
    )


def test_candidate_selector_returns_needs_work_when_no_candidate_passes(
    monkeypatch,
):
    cases = []
    traces = []
    for case_id, rank in (("case-a", 22), ("case-b", 26)):
        target = f"iwiki-mcp/answer.md#Target {case_id}:0"
        cases.append(BenchmarkCase(
            case_id=case_id,
            domain="iwiki-mcp",
            query="needle",
            relevant={target: 3},
            intents={"api": [target]},
            k=1,
        ))
        trace = _trace(k=1)
        trace["case_id"] = case_id
        trace["metrics_input"] = {
            "ranking": ["iwiki-mcp/noise.md#N:0"],
            "relevant": {target: 3},
            "intents": {"api": [target]},
        }
        trace["stages"]["fusion"]["candidate_identities"] = [
            *(f"iwiki-mcp/noise.md#N:{index}" for index in range(rank - 1)),
            target,
        ]
        traces.extend(_all_modes(trace))

    def replay(trace, candidate):
        target = next(iter(trace["metrics_input"]["relevant"]))
        return [target] if candidate.family == "baseline" else ["iwiki-mcp/noise.md#N:0"]

    monkeypatch.setattr(
        "eval.search_pipeline.selection.replay_fusion_candidate", replay,
    )

    decision = select_fusion_candidate(cases, traces)

    assert decision["status"] == "needs_work"
    assert decision["reason"] == "no_passing_fusion_candidate"
    assert decision["candidate"] is None


def test_live_corpus_has_two_reviewed_cases_per_query_class():
    expected = {
        "exact_identifier",
        "semantic_paraphrase",
        "multi_intent",
        "repeated_heading",
        "graph_distractor",
        "competing_evidence",
    }

    assert len(DEFAULT_LIVE_CASES) == 12
    assert len({case.case_id for case in DEFAULT_LIVE_CASES}) == 12
    assert Counter(case.query_class for case in DEFAULT_LIVE_CASES) == {
        name: 2 for name in expected
    }
    assert all(case.relevant and case.intents and case.k == 8 for case in DEFAULT_LIVE_CASES)


def test_fusion_grid_is_bounded_and_deterministic():
    first = fusion_weight_grid()

    assert PAGE_WEIGHTS == (0.025, 0.05, 0.1)
    assert GRAPH_WEIGHTS == (0.01, 0.025, 0.05)
    assert RERANK_BATCHES == (16, 24, 32)
    assert first == fusion_weight_grid()
    assert len(first) == 8
    assert all(item["semantic_chunk"] == item["lexical_section"] == 1.0 for item in first)
    assert all(item["semantic_page"] == item["lexical_page"] for item in first)
    assert all(item["graph_page"] <= item["semantic_page"] for item in first)


def test_replay_fusion_matches_production_when_equal_scores_use_ordinals():
    alpha = {
        "domain": "iwiki-mcp",
        "file": "retrieval.md",
        "heading": "Alpha",
        "chunk": 0,
        "ordinal": 20,
    }
    beta = {**alpha, "heading": "Beta", "ordinal": 10}
    signals = {
        "semantic_chunk": [alpha, beta],
        "lexical_section": [beta, alpha],
    }
    trace = _trace()
    trace["stages"]["signals"] = {"ranked": signals}

    production = fuse_ranked(signals, limit=2)
    assert replay_fusion(trace, {}) == [
        f"{hit['domain']}/{hit['file']}#{hit['heading']}:{hit['chunk']}"
        for hit in production
    ] == [
        "iwiki-mcp/retrieval.md#Beta:0",
        "iwiki-mcp/retrieval.md#Alpha:0",
    ]


def test_selector_rejects_legacy_trace_without_ranked_ordinals():
    traces = _all_modes(_trace())
    for trace in traces:
        trace["stages"]["signals"].pop("ranked")

    decision = select_fusion_weights([_case()], traces)

    assert replay_fusion(traces[0], {}) is None
    assert decision["status"] == "needs_work"
    assert decision["reason"] == "replay_evidence_incomplete"
    assert all(item["passed"] is False for item in decision["candidates"])


def test_selector_output_is_byte_stable_for_same_captured_traces():
    case = _case(k=1)
    traces = [_trace()]

    first = json.dumps(
        select_fusion_weights([case], traces),
        sort_keys=True,
        separators=(",", ":"),
    )
    second = json.dumps(
        select_fusion_weights([case], traces),
        sort_keys=True,
        separators=(",", ":"),
    )

    assert first == second


def test_selector_rejects_recall_regression_before_ndcg_optimization():
    case = _case(k=1)
    answer = "iwiki-mcp/mcp-server.md#Tool surface:0"
    noise = "iwiki-mcp/retrieval.md#Hybrid search:0"
    traces = [_trace(k=1, signals={
        "semantic_chunk": [noise],
        "semantic_page": [answer],
        "lexical_page": [answer],
        "graph_page": [answer],
    })]

    decision = select_fusion_weights([case], traces)

    assert any("recall_regression" in item["reasons"] for item in decision["candidates"])


def test_selector_rejects_ndcg_and_new_lost_after_fusion_findings():
    case = _case()
    answer = "iwiki-mcp/mcp-server.md#Tool surface:0"
    noise = "iwiki-mcp/retrieval.md#Hybrid search:0"
    extra_noise = "iwiki-mcp/indexing.md#Vector store:0"
    traces = [_trace(
        signals={
            "semantic_chunk": [noise, extra_noise],
            "lexical_section": [extra_noise, noise],
            "semantic_page": [answer],
            "lexical_page": [answer],
            "graph_page": [answer],
        },
        ranking=[answer, noise],
    )]

    decision = select_fusion_weights([case], traces)
    rejected = [item for item in decision["candidates"] if not item["passed"]]

    assert any("ndcg_loss_exceeds_limit" in item["reasons"] for item in rejected)
    assert any("new_lost_after_fusion_top_k" in item["reasons"] for item in rejected)


def test_selector_requires_confirmed_loss_recovery_in_final_top_eight(monkeypatch):
    targets = {
        "case-a": "iwiki-mcp/other.md#Target A:0",
        "case-b": "iwiki-mcp/other.md#Target B:0",
    }
    cases = [
        BenchmarkCase(
            case_id=case_id,
            domain="iwiki-mcp",
            query="needle",
            relevant={target: 3},
            intents={"api": [target]},
            k=9,
        )
        for case_id, target in targets.items()
    ]
    traces = []
    for case_id, target in targets.items():
        trace = _trace(k=9)
        trace["case_id"] = case_id
        trace["metrics_input"] = {
            "ranking": [f"iwiki-mcp/noise.md#N:{rank}" for rank in range(8)],
            "relevant": {target: 3},
            "intents": {"api": [target]},
        }
        rank = 22 if case_id == "case-a" else 26
        trace["stages"]["fusion"]["candidate_identities"] = [
            *(f"iwiki-mcp/noise.md#N:{item}" for item in range(rank - 1)),
            target,
        ]
        traces.extend(_all_modes(trace))

    def fake_replay(trace, weights):
        target = targets[trace["case_id"]]
        return [
            *(f"iwiki-mcp/noise.md#R:{rank}" for rank in range(8)),
            target,
        ] if not weights else [
            *([target] if trace["case_id"] == "case-a" else []),
            *(f"iwiki-mcp/noise.md#R:{rank}" for rank in range(8)),
            target,
        ]

    monkeypatch.setattr(
        "eval.search_pipeline.selection.replay_fusion", fake_replay,
    )

    decision = select_fusion_weights(cases, traces)

    assert all(
        "confirmed_loss_not_recovered" in item["reasons"]
        for item in decision["candidates"]
    )


@pytest.mark.parametrize("missing_rank", (22, 26))
def test_selector_rejects_incomplete_confirmed_loss_evidence(
    monkeypatch, missing_rank,
):
    targets = {
        "case-a": "iwiki-mcp/other.md#Target A:0",
        "case-b": "iwiki-mcp/other.md#Target B:0",
    }
    cases = [
        BenchmarkCase(
            case_id=case_id,
            domain="iwiki-mcp",
            query="needle",
            relevant={target: 3},
            intents={"api": [target]},
            k=8,
        )
        for case_id, target in targets.items()
    ]
    traces = []
    for case_id, target in targets.items():
        rank = 22 if case_id == "case-a" else 26
        trace = _trace(k=8)
        trace["case_id"] = case_id
        trace["metrics_input"] = {
            "ranking": [f"iwiki-mcp/noise.md#N:{item}" for item in range(8)],
            "relevant": {target: 3},
            "intents": {"api": [target]},
        }
        trace["stages"]["fusion"]["candidate_identities"] = [
            *(f"iwiki-mcp/noise.md#N:{item}" for item in range(rank - 1)),
            target,
        ] if rank != missing_rank else [target]
        traces.extend(_all_modes(trace))

    recovered_target = targets["case-b" if missing_rank == 22 else "case-a"]

    def fake_replay(trace, weights):
        target = targets[trace["case_id"]]
        return [
            *(f"iwiki-mcp/noise.md#R:{rank}" for rank in range(8)),
            target,
        ] if not weights else [
            *([target] if target == recovered_target else []),
            *(f"iwiki-mcp/noise.md#R:{rank}" for rank in range(8)),
            target,
        ]

    monkeypatch.setattr(
        "eval.search_pipeline.selection.replay_fusion", fake_replay,
    )

    decision = select_fusion_weights(cases, traces)

    assert decision["status"] == "needs_work"
    assert decision["reason"] == "confirmed_loss_evidence_incomplete"
    assert decision["weights"] is None
    assert all(item["passed"] is False for item in decision["candidates"])
    assert all(
        "confirmed_loss_evidence_incomplete" in item["reasons"]
        for item in decision["candidates"]
    )


def test_fusion_selector_rejects_missing_reviewed_case_trace():
    decision = select_fusion_weights([_case(), _second_case()], [_trace()])

    assert decision["status"] == "needs_work"
    assert decision["reason"] == "case_mode_evidence_incomplete"
    assert all(item["passed"] is False for item in decision["candidates"])
    assert all(
        "case_mode_evidence_incomplete" in item["reasons"]
        for item in decision["candidates"]
    )


def test_fusion_selector_rejects_missing_mode_trace():
    first = _trace(mode="hybrid")
    second = _trace(mode="semantic")
    third = _trace(mode="hybrid")
    third["case_id"] = "case-b"

    decision = select_fusion_weights([_case(), _second_case()], [first, second, third])

    assert decision["status"] == "needs_work"
    assert decision["reason"] == "case_mode_evidence_incomplete"


def test_fusion_selector_rejects_duplicate_case_mode_trace():
    first = _trace()
    second = _trace()
    third = _trace()
    third["case_id"] = "case-b"

    decision = select_fusion_weights([_case(), _second_case()], [first, second, third])

    assert decision["status"] == "needs_work"
    assert decision["reason"] == "case_mode_evidence_incomplete"


def test_fusion_selector_rejects_extra_case_mode_trace():
    traces = _all_modes(_trace())
    traces.append(_trace(mode="unsupported"))

    decision = select_fusion_weights([_case()], traces)

    assert decision["status"] == "needs_work"
    assert decision["reason"] == "case_mode_evidence_incomplete"


@pytest.mark.parametrize("invalid_k", (0, -1, True, "2", 2.0, float("inf")))
def test_fusion_selector_rejects_invalid_trace_k(invalid_k):
    traces = _all_modes(_trace())
    traces[0]["k"] = invalid_k

    decision = select_fusion_weights([_case()], traces)

    assert decision["status"] == "needs_work"
    assert decision["reason"] == "evidence_invalid"
    assert all(item["passed"] is False for item in decision["candidates"])
    assert all("evidence_invalid" in item["reasons"] for item in decision["candidates"])


def test_confirmed_loss_evidence_allows_same_identity_in_distinct_records(
    monkeypatch,
):
    target = "iwiki-mcp/mcp-server.md#Tool surface:0"
    cases = [_case(), BenchmarkCase(
        case_id="case-b",
        domain="iwiki-mcp",
        query="other needle",
        relevant={target: 3},
        intents={"api": [target]},
        k=2,
    )]
    traces = _all_modes(_trace())
    second = _trace()
    second["case_id"] = "case-b"
    second["metrics_input"] = {
        "ranking": [target],
        "relevant": {target: 3},
        "intents": {"api": [target]},
    }
    traces.extend(_all_modes(second))
    for trace, rank in ((traces[0], 22), (traces[4], 26)):
        trace["stages"]["fusion"]["candidate_identities"] = [
            *(f"iwiki-mcp/noise.md#N:{item}" for item in range(rank - 1)),
            target,
        ]
        trace["metrics_input"]["ranking"] = ["iwiki-mcp/noise.md#N:0"]

    monkeypatch.setattr(
        "eval.search_pipeline.selection.replay_fusion",
        lambda trace, weights: [target],
    )

    decision = select_fusion_weights(cases, traces)

    assert decision["status"] == "passed"


def test_rerank_selector_requires_quality_and_latency_gates():
    case = _case(k=1)
    answer = "iwiki-mcp/mcp-server.md#Tool surface:0"
    noise = "iwiki-mcp/retrieval.md#Hybrid search:0"
    baseline = _measured([
        _trace(rerank_ms=value, k=1) for value in (100, 110, 120)
    ])
    low_quality = _measured([
        _trace(ranking=[noise, answer], rerank_ms=value, k=1)
        for value in (50, 55, 60)
    ])
    slow = _measured([
        _trace(rerank_ms=value, k=1) for value in (80, 85, 91)
    ])

    decision = select_rerank_batch(
        [case],
        {32: baseline, 16: low_quality, 24: slow},
    )
    candidates = {item["batch"]: item for item in decision["candidates"]}

    assert candidates[16]["passed"] is False
    assert "recall_regression" in candidates[16]["reasons"]
    assert candidates[24]["passed"] is False
    assert "p95_improvement_below_25_percent" in candidates[24]["reasons"]
    assert decision["batch"] == 32
    assert decision["status"] == "needs_work"
    assert decision["reason"] == "latency_gate_unresolved"


@pytest.mark.parametrize(
    "invalid_latency",
    (float("nan"), float("inf"), -1.0, "50", True),
)
def test_rerank_selector_rejects_invalid_latency_for_every_batch(
    invalid_latency,
):
    case = _case()
    baseline = _measured([
        _trace(rerank_ms=value) for value in (100, 110, 120)
    ])
    faster = _measured([
        _trace(rerank_ms=value) for value in (50, 55, 60)
    ])
    faster[0]["latency"]["rerank_ms"] = invalid_latency

    decision = select_rerank_batch(
        [case],
        {32: baseline, 16: faster, 24: faster},
    )

    assert decision["status"] == "needs_work"
    assert decision["reason"] == "invalid_rerank_evidence"
    assert all(item["passed"] is False for item in decision["candidates"])
    assert all(
        "invalid_rerank_evidence" in item["reasons"]
        for item in decision["candidates"]
    )


def test_rerank_selector_rejects_missing_measured_sample_id():
    case = _case()
    baseline = _measured([
        _trace(rerank_ms=value) for value in (100, 110, 120)
    ])
    faster = _measured([
        _trace(rerank_ms=value) for value in (50, 55, 60)
    ])
    del faster[0]["sample_id"]

    decision = select_rerank_batch(
        [case],
        {32: baseline, 16: faster, 24: faster},
    )

    assert decision["status"] == "needs_work"
    assert decision["reason"] == "invalid_rerank_evidence"
    assert all(item["p95_rerank_ms"] is None for item in decision["candidates"])


@pytest.mark.parametrize("unknown_batch", (32, 16))
def test_rerank_selector_rejects_unknown_reviewed_case_samples(unknown_batch):
    case = _case()
    baseline = _measured([
        _trace(rerank_ms=value) for value in (100, 110, 120)
    ])
    faster = _measured([
        _trace(rerank_ms=value) for value in (50, 55, 60)
    ])
    runs = {32: baseline, 16: faster, 24: faster}
    runs[unknown_batch][0]["case_id"] = "case-unknown"

    decision = select_rerank_batch([case], runs)

    assert decision["status"] == "needs_work"
    assert all(item["passed"] is False for item in decision["candidates"])
    assert all(
        "case_mode_sample_mismatch" in item["reasons"]
        for item in decision["candidates"]
    )


def test_rerank_selector_rejects_unknown_only_samples():
    case = _case()
    baseline = [_trace(rerank_ms=value) for value in (100, 110, 120)]
    faster = [_trace(rerank_ms=value) for value in (50, 55, 60)]
    for traces in (baseline, faster):
        for trace in traces:
            trace["case_id"] = "case-unknown"

    decision = select_rerank_batch(
        [case],
        {32: baseline, 16: faster, 24: faster},
    )

    assert decision["status"] == "needs_work"
    assert all(item["passed"] is False for item in decision["candidates"])
    assert all(
        "case_mode_sample_mismatch" in item["reasons"]
        for item in decision["candidates"]
    )


def test_rerank_selector_rejects_non_hybrid_samples():
    case = _case()
    baseline = [_trace(rerank_ms=value) for value in (100, 110, 120)]
    faster = [_trace(rerank_ms=value) for value in (50, 55, 60)]
    baseline[0]["mode"] = "semantic"

    decision = select_rerank_batch(
        [case],
        {32: baseline, 16: faster, 24: faster},
    )

    assert decision["status"] == "needs_work"
    assert all(item["passed"] is False for item in decision["candidates"])
    assert all(
        "case_mode_sample_mismatch" in item["reasons"]
        for item in decision["candidates"]
    )


def test_rerank_selector_rejects_missing_baseline_mode():
    case = _case()
    baseline = [
        _trace(mode="hybrid", rerank_ms=100),
        _trace(mode="semantic", rerank_ms=100),
    ]
    incomplete = [_trace(mode="hybrid", rerank_ms=50)]

    decision = select_rerank_batch(
        [case],
        {32: baseline, 16: incomplete, 24: incomplete},
    )
    candidates = {item["batch"]: item for item in decision["candidates"]}

    assert candidates[16]["passed"] is False
    assert "missing_baseline_mode" in candidates[16]["reasons"]


def test_rerank_selector_rejects_missing_and_extra_case_mode_samples():
    cases = [_case(), BenchmarkCase(
        case_id="case-b",
        domain="iwiki-mcp",
        query="other needle",
        relevant={"iwiki-mcp/other.md#Target:0": 3},
        intents={"other": ["iwiki-mcp/other.md#Target:0"]},
    )]
    baseline = [_trace(rerank_ms=100), _trace(rerank_ms=110)]
    baseline[1]["case_id"] = "case-b"
    baseline[1]["mode"] = "semantic"
    candidate = [_trace(rerank_ms=50), _trace(rerank_ms=55)]
    candidate[1]["case_id"] = "case-c"
    candidate[1]["mode"] = "semantic"

    decision = select_rerank_batch(
        cases,
        {32: baseline, 16: candidate, 24: candidate},
    )
    candidates = {item["batch"]: item for item in decision["candidates"]}

    assert candidates[16]["passed"] is False
    assert "case_mode_sample_mismatch" in candidates[16]["reasons"]


def test_rerank_selector_rejects_different_measured_sample_id():
    case = _case()
    baseline = [_trace(rerank_ms=100), _trace(rerank_ms=110)]
    candidate = [_trace(rerank_ms=50), _trace(rerank_ms=55)]
    for sample_id, trace in enumerate(baseline, 1):
        trace["sample_id"] = sample_id
    candidate[0]["sample_id"] = 1
    candidate[1]["sample_id"] = 3

    decision = select_rerank_batch(
        [case],
        {32: baseline, 16: candidate, 24: candidate},
    )
    candidates = {item["batch"]: item for item in decision["candidates"]}

    assert candidates[16]["passed"] is False
    assert "case_mode_sample_mismatch" in candidates[16]["reasons"]


def test_rerank_selector_rejects_missing_duplicate_measured_sample():
    case = _case()
    baseline = [_trace(rerank_ms=value) for value in (100, 110)]
    candidate = [_trace(rerank_ms=50)]
    for trace in baseline:
        trace["sample_id"] = 1
    candidate[0]["sample_id"] = 1

    decision = select_rerank_batch(
        [case],
        {32: baseline, 16: candidate, 24: candidate},
    )
    candidates = {item["batch"]: item for item in decision["candidates"]}

    assert decision["status"] == "needs_work"
    assert decision["batch"] is None
    assert decision["reason"] == "duplicate_measured_sample"
    assert candidates[16]["passed"] is False
    assert "duplicate_measured_sample" in candidates[16]["reasons"]


@pytest.mark.parametrize("duplicate_batch", (32, 16, 24))
def test_rerank_selector_rejects_duplicate_measured_sample_in_any_batch(
    duplicate_batch,
):
    case = _case()
    runs = {
        batch: [_trace(rerank_ms=value) for value in latencies]
        for batch, latencies in {
            32: (100, 110, 120),
            16: (50, 55, 60),
            24: (50, 55, 60),
        }.items()
    }
    for traces in runs.values():
        for sample_id, trace in enumerate(traces, 1):
            trace["sample_id"] = sample_id
    runs[duplicate_batch][-1]["sample_id"] = 1

    decision = select_rerank_batch([case], runs)

    assert decision["status"] == "needs_work"
    assert decision["batch"] is None
    assert decision["reason"] == "duplicate_measured_sample"
    assert all(item["passed"] is False for item in decision["candidates"])
    assert all(
        "duplicate_measured_sample" in item["reasons"]
        for item in decision["candidates"]
    )


def test_rerank_selector_returns_first_passing_smaller_batch():
    case = _case()
    baseline = _measured([
        _trace(rerank_ms=value) for value in (100, 110, 120)
    ])
    faster = _measured([
        _trace(rerank_ms=value) for value in (50, 55, 60)
    ])

    decision = select_rerank_batch(
        [case],
        {32: baseline, 16: faster, 24: faster},
    )

    assert decision["status"] == "passed"
    assert decision["batch"] == 16
