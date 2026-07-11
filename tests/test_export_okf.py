import os

from iwiki_mcp import indexer, server


def _seed(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
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


def test_export_okf_sweep_adds_frontmatter_and_converts_links(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    dom = os.path.join(b, "backend")
    with open(os.path.join(dom, "a.md"), "w", encoding="utf-8") as fh:
        fh.write("# A\n\n## Overview\ns\n\n## B\nsee [[a#B]]\n")
    out = server.wiki_export_okf("backend")
    assert out["domain"] == "backend"
    assert "a" in out["added_frontmatter"]
    assert "a" in out["fixed_links"]
    text = open(os.path.join(dom, "a.md"), encoding="utf-8").read()
    assert text.startswith("---\n")
    assert "type: concept" in text
    assert "[B](a.md#b)" in text                 # wikilink converted in-place
    assert os.path.isfile(os.path.join(dom, "index.md"))
    assert os.path.isfile(os.path.join(dom, "log.md"))


def test_export_okf_idempotent(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    dom = os.path.join(b, "backend")
    with open(os.path.join(dom, "a.md"), "w", encoding="utf-8") as fh:
        fh.write("# A\n\n## Overview\ns\n")
    server.wiki_export_okf("backend")
    first = open(os.path.join(dom, "a.md"), encoding="utf-8").read()
    out2 = server.wiki_export_okf("backend")
    second = open(os.path.join(dom, "a.md"), encoding="utf-8").read()
    assert first == second
    assert out2["added_frontmatter"] == []
    assert out2["fixed_links"] == []


def test_export_okf_preserves_existing_type(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    dom = os.path.join(b, "backend")
    with open(os.path.join(dom, "a.md"), "w", encoding="utf-8") as fh:
        fh.write("---\ntype: api\n---\n# A\n\n## Overview\ns\n")
    server.wiki_export_okf("backend")
    text = open(os.path.join(dom, "a.md"), encoding="utf-8").read()
    assert "type: api" in text


def test_export_okf_migrates_overview_and_status(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    dom = os.path.join(b, "backend")
    # legacy page: frontmatter present, empty description, ## Overview in body, no status
    with open(os.path.join(dom, "a.md"), "w", encoding="utf-8") as fh:
        fh.write("---\ntype: api\n---\n# A\n\n## Overview\nsummary text\n\n## B\nwords\n")
    server.wiki_export_okf("backend")
    text = open(os.path.join(dom, "a.md"), encoding="utf-8").read()
    assert "## Overview" not in text                 # section removed
    assert "description: summary text" in text       # backfilled from Overview
    assert "status: stub" in text                    # defaulted
    assert "type: api" in text                        # preserved
    # idempotent on re-run
    first = text
    server.wiki_export_okf("backend")
    assert open(os.path.join(dom, "a.md"), encoding="utf-8").read() == first
