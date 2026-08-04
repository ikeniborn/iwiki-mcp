from __future__ import annotations

from dataclasses import FrozenInstanceError
from hashlib import sha256
import builtins
from contextlib import contextmanager
import importlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


_VALID_V1_SCHEMA = """
CREATE TABLE domains (
    domain TEXT PRIMARY KEY,
    indexed_commit TEXT,
    markdown_fingerprint TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('ready', 'dirty', 'rebuilding')),
    indexed_at TEXT NOT NULL
);
CREATE TABLE pages (
    page_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    file TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    link_hash TEXT NOT NULL,
    UNIQUE(domain, file)
);
CREATE TABLE anchors (
    page_id TEXT NOT NULL,
    anchor TEXT NOT NULL,
    heading TEXT NOT NULL,
    PRIMARY KEY(page_id, anchor),
    FOREIGN KEY(page_id) REFERENCES pages(page_id) ON DELETE CASCADE
);
CREATE TABLE edges (
    source_page_id TEXT NOT NULL,
    target_page_id TEXT NOT NULL,
    target_anchor TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL CHECK (kind IN ('intra', 'cross')),
    raw_target TEXT NOT NULL,
    PRIMARY KEY(source_page_id, target_page_id, target_anchor),
    FOREIGN KEY(source_page_id) REFERENCES pages(page_id) ON DELETE CASCADE
);
CREATE INDEX edges_target_idx ON edges(target_page_id);
"""


def _graph_store_type():
    from iwiki_mcp.engine.graph_store import GraphStore

    return GraphStore


def _open_store(tmp_path: Path):
    store = _graph_store_type()(tmp_path)
    connection = store.connect()
    return store, connection


def test_build_domain_snapshot_is_sorted_immutable_and_normalized(tmp_path):
    from iwiki_mcp.engine.graph_store import build_domain_snapshot

    domain_dir = tmp_path / "docs"
    (domain_dir / "concept").mkdir(parents=True)
    (domain_dir / "zeta.md").write_text("# Zeta\n", encoding="utf-8")
    source = (
        "# Source\n"
        "## Deep Heading\n"
        "[Zulu](zeta.md#Zeta)\n"
        "[[zeta#Zeta]]\n"
        "[Remote](iwiki://other/concept/page#Remote%20Heading)\n"
    )
    (domain_dir / "concept" / "source.md").write_text(source, encoding="utf-8")

    snapshot = build_domain_snapshot("docs", domain_dir)

    assert snapshot.domain == "docs"
    assert [page.file for page in snapshot.pages] == [
        "concept/source.md",
        "zeta.md",
    ]
    source_page = snapshot.pages[0]
    assert source_page.page_id == "docs/concept/source"
    assert source_page.content_hash == sha256(source.encode()).hexdigest()
    assert [(anchor.page_id, anchor.anchor, anchor.heading) for anchor in snapshot.anchors] == [
        ("docs/concept/source", "source", "Source"),
        ("docs/concept/source", "deep-heading", "Deep Heading"),
        ("docs/zeta", "zeta", "Zeta"),
    ]
    assert [
        (
            edge.source_page_id,
            edge.target_page_id,
            edge.target_anchor,
            edge.kind,
            edge.raw_target,
        )
        for edge in snapshot.edges
    ] == [
        ("docs/concept/source", "docs/zeta", "zeta", "intra", "zeta#Zeta"),
        (
            "docs/concept/source",
            "other/concept/page",
            "remote-heading",
            "cross",
            "iwiki://other/concept/page#Remote%20Heading",
        ),
    ]
    with pytest.raises(FrozenInstanceError):
        source_page.file = "changed.md"


def test_snapshot_excludes_reserved_pages_sources_and_targets(tmp_path):
    from iwiki_mcp.engine.graph_store import build_domain_snapshot

    domain_dir = tmp_path / "docs"
    (domain_dir / "nested").mkdir(parents=True)
    (domain_dir / "index.md").write_text(
        "# Generated\n[Authored](nested/authored.md)\n", encoding="utf-8"
    )
    (domain_dir / "log.md").write_text("# Generated Log\n", encoding="utf-8")
    (domain_dir / "nested" / "authored.md").write_text(
        "# Authored\n[Index](index.md)\n[Log](log.md)\n[Nested](index.md#Index)\n",
        encoding="utf-8",
    )

    snapshot = build_domain_snapshot("docs", domain_dir)

    assert [page.page_id for page in snapshot.pages] == ["docs/nested/authored"]
    assert snapshot.edges == ()


def test_reserved_root_index_cannot_bridge_two_authored_pages(tmp_path):
    from iwiki_mcp.engine.graph_store import build_domain_snapshot

    domain_dir = tmp_path / "docs"
    domain_dir.mkdir()
    (domain_dir / "a.md").write_text(
        "# A\n[Index](index.md)\n", encoding="utf-8"
    )
    (domain_dir / "index.md").write_text(
        "# Index\n[B](b.md)\n", encoding="utf-8"
    )
    (domain_dir / "b.md").write_text("# B\n", encoding="utf-8")

    snapshot = build_domain_snapshot("docs", domain_dir)

    assert [page.page_id for page in snapshot.pages] == ["docs/a", "docs/b"]
    assert snapshot.edges == ()


def test_rebuild_stores_all_h1_through_h6_anchors(tmp_path):
    from iwiki_mcp.engine.graph_store import GraphStore

    domain_dir = tmp_path / "docs"
    domain_dir.mkdir()
    headings = "\n".join(
        f"{'#' * level} Heading {level}" for level in range(1, 7)
    )
    (domain_dir / "page.md").write_text(f"{headings}\n", encoding="utf-8")

    store = GraphStore(tmp_path)
    store.rebuild_domain(
        "docs",
        domain_dir,
        markdown_fingerprint="headings",
        fingerprint_provider=lambda: "headings",
        indexed_at="2026-08-04T12:00:00Z",
    )
    snapshot = store.load_ready_domain("docs")

    assert [
        (anchor.anchor, anchor.heading) for anchor in snapshot.anchors
    ] == [(f"heading-{level}", f"Heading {level}") for level in range(1, 7)]


