import iwiki_mcp.server as server


def test_wiki_search_passes_facets(monkeypatch):
    captured = {}

    def fake_hybrid(cfg, base, doms, query, top_k, threshold, mode, type=None, tags=None):
        captured.update(type=type, tags=tags)
        return []

    class FakeConfig:
        top_k = 10
        score_threshold = 0.5

    monkeypatch.setattr(server.retrieval, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(
        server.base, "resolve_binding",
        lambda: server.base.Binding(
            base="/b", read=("d",), write="d", project_dir="/p"
        )
    )
    monkeypatch.setattr(server.base, "resolve_scope", lambda bind, scope, doms: ["d"])
    monkeypatch.setattr(server.Config, "load", staticmethod(lambda: FakeConfig()))

    server.wiki_search("q", type="api", tags=["x"])
    assert captured == {"type": "api", "tags": ["x"]}


def test_wiki_search_normalizes_facets(monkeypatch):
    captured = {}

    def fake_hybrid(cfg, base, doms, query, top_k, threshold, mode, type=None, tags=None):
        captured.update(type=type, tags=tags)
        return []

    class FakeConfig:
        top_k = 10
        score_threshold = 0.5

    monkeypatch.setattr(server.retrieval, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(
        server.base, "resolve_binding",
        lambda: server.base.Binding(
            base="/b", read=("d",), write="d", project_dir="/p"
        )
    )
    monkeypatch.setattr(server.base, "resolve_scope", lambda bind, scope, doms: ["d"])
    monkeypatch.setattr(server.Config, "load", staticmethod(lambda: FakeConfig()))

    server.wiki_search("q", type="API", tags=["Config"])
    assert captured == {"type": "api", "tags": ["config"]}
