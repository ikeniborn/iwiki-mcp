from pathlib import Path

from iwiki_mcp import base, indexer, okf, server
from iwiki_mcp.engine.config import Config
from iwiki_mcp.engine.okf_artifacts import RESERVED_OKF, render_index, render_log
from iwiki_mcp.engine.grep import grep_sections
from iwiki_mcp.engine.lint import lint
from iwiki_mcp.engine.store import VectorStore


def _seed_backend(tmp_path, monkeypatch):
    """Network-free harness (mirrors tests/test_server_write.py::_seed): a
    `backend` domain bound via .iwiki.toml, dummy LLM env, stubbed embeddings."""
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
    return str(b), str(proj)


def test_reserved_okf_constant():
    assert RESERVED_OKF == ("index.md", "log.md")


def test_render_index_sorted_links():
    assert render_index(["b", "a"]) == "# Index\n\n- [a](a.md)\n- [b](b.md)\n"


def test_render_index_empty():
    assert render_index([]) == "# Index\n\n"


def test_render_log_lines():
    recs = [{"date": "2026-07-01", "op": "ingest", "page": "a.md"}]
    assert render_log(recs) == "# Log\n\n- 2026-07-01 ingest a.md\n"


def test_render_log_empty():
    assert render_log([]) == "# Log\n\n"


def test_grep_skips_reserved(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / "a.md").write_text("## H\ntoken here\n", encoding="utf-8")
    (d / "index.md").write_text("## H\ntoken here\n", encoding="utf-8")
    (d / "sub").mkdir()
    (d / "sub" / "index.md").write_text("## H\ntoken here\n", encoding="utf-8")
    hits = grep_sections(str(d), "token", 10)
    files = [h["file"] for h in hits]
    assert "index.md" not in files                   # domain-root reserved skipped
    assert "log.md" not in files
    assert files == ["a.md", "sub/index.md"]          # nested sub/index.md kept


def test_lint_skips_reserved(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / "a.md").write_text(
        "---\ntype: concept\n---\n# A\n\n## Overview\ns\n", encoding="utf-8")
    (d / "index.md").write_text("# Index\n\n- [a](a.md)\n", encoding="utf-8")
    (d / "log.md").write_text("# Log\n\n", encoding="utf-8")
    (d / "sub").mkdir()
    (d / "sub" / "index.md").write_text(
        "---\ntype: concept\n---\n# Sub\n\n## Overview\ns\n", encoding="utf-8")
    report = lint(str(d))
    assert report["pages"] == 2                      # a.md + sub/index.md counted
    assert report["missing_frontmatter"] == []       # index.md/log.md not flagged


def test_index_domain_skips_reserved_keeps_nested(tmp_path, monkeypatch):
    monkeypatch.setenv("IWIKI_BASE_DIR", str(tmp_path / "wiki"))
    monkeypatch.setenv("IWIKI_PROJECT_DIR", str(tmp_path / "proj"))
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", "2")
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, t: [[1.0, 0.0] for _ in t])
    base_dir = tmp_path / "wiki"
    dom = base_dir / "d"
    (dom / ".iwiki").mkdir(parents=True)
    (dom / "a.md").write_text(
        "# A\n\n## Overview\nsummary\n\n## Body\ncontent\n", encoding="utf-8")
    # Reserved root files carry an indexable ## section: they would produce records
    # if the exclusion regressed, so their absence below is a real assertion.
    (dom / "index.md").write_text(
        "# Index\n\n## Body\nreserved content\n", encoding="utf-8")
    (dom / "log.md").write_text(
        "# Log\n\n## Body\nreserved content\n", encoding="utf-8")
    (dom / "sub").mkdir()
    (dom / "sub" / "index.md").write_text(
        "# Sub\n\n## Overview\ns\n\n## Body\nnested content\n", encoding="utf-8")
    indexer.index_domain(Config.load(), str(base_dir), "d")
    recs = VectorStore(base.index_path(str(base_dir), "d")).load()
    files = {r.file for r in recs}
    assert "index.md" not in files                   # domain-root reserved excluded
    assert "log.md" not in files
    assert "sub/index.md" in files                    # nested sub/index.md kept


