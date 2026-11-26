import pytest

from iwiki_mcp.engine.lifecycle import is_current


@pytest.mark.parametrize("meta,expected", [
    ({}, True), ({"status": "stable"}, True), ({"status": "draft"}, False),
    ({"status": "stub"}, False), ({"status": "developing"}, False),
    ({"status": "deprecated"}, False), ({"status": "custom"}, False),
])
def test_declared_status_is_preserved(meta, expected):
    before = meta.copy()
    assert is_current(meta) is expected
    assert meta == before


def retired(path):
    return {"status": "deprecated", "superseded_by": path}


def test_scoped_successor_paths_and_cycles():
    from iwiki_mcp.engine.lifecycle import BundleLifecycle
    pages = {"guide/a.md": retired("../r/b.md"), "r/b.md": retired("/r/C%20D.md"),
             "r/C D.md": {}, "r/loop.md": retired("./loop.md"),
             "guide/dead.md": retired("absent.md")}
    graph = BundleLifecycle(pages)
    assert graph.resolve("guide/a.md") == "r/C D.md"
    assert graph.resolve("r/loop.md") is None
    assert graph.resolve("guide/dead.md") is None
    assert graph.resolve("r/C D.md") == "r/C D.md"


@pytest.mark.parametrize("value", ["../../outside.md", "//host/x.md", "https://example.org/a.md",
                                   "%2Fbad.md", "a%5cb.md", "%00.md", "/missing.md"])
def test_no_lookup_outside_bundle(value):
    from iwiki_mcp.engine.lifecycle import BundleLifecycle
    assert BundleLifecycle({"a.md": retired(value)}).resolve("a.md") is None
