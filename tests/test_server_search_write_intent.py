import os

from iwiki_mcp import base, indexer, retrieval, server


def _bind(tmp_path, monkeypatch, dom):
    os.makedirs(tmp_path / dom, exist_ok=True)
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", "2")
    monkeypatch.setattr(
        base, "resolve_binding",
        lambda: base.Binding(base=str(tmp_path), read=(dom,), write=dom,
                             project_dir=str(tmp_path)),
    )
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, t: [[1.0, 0.0] for _ in t])
    monkeypatch.setattr(retrieval, "embed_texts", lambda cfg, t: [[1.0, 0.0] for _ in t])


def test_search_write_intent_returns_single_target(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch, "d")    # local server-bind helper (env+binding+embed+makedirs)
    server.wiki_write_page("d", "retrieval",
                           "# Retrieval\n\n## Purpose\n\nBody.\n", type="architecture",
                           description="Explains the purpose of retrieval and how "
                                       "search locates a precise write target.")
    res = server.wiki_search("purpose of retrieval", intent="write", heading="Purpose")
    assert "target" in res
    assert res["target"]["exists"] is True
    assert res["target"]["heading"] == "Purpose"
    # a heading absent from the page is a genuine miss
    miss = server.wiki_search("purpose of retrieval", intent="write", heading="Nope")
    assert miss["target"]["exists"] is False
    # read intent unchanged
    assert "results" in server.wiki_search("retrieval")
