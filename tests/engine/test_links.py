from dataclasses import FrozenInstanceError

import pytest

from iwiki_mcp.engine import links as links_module
from iwiki_mcp.engine.links import (
    has_legacy_wikilink,
    parse_links,
    slugify_heading,
    to_markdown_links,
)


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


def test_slugify_matches_github_no_hyphen_collapse():
    # GitHub does NOT collapse repeated hyphens: punctuation or extra spaces
    # between words leave a gap of hyphens, so the anchor resolves on GitHub.
    assert slugify_heading("Data / Flow") == "data--flow"
    assert slugify_heading("A - B") == "a---b"


def test_slugify_is_deterministic_and_idempotent():
    once = slugify_heading("Claude Binary Detection")
    assert once == "claude-binary-detection"
    assert slugify_heading(once) == once


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


def test_has_legacy_wikilink_ignores_bare_anchor():
    # A bare same-page anchor has no slug: parse_links rejects it and
    # to_markdown_links never rewrites it, so it must not read as un-migrated.
    assert has_legacy_wikilink("bare [[#anchor]] only") is False
    assert has_legacy_wikilink("real [[page]] and [[#a]]") is True


def test_markdown_noncanonical_anchor_slugified_and_dedupes_with_legacy():
    md = "[Docs](guide.md#My-Section) and [[guide#My Section]]"
    assert parse_links(md) == ["guide#my-section"]


def test_legacy_bare_anchor_rejected():
    assert parse_links("see [[#Something]] here") == []


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


def test_to_markdown_links_tolerates_nul_sentinel_lookalike():
    # A NUL-sentinel look-alike already in the body (never in real markdown)
    # must pass through untouched, not raise IndexError from the restore step.
    body = "weird \x000\x00 text with [[core]]"
    assert to_markdown_links(body) == "weird \x000\x00 text with [core](core.md)"


def test_structured_relative_markdown_target_has_normalized_fields():
    assert links_module.parse_link_targets(
        "See [Login](./concept/auth.md#Login-Flow).", "source-domain"
    ) == [
        links_module.LinkTarget(
            source_domain="source-domain",
            target_domain="source-domain",
            target_page="concept/auth",
            target_anchor="login-flow",
            raw_target="./concept/auth.md#Login-Flow",
            kind="intra",
            is_reserved=False,
        )
    ]


def test_structured_legacy_target_has_normalized_fields():
    assert links_module.parse_link_targets(
        "[[concept/auth#Login Flow|login]]", "docs"
    ) == [
        links_module.LinkTarget(
            source_domain="docs",
            target_domain="docs",
            target_page="concept/auth",
            target_anchor="login-flow",
            raw_target="concept/auth#Login Flow",
            kind="intra",
            is_reserved=False,
        )
    ]


def test_structured_legacy_target_preserves_exact_authored_raw_for_dedup():
    targets = links_module.parse_link_targets(
        "[[  guide# Heading  ]] [markdown](guide.md#heading)", "docs"
    )
    assert len(targets) == 1
    assert targets[0].target_page == "guide"
    assert targets[0].target_anchor == "heading"
    assert targets[0].raw_target == "  guide# Heading  "


@pytest.mark.parametrize(
    "source_domain",
    [
        "",
        ".hidden",
        ".",
        "..",
        "bad/domain",
        r"bad\domain",
        "/absolute",
        "C:drive",
    ],
)
def test_structured_parser_rejects_invalid_source_domain(source_domain):
    assert links_module.parse_link_targets("[guide](guide.md)", source_domain) == []


def test_structured_cross_domain_uri_accepts_optional_md_suffix():
    md = (
        "[A](iwiki://backend/reference/auth-api.md#Token-Flow) "
        "[B](iwiki://backend/reference/other)"
    )
    assert links_module.parse_link_targets(md, "frontend") == [
        links_module.LinkTarget(
            source_domain="frontend",
            target_domain="backend",
            target_page="reference/auth-api",
            target_anchor="token-flow",
            raw_target="iwiki://backend/reference/auth-api.md#Token-Flow",
            kind="cross",
            is_reserved=False,
        ),
        links_module.LinkTarget(
            source_domain="frontend",
            target_domain="backend",
            target_page="reference/other",
            target_anchor="",
            raw_target="iwiki://backend/reference/other",
            kind="cross",
            is_reserved=False,
        ),
    ]


@pytest.mark.parametrize(
    "target",
    [
        "iwiki://backend/page?mode=full",
        "iwiki://user@backend/page",
        "iwiki://backend:8443/page",
        "iwiki://backend/type%2Fpage",
        "iwiki://backend/type%5Cpage",
        "iwiki:///page",
        "iwiki://backend",
        "iwiki://backend/",
        "iwiki://backend//page",
        "iwiki://backend/./page",
        "iwiki://backend/type/../page",
        "iwiki://backend/%2e%2e/page",
        "iwiki://backend/C:/page",
    ],
)
def test_structured_cross_domain_uri_rejects_unsafe_targets(target):
    assert links_module.parse_link_targets(f"[unsafe]({target})", "source") == []


