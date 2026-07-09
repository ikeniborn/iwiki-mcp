from iwiki_mcp.engine.links import parse_links, slugify_heading


def test_ignores_fenced_code_block():
    md = (
        "See [[real-page]] for details.\n\n"
        "```bash\n"
        "if [[ $# -gt 0 ]]; then echo hi; fi\n"
        '[[ -d "$LIB_DIR/<name>" ]]\n'
        "```\n"
    )
    assert parse_links(md) == ["real-page"]


def test_ignores_inline_code():
    md = "Use `[[ -d x ]]` in bash, but link to [[guide]] here."
    assert parse_links(md) == ["guide"]


def test_alias_form_returns_target():
    assert parse_links("[[core|the core module]]") == ["core"]


def test_dedup_preserves_order():
    md = "[[a]] then [[b]] then [[a]] again, and [[c]]."
    assert parse_links(md) == ["a", "b", "c"]


def test_section_ref_target_kept_whole():
    assert parse_links("[[nvm#Claude Binary Detection]]") == ["nvm#Claude Binary Detection"]


def test_slugify_lowercases_and_hyphenates():
    assert slugify_heading("Related Sections") == "related-sections"


def test_slugify_strips_punctuation():
    assert slugify_heading("API: the /v1 endpoint!") == "api-the-v1-endpoint"


def test_slugify_collapses_whitespace_and_hyphens():
    assert slugify_heading("Foo   ---  Bar") == "foo-bar"


def test_slugify_is_deterministic_and_idempotent():
    once = slugify_heading("Claude Binary Detection")
    assert once == "claude-binary-detection"
    assert slugify_heading(once) == once
