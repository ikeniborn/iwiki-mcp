"""PostgreSQL publication session lifecycle, ownership, and activation tests."""
from __future__ import annotations

import pytest


pytestmark = pytest.mark.postgres_integration


def test_begin_stages_one_session_with_fixed_owner_and_lease(pg_graph):
    session = pg_graph.begin()

    stored = pg_graph.session(session)
    assert stored["state"] == "staging"
    assert stored["owner_id"] == pg_graph.owner_id
    assert stored["lease_expires_at"] == session.lease_expires_at
    assert session.base_snapshot_revision is None
    assert pg_graph.reader_status()["state"] == "missing"


def test_batches_replay_by_hash_and_reject_conflicting_payloads(pg_graph):
    session = pg_graph.begin()
    first = pg_graph.batches[0]

    assert pg_graph.publish_batch(session, first) == {"accepted": True}
    assert pg_graph.publish_batch(session, first) == {"accepted": True}
    assert pg_graph.publish_batch(session, pg_graph.tampered(first)) == {
        "error": "batch_conflict",
        "hint": "resend the identical batch or begin a new session",
    }
    assert pg_graph.batch_count(session) == 1


def test_finalize_rejects_incomplete_snapshots(pg_graph):
    session = pg_graph.begin()
    pg_graph.publish_batch(session, pg_graph.batches[0])

    assert pg_graph.finalize(session) == {
        "error": "snapshot_incomplete",
        "hint": "publish every expected batch before finalizing",
    }
    assert pg_graph.reader_status()["state"] == "missing"


def test_finalize_recomputes_header_graph_revision(pg_graph):
    session = pg_graph.begin(
        header=pg_graph.header_with_revision("sha256:" + "0" * 64)
    )
    pg_graph.upload_all(session)

    assert pg_graph.finalize(session)["error"] == "revision_mismatch"
    assert pg_graph.reader_status()["state"] == "missing"


def test_finalize_activates_one_ready_snapshot_and_replays_terminally(pg_graph):
    session = pg_graph.complete_session()

    result = pg_graph.finalize(session)
    assert result["state"] == "ready"
    assert result["snapshot_revision"].startswith("sha256:")
    assert pg_graph.finalize(session) == result

    status = pg_graph.reader_status()
    assert status["state"] == "ready"
    assert status["snapshot_revision"] == result["snapshot_revision"]
    assert pg_graph.active_rows() == pg_graph.expected_counts


def test_same_domain_publishers_use_optimistic_conflict(pg_graph):
    first = pg_graph.complete_session()
    second = pg_graph.complete_session()

    ready = pg_graph.finalize(first)
    assert ready["state"] == "ready"
    assert pg_graph.finalize(second) == {
        "error": "snapshot_conflict",
        "hint": "begin a new publication session and retry",
    }
    assert pg_graph.reader_status()["snapshot_revision"] == ready[
        "snapshot_revision"
    ]


def test_markdown_change_between_begin_and_finalize_conflicts(pg_graph):
    session = pg_graph.complete_session()
    pg_graph.write_markdown_page("architecture", "# Architecture\n\n## Body\ntext\n")

    assert pg_graph.finalize(session) == {
        "error": "snapshot_conflict",
        "hint": "begin a new publication session and retry",
    }
    assert pg_graph.reader_status()["state"] == "missing"


def test_expired_lease_rejects_every_mutation(pg_graph):
    session = pg_graph.begin()
    pg_graph.advance_clock(pg_graph.session_ttl_seconds + 1)

    assert pg_graph.publish_batch(session, pg_graph.batches[0]) == {
        "error": "session_expired",
        "hint": "begin a new publication session",
    }
    assert pg_graph.finalize(session)["error"] == "session_expired"


def test_accepted_batches_renew_the_lease_and_rejections_do_not(pg_graph):
    session = pg_graph.begin()
    pg_graph.advance_clock(1)

    pg_graph.publish_batch(session, pg_graph.batches[0])
    renewed = pg_graph.session(session)["lease_expires_at"]
    assert renewed > session.lease_expires_at

    pg_graph.publish_batch(session, pg_graph.tampered(pg_graph.batches[0]))
    assert pg_graph.session(session)["lease_expires_at"] == renewed


def test_replacement_publisher_cannot_take_over_session(pg_graph):
    session = pg_graph.begin()
    replacement = pg_graph.reopen_with_new_ephemeral_owner()

    assert replacement.publish_batch(session, pg_graph.batches[0]) == {
        "error": "unauthorized",
        "hint": "this publisher does not own the session",
    }
    assert replacement.abort(session) == {
        "error": "unauthorized",
        "hint": "this publisher does not own the session",
    }
    assert pg_graph.session(session)["state"] == "staging"


def test_abort_releases_staging_without_touching_the_active_snapshot(pg_graph):
    ready = pg_graph.finalize(pg_graph.complete_session())
    session = pg_graph.complete_session()

    assert pg_graph.abort(session) == {"state": "aborted"}
    assert pg_graph.session(session)["state"] == "aborted"
    assert pg_graph.batch_count(session) == 0
    assert pg_graph.reader_status()["snapshot_revision"] == ready[
        "snapshot_revision"
    ]


