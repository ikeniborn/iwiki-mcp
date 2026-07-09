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
