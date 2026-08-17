import pytest

from iwiki_mcp.engine.section import (
    SectionError,
    delete_section,
    insert_section,
    list_sections,
    move_section,
    replace_section,
)

PAGE = "# Auth\n## Overview\nsummary\n## Flow\nold body here\n## Notes\nkeep\n"


def test_list_sections_returns_heading_and_body_in_order():
    sections = list_sections(PAGE)
    assert [s.heading for s in sections] == ["Overview", "Flow", "Notes"]
    assert sections[1].body.strip() == "old body here"


def test_list_sections_empty_content_returns_empty_list():
    assert list_sections("# Title\nno sections here\n") == []


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


def test_insert_section_after_existing_heading():
    out = insert_section(PAGE, "New", "new body", after="Flow")
    assert out.index("## Flow") < out.index("## New") < out.index("## Notes")
    assert "## New\nnew body" in out


def test_insert_section_before_existing_heading():
    out = insert_section(PAGE, "New", "new body", before="Notes")
    assert out.index("## Flow") < out.index("## New") < out.index("## Notes")


def test_insert_section_defaults_to_append_at_end():
    out = insert_section(PAGE, "New", "new body")
    assert out.rstrip().endswith("## New\nnew body")


def test_insert_section_rejects_both_after_and_before():
    with pytest.raises(SectionError, match="after.*before|before.*after"):
        insert_section(PAGE, "New", "body", after="Flow", before="Notes")


def test_insert_section_rejects_anchor_collision():
    with pytest.raises(SectionError, match="collides"):
        insert_section(PAGE, "Flow", "body")


def test_insert_section_rejects_h2_in_body():
    with pytest.raises(SectionError):
        insert_section(PAGE, "New", "## Injected\nx")


def test_insert_section_rejects_unknown_anchor_point():
    with pytest.raises(SectionError, match="not found"):
        insert_section(PAGE, "New", "body", after="Nonexistent")


def test_delete_section_removes_target_only():
    out = delete_section(PAGE, "Flow")
    assert "## Flow" not in out
    assert "## Overview" in out
    assert "## Notes" in out


def test_delete_section_missing_heading_raises():
    with pytest.raises(SectionError, match="not found"):
        delete_section(PAGE, "Nonexistent")


def test_delete_section_rejects_reserved_heading():
    reserved = PAGE + "## Outgoing links\n- x\n"
    with pytest.raises(SectionError, match="reserved"):
        delete_section(reserved, "Outgoing links")


def test_delete_section_rejects_last_remaining_section():
    single = "# T\n## Only\nbody\n"
    with pytest.raises(SectionError, match="last"):
        delete_section(single, "Only")


def test_move_section_after_target():
    out = move_section(PAGE, "Flow", after="Notes")
    assert out.index("## Overview") < out.index("## Notes") < out.index("## Flow")


def test_move_section_before_target():
    out = move_section(PAGE, "Notes", before="Overview")
    assert out.index("## Notes") < out.index("## Overview") < out.index("## Flow")


def test_move_section_rejects_self_reference():
    with pytest.raises(SectionError, match="itself"):
        move_section(PAGE, "Flow", after="Flow")


def test_move_section_no_op_next_to_itself_still_succeeds():
    # Flow is already immediately before Notes — moving it "before Notes"
    # is a valid no-op reorder, not an error.
    out = move_section(PAGE, "Flow", before="Notes")
    assert out.index("## Overview") < out.index("## Flow") < out.index("## Notes")


def test_move_section_rejects_reserved_heading():
    reserved = PAGE + "## Outgoing links\n- x\n"
    with pytest.raises(SectionError, match="reserved"):
        move_section(reserved, "Outgoing links", before="Overview")


def test_move_section_rejects_moving_overview():
    with pytest.raises(SectionError, match="reserved"):
        move_section(PAGE, "Overview", after="Notes")