def test_begin_cleans_bounded_expired_staging_sessions(pg_graph):
    stale = [pg_graph.begin() for _attempt in range(3)]
    pg_graph.advance_clock(
        pg_graph.session_ttl_seconds + pg_graph.staging_retention_seconds + 1
    )

    pg_graph.begin()

    remaining = [
        session for session in stale if pg_graph.session(session) is not None
    ]
    assert len(remaining) == 3 - pg_graph.staging_cleanup_limit


def test_finalize_waits_for_configured_lock_timeout(pg_graph):
    session = pg_graph.complete_session()

    lease = pg_graph.session(session)["lease_expires_at"]
    with pg_graph.hold_domain_advisory_lock():
        result = pg_graph.finalize(session)

    assert result == {
        "error": "busy",
        "hint": "another publication holds this domain",
        "retryable": True,
    }
    assert pg_graph.session(session)["lease_expires_at"] == lease
    assert pg_graph.finalize(session)["state"] == "ready"


def test_separate_domains_finalize_without_blocking_each_other(pg_graph):
    other = pg_graph.for_domain("private")
    first = pg_graph.complete_session()
    second = other.complete_session()

    with pg_graph.hold_domain_advisory_lock():
        assert other.finalize(second)["state"] == "ready"

    assert pg_graph.finalize(first)["state"] == "ready"


class _CountingCursor:
    """Cursor proxy counting the PostgreSQL commands one publication issues."""

    def __init__(self, cursor, commands):
        self._cursor = cursor
        self._commands = commands

    def __repr__(self):
        return "<counting publication cursor>"

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self._cursor.__exit__(*exc_info)

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def execute(self, *args, **kwargs):
        self._commands.append("execute")
        return self._cursor.execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        parameters = list(args[1])
        self._commands.extend("execute" for _item in parameters)
        return self._cursor.executemany(args[0], parameters, **kwargs)


class _CountingConnection:
    """Connection proxy handing out counting cursors."""

    def __init__(self, connection, commands):
        self._connection = connection
        self._commands = commands

    def __repr__(self):
        return "<counting publication connection>"

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, *exc):
        return self._connection.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def cursor(self, *args, **kwargs):
        return _CountingCursor(
            self._connection.cursor(*args, **kwargs), self._commands
        )


def _relation_heavy_rows(rows, count):
    """Return the fixture rows with `count` relations over the same symbols."""
    relation = rows["relations"][0]
    return {
        **rows,
        "relations": [
            {**relation, "relation_id": f"relation-{index}"}
            for index in range(count)
        ],
    }


def test_activation_cost_is_bounded_by_row_kinds_not_row_count(pg_graph):
    """Activation must not execute one SQL command per published row or link."""
    import psycopg

    from iwiki_mcp.codegraph import publication
    from iwiki_mcp.postgres.codegraph import PostgresCodeGraphStore

    rows = _relation_heavy_rows(pg_graph.rows, 300)
    header = pg_graph.header_with_counts(rows)
    commands = []
    store = PostgresCodeGraphStore(
        pg_graph.dsn,
        pg_graph.iwiki_id,
        pg_graph.domain,
        "owner-counting",
        lock_timeout_ms=500,
        session_ttl_seconds=60,
        staging_retention_seconds=60,
        staging_cleanup_limit=2,
        connection_factory=lambda: _CountingConnection(
            psycopg.connect(pg_graph.dsn), commands
        ),
    )
    pg_graph.write_markdown_page(
        "architecture", _selector_page("pkg.module_0.run")
    )
    session = store.begin(header)
    for batch in publication.iter_snapshot_batches(
        rows, max_rows=1000, max_bytes=1_000_000
    ):
        assert store.publish_batch(session, batch) == {"accepted": True}

    commands.clear()
    result = store.finalize(session)

    assert result["state"] == "ready"
    assert result["counts"]["relations"] == 300
    assert result["wiki_links"] == 300
    assert len(commands) < 30
    assert pg_graph.active_rows()["relations"] == 300


def test_readers_never_observe_staging_rows(pg_graph):
    ready = pg_graph.finalize(pg_graph.complete_session())
    staged = pg_graph.complete_session()

    status = pg_graph.reader_status()
    assert status["snapshot_revision"] == ready["snapshot_revision"]
    assert pg_graph.active_rows() == pg_graph.expected_counts
    assert pg_graph.snapshot_state(staged) == "staging"


def _selector_page(qualified_name):
    return (
        "---\n"
        "type: concept\n"
        "title: Architecture\n"
        "description: architecture page\n"
        "tags: [fixture]\n"
        "status: stable\n"
        "code:\n"
        "  symbols:\n"
        f"    - qualified_name: {qualified_name}\n"
        "---\n"
        "# Architecture\n\n## Body\ntext\n"
    )


