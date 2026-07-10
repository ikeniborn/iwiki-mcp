import json
import os

from iwiki_mcp import base, indexer, server


def _seed(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
    b.mkdir()
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
    return str(b), str(proj)


def _write(md, source=None):
    return server.wiki_write_page("backend", "auth", md, source=source)


BASE_MD = "# Auth\n## Overview\nsummary\n## Flow\nlogin then token\n"


def test_update_edits_section_and_returns_pushed_key(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch)
    _write(BASE_MD)
    out = server.wiki_update_page("backend", "auth", "Flow", "refreshed flow text")
    assert out["page"] == "backend/auth.md"
    assert out["heading"] == "Flow"
    assert "pushed" in out and "committed" in out
    content = open(os.path.join(b, "backend", "auth.md"), encoding="utf-8").read()
    assert "refreshed flow text" in content
    assert "login then token" not in content


def test_update_page_not_found(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_update_page("backend", "nope", "Flow", "x")
    assert "error" in out and "not found" in out["error"]


def test_update_missing_heading(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    _write(BASE_MD)
    out = server.wiki_update_page("backend", "auth", "Nonexistent", "y")
    assert "error" in out and "not found" in out["error"]


def test_update_rejects_deep_heading_in_body(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    _write(BASE_MD)
    out = server.wiki_update_page("backend", "auth", "Flow", "### too deep\ny")
    assert "error" in out


def test_update_upserts_log_when_source_given(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch)
    src = tmp_path / "src.txt"
    src.write_text("v1")
    _write(BASE_MD, source=str(src))
    src.write_text("v2")

    out = server.wiki_update_page("backend", "auth", "Flow", "new", source=str(src))
    assert "error" not in out

    text = open(base.log_path(b, "backend"), encoding="utf-8").read()
    recs = [json.loads(line) for line in text.splitlines() if line.strip()]
    ingest = [r for r in recs if r.get("op") == "ingest" and r["page"] == "auth.md"]
    assert len(ingest) == 1
    assert ingest[0]["source"] == str(src)


def test_update_rolls_back_file_and_log_on_index_failure(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch)
    src = tmp_path / "src.txt"
    src.write_text("v1")
    _write(BASE_MD, source=str(src))
    log_before = open(base.log_path(b, "backend"), encoding="utf-8").read()

    monkeypatch.setattr(
        indexer, "index_domain",
        lambda cfg, base, domain: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    src.write_text("v2")
    out = server.wiki_update_page("backend", "auth", "Flow", "newbody", source=str(src))

    assert "error" in out
    content = open(os.path.join(b, "backend", "auth.md"), encoding="utf-8").read()
    assert "login then token" in content and "newbody" not in content
    assert open(base.log_path(b, "backend"), encoding="utf-8").read() == log_before


def test_update_removes_log_it_created_on_rollback(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch)
    # page exists on disk but NO ingest log yet (log-less page)
    page = os.path.join(b, "backend", "auth.md")
    with open(page, "w", encoding="utf-8") as fh:
        fh.write(BASE_MD)
    log_file = base.log_path(b, "backend")
    assert not os.path.exists(log_file)

    src = tmp_path / "src.txt"
    src.write_text("v1")
    monkeypatch.setattr(
        indexer, "index_domain",
        lambda cfg, base, domain: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    out = server.wiki_update_page("backend", "auth", "Flow", "newbody", source=str(src))

    assert "error" in out
    assert open(page, encoding="utf-8").read() == BASE_MD          # file restored
    assert not os.path.exists(log_file)                            # log removed on rollback


def test_update_normalizes_wikilinks_in_edited_section(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch)
    _write(BASE_MD)
    server.wiki_update_page("backend", "auth", "Flow", "see [[core|the core]] now")
    content = open(os.path.join(b, "backend", "auth.md"), encoding="utf-8").read()
    assert "[the core](core.md)" in content
    assert "[[core|the core]]" not in content


def test_update_sets_description_and_status(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch)
    server.wiki_write_page("backend", "alice", "# Alice\n\n## Role\nwork.\n",
                           source=None, type="person", description="old desc")
    res = server.wiki_update_page("backend", "alice", "Role", "new role prose.\n",
                                  description="new desc", status="deprecated")
    assert "error" not in res
    path = os.path.join(b, "backend", "alice.md")
    content = open(path, encoding="utf-8").read()
    meta, _ = server._fm.split(content)
    assert meta["description"] == "new desc"
    assert meta["status"] == "deprecated"
