import iwiki_mcp.indexer as indexer
from iwiki_mcp.engine.config import Config
from iwiki_mcp.engine.store import VectorStore
from iwiki_mcp.base import index_path


def _cfg():
    return Config(base_url="x", api_key="x", embed_model="m",
                  dimensions=2, chunk_size=512, chunk_overlap=64, summary_max=400,
                  top_k=8, score_threshold=0.2, graph_depth=2, ignore=None)


def test_reindex_refreshes_facets_without_reembed(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, texts: [[0.1, 0.2] for _ in texts])
    base = tmp_path
    (base / "d" / ".iwiki").mkdir(parents=True)
    page = base / "d" / "p.md"
    page.write_text("---\ntype: api\ntags: [a]\n---\n# T\n\n## Overview\ns\n\n## B\nwords here\n", encoding="utf-8")
    indexer.index_domain(_cfg(), str(base), "d")
    # change only the frontmatter (body/hash unchanged)
    page.write_text("---\ntype: guide\ntags: [b]\n---\n# T\n\n## Overview\ns\n\n## B\nwords here\n", encoding="utf-8")
    stats = indexer.index_domain(_cfg(), str(base), "d")
    recs = VectorStore(index_path(str(base), "d")).load()
    assert recs and all(r.type == "guide" for r in recs)
    assert all(r.tags == ["b"] for r in recs)
    assert stats["reused"] >= 1  # body unchanged -> not re-embedded