def test_target_owns_derived_links_from_page_selectors(pg_graph):
    pg_graph.write_markdown_page(
        "architecture", _selector_page("pkg.module_0.run")
    )
    ready = pg_graph.finalize(pg_graph.complete_session())

    assert ready["wiki_links"] == 1
    links = pg_graph.wiki_links()
    assert [row[0] for row in links] == ["relation-0"]
    assert links[0][1] == {"kind": "symbol", "source": "pkg.module_0.run"}


def test_publisher_batches_cannot_supply_wiki_links(pg_graph):
    pg_graph.write_markdown_page(
        "architecture", _selector_page("pkg.module_0.run")
    )
    session = pg_graph.begin()
    pg_graph.upload_all(session)

    assert pg_graph.finalize(session)["state"] == "ready"
    assert [row[0] for row in pg_graph.wiki_links()] == ["relation-0"]


def test_snapshot_binds_the_canonical_markdown_revision(pg_graph):
    pg_graph.write_markdown_page(
        "architecture", _selector_page("pkg.module_0.run")
    )
    snapshot = pg_graph.markdown_snapshot()
    ready = pg_graph.finalize(pg_graph.complete_session())

    assert ready["markdown_revision"] == snapshot.revision
    assert snapshot.revision.startswith("sha256:")
    status = pg_graph.reader_status()
    assert status["markdown_revision"] == snapshot.revision
    assert status["wiki_links_stale"] is False


def test_status_and_lint_report_stale_links_after_markdown_changes(pg_graph):
    pg_graph.write_markdown_page(
        "architecture", _selector_page("pkg.module_0.run")
    )
    pg_graph.finalize(pg_graph.complete_session())
    pg_graph.write_markdown_page("guide", _selector_page("pkg.module_1.run"))

    status = pg_graph.reader_status()
    assert status["wiki_links_stale"] is True
    assert status["stored_markdown_generation"] != status[
        "current_markdown_generation"
    ]

    report = pg_graph.lint()["code_graph"]
    assert report["wiki_links_stale"] is True
    assert report["stored_markdown_revision"].startswith("sha256:")
    assert report["current_markdown_revision"].startswith("sha256:")
    assert report["stored_markdown_revision"] != report[
        "current_markdown_revision"
    ]
    assert report["stored_change_token"] != report["current_change_token"]


def test_selector_update_republishes_wiki_context(
    pg_ranked_graph, hosted_empty_code
):
    import json

    from iwiki_mcp import server
    from iwiki_mcp.codegraph.publication import (
        header_payload,
        iter_snapshot_batches,
    )

    graph = hosted_empty_code.graph
    assert graph is pg_ranked_graph
    page_slug = "concept/selector-update-hydration"
    symbol = graph.rows["symbols"][0]
    source_relations = [
        relation
        for relation in graph.rows["relations"]
        if relation["source_symbol_id"] == symbol["symbol_id"]
    ]
    assert source_relations
    assert all(
        relation["source_file_id"] == symbol["file_id"]
        for relation in source_relations
    )
    store = graph.markdown_store()
    store.write_page(
        graph.domain,
        page_slug,
        "# Selector Update Hydration\n\n## Body\ntext\n",
    )

    def publish():
        session = server.wiki_code_publish_begin(header_payload(graph.header))
        assert set(session) >= {
            "session_id",
            "max_batch_rows",
            "max_batch_bytes",
        }
        batches = iter_snapshot_batches(
            graph.rows,
            max_rows=session["max_batch_rows"],
            max_bytes=session["max_batch_bytes"],
        )
        for batch in batches:
            accepted = server.wiki_code_publish_batch(
                session["session_id"],
                batch.kind,
                batch.ordinal,
                json.loads(bytes(batch.payload).decode("utf-8")),
                batch.payload_hash,
            )
            assert accepted == {"accepted": True}
        return server.wiki_code_publish_finalize(session["session_id"])

    initial = publish()
    assert initial["state"] == "ready"
    assert initial["wiki_links"] == 0

    before = store.read_page(graph.domain, page_slug)
    updated = server.wiki_update_page(
        graph.domain,
        page_slug,
        code={"symbols": [{"qualified_name": symbol["qualified_name"]}]},
        expected_revision=before["revision"],
    )

    assert set(updated) == {"page", "revision", "indexed_chunks"}
    assert updated["page"] == f"{graph.domain}/{page_slug}.md"
    assert updated["revision"] == before["revision"] + 1
    stale = server.wiki_code_status()
    assert stale["state"] == "ready"
    assert stale["wiki_links_stale"] is True
    assert stale["stored_markdown_generation"] != stale[
        "current_markdown_generation"
    ]
    stale_context = server.wiki_code_context(
        [symbol["symbol_id"]], include_wiki=True
    )
    assert stale_context["wiki_pages"] == []
    assert "wiki_links_stale" in stale_context["warnings"]

    republished = publish()
    assert republished["state"] == "ready"
    assert republished["wiki_links"] > 0
    fresh = server.wiki_code_status()
    assert fresh["state"] == "ready"
    assert fresh["wiki_links_stale"] is False

    context = server.wiki_code_context(
        [symbol["symbol_id"]], include_wiki=True
    )
    assert context["state"] == "ready"
    assert context["wiki_links_stale"] is False
    assert page_slug in {page["page_id"] for page in context["wiki_pages"]}


