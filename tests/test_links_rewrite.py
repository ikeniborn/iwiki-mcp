from iwiki_mcp.engine.links import rewrite_link_targets


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
