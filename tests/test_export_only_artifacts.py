import os

from iwiki_mcp import indexer, server


def _bind(tmp_path, monkeypatch, dom):
    b = tmp_path
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text(f'read = ["{dom}"]\nwrite = "{dom}"\n')
    monkeypatch.setenv("IWIKI_BASE_DIR", str(b))
    monkeypatch.setenv("IWIKI_PROJECT_DIR", str(proj))
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", "2")
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, t: [[1.0, 0.0] for _ in t])
    os.makedirs(tmp_path / dom, exist_ok=True)
    return str(b)


def test_write_emits_no_okf_artifacts_but_export_does(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch, "d")
    server.wiki_write_page("d", "x", "# X\n\n## Purpose\n\nBody.\n", type="guide")
    assert not (tmp_path / "d" / "index.md").exists()
    assert not (tmp_path / "d" / "log.md").exists()
    server.wiki_export_okf("d")
    assert (tmp_path / "d" / "index.md").is_file()
    assert (tmp_path / "d" / "log.md").is_file()