def _active_wiki_links(graph):
    """Read the links of the snapshot that is active right now.

    The fixture accessor spans every snapshot of the domain, and a
    republication activates a new one beside the old, so a refresh and a
    publication are only comparable when both are read snapshot-scoped.
    """
    domain_id = graph._domain_id()
    active = graph._query(
        "SELECT active_snapshot_id FROM iwiki.code_graph_domain_state "
        "WHERE iwiki_id = %s AND domain_id = %s",
        (graph.iwiki_id, domain_id),
        admin=True,
    )
    return graph._query(
        "SELECT relation_id, selector FROM iwiki.code_graph_wiki_links "
        "WHERE iwiki_id = %s AND domain_id = %s AND snapshot_id = %s "
        "ORDER BY relation_id",
        (graph.iwiki_id, domain_id, active[0][0]),
        admin=True,
    )


def test_refresh_rederives_wiki_links_without_touching_the_graph(pg_graph):
    pg_graph.write_markdown_page(
        "architecture", _selector_page("pkg.module_0.run")
    )
    pg_graph.finalize(pg_graph.complete_session())
    before_status = pg_graph.reader_status()
    before_rows = pg_graph.active_rows()

    pg_graph.write_markdown_page("guide", _selector_page("pkg.module_1.run"))
    assert pg_graph.reader_status()["wiki_links_stale"] is True

    result = pg_graph.store.refresh_wiki_links()

    assert result["state"] == "ready"
    assert result["wiki_links_stale"] is False
    assert result["snapshot_revision"] == before_status["snapshot_revision"]
    assert result["markdown_revision"] != result["previous_markdown_revision"]

    after_status = pg_graph.reader_status()
    assert after_status["wiki_links_stale"] is False
    assert after_status["snapshot_revision"] == before_status["snapshot_revision"]
    assert pg_graph.active_rows() == before_rows

    report = pg_graph.lint()["code_graph"]
    assert report["wiki_links_stale"] is False
    assert report["stored_markdown_revision"] == report[
        "current_markdown_revision"
    ]


def test_refresh_matches_what_a_full_publication_derives(pg_graph):
    pg_graph.write_markdown_page(
        "architecture", _selector_page("pkg.module_0.run")
    )
    pg_graph.finalize(pg_graph.complete_session())
    pg_graph.write_markdown_page("guide", _selector_page("pkg.module_1.run"))

    pg_graph.store.refresh_wiki_links()
    refreshed = _active_wiki_links(pg_graph)

    pg_graph.finalize(pg_graph.complete_session())
    republished = _active_wiki_links(pg_graph)

    assert refreshed == republished
    assert refreshed


def test_refresh_without_an_active_snapshot_refuses(pg_graph):
    result = pg_graph.store.refresh_wiki_links()

    assert result["state"] == "missing_snapshot"
    assert "publish" in result["hint"]


def test_refresh_advances_the_revision_when_no_link_changed(pg_graph):
    pg_graph.write_markdown_page(
        "architecture", _selector_page("pkg.module_0.run")
    )
    pg_graph.finalize(pg_graph.complete_session())
    pg_graph.write_markdown_page("prose", "# Prose\n\n## Body\ntext\n")
    before = pg_graph.wiki_links()
    assert pg_graph.reader_status()["wiki_links_stale"] is True

    result = pg_graph.store.refresh_wiki_links()

    assert pg_graph.wiki_links() == before
    assert result["markdown_revision"] != result["previous_markdown_revision"]
    assert pg_graph.reader_status()["wiki_links_stale"] is False


def test_a_page_pinned_by_a_superseded_snapshot_still_deletes(pg_graph):
    """The derived links of an old snapshot must not outrank the page.

    Publishing twice leaves the first snapshot superseded but retained, and
    nothing ever removes it, so its `DOCUMENTED_BY` rows used to refuse the
    delete for the rest of the page's life.
    """
    store = pg_graph.markdown_store()
    store.write_page(
        pg_graph.domain, "architecture", _selector_page("pkg.module_0.run")
    )
    pg_graph.finalize(pg_graph.complete_session())
    pg_graph.finalize(pg_graph.complete_session())
    assert pg_graph.wiki_links()
    before_rows = pg_graph.active_rows()
    before_active = pg_graph.reader_status()["snapshot_id"]

    page = store.read_page(pg_graph.domain, "architecture")
    result = store.delete_page(
        pg_graph.domain, "architecture", page["revision"]
    )

    assert "error" not in result
    assert store.read_page(pg_graph.domain, "architecture") is None
    assert pg_graph.wiki_links() == []
    assert pg_graph.active_rows() == before_rows
    assert pg_graph.reader_status()["snapshot_id"] == before_active


