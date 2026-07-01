import pytest

from iwiki_mcp.engine.section import SectionError, replace_section

PAGE = "# Auth\n## Overview\nsummary\n## Flow\nold body here\n## Notes\nkeep\n"


def test_replace_section_swaps_only_target_body():
    out = replace_section(PAGE, "Flow", "new body")
    assert "## Flow\nnew body" in out
    assert "old body here" not in out
    assert "## Overview\nsummary" in out
    assert "## Notes\nkeep" in out


def test_replace_section_last_section():
    out = replace_section(PAGE, "Notes", "fresh notes")
    assert "## Notes\nfresh notes" in out
    assert "keep" not in out


def test_replace_section_strips_leading_hashes_in_heading():
    out = replace_section(PAGE, "## Flow", "b2")
    assert "## Flow\nb2" in out


def test_replace_section_overview_is_editable():
    out = replace_section(PAGE, "Overview", "new summary")
    assert "## Overview\nnew summary" in out


def test_replace_section_missing_heading_raises():
    with pytest.raises(SectionError):
        replace_section(PAGE, "Nonexistent", "x")


def test_replace_section_duplicate_heading_raises():
    dup = "# T\n## Flow\na\n## Flow\nb\n"
    with pytest.raises(SectionError):
        replace_section(dup, "Flow", "x")


def test_replace_section_empty_heading_raises():
    with pytest.raises(SectionError):
        replace_section(PAGE, "  ", "x")


def test_replace_section_rejects_h2_in_body():
    with pytest.raises(SectionError):
        replace_section(PAGE, "Flow", "## Injected\nx")
