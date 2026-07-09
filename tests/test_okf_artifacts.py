from iwiki_mcp.engine.okf_artifacts import RESERVED_OKF, render_index, render_log
from iwiki_mcp.engine.grep import grep_sections
from iwiki_mcp.engine.lint import lint


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
    hits = grep_sections(str(d), "token", 10)
    assert [h["file"] for h in hits] == ["a.md"]


def test_lint_skips_reserved(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / "a.md").write_text(
        "---\ntype: concept\n---\n# A\n\n## Overview\ns\n", encoding="utf-8")
    (d / "index.md").write_text("# Index\n\n- [a](a.md)\n", encoding="utf-8")
    (d / "log.md").write_text("# Log\n\n", encoding="utf-8")
    report = lint(str(d))
    assert report["pages"] == 1                      # only a.md counted
    assert report["missing_frontmatter"] == []       # index.md/log.md not flagged