def test_deleting_a_page_leaves_the_links_of_other_pages(pg_graph):
    """The cascade is scoped to the deleted page, not to the relation.

    Both pages select the one symbol the fixture graph gives a relation, so
    each holds its own row for `relation-0` and the surviving page must keep
    its row when the other page goes.
    """
    store = pg_graph.markdown_store()
    store.write_page(
        pg_graph.domain, "architecture", _selector_page("pkg.module_0.run")
    )
    store.write_page(
        pg_graph.domain, "guide", _selector_page("pkg.module_0.run")
    )
    pg_graph.finalize(pg_graph.complete_session())
    before = pg_graph.wiki_links()
    assert len(before) == 2
    before_rows = pg_graph.active_rows()

    page = store.read_page(pg_graph.domain, "architecture")
    store.delete_page(pg_graph.domain, "architecture", page["revision"])

    assert len(pg_graph.wiki_links()) == 1
    assert pg_graph.active_rows() == before_rows
    assert store.read_page(pg_graph.domain, "guide") is not None


def _snapshot_states(graph):
    return graph._query(
        "SELECT snapshot_id, state FROM iwiki.code_graph_snapshots "
        "WHERE iwiki_id = %s AND domain_id = %s ORDER BY ready_at",
        (graph.iwiki_id, graph._domain_id()),
        admin=True,
    )


def _relation_rows(graph) -> int:
    return graph._query(
        "SELECT count(*) FROM iwiki.code_graph_relations "
        "WHERE iwiki_id = %s AND domain_id = %s",
        (graph.iwiki_id, graph._domain_id()),
        admin=True,
    )[0][0]


def test_a_superseded_snapshot_is_pruned_once_it_leaves_the_window(pg_graph):
    """Nothing reads a superseded snapshot, so keeping every one is a leak."""
    pg_graph.finalize(pg_graph.complete_session())
    first = pg_graph.reader_status()["snapshot_id"]
    pg_graph.finalize(pg_graph.complete_session())
    second = pg_graph.reader_status()["snapshot_id"]
    assert {row[0] for row in _snapshot_states(pg_graph)} == {first, second}

    pg_graph.advance_clock(pg_graph.superseded_retention_seconds + 1)
    pg_graph.finalize(pg_graph.complete_session())
    third = pg_graph.reader_status()["snapshot_id"]

    # Call the cleanup entry point directly so the prune below is observed
    # after it has actually run, not raced against `begin()`'s background
    # scheduling.
    store = pg_graph.store
    store.run_cleanup_cycle()

    remaining = {row[0] for row in _snapshot_states(pg_graph)}
    assert first not in remaining
    assert third in remaining
    assert pg_graph.reader_status()["snapshot_id"] == third


def test_pruning_never_removes_the_active_snapshot(pg_graph):
    pg_graph.finalize(pg_graph.complete_session())
    active = pg_graph.reader_status()["snapshot_id"]

    pg_graph.advance_clock(pg_graph.superseded_retention_seconds * 10)
    pg_graph.store.begin(pg_graph.header)

    remaining = {row[0] for row in _snapshot_states(pg_graph)}
    assert active in remaining
    assert pg_graph.reader_status()["snapshot_id"] == active


def test_a_recent_supersession_survives_inside_the_window(pg_graph):
    pg_graph.finalize(pg_graph.complete_session())
    first = pg_graph.reader_status()["snapshot_id"]
    pg_graph.finalize(pg_graph.complete_session())

    pg_graph.store.begin(pg_graph.header)

    assert first in {row[0] for row in _snapshot_states(pg_graph)}


def _snapshot_rows(graph, snapshot_id):
    counts = {}
    for kind, table in (
        ("wiki_links", "code_graph_wiki_links"),
        ("relations", "code_graph_relations"),
        ("symbols", "code_graph_symbols"),
        ("files", "code_graph_files"),
    ):
        counts[kind] = graph._query(
            f"SELECT count(*) FROM iwiki.{table} "
            "WHERE iwiki_id = %s AND domain_id = %s AND snapshot_id = %s",
            (graph.iwiki_id, graph._domain_id(), snapshot_id),
            admin=True,
        )[0][0]
    return counts


def _store_with_factory(graph, factory):
    """A store on the fixture's wiki and clock, with a factory of our own.

    The clock matters: `advance_clock` is how the suite ages a snapshot past
    the retention window, and a store with the real clock would never see a
    candidate.
    """
    from iwiki_mcp.postgres.codegraph import PostgresCodeGraphStore

    return PostgresCodeGraphStore(
        graph.dsn,
        graph.iwiki_id,
        graph.domain,
        graph.owner_id,
        lock_timeout_ms=graph.lock_timeout_ms,
        session_ttl_seconds=graph.session_ttl_seconds,
        staging_retention_seconds=graph.staging_retention_seconds,
        staging_cleanup_limit=graph.staging_cleanup_limit,
        superseded_retention_seconds=graph.superseded_retention_seconds,
        superseded_cleanup_limit=graph.superseded_cleanup_limit,
        connection_factory=factory,
        clock=graph._now,
    )


