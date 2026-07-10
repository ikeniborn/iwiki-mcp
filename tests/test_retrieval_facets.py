import iwiki_mcp.indexer as indexer
import iwiki_mcp.retrieval as retrieval
from iwiki_mcp.engine.config import Config
from iwiki_mcp.retrieval import _facet_ok


def test_facet_ok_type_and_tags():
    assert _facet_ok("api", ["a", "b"], None, None)
    assert _facet_ok("api", ["a"], "api", None)
    assert not _facet_ok("guide", ["a"], "api", None)
    assert _facet_ok("api", ["a", "b"], None, ["b"])
    assert not _facet_ok("api", ["a"], None, ["z"])
    assert not _facet_ok(None, [], "api", None)


def _cfg():
    return Config(base_url="x", api_key="x", embed_model="m", dimensions=2,
                  chunk_size=512, chunk_overlap=64, summary_max=400, top_k=8,
                  score_threshold=-1.0, graph_depth=2, ignore=None)


def _seed_two_typed_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, texts: [[1.0, 0.0] for _ in texts])
    monkeypatch.setattr(retrieval, "embed_texts", lambda cfg, texts: [[1.0, 0.0] for _ in texts])
    (tmp_path / "d" / ".iwiki").mkdir(parents=True)
    (tmp_path / "d" / "a.md").write_text(
        "---\ntype: api\ntags: [alpha]\ndescription: widget notes a\n---\n"
        "# A\n\n## Overview\ns\n\n## B\nwidget content here\n",
        encoding="utf-8")
    (tmp_path / "d" / "b.md").write_text(
        "---\ntype: guide\ntags: [beta]\ndescription: widget notes b\n---\n"
        "# B\n\n## Overview\ns\n\n## C\nwidget content here\n",
        encoding="utf-8")
    indexer.index_domain(_cfg(), str(tmp_path), "d")


def test_vector_search_filters_by_type(tmp_path, monkeypatch):
    _seed_two_typed_pages(tmp_path, monkeypatch)
    hits = retrieval.vector_search(_cfg(), str(tmp_path), ["d"], "widget", top_k=8,
                                   threshold=-1.0, type="api")
    files = {h["file"] for h in hits}
    assert files == {"a.md"}


def test_vector_search_filters_by_tags(tmp_path, monkeypatch):
    _seed_two_typed_pages(tmp_path, monkeypatch)
    hits = retrieval.vector_search(_cfg(), str(tmp_path), ["d"], "widget", top_k=8,
                                   threshold=-1.0, tags=["beta"])
    files = {h["file"] for h in hits}
    assert files == {"b.md"}


def test_lexical_search_filters_by_type(tmp_path, monkeypatch):
    _seed_two_typed_pages(tmp_path, monkeypatch)
    hits = retrieval.lexical_search(str(tmp_path), ["d"], "widget", top_k=8, type="api")
    files = {h["file"] for h in hits}
    assert files == {"a.md"}


def test_hybrid_search_respects_type_filter(tmp_path, monkeypatch):
    _seed_two_typed_pages(tmp_path, monkeypatch)
    hits = retrieval.hybrid_search(_cfg(), str(tmp_path), ["d"], "widget", top_k=8,
                                   threshold=-1.0, mode="hybrid", type="guide")
    files = {h["file"] for h in hits}
    assert files == {"b.md"}


def test_no_facet_returns_both(tmp_path, monkeypatch):
    _seed_two_typed_pages(tmp_path, monkeypatch)
    hits = retrieval.vector_search(_cfg(), str(tmp_path), ["d"], "widget", top_k=8,
                                   threshold=-1.0)
    files = {h["file"] for h in hits}
    assert files == {"a.md", "b.md"}
