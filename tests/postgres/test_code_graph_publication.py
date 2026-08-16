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


def test_finalize_detects_markdown_generation_change(pg_graph):
    session = pg_graph.complete_session()
    pg_graph.bump_markdown_generation()

    assert pg_graph.finalize(session) == {
        "error": "markdown_unavailable",
        "hint": "reindex against the current Markdown revision",
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
