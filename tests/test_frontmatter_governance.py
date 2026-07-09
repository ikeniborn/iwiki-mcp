from iwiki_mcp.engine import frontmatter as fm


def test_coerce_type_case_and_whitespace_insensitive():
    assert fm.coerce_type("API") == "api"
    assert fm.coerce_type(" Architecture ") == "architecture"
    assert fm.coerce_type(" api ") == "api"
    assert fm.coerce_type("weird") == fm.DEFAULT_TYPE


def test_normalize_tag_folds_list_delimiters():
    n = fm.normalize_tag("config,setup")
    assert "," not in n
    assert n == "config-setup"
    # round-tripping through the inline `tags: [...]` rendering must not split it
    meta = {"tags": fm.normalize_tags(["config,setup", "other[thing]"])}
    rendered = fm.render(meta)
    meta2, _ = fm.split(rendered + "# x\n")
    assert meta2["tags"] == meta["tags"]
    assert all("," not in t and "[" not in t and "]" not in t for t in meta2["tags"])


def test_render_split_round_trip_bracket_like_title():
    meta = {"type": "concept", "title": "[Draft]"}
    rendered = fm.render(meta)
    assert 'title: "[Draft]"' in rendered
    meta2, _ = fm.split(rendered + "# x\n")
    assert meta2["title"] == "[Draft]"
    assert isinstance(meta2["title"], str)


def test_render_split_round_trip_comma_and_colon_description():
    meta = {"type": "concept", "description": "note: with, delimiters"}
    rendered = fm.render(meta)
    meta2, _ = fm.split(rendered + "# x\n")
    assert meta2["description"] == meta["description"]


def test_render_split_round_trip_simple_values_stay_bare():
    meta = {"type": "api", "title": "Base binding", "timestamp": "2026-07-09T12:00:00"}
    rendered = fm.render(meta)
    assert 'title: "' not in rendered
    assert "title: Base binding" in rendered
    meta2, _ = fm.split(rendered + "# x\n")
    assert meta2 == meta


def test_split_unquoted_existing_pages_still_parse():
    content = "---\ntype: api\ntitle: Base binding\n---\n# X\n"
    meta, body = fm.split(content)
    assert meta["type"] == "api"
    assert meta["title"] == "Base binding"
    assert body.startswith("# X")


def test_render_split_round_trip_generic_scalars():
    for v in ["simple", "[a, b]", "with, comma", "with: colon", "trailing:", " leading", "trailing ", ""]:
        meta = {"description": v}
        meta2, _ = fm.split(fm.render(meta) + "# x\n")
        assert meta2 == meta, f"round-trip failed for {v!r}: got {meta2}"


def test_derive_description_requires_first_section_overview():
    body = "# T\n\n## Other\nintro\n\n## Overview\nnot the first section\n"
    assert fm.derive_description(body) == ""


def test_derive_description_first_section_overview():
    body = "# T\n\n## Overview\nsummary text\n\n## Other\nmore\n"
    assert fm.derive_description(body) == "summary text"