def _aged_superseded(graph, publications: int = 3) -> str:
    """Publish repeatedly, age past the window, return the oldest snapshot."""
    for _ in range(publications):
        graph.finalize(graph.complete_session())
    graph.advance_clock(graph.superseded_retention_seconds + 1)
    return _snapshot_states(graph)[0][0]


def test_a_reactivated_snapshot_stops_its_drain_within_one_batch(
    pg_graph, monkeypatch
):
    """The retention window exists to be a revert target; the drain must not
    strip the snapshot an operator just restored."""
    monkeypatch.setattr(type(pg_graph.store), "_CLEANUP_BATCH_ROWS", 1)
    oldest = _aged_superseded(pg_graph)
    before = _snapshot_rows(pg_graph, oldest)
    assert sum(before.values()) > 4, "fixture must supply several batches"

    store = pg_graph.store
    original = store._delete_batch
    calls = {"n": 0}

    def reactivate_then_delete(cursor, table, domain_id, sid, limit):
        calls["n"] += 1
        if calls["n"] == 2:
            cursor.execute(
                "UPDATE iwiki.code_graph_domain_state "
                "SET active_snapshot_id = %s "
                "WHERE iwiki_id = %s AND domain_id = %s",
                (sid, store.iwiki_id, domain_id),
            )
        return original(cursor, table, domain_id, sid, limit)

    monkeypatch.setattr(store, "_delete_batch", reactivate_then_delete)
    store.run_cleanup_cycle()

    after = _snapshot_rows(pg_graph, oldest)
    assert oldest in {row[0] for row in _snapshot_states(pg_graph)}, (
        "the restored snapshot's row was removed"
    )
    assert sum(after.values()) > 0, "the drain continued past the reactivation"


def test_every_code_graph_child_table_reaches_code_graph_files(pg_graph):
    """The one-table guard is sufficient only while files roots the chain.

    Read the live catalogue rather than a hand-written list: the point is to
    fail when a migration adds a child table that does not depend on files,
    which is exactly the case a hand-written list would not know about.
    """
    rows = pg_graph._query(
        "SELECT c.relname, f.relname "
        "FROM pg_constraint con "
        "JOIN pg_class c ON c.oid = con.conrelid "
        "JOIN pg_class f ON f.oid = con.confrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE con.contype = 'f' AND n.nspname = 'iwiki' "
        "AND c.relname LIKE 'code\\_graph\\_%%'",
        (),
        admin=True,
    )
    parents = {}
    for child, parent in rows:
        parents.setdefault(child, set()).add(parent)

    children = {
        "code_graph_wiki_links",
        "code_graph_relations",
        "code_graph_symbols",
    }
    assert children <= set(parents), "a child table has no foreign keys at all"

    for table in children:
        reached, frontier = set(), [table]
        while frontier:
            for parent in parents.get(frontier.pop(), ()):
                if parent not in reached:
                    reached.add(parent)
                    frontier.append(parent)
        assert "code_graph_files" in reached, (
            f"{table} no longer depends on code_graph_files, so guarding the "
            "snapshot-row delete on files alone is no longer sufficient"
        )


def test_a_kill_mid_drain_keeps_the_batches_already_committed(
    pg_graph, monkeypatch
):
    """The published claim is 'at most one batch'; make it true.

    The fixture's default snapshot is small (files=2, symbols=2,
    relations=1, wiki_links=0), so the trigger counts real (non-empty)
    batches rather than a fixed call number: it lets exactly two commit,
    then kills the next call outright, regardless of which child table it
    lands on.
    """
    monkeypatch.setattr(type(pg_graph.store), "_CLEANUP_BATCH_ROWS", 2)
    oldest = _aged_superseded(pg_graph)
    before = _snapshot_rows(pg_graph, oldest)
    assert sum(before.values()) > 2, "fixture must supply several batches"

    store = pg_graph.store
    real_delete_batch = store._delete_batch
    committed = {"n": 0}

    def exploding(cursor, table, domain_id, sid, limit):
        if committed["n"] >= 2:
            raise RuntimeError("killed mid-drain")
        removed = real_delete_batch(cursor, table, domain_id, sid, limit)
        if removed:
            committed["n"] += 1
        return removed

    monkeypatch.setattr(store, "_delete_batch", exploding)

    with pytest.raises(RuntimeError):
        store.run_cleanup_cycle()

    after = _snapshot_rows(pg_graph, oldest)
    removed = sum(before.values()) - sum(after.values())
    assert removed > 0, "the committed batches were rolled back with the kill"
    assert removed <= 2 * store._CLEANUP_BATCH_ROWS, (
        "more than the two committed batches went missing"
    )


def test_a_cycle_opens_exactly_one_connection(pg_graph):
    """One connection per cycle is what makes per-batch commits affordable."""
    import psycopg

    opened = []

    def factory():
        opened.append(1)
        return psycopg.connect(pg_graph.dsn)

    _aged_superseded(pg_graph)
    store = _store_with_factory(pg_graph, factory)

    store.run_cleanup_cycle()

    assert len(opened) == 1


