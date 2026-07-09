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


def test_section_ref_heading_is_slugified():
    assert parse_links("[[nvm#Claude Binary Detection]]") == ["nvm#claude-binary-detection"]


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


from iwiki_mcp.engine.links import has_legacy_wikilink


def test_markdown_link_with_anchor_parsed():
    assert parse_links("See [Flow](auth.md#login-flow) here.") == ["auth#login-flow"]


def test_markdown_link_without_anchor_parsed():
    assert parse_links("[Auth](auth.md)") == ["auth"]


def test_markdown_link_strips_dot_slash_and_md():
    assert parse_links("[x](./guide.md)") == ["guide"]


def test_markdown_image_rejected():
    assert parse_links("![diagram](arch.md)") == []


def test_markdown_external_absolute_anchor_mailto_rejected():
    md = "[a](https://x.md) [b](/abs.md) [c](#local) [d](mailto:x@y.md)"
    assert parse_links(md) == []


def test_markdown_non_md_target_rejected():
    assert parse_links("[code](server.py) and [pdf](doc.pdf)") == []


def test_markdown_link_in_fence_ignored():
    md = "```\n[t](base.md)\n```\nreal [x](real.md)\n"
    assert parse_links(md) == ["real"]


def test_markdown_and_legacy_dedup_by_normalized_key():
    md = "[Bar](foo.md#bar-baz) and [[foo#Bar Baz]]"
    assert parse_links(md) == ["foo#bar-baz"]


def test_has_legacy_wikilink_true_false_and_code():
    assert has_legacy_wikilink("see [[x]] here") is True
    assert has_legacy_wikilink("see [x](x.md) here") is False
    assert has_legacy_wikilink("`[[ $# ]]` in code") is False


def test_markdown_noncanonical_anchor_slugified_and_dedupes_with_legacy():
    md = "[Docs](guide.md#My-Section) and [[guide#My Section]]"
    assert parse_links(md) == ["guide#my-section"]


def test_legacy_bare_anchor_rejected():
    assert parse_links("see [[#Something]] here") == []


from iwiki_mcp.engine.links import to_markdown_links


def test_rewrite_plain_slug():
    assert to_markdown_links("see [[core]] now") == "see [core](core.md) now"


def test_rewrite_slug_heading():
    assert to_markdown_links("[[nvm#Claude Binary Detection]]") == \
        "[Claude Binary Detection](nvm.md#claude-binary-detection)"


def test_rewrite_slug_alias():
    assert to_markdown_links("[[core|the core]]") == "[the core](core.md)"


def test_rewrite_slug_heading_alias():
    assert to_markdown_links("[[nvm#Binary Detection|see nvm]]") == \
        "[see nvm](nvm.md#binary-detection)"


def test_bash_wikilike_in_fence_untouched():
    md = "```bash\nif [[ $# -gt 0 ]]; then :; fi\n```\n"
    assert to_markdown_links(md) == md


def test_markdown_example_in_fence_untouched():
    md = "```\n[[core]] renders as [core](core.md)\n```\n"
    assert to_markdown_links(md) == md


def test_inline_code_wikilink_untouched():
    md = "use `[[x]]` literally"
    assert to_markdown_links(md) == md


def test_idempotent_on_markdown_body():
    md = "already [core](core.md) linked"
    assert to_markdown_links(md) == md


def test_idempotent_rerun():
    once = to_markdown_links("[[a#B c]] and [[d]]")
    assert to_markdown_links(once) == once