def test_build_domain_snapshot_rechecks_directory_after_enumeration(
    tmp_path, monkeypatch
):
    from iwiki_mcp.engine.graph_store import build_domain_snapshot

    domain_dir = tmp_path / "docs"
    domain_dir.mkdir()
    original_rglob = Path.rglob

    def delete_empty_domain_after_scan(path, pattern):
        files = list(original_rglob(path, pattern))
        path.rmdir()
        return iter(files)

    monkeypatch.setattr(Path, "rglob", delete_empty_domain_after_scan)

    with pytest.raises(OSError, match="^domain directory is unavailable$"):
        build_domain_snapshot("docs", domain_dir)


def test_link_hash_depends_on_sorted_normalized_edges_not_authored_order(tmp_path):
    from iwiki_mcp.engine.graph_store import build_domain_snapshot

    domain_dir = tmp_path / "docs"
    domain_dir.mkdir()
    page = domain_dir / "source.md"
    page.write_text(
        "[B](target.md#Heading)\n[[target#Heading]]\n[Other](other.md)\n",
        encoding="utf-8",
    )
    first = build_domain_snapshot("docs", domain_dir).pages[0]
    page.write_text(
        "[Other](other.md)\n[[target#Heading]]\n[B](target.md#Heading)\n",
        encoding="utf-8",
    )
    second_snapshot = build_domain_snapshot("docs", domain_dir)
    second = second_snapshot.pages[0]

    assert first.content_hash != second.content_hash
    assert first.link_hash == second.link_hash
    assert [edge.raw_target for edge in second_snapshot.edges] == [
        "other.md",
        "target#Heading",
    ]


def test_link_hash_excludes_equivalent_authored_raw_target_syntax(tmp_path):
    from iwiki_mcp.engine.graph_store import build_domain_snapshot

    domain_dir = tmp_path / "docs"
    domain_dir.mkdir()
    page = domain_dir / "source.md"
    page.write_text("[Target](target.md#Heading)\n", encoding="utf-8")
    markdown_snapshot = build_domain_snapshot("docs", domain_dir)
    page.write_text("[[target#Heading]]\n", encoding="utf-8")
    wikilink_snapshot = build_domain_snapshot("docs", domain_dir)

    markdown_edge = markdown_snapshot.edges[0]
    wikilink_edge = wikilink_snapshot.edges[0]
    assert (
        markdown_edge.source_page_id,
        markdown_edge.target_page_id,
        markdown_edge.target_anchor,
        markdown_edge.kind,
    ) == (
        wikilink_edge.source_page_id,
        wikilink_edge.target_page_id,
        wikilink_edge.target_anchor,
        wikilink_edge.kind,
    )
    assert markdown_edge.raw_target != wikilink_edge.raw_target
    assert markdown_snapshot.pages[0].link_hash == wikilink_snapshot.pages[0].link_hash


def _graph_rows(store):
    with store.read_snapshot() as connection:
        return {
            table: [
                tuple(row)
                for row in connection.execute(
                    f"SELECT * FROM {table} ORDER BY {order}"
                )
            ]
            for table, order in (
                ("pages", "page_id"),
                ("anchors", "page_id, anchor"),
                (
                    "edges",
                    "source_page_id, target_page_id, target_anchor",
                ),
            )
        }


def test_incremental_refresh_matches_full_rebuild_after_create_delete_and_move(
    tmp_path,
):
    from iwiki_mcp.engine.graph_store import GraphStore

    domain_dir = tmp_path / "wiki" / "docs"
    domain_dir.mkdir(parents=True)
    initial_source = (
        "# Source\n"
        "[Target](target.md#Heading)\n"
        "[[target#Heading]]\n"
        "[Old](old.md)\n"
        "[Removed](removed.md)\n"
    )
    (domain_dir / "source.md").write_text(initial_source, encoding="utf-8")
    (domain_dir / "target.md").write_text("# Heading\n", encoding="utf-8")
    (domain_dir / "old.md").write_text("# Old\n", encoding="utf-8")
    (domain_dir / "removed.md").write_text("# Removed\n", encoding="utf-8")
    incremental = GraphStore(tmp_path / "incremental")
    incremental.rebuild_domain(
        "docs",
        domain_dir,
        markdown_fingerprint="initial",
        fingerprint_provider=lambda: "initial",
        indexed_commit="commit-1",
        indexed_at="2026-08-04T10:00:00Z",
    )

    final_source = (
        "# Source Changed\n"
        "[[target#Heading]]\n"
        "[Target](target.md#Heading)\n"
        "[New](new.md)\n"
    )
    (domain_dir / "source.md").write_text(final_source, encoding="utf-8")
    incremental.refresh_page("docs", "source.md", final_source)
    (domain_dir / "created.md").write_text("# Created\n", encoding="utf-8")
    incremental.refresh_page("docs", "created.md", "# Created\n")
    (domain_dir / "target.md").write_text("# Heading Changed\n", encoding="utf-8")
    incremental.refresh_page("docs", "target.md", "# Heading Changed\n")
    (domain_dir / "target.md").unlink()
    incremental.delete_page("docs", "target.md")
    (domain_dir / "removed.md").unlink()
    incremental.delete_page("docs", "removed.md")
    (domain_dir / "old.md").rename(domain_dir / "new.md")
    incremental.delete_page("docs", "old.md")
    incremental.refresh_page("docs", "new.md", "# Old\n")

    rebuilt = GraphStore(tmp_path / "rebuilt")
    rebuilt.rebuild_domain(
        "docs",
        domain_dir,
        markdown_fingerprint="final",
        fingerprint_provider=lambda: "final",
        indexed_commit="commit-2",
        indexed_at="2026-08-04T11:00:00Z",
    )

    assert _graph_rows(incremental) == _graph_rows(rebuilt)
    rows = _graph_rows(incremental)
    assert {row[0] for row in rows["pages"]} == {
        "docs/created",
        "docs/new",
        "docs/source",
    }
    assert (
        "docs/source",
        "docs/target",
        "heading",
        "intra",
        "target#Heading",
    ) in rows["edges"]


