import os

from iwiki_mcp import base, indexer, server


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


def test_read_update_delete_by_type_slug(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch, "d")
    write_res = server.wiki_write_page(
        "d", "cfg", "# Cfg\n\n## Purpose\n\nBody.\n", type="reference")
    assert write_res["page"] == "d/reference/cfg.md"
    assert (tmp_path / "d" / "reference" / "cfg.md").is_file()

    # read/update/delete address the page by its full <type>/<slug> identity.
    read_res = server.wiki_read_page("d", "reference/cfg")
    assert "error" not in read_res
    assert "Body." in read_res["markdown"]

    server.wiki_update_page("d", "reference/cfg", "Purpose", "New body.\n")
    assert "New body." in server.wiki_read_page("d", "reference/cfg")["markdown"]

    server.wiki_delete_page("d", "reference/cfg")
    assert "error" in server.wiki_read_page("d", "reference/cfg")
