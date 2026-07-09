from iwiki_mcp.engine import frontmatter as fm


def test_split_extracts_meta_and_body():
    content = "---\ntype: api\ntitle: X\ntags: [a, b]\n---\n# X\n\n## Overview\nhi\n"
    meta, body = fm.split(content)
    assert meta["type"] == "api"
    assert meta["title"] == "X"
    assert meta["tags"] == ["a", "b"]
    assert body.startswith("# X")


def test_split_no_frontmatter_returns_empty_meta():
    content = "# X\n\n## Overview\nhi\n"
    meta, body = fm.split(content)
    assert meta == {}
    assert body == content


def test_split_malformed_is_failsoft():
    content = "---\nnot closed\n# X\n"
    meta, body = fm.split(content)
    assert meta == {}
    assert body == content


def test_render_round_trips():
    meta = {"type": "api", "title": "X", "tags": ["a", "b"]}
    meta2, _ = fm.split(fm.render(meta) + "# body\n")
    assert meta2["type"] == "api"
    assert meta2["tags"] == ["a", "b"]


def test_normalize_tag_kebab_lowercase():
    assert fm.normalize_tag("  Data Flow ") == "data-flow"
    assert fm.normalize_tag("Config_Key") == "config-key"


def test_normalize_tags_dedupe_and_cap():
    tags = fm.normalize_tags(["A", "a", "b", "c", "d", "e", "f"])
    assert tags[:2] == ["a", "b"]
    assert len(tags) == fm.MAX_TAGS


def test_coerce_type_clamps_offvocab():
    assert fm.coerce_type("api") == "api"
    assert fm.coerce_type("weird") == fm.DEFAULT_TYPE
    assert fm.coerce_type(None) == fm.DEFAULT_TYPE


def test_derive_title_from_h1_then_slug():
    assert fm.derive_title("# Base binding\n\n## Overview\nx", "b") == "Base binding"
    assert fm.derive_title("## Overview\nx", "my-slug") == "my slug"


def test_derive_description_from_overview_capped():
    body = "# T\n\n## Overview\n" + "word " * 200 + "\n\n## Other\nx"
    desc = fm.derive_description(body, max_chars=50)
    assert len(desc) <= 50
    assert desc.startswith("word")