def test_refresh_page_rolls_back_page_anchors_and_edges_together(tmp_path):
    from iwiki_mcp.engine.graph_store import GraphStore, GraphStoreError

    store = GraphStore(tmp_path)
    store.refresh_page("docs", "page.md", "# Before\n[Good](good.md)\n")
    before = _graph_rows(store)
    connection = store.connect()
    connection.execute(
        "CREATE TRIGGER reject_bad_edge BEFORE INSERT ON edges "
        "WHEN NEW.target_page_id = 'docs/bad' "
        "BEGIN SELECT RAISE(ABORT, 'private rejection'); END"
    )
    connection.commit()
    connection.close()

    with pytest.raises(GraphStoreError, match="^graph transaction failed$"):
        store.refresh_page("docs", "page.md", "# After\n[Bad](bad.md)\n")

    assert _graph_rows(store) == before


def test_rebuild_commits_rebuilding_before_read_and_ready_with_watermark(
    tmp_path, monkeypatch
):
    from iwiki_mcp.engine import graph_store

    domain_dir = tmp_path / "docs"
    domain_dir.mkdir()
    (domain_dir / "page.md").write_text("# Page\n", encoding="utf-8")
    store = graph_store.GraphStore(tmp_path)
    store.mark_domain_dirty("docs")
    with store.read_snapshot() as connection:
        assert connection.execute(
            "SELECT state FROM domains WHERE domain = 'docs'"
        ).fetchone()[0] == "dirty"

    original_build = graph_store.build_domain_snapshot

    def observe_rebuilding(domain, path):
        with store.read_snapshot() as connection:
            assert connection.execute(
                "SELECT state FROM domains WHERE domain = ?", (domain,)
            ).fetchone()[0] == "rebuilding"
        return original_build(domain, path)

    monkeypatch.setattr(graph_store, "build_domain_snapshot", observe_rebuilding)
    store.rebuild_domain(
        "docs",
        domain_dir,
        markdown_fingerprint="fingerprint-2",
        fingerprint_provider=lambda: "fingerprint-2",
        indexed_commit="commit-2",
        indexed_at="2026-08-04T12:00:00Z",
    )

    with store.read_snapshot() as connection:
        assert tuple(
            connection.execute(
                "SELECT indexed_commit, markdown_fingerprint, state, indexed_at "
                "FROM domains WHERE domain = 'docs'"
            ).fetchone()
        ) == (
            "commit-2",
            "fingerprint-2",
            "ready",
            "2026-08-04T12:00:00Z",
        )
        assert connection.execute("SELECT page_id FROM pages").fetchone()[0] == (
            "docs/page"
        )


def test_rebuild_fingerprint_mismatch_preserves_old_rows_and_commits_dirty(
    tmp_path,
):
    from iwiki_mcp.engine.graph_store import GraphStore, GraphStoreError

    domain_dir = tmp_path / "docs"
    domain_dir.mkdir()
    page = domain_dir / "page.md"
    page.write_text("# Old\n", encoding="utf-8")
    store = GraphStore(tmp_path)
    store.rebuild_domain(
        "docs",
        domain_dir,
        markdown_fingerprint="old",
        fingerprint_provider=lambda: "old",
        indexed_at="2026-08-04T10:00:00Z",
    )
    old_rows = _graph_rows(store)
    page.write_text("# New\n", encoding="utf-8")

    def changed_fingerprint():
        (domain_dir / "late.md").write_text("# Late\n", encoding="utf-8")
        return "changed-after-read"

    with pytest.raises(GraphStoreError, match="^cannot rebuild domain graph$") as exc:
        store.rebuild_domain(
            "docs",
            domain_dir,
            markdown_fingerprint="expected-before-read",
            fingerprint_provider=changed_fingerprint,
            indexed_at="2026-08-04T11:00:00Z",
        )

    assert "changed-after-read" not in str(exc.value)
    assert _graph_rows(store) == old_rows
    with store.read_snapshot() as connection:
        assert connection.execute(
            "SELECT state FROM domains WHERE domain = 'docs'"
        ).fetchone()[0] == "dirty"


def test_rebuild_checks_fingerprint_after_build_inside_final_transaction(
    tmp_path, monkeypatch
):
    from iwiki_mcp.engine import graph_store

    domain_dir = tmp_path / "docs"
    domain_dir.mkdir()
    (domain_dir / "page.md").write_text("# Page\n", encoding="utf-8")
    store = graph_store.GraphStore(tmp_path)
    events = []
    active_connections = []
    original_build = graph_store.build_domain_snapshot
    original_transaction = store.transaction

    def observed_build(domain, path):
        events.append("build")
        return original_build(domain, path)

    @contextmanager
    def observed_transaction():
        with original_transaction() as connection:
            events.append("transaction")
            active_connections.append(connection)
            try:
                yield connection
            finally:
                active_connections.pop()

    def current_fingerprint():
        events.append("provider")
        assert active_connections
        assert active_connections[-1].in_transaction
        return "expected"

    monkeypatch.setattr(graph_store, "build_domain_snapshot", observed_build)
    monkeypatch.setattr(store, "transaction", observed_transaction)

    store.rebuild_domain(
        "docs",
        domain_dir,
        markdown_fingerprint="expected",
        fingerprint_provider=current_fingerprint,
        indexed_at="2026-08-04T11:00:00Z",
    )

    assert events == ["transaction", "build", "transaction", "provider"]


