import iwiki_mcp.indexer as indexer
import iwiki_mcp.retrieval as retrieval
from iwiki_mcp.engine.config import Config
from iwiki_mcp.retrieval import _facet_ok


def _cfg():
    return Config(base_url="x", api_key="x", embed_model="m", dimensions=2,
                  chunk_size=512, chunk_overlap=64, summary_max=400, top_k=8,
                  score_threshold=-1.0, graph_depth=2, ignore=None)


def test_facet_ok_type_and_tags():
    assert _facet_ok("api", ["a", "b"], None, None)
    assert _facet_ok("api", ["a"], "api", None)
    assert not _facet_ok("guide", ["a"], "api", None)
    assert _facet_ok("api", ["a", "b"], None, ["b"])
    assert not _facet_ok("api", ["a"], None, ["z"])
    assert not _facet_ok(None, [], "api", None)


def test_hybrid_facets_filter_every_signal_including_graph(tmp_path, monkeypatch):
    domain = tmp_path / "d"
    (domain / "guide").mkdir(parents=True)
    (domain / "api").mkdir(parents=True)
    (domain / "guide" / "allowed.md").write_text(
        "---\ntype: guide\ntags: [safe]\ndescription: widget allowed\n---\n"
        "# Allowed\n\n## Allowed\nwidget content\n\n"
        "[Blocked](api/blocked.md)\n",
        encoding="utf-8",
    )
    (domain / "api" / "blocked.md").write_text(
        "---\ntype: api\ntags: [unsafe]\ndescription: widget blocked\n---\n"
        "# Blocked\n\n## Blocked\nwidget content\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(indexer, "embed_texts",
                        lambda cfg, texts: [[1.0, 0.0] for _ in texts])
    indexer.index_domain(_cfg(), str(tmp_path), "d")
    monkeypatch.setattr(retrieval, "embed_texts",
                        lambda cfg, texts: [[1.0, 0.0]])

    hits = retrieval.prepare_read_candidates(
        _cfg(), str(tmp_path), ["d"], "widget", 8, -1.0,
        mode="hybrid", type="guide", tags=["safe"],
    )

    assert hits
    assert {hit["file"] for hit in hits} == {"guide/allowed.md"}
