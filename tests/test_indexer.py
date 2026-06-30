import json

from iwiki_mcp import base, indexer
from iwiki_mcp.engine.config import Config


def _cfg(dimensions=2):
    return Config(base_url="http://x/v1", api_key="k", embed_model="m",
                  dimensions=dimensions, chunk_size=512, chunk_overlap=64, summary_max=400,
                  top_k=8, score_threshold=0.2, graph_depth=2, ignore=None)


def test_index_domain_stores_relative_paths(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
    (b / "backend" / ".iwiki").mkdir(parents=True)
    (b / "backend" / "auth.md").write_text(
        "# Auth\n## Overview\nsummary\n## Flow\nlogin then token\n")
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, texts: [[1.0, 0.0] for _ in texts])
    stats = indexer.index_domain(_cfg(), str(b), "backend")
    assert stats["indexed_chunks"] >= 1
    recs = [json.loads(l) for l in open(base.index_path(str(b), "backend"))]
    assert all(r["file"] == "auth.md" for r in recs)   # domain-relative, portable


def test_index_domain_stores_nested_paths_as_posix(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
    (b / "backend" / ".iwiki").mkdir(parents=True)
    (b / "backend" / "nested").mkdir()
    (b / "backend" / "nested" / "auth.md").write_text(
        "# Auth\n## Overview\nsummary\n## Flow\nlogin then token\n"
    )
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, texts: [[1.0, 0.0] for _ in texts])
    indexer.index_domain(_cfg(), str(b), "backend")
    recs = [json.loads(l) for l in open(base.index_path(str(b), "backend"))]
    assert all(r["file"] == "nested/auth.md" for r in recs)


def test_index_domain_reembeds_stale_dimensions(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
    (b / "backend" / ".iwiki").mkdir(parents=True)
    (b / "backend" / "auth.md").write_text(
        "# Auth\n## Overview\nsummary\n## Flow\nlogin then token\n"
    )
    monkeypatch.setattr(
        indexer, "embed_texts", lambda cfg, texts: [[1.0] + [0.0] * (cfg.dimensions - 1) for _ in texts]
    )

    indexer.index_domain(_cfg(dimensions=2), str(b), "backend")
    stats = indexer.index_domain(_cfg(dimensions=3), str(b), "backend")

    recs = [json.loads(l) for l in open(base.index_path(str(b), "backend"))]
    assert stats["embedded"] == 1
    assert stats["reused"] == 0
    assert all(r["dim"] == 3 for r in recs)


def test_append_log_writes_record(tmp_path):
    b = tmp_path / "wiki"
    (b / "backend" / ".iwiki").mkdir(parents=True)
    indexer.append_log(
        str(b), "backend", "ingest", "src/auth.py", "auth.md", src_hash="abc123"
    )
    line = open(base.log_path(str(b), "backend")).read().strip()
    rec = __import__("json").loads(line)
    assert rec["op"] == "ingest" and rec["page"] == "auth.md" and rec["src_hash"] == "abc123"