def test_rebuild_fingerprint_provider_failure_preserves_rows_and_commits_dirty(
    tmp_path,
):
    from iwiki_mcp.engine.graph_store import GraphStore, GraphStoreError

    domain_dir = tmp_path / "docs"
    domain_dir.mkdir()
    page = domain_dir / "page.md"
    page.write_text("# Old\n", encoding="utf-8")
    store = GraphStore(tmp_path)
    store.rebuild_domain(
        "docs",
        domain_dir,
        markdown_fingerprint="old",
        fingerprint_provider=lambda: "old",
        indexed_at="2026-08-04T10:00:00Z",
    )
    old_rows = _graph_rows(store)
    page.write_text("# New\n", encoding="utf-8")
    private_error = OSError("private fingerprint failure")

    def fail_fingerprint():
        raise private_error

    with pytest.raises(GraphStoreError, match="^cannot rebuild domain graph$") as exc:
        store.rebuild_domain(
            "docs",
            domain_dir,
            markdown_fingerprint="new",
            fingerprint_provider=fail_fingerprint,
            indexed_at="2026-08-04T11:00:00Z",
        )

    assert exc.value.__cause__ is private_error
    assert "private" not in str(exc.value)
    assert _graph_rows(store) == old_rows
    with store.read_snapshot() as connection:
        assert connection.execute(
            "SELECT state FROM domains WHERE domain = 'docs'"
        ).fetchone()[0] == "dirty"


def test_rebuild_restarts_preexisting_rebuilding_domain(tmp_path):
    from iwiki_mcp.engine.graph_store import GraphStore

    domain_dir = tmp_path / "docs"
    domain_dir.mkdir()
    (domain_dir / "page.md").write_text("# Page\n", encoding="utf-8")
    store = GraphStore(tmp_path)
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO domains VALUES (?, ?, ?, ?, ?)",
            ("docs", None, "old", "rebuilding", "2026-08-04T00:00:00Z"),
        )

    store.rebuild_domain(
        "docs",
        domain_dir,
        markdown_fingerprint="new",
        fingerprint_provider=lambda: "new",
        indexed_at="2026-08-04T12:00:00Z",
    )

    with store.read_snapshot() as connection:
        assert tuple(
            connection.execute(
                "SELECT markdown_fingerprint, state FROM domains WHERE domain = 'docs'"
            ).fetchone()
        ) == ("new", "ready")


def test_rebuild_parse_failure_commits_dirty_and_sanitizes_error(
    tmp_path, monkeypatch
):
    from iwiki_mcp.engine import graph_store

    store = graph_store.GraphStore(tmp_path)
    private_error = OSError("/private/wiki/page.md failed")

    def fail_build(domain, path):
        raise private_error

    monkeypatch.setattr(graph_store, "build_domain_snapshot", fail_build)

    with pytest.raises(
        graph_store.GraphStoreError, match="^cannot rebuild domain graph$"
    ) as exc:
        store.rebuild_domain(
            "docs",
            tmp_path / "missing-private-domain",
            markdown_fingerprint="new",
            fingerprint_provider=lambda: "new",
            indexed_commit=None,
            indexed_at="2026-08-04T12:00:00Z",
        )

    assert exc.value.__cause__ is private_error
    assert "private" not in str(exc.value)
    with store.read_snapshot() as connection:
        assert connection.execute(
            "SELECT state FROM domains WHERE domain = 'docs'"
        ).fetchone()[0] == "dirty"


@pytest.mark.parametrize("replacement", ["missing", "file"])
def test_rebuild_invalid_domain_preserves_old_rows_and_commits_dirty(
    tmp_path, replacement
):
    from iwiki_mcp.engine.graph_store import GraphStore, GraphStoreError

    domain_dir = tmp_path / "docs"
    domain_dir.mkdir()
    page = domain_dir / "old.md"
    page.write_text("# Old\n", encoding="utf-8")
    store = GraphStore(tmp_path)
    store.rebuild_domain(
        "docs",
        domain_dir,
        markdown_fingerprint="old",
        fingerprint_provider=lambda: "old",
        indexed_at="2026-08-04T10:00:00Z",
    )
    old_rows = _graph_rows(store)
    page.unlink()
    domain_dir.rmdir()
    if replacement == "file":
        domain_dir.write_text("not a domain", encoding="utf-8")

    with pytest.raises(GraphStoreError, match="^cannot rebuild domain graph$"):
        store.rebuild_domain(
            "docs",
            domain_dir,
            markdown_fingerprint="invalid",
            fingerprint_provider=lambda: "invalid",
            indexed_at="2026-08-04T11:00:00Z",
        )

    assert _graph_rows(store) == old_rows
    with store.read_snapshot() as connection:
        assert connection.execute(
            "SELECT state FROM domains WHERE domain = 'docs'"
        ).fetchone()[0] == "dirty"


def test_rebuild_accepts_existing_empty_domain_directory(tmp_path):
    from iwiki_mcp.engine.graph_store import GraphStore

    domain_dir = tmp_path / "empty"
    domain_dir.mkdir()
    store = GraphStore(tmp_path)

    store.rebuild_domain(
        "empty",
        domain_dir,
        markdown_fingerprint="empty",
        fingerprint_provider=lambda: "empty",
        indexed_at="2026-08-04T11:00:00Z",
    )

    snapshot = store.load_ready_domain("empty")
    assert snapshot.pages == ()
    assert snapshot.anchors == ()
    assert snapshot.edges == ()