def test_one_connection_serves_many_transactions(pg_graph):
    """Committing per batch must not mean connecting per batch."""
    import psycopg

    opened = []

    def factory():
        opened.append(1)
        return psycopg.connect(pg_graph.dsn)

    store = _store_with_factory(pg_graph, factory)

    with store._connection() as connection:
        with store._transaction_on(connection) as cursor:
            cursor.execute("SELECT 1")
            assert cursor.fetchone()[0] == 1
        with store._transaction_on(connection) as cursor:
            cursor.execute("SELECT 2")
            assert cursor.fetchone()[0] == 2

    assert len(opened) == 1, "each transaction opened its own connection"


def test_pruning_removes_the_child_rows_of_the_snapshot_it_drops(pg_graph):
    """The prune deletes children itself instead of leaving it to the cascade.

    Cascading searches every child table once per deleted parent row, and
    those foreign keys carry no index, which is what turned one prune into a
    49-minute transaction on a live domain.
    """
    pg_graph.finalize(pg_graph.complete_session())
    first = pg_graph.reader_status()["snapshot_id"]
    assert any(_snapshot_rows(pg_graph, first).values())
    pg_graph.finalize(pg_graph.complete_session())

    pg_graph.advance_clock(pg_graph.superseded_retention_seconds + 1)
    pg_graph.store.begin(pg_graph.header)

    # Call the cleanup entry point directly so the prune below is observed
    # after it has actually run, not raced against `begin()`'s background
    # scheduling.
    store = pg_graph.store
    store.run_cleanup_cycle()

    assert _snapshot_rows(pg_graph, first) == {
        "wiki_links": 0,
        "relations": 0,
        "symbols": 0,
        "files": 0,
    }


def test_the_backlog_drains_across_successive_publications(pg_graph):
    """R6 measured as behaviour: a constant comparison passes while it stalls."""
    for _ in range(pg_graph.superseded_cleanup_limit + 3):
        pg_graph.finalize(pg_graph.complete_session())
    pg_graph.advance_clock(pg_graph.superseded_retention_seconds + 1)

    store = pg_graph.store
    before = _relation_rows(pg_graph)
    store.run_cleanup_cycle()
    after = _relation_rows(pg_graph)

    assert after < before, "the backlog did not shrink"


def test_cleanup_deletes_a_snapshot_row_only_after_every_child_is_gone(pg_graph):
    """Guarding on one table lets the unindexed cascade fire mid-snapshot."""
    for _ in range(3):
        pg_graph.finalize(pg_graph.complete_session())
    pg_graph.advance_clock(pg_graph.superseded_retention_seconds + 1)

    oldest = _snapshot_states(pg_graph)[0][0]
    rows = _snapshot_rows(pg_graph, oldest)
    assert rows["files"] > 0, "fixture must seed file rows to guard against"
    budget = rows["wiki_links"] + rows["relations"] + rows["symbols"]
    assert budget > 0, "fixture must seed non-file child rows to drain first"

    store = pg_graph.store
    with store._connection() as connection:
        with store._transaction_on(connection) as cursor:
            domain_id = store._domain_id(cursor)
        store._drain_snapshot(connection, domain_id, oldest, budget)

    after = _snapshot_rows(pg_graph, oldest)
    assert after["files"] > 0, "files drained before every other child table"
    assert oldest in {row[0] for row in _snapshot_states(pg_graph)}, (
        "a snapshot row was deleted while its files remained"
    )


def test_cleanup_never_touches_the_active_snapshot(pg_graph):
    for _ in range(3):
        pg_graph.finalize(pg_graph.complete_session())
    pg_graph.advance_clock(pg_graph.superseded_retention_seconds + 1)
    active = pg_graph.reader_status()["snapshot_id"]

    pg_graph.store.begin(pg_graph.header)

    assert pg_graph.reader_status()["snapshot_id"] == active
    assert pg_graph.reader_status()["state"] == "ready"


class _SweepSettings:
    """Minimal settings for the sweep: retention zero so seeded rows qualify."""

    publication_session_ttl_seconds = 1800
    staging_retention_seconds = 86400
    staging_cleanup_limit = 100
    superseded_retention_seconds = 0
    superseded_cleanup_limit = 2


def _sweep_binding(graph, domains):
    from psycopg.conninfo import conninfo_to_dict

    from iwiki_mcp.storage import PostgresBinding

    values = conninfo_to_dict(str(graph.dsn))
    return PostgresBinding(
        host=values.get("host", "127.0.0.1"),
        port=int(values.get("port", 5432)),
        database=values["dbname"],
        user=values["user"],
        sslmode=values.get("sslmode", "prefer"),
        password=values.get("password", ""),
        iwiki_id=graph.iwiki_id,
        read=tuple(domains),
        write=tuple(domains),
        primary=domains[0],
        project_dir="/hosted-without-checkout",
        embed_model="fixture-model",
        embed_dimensions=3,
        rerank_model="",
    )


