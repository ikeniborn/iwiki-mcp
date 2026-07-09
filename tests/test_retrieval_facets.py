from iwiki_mcp.retrieval import _facet_ok


def test_facet_ok_type_and_tags():
    assert _facet_ok("api", ["a", "b"], None, None)
    assert _facet_ok("api", ["a"], "api", None)
    assert not _facet_ok("guide", ["a"], "api", None)
    assert _facet_ok("api", ["a", "b"], None, ["b"])
    assert not _facet_ok("api", ["a"], None, ["z"])
    assert not _facet_ok(None, [], "api", None)
