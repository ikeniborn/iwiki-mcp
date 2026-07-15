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

    fused = fuse_ranked({"one": [hit, hit]}, limit=2)

    assert len(fused) == 1
    assert fused[0]["score"] == 1 / 61
