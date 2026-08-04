from iwiki_mcp.engine.links import (
    CrossDomainRewrite,
    rewrite_cross_domain_links,
    rewrite_link_targets,
    rewrite_relative_anchors,
)


def test_rewrite_cross_domain_link_matches_normalized_target_and_preserves_md():
    body = "[Auth](iwiki://backend/concept/auth.md#Login-Flow)\n"
    out, count = rewrite_cross_domain_links(
        body,
        "frontend",
        CrossDomainRewrite("backend", "concept/auth", "concept/login", "login flow", "Sign In"),
    )
    assert out == "[Auth](iwiki://backend/concept/login.md#sign-in)\n"
    assert count == 1


def test_rewrite_cross_domain_link_ignores_code_images_and_mismatches():
    body = (
        "`[code](iwiki://backend/concept/auth)`\n"
        "![image](iwiki://backend/concept/auth)\n"
        "[other](iwiki://other/concept/auth)\n"
        "[real](iwiki://backend/concept/auth#keep)\n"
    )
    out, count = rewrite_cross_domain_links(
        body, "frontend", CrossDomainRewrite("backend", "concept/auth", "concept/login", "missing")
    )
    assert out == body
    assert count == 0


def test_rewrite_relative_anchors_rewrites_only_matching_markdown_href():
    body = "[one](#Old-Heading) [two](guide.md#old-heading) `[#Old Heading](#Old Heading)`\n"
    out, count = rewrite_relative_anchors(body, "old heading", "New Heading")
    assert out == "[one](#new-heading) [two](guide.md#new-heading) `[#Old Heading](#Old Heading)`\n"
    assert count == 2


def test_rewrite_relative_anchors_can_scope_to_exact_page_identity():
    body = (
        "[target](concept/x.md#Old) [other](other.md#Old) "
        "[self](#Old)\n"
    )
    out, count = rewrite_relative_anchors(
        body,
        "Old",
        "New",
        target_page="concept/x",
        source_page="concept/x",
    )
    assert out == (
        "[target](concept/x.md#new) [other](other.md#Old) "
        "[self](#new)\n"
    )
    assert count == 2


def test_rewrite_link_targets_markdown_and_legacy():
    body = "See [A](alpha.md#s) and [[alpha#S]] and `alpha.md`.\n"
    out = rewrite_link_targets(body, {"alpha": "concept/alpha"})
    assert "(concept/alpha.md#s)" in out
    assert "[[concept/alpha#S]]" in out
    assert "`alpha.md`" in out          # code span untouched


def test_rewrite_is_noop_without_match():
    body = "See [B](beta.md).\n"
    assert rewrite_link_targets(body, {"alpha": "concept/alpha"}) == body


def test_rewrite_link_targets_preserves_text_equal_to_target():
    # When the visible link text is literally the same string as the href
    # ([alpha.md](alpha.md)), only the href may change -- a naive
    # str.replace(target, ...) without a count (or even with count=1, which
    # hits the leftmost/text occurrence first) mutates the wrong span.
    body = "[alpha.md](alpha.md)\n"
    out = rewrite_link_targets(body, {"alpha": "concept/alpha"})
    assert out == "[alpha.md](concept/alpha.md)\n"


def test_rewrite_link_targets_leaves_cross_domain_uri_untouched():
    body = "[Auth](iwiki://backend/concept/auth.md#flow)\n"
    assert rewrite_link_targets(body, {"concept/auth": "concept/login"}) == body
