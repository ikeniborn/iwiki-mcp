import json
import os

from iwiki_mcp import base, indexer, server


def _seed_legacy_domain(tmp_path, monkeypatch):
    """A domain still on the pre-Unit-1 layout: store/log under .iwiki/."""
    b = tmp_path / "wiki"
    dom = b / "backend"
    legacy = dom / ".iwiki"
    legacy.mkdir(parents=True)
    legacy_records = [
        {"op": "ingest", "source": "src/a.py", "page": "a.md",
         "date": "2024-01-01", "src_hash": "aaaa"},
        {"op": "ingest", "source": "src/b.py", "page": "b.md",
         "date": "2024-01-02", "src_hash": "bbbb"},
    ]
    with open(legacy / "log.jsonl", "w", encoding="utf-8") as fh:
        for rec in legacy_records:
            fh.write(json.dumps(rec) + "\n")
    (legacy / "index.jsonl").touch()  # empty legacy index; only log history matters here

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text('read = ["backend"]\nwrite = "backend"\n')
    monkeypatch.setenv("IWIKI_BASE_DIR", str(b))
    monkeypatch.setenv("IWIKI_PROJECT_DIR", str(proj))
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", "2")
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, t: [[1.0, 0.0] for _ in t])
    return str(b), str(proj), legacy_records


def test_write_page_on_legacy_domain_preserves_prior_log_history(tmp_path, monkeypatch):
    b, _, legacy_records = _seed_legacy_domain(tmp_path, monkeypatch)

    md = "# Auth\n## Overview\nsummary\n## Flow\nlogin then token\n"
    out = server.wiki_write_page("backend", "auth", md)

    assert "error" not in out, out
    root_log = base.log_path(b, "backend")
    assert os.path.isfile(root_log)
    lines = open(root_log, encoding="utf-8").read().splitlines()
    recs = [json.loads(line) for line in lines if line.strip()]
    # All prior legacy history must survive migration, plus the new write's record.
    assert len(recs) == 3
    for legacy_rec in legacy_records:
        assert legacy_rec in recs
    assert any(r.get("page") == "concept/auth.md" for r in recs)
    # The legacy .iwiki dir is fully migrated away.
    assert not (tmp_path / "wiki" / "backend" / ".iwiki").exists()
