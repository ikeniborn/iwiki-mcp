import pytest

from eval.search_pipeline.fixtures import BenchmarkCase
from eval.search_pipeline.instrumentation import trace_query
from eval.search_pipeline.metrics import identity
from iwiki_mcp.engine.config import Config
from iwiki_mcp.indexer import index_domain


def _build_trace_fixture(tmp_path, monkeypatch, *, rerank_model=""):
    base = tmp_path / "base"
    domain = "eval"
    page_dir = base / domain / "guide"
    page_dir.mkdir(parents=True)
    (page_dir / "auth.md").write_text(
        "# Auth Guide\n\n"
        "## Rotation\n\n"
        "refresh_token credentials rotate safely without exposing secrets.\n\n"
        "## Overview\n\n"
        "general authentication overview.\n",
        encoding="utf-8",
    )
    (page_dir / "backup.md").write_text(
        "# Backup Guide\n\n"
        "## Rotation\n\n"
        "backup refresh_token credentials procedure for operators.\n",
        encoding="utf-8",
    )

    def fake_embed_texts(cfg, texts):
        vectors = []
        for text in texts:
            score = 1.0 if "refresh_token" in text else 0.1
            vectors.append([score, 1.0 - score])
        return vectors

    monkeypatch.setattr("iwiki_mcp.indexer.embed_texts", fake_embed_texts)
    monkeypatch.setattr("iwiki_mcp.retrieval.embed_texts", fake_embed_texts)

    cfg = Config(
        base_url="https://example.invalid/v1",
        api_key="test-key",
        embed_model="test-embed",
        dimensions=2,
        chunk_size=256,
        chunk_overlap=32,
        summary_max=120,
        top_k=3,
        score_threshold=0.0,
        graph_depth=1,
        ignore=None,
        seed_top_k=2,
        bfs_top_k=2,
        seed_threshold=0.0,
        rerank_model=rerank_model,
    )
    index_domain(cfg, str(base), domain)
    case = BenchmarkCase(
        case_id="offline-symbol",
        domain=domain,
        query="refresh_token credentials",
        relevant={"eval/guide/auth.md#Rotation:0": 3},
        intents={"symbol": ["eval/guide/auth.md#Rotation:0"]},
        k=3,
    )
    return cfg, str(base), case


def test_trace_query_records_stage_counts_and_final_results(tmp_path, monkeypatch):
    cfg, base, case = _build_trace_fixture(tmp_path, monkeypatch)

    trace = trace_query(
        cfg,
        base,
        case,
        mode="hybrid",
        rerank_enabled=False,
    )

    assert set(trace) == {
        "case_id",
        "domain",
        "query",
        "mode",
        "k",
        "latency",
        "stages",
        "results",
        "metrics_input",
    }
    assert set(trace["stages"]) == {"signals", "fusion", "hydration", "rerank"}
    final_identities = [identity(result) for result in trace["results"]]
    assert final_identities[0] == "eval/guide/auth.md#Rotation:0"
    assert trace["k"] == case.k
    assert trace["stages"]["signals"]["counts"]["semantic_chunk"] >= 1
    assert trace["stages"]["signals"]["counts"]["lexical_section"] >= 1
    assert trace["stages"]["fusion"]["candidate_count"] >= 1
    assert (
        trace["stages"]["fusion"]["candidate_identities"][0]
        == "eval/guide/auth.md#Rotation:0"
    )
    assert (
        trace["stages"]["hydration"]["requested"]
        == trace["stages"]["fusion"]["candidate_count"]
    )
    assert trace["stages"]["hydration"]["hydrated"] >= 1
    assert "requested_identities" not in trace["stages"]["hydration"]
    assert trace["stages"]["rerank"] == {
        "applied": False,
        "warning": "rerank disabled",
    }
    assert trace["metrics_input"] == {
        "ranking": final_identities,
        "relevant": case.relevant,
        "intents": case.intents,
    }
    assert trace["latency"]["total_ms"] >= 0
    for stage in (
        "embedding_ms",
        "signals_ms",
        "fusion_ms",
        "hydration_ms",
        "rerank_ms",
    ):
        assert stage in trace["latency"]


