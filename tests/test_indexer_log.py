import json

from iwiki_mcp import base, indexer


def _recs(b, domain):
    text = open(base.log_path(str(b), domain), encoding="utf-8").read()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_upsert_replaces_prior_ingest_for_page(tmp_path):
    b = tmp_path / "wiki"
    (b / "backend").mkdir(parents=True)
    indexer.append_log(str(b), "backend", "ingest", "s1", "p.md", "h1")
    indexer.append_log(str(b), "backend", "ingest", "s2", "other.md", "h2")

    indexer.upsert_ingest_log(str(b), "backend", "s1b", "p.md", "h3")

    recs = _recs(b, "backend")
    p = [r for r in recs if r["page"] == "p.md"]
    assert len(p) == 1
    assert p[0]["src_hash"] == "h3"
    assert p[0]["source"] == "s1b"
    assert any(r["page"] == "other.md" for r in recs)


def test_upsert_creates_log_when_absent(tmp_path):
    b = tmp_path / "wiki"
    (b / "backend").mkdir(parents=True)

    indexer.upsert_ingest_log(str(b), "backend", "s", "p.md", "h")

    recs = _recs(b, "backend")
    assert len(recs) == 1 and recs[0]["page"] == "p.md"