def test_list_pages_skips_reserved_keeps_nested(tmp_path, monkeypatch):
    b, _ = _seed_backend(tmp_path, monkeypatch)
    dom = Path(b) / "backend"
    (dom / "index.md").write_text("# Index\n\n", encoding="utf-8")
    (dom / "log.md").write_text("# Log\n\n", encoding="utf-8")
    (dom / "a.md").write_text("# A\n\n## Overview\ns\n", encoding="utf-8")
    (dom / "sub").mkdir()
    (dom / "sub" / "index.md").write_text("# Sub\n\n## Overview\ns\n", encoding="utf-8")
    out = server.wiki_list_pages("backend")
    slugs = {p["slug"] for p in out["pages"]}
    assert "a" in slugs
    assert "sub/index" in slugs                        # nested kept
    assert "index" not in slugs and "log" not in slugs  # domain-root reserved skipped


def test_unmigrated_pages_skips_reserved(tmp_path):
    dom = tmp_path / "d"
    dom.mkdir()
    (dom / "index.md").write_text("# Index\n\n", encoding="utf-8")
    (dom / "log.md").write_text("# Log\n\n", encoding="utf-8")
    (dom / "a.md").write_text("# A\n\n## Overview\ns\n", encoding="utf-8")
    (dom / "sub").mkdir()
    (dom / "sub" / "index.md").write_text("# Sub\n\n## Overview\ns\n", encoding="utf-8")
    slugs = [slug for slug, *_ in server._unmigrated_pages(dom)]
    assert "a" in slugs
    assert "sub/index" in slugs                        # nested kept
    assert "index" not in slugs and "log" not in slugs  # domain-root reserved skipped


def test_refresh_artifacts_writes_index_and_log(tmp_path):
    dom = tmp_path / "wiki" / "d"
    (dom / ".iwiki").mkdir(parents=True)
    (dom / ".iwiki" / "log.jsonl").write_text(
        '{"op":"ingest","page":"a.md","date":"2026-07-01"}\n', encoding="utf-8")
    (dom / "a.md").write_text(
        "---\ntype: concept\n---\n# A\n\n## Overview\ns\n", encoding="utf-8")
    warn = okf.refresh_artifacts(str(tmp_path / "wiki"), "d")
    assert warn is None
    assert (dom / "index.md").read_text(encoding="utf-8") == "# Index\n\n- [a](a.md)\n"
    assert (dom / "log.md").read_text(encoding="utf-8") == \
        "# Log\n\n- 2026-07-01 ingest a.md\n"


def test_refresh_artifacts_excludes_reserved_from_index(tmp_path):
    dom = tmp_path / "wiki" / "d"
    (dom / ".iwiki").mkdir(parents=True)
    (dom / "a.md").write_text("# A\n\n## Overview\ns\n", encoding="utf-8")
    okf.refresh_artifacts(str(tmp_path / "wiki"), "d")           # first run
    okf.refresh_artifacts(str(tmp_path / "wiki"), "d")           # idempotent re-run
    assert (dom / "index.md").read_text(encoding="utf-8") == "# Index\n\n- [a](a.md)\n"


def test_refresh_artifacts_warns_on_authored_reserved(tmp_path):
    dom = tmp_path / "wiki" / "d"
    (dom / ".iwiki").mkdir(parents=True)
    (dom / "index.md").write_text(
        "---\ntype: concept\n---\n# Real\n\n## Overview\nx\n", encoding="utf-8")
    warn = okf.refresh_artifacts(str(tmp_path / "wiki"), "d")
    assert warn and "index.md" in warn
    assert "Real" in (dom / "index.md").read_text(encoding="utf-8")   # left intact
