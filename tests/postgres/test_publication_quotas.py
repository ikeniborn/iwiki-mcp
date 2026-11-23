"""PostgreSQL publication capacity and terminal replay at transaction boundaries."""
from __future__ import annotations

import pytest


pytestmark = pytest.mark.postgres_integration


def test_activation_releases_receipts_only_after_success(pg_graph):
    session = pg_graph.begin()
    pg_graph.publish_batch(session, pg_graph.batches[0])
    assert pg_graph.finalize(session)["error"] == "snapshot_incomplete"
    assert pg_graph.batch_count(session) == 1
    pg_graph.upload_all(session)

    ready = pg_graph.finalize(session)
    assert ready["state"] == "ready"
    assert pg_graph.batch_count(session) == 0
    assert pg_graph.finalize(session) == ready
    assert pg_graph.active_rows() == pg_graph.expected_counts


def test_finalize_replays_owned_attempt_after_session_cleanup_until_deadline(pg_graph):
    session = pg_graph.store.begin(
        pg_graph.header, attempt_key="recover-before-deadline",
        recovery_deadline=int(pg_graph._now().timestamp()) + 600,
        max_batch_rows=1000, max_batch_bytes=1_000_000,
    )
    pg_graph.upload_all(session)
    ready = pg_graph.finalize(session)
    pg_graph.write_markdown_page("new", "# New\n\n## Body\nUpdated.\n")
    later = pg_graph.finalize(pg_graph.complete_session())
    pg_graph.advance_clock(pg_graph.superseded_retention_seconds + 1)
    pg_graph.store.run_cleanup_cycle()
    assert pg_graph.session(session) is None
    assert pg_graph.finalize(session) == ready
    assert pg_graph.with_owner("foreign-owner").finalize(session)["error"] == "unauthorized"
    assert pg_graph.reader_status()["snapshot_revision"] == later["snapshot_revision"]
    pg_graph.advance_clock(600)
    assert pg_graph.finalize(session)["error"] == "session_expired"


def _limits(graph, *, sessions=64, owner_sessions=8, staged_bytes=2**31, owner_bytes=2**28):
    graph._query(
        "UPDATE iwiki.publication_policy SET limits = %s::bigint[] RETURNING singleton",
        ([sessions, owner_sessions, staged_bytes, owner_bytes],), admin=True,
    )


def _race(first, second):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    start = threading.Barrier(2)

    def contender(call):
        start.wait(timeout=5)
        return call()

    with ThreadPoolExecutor(2) as workers:
        futures = [workers.submit(contender, call) for call in (first, second)]
        return [future.result(timeout=10) for future in futures]


def _totals(graph):
    usage = graph._query(
        "SELECT count(*) FILTER (WHERE is_staging AND expires_at > iwiki.publication_now()), "
        "coalesce(sum(charged_bytes),0) FROM iwiki.publication_usage", admin=True,
    )[0]
    receipts = graph._query(
        "SELECT count(*) FILTER (WHERE state='staging' "
        "AND least(lease_expires_at,recovery_deadline)"
        " > iwiki.publication_now()), "
        "(SELECT coalesce(sum(byte_count),0) FROM iwiki.code_graph_batches) "
        "FROM iwiki.code_graph_publication_sessions", admin=True,
    )[0]
    assert usage == receipts
    return usage


@pytest.mark.parametrize("limit", ["sessions", "owner_sessions"])
def test_concurrent_domains_contend_for_the_last_slot(pg_graph, limit):
    other = pg_graph.for_domain("private")
    _limits(pg_graph, **{limit: 1})
    options = dict(
        attempt_key="shared-recovery-key", recovery_deadline=int(pg_graph._now().timestamp()) + 600,
        max_batch_rows=1000, max_batch_bytes=1_000_000,
    )
    contenders = (pg_graph, other)
    results = _race(*(lambda g=g: g.store.begin(g.header, **options) for g in contenders))
    loser = next(i for i, result in enumerate(results) if isinstance(result, dict))
    assert results[loser] == {
        "error": "publication_quota_exceeded", "resource": "sessions",
        "hint": "retry when publication capacity is available",
    }
    winner = contenders[1 - loser]
    assert winner.store.begin(winner.header, **options) == results[1 - loser]
    assert _totals(pg_graph) == (1, 0)
    for table in ("code_graph_publication_attempts", "code_graph_snapshots"):
        assert pg_graph._query(f"SELECT count(*) FROM iwiki.{table}", admin=True) == [(1,)]
    winner.abort(results[1 - loser])
    assert contenders[loser].begin().session_id
    assert _totals(pg_graph) == (1, 0)


