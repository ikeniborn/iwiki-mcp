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


def test_migrate_layout_moves_flat_pages_and_rewrites_links(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch, "d")
    dom = tmp_path / "d"
    (dom / "alpha.md").write_text(
        "---\ntype: concept\ntitle: Alpha\n---\n# Alpha\n\n## S\n\nBody.\n", encoding="utf-8")
    (dom / "beta.md").write_text(
        "---\ntype: guide\ntitle: Beta\n---\n# Beta\n\n## S\n\nSee [A](alpha.md#s).\n",
        encoding="utf-8")

    res = server.wiki_migrate_okf("d")

    assert (dom / "concept" / "alpha.md").is_file()
    assert (dom / "guide" / "beta.md").is_file()
    assert not (dom / "alpha.md").exists()
    assert "(concept/alpha.md#s)" in (dom / "guide" / "beta.md").read_text()
    assert "alpha -> concept/alpha" in res["moved"]
    assert "beta -> guide/beta" in res["moved"]


def test_migrate_layout_skips_untyped_pages(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch, "d")
    dom = tmp_path / "d"
    (dom / "a.md").write_text("# A\n\n## Overview\ns\n\n## B\nwords\n", encoding="utf-8")

    res = server.wiki_migrate_okf("d")

    assert (dom / "a.md").is_file()
    assert res["moved"] == []


def test_migrate_layout_is_idempotent(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch, "d")
    dom = tmp_path / "d"
    (dom / "alpha.md").write_text(
        "---\ntype: concept\ntitle: Alpha\n---\n# Alpha\n\n## S\n\nBody.\n", encoding="utf-8")

    server.wiki_migrate_okf("d")
    res2 = server.wiki_migrate_okf("d")

    assert (dom / "concept" / "alpha.md").is_file()
    assert res2["moved"] == []


def test_migrate_layout_rejects_path_escape_via_frontmatter_type(tmp_path, monkeypatch):
    # SECURITY (holistic review finding 1): a page's frontmatter `type` is
    # attacker-controlled content, not a validated identifier. fm.normalize_type
    # only strips/lower-cases it -- it does NOT reject '/', '..', or a leading
    # '.'. Before the fix, migrate_layout joined it straight into
    # "<type>/<slug>" and move_page's os.replace() happily relocated the file
    # OUTSIDE the domain (and outside the base entirely).
    _bind(tmp_path, monkeypatch, "d")
    dom = tmp_path / "d"
    (dom / "victim.md").write_text(
        "---\ntype: ../../escape\ntitle: Victim\n---\n# Victim\n\n## S\n\nBody.\n",
        encoding="utf-8")

    res = server.wiki_migrate_okf("d")

    # must NOT have escaped the domain (two '..' from <base>/d -> <base>'s parent)
    escaped = tmp_path.parent / "escape" / "victim.md"
    assert not escaped.exists()
    assert not (tmp_path / "escape").exists()
    # the original page stays exactly where it was -- never silently moved
    assert (dom / "victim.md").is_file()
    assert res["moved"] == []
    assert any("victim" in s for s in res.get("layout_skipped_unsafe", []))
