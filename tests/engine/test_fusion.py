import math

import pytest

from iwiki_mcp.engine.fusion import fuse_ranked


def _hit(file, heading, chunk=0, **extra):
    return {
        "domain": "d",
        "file": file,
        "heading": heading,
        "chunk": chunk,
        **extra,
    }


def test_fuse_ranked_combines_signals_with_reciprocal_rank_scores():
    signals = {
        "semantic": [_hit("a", "A"), _hit("b", "B")],
        "lexical": [_hit("b", "B"), _hit("c", "C")],
    }

    fused = fuse_ranked(signals, limit=3)

    assert [hit["file"] for hit in fused] == ["b", "a", "c"]
    assert fused[0]["signals"] == ["semantic", "lexical"]


def test_fuse_ranked_identity_includes_chunk_and_ties_are_stable():
    signals = {
        "one": [_hit("b", "S", 0), _hit("a", "S", 1)],
        "two": [_hit("a", "S", 0)],
    }

    fused = fuse_ranked(signals, limit=3)

    assert [(hit["file"], hit["chunk"]) for hit in fused] == [
        ("a", 0),
        ("b", 0),
        ("a", 1),
    ]


def test_fuse_ranked_respects_limit():
    fused = fuse_ranked({"one": [_hit("a", "A"), _hit("b", "B")]}, limit=1)

    assert [hit["file"] for hit in fused] == ["a"]


def test_fuse_ranked_ignores_duplicate_identity_within_signal():
    hit = _hit("a", "A")

    fused = fuse_ranked({"one": [hit, dict(hit)]}, limit=2)

    assert len(fused) == 1
    assert fused[0]["score"] == 1 / 61


def test_fuse_ranked_weighted_direct_signal_beats_page_and_graph_fanout():
    signals = {
        "semantic_chunk": [_hit("direct", "Direct")],
        "semantic_page": [
            _hit("broad-a", "A"),
            _hit("broad-b", "B"),
            _hit("broad-c", "C"),
        ],
        "graph_page": [
            _hit("broad-a", "A"),
            _hit("broad-b", "B"),
            _hit("broad-c", "C"),
        ],
    }

    fused = fuse_ranked(
        signals,
        limit=4,
        signal_weights={"semantic_page": 0.1, "graph_page": 0.1},
    )

    assert [hit["file"] for hit in fused] == [
        "direct",
        "broad-a",
        "broad-b",
        "broad-c",
    ]


def test_fuse_ranked_missing_signal_weight_defaults_to_one():
    signals = {
        "semantic": [_hit("a", "A")],
        "lexical": [_hit("b", "B")],
    }

    assert fuse_ranked(signals, limit=2) == fuse_ranked(
        signals,
        limit=2,
        signal_weights={"semantic": 1.0},
    )


def test_fuse_ranked_explicit_default_rrf_k_matches_implicit_default():
    signals = {
        "semantic": [_hit("a", "A"), _hit("b", "B")],
        "lexical": [_hit("b", "B"), _hit("c", "C")],
    }

    assert fuse_ranked(signals, limit=3) == fuse_ranked(
        signals,
        limit=3,
        rrf_k=60,
    )


def test_fuse_ranked_lower_rrf_k_increases_score_gap_between_ranks():
    signals = {"semantic": [_hit("a", "A"), _hit("b", "B")]}

    default_scores = fuse_ranked(signals, limit=2)
    lower_k_scores = fuse_ranked(signals, limit=2, rrf_k=1)

    default_gap = default_scores[0]["score"] - default_scores[1]["score"]
    lower_k_gap = lower_k_scores[0]["score"] - lower_k_scores[1]["score"]

    assert lower_k_gap > default_gap


@pytest.mark.parametrize("rrf_k", [0, -1, True, 1.5, "10"])
def test_fuse_ranked_rejects_invalid_rrf_k(rrf_k):
    with pytest.raises(ValueError, match="rrf_k must be a positive integer"):
        fuse_ranked({"semantic": [_hit("a", "A")]}, limit=1, rrf_k=rrf_k)


@pytest.mark.parametrize("rrf_k", [0, True, "10"])
def test_fuse_ranked_rejects_invalid_rrf_k_before_zero_limit_return(rrf_k):
    with pytest.raises(ValueError, match="rrf_k must be a positive integer"):
        fuse_ranked({"semantic": [_hit("a", "A")]}, limit=0, rrf_k=rrf_k)


@pytest.mark.parametrize("weight", [0, -1, math.inf, math.nan, "bad"])
def test_fuse_ranked_rejects_invalid_signal_weight(weight):
    with pytest.raises(ValueError, match="signal weight"):
        fuse_ranked(
            {"semantic": [_hit("a", "A")]},
            limit=1,
            signal_weights={"semantic": weight},
        )


def test_fuse_ranked_zero_limit_returns_before_validating_weights():
    assert fuse_ranked(
        {"semantic": [_hit("a", "A")]},
        limit=0,
        signal_weights={"semantic": 0},
    ) == []
