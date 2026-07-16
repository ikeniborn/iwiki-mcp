from iwiki_mcp.engine.grep import grep_sections


def test_grep_finds_exact_symbol(tmp_path):
    (tmp_path / "auth.md").write_text(
        "# Auth\n## Overview\ngeneral\n## Token\nthe refresh_token rotates\n")
    (tmp_path / "ui.md").write_text("# UI\n## Layout\nbuttons and panels\n")
    hits = grep_sections(str(tmp_path), "refresh_token", top_k=5)
    assert hits and hits[0]["file"] == "auth.md"
    assert hits[0]["heading"] == "Token"
    assert hits[0]["hit"] == "lexical"


def test_grep_empty_for_no_terms(tmp_path):
    assert grep_sections(str(tmp_path), "a", top_k=5) == []


def test_grep_returns_nested_file_with_posix_separator(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "auth.md").write_text("# Auth\n## Token\nrefresh_token\n")

    hits = grep_sections(str(tmp_path), "refresh_token", top_k=5)

    assert hits[0]["file"] == "nested/auth.md"


def test_grep_empty_for_non_positive_top_k(tmp_path):
    (tmp_path / "auth.md").write_text("# Auth\n## Token\nrefresh_token\n")
    (tmp_path / "other.md").write_text("# Other\n## Token\nrefresh_token\n")

    assert grep_sections(str(tmp_path), "refresh_token", top_k=0) == []
    assert grep_sections(str(tmp_path), "refresh_token", top_k=-1) == []


def test_grep_orders_ties_by_file_then_heading(tmp_path):
    (tmp_path / "b.md").write_text("# B\n## Alpha\nrefresh_token\n")
    (tmp_path / "a.md").write_text(
        "# A\n## Zulu\nrefresh_token\n## Alpha\nrefresh_token\n")

    hits = grep_sections(str(tmp_path), "refresh_token", top_k=5)

    assert [(hit["file"], hit["heading"]) for hit in hits] == [
        ("a.md", "Alpha"),
        ("a.md", "Zulu"),
        ("b.md", "Alpha"),
    ]


def test_grep_none_returns_all_positive_sections(tmp_path):
    (tmp_path / "a.md").write_text(
        "# A\n## One\nneedle\n## Two\nneedle needle\n## Three\nneedle\n",
        encoding="utf-8",
    )

    hits = grep_sections(str(tmp_path), "needle", top_k=None)

    assert len(hits) == 3