class _SyncRuntime:
    """Runs a queued job inline, standing in for the maintenance workers."""

    def submit(self, job):
        from iwiki_mcp.codegraph import application

        application.run_cleanup_job(job, None)
        return True


def test_the_sweep_drains_a_domain_that_did_not_publish(pg_graph):
    """The regression: cleanup used to be reachable only from that domain's
    own publication, so a domain that stopped publishing kept its rows.
    """
    from iwiki_mcp.codegraph import application

    other = pg_graph.for_domain("private")
    for _ in range(3):
        other.finalize(other.complete_session())

    before = _relation_rows(other)
    assert before > 0, "fixture must seed rows in the non-publishing domain"

    binding = _sweep_binding(pg_graph, ("docs", "private"))
    queued = application.schedule_wiki_cleanup(
        binding, "owner-sweep", _SweepSettings(), runtime=_SyncRuntime()
    )

    assert queued == 2
    assert _relation_rows(other) < before, (
        "the sweep left a non-publishing domain's backlog in place"
    )


def test_the_sweep_builds_one_store_per_domain_and_mixes_none(
    pg_graph, monkeypatch
):
    """Domains must never be mixed: each queued job's store cleans only the
    one domain it was built and validated for."""
    from iwiki_mcp.codegraph import application

    built = []
    swept = []

    class _Recorder:
        def __init__(self, domain):
            self.domain = domain

        def run_cleanup_cycle(self, connection=None):
            swept.append(self.domain)
            return 0

    def fake_publisher(
        binding, owner_id, settings, *, lock_timeout_ms, domain,
        connection_factory=None,
    ):
        built.append(domain)
        return _Recorder(domain)

    monkeypatch.setattr(
        application, "create_postgres_publisher", fake_publisher
    )

    binding = _sweep_binding(pg_graph, ("docs", "private"))
    application.schedule_wiki_cleanup(
        binding, "owner-sweep", _SweepSettings(), runtime=_SyncRuntime()
    )

    assert built == ["docs", "private"]
    assert swept == ["docs", "private"]


def test_the_candidate_page_size_keeps_its_default():
    """The row budget governs volume now; this parameter only pages the
    candidate query, and changing its default is proposal-first."""
    import inspect

    from iwiki_mcp.postgres.codegraph import PostgresCodeGraphStore

    parameter = inspect.signature(
        PostgresCodeGraphStore.__init__
    ).parameters["superseded_cleanup_limit"]

    assert parameter.default == 2


def test_principal_validation_uses_the_supplied_factory(pg_graph):
    """A worker holding two connections would put the ceiling out by one per
    worker, which is the whole of the bound at two workers."""
    import psycopg

    from iwiki_mcp.postgres import store as store_module

    opened = []

    def factory():
        opened.append(1)
        return psycopg.connect(pg_graph.dsn)

    result = store_module.validate_direct_principal(
        pg_graph.dsn,
        iwiki_id=pg_graph.iwiki_id,
        connection_factory=factory,
    )

    assert result is None
    assert len(opened) == 1, "validation ignored the factory and dialled out"


def test_run_cleanup_job_runs_on_the_maintenance_pool_connection(pg_graph):
    """Close the gap nothing else exercises: `create_postgres_publisher`'s
    `connection_factory` pass-through must reach both construction (where
    `validate_direct_principal` runs under `require_database_principal=True`)
    and the drain itself, so a queued job never opens a connection outside
    the maintenance pool.

    Revert the one-line `connection_factory=connection_factory` pass-through
    in `create_postgres_publisher` and this fails: construction falls back
    to the store's own `psycopg.connect(dsn)` default, the pool below is
    touched once instead of twice, and the count assertion below catches it
    even though the cleanup cycle still completes.
    """
    from psycopg_pool import ConnectionPool

    from iwiki_mcp.codegraph import application, maintenance

    other = pg_graph.for_domain("private")
    for _ in range(3):
        other.finalize(other.complete_session())
    before = _relation_rows(other)
    assert before > 0, "fixture must seed rows to drain"

    pool = ConnectionPool(
        str(pg_graph.dsn),
        min_size=0,
        max_size=1,
        open=False,
        name="test-maintenance-pool",
    )
    pool.open(wait=True)
    used = []

    def counting_factory():
        used.append(1)
        return pool.connection()

    binding = _sweep_binding(pg_graph, ("docs", "private"))
    job = maintenance.CleanupJob(
        iwiki_id=pg_graph.iwiki_id,
        domain="private",
        binding=binding,
        owner_id="owner-maintenance",
        settings=_SweepSettings(),
        lock_timeout_ms=500,
    )
    try:
        removed = application.run_cleanup_job(job, counting_factory)
    finally:
        pool.close()

    assert removed > 0, "the cleanup cycle removed no rows"
    assert len(used) == 2, (
        "expected one pooled connection for principal validation and one "
        "for the drain; the pass-through is not reaching both"
    )
    assert _relation_rows(other) < before
