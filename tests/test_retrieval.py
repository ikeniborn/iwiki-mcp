import json

import pytest

from iwiki_mcp import retrieval, indexer
from iwiki_mcp.engine.config import Config


def _cfg():
    return Config(base_url="http://x/v1", api_key="k", embed_model="m",
                  dimensions=2, chunk_size=512, chunk_overlap=64, summary_max=400,
                  top_k=8, score_threshold=0.0, graph_depth=2, ignore=None)


def _seed(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
    for d, body in (("a", "alpha refresh_token here"), ("b", "beta gamma")):
        (b / d / ".iwiki").mkdir(parents=True)
        (b / d / "p.md").write_text(
            f"---\ndescription: {d} page summary\n---\n# P\n## Overview\no\n## S\n{body}\n")
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
    # a pure-grep hit ran no graph expansion; it must not be mislabeled "graph"
    assert all(h["source"] == "lexical" for h in hits if h["hit"] == "lexical")


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


def test_hierarchical_vector_returns_pool_sections_with_source(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
    (b / "d" / ".iwiki").mkdir(parents=True)
    (b / "d" / "a.md").write_text(
        "---\ndescription: alpha topic overview\n---\n"
        "# A\n\n## Alpha\nalpha topic details\n\n[B](b.md)\n"
    )
    (b / "d" / "b.md").write_text(
        "---\ndescription: unrelated other page\n---\n"
        "# B\n\n## Beta\nbeta topic details\n"
    )
    monkeypatch.setattr(indexer, "embed_texts",
                        lambda cfg, texts: [[1.0, 0.0] for _ in texts])
    indexer.index_domain(_cfg(), str(b), "d")
    monkeypatch.setattr(retrieval, "embed_texts", lambda cfg, texts: [[1.0, 0.0]])

    hits = retrieval.hybrid_search(_cfg(), str(b), ["d"], "alpha topic",
                                   top_k=5, threshold=0.0, mode="vector")
    files = {h["file"] for h in hits}
    assert "a.md" in files                      # seed article's section
    assert all("source" in h for h in hits)      # source tag present


def test_vector_hybrid_score_is_a_json_serializable_float(tmp_path, monkeypatch):
    """Regression: query vectors are cast through numpy (np.float32), which
    must not leak into hit['score'] — FastMCP's JSON encoder stringifies
    numpy scalars instead of emitting a number."""
    b = tmp_path / "wiki"
    (b / "d" / ".iwiki").mkdir(parents=True)
    (b / "d" / "a.md").write_text(
        "---\ndescription: alpha topic overview\n---\n"
        "# A\n\n## Alpha\nalpha topic details\n\n[B](b.md)\n"
    )
    (b / "d" / "b.md").write_text(
        "---\ndescription: beta unrelated overview\n---\n"
        "# B\n\n## Beta\nbeta topic details\n"
    )

    def _fake_embed(cfg, texts):
        # Distinct embeddings per page, so seed (a, cos=1.0) and graph
        # (b, cos=0.0, pulled in only via the a->b link) are genuinely
        # different, not the identical-vector fixtures used elsewhere.
        return [([1.0, 0.0] if "alpha" in t.lower() else [0.0, 1.0]) for t in texts]

    monkeypatch.setattr(indexer, "embed_texts", _fake_embed)
    indexer.index_domain(_cfg(), str(b), "d")
    monkeypatch.setattr(retrieval, "embed_texts", lambda cfg, texts: [[1.0, 0.0]])

    hits = retrieval.hybrid_search(_cfg(), str(b), ["d"], "alpha topic",
                                   top_k=5, threshold=0.0, mode="vector")

    assert {h["source"] for h in hits} == {"seed", "graph"}  # real seed vs graph
    assert all(isinstance(h["score"], float) for h in hits)
    json.dumps(hits)  # must not raise / must not silently stringify numbers
