import json

from iwiki_mcp import base, indexer
from iwiki_mcp.engine import store
from iwiki_mcp.engine.config import Config


def _cfg(dimensions=2):
    return Config(base_url="http://x/v1", api_key="k", embed_model="m",
                  dimensions=dimensions, chunk_size=512, chunk_overlap=64, summary_max=400,
                  top_k=8, score_threshold=0.2, graph_depth=2, ignore=None)


def test_index_domain_stores_relative_paths(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
    (b / "backend").mkdir(parents=True)
    (b / "backend" / "auth.md").write_text(
        "# Auth\n## Overview\nsummary\n## Flow\nlogin then token\n")
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, texts: [[1.0, 0.0] for _ in texts])
    stats = indexer.index_domain(_cfg(), str(b), "backend")
    assert stats["indexed_chunks"] >= 1
    recs = [json.loads(line) for line in open(base.index_path(str(b), "backend"))]
    assert all(r["file"] == "auth.md" for r in recs)   # domain-relative, portable


def test_index_domain_stores_nested_paths_as_posix(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
    (b / "backend").mkdir(parents=True)
    (b / "backend" / "nested").mkdir()
    (b / "backend" / "nested" / "auth.md").write_text(
        "# Auth\n## Overview\nsummary\n## Flow\nlogin then token\n"
    )
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, texts: [[1.0, 0.0] for _ in texts])
    indexer.index_domain(_cfg(), str(b), "backend")
    recs = [json.loads(line) for line in open(base.index_path(str(b), "backend"))]
    assert all(r["file"] == "nested/auth.md" for r in recs)


def test_index_domain_reembeds_stale_dimensions(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
    (b / "backend").mkdir(parents=True)
    (b / "backend" / "auth.md").write_text(
        "# Auth\n## Overview\nsummary\n## Flow\nlogin then token\n"
    )
    monkeypatch.setattr(
        indexer,
        "embed_texts",
        lambda cfg, texts: [[1.0] + [0.0] * (cfg.dimensions - 1) for _ in texts],
    )

    indexer.index_domain(_cfg(dimensions=2), str(b), "backend")
    stats = indexer.index_domain(_cfg(dimensions=3), str(b), "backend")

    recs = [json.loads(line) for line in open(base.index_path(str(b), "backend"))]
    assert stats["embedded"] == 1
    assert stats["reused"] == 0
    assert all(r["dim"] == 3 for r in recs)


def test_reindex_migrates_old_schema_and_adds_summary(tmp_path, monkeypatch):
    from iwiki_mcp.engine.chunk import chunk_markdown

    b = tmp_path / "wiki"
    (b / "backend").mkdir(parents=True)
    content = '---\ndescription: "Sum txt."\n---\n# T\n\n## Sec\nbody\n'
    (b / "backend" / "p.md").write_text(content, encoding="utf-8")
    monkeypatch.setattr(
        indexer,
        "embed_texts",
        lambda cfg, texts: [[float(len(t))] + [0.0] * (cfg.dimensions - 1) for t in texts],
    )
    cfg = _cfg(dimensions=2)

    # Compute the real section-chunk hash chunk_markdown will produce, so the
    # seeded old-schema record collides on (id, chunk, hash, dim) -- it would be
    # reused if the schema-version guard were missing, even though it predates
    # the kind/summary migration.
    section = next(c for c in chunk_markdown(
        "p.md", content, cfg.chunk_size, cfg.chunk_overlap, cfg.summary_max)
        if c.kind == "section")

    # seed an OLD-schema index record (v defaults to 1, kind defaults to "section")
    old = store.Record(id=section.id, file="p.md", heading="Sec", chunk=0,
                       hash=section.hash, dim=2, scale=1.0, q=[0, 0])
    store.save_index(base.index_path(str(b), "backend"), [old])

    stats = indexer.index_domain(cfg, str(b), "backend")

    recs = store.load_index(base.index_path(str(b), "backend"))
    kinds = sorted(r.kind for r in recs)
    assert "summary" in kinds and "section" in kinds
    assert all(r.v == store.SCHEMA_VERSION for r in recs)
    assert stats["reused"] == 0  # old v==1 record not reused despite hash/dim match


def test_reindex_migrates_repeated_heading_identity_without_schema_bump(
        tmp_path, monkeypatch):
    from iwiki_mcp.engine.chunk import chunk_markdown

    b = tmp_path / "wiki"
    (b / "backend").mkdir(parents=True)
    content = (
        "## Setup\nfirst setup\n"
        "## Other\nstable body\n"
        "## Setup\nsecond setup\n"
    )
    (b / "backend" / "p.md").write_text(content, encoding="utf-8")
    cfg = _cfg()
    chunks = chunk_markdown(
        "p.md", content, cfg.chunk_size, cfg.chunk_overlap, cfg.summary_max)
    first_setup, other, second_setup = chunks

    old_first = store.make_record(first_setup, [1.0, 0.0])
    old_second = store.make_record(second_setup, [0.0, 1.0])
    old_second.chunk = 0
    stable_other = store.make_record(other, [1.0, 0.0])
    store.save_index(
        base.index_path(str(b), "backend"),
        [old_first, old_second, stable_other],
    )

    embedded_texts = []

    def fake_embed(cfg, texts):
        embedded_texts.extend(texts)
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(indexer, "embed_texts", fake_embed)
    stats = indexer.index_domain(cfg, str(b), "backend")

    recs = store.load_index(base.index_path(str(b), "backend"))
    setup = [r for r in recs if r.heading == "Setup"]
    assert [(r.chunk, r.ordinal) for r in setup] == [(0, 0), (1, 2)]
    assert len({(r.file, r.heading, r.chunk) for r in recs}) == len(recs)
    assert stats["reused"] == 1
    assert stats["embedded"] == 2
    assert embedded_texts == [first_setup.text, second_setup.text]
    assert all(r.v == store.SCHEMA_VERSION for r in recs)


def test_reused_record_refreshes_current_ordinal(tmp_path, monkeypatch):
    from iwiki_mcp.engine.chunk import chunk_markdown

    b = tmp_path / "wiki"
    (b / "backend").mkdir(parents=True)
    content = "## Other\nother body\n## Stable\nstable body\n"
    (b / "backend" / "p.md").write_text(content, encoding="utf-8")
    cfg = _cfg()
    chunks = chunk_markdown(
        "p.md", content, cfg.chunk_size, cfg.chunk_overlap, cfg.summary_max)
    stable = next(c for c in chunks if c.heading == "Stable")
    old_stable = store.make_record(stable, [1.0, 0.0])
    old_stable.ordinal = 99
    store.save_index(base.index_path(str(b), "backend"), [old_stable])
    monkeypatch.setattr(
        indexer,
        "embed_texts",
        lambda cfg, texts: [[1.0, 0.0] for _ in texts],
    )

    indexer.index_domain(cfg, str(b), "backend")

    recs = store.load_index(base.index_path(str(b), "backend"))
    current_stable = next(r for r in recs if r.heading == "Stable")
    assert current_stable.ordinal == 1


def test_append_log_writes_record(tmp_path):
    b = tmp_path / "wiki"
    (b / "backend").mkdir(parents=True)
    indexer.append_log(
        str(b), "backend", "ingest", "src/auth.py", "auth.md", src_hash="abc123"
    )
    line = open(base.log_path(str(b), "backend")).read().strip()
    rec = __import__("json").loads(line)
    assert rec["op"] == "ingest" and rec["page"] == "auth.md" and rec["src_hash"] == "abc123"