@pytest.mark.parametrize("limit", ["staged_bytes", "owner_bytes"])
def test_last_byte_race_and_lowered_limits_preserve_replay_and_lease(pg_graph, limit):
    other = pg_graph.for_domain("private")
    contenders = (pg_graph, other)
    sessions = [graph.begin() for graph in contenders]
    batch = pg_graph.batches[0]
    _limits(pg_graph, **{limit: batch.byte_count})
    results = _race(*(lambda i=i: contenders[i].publish_batch(sessions[i], batch) for i in (0, 1)))
    winner = results.index({"accepted": True})
    loser = 1 - winner
    assert results[loser]["error"] == "publication_quota_exceeded"
    assert results[loser]["resource"] == "staged_bytes"
    lease = contenders[loser].session(sessions[loser])["lease_expires_at"]
    assert lease == sessions[loser].lease_expires_at
    assert _totals(pg_graph) == (2, batch.byte_count)

    _limits(pg_graph, **{limit: batch.byte_count - 1})
    assert contenders[winner].publish_batch(sessions[winner], batch) == {"accepted": True}
    conflict = contenders[winner].publish_batch(sessions[winner], pg_graph.tampered(batch))
    assert conflict["error"] == "batch_conflict"
    foreign = pg_graph.with_owner("someone-else")
    assert foreign.publish_batch(sessions[0], batch)["error"] == "unauthorized"
    assert _totals(pg_graph) == (2, batch.byte_count)
    contenders[winner].abort(sessions[winner])
    assert _totals(pg_graph) == (1, 0)
    _limits(pg_graph, **{limit: batch.byte_count})
    assert contenders[loser].publish_batch(sessions[loser], batch) == {"accepted": True}


def test_expiry_returns_slot_before_payload_retention_ends(pg_graph):
    batch = pg_graph.batches[0]
    _limits(pg_graph, sessions=1, staged_bytes=batch.byte_count)
    old = pg_graph.begin()
    assert pg_graph.publish_batch(old, batch) == {"accepted": True}
    pg_graph.advance_clock(pg_graph.session_ttl_seconds + 1)
    assert _totals(pg_graph) == (0, batch.byte_count)
    new = pg_graph.begin()
    assert pg_graph.publish_batch(new, batch)["resource"] == "staged_bytes"
    assert pg_graph.publish_batch(old, batch)["error"] == "session_expired"
    pg_graph.advance_clock(pg_graph.staging_retention_seconds + 1)
    pg_graph.store.cleanup_staging(pg_graph._now())
    assert _totals(pg_graph) == (0, 0)


def test_failed_activation_and_rolled_back_commit_retain_usage(pg_graph, monkeypatch):
    session = pg_graph.complete_session()
    expected_bytes = sum(b.byte_count for b in pg_graph.batches)
    original = pg_graph.store._delete_session_batches

    def fail_after_deleting(cursor, domain_id, session_id):
        original(cursor, domain_id, session_id)
        raise RuntimeError("simulate worker loss before commit")

    with monkeypatch.context() as patch:
        patch.setattr(pg_graph.store, "_delete_session_batches", fail_after_deleting)
        with pytest.raises(RuntimeError, match="before commit"):
            pg_graph.finalize(session)
    assert _totals(pg_graph) == (1, expected_bytes)
    assert pg_graph.reader_status()["state"] == "missing"
    assert pg_graph.finalize(session)["state"] == "ready"
    assert _totals(pg_graph) == (0, 0)


