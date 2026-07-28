import pytest

from eval.search_pipeline.fixtures import BenchmarkCase
from eval.search_pipeline.metrics import (
    identity,
    intent_coverage_at_k,
    latency_summary,
    mrr_at_k,
    ndcg_at_k,
    recall_at_k,
    source_mix,
)


def test_identity_uses_public_search_fields():
    assert identity({
        "domain": "iwiki-mcp",
        "file": "retrieval.md",
        "heading": "Hybrid search",
        "chunk": 0,
    }) == "iwiki-mcp/retrieval.md#Hybrid search:0"


def test_quality_metrics_use_case_relevance():
    case = BenchmarkCase(
        case_id="modes",
        domain="iwiki-mcp",
        query="search mode enum",
        relevant={
            "iwiki-mcp/mcp-server.md#Tool surface:0": 3,
            "iwiki-mcp/retrieval.md#Hybrid search:0": 2,
        },
        intents={
            "api": ["iwiki-mcp/mcp-server.md#Tool surface:0"],
            "retrieval": ["iwiki-mcp/retrieval.md#Hybrid search:0"],
        },
    )
    ranking = [
        "iwiki-mcp/retrieval.md#Hybrid search:0",
        "iwiki-mcp/indexing.md#Configuration:0",
        "iwiki-mcp/mcp-server.md#Tool surface:0",
    ]

    assert recall_at_k(ranking, case, 2) == 1.0
    assert mrr_at_k(ranking, case, 3) == 1.0
    assert ndcg_at_k(ranking, case, 3) == pytest.approx(0.730929, rel=1e-6)
    assert intent_coverage_at_k(ranking, case, 2) == 0.5


def test_source_mix_counts_hit_and_source_fields():
    mix = source_mix([
        {"hit": "both", "source": "seed"},
        {"hit": "lexical", "source": "lexical"},
        {"hit": "semantic", "source": "graph"},
    ])

    assert mix == {
        "hit": {"both": 1, "lexical": 1, "semantic": 1},
        "source": {"graph": 1, "lexical": 1, "seed": 1},
    }


def test_latency_summary_is_stable_and_rounded():
    assert latency_summary({"embed_ms": 1.23456, "fusion_ms": 0.1}) == {
        "embed_ms": 1.235,
        "fusion_ms": 0.1,
        "total_ms": 1.335,
    }
