import json
from dataclasses import replace

import pytest

from iwiki_mcp import indexer, retrieval
from iwiki_mcp.engine.config import Config


def _cfg():
    return Config(base_url="http://x/v1", api_key="k", embed_model="m",
                  dimensions=2, chunk_size=512, chunk_overlap=64, summary_max=400,
                  top_k=8, score_threshold=0.0, graph_depth=2, ignore=None,
                  seed_top_k=1, bfs_top_k=10, seed_threshold=0.5)


def _seed(tmp_path, monkeypatch):
    base = tmp_path / "wiki"
    domain = base / "d"
    domain.mkdir(parents=True)
    (domain / "seed.md").write_text(
        "---\ndescription: alpha seed\n---\n# Seed\n\n"
        "## Match\nrefresh_token alpha\n\n[Graph](graph.md)\n",
        encoding="utf-8",
    )
    (domain / "graph.md").write_text(
        "---\ndescription: beta graph\n---\n# Graph\n\n## Graph\nbeta details\n",
        encoding="utf-8",
    )
    (domain / "global.md").write_text(
        "---\ndescription: beta global\n---\n# Global\n\n## Global\nalpha details\n",
        encoding="utf-8",
    )

    def fake_index_embed(cfg, texts):
        return [[1.0, 0.0] if "alpha" in text.lower() else [0.0, 1.0]
                for text in texts]

    monkeypatch.setattr(indexer, "embed_texts", fake_index_embed)
    indexer.index_domain(_cfg(), str(base), "d")
    return str(base)


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
def test_semantic_modes_embed_query_once(tmp_path, monkeypatch, mode):
    base = _seed(tmp_path, monkeypatch)
    calls = []

    def fake_query_embed(cfg, texts):
        calls.append(texts)
        return [[1.0, 0.0]]

    monkeypatch.setattr(retrieval, "embed_texts", fake_query_embed)

    retrieval.search_read(_cfg(), base, ["d"], "alpha", 5, 0.0, mode=mode)

    assert calls == [["alpha"]]


def test_lexical_mode_never_embeds_and_returns_only_lexical_hits(tmp_path, monkeypatch):
    base = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        retrieval, "embed_texts",
        lambda cfg, texts: (_ for _ in ()).throw(AssertionError("embedded query")),
    )

    hits = retrieval.search_read(
        _cfg(), base, ["d"], "refresh_token", 10, 0.0, mode="lexical"
    )

    assert hits
    assert all(hit["hit"] == "lexical" for hit in hits)


def test_hybrid_duplicate_hit_is_both(tmp_path, monkeypatch):
    base = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(retrieval, "embed_texts", lambda cfg, texts: [[1.0, 0.0]])

    hits = retrieval.search_read(
        _cfg(), base, ["d"], "refresh_token alpha", 10, 0.0, mode="hybrid"
    )

    duplicate = next(hit for hit in hits
                     if hit["file"] == "seed.md" and hit["heading"] == "Match")
    assert duplicate["hit"] == "both"


def test_semantic_includes_global_section_outside_seed_graph(tmp_path, monkeypatch):
    base = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(retrieval, "embed_texts", lambda cfg, texts: [[1.0, 0.0]])

    hits = retrieval.search_read(
        _cfg(), base, ["d"], "alpha", 10, 0.5, mode="semantic"
    )

    global_hit = next(hit for hit in hits if hit["file"] == "global.md")
    assert global_hit["source"] == "global"


def test_semantic_seed_receives_graph_page_signal_and_keeps_seed_source(
        tmp_path, monkeypatch):
    base = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(retrieval, "embed_texts", lambda cfg, texts: [[1.0, 0.0]])

    hits = retrieval.prepare_read_candidates(
        _cfg(), base, ["d"], "alpha", 10, 0.5, mode="semantic"
    )

    seed = next(hit for hit in hits if hit["file"] == "seed.md")
    assert seed["score"] == pytest.approx(2 / 61 + 1 / 62)
    assert seed["source"] == "seed"


def test_lexical_seed_expands_graph_without_embedding(tmp_path, monkeypatch):
    base = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        retrieval, "embed_texts",
        lambda cfg, texts: (_ for _ in ()).throw(AssertionError("embedded query")),
    )

    hits = retrieval.search_read(
        _cfg(), base, ["d"], "refresh_token", 10, 0.0, mode="lexical"
    )

    graph_hit = next(hit for hit in hits if hit["file"] == "graph.md")
    assert graph_hit["source"] == "graph"
    assert graph_hit["hit"] == "lexical"


def test_candidate_limit_has_floor_but_never_reduces_top_k(monkeypatch):
    monkeypatch.setattr(retrieval, "CANDIDATE_LIMIT", 2)

    assert retrieval._candidate_limit(5) == 5
    assert retrieval._candidate_limit(1) == 2


def test_invalid_mode_lists_allowed_values():
    with pytest.raises(
        ValueError,
        match="invalid search mode: bogus; allowed values: hybrid, lexical, semantic",
    ):
        retrieval.search_read(_cfg(), "base", ["d"], "q", 10, 0.0, mode="bogus")


def test_empty_request_does_not_embed(monkeypatch):
    monkeypatch.setattr(
        retrieval, "embed_texts",
        lambda cfg, texts: (_ for _ in ()).throw(AssertionError("embedded query")),
    )

    assert retrieval.prepare_read_candidates(
        _cfg(), "base", [], "q", 10, 0.0, mode="semantic"
    ) == []
    assert retrieval.prepare_read_candidates(
        _cfg(), "base", ["d"], "q", 0, 0.0, mode="semantic"
    ) == []


def test_vector_search_is_semantic_compatibility_alias(tmp_path, monkeypatch):
    base = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(retrieval, "embed_texts", lambda cfg, texts: [[1.0, 0.0]])

    hits = retrieval.vector_search(_cfg(), base, ["d"], "alpha", 10, 0.0)

    assert hits
    assert all(hit["hit"] == "semantic" for hit in hits)
    json.dumps(hits)


def test_hydrate_candidates_preserves_order_and_exact_chunks(tmp_path):
    base = tmp_path / "wiki"
    page = base / "d" / "page.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ndescription: secret frontmatter\n---\n# Page\n\n"
        "## Long\none two three four five six\n\n## Other\nno leakage\n",
        encoding="utf-8",
    )
    candidates = [
        {"domain": "d", "file": "page.md", "heading": "Long", "chunk": 1,
         "score": 0.2, "hit": "semantic", "source": "global"},
        {"domain": "d", "file": "page.md", "heading": "Long", "chunk": 0,
         "score": 0.1, "hit": "semantic", "source": "seed"},
    ]

    hydrated = retrieval.hydrate_candidates(
        replace(_cfg(), chunk_size=3, chunk_overlap=0), str(base), candidates
    )

    assert [(hit["heading"], hit["chunk"]) for hit in hydrated] == [
        ("Long", 1), ("Long", 0)
    ]
    assert hydrated[0]["text"] == "## Long\nfour five six"
    assert "frontmatter" not in hydrated[0]["text"]
    assert "Other" not in hydrated[0]["text"]


def test_hydrate_candidates_omits_stale_heading_and_missing_page(tmp_path):
    base = tmp_path / "wiki"
    page = base / "d" / "page.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Page\n\n## Current\ntext\n", encoding="utf-8")
    candidates = [
        {"domain": "d", "file": "page.md", "heading": "Stale", "chunk": 0},
        {"domain": "d", "file": "missing.md", "heading": "Current", "chunk": 0},
    ]

    assert retrieval.hydrate_candidates(_cfg(), str(base), candidates) == []