def test_server_limits_span_unrelated_database_principals(pg_graph, runtime_principal):
    from psycopg.conninfo import make_conninfo

    role, password = runtime_principal(["private"], ["private"])
    other = type(pg_graph)(
        make_conninfo(pg_graph.admin_dsn, user=role, password=password),
        pg_graph.admin_dsn, pg_graph.iwiki_id, "private",
    )
    _limits(pg_graph, sessions=1, owner_sessions=1)
    first = pg_graph.begin()
    assert other.begin()["resource"] == "sessions"
    batch = pg_graph.batches[0]
    _limits(pg_graph, sessions=2, owner_sessions=1, staged_bytes=batch.byte_count)
    second = other.begin()
    assert other.publish_batch(second, batch) == {"accepted": True}
    assert pg_graph.publish_batch(first, batch)["resource"] == "staged_bytes"
    assert _totals(pg_graph) == (2, batch.byte_count)
    assert other.abort(second) == {"state": "aborted"}
    assert pg_graph.publish_batch(first, batch) == {"accepted": True}


@pytest.mark.parametrize("recoverable", [False, True])
def test_replay_cannot_revive_slot_that_expires_while_waiting(pg_graph, monkeypatch, recoverable):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    import psycopg

    batch = pg_graph.batches[0]
    _limits(pg_graph, sessions=1)
    options = dict(attempt_key="lease-replay-race",
                   recovery_deadline=int(pg_graph._now().timestamp()) + 600,
                   max_batch_rows=1000, max_batch_bytes=1_000_000) if recoverable else {}
    old = pg_graph.store.begin(pg_graph.header, **options)
    assert pg_graph.publish_batch(old, batch) == {"accepted": True}
    pending = threading.Event()
    renew = pg_graph.store._renew

    def waiting_to_renew(*args):
        pending.set()
        return renew(*args)

    monkeypatch.setattr(pg_graph.store, "_renew", waiting_to_renew)
    pg_graph.store._lock_timeout_ms = 5000
    other = pg_graph.for_domain("private")
    with ThreadPoolExecutor(2) as workers:
        with psycopg.connect(pg_graph.admin_dsn) as blocker:
            blocker.execute("SELECT pg_advisory_xact_lock(7595435311942266218::bigint)")
            replay = workers.submit(pg_graph.publish_batch, old, batch)
            assert pending.wait(3)
            pg_graph.advance_clock(pg_graph.session_ttl_seconds + 1)
            other._offset = pg_graph._offset
            begin = workers.submit(other.begin)
        assert replay.result(timeout=8)["error"] == "session_expired"
        assert begin.result(timeout=8).session_id
    assert _totals(pg_graph) == (1, batch.byte_count)


def test_busy_and_rollback_before_attempt_commit_leave_key_available(pg_graph, monkeypatch):
    import psycopg

    options = dict(attempt_key="retry-the-same-attempt",
                   recovery_deadline=int(pg_graph._now().timestamp()) + 600,
                   max_batch_rows=1000, max_batch_bytes=1_000_000)
    with psycopg.connect(pg_graph.admin_dsn) as blocker:
        blocker.execute("SELECT pg_advisory_xact_lock(7595435311942266218::bigint)")
        assert pg_graph.store.begin(pg_graph.header, **options)["error"] == "busy"
    assert _totals(pg_graph) == (0, 0)

    def disconnect(*args):
        raise RuntimeError("before acknowledgement")

    with monkeypatch.context() as patch:
        patch.setattr(pg_graph.store, "_check_commit_deadline", disconnect)
        with pytest.raises(RuntimeError, match="before acknowledgement"):
            pg_graph.store.begin(pg_graph.header, **options)
    assert _totals(pg_graph) == (0, 0)
    for table in ("code_graph_publication_attempts", "code_graph_snapshots"):
        assert pg_graph._query(f"SELECT count(*) FROM iwiki.{table}", admin=True) == [(0,)]
    assert pg_graph.store.begin(pg_graph.header, **options).session_id


