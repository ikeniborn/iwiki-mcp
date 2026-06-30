from iwiki_mcp import base, server


def _seed(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
    (b / "backend" / ".iwiki").mkdir(parents=True)
    (b / "backend" / "auth.md").write_text("# Auth\n## Overview\no\n## Flow\nx\n")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text('read = ["backend"]\nwrite = "backend"\n')
    monkeypatch.setenv("IWIKI_BASE_DIR", str(b))
    monkeypatch.setenv("IWIKI_PROJECT_DIR", str(proj))
    return str(b)


def test_status(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_status()
    assert out["write"] == "backend"
    assert "backend" in out["domains"]


def test_list_domains_and_pages(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    assert "backend" in server.wiki_list_domains()["domains"]
    pages = server.wiki_list_pages("backend")["pages"]
    assert any(p["slug"] == "auth" for p in pages)


def test_read_page(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    md = server.wiki_read_page("backend", "auth")["markdown"]
    assert "## Flow" in md


def test_read_missing_page(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_read_page("backend", "nope")
    assert "error" in out


def test_status_no_base(monkeypatch):
    monkeypatch.delenv("IWIKI_BASE_DIR", raising=False)
    monkeypatch.setenv("IWIKI_PROJECT_DIR", "/tmp/does-not-exist-iwiki")
    out = server.wiki_status()
    assert "error" in out
