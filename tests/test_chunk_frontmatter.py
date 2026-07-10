from iwiki_mcp.engine.chunk import chunk_markdown


PAGE = (
    "---\ntype: api\ntags: [binding, config]\n---\n"
    "# Title\n\n## Overview\nsummary here\n\n## Body\nreal content words\n"
)


def test_frontmatter_excluded_from_chunk_text():
    chunks = chunk_markdown("p.md", PAGE, size=512, overlap=64)
    assert chunks, "expected at least one chunk"
    for c in chunks:
        assert "type: api" not in c.text
        assert "---" not in c.text


def test_type_and_tags_stamped_on_chunks():
    chunks = chunk_markdown("p.md", PAGE, size=512, overlap=64)
    assert all(c.type == "api" for c in chunks)
    assert all(c.tags == ["binding", "config"] for c in chunks)


def test_page_without_frontmatter_defaults():
    plain = "# T\n\n## Overview\ns\n\n## B\nwords\n"
    chunks = chunk_markdown("p.md", plain, size=512, overlap=64)
    assert all(c.type is None for c in chunks)
    assert all(c.tags == [] for c in chunks)


def test_summary_from_description_not_overview():
    page = (
        "---\ntype: person\ndescription: Alice covers AR ledger and refunds.\n---\n"
        "# Alice\n\n## Role\nreal content words here\n"
    )
    chunks = chunk_markdown("p.md", page, size=512, overlap=64)
    assert chunks
    summ = next(c for c in chunks if c.kind == "summary")
    assert summ.text == "Alice covers AR ledger and refunds."


def test_overview_excluded_from_index():
    # `## Overview` is never indexed and is not the summary source. An un-migrated
    # Overview (no frontmatter description) does not enter the vectors at all.
    page = "# T\n\n## Overview\nsummary body\n\n## Body\nwords\n"
    chunks = chunk_markdown("p.md", page, size=512, overlap=64)
    headings = {c.heading for c in chunks}
    assert "Overview" not in headings
    assert headings == {"Body"}


def test_reserved_link_sections_excluded():
    page = (
        "---\ntype: person\ndescription: d\n---\n# T\n\n## Role\nprose words\n\n"
        "## Outgoing links\n- [x](y.md)\n\n## External links\n- https://example.com\n"
    )
    chunks = chunk_markdown("p.md", page, size=512, overlap=64)
    headings = {c.heading for c in chunks if c.kind == "section"}
    assert headings == {"Role"}
    assert all("example.com" not in c.text and "y.md" not in c.text for c in chunks)


def test_overview_excluded_even_when_not_first_section():
    # exclusion is by heading, not position — a non-first Overview is still dropped
    page = "# T\n\n## Body\nwords here\n\n## Overview\nsummary text\n"
    chunks = chunk_markdown("p.md", page, size=512, overlap=64)
    assert {c.heading for c in chunks} == {"Body"}


def test_list_description_does_not_crash():
    # a hand-authored `description: [a, b]` parses to a list — chunking must not
    # crash (mirrors validate's isinstance guard); no article summary is emitted.
    page = "---\ntype: person\ndescription: [a, b]\n---\n# T\n\n## Body\nwords\n"
    chunks = chunk_markdown("p.md", page, size=512, overlap=64)
    assert {c.heading for c in chunks} == {"Body"}


_PAGE = (
    "---\ntype: reference\ndescription: \"Alpha beta gamma summary.\"\n---\n"
    "# Big Title\n\n## First\nbody words here\n\n## Second\nmore body text\n"
)


def test_summary_chunk_holds_description_only():
    chunks = chunk_markdown("a.md", _PAGE, 512, 64)
    summ = [c for c in chunks if c.kind == "summary"]
    assert len(summ) == 1
    assert summ[0].text == "Alpha beta gamma summary."
    assert summ[0].heading == ""


def test_section_chunk_excludes_title_and_description():
    chunks = chunk_markdown("a.md", _PAGE, 512, 64)
    secs = [c for c in chunks if c.kind == "section"]
    assert secs and all(c.ordinal >= 0 for c in secs)
    first = next(c for c in secs if c.heading == "First")
    assert first.text == "## First\nbody words here"
    assert "Big Title" not in first.text
    assert "Alpha beta gamma" not in first.text
