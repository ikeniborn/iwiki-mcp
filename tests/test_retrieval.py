import pytest

from iwiki_mcp import retrieval, indexer, base
from iwiki_mcp.engine.config import Config


def _cfg():
    return Config(base_url="http://x/v1", api_key="k", embed_model="m",
                  dimensions=2, chunk_size=512, chunk_overlap=64, summary_max=400,
                  top_k=8, score_threshold=0.0, graph_depth=2, ignore=None)


def _seed(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
    for d, body in (("a", "alpha refresh_token here"), ("b", "beta gamma")):
        (b / d / ".iwiki").mkdir(parents=True)
        (b / d / "p.md").write_text(f"# P\n## Overview\no\n## S\n{body}\n")
    monkeypatch.setattr(indexer, "embed_texts",
                        lambda cfg, texts: [[1.0, 0.0] for _ in texts])
    indexer.index_domain(_cfg(), str(b), "a")
    indexer.index_domain(_cfg(), str(b), "b")
    return str(b)


def test_vector_search_merges_domains(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(retrieval, "embed_texts",
                        lambda cfg, texts: [[1.0, 0.0]])
    hits = retrieval.vector_search(_cfg(), b, ["a", "b"], "q", top_k=10, threshold=0.0)
    assert {h["domain"] for h in hits} == {"a", "b"}
    assert all(h["hit"] == "vector" for h in hits)


def test_vector_search_empty_domains_does_not_embed(monkeypatch):
    monkeypatch.setattr(
        retrieval, "embed_texts",
        lambda cfg, texts: (_ for _ in ()).throw(AssertionError("embedded query")),
    )

    assert retrieval.vector_search(
        _cfg(), "base", [], "q", top_k=10, threshold=0.0
    ) == []


def test_hybrid_adds_lexical(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(retrieval, "embed_texts",
                        lambda cfg, texts: [[0.0, 1.0]])   # orthogonal -> no vector hits
    hits = retrieval.hybrid_search(_cfg(), b, ["a", "b"], "refresh_token",
                                   top_k=10, threshold=0.99, mode="hybrid")
    assert any(h["hit"] == "lexical" and h["domain"] == "a" for h in hits)


def test_hybrid_preserves_best_vector_duplicate(monkeypatch):
    monkeypatch.setattr(
        retrieval, "vector_search",
        lambda cfg, base, domains, query, top_k, threshold, type=None, tags=None: [
            {"domain": "a", "file": "p.md", "heading": "S", "chunk": 1,
             "score": 0.9, "hit": "vector"},
            {"domain": "a", "file": "p.md", "heading": "S", "chunk": 2,
             "score": 0.4, "hit": "vector"},
        ],
    )
    monkeypatch.setattr(
        retrieval, "lexical_search",
        lambda base, domains, query, top_k, type=None, tags=None: [],
    )

    hits = retrieval.hybrid_search(
        _cfg(), "base", ["a"], "q", top_k=10, threshold=0.0, mode="hybrid"
    )

    assert hits[0]["score"] == 0.9
    assert hits[0]["chunk"] == 1


def test_hybrid_rejects_invalid_mode():
    with pytest.raises(ValueError, match="invalid search mode: bogus"):
        retrieval.hybrid_search(
            _cfg(), "base", ["a"], "q", top_k=10, threshold=0.0, mode="bogus"
        )
