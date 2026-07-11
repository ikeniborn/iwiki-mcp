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


def test_write_places_page_under_type_dir(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch, "d")              # local harness (see Global Constraints)
    res = server.wiki_write_page("d", "retrieval",
                                 "# Retrieval\n\n## Purpose\n\nBody.\n", type="architecture")
    assert res["page"] == "d/architecture/retrieval.md"
    assert (tmp_path / "d" / "architecture" / "retrieval.md").is_file()


def test_write_rejects_type_segment_mismatch(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch, "d")
    res = server.wiki_write_page("d", "guide/retrieval",
                                 "# R\n\n## Purpose\n\nBody.\n", type="architecture")
    assert "error" in res and "type" in res["error"].lower()


def test_write_rejects_unsafe_type_segment(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch, "d")
    res = server.wiki_write_page("d", "retrieval",
                                 "# R\n\n## Purpose\n\nBody.\n", type="a/b")
    assert "error" in res and "type" in res["error"].lower()
