import pytest

from iwiki_mcp import indexer, retrieval, server
from iwiki_mcp.engine.embed import EmbedError


def _seed(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
    (b / "backend").mkdir(parents=True)
    (b / "backend" / "auth.md").write_text(
        "---\ndescription: auth token guide\n---\n"
        "# Auth\n## Overview\no\n## Token\nrefresh_token rotates\n"
    )
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text('read = ["backend"]\nwrite = "backend"\n')
    monkeypatch.setenv("IWIKI_BASE_DIR", str(b))
    monkeypatch.setenv("IWIKI_PROJECT_DIR", str(proj))
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", "2")
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, t: [[1.0, 0.0] for _ in t])
    monkeypatch.setattr(retrieval, "embed_texts", lambda cfg, t: [[1.0, 0.0]])
    indexer.index_domain(
        __import__("iwiki_mcp.engine.config", fromlist=["Config"]).Config.load(),
        str(b),
        "backend",
    )
    return str(b)


def test_search_returns_results(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_search("token", scope="project", threshold=0.0)
    assert "results" in out and out["results"]
    assert out["results"][0]["domain"] == "backend"


def test_search_lexical_mode(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_search("refresh_token", mode="lexical")
    assert any(r["hit"] == "lexical" for r in out["results"])


def test_search_rejects_hidden_explicit_domain(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    hidden = tmp_path / "wiki" / ".secret"
    hidden.mkdir()
    (hidden / "hidden.md").write_text("# Hidden\n## Token\nhidden_token\n")

    out = server.wiki_search(
        "hidden_token",
        mode="lexical",
        domains=[".secret"],
    )

    assert "error" in out
    assert "results" not in out


def test_related_returns_vector_and_graph_keys(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_related("backend", "auth.md#Token")
    assert "vector" in out
    assert "graph" in out


def test_related_graph_fallback_reads_domain_relative_files(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    backend = tmp_path / "wiki" / "backend"
    (backend / "auth.md").write_text(
        "# Auth\n## Overview\no\n## Token\nrefresh_token rotates [[other.md]]\n"
    )
    (backend / "other.md").write_text("# Other\n")

    out = server.wiki_related("backend", "auth.md#Token")

    assert out["vector"] == []
    assert "other" in out["graph"]


def test_related_rejects_hidden_domain(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_related(".secret", "hidden.md#Token")
    assert "error" in out


def test_search_preserves_explicit_zero_k(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_search("token", scope="project", k=0, threshold=0.0)
    assert out["results"] == []


@pytest.mark.parametrize("mode", ["hybrid", "lexical", "semantic"])
def test_search_accepts_canonical_modes(tmp_path, monkeypatch, mode):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_search("token", mode=mode, threshold=0.0)
    assert "error" not in out
    assert "results" in out


def test_search_rejects_vector_with_allowed_values(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_search("token", mode="vector")
    assert "error" in out
    assert "hybrid, lexical, semantic" in out["error"]


@pytest.mark.parametrize("configured", ["lexical", "semantic"])
def test_omitted_mode_uses_environment_default(tmp_path, monkeypatch, configured):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv("IWIKI_SEARCH_MODE", configured)
    captured = {}

    def capture(*args, mode, **kwargs):
        captured["mode"] = mode
        return []

    monkeypatch.setattr(server.retrieval, "prepare_read_candidates", capture)
    server.wiki_search("token")
    assert captured["mode"] == configured


def test_explicit_mode_overrides_environment_default(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv("IWIKI_SEARCH_MODE", "lexical")
    captured = {}

    def capture(*args, mode, **kwargs):
        captured["mode"] = mode
        return []

    monkeypatch.setattr(server.retrieval, "prepare_read_candidates", capture)
    server.wiki_search("token", mode="semantic")
    assert captured["mode"] == "semantic"


def test_disabled_reranker_keeps_existing_top_level_shape(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.delenv("IWIKI_RERANK_MODEL", raising=False)
    monkeypatch.setattr(
        server.rerank, "rerank_candidates",
        lambda *args: (_ for _ in ()).throw(AssertionError("reranker called")),
    )
    out = server.wiki_search("token", threshold=0.0)
    assert set(out) == {"results"}


def test_configured_reranker_adds_metadata(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv("IWIKI_RERANK_MODEL", "model")

    captured = {}

    def rerank(cfg, query, candidates, top_n):
        captured["top_n"] = top_n
        return (
            [{k: v for k, v in item.items() if k != "text"}
             for item in reversed(candidates)],
            {"applied": True},
        )

    monkeypatch.setattr(
        server.rerank, "rerank_candidates",
        rerank,
    )
    out = server.wiki_search("token", k=3, threshold=0.0)
    assert out["rerank"] == {"applied": True}
    assert all("text" not in item for item in out["results"])
    assert captured["top_n"] == 3


def test_reranker_can_promote_candidate_below_preliminary_top_k(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv("IWIKI_RERANK_MODEL", "model")
    candidates = [
        {"domain": "backend", "file": f"{name}.md", "heading": "H", "chunk": 0,
         "score": score, "hit": "semantic", "source": "global"}
        for name, score in (("first", 0.3), ("second", 0.2), ("promoted", 0.1))
    ]
    monkeypatch.setattr(
        server.retrieval, "prepare_read_candidates", lambda *args, **kwargs: candidates,
    )
    monkeypatch.setattr(
        server.retrieval, "hydrate_candidates",
        lambda cfg, base, items: [{**item, "text": item["file"]} for item in items],
    )
    monkeypatch.setattr(
        server.rerank, "rerank_candidates",
        lambda cfg, query, items, top_n: (
            [{key: value for key, value in item.items() if key != "text"}
             for item in reversed(items)],
            {"applied": True},
        ),
    )
    out = server.wiki_search("token", k=2)
    assert [item["file"] for item in out["results"]] == ["promoted.md", "second.md"]


def test_reranker_failure_preserves_complete_preliminary_order(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv("IWIKI_RERANK_MODEL", "model")
    preliminary = [
        {"domain": "backend", "file": "auth.md", "heading": "Token", "chunk": 0,
         "score": 0.7, "hit": "both", "source": "seed"},
        {"domain": "backend", "file": "stale.md", "heading": "Missing", "chunk": 0,
         "score": 0.6, "hit": "semantic", "source": "global"},
    ]
    monkeypatch.setattr(
        server.retrieval, "prepare_read_candidates", lambda *args, **kwargs: preliminary,
    )
    monkeypatch.setattr(
        server.rerank, "rerank_candidates",
        lambda cfg, query, candidates, top_n: (
            [{key: value for key, value in item.items() if key != "text"}
             for item in candidates],
            {"applied": False, "warning": "reranker unavailable"},
        ),
    )
    out = server.wiki_search("token")
    assert out["results"] == preliminary
    assert out["rerank"] == {"applied": False, "warning": "reranker unavailable"}


def test_partial_rerank_preserves_all_unscored_preliminary_order(
    tmp_path, monkeypatch
):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv("IWIKI_RERANK_MODEL", "model")
    preliminary = [
        {"domain": "backend", "file": "stale-a.md", "heading": "H", "chunk": 0,
         "score": 0.3, "hit": "semantic", "source": "global"},
        {"domain": "backend", "file": "scored-b.md", "heading": "H", "chunk": 0,
         "score": 0.2, "hit": "semantic", "source": "global"},
        {"domain": "backend", "file": "unscored-c.md", "heading": "H", "chunk": 0,
         "score": 0.1, "hit": "semantic", "source": "global"},
    ]
    monkeypatch.setattr(
        server.retrieval, "prepare_read_candidates",
        lambda *args, **kwargs: preliminary,
    )
    monkeypatch.setattr(
        server.retrieval, "hydrate_candidates",
        lambda cfg, base, items: [
            {**item, "text": item["file"]} for item in items[1:]
        ],
    )
    monkeypatch.setattr(
        server.rerank, "rerank_candidates",
        lambda cfg, query, candidates, top_n: (
            [
                {key: value for key, value in candidates[0].items() if key != "text"},
                {key: value for key, value in candidates[1].items() if key != "text"},
            ],
            {"applied": True, "_scored_count": 1},
        ),
    )

    out = server.wiki_search("token", k=3)

    assert [item["file"] for item in out["results"]] == [
        "scored-b.md", "stale-a.md", "unscored-c.md"
    ]
    assert out["rerank"] == {"applied": True}


def test_configured_reranker_with_no_hydrated_candidates_fails_soft(
    tmp_path, monkeypatch
):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv("IWIKI_RERANK_MODEL", "model")
    preliminary = [
        {"domain": "backend", "file": "stale.md", "heading": "Gone", "chunk": 0,
         "score": 0.5, "hit": "semantic", "source": "global"}
    ]
    monkeypatch.setattr(
        server.retrieval, "prepare_read_candidates", lambda *args, **kwargs: preliminary,
    )
    monkeypatch.setattr(server.retrieval, "hydrate_candidates", lambda *args: [])
    out = server.wiki_search("token")
    assert out["results"] == preliminary
    assert out["rerank"] == {"applied": False, "warning": "reranker unavailable"}


def test_embedding_error_remains_visible_when_reranker_is_configured(
    tmp_path, monkeypatch
):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv("IWIKI_RERANK_MODEL", "model")
    monkeypatch.setattr(
        server.retrieval, "prepare_read_candidates",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            EmbedError("embedding unavailable")
        ),
    )
    monkeypatch.setattr(
        server.rerank, "rerank_candidates",
        lambda *args: (_ for _ in ()).throw(AssertionError("reranker called")),
    )
    assert server.wiki_search("token") == {"error": "embedding unavailable"}
