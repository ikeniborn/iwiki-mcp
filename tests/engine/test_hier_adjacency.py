from iwiki_mcp.engine.hier import _adjacency


def test_adjacency_crosses_type_dirs(tmp_path):
    (tmp_path / "guide").mkdir()
    (tmp_path / "api").mkdir()
    (tmp_path / "guide" / "a.md").write_text(
        "# A\n\n## S\n\nSee [B](api/b.md#s).\n", encoding="utf-8")
    (tmp_path / "api" / "b.md").write_text("# B\n\n## S\n\nBody.\n", encoding="utf-8")

    adj = _adjacency(str(tmp_path))

    assert "api/b.md" in adj["guide/a.md"]
    assert "guide/a.md" in adj["api/b.md"]   # undirected