def test_rebuild_store_failure_rolls_back_rows_then_commits_dirty(tmp_path):
    from iwiki_mcp.engine.graph_store import GraphStore, GraphStoreError

    domain_dir = tmp_path / "docs"
    domain_dir.mkdir()
    (domain_dir / "page.md").write_text("# Page\n", encoding="utf-8")
    store = GraphStore(tmp_path)
    connection = store.connect()
    connection.execute(
        "CREATE TRIGGER reject_page BEFORE INSERT ON pages "
        "BEGIN SELECT RAISE(ABORT, 'private rejection'); END"
    )
    connection.commit()
    connection.close()

    with pytest.raises(
        GraphStoreError, match="^cannot rebuild domain graph$"
    ) as exc:
        store.rebuild_domain(
            "docs",
            domain_dir,
            markdown_fingerprint="new",
            fingerprint_provider=lambda: "new",
            indexed_commit=None,
            indexed_at="2026-08-04T12:00:00Z",
        )

    assert isinstance(exc.value.__cause__, sqlite3.DatabaseError)
    assert "private rejection" not in str(exc.value)
    with store.read_snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM pages").fetchone()[0] == 0
        assert connection.execute(
            "SELECT state FROM domains WHERE domain = 'docs'"
        ).fetchone()[0] == "dirty"


@pytest.mark.parametrize("state", ["dirty", "rebuilding"])
def test_load_ready_domain_rejects_unavailable_states(tmp_path, state):
    from iwiki_mcp.engine.graph_store import GraphDomainUnavailable, GraphStore

    store = GraphStore(tmp_path)
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO domains VALUES (?, ?, ?, ?, ?)",
            ("docs", None, "old", state, "2026-08-04T00:00:00Z"),
        )

    with pytest.raises(GraphDomainUnavailable) as exc:
        store.load_ready_domain("docs")

    assert exc.value.state == state


def test_load_ready_domain_rejects_missing_and_returns_only_ready_domain(tmp_path):
    from iwiki_mcp.engine.graph_store import GraphDomainUnavailable, GraphStore

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "page.md").write_text(
        "# Page\n[Cross](iwiki://other/target)\n", encoding="utf-8"
    )
    store = GraphStore(tmp_path)
    with pytest.raises(GraphDomainUnavailable) as exc:
        store.load_ready_domain("docs")
    assert exc.value.state == "missing"

    store.rebuild_domain(
        "docs",
        docs_dir,
        markdown_fingerprint="ready",
        fingerprint_provider=lambda: "ready",
        indexed_commit=None,
        indexed_at="2026-08-04T12:00:00Z",
    )

    snapshot = store.load_ready_domain("docs")
    assert [page.page_id for page in snapshot.pages] == ["docs/page"]
    assert [edge.target_page_id for edge in snapshot.edges] == ["other/target"]


def test_wal_reader_keeps_old_ready_snapshot_during_public_rebuild(
    tmp_path, monkeypatch
):
    from iwiki_mcp.engine.graph_store import GraphDomainUnavailable, GraphStore

    domain_dir = tmp_path / "docs"
    domain_dir.mkdir()
    (domain_dir / "old.md").write_text("# Old\n", encoding="utf-8")
    store = GraphStore(tmp_path)
    store.rebuild_domain(
        "docs",
        domain_dir,
        markdown_fingerprint="old",
        fingerprint_provider=lambda: "old",
        indexed_commit=None,
        indexed_at="2026-08-04T10:00:00Z",
    )
    (domain_dir / "old.md").unlink()
    (domain_dir / "new.md").write_text("# New\n", encoding="utf-8")
    replacement_started = threading.Event()
    allow_commit = threading.Event()
    original_insert_snapshot = GraphStore._insert_snapshot

    def paused_insert_snapshot(connection, snapshot):
        original_insert_snapshot(connection, snapshot)
        replacement_started.set()
        assert allow_commit.wait(timeout=5)

    monkeypatch.setattr(
        GraphStore, "_insert_snapshot", staticmethod(paused_insert_snapshot)
    )

    with store.read_snapshot() as old_reader:
        assert old_reader.execute("SELECT page_id FROM pages").fetchone()[0] == (
            "docs/old"
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            rebuild = executor.submit(
                store.rebuild_domain,
                "docs",
                domain_dir,
                markdown_fingerprint="new",
                fingerprint_provider=lambda: "new",
                indexed_at="2026-08-04T11:00:00Z",
            )
            try:
                assert replacement_started.wait(timeout=5)
                assert old_reader.execute(
                    "SELECT page_id FROM pages"
                ).fetchone()[0] == "docs/old"
                with pytest.raises(GraphDomainUnavailable) as exc:
                    store.load_ready_domain("docs")
                assert exc.value.state == "rebuilding"
            finally:
                allow_commit.set()
            rebuild.result(timeout=5)
        assert old_reader.execute("SELECT page_id FROM pages").fetchone()[0] == (
            "docs/old"
        )

    assert store.load_ready_domain("docs").pages[0].page_id == "docs/new"


def test_graph_rebuild_does_not_import_embedding_modules(tmp_path, monkeypatch):
    from iwiki_mcp.engine import graph_store

    imported_embedding_modules = []
    original_import = builtins.__import__

    def guard_import(name, globals=None, locals=None, fromlist=(), level=0):
        if "embed" in name:
            imported_embedding_modules.append(name)
            raise AssertionError(f"embedding import during graph rebuild: {name}")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guard_import)
    reloaded = importlib.reload(graph_store)
    domain_dir = tmp_path / "docs"
    domain_dir.mkdir()
    (domain_dir / "page.md").write_text("# Page\n", encoding="utf-8")

    reloaded.GraphStore(tmp_path).rebuild_domain(
        "docs",
        domain_dir,
        markdown_fingerprint="ready",
        fingerprint_provider=lambda: "ready",
        indexed_commit=None,
        indexed_at="2026-08-04T12:00:00Z",
    )

    assert imported_embedding_modules == []


def test_graph_store_uses_base_local_graph_path_and_creates_directory(tmp_path):
    GraphStore = _graph_store_type()
    store = GraphStore(tmp_path)

    assert store.path == tmp_path / ".iwiki" / "graph.sqlite3"
    assert not store.path.parent.exists()

    connection = store.connect()
    connection.close()

    assert store.path.is_file()
    assert store.path.parent.is_dir()


