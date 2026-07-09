import os

from iwiki_mcp import base, indexer, server
from iwiki_mcp.engine.store import VectorStore


def _seed(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
    (b / "backend" / ".iwiki").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text('read = ["backend"]\nwrite = "backend"\n')
    monkeypatch.setenv("IWIKI_BASE_DIR", str(b))
    monkeypatch.setenv("IWIKI_PROJECT_DIR", str(proj))
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", "2")
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, t: [[1.0, 0.0] for _ in t])
    return str(b)


def test_write_refreshes_okf_artifacts(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    server.wiki_write_page("backend", "auth", "# Auth\n\n## Overview\ns\n\n## Flow\nx\n")
    dom = os.path.join(b, "backend")
    assert os.path.isfile(os.path.join(dom, "index.md"))
    assert os.path.isfile(os.path.join(dom, "log.md"))
    assert "[auth](auth.md)" in open(os.path.join(dom, "index.md"), encoding="utf-8").read()


def test_reserved_files_not_indexed(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    server.wiki_write_page("backend", "auth", "# Auth\n\n## Overview\ns\n\n## Flow\nx\n")
    server.wiki_index("backend")            # reindex with index.md/log.md present
    recs = VectorStore(base.index_path(b, "backend")).load()
    assert all(r.file not in ("index.md", "log.md") for r in recs)


def test_delete_refreshes_index(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    server.wiki_write_page("backend", "auth", "# Auth\n\n## Overview\ns\n\n## Flow\nx\n")
    server.wiki_write_page("backend", "db", "# DB\n\n## Overview\ns\n\n## Schema\nx\n")
    server.wiki_delete_page("backend", "auth")
    idx = open(os.path.join(b, "backend", "index.md"), encoding="utf-8").read()
    assert "[db](db.md)" in idx and "auth.md" not in idx


def test_write_rejects_reserved_slug(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    out = server.wiki_write_page("backend", "index", "# I\n\n## Overview\nx\n")
    assert "error" in out and "reserved" in out["error"]
    assert not os.path.isfile(os.path.join(b, "backend", "index.md"))
