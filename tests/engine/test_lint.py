import hashlib
import importlib
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone

from iwiki_mcp.engine.graph_store import GraphStore, SCHEMA_VERSION
from iwiki_mcp.graph import markdown_fingerprint
from iwiki_mcp.engine.lint import lint


def _wiki(tmp_path, pages: dict) -> str:
    wd = tmp_path / "wiki"
    wd.mkdir()
    for name, body in pages.items():
        (wd / name).write_text(body, encoding="utf-8")
    return str(wd)


def test_absent_wiki_is_noop(tmp_path):
    assert lint(str(tmp_path / "nope")) == {"wiki_present": False}


def test_engine_lint_remains_markdown_only_composition_input(tmp_path):
    wd = _wiki(tmp_path, {"a.md": "## A\nbody\n"})

    report = lint(wd)

    assert "broken" in report
    assert "code_graph" not in report


def test_detects_broken_ref(tmp_path):
    wd = _wiki(tmp_path, {"a.md": "## A\nlink to [[missing]] here\n"})
    out = lint(wd)
    assert any(b["ref"] == "missing" for b in out["broken"])


def test_code_fence_ref_not_broken(tmp_path):
    # page-level regression for P1: bash [[...]] in a fence is not a broken ref
    wd = _wiki(tmp_path, {
        "a.md": "## A\n```bash\nif [[ -d x ]]; then :; fi\n```\n[[b]]\n",
        "b.md": "## B\nbody\n",
    })
    assert lint(wd)["broken"] == []


def test_detects_orphan(tmp_path):
    wd = _wiki(tmp_path, {"a.md": "## A\nno links\n", "b.md": "## B\nno links\n"})
    out = lint(wd)
    assert set(out["orphans"]) == {
        os.path.normpath(os.path.join(wd, "a.md")),
        os.path.normpath(os.path.join(wd, "b.md")),
    }