def test_graph_store_initializes_schema_version_one(tmp_path):
    _, connection = _open_store(tmp_path)

    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert tables == {"domains", "pages", "anchors", "edges"}


def test_graph_store_configures_connection_pragmas(tmp_path):
    _, connection = _open_store(tmp_path)

    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("PRAGMA synchronous").fetchone()[0] == 1
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_schema_enforces_domain_state_and_page_uniqueness(tmp_path):
    _, connection = _open_store(tmp_path)

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO domains VALUES (?, ?, ?, ?, ?)",
            ("docs", None, "fingerprint", "unknown", "2026-08-04T00:00:00Z"),
        )

    connection.execute(
        "INSERT INTO domains VALUES (?, ?, ?, ?, ?)",
        ("docs", None, "fingerprint", "ready", "2026-08-04T00:00:00Z"),
    )
    connection.execute(
        "INSERT INTO pages VALUES (?, ?, ?, ?, ?)",
        ("page-1", "docs", "one.md", "content", "links"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO pages VALUES (?, ?, ?, ?, ?)",
            ("page-2", "docs", "one.md", "other-content", "other-links"),
        )


def test_schema_enforces_edges_and_cascades_source_page_rows(tmp_path):
    _, connection = _open_store(tmp_path)
    connection.execute(
        "INSERT INTO pages VALUES (?, ?, ?, ?, ?)",
        ("source", "docs", "source.md", "content", "links"),
    )
    connection.execute(
        "INSERT INTO pages VALUES (?, ?, ?, ?, ?)",
        ("target", "docs", "target.md", "content", "links"),
    )
    connection.execute(
        "INSERT INTO anchors VALUES (?, ?, ?)", ("source", "intro", "Intro")
    )
    connection.execute(
        "INSERT INTO edges(source_page_id, target_page_id, kind, raw_target) "
        "VALUES (?, ?, ?, ?)",
        ("source", "target", "intra", "target"),
    )

    edge = connection.execute(
        "SELECT target_anchor FROM edges WHERE source_page_id = 'source'"
    ).fetchone()
    assert edge[0] == ""

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO edges VALUES (?, ?, ?, ?, ?)",
            ("source", "elsewhere", "anchor", "external", "elsewhere"),
        )

    connection.execute("DELETE FROM pages WHERE page_id = 'source'")
    assert connection.execute("SELECT count(*) FROM anchors").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM edges").fetchone()[0] == 0


def test_edge_target_is_not_a_foreign_key(tmp_path):
    _, connection = _open_store(tmp_path)
    connection.execute(
        "INSERT INTO pages VALUES (?, ?, ?, ?, ?)",
        ("source", "docs", "source.md", "content", "links"),
    )
    connection.execute(
        "INSERT INTO pages VALUES (?, ?, ?, ?, ?)",
        ("target", "docs", "target.md", "content", "links"),
    )

    connection.execute(
        "INSERT INTO edges VALUES (?, ?, ?, ?, ?)",
        ("source", "target", "", "cross", "other/page"),
    )
    connection.execute("DELETE FROM pages WHERE page_id = 'target'")

    assert connection.execute("SELECT count(*) FROM edges").fetchone()[0] == 1


def test_schema_has_only_target_lookup_index_and_no_pages_domain_index(tmp_path):
    _, connection = _open_store(tmp_path)

    explicit_indexes = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'index' AND sql IS NOT NULL"
        )
    }
    assert explicit_indexes == {"edges_target_idx"}
    indexed_columns = [
        row[2] for row in connection.execute("PRAGMA index_info(edges_target_idx)")
    ]
    assert indexed_columns == ["target_page_id"]


def test_empty_version_zero_database_initializes(tmp_path):
    graph_path = tmp_path / ".iwiki" / "graph.sqlite3"
    graph_path.parent.mkdir()
    sqlite3.connect(graph_path).close()

    _, connection = _open_store(tmp_path)

    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_concurrent_version_zero_initialization_rechecks_schema_after_lock(
    tmp_path, monkeypatch
):
    from iwiki_mcp.engine import graph_store

    graph_path = tmp_path / ".iwiki" / "graph.sqlite3"
    graph_path.parent.mkdir()
    connection = sqlite3.connect(graph_path)
    assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    connection.close()

    migration_barrier = threading.Barrier(2)
    original_migrate_schema = graph_store.GraphStore._migrate_schema

    def synchronized_migrate_schema(self, connection, source_version):
        migration_barrier.wait(timeout=5)
        return original_migrate_schema(self, connection, source_version)

    monkeypatch.setattr(
        graph_store.GraphStore, "_migrate_schema", synchronized_migrate_schema
    )

    def connect_and_read_version():
        connection = graph_store.GraphStore(tmp_path).connect()
        try:
            return connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(connect_and_read_version) for _ in range(2)]

    assert [future.result() for future in futures] == [1, 1]


def test_version_zero_does_not_check_tables_before_migration_lock(
    tmp_path, monkeypatch
):
    from iwiki_mcp.engine import graph_store

    graph_path = tmp_path / ".iwiki" / "graph.sqlite3"
    graph_path.parent.mkdir()
    connection = sqlite3.connect(graph_path)
    assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    connection.close()

    second_outer_check_started = threading.Event()
    winner_connected = threading.Event()
    outer_check_lock = threading.Lock()
    outer_check_count = 0
    original_user_tables = graph_store.GraphStore._user_tables

    def synchronized_user_tables(connection):
        nonlocal outer_check_count
        if connection.in_transaction:
            return original_user_tables(connection)
        with outer_check_lock:
            outer_check_count += 1
            is_winner = outer_check_count == 1
        if is_winner:
            assert second_outer_check_started.wait(timeout=5)
        else:
            second_outer_check_started.set()
            assert winner_connected.wait(timeout=5)
        return original_user_tables(connection)

    monkeypatch.setattr(
        graph_store.GraphStore, "_user_tables", staticmethod(synchronized_user_tables)
    )

    def connect_and_read_version():
        connection = graph_store.GraphStore(tmp_path).connect()
        winner_connected.set()
        try:
            return connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(connect_and_read_version) for _ in range(2)]

    assert [future.result() for future in futures] == [1, 1]


