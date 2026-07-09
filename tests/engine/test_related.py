from iwiki_mcp.engine.store import Record, quantize
from iwiki_mcp.engine.related import related, _graph_neighbours


def _rec(id, file, vec):
    scale, q = quantize(vec)
    return Record(id=id, file=file, heading=id.split("#")[-1], chunk=0,
                  hash="h", dim=len(vec), scale=scale, q=q)


def test_vector_neighbours_ranked_and_self_excluded():
    recs = [
        _rec("a.md#A", "a.md", [1.0, 0.0]),
        _rec("b.md#B", "b.md", [0.9, 0.1]),   # close to A
        _rec("c.md#C", "c.md", [0.0, 1.0]),   # orthogonal to A
    ]
    out = related("a.md#A", recs, top_k=2, graph_depth=2)
    ids = [d["id"] for d in out["vector"]]
    assert ids[0] == "b.md#B"
    assert "a.md#A" not in ids


def test_graph_skips_unreadable_path(tmp_path):
    # A path that exists but cannot be read as a file (a directory) must be
    # skipped by the BFS, not raise IsADirectoryError.
    d = tmp_path / "weird.md"
    d.mkdir()
    assert _graph_neighbours(str(d), depth=1) == []


def test_graph_follows_extensionless_links_beyond_first_hop(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("[[b]]\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("[[c]]\n", encoding="utf-8")
    (tmp_path / "c.md").write_text("## C\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert set(_graph_neighbours("a.md", depth=2)) == {"b", "c"}


def test_graph_neighbours_identical_for_markdown_and_legacy(tmp_path, monkeypatch):
    # legacy [[...]] chain a -> b -> c
    (tmp_path / "leg_a.md").write_text("[[leg_b]]\n", encoding="utf-8")
    (tmp_path / "leg_b.md").write_text("[[leg_c]]\n", encoding="utf-8")
    (tmp_path / "leg_c.md").write_text("## C\n", encoding="utf-8")
    # markdown chain of the same shape
    (tmp_path / "md_a.md").write_text("[b](md_b.md)\n", encoding="utf-8")
    (tmp_path / "md_b.md").write_text("[c](md_c.md)\n", encoding="utf-8")
    (tmp_path / "md_c.md").write_text("## C\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert set(_graph_neighbours("leg_a.md", depth=2)) == {"leg_b", "leg_c"}
    assert set(_graph_neighbours("md_a.md", depth=2)) == {"md_b", "md_c"}
