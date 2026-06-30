from iwiki_mcp.engine.grep import grep_sections


def test_grep_finds_exact_symbol(tmp_path):
    (tmp_path / ".iwiki").mkdir()
    (tmp_path / "auth.md").write_text(
        "# Auth\n## Overview\ngeneral\n## Token\nthe refresh_token rotates\n")
    (tmp_path / "ui.md").write_text("# UI\n## Layout\nbuttons and panels\n")
    hits = grep_sections(str(tmp_path), "refresh_token", top_k=5)
    assert hits and hits[0]["file"] == "auth.md"
    assert hits[0]["heading"] == "Token"
    assert hits[0]["hit"] == "lexical"


def test_grep_empty_for_no_terms(tmp_path):
    assert grep_sections(str(tmp_path), "a", top_k=5) == []