def test_stale_ignores_legacy_and_malformed_log_records(tmp_path):
    wd = _wiki(tmp_path, {"a.md": "## A\nbody\n"})
    with open(os.path.join(wd, "log.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"op": "init", "scope": "x", "note": "legacy"}) + "\n")
        fh.write("not json at all\n")
    out = lint(wd)
    assert out["wiki_present"] is True
    assert out["stale"] == []   # records lacking source/page are tolerated, ignored


def test_section_findings_folded_into_report(tmp_path):
    # page with a ### deep heading → deep_heading surfaces; missing_overview is gone
    wd = _wiki(tmp_path, {"a.md": "## A\nlead.\n\n### deep\nx\n"})
    out = lint(wd)
    types = {f["type"] for f in out["sections"]}
    assert "deep_heading" in types
    assert "missing_overview" not in types
    assert all("page" in f for f in out["sections"])


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _wiki_with_log(tmp_path, page_body, src_body, src_hash=None):
    """Wiki dir with one page, one source file, and a single ingest log record
    (absolute paths). Returns (wiki_dir, src_path, page_path)."""
    wd = tmp_path / "wiki"
    wd.mkdir()
    page = wd / "a.md"
    page.write_text(page_body, encoding="utf-8")
    src = tmp_path / "a.py"
    src.write_text(src_body, encoding="utf-8")
    rec = {"op": "ingest", "source": str(src), "page": str(page)}
    if src_hash is not None:
        rec["src_hash"] = src_hash
    (wd / "log.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
    return str(wd), str(src), str(page)


def test_stale_hash_match_overrides_older_page_mtime(tmp_path):
    # The cure case: page mtime OLDER than source, but src_hash matches the
    # current source → NOT stale (kills git-reset / same-day false positives).
    wd, src, page = _wiki_with_log(
        tmp_path, "## A\nbody\n", "print('x')\n", src_hash=_h("print('x')\n"))
    os.utime(src, (2000, 2000))
    os.utime(page, (1000, 1000))
    assert lint(wd)["stale"] == []


def test_stale_hash_mismatch_is_stale_even_if_page_newer(tmp_path):
    # Hash recorded for OLD content; source now differs → stale regardless of mtime.
    wd, src, page = _wiki_with_log(
        tmp_path, "## A\nbody\n", "new content\n", src_hash=_h("old content\n"))
    os.utime(src, (1000, 1000))
    os.utime(page, (2000, 2000))
    assert any(s["source"] == src for s in lint(wd)["stale"])


def test_stale_resolves_domain_relative_logged_page(tmp_path):
    wd = _wiki(tmp_path, {"a.md": "## A\nbody\n"})
    src = tmp_path / "src.py"
    src.write_text("new content\n", encoding="utf-8")
    rec = {
        "op": "ingest",
        "source": str(src),
        "page": "a.md",
        "src_hash": _h("old content\n"),
    }
    with open(os.path.join(wd, "log.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")

    assert lint(wd)["stale"] == [
        {"page": os.path.normpath(os.path.join(wd, "a.md")), "source": str(src)}
    ]


def test_stale_without_hash_uses_mtime(tmp_path):
    # No src_hash in the record → unchanged mtime behaviour.
    wd, src, page = _wiki_with_log(tmp_path, "## A\nbody\n", "x\n")
    os.utime(src, (2000, 2000))
    os.utime(page, (1000, 1000))
    assert any(s["source"] == src for s in lint(wd)["stale"])
    os.utime(page, (3000, 3000))
    assert lint(wd)["stale"] == []


def test_stale_hash_present_but_unreadable_falls_back_to_mtime(tmp_path, monkeypatch):
    # src_hash present but source unreadable (_src_hash → None) → mtime path.
    import iwiki_mcp.engine.lint as lintmod
    wd, src, page = _wiki_with_log(
        tmp_path, "## A\nbody\n", "x\n", src_hash="deadbeefdeadbeef")
    monkeypatch.setattr(lintmod, "_src_hash", lambda p: None)
    os.utime(src, (2000, 2000))
    os.utime(page, (1000, 1000))
    assert any(s["source"] == src for s in lint(wd)["stale"])


def test_stale_last_wins_after_delete_and_reingest(tmp_path):
    # ingest(old hash) -> delete -> ingest(new hash matching current source):
    # last-wins => judged by the NEWEST record => NOT stale.
    wd = _wiki(tmp_path, {"a.md": "## A\nbody\n"})
    src = tmp_path / "s.py"
    src.write_text("new\n", encoding="utf-8")
    recs = [
        {"op": "ingest", "source": str(src), "page": "a.md", "src_hash": _h("old\n")},
        {"op": "delete", "source": "", "page": "a.md"},
        {"op": "ingest", "source": str(src), "page": "a.md", "src_hash": _h("new\n")},
    ]
    with open(os.path.join(wd, "log.jsonl"), "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")
    assert lint(wd)["stale"] == []


def test_missing_source_flags_absolute_gone(tmp_path):
    wd = _wiki(tmp_path, {"a.md": "## A\nbody\n"})
    gone = tmp_path / "gone.py"  # never created
    rec = {"op": "ingest", "source": str(gone), "page": "a.md"}
    open(os.path.join(wd, "log.jsonl"), "w", encoding="utf-8").write(
        json.dumps(rec) + "\n")
    assert lint(wd)["missing_source"] == [
        {"page": os.path.normpath(os.path.join(wd, "a.md")), "source": str(gone)}
    ]


def test_missing_source_present_not_flagged(tmp_path):
    wd, src, page = _wiki_with_log(tmp_path, "## A\nb\n", "x\n")
    assert lint(wd)["missing_source"] == []


def test_missing_source_empty_source_skipped(tmp_path):
    wd = _wiki(tmp_path, {"a.md": "## A\nb\n"})
    rec = {"op": "ingest", "source": "", "page": "a.md"}
    open(os.path.join(wd, "log.jsonl"), "w", encoding="utf-8").write(
        json.dumps(rec) + "\n")
    assert lint(wd)["missing_source"] == []


def test_missing_source_page_absent_skipped(tmp_path):
    wd = _wiki(tmp_path, {"keep.md": "## K\nx\n"})  # a.md never created
    gone = tmp_path / "gone.py"
    rec = {"op": "ingest", "source": str(gone), "page": "a.md"}
    open(os.path.join(wd, "log.jsonl"), "w", encoding="utf-8").write(
        json.dumps(rec) + "\n")
    assert lint(wd)["missing_source"] == []


def test_missing_source_relative_found_under_project_dir(tmp_path):
    wd = _wiki(tmp_path, {"a.md": "## A\nb\n"})
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "src.py").write_text("x\n", encoding="utf-8")
    rec = {"op": "ingest", "source": "src.py", "page": "a.md"}
    open(os.path.join(wd, "log.jsonl"), "w", encoding="utf-8").write(
        json.dumps(rec) + "\n")
    assert lint(wd, project_dir=str(proj))["missing_source"] == []


def test_missing_source_relative_absent_is_flagged(tmp_path, monkeypatch):
    wd = _wiki(tmp_path, {"a.md": "## A\nb\n"})
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)  # cwd fallback also lacks src.py
    rec = {"op": "ingest", "source": "src.py", "page": "a.md"}
    open(os.path.join(wd, "log.jsonl"), "w", encoding="utf-8").write(
        json.dumps(rec) + "\n")
    assert lint(wd, project_dir=str(empty))["missing_source"] == [
        {"page": os.path.normpath(os.path.join(wd, "a.md")), "source": "src.py"}
    ]


def test_missing_source_last_wins_after_delete_and_reingest(tmp_path):
    wd = _wiki(tmp_path, {"a.md": "## A\nb\n"})
    newsrc = tmp_path / "new.py"
    newsrc.write_text("x\n", encoding="utf-8")
    oldsrc = tmp_path / "old.py"  # never created
    recs = [
        {"op": "ingest", "source": str(oldsrc), "page": "a.md"},
        {"op": "delete", "source": "", "page": "a.md"},
        {"op": "ingest", "source": str(newsrc), "page": "a.md"},
    ]
    with open(os.path.join(wd, "log.jsonl"), "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")
    assert lint(wd)["missing_source"] == []


def test_broken_markdown_link_flagged(tmp_path):
    wd = _wiki(tmp_path, {"a.md": "## A\nlink [x](missing.md) here\n"})
    out = lint(wd)
    assert any(b["ref"] == "missing" for b in out["broken"])


def test_valid_markdown_anchor_matches_via_slug(tmp_path):
    wd = _wiki(tmp_path, {
        "a.md": "## A\nsee [B](b.md#the-section)\n",
        "b.md": "## The Section\nbody\n",
    })
    assert lint(wd)["broken"] == []


def test_broken_markdown_anchor_flagged(tmp_path):
    wd = _wiki(tmp_path, {
        "a.md": "## A\nsee [B](b.md#no-such)\n",
        "b.md": "## The Section\nbody\n",
    })
    assert any(b["ref"] == "b#no-such" for b in lint(wd)["broken"])


def test_reserved_target_is_reported_separately_and_never_broken(tmp_path):
    wd = _wiki(tmp_path, {
        "a.md": "# A\n\n## Links\n[Index](index.md) and [[log]].\n",
        "index.md": "# Generated index\n",
        "log.md": "# Generated log\n",
    })

    out = lint(wd)

    page = os.path.normpath(os.path.join(wd, "a.md"))
    assert out["broken"] == []
    assert out["reserved_target"] == [
        {"page": page, "ref": "index"},
        {"page": page, "ref": "log"},
    ]


def test_lint_missing_graph_is_read_only_and_reports_remediation(tmp_path):
    base_dir = tmp_path / "base"
    domain_dir = base_dir / "docs"
    domain_dir.mkdir(parents=True)
    (domain_dir / "a.md").write_text("# A\n\n## Body\ntext\n", encoding="utf-8")
    graph_path = base_dir / ".iwiki" / "graph.sqlite3"

    out = lint(
        str(domain_dir), domain="docs", base_dir=str(base_dir)
    )

    assert not graph_path.exists()
    assert out["graph"] == {
        "available": False,
        "schema_version": None,
        "state": "missing",
        "fingerprint_match": None,
        "missing_pages": [],
        "extra_pages": [],
        "missing_edges": [],
        "extra_edges": [],
        "anchor_mismatches": [],
        "reason": "graph database is missing",
        "hint": "run wiki_index('docs')",
    }


def _git(cwd, *args):
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    )


def _ready_graph(tmp_path):
    base_dir = tmp_path / "base"
    domain_dir = base_dir / "docs"
    domain_dir.mkdir(parents=True)
    (domain_dir / "a.md").write_text(
        "# A\n\n## Links\n[B](b.md#details)\n", encoding="utf-8"
    )
    (domain_dir / "b.md").write_text(
        "# B\n\n### Details\nbody\n", encoding="utf-8"
    )
    (domain_dir / "c.md").write_text(
        "# C\n\n## Body\nbody\n", encoding="utf-8"
    )
    _git(base_dir, "init", "-q")
    _git(base_dir, "config", "user.email", "t@t")
    _git(base_dir, "config", "user.name", "t")
    _git(base_dir, "add", "-A")
    _git(base_dir, "commit", "-q", "-m", "seed")
    fingerprint = markdown_fingerprint(str(base_dir), "docs")
    store = GraphStore(base_dir)
    store.rebuild_domain(
        "docs",
        domain_dir,
        markdown_fingerprint=fingerprint.value,
        fingerprint_provider=lambda: markdown_fingerprint(
            str(base_dir), "docs"
        ).value,
        indexed_commit=fingerprint.indexed_commit,
        indexed_at=datetime.now(timezone.utc).isoformat(),
    )
    return base_dir, domain_dir, store


def test_lint_reports_exact_ready_graph_parity_without_graph_writes(
    tmp_path, monkeypatch
):
    base_dir, domain_dir, store = _ready_graph(tmp_path)
    before = store.path.read_bytes()

    def fail_write(*args, **kwargs):
        raise AssertionError("lint must not mutate graph")

    monkeypatch.setattr(GraphStore, "connect", fail_write)
    monkeypatch.setattr(GraphStore, "rebuild_domain", fail_write)
    monkeypatch.setattr(GraphStore, "refresh_pages", fail_write)
    monkeypatch.setattr(GraphStore, "mark_domain_dirty", fail_write)

    out = lint(
        str(domain_dir), domain="docs", base_dir=str(base_dir)
    )

    assert out["broken"] == []
    assert out["graph"] == {
        "available": True,
        "schema_version": SCHEMA_VERSION,
        "state": "ready",
        "fingerprint_match": True,
        "missing_pages": [],
        "extra_pages": [],
        "missing_edges": [],
        "extra_edges": [],
        "anchor_mismatches": [],
    }
    assert store.path.read_bytes() == before


def test_lint_does_not_create_sqlite_sidecars_for_ready_graph(
    tmp_path, monkeypatch
):
    base_dir, domain_dir, store = _ready_graph(tmp_path)
    sidecars = [
        store.path.with_name(store.path.name + suffix)
        for suffix in ("-wal", "-shm")
    ]
    for sidecar in sidecars:
        assert not sidecar.exists()
    lint_module = importlib.import_module("iwiki_mcp.engine.lint")
    real_connect = sqlite3.connect
    observed_sidecars = []

    def observing_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        observed_sidecars.extend(path.name for path in sidecars if path.exists())
        return connection

    monkeypatch.setattr(lint_module.sqlite3, "connect", observing_connect)

    report = lint(str(domain_dir), domain="docs", base_dir=str(base_dir))

    assert report["graph"]["state"] == "ready"
    assert observed_sidecars == []
    assert all(not path.exists() for path in sidecars)


def test_lint_reads_quiescent_nonempty_wal_without_mutating_originals(tmp_path):
    base_dir, domain_dir, store = _ready_graph(tmp_path)
    writer = sqlite3.connect(store.path)
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    writer.execute(
        "UPDATE domains SET state = 'dirty' WHERE domain = 'docs'"
    )
    writer.commit()
    wal_path = store.path.with_name(store.path.name + "-wal")
    assert wal_path.stat().st_size > 0
    before = {
        path.name: path.read_bytes()
        for path in store.path.parent.iterdir()
        if path.name.startswith(store.path.name)
    }
    try:
        report = lint(str(domain_dir), domain="docs", base_dir=str(base_dir))
        after = {
            path.name: path.read_bytes()
            for path in store.path.parent.iterdir()
            if path.name.startswith(store.path.name)
        }
    finally:
        writer.close()

    assert report["graph"]["available"] is True
    assert report["graph"]["state"] == "dirty"
    assert after == before


def test_lint_rejects_nonempty_wal_that_changes_during_snapshot(
    tmp_path, monkeypatch
):
    base_dir, domain_dir, store = _ready_graph(tmp_path)
    writer = sqlite3.connect(store.path)
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    writer.execute(
        "UPDATE domains SET state = 'dirty' WHERE domain = 'docs'"
    )
    writer.commit()
    wal_path = store.path.with_name(store.path.name + "-wal")
    assert wal_path.stat().st_size > 0
    lint_module = importlib.import_module("iwiki_mcp.engine.lint")
    monkeypatch.setattr(lint_module, "shutil", shutil, raising=False)
    real_copyfile = shutil.copyfile
    raced = False

    def racing_copyfile(source, target):
        nonlocal raced
        result = real_copyfile(source, target)
        if not raced and os.fspath(source) == os.fspath(wal_path):
            raced = True
            writer.execute(
                "UPDATE domains SET indexed_at = 'raced' WHERE domain = 'docs'"
            )
            writer.commit()
        return result

    monkeypatch.setattr(lint_module.shutil, "copyfile", racing_copyfile)
    try:
        report = lint(str(domain_dir), domain="docs", base_dir=str(base_dir))
    finally:
        writer.close()

    assert raced is True
    assert report["graph"]["available"] is False
    assert report["graph"]["state"] == "busy"
    assert report["graph"]["reason"] == "graph database is busy"


def test_explicit_empty_domain_reports_graph_extras_but_legacy_call_is_noop(
    tmp_path,
):
    base_dir = tmp_path / "base"
    domain_dir = base_dir / "docs"
    domain_dir.mkdir(parents=True)
    store = GraphStore(base_dir)
    connection = store.connect()
    try:
        connection.execute(
            "INSERT INTO domains VALUES (?, NULL, ?, 'ready', ?)",
            ("docs", "stale", datetime.now(timezone.utc).isoformat()),
        )
        connection.execute(
            "INSERT INTO pages VALUES "
            "('docs/extra', 'docs', 'extra.md', 'content', 'links')"
        )
        connection.commit()
    finally:
        connection.close()

    assert lint(str(domain_dir)) == {"wiki_present": False}

    report = lint(
        str(domain_dir), domain="docs", base_dir=str(base_dir)
    )

    assert _ordinary_findings(report) == {
        "wiki_present": True,
        "pages": 0,
        "broken": [],
        "orphans": [],
        "stale": [],
        "missing_source": [],
        "legacy_wikilink": [],
        "sections": [],
        "missing_frontmatter": [],
        "tag_drift": [],
        "reserved_target": [],
        "unavailable_domain": [],
    }
    assert [
        page["page_id"] for page in report["graph"]["extra_pages"]
    ] == ["docs/extra"]


def _ordinary_findings(report):
    return {key: value for key, value in report.items() if key != "graph"}


def test_lint_ordinary_findings_ignore_every_graph_availability_state(tmp_path):
    base_dir, domain_dir, store = _ready_graph(tmp_path)
    ready = lint(str(domain_dir), domain="docs", base_dir=str(base_dir))
    ordinary = _ordinary_findings(ready)

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE domains SET state = 'dirty' WHERE domain = 'docs'"
        )
    dirty = lint(str(domain_dir), domain="docs", base_dir=str(base_dir))
    assert _ordinary_findings(dirty) == ordinary
    assert dirty["graph"]["state"] == "dirty"
    assert dirty["graph"]["hint"] == "run wiki_index('docs')"

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE domains SET state = 'rebuilding' WHERE domain = 'docs'"
        )
    rebuilding = lint(str(domain_dir), domain="docs", base_dir=str(base_dir))
    assert _ordinary_findings(rebuilding) == ordinary
    assert rebuilding["graph"]["state"] == "rebuilding"

    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA user_version = 999")
    incompatible = lint(str(domain_dir), domain="docs", base_dir=str(base_dir))
    assert _ordinary_findings(incompatible) == ordinary
    assert incompatible["graph"]["state"] == "incompatible"
    assert incompatible["graph"]["reason"] == "graph schema is incompatible"
    assert str(tmp_path) not in repr(incompatible["graph"])

    store.path.unlink()
    missing = lint(str(domain_dir), domain="docs", base_dir=str(base_dir))
    assert _ordinary_findings(missing) == ordinary
    assert missing["graph"]["state"] == "missing"

    store.path.write_bytes(b"not a sqlite database")
    corrupt = lint(str(domain_dir), domain="docs", base_dir=str(base_dir))
    assert _ordinary_findings(corrupt) == ordinary
    assert corrupt["graph"]["state"] == "corrupt"
    assert corrupt["graph"]["reason"] == "graph database is corrupt"
    assert str(tmp_path) not in repr(corrupt["graph"])