def test_terminal_operations_and_sweep_release_capacity_once(pg_graph):
    session = pg_graph.complete_session()
    finalize, abort = _race(lambda: pg_graph.finalize(session), lambda: pg_graph.abort(session))
    assert finalize == abort
    assert finalize["state"] in {"ready", "aborted"}
    assert _totals(pg_graph) == (0, 0)
    assert pg_graph.finalize(session) == finalize

    staged = pg_graph.begin()
    batch = pg_graph.batches[0]
    result, aborted = _race(lambda: pg_graph.publish_batch(staged, batch),
                            lambda: pg_graph.abort(staged))
    assert result.get("accepted") or result["error"] == "snapshot_conflict"
    assert aborted == {"state": "aborted"}
    assert _totals(pg_graph) == (0, 0)

    stale = pg_graph.begin()
    assert pg_graph.publish_batch(stale, batch) == {"accepted": True}
    pg_graph.advance_clock(pg_graph.session_ttl_seconds + pg_graph.staging_retention_seconds + 1)
    result, _ = _race(lambda: pg_graph.publish_batch(stale, batch),
                      lambda: pg_graph.store.cleanup_staging(pg_graph._now()))
    assert result["error"] in {"session_expired", "unauthorized"}
    pg_graph.store.cleanup_staging(pg_graph._now())
    assert _totals(pg_graph) == (0, 0)


def test_hosted_token_budget_spans_its_domains_and_server_budget_spans_tenants(
    pg_graph, publication_pair, monkeypatch,
):
    from contextlib import ExitStack
    from types import SimpleNamespace

    from psycopg.conninfo import make_conninfo
    from psycopg_pool import ConnectionPool

    from iwiki_mcp import server
    from iwiki_mcp.postgres.auth import AuthStore
    from iwiki_mcp.postgres.config import HostedCodeGraphConfig

    auth = AuthStore(pg_graph.admin_dsn)
    auth.create_wiki("wiki-b", "wiki-b")
    auth.create_domain("wiki-b", "isolated")
    monkeypatch.setattr(server, "_HOSTED_CODE_GRAPH", HostedCodeGraphConfig())
    with ExitStack() as pools:
        stores = []
        for tenant, domains in (("wiki-a", ["docs", "private"]), ("wiki-b", ["isolated"])):
            issuer, publisher = publication_pair(tenant, domains)
            issuer_dsn = make_conninfo(pg_graph.admin_dsn, user=issuer[0], password=issuer[1])
            pub_dsn = make_conninfo(pg_graph.admin_dsn, user=publisher[0], password=publisher[1])
            pool = pools.enter_context(ConnectionPool(issuer_dsn, min_size=1, max_size=2))
            pub_pool = pools.enter_context(ConnectionPool(pub_dsn, min_size=1, max_size=2))
            bearer = auth.create_token(tenant, "publisher", read_domains=domains,
                                       write_domains=domains)["token"]
            context = AuthStore(issuer_dsn).authenticate(bearer)
            stores.append((tenant, pool, issuer_dsn, pub_pool, context.token_id))

        def begin(index, domain):
            tenant, pool, dsn, pub_pool, token_id = stores[index]
            monkeypatch.setattr(server, "_HOSTED_POOL", pool)
            monkeypatch.setattr(server, "_HOSTED_PUBLICATION_POOL", pub_pool)
            binding = SimpleNamespace(iwiki_id=tenant, primary=domain, connection_dsn=lambda: dsn)
            return server._postgres_code_store(binding, token_id).begin(pg_graph.header)

        _limits(pg_graph, sessions=2, owner_sessions=1)
        assert begin(0, "docs").session_id
        assert begin(0, "private")["resource"] == "sessions"
        assert begin(1, "isolated").session_id
        _limits(pg_graph, sessions=2, owner_sessions=2)
        assert begin(0, "private")["resource"] == "sessions"
        assert _totals(pg_graph) == (2, 0)


