import os

from iwiki_mcp import base, indexer, server


def _seed(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
    b.mkdir()
    (b / "backend").mkdir(parents=True)
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


def _write():
    return server.wiki_write_page(
        "backend", "auth", "# Auth\n## Overview\nsummary\n## Flow\nlogin then token\n"
    )


def test_delete_removes_file_log_and_index_records(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    _write()
    # no type/chat model -> default "concept"; addressed by full identity.
    out = server.wiki_delete_page("backend", "concept/auth")
    assert out["deleted"] == "backend/concept/auth.md"
    assert not os.path.exists(os.path.join(b, "backend", "concept", "auth.md"))
    log_text = open(base.log_path(b, "backend"), encoding="utf-8").read()
    assert '"op": "delete"' in log_text
    ip = base.index_path(b, "backend")
    index_text = open(ip, encoding="utf-8").read() if os.path.exists(ip) else ""
    assert "auth.md" not in index_text


def test_delete_last_page_leaves_empty_index(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    _write()
    out = server.wiki_delete_page("backend", "concept/auth")
    assert out["indexed_chunks"] == 0


def test_delete_missing_page_errors(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_delete_page("backend", "ghost")
    assert "error" in out and "not found" in out["error"]


def test_delete_unknown_domain_errors(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_delete_page("nope", "auth")
    assert "error" in out


def test_delete_rolls_back_on_index_failure(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    _write()
    monkeypatch.setattr(
        indexer,
        "index_domain",
        lambda cfg, base, domain: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    out = server.wiki_delete_page("backend", "concept/auth")
    assert "error" in out
    assert os.path.exists(os.path.join(b, "backend", "concept", "auth.md"))
    log_text = open(base.log_path(b, "backend"), encoding="utf-8").read()
    assert '"op": "delete"' not in log_text


def test_delete_invalid_slug_errors(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_delete_page("backend", "../escape")
    assert "error" in out