def test_lint_busy_graph_preserves_markdown_findings_and_sanitizes_reason(tmp_path):
    base_dir, domain_dir, store = _ready_graph(tmp_path)
    ordinary = _ordinary_findings(
        lint(str(domain_dir), domain="docs", base_dir=str(base_dir))
    )
    locker = sqlite3.connect(store.path)
    locker.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    locker.execute("PRAGMA journal_mode = DELETE")
    locker.execute("BEGIN EXCLUSIVE")
    try:
        report = lint(str(domain_dir), domain="docs", base_dir=str(base_dir))
    finally:
        locker.rollback()
        locker.close()

    assert _ordinary_findings(report) == ordinary
    assert report["graph"]["state"] == "busy"
    assert report["graph"]["reason"] == "graph database is busy"
    assert str(tmp_path) not in repr(report["graph"])


def test_lint_reports_exact_page_edge_anchor_and_fingerprint_drift(tmp_path):
    base_dir, domain_dir, store = _ready_graph(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE pages SET content_hash = 'bad' WHERE page_id = 'docs/a'"
        )
        connection.execute("DELETE FROM pages WHERE page_id = 'docs/c'")
        connection.execute("DELETE FROM anchors WHERE page_id = 'docs/c'")
        connection.execute(
            "INSERT INTO pages VALUES "
            "('docs/extra', 'docs', 'extra.md', 'extra-content', 'extra-links')"
        )
        connection.execute(
            "DELETE FROM edges WHERE source_page_id = 'docs/a'"
        )
        connection.execute(
            "INSERT INTO edges VALUES "
            "('docs/a', 'docs/ghost', '', 'intra', 'ghost.md')"
        )
        connection.execute(
            "INSERT INTO edges VALUES "
            "('docs/orphan', 'docs/b', '', 'intra', 'b.md')"
        )
        connection.execute(
            "DELETE FROM anchors WHERE page_id = 'docs/b' AND anchor = 'details'"
        )
        connection.execute(
            "INSERT INTO anchors VALUES ('docs/b', 'extra', 'Extra')"
        )
        connection.execute(
            "INSERT INTO anchors VALUES ('docs/orphan', 'orphan', 'Orphan')"
        )
        connection.execute(
            "UPDATE domains SET markdown_fingerprint = 'stale' "
            "WHERE domain = 'docs'"
        )

    graph = lint(
        str(domain_dir), domain="docs", base_dir=str(base_dir)
    )["graph"]

    assert graph["available"] is True
    assert graph["state"] == "ready"
    assert graph["fingerprint_match"] is False
    assert [page["page_id"] for page in graph["missing_pages"]] == [
        "docs/a", "docs/c"
    ]
    assert [page["page_id"] for page in graph["extra_pages"]] == [
        "docs/a", "docs/extra"
    ]
    assert [edge["target_page_id"] for edge in graph["missing_edges"]] == [
        "docs/b"
    ]
    assert [edge["target_page_id"] for edge in graph["extra_edges"]] == [
        "docs/ghost", "docs/b"
    ]
    assert graph["anchor_mismatches"] == [
        {
            "page_id": "docs/b",
            "missing": [{"anchor": "details", "heading": "Details"}],
            "extra": [{"anchor": "extra", "heading": "Extra"}],
        },
        {
            "page_id": "docs/c",
            "missing": [
                {"anchor": "body", "heading": "Body"},
                {"anchor": "c", "heading": "C"},
            ],
            "extra": [],
        },
        {
            "page_id": "docs/orphan",
            "missing": [],
            "extra": [{"anchor": "orphan", "heading": "Orphan"}],
        },
    ]
    assert graph["hint"] == "run wiki_index('docs')"


def test_legacy_wikilink_lists_only_unmigrated_pages(tmp_path):
    wd = _wiki(tmp_path, {
        "a.md": "## A\nold [[b]] link\n",
        "b.md": "## B\nnew [x](a.md) link\n",
    })
    assert lint(wd)["legacy_wikilink"] == [
        os.path.normpath(os.path.join(wd, "a.md"))
    ]