class _PragmaResult:
    def __init__(self, value):
        self.value = value

    def fetchone(self):
        return (self.value,)


class _WalRetryConnection:
    def __init__(self, locked_attempts):
        self.locked_attempts = locked_attempts
        self.journal_attempts = 0
        self.row_factory = None
        self.statements = []

    def execute(self, statement):
        self.statements.append(statement)
        if statement == "PRAGMA journal_mode = WAL":
            self.journal_attempts += 1
            if self.journal_attempts <= self.locked_attempts:
                error = sqlite3.OperationalError("database is locked")
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise error
            return _PragmaResult("wal")
        return _PragmaResult(None)


def test_graph_store_sets_busy_timeout_before_bounded_wal_retry():
    from iwiki_mcp.engine import graph_store

    connection = _WalRetryConnection(locked_attempts=2)

    graph_store.GraphStore._configure(connection)

    assert connection.statements[0] == "PRAGMA busy_timeout = 5000"
    assert connection.journal_attempts == 3


def test_version_zero_with_unknown_tables_is_rejected_without_deleting_database(
    tmp_path,
):
    from iwiki_mcp.engine.graph_store import GraphSchemaError

    graph_path = tmp_path / ".iwiki" / "graph.sqlite3"
    graph_path.parent.mkdir()
    connection = sqlite3.connect(graph_path)
    connection.execute("CREATE TABLE foreign_data(value TEXT)")
    connection.commit()
    connection.close()

    with pytest.raises(GraphSchemaError, match="^unrecognized graph schema$"):
        _graph_store_type()(tmp_path).connect()

    assert graph_path.is_file()
    connection = sqlite3.connect(graph_path)
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchone()[0] == "foreign_data"


def test_newer_schema_is_rejected_with_sanitized_error(tmp_path):
    from iwiki_mcp.engine.graph_store import GraphSchemaError

    graph_path = tmp_path / ".iwiki" / "graph.sqlite3"
    graph_path.parent.mkdir()
    connection = sqlite3.connect(graph_path)
    connection.execute("PRAGMA user_version = 2")
    connection.close()

    with pytest.raises(GraphSchemaError, match="^unsupported graph schema version$") as exc:
        _graph_store_type()(tmp_path).connect()

    assert str(graph_path) not in str(exc.value)
    assert graph_path.is_file()


def test_corrupt_database_raises_sanitized_store_error_without_replacing_file(tmp_path):
    from iwiki_mcp.engine.graph_store import GraphStoreError

    graph_path = tmp_path / ".iwiki" / "graph.sqlite3"
    graph_path.parent.mkdir()
    contents = b"not a sqlite database\x00private payload"
    graph_path.write_bytes(contents)

    with pytest.raises(GraphStoreError, match="^cannot open graph store$") as exc:
        _graph_store_type()(tmp_path).connect()

    assert str(graph_path) not in str(exc.value)
    assert "private payload" not in str(exc.value)
    assert graph_path.read_bytes() == contents


def test_transaction_commits_and_rolls_back_as_one_unit(tmp_path):
    store, connection = _open_store(tmp_path)
    connection.close()

    with store.transaction() as transaction:
        transaction.execute(
            "INSERT INTO domains VALUES (?, ?, ?, ?, ?)",
            ("kept", None, "one", "ready", "2026-08-04T00:00:00Z"),
        )

    with pytest.raises(RuntimeError, match="stop"):
        with store.transaction() as transaction:
            transaction.execute(
                "INSERT INTO domains VALUES (?, ?, ?, ?, ?)",
                ("rolled-back", None, "two", "dirty", "2026-08-04T00:00:00Z"),
            )
            raise RuntimeError("stop")

    connection = store.connect()
    domains = {
        row[0] for row in connection.execute("SELECT domain FROM domains")
    }
    assert domains == {"kept"}


def test_transaction_sanitizes_database_errors_and_rolls_back(tmp_path):
    from iwiki_mcp.engine.graph_store import GraphStoreError

    store, connection = _open_store(tmp_path)
    connection.close()

    with pytest.raises(GraphStoreError, match="^graph transaction failed$") as exc:
        with store.transaction() as transaction:
            transaction.execute(
                "INSERT INTO domains VALUES (?, ?, ?, ?, ?)",
                (
                    "rolled-back",
                    None,
                    "one",
                    "ready",
                    "2026-08-04T00:00:00Z",
                ),
            )
            transaction.execute("SELECT private_transaction_token")

    assert isinstance(exc.value.__cause__, sqlite3.DatabaseError)
    assert "private_transaction_token" in str(exc.value.__cause__)
    assert "private_transaction_token" not in str(exc.value)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        transaction.execute("SELECT 1")

    connection = store.connect()
    assert connection.execute("SELECT count(*) FROM domains").fetchone()[0] == 0
    connection.close()