@pytest.mark.parametrize(
    "target",
    ["iwiki://backend/.hidden/page", ".hidden/page.md"],
)
def test_structured_parser_rejects_hidden_type_segment(target):
    assert links_module.parse_link_targets(f"[.]({target})", "source") == []


def test_structured_parser_allows_hidden_nested_slug_segment():
    targets = links_module.parse_link_targets(
        "[nested](type/.hidden-slug.md)", "source"
    )
    assert [target.target_page for target in targets] == ["type/.hidden-slug"]


@pytest.mark.parametrize(
    ("opener", "closer"),
    [
        ("````md", "````"),
        ("```md", "````"),
        ("~~~md", "~~~~"),
    ],
)
def test_variable_length_fences_mask_links_and_headings(opener, closer):
    md = (
        f"{opener}\n"
        "[hidden](hidden.md) [[legacy-hidden]]\n"
        "## Hidden heading\n"
        f"{closer}\n"
        "[real](real.md)\n"
        "## Real heading\n"
    )
    assert [
        target.target_page
        for target in links_module.parse_link_targets(md, "source")
    ] == ["real"]
    assert links_module.parse_heading_anchors(md) == [
        links_module.HeadingAnchor("real-heading", "Real heading")
    ]
    assert parse_links(md) == ["real"]


def test_double_backtick_inline_span_masks_links_and_headings():
    md = (
        "``[hidden](hidden.md) [[legacy-hidden]]\n"
        "## Hidden heading``\n"
        "[real](real.md)\n"
        "## Real heading\n"
    )
    assert [
        target.target_page
        for target in links_module.parse_link_targets(md, "source")
    ] == ["real"]
    assert links_module.parse_heading_anchors(md) == [
        links_module.HeadingAnchor("real-heading", "Real heading")
    ]
    assert parse_links(md) == ["real"]


@pytest.mark.parametrize(
    ("heading", "expected_anchor"),
    [
        ("## Use `foo`", "use-foo"),
        ("## Use ``foo bar``", "use-foo-bar"),
    ],
)
def test_heading_anchor_preserves_inline_code_text(heading, expected_anchor):
    assert links_module.parse_heading_anchors(heading) == [
        links_module.HeadingAnchor(expected_anchor, heading[3:])
    ]


def test_structured_parser_ignores_code_images_and_external_schemes():
    md = (
        "`[inline](iwiki://backend/inline)`\n"
        "```md\n[fenced](iwiki://backend/fenced)\n```\n"
        "![image](iwiki://backend/image)\n"
        "[https](https://backend/page.md)\n"
        "[other](other://backend/page.md)\n"
        "[mailto](mailto:user@example.com)\n"
        "[real](iwiki://backend/real)\n"
    )
    assert [
        target.target_page
        for target in links_module.parse_link_targets(md, "source")
    ] == ["real"]


def test_structured_root_okf_targets_are_reserved_but_nested_name_is_not():
    md = (
        "[index](index.md) [[log]] "
        "[cross](iwiki://other/index.md) "
        "[concept](concept/index.md)"
    )
    targets = links_module.parse_link_targets(md, "source")
    actual = [
        (target.target_domain, target.target_page, target.is_reserved)
        for target in targets
    ]
    assert actual == [
        ("source", "index", True),
        ("source", "log", True),
        ("other", "index", True),
        ("source", "concept/index", False),
    ]


def test_structured_duplicate_keeps_lexicographically_smallest_raw_target():
    targets = links_module.parse_link_targets(
        "[plain](guide.md) [dot](./guide.md) [[guide]]", "source"
    )
    assert len(targets) == 1
    assert targets[0].raw_target == "./guide.md"


def test_structured_models_are_immutable():
    target = links_module.parse_link_targets("[guide](guide.md)", "source")[0]
    with pytest.raises(FrozenInstanceError):
        target.target_page = "changed"


def test_parse_heading_anchors_extracts_h1_through_h6_and_ignores_code():
    md = (
        "# One\n## Two\n### Three\n#### Four\n##### Five\n###### Six\n"
        "####### Not a heading\n"
        "```md\n## Fenced\n```\n"
        "`## Inline`\n"
    )
    assert links_module.parse_heading_anchors(md) == [
        links_module.HeadingAnchor("one", "One"),
        links_module.HeadingAnchor("two", "Two"),
        links_module.HeadingAnchor("three", "Three"),
        links_module.HeadingAnchor("four", "Four"),
        links_module.HeadingAnchor("five", "Five"),
        links_module.HeadingAnchor("six", "Six"),
    ]


def test_parse_heading_anchors_keeps_earliest_heading_for_duplicate_slug():
    md = "## First!\n###### First\n## Other\n"
    assert links_module.parse_heading_anchors(md) == [
        links_module.HeadingAnchor("first", "First!"),
        links_module.HeadingAnchor("other", "Other"),
    ]
