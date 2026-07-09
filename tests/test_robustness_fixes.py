"""Tests for robustness fixes: per-line log tolerance, scalar-tag guard, blank search type."""
import os

from iwiki_mcp import base, indexer, server
from iwiki_mcp.engine import frontmatter as fm
from iwiki_mcp import okf


def _seed(tmp_path, monkeypatch, with_domain=True):
    """Set up test base, project, and environment."""
    b = tmp_path / "wiki"
    b.mkdir()
    if with_domain:
        (b / "backend" / ".iwiki").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text('read = ["backend"]\nwrite = "backend"\n')
    monkeypatch.setenv("IWIKI_BASE_DIR", str(b))
    monkeypatch.setenv("IWIKI_PROJECT_DIR", str(proj))
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", "2")
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, t: [[1.0, 0.0] for _ in t])
    return str(b), str(proj)


def test_latest_source_tolerates_corrupt_line(tmp_path, monkeypatch):
    """latest_source returns valid source even when log has corrupt line before it."""
    b, _ = _seed(tmp_path, monkeypatch)
    log_path = os.path.join(b, "backend", ".iwiki", "log.jsonl")
    # Write a corrupt line followed by a valid record
    with open(log_path, "w", encoding="utf-8") as f:
        f.write('{"op":"ingest","page":"old.md","source":"old.py"}\n')
        f.write('this is garbage\n')  # corrupt line
        f.write('{"op":"ingest","page":"test.md","source":"test.py","date":"2026-07-01"}\n')

    result = okf.latest_source(b, "backend", "test.md")
    assert result == "test.py", "Should return valid source despite corrupt line"


def test_normalize_tags_guards_scalar_string():
    """normalize_tags handles bare string (e.g., from corrupted frontmatter)."""
    # Direct scalar string (normally shouldn't happen, but adversarial)
    result = fm.normalize_tags("foo")
    assert result == ["foo"], f"Expected ['foo'], got {result}"

    # Verify it doesn't iterate characters
    result = fm.normalize_tags("abc")
    assert result == ["abc"], f"Expected ['abc'], got {result}"
    assert result != ["a", "b", "c"], "Should not iterate string characters"


def test_wiki_search_whitespace_type_becomes_no_filter(monkeypatch):
    """wiki_search forwards whitespace-only type as None (no filter)."""
    captured = {}

    def fake_hybrid(cfg, base, doms, query, top_k, threshold, mode, type=None, tags=None):
        captured.update(type=type, tags=tags)
        return []

    class FakeConfig:
        top_k = 10
        score_threshold = 0.5

    monkeypatch.setattr(server.retrieval, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(server.base, "resolve_binding",
                        lambda: server.base.Binding(base="/b", read=("d",), write="d", project_dir="/p"))
    monkeypatch.setattr(server.base, "resolve_scope", lambda bind, scope, doms: ["d"])
    monkeypatch.setattr(server.Config, "load", staticmethod(lambda: FakeConfig()))

    server.wiki_search("q", type="  ")
    assert captured["type"] is None, f"Expected type=None for whitespace input, got {captured['type']}"
