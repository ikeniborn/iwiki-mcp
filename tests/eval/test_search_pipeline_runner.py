from eval.search_pipeline.fixtures import BenchmarkCase
from eval.search_pipeline.instrumentation import trace_query
from eval.search_pipeline.metrics import identity
from iwiki_mcp.engine.config import Config
from iwiki_mcp.indexer import index_domain


def test_trace_query_records_stage_counts_and_final_results(tmp_path, monkeypatch):
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

    trace = trace_query(
        cfg,
        str(base),
        case,
        mode="hybrid",
        rerank_enabled=False,
    )

    final_identities = [identity(result) for result in trace["final_results"]]
    assert final_identities[0] == "eval/guide/auth.md#Rotation:0"
    assert trace["signal_counts"]["semantic_chunk"] >= 1
    assert trace["signal_counts"]["lexical_section"] >= 1
    assert trace["fusion"]["candidate_count"] >= 1
    assert trace["fusion"]["identities"][0] == "eval/guide/auth.md#Rotation:0"
    assert trace["hydration"]["requested"] == trace["fusion"]["candidate_count"]
    assert trace["hydration"]["hydrated"] >= 1
    assert trace["rerank"] == {
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
