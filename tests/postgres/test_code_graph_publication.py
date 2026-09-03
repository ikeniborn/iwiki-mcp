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