def test_read_snapshot_is_read_only_and_uses_immutable_rows(tmp_path):
    from iwiki_mcp.engine.graph_store import GraphStoreError

    store, connection = _open_store(tmp_path)
    connection.execute(
        "INSERT INTO domains VALUES (?, ?, ?, ?, ?)",
        ("docs", None, "one", "rebuilding", "2026-08-04T00:00:00Z"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(GraphStoreError, match="^graph read failed$") as exc:
        with store.read_snapshot() as snapshot:
            row = snapshot.execute(
                "SELECT domain, state FROM domains WHERE domain = ?", ("docs",)
            ).fetchone()
            assert tuple(row) == ("docs", "rebuilding")
            with pytest.raises(TypeError):
                row[0] = "changed"
            snapshot.execute("DELETE FROM domains")

    assert isinstance(exc.value.__cause__, sqlite3.DatabaseError)


def test_read_snapshot_sanitizes_database_errors_and_closes(tmp_path):
    from iwiki_mcp.engine.graph_store import GraphStoreError

    store, connection = _open_store(tmp_path)
    connection.close()

    with pytest.raises(GraphStoreError, match="^graph read failed$") as exc:
        with store.read_snapshot() as snapshot:
            snapshot.execute("SELECT private_read_token")

    assert isinstance(exc.value.__cause__, sqlite3.DatabaseError)
    assert "private_read_token" in str(exc.value.__cause__)
    assert "private_read_token" not in str(exc.value)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        snapshot.execute("SELECT 1")


def test_read_snapshot_preserves_non_database_exceptions(tmp_path):
    store, connection = _open_store(tmp_path)
    connection.close()
    user_error = RuntimeError("user callback failed")

    with pytest.raises(RuntimeError, match="^user callback failed$") as exc:
        with store.read_snapshot() as snapshot:
            raise user_error

    assert exc.value is user_error
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        snapshot.execute("SELECT 1")


def _write_version_one_database(graph_path: Path, schema: str) -> None:
    graph_path.parent.mkdir()
    connection = sqlite3.connect(graph_path)
    connection.executescript(schema)
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()


def test_version_one_without_tables_is_rejected_and_preserved(tmp_path):
    from iwiki_mcp.engine.graph_store import GraphSchemaError

    graph_path = tmp_path / ".iwiki" / "graph.sqlite3"
    _write_version_one_database(graph_path, "")

    with pytest.raises(GraphSchemaError, match="^incompatible graph schema$"):
        _graph_store_type()(tmp_path).connect()

    connection = sqlite3.connect(graph_path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    assert connection.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
    ).fetchone()[0] == 0
    connection.close()


@pytest.mark.parametrize(
    "schema",
    [
        _VALID_V1_SCHEMA.replace(
            "link_hash TEXT NOT NULL", "link_hash BLOB NOT NULL"
        ),
        _VALID_V1_SCHEMA.replace(
            "    link_hash TEXT NOT NULL,\n    UNIQUE(domain, file)\n",
            "    link_hash TEXT NOT NULL\n",
        ),
        _VALID_V1_SCHEMA.replace(
            "    PRIMARY KEY(page_id, anchor),\n"
            "    FOREIGN KEY(page_id) REFERENCES pages(page_id) ON DELETE CASCADE\n",
            "    PRIMARY KEY(page_id, anchor)\n",
        ),
        _VALID_V1_SCHEMA.replace(
            "    FOREIGN KEY(source_page_id) REFERENCES pages(page_id) ON DELETE CASCADE\n",
            "    FOREIGN KEY(target_page_id) REFERENCES pages(page_id) ON DELETE CASCADE\n",
        ),
        _VALID_V1_SCHEMA.replace(" DEFAULT ''", ""),
        _VALID_V1_SCHEMA.replace(
            "CHECK (state IN ('ready', 'dirty', 'rebuilding'))",
            "CHECK (state IN ('ready', 'dirty'))",
        ),
        _VALID_V1_SCHEMA.replace(
            "CHECK (kind IN ('intra', 'cross'))", "CHECK (kind IN ('intra'))"
        ),
        _VALID_V1_SCHEMA.replace(
            "    indexed_at TEXT NOT NULL\n);",
            "    indexed_at TEXT NOT NULL,\n"
            "    CHECK (state != 'dirty')\n);",
        ),
        _VALID_V1_SCHEMA.replace(
            "    raw_target TEXT NOT NULL,\n",
            "    raw_target TEXT NOT NULL,\n"
            "    CHECK (kind != 'cross'),\n",
        ),
        _VALID_V1_SCHEMA.replace(
            "CREATE INDEX edges_target_idx ON edges(target_page_id);", ""
        ),
        _VALID_V1_SCHEMA.replace(
            "CREATE INDEX edges_target_idx", "CREATE UNIQUE INDEX edges_target_idx"
        ),
        _VALID_V1_SCHEMA + "CREATE TABLE unexpected(value TEXT);",
    ],
    ids=[
        "wrong-column-type",
        "missing-page-unique",
        "missing-anchor-foreign-key",
        "wrong-edge-foreign-key",
        "missing-target-anchor-default",
        "wrong-domain-state-check",
        "wrong-edge-kind-check",
        "extra-domain-state-check",
        "extra-edge-kind-check",
        "missing-target-index",
        "unique-target-index",
        "extra-table",
    ],
)
def test_malformed_version_one_schema_is_rejected(tmp_path, schema):
    from iwiki_mcp.engine.graph_store import GraphSchemaError

    graph_path = tmp_path / ".iwiki" / "graph.sqlite3"
    _write_version_one_database(graph_path, schema)

    with pytest.raises(GraphSchemaError, match="^incompatible graph schema$"):
        _graph_store_type()(tmp_path).connect()

    assert graph_path.is_file()


def test_failing_known_migration_rolls_back_schema_and_version(tmp_path, monkeypatch):
    from iwiki_mcp.engine import graph_store

    graph_path = tmp_path / ".iwiki" / "graph.sqlite3"
    graph_path.parent.mkdir()
    sqlite3.connect(graph_path).close()
    monkeypatch.setitem(
        graph_store._MIGRATIONS,
        0,
        (
            1,
            (
                "CREATE TABLE partial(value TEXT)",
                "THIS IS NOT VALID SQL",
            ),
        ),
    )

    with pytest.raises(graph_store.GraphStoreError, match="^cannot open graph store$"):
        graph_store.GraphStore(tmp_path).connect()

    connection = sqlite3.connect(graph_path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    assert connection.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
    ).fetchone()[0] == 0
    connection.close()