def test_lowering_only_slot_limit_permits_bytes_and_release(pg_graph):
    first, second = pg_graph.begin(), pg_graph.begin()
    _limits(pg_graph, sessions=1)
    assert pg_graph.begin()["resource"] == "sessions"
    batch = pg_graph.batches[0]
    assert pg_graph.publish_batch(first, batch) == {"accepted": True}
    assert pg_graph.abort(second) == {"state": "aborted"}
    assert pg_graph.begin()["resource"] == "sessions"
    assert _totals(pg_graph) == (1, batch.byte_count)
    assert pg_graph.abort(first) == {"state": "aborted"}
    assert pg_graph.begin().session_id


def test_original_expiry_precedes_byte_refusal_after_lock_wait(pg_graph, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    import psycopg

    first, second = pg_graph.begin(), pg_graph.begin()
    batch = pg_graph.batches[0]
    _limits(pg_graph, staged_bytes=batch.byte_count)
    assert pg_graph.publish_batch(first, batch) == {"accepted": True}
    guarded = threading.Event()
    original = pg_graph.store._guard

    def checked_before_expiry(record, now):
        result = original(record, now)
        assert result is None
        guarded.set()
        return result

    monkeypatch.setattr(pg_graph.store, "_guard", checked_before_expiry)
    pg_graph.store._lock_timeout_ms = 5000
    with ThreadPoolExecutor(1) as workers:
        with psycopg.connect(pg_graph.admin_dsn) as blocker:
            blocker.execute("SELECT pg_advisory_xact_lock(7595435311942266218::bigint)")
            attempt = workers.submit(pg_graph.publish_batch, second, batch)
            assert guarded.wait(3)
            pg_graph.advance_clock(pg_graph.session_ttl_seconds + 1)
        assert attempt.result(timeout=8)["error"] == "session_expired"
    assert _totals(pg_graph) == (0, batch.byte_count)


@pytest.mark.parametrize("ttl", [1800, 3600])
def test_direct_nondefault_ttl_uses_database_clock_on_begin_and_renew(pg_graph, ttl, monkeypatch):
    import datetime

    store = pg_graph.store
    monkeypatch.setattr(store, "_session_ttl_seconds", ttl)
    monkeypatch.setattr(store, "_clock", lambda: pg_graph._now() + datetime.timedelta(days=2))
    started = pg_graph._query("SELECT iwiki.publication_now()", admin=True)[0][0]
    session = store.begin(pg_graph.header)
    expiry = datetime.datetime.fromisoformat(session.lease_expires_at)
    assert expiry == datetime.datetime.fromisoformat(pg_graph.session(session)["lease_expires_at"])
    assert started + datetime.timedelta(seconds=ttl) <= expiry
    pg_graph.advance_clock(5)
    assert store.publish_batch(session, pg_graph.batches[0]) == {"accepted": True}
    renewed = datetime.datetime.fromisoformat(pg_graph.session(session)["lease_expires_at"])
    assert expiry + datetime.timedelta(seconds=5) <= renewed
    ended = pg_graph._query("SELECT iwiki.publication_now()", admin=True)[0][0]
    assert renewed <= ended + datetime.timedelta(seconds=ttl)


def test_hosted_skew_does_not_change_lease_or_recovery_deadline(hosted_empty_code, monkeypatch):
    import datetime

    from iwiki_mcp import server

    binding = hosted_empty_code.binding
    graph = hosted_empty_code.graph
    store = server._postgres_code_store(binding, server._AUTH_CONTEXT.get().token_id)
    monkeypatch.setattr(store, "_clock", lambda: graph._now() + datetime.timedelta(days=2))
    deadline = int(graph._now().timestamp()) + 600
    options = dict(attempt_key="database-clock-deadline", recovery_deadline=deadline,
                   max_batch_rows=1000, max_batch_bytes=1_000_000)
    session = store.begin(graph.header, **options)
    assert datetime.datetime.fromisoformat(session.lease_expires_at).timestamp() == deadline
    assert store.begin(graph.header, **options) == session
    assert store.inspect(session.session_id)["recovery_deadline"] == deadline
    for batch in graph.batches:
        assert store.publish_batch(session, batch) == {"accepted": True}
    assert store.finalize(session)["state"] == "ready"
