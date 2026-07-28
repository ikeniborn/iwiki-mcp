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
