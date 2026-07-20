from pathlib import Path

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


def test_adjacency_excludes_reserved_okf_hub(tmp_path):
    # index.md (export-generated) links every page in the domain; if its own
    # content were walked, it would become an all-pages hub -- unrelated pages
    # becoming mutual neighbours through it -- pulling the whole domain into
    # any single seed's expand_graph candidate pool. guide/a and api/b never
    # link to each other directly.
    (tmp_path / "guide").mkdir()
    (tmp_path / "api").mkdir()
    (tmp_path / "guide" / "a.md").write_text("# A\n\n## S\n\nBody.\n", encoding="utf-8")
    (tmp_path / "api" / "b.md").write_text("# B\n\n## S\n\nBody.\n", encoding="utf-8")
    (tmp_path / "index.md").write_text(
        "# Index\n\n- [guide/a](guide/a.md)\n- [api/b](api/b.md)\n", encoding="utf-8")

    adj = _adjacency(str(tmp_path))

    assert "index.md" not in adj
    assert "index.md" not in adj.get("guide/a.md", set())
    assert "index.md" not in adj.get("api/b.md", set())


def test_adjacency_uses_partial_markdown_mapping_and_disk_fallback(
        tmp_path, monkeypatch):
    cached = tmp_path / "cached.md"
    fallback = tmp_path / "fallback.md"
    cached.write_text("[Disk](disk-only.md)\n", encoding="utf-8")
    fallback.write_text("[Target](fallback-target.md)\n", encoding="utf-8")
    (tmp_path / "mapped.md").write_text("# Mapped\n", encoding="utf-8")
    (tmp_path / "disk-only.md").write_text("# Disk\n", encoding="utf-8")
    (tmp_path / "fallback-target.md").write_text("# Target\n", encoding="utf-8")
    reads = []
    real_read_text = Path.read_text

    def tracked_read_text(path, *args, **kwargs):
        reads.append(path)
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracked_read_text)

    adjacency = _adjacency(
        str(tmp_path), {"cached.md": "[Mapped](mapped.md)\n"}
    )

    assert cached not in reads
    assert fallback in reads
    assert adjacency["cached.md"] == {"mapped.md"}
    assert "cached.md" in adjacency["mapped.md"]
    assert "disk-only.md" not in adjacency.get("cached.md", set())
    assert adjacency["fallback.md"] == {"fallback-target.md"}
