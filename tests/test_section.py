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


def test_replace_section_renames_heading_and_replaces_body():
    out = replace_section(PAGE, "Flow", "new", new_heading="New Flow")
    assert "## New Flow\nnew" in out
    assert "## Flow" not in out


@pytest.mark.parametrize("new_heading", ["", "!!!"])
def test_replace_section_rejects_empty_normalized_new_heading(new_heading):
    with pytest.raises(SectionError, match="empty normalized heading"):
        replace_section(PAGE, "Flow", "new", new_heading=new_heading)


def test_replace_section_rejects_new_heading_anchor_collision():
    with pytest.raises(SectionError, match="collides"):
        replace_section(PAGE, "Flow", "new", new_heading="Notes!")


def test_replace_section_rejects_anchor_collision_with_any_heading_level():
    content = "# Auth\n## Flow\nold\n### Notes!\nkeep\n"
    with pytest.raises(SectionError, match="collides"):
        replace_section(content, "Flow", "new", new_heading="Notes")