def test_trace_query_keeps_fused_results_when_hydration_drops_candidates(
    tmp_path,
    monkeypatch,
):
    cfg, base, case = _build_trace_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "iwiki_mcp.retrieval.hydrate_candidates",
        lambda *args, **kwargs: [],
    )

    trace = trace_query(
        cfg,
        base,
        case,
        mode="hybrid",
        rerank_enabled=False,
    )

    fused_identities = trace["stages"]["fusion"]["candidate_identities"]
    result_identities = [identity(result) for result in trace["results"]]
    assert (
        trace["stages"]["hydration"]["dropped"]
        == trace["stages"]["fusion"]["candidate_count"]
    )
    assert trace["stages"]["hydration"]["hydrated"] == 0
    assert result_identities == fused_identities[:case.k]


def test_trace_query_keeps_fused_results_when_rerank_falls_back(
    tmp_path,
    monkeypatch,
):
    cfg, base, case = _build_trace_fixture(
        tmp_path,
        monkeypatch,
        rerank_model="test-rerank",
    )
    monkeypatch.setattr(
        "eval.search_pipeline.instrumentation.rerank.rerank_candidates",
        lambda *args, **kwargs: ([], {"applied": False, "warning": "fallback"}),
    )

    trace = trace_query(
        cfg,
        base,
        case,
        mode="hybrid",
        rerank_enabled=True,
    )

    fused_identities = trace["stages"]["fusion"]["candidate_identities"]
    result_identities = [identity(result) for result in trace["results"]]
    assert trace["stages"]["rerank"] == {"applied": False, "warning": "fallback"}
    assert result_identities == fused_identities[:case.k]


def test_trace_query_refuses_legacy_store_before_retrieval_helpers(
    tmp_path,
    monkeypatch,
):
    cfg, base, case = _build_trace_fixture(tmp_path, monkeypatch)
    (tmp_path / "base" / case.domain / ".iwiki").mkdir()

    def fail_domain_signals(*args, **kwargs):
        raise AssertionError("_domain_signals must not be called")

    monkeypatch.setattr("iwiki_mcp.retrieval._domain_signals", fail_domain_signals)

    with pytest.raises(RuntimeError, match="legacy store layout"):
        trace_query(
            cfg,
            base,
            case,
            mode="hybrid",
            rerank_enabled=False,
        )


def test_trace_query_rejects_invalid_domain_before_retrieval_helpers(
    tmp_path,
    monkeypatch,
):
    cfg, base, case = _build_trace_fixture(tmp_path, monkeypatch)
    invalid_case = BenchmarkCase(
        case_id=case.case_id,
        domain="../outside",
        query=case.query,
        relevant=case.relevant,
        intents=case.intents,
        k=case.k,
    )

    def fail_domain_signals(*args, **kwargs):
        raise AssertionError("_domain_signals must not be called")

    monkeypatch.setattr("iwiki_mcp.retrieval._domain_signals", fail_domain_signals)

    with pytest.raises(ValueError, match="invalid domain"):
        trace_query(
            cfg,
            base,
            invalid_case,
            mode="hybrid",
            rerank_enabled=False,
        )


def test_trace_query_merges_applied_rerank_with_unscored_fused_candidates(
    tmp_path,
    monkeypatch,
):
    cfg, base, case = _build_trace_fixture(
        tmp_path,
        monkeypatch,
        rerank_model="test-rerank",
    )
    captured = {}

    def fake_rerank_candidates(cfg, query, candidates, top_n=None):
        assert len(candidates) >= 2
        reranked = dict(candidates[1])
        reranked["score"] = 42.0
        captured["scored_identity"] = identity(reranked)
        return [reranked], {"applied": True, "_scored_count": 1}

    monkeypatch.setattr(
        "eval.search_pipeline.instrumentation.rerank.rerank_candidates",
        fake_rerank_candidates,
    )

    trace = trace_query(
        cfg,
        base,
        case,
        mode="hybrid",
        rerank_enabled=True,
    )

    rerank_metadata = trace["stages"]["rerank"]
    fused_identities = trace["stages"]["fusion"]["candidate_identities"]
    expected = [captured["scored_identity"]]
    expected.extend(
        item for item in fused_identities
        if item != captured["scored_identity"]
    )
    result_identities = [identity(result) for result in trace["results"]]
    assert rerank_metadata["applied"] is True
    assert "_scored_count" not in rerank_metadata
    assert rerank_metadata["scored_count"] == 1
    assert result_identities == expected[:case.k]
