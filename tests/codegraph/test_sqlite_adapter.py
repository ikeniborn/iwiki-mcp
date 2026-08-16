"""Real SQLite adapter contract over portable code-graph row batches."""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import threading

import pytest
from filelock import Timeout

from iwiki_mcp.lock import mutation_lock
from iwiki_mcp.codegraph.canonical import (
    canonical_bytes_sha256,
    canonical_json_bytes,
)
from iwiki_mcp.codegraph.context import validate_context_request
from iwiki_mcp.codegraph.context import CodeGraphContext
from iwiki_mcp.codegraph.indexer import (
    _seal_ready_metadata,
    exact_ready_metadata,
)
from iwiki_mcp.codegraph.linking import WikiSelectorResolver
from iwiki_mcp.codegraph.publication import (
    SnapshotBatch,
    SnapshotHeader,
    graph_payload_revision,
    iter_snapshot_batches,
)
from iwiki_mcp.codegraph.query import validate_search_request
from iwiki_mcp.codegraph.schema import CodeGraphStoreError, validate_schema
from iwiki_mcp.codegraph.sqlite_adapter import (
    SqliteCodeGraphReader,
    SqliteSnapshotPublisher,
)
import iwiki_mcp.codegraph.sqlite_adapter as sqlite_adapter_module
import iwiki_mcp.codegraph.store as store_module
from iwiki_mcp.codegraph.store import CodeGraphStore


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class ObservedRLock:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._coordinator = threading.get_ident()
        self.contender_waiting = threading.Event()

    def acquire(self, *args, **kwargs):
        if threading.get_ident() != self._coordinator:
            self.contender_waiting.set()
        return self._lock.acquire(*args, **kwargs)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_args) -> None:
        self.release()


def _publisher(
    seed_runtime, built, clock, *, git_remote=None, **config_overrides
):
    indexer = seed_runtime.runtime._indexer
    config = replace(indexer.config, **config_overrides)
    publisher_kwargs = {}
    if git_remote is not None:
        publisher_kwargs["git_remote"] = git_remote
    return SqliteSnapshotPublisher(
        store=indexer.store,
        domain=seed_runtime.binding.primary,
        private_root=built.private_root,
        selector_resolver=WikiSelectorResolver(seed_runtime.binding.base),
        lock_path=indexer.paths.lock,
        config=config,
        diagnostics=built.diagnostics,
        clock=clock,
        **publisher_kwargs,
    )


def _make_sqlite_contract(seed_runtime):
    wiki_page = Path(seed_runtime.binding.base) / "project" / "overview.md"
    wiki_page.write_text(
        "---\n"
        "type: concept\n"
        "code:\n"
        "  symbols:\n"
        "    - qualified_name: pkg.service.Service.run\n"
        "---\n"
        "# Project\n",
        encoding="utf-8",
    )
    indexer = seed_runtime.runtime._indexer
    indexer.wiki_selector_resolver = WikiSelectorResolver(
        seed_runtime.binding.base
    )
    built = indexer.build_rows()
    clock = MutableClock(datetime(2026, 8, 14, tzinfo=timezone.utc))
    publisher = _publisher(
        seed_runtime,
        built,
        clock,
        max_batch_rows=1,
        max_batch_bytes=4096,
        publication_session_ttl_seconds=10,
        staging_retention_seconds=20,
        staging_cleanup_limit=1,
    )
    reader = SqliteCodeGraphReader(
        store=indexer.store,
        domain=seed_runtime.binding.primary,
        private_root=built.private_root,
        lock_path=indexer.paths.lock,
        max_file_bytes=indexer.config.max_file_bytes,
        selector_resolver=WikiSelectorResolver(seed_runtime.binding.base),
    )
    return seed_runtime, built, clock, publisher, reader


@pytest.fixture
def sqlite_contract(seed_runtime):
    return _make_sqlite_contract(seed_runtime)


@pytest.fixture
def race_sqlite_contract(seed_runtime):
    return _make_sqlite_contract(
        seed_runtime.with_config(max_rebuild_seconds=10)
    )


def _batches(built):
    return tuple(iter_snapshot_batches(
        built.tables,
        max_rows=1,
        max_bytes=4096,
    ))


def _publish_all(publisher, built):
    session = publisher.begin(built.header)
    for batch in _batches(built):
        assert publisher.publish_batch(session, batch)["accepted"] is True
    return session, publisher.finalize(session)


def test_sqlite_adapter_uses_shared_batches_and_atomic_replace(sqlite_contract):
    _runtime, built, _clock, publisher, reader = sqlite_contract
    session = publisher.begin(built.header)
    batches = _batches(built)

    for batch in batches:
        assert publisher.publish_batch(session, batch)["accepted"] is True
        assert publisher.publish_batch(session, batch)["accepted"] is True

    result = publisher.finalize(session)
    search = reader.search(validate_search_request("Service"))
    symbol_id = next(
        row["symbol_id"] for row in built.tables["symbols"]
        if row["qualified_name"] == "pkg.service.Service.run"
    )
    context = reader.context(validate_context_request([symbol_id]))

    assert result["state"] == "ready"
    assert reader.status()["snapshot_revision"] == result["snapshot_revision"]
    assert search["results"]
    assert context["nodes"]
    with closing(sqlite3.connect(_runtime.paths.database)) as connection:
        assert connection.execute(
            "SELECT page_id FROM wiki_code_links WHERE page_id = ?",
            ("project/overview",),
        ).fetchone() == ("project/overview",)
    assert publisher.finalize(session) == result


def test_sqlite_adapter_preserves_existing_link_inclusive_revision(
    sqlite_contract,
):
    runtime, _built, clock, _initial_publisher, _reader = sqlite_contract
    legacy = runtime.index(force=True)
    built = runtime.runtime._indexer.build_rows()
    publisher = _publisher(
        runtime,
        built,
        clock,
        max_batch_rows=1,
        max_batch_bytes=4096,
    )

    _session, result = _publish_all(publisher, built)

    assert result["snapshot_revision"] == legacy["revision"]


def test_begin_rejects_cross_repository_scope_without_creating_session(
    sqlite_contract
):
    runtime, built, _clock, publisher, _reader = sqlite_contract
    wrong_scope = SnapshotHeader(**{
        **built.header.__dict__,
        "repository_id": "private-cross-domain-repository",
    })

    result = publisher.begin(wrong_scope)

    assert result == {"error": "scope_mismatch"}
    assert "private-cross-domain" not in repr(result)
    assert not publisher._sessions
    assert not tuple(runtime.paths.database.parent.glob(
        f"{runtime.paths.database.name}.staging-*"
    ))


def test_begin_returns_closed_markdown_unavailable(
    sqlite_contract, monkeypatch
):
    runtime, built, _clock, publisher, _reader = sqlite_contract

    def fail_capture(**_kwargs):
        raise sqlite_adapter_module.SelectorError(
            "/private/wiki: raw OS failure"
        )

    monkeypatch.setattr(
        publisher._selector_resolver, "capture", fail_capture
    )

    result = publisher.begin(built.header)

    assert result == {"error": "markdown_unavailable"}
    assert "/private" not in repr(result)
    assert not publisher._sessions
    assert not tuple(runtime.paths.database.parent.glob(
        f"{runtime.paths.database.name}.staging-*"
    ))


def test_begin_wiki_lock_timeout_before_capture_is_sanitized_busy(
    sqlite_contract, monkeypatch
):
    runtime, built, _clock, publisher, _reader = sqlite_contract
    capture_called = False

    @contextmanager
    def fail_before_capture(*_args, **_kwargs):
        raise Timeout("/private/wiki.lock: raw OS failure")
        yield

    def observe_capture(**_kwargs):
        nonlocal capture_called
        capture_called = True
        raise AssertionError("capture must not run after lock timeout")

    monkeypatch.setattr(
        sqlite_adapter_module, "_wiki_read_lock", fail_before_capture
    )
    monkeypatch.setattr(
        publisher._selector_resolver, "capture", observe_capture
    )

    result = publisher.begin(built.header)

    assert result == {"error": "busy"}
    assert capture_called is False
    assert "/private" not in repr(result)
    assert "raw OS" not in repr(result)
    assert not publisher._sessions
    assert not tuple(runtime.paths.database.parent.glob(
        f"{runtime.paths.database.name}.staging-*"
    ))


@pytest.mark.parametrize(
    "changes",
    [
        {"protocol_version": 2},
        {"schema_version": 1},
        {"languages": ("typescript",)},
    ],
)
def test_begin_maps_malformed_header_to_closed_publication_error(
    sqlite_contract, changes
):
    _runtime, built, _clock, publisher, _reader = sqlite_contract
    malformed = SnapshotHeader(**{**built.header.__dict__, **changes})

    assert publisher.begin(malformed) == {"error": "snapshot_incomplete"}
    assert not publisher._sessions


def test_sqlite_reader_accepts_legacy_sidecar_snapshot(sqlite_contract):
    runtime, built, _clock, _publisher, reader = sqlite_contract
    legacy = runtime.index(force=True)

    status = reader.status()

    assert status["state"] == "ready"
    assert status["snapshot_revision"] == legacy["revision"]


def test_sqlite_reader_retains_guarded_local_source(sqlite_contract):
    _runtime, built, _clock, publisher, reader = sqlite_contract
    _publish_all(publisher, built)
    service = next(
        row for row in built.tables["files"]
        if row["path"] == "src/pkg/service.py"
    )

    response = reader.context(validate_context_request(
        [service["file_id"]], include_source=True, depth=0
    ))

    source_file = next(
        row for row in response["files"]
        if row["file_id"] == service["file_id"]
    )
    assert source_file["source"].startswith("class Service")


def test_sqlite_adapter_rejects_conflicting_batch_without_renewing_lease(
    sqlite_contract,
):
    _runtime, built, clock, publisher, _reader = sqlite_contract
    session = publisher.begin(built.header)
    batch = _batches(built)[0]
    assert publisher.publish_batch(session, batch)["accepted"] is True
    changed_rows = json.loads(batch.payload)
    changed_rows[0]["indexed_at"] = "2026-08-14T00:00:01Z"
    changed_payload = canonical_json_bytes(changed_rows)
    conflicting = SnapshotBatch(
        kind=batch.kind,
        ordinal=batch.ordinal,
        row_count=batch.row_count,
        byte_count=len(changed_payload),
        payload_hash=canonical_bytes_sha256(changed_payload, prefix=True),
        payload=changed_payload,
    )
    clock.advance(1)

    rejected = publisher.publish_batch(session, conflicting)
    clock.advance(9)

    assert rejected == {"error": "batch_conflict"}
    assert publisher.publish_batch(session, _batches(built)[1]) == {
        "error": "session_expired"
    }


def test_idempotent_replay_renews_lease(sqlite_contract):
    _runtime, built, clock, publisher, _reader = sqlite_contract
    session = publisher.begin(built.header)
    batches = _batches(built)
    assert publisher.publish_batch(session, batches[0])["accepted"] is True
    clock.advance(9)

    assert publisher.publish_batch(session, batches[0])["accepted"] is True
    clock.advance(2)

    assert publisher.publish_batch(session, batches[1])["accepted"] is True


def test_sqlite_session_rejects_expiry_and_replacement_owner(sqlite_contract):
    runtime, built, clock, publisher, _reader = sqlite_contract
    session = publisher.begin(built.header)
    clock.advance(11)

    assert publisher.publish_batch(session, _batches(built)[0]) == {
        "error": "session_expired"
    }

    replacement = _publisher(
        runtime,
        built,
        clock,
        max_batch_rows=1,
        max_batch_bytes=4096,
        publication_session_ttl_seconds=10,
        staging_retention_seconds=20,
        staging_cleanup_limit=1,
    )
    assert replacement.abort(session) == {"error": "unauthorized"}


def test_sqlite_abort_rejects_expired_session(sqlite_contract):
    _runtime, built, clock, publisher, _reader = sqlite_contract
    session = publisher.begin(built.header)
    clock.advance(11)

    assert publisher.abort(session) == {"error": "session_expired"}


def test_expired_session_precedes_batch_validation(sqlite_contract):
    _runtime, built, clock, publisher, _reader = sqlite_contract
    session = publisher.begin(built.header)
    batch = _batches(built)[0]
    clock.advance(11)
    invalid = SnapshotBatch(
        kind=batch.kind,
        ordinal=batch.ordinal,
        row_count=batch.row_count,
        byte_count=2,
        payload_hash="sha256:" + "0" * 64,
        payload=b"[]",
    )

    assert publisher.publish_batch(session, invalid) == {
        "error": "session_expired"
    }


def test_finalize_validates_counts_revision_and_normalized_rows(sqlite_contract):
    _runtime, built, _clock, publisher, _reader = sqlite_contract
    forged = SnapshotHeader(
        **{
            **built.header.__dict__,
            "graph_payload_revision": "sha256:" + "f" * 64,
        }
    )
    session = publisher.begin(forged)
    for batch in _batches(built):
        assert publisher.publish_batch(session, batch)["accepted"] is True

    assert publisher.finalize(session) == {"error": "revision_mismatch"}

    incomplete = publisher.begin(built.header)
    assert publisher.publish_batch(incomplete, _batches(built)[0])["accepted"]
    assert publisher.finalize(incomplete) == {"error": "snapshot_incomplete"}

    corrupt_tables = dict(built.tables)
    corrupt_file = dict(corrupt_tables["files"][0])
    corrupt_file["path_casefold"] = "not-normalized"
    corrupt_tables["files"] = (corrupt_file, *corrupt_tables["files"][1:])
    corrupt_header = SnapshotHeader(**{
        **built.header.__dict__,
        "graph_payload_revision": graph_payload_revision(corrupt_tables),
    })
    corrupt = publisher.begin(corrupt_header)
    for batch in iter_snapshot_batches(
        corrupt_tables, max_rows=1, max_bytes=4096
    ):
        assert publisher.publish_batch(corrupt, batch)["accepted"] is True
    assert publisher.finalize(corrupt) == {"error": "snapshot_incomplete"}


def test_markdown_change_conflicts_and_preserves_active_snapshot(sqlite_contract):
    runtime, built, _clock, publisher, reader = sqlite_contract
    _session, ready = _publish_all(publisher, built)
    prior_revision = ready["snapshot_revision"]
    next_session = publisher.begin(built.header)
    for batch in _batches(built):
        assert publisher.publish_batch(next_session, batch)["accepted"] is True
    Path(runtime.binding.base, "project", "overview.md").write_text(
        "# Changed\n", encoding="utf-8"
    )

    assert publisher.finalize(next_session) == {"error": "snapshot_conflict"}
    assert reader.status()["snapshot_revision"] == prior_revision


def test_finalize_publish_failure_is_terminal_and_preserves_canonical(
    sqlite_contract, monkeypatch
):
    _runtime, built, _clock, publisher, reader = sqlite_contract
    _prior_session, prior = _publish_all(publisher, built)
    session = publisher.begin(built.header)
    for batch in _batches(built):
        assert publisher.publish_batch(session, batch)["accepted"] is True

    def fail_publish(*_args, **_kwargs):
        raise CodeGraphStoreError("injected publish failure")

    monkeypatch.setattr(
        publisher._store,
        "replace_staging_logical",
        fail_publish,
    )

    result = publisher.finalize(session)

    assert result == {"error": "snapshot_incomplete"}
    assert publisher.finalize(session) == result
    assert reader.status()["snapshot_revision"] == prior["snapshot_revision"]


def test_reader_suppresses_links_when_durable_markdown_revision_is_stale(
    sqlite_contract,
):
    runtime, built, _clock, publisher, _reader = sqlite_contract
    _session, ready = _publish_all(publisher, built)
    indexer = runtime.runtime._indexer
    metadata = json.loads(runtime.paths.metadata.read_text(encoding="utf-8"))
    extra_sidecar = runtime.paths.database.with_name(
        f"{runtime.paths.database.stem}.publication.json"
    )

    assert exact_ready_metadata(metadata)
    assert metadata["markdown_revision"] == ready["markdown_revision"]
    assert not extra_sidecar.exists()

    reader = SqliteCodeGraphReader(
        store=indexer.store,
        domain=runtime.binding.primary,
        private_root=built.private_root,
        lock_path=indexer.paths.lock,
        max_file_bytes=indexer.config.max_file_bytes,
        selector_resolver=WikiSelectorResolver(runtime.binding.base),
    )
    symbol_id = next(
        row["symbol_id"] for row in built.tables["symbols"]
        if row["qualified_name"] == "pkg.service.Service.run"
    )

    current = reader.status()
    linked = reader.context(validate_context_request([symbol_id]))

    assert current["stored_markdown_revision"] == ready["markdown_revision"]
    assert current["current_markdown_revision"] == ready["markdown_revision"]
    assert current["wiki_links_stale"] is False
    assert linked["wiki_pages"]

    Path(runtime.binding.base, "project", "overview.md").write_text(
        "# Changed\n", encoding="utf-8"
    )

    stale = reader.status()
    context = reader.context(validate_context_request([symbol_id]))

    assert stale["stored_markdown_revision"] == ready["markdown_revision"]
    assert stale["current_markdown_revision"] != ready["markdown_revision"]
    assert stale["wiki_links_stale"] is True
    assert context["nodes"]
    assert context["wiki_pages"] == []
    assert "wiki_links_stale" in context["warnings"]


def test_late_metadata_failure_keeps_atomically_published_snapshot(
    sqlite_contract, monkeypatch
):
    runtime, built, clock, publisher, reader = sqlite_contract
    _session, prior = _publish_all(publisher, built)
    prior_symbol_id = next(
        row["symbol_id"] for row in built.tables["symbols"]
        if row["qualified_name"] == "pkg.service.Service.run"
    )
    Path(runtime.binding.project_dir, "src", "pkg", "service.py").write_text(
        "def replacement_symbol():\n    return None\n",
        encoding="utf-8",
    )
    replacement = runtime.runtime._indexer.build_rows()
    replacement_publisher = _publisher(runtime, replacement, clock)
    session = replacement_publisher.begin(replacement.header)
    for batch in _batches(replacement):
        assert replacement_publisher.publish_batch(session, batch)[
            "accepted"
        ] is True
    publish_metadata = replacement_publisher._store.publish_metadata
    calls = 0

    def fail_ready_metadata(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            with closing(sqlite3.connect(runtime.paths.database)) as connection:
                revision = connection.execute(
                    "SELECT revision FROM repositories"
                ).fetchone()[0]
            assert revision != prior["snapshot_revision"]
            raise CodeGraphStoreError("injected late metadata failure")
        return publish_metadata(*args, **kwargs)

    monkeypatch.setattr(
        replacement_publisher._store,
        "publish_metadata",
        fail_ready_metadata,
    )

    result = replacement_publisher.finalize(session)
    status = reader.status()

    assert result["state"] == "ready"
    assert replacement_publisher.finalize(session) == result
    assert status["state"] == "ready"
    assert status["snapshot_revision"] == result["snapshot_revision"]
    assert status["snapshot_revision"] != prior["snapshot_revision"]
    assert all(
        item["entity_id"] != prior_symbol_id
        for item in reader.search(validate_search_request("Service"))["results"]
    )
    assert reader.search(validate_search_request("replacement_symbol"))[
        "results"
    ]


def test_directory_sync_failure_reconciles_by_active_session(
    sqlite_contract, monkeypatch
):
    _runtime, built, _clock, publisher, reader = sqlite_contract
    session = publisher.begin(built.header)
    for batch in _batches(built):
        assert publisher.publish_batch(session, batch)["accepted"] is True
    calls = 0

    def fail_directory_sync():
        nonlocal calls
        calls += 1
        raise CodeGraphStoreError("/private/cache: raw OS failure")

    monkeypatch.setattr(
        publisher,
        "_sync_canonical_directory",
        fail_directory_sync,
        raising=False,
    )

    first = publisher.finalize(session)

    assert first["error"] == "commit_uncertain"
    assert set(first) == {"error", "snapshot_revision"}
    assert "/private" not in repr(first)
    assert reader.status()["snapshot_revision"] == first["snapshot_revision"]
    assert publisher.publish_batch(session, _batches(built)[0]) == {
        "error": "session_expired"
    }
    assert publisher.abort(session) == {"error": "session_expired"}

    repeated = publisher.finalize(session)
    assert repeated == first
    assert calls == 2

    monkeypatch.setattr(
        publisher,
        "_sync_canonical_directory",
        lambda: None,
    )
    second = publisher.finalize(session)

    assert calls == 2
    assert second["state"] == "ready"
    assert second["snapshot_revision"] == first["snapshot_revision"]


def test_uncertainty_blocks_batch_and_abort_when_journal_transition_fails(
    sqlite_contract, monkeypatch
):
    _runtime, built, _clock, publisher, _reader = sqlite_contract
    session = publisher.begin(built.header)
    batches = _batches(built)
    for batch in batches:
        assert publisher.publish_batch(session, batch)["accepted"] is True

    def fail_directory_sync():
        raise CodeGraphStoreError("/private/cache: raw OS failure")

    original_set_state = publisher._set_state

    def fail_uncertain_transition(record, state, now):
        if state == "commit_uncertain":
            raise CodeGraphStoreError("/private/journal: raw OS failure")
        return original_set_state(record, state, now)

    monkeypatch.setattr(
        publisher, "_sync_canonical_directory", fail_directory_sync
    )
    monkeypatch.setattr(publisher, "_set_state", fail_uncertain_transition)

    uncertain = publisher.finalize(session)

    assert uncertain["error"] == "commit_uncertain"
    assert set(uncertain) == {"error", "snapshot_revision"}
    journal = publisher._sessions[session.session_id].staging
    assert tuple(sorted(
        _runtime.paths.database.parent.glob(
            f"{_runtime.paths.database.name}.staging-*"
        )
    )) == (journal.parent,)

    def fail_journal_open(_record):
        raise CodeGraphStoreError("/private/journal: persistent OS failure")

    monkeypatch.setattr(publisher, "_connect", fail_journal_open)
    assert publisher.publish_batch(session, batches[0]) == {
        "error": "session_expired"
    }
    assert publisher.abort(session) == {"error": "session_expired"}
    assert publisher.finalize(session) == uncertain
    assert "/private" not in repr(uncertain)


def test_concurrent_uncertain_finalize_reconciles_once(
    sqlite_contract, monkeypatch
):
    _runtime, built, _clock, publisher, _reader = sqlite_contract
    session = publisher.begin(built.header)
    for batch in _batches(built):
        assert publisher.publish_batch(session, batch)["accepted"] is True

    monkeypatch.setattr(
        publisher,
        "_sync_canonical_directory",
        lambda: (_ for _ in ()).throw(CodeGraphStoreError("sync failed")),
    )
    uncertain = publisher.finalize(session)
    assert uncertain["error"] == "commit_uncertain"

    sync_barrier = threading.Barrier(2)
    sync_calls = 0
    sync_lock = threading.Lock()

    def coordinated_sync():
        nonlocal sync_calls
        with sync_lock:
            sync_calls += 1
        try:
            sync_barrier.wait(timeout=0.25)
        except threading.BrokenBarrierError:
            pass

    monkeypatch.setattr(
        publisher, "_sync_canonical_directory", coordinated_sync
    )
    start = threading.Barrier(3)
    results = []
    failures = []

    def finalize_from_thread():
        try:
            start.wait(timeout=2)
            results.append(publisher.finalize(session))
        except BaseException as exc:  # pragma: no cover - assertion evidence
            failures.append(exc)

    threads = [
        threading.Thread(target=finalize_from_thread),
        threading.Thread(target=finalize_from_thread),
    ]
    for thread in threads:
        thread.start()
    start.wait(timeout=2)
    for thread in threads:
        thread.join(timeout=2)

    assert not failures
    assert all(not thread.is_alive() for thread in threads)
    assert sync_calls == 1
    assert len(results) == 2
    assert results[0] == results[1]
    assert results[0]["state"] == "ready"
    assert results[0]["snapshot_revision"] == uncertain["snapshot_revision"]


def test_ready_finalize_wins_race_with_paused_batch(race_sqlite_contract):
    _runtime, built, _clock, publisher, _reader = race_sqlite_contract
    session = publisher.begin(built.header)
    batches = _batches(built)
    for batch in batches:
        assert publisher.publish_batch(session, batch)["accepted"] is True
    mutex = ObservedRLock()
    publisher._mutex = mutex
    results = []
    failures = []

    def replay_batch():
        try:
            results.append(publisher.publish_batch(session, batches[0]))
        except BaseException as exc:  # pragma: no cover - assertion evidence
            failures.append(exc)

    with mutex:
        contender = threading.Thread(target=replay_batch)
        contender.start()
        assert mutex.contender_waiting.wait(2)
        ready = publisher.finalize(session)
    contender.join(timeout=2)

    assert not contender.is_alive()
    assert not failures
    assert results == [ready]
    assert ready["state"] == "ready"


def test_ready_finalize_wins_race_with_paused_abort(race_sqlite_contract):
    _runtime, built, _clock, publisher, _reader = race_sqlite_contract
    session = publisher.begin(built.header)
    for batch in _batches(built):
        assert publisher.publish_batch(session, batch)["accepted"] is True
    mutex = ObservedRLock()
    publisher._mutex = mutex
    results = []
    failures = []

    def abort_session():
        try:
            results.append(publisher.abort(session))
        except BaseException as exc:  # pragma: no cover - assertion evidence
            failures.append(exc)

    with mutex:
        contender = threading.Thread(target=abort_session)
        contender.start()
        assert mutex.contender_waiting.wait(2)
        ready = publisher.finalize(session)
    contender.join(timeout=2)

    assert not contender.is_alive()
    assert not failures
    assert results == [ready]
    assert ready["state"] == "ready"


def test_uncertain_finalize_rejects_different_active_session(
    sqlite_contract, monkeypatch
):
    runtime, built, clock, publisher, _reader = sqlite_contract
    session = publisher.begin(built.header)
    for batch in _batches(built):
        assert publisher.publish_batch(session, batch)["accepted"] is True

    def fail_directory_sync():
        raise CodeGraphStoreError("sync failed")

    monkeypatch.setattr(
        publisher,
        "_sync_canonical_directory",
        fail_directory_sync,
    )
    uncertain = publisher.finalize(session)
    Path(runtime.binding.project_dir, "src", "pkg", "service.py").write_text(
        "def replacement_symbol():\n    return None\n",
        encoding="utf-8",
    )
    replacement_rows = runtime.runtime._indexer.build_rows()
    replacement = _publisher(runtime, replacement_rows, clock)
    _replacement_session, ready = _publish_all(
        replacement,
        replacement_rows,
    )

    assert ready["snapshot_revision"] != uncertain["snapshot_revision"]
    assert publisher.finalize(session) == {"error": "snapshot_conflict"}


def test_post_replace_external_journal_failure_uses_embedded_terminal_result(
    sqlite_contract, monkeypatch
):
    _runtime, built, _clock, publisher, reader = sqlite_contract
    session = publisher.begin(built.header)
    for batch in _batches(built):
        assert publisher.publish_batch(session, batch)["accepted"] is True
    calls = 0

    def fail_external_terminal(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise CodeGraphStoreError("external journal failed")

    monkeypatch.setattr(
        publisher,
        "_record_external_terminal",
        fail_external_terminal,
        raising=False,
    )

    result = publisher.finalize(session)

    assert calls == 1
    assert result["state"] == "ready"
    assert reader.status()["snapshot_revision"] == result[
        "snapshot_revision"
    ]


def test_reader_rejects_root_path_changed_after_embedded_seal(sqlite_contract):
    runtime, built, _clock, publisher, reader = sqlite_contract
    _publish_all(publisher, built)
    symbol_id = next(
        row["symbol_id"] for row in built.tables["symbols"]
        if row["qualified_name"] == "pkg.service.Service.run"
    )
    with closing(sqlite3.connect(runtime.paths.database)) as connection:
        connection.execute(
            "UPDATE repositories SET root_path = root_path || '.changed'"
        )
        connection.commit()
    metadata = json.loads(runtime.paths.metadata.read_text(encoding="utf-8"))
    metadata["storage_stamp"] = publisher._store.storage_stamp()
    _seal_ready_metadata(metadata)
    runtime.paths.metadata.write_text(
        json.dumps(metadata, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    assert reader.status()["state"] == "missing"
    assert reader.search(validate_search_request("Service"))["results"] == []
    assert reader.context(validate_context_request([symbol_id]))["nodes"] == []


def test_publication_profile_preserves_git_remote_and_raw_copy(
    sqlite_contract, tmp_path
):
    runtime, built, clock, _publisher_instance, _reader = sqlite_contract
    publisher = _publisher(
        runtime,
        built,
        clock,
        git_remote="iwiki-publication:legitimate",
    )
    _session, result = _publish_all(publisher, built)
    copied_path = tmp_path / "external-copy.sqlite3"
    shutil.copy2(runtime.paths.database, copied_path)
    copied_store = CodeGraphStore(copied_path)
    indexer = runtime.runtime._indexer
    copied_reader = SqliteCodeGraphReader(
        store=copied_store,
        domain=runtime.binding.primary,
        private_root=built.private_root,
        lock_path=indexer.paths.lock,
        max_file_bytes=indexer.config.max_file_bytes,
        selector_resolver=WikiSelectorResolver(runtime.binding.base),
    )

    with closing(sqlite3.connect(copied_path)) as connection:
        assert connection.execute(
            "SELECT git_remote FROM repositories"
        ).fetchone() == ("iwiki-publication:legitimate",)
        validate_schema(connection)
    assert copied_store.stable_rows("repositories")[0]["git_remote"] == (
        "iwiki-publication:legitimate"
    )
    assert copied_reader.status()["snapshot_revision"] == result[
        "snapshot_revision"
    ]


def test_reader_rejects_corrupt_embedded_publication_envelope(sqlite_contract):
    runtime, built, _clock, publisher, reader = sqlite_contract
    _publish_all(publisher, built)
    with closing(sqlite3.connect(runtime.paths.database)) as connection:
        connection.execute(
            "UPDATE code_graph_publication SET envelope_digest = ?",
            ("sha256:" + "0" * 64,),
        )
        connection.commit()

    assert reader.status()["error"] == "missing_snapshot"


def test_complete_prior_raw_copy_remains_a_valid_snapshot(
    sqlite_contract, tmp_path
):
    runtime, built, clock, publisher, reader = sqlite_contract
    _session, prior = _publish_all(publisher, built)
    replay = tmp_path / "prior-snapshot.sqlite3"
    shutil.copy2(runtime.paths.database, replay)
    Path(runtime.binding.project_dir, "src", "pkg", "service.py").write_text(
        "def replacement_symbol():\n    return None\n",
        encoding="utf-8",
    )
    replacement = runtime.runtime._indexer.build_rows()
    replacement_publisher = _publisher(runtime, replacement, clock)
    _session, current = _publish_all(replacement_publisher, replacement)
    assert current["snapshot_revision"] != prior["snapshot_revision"]

    os.replace(replay, runtime.paths.database)

    assert reader.status()["snapshot_revision"] == prior["snapshot_revision"]
    assert reader.search(validate_search_request("Service"))["results"]


def test_finalize_prepares_complete_prior_backup_outside_graph_lock(
    sqlite_contract, tmp_path, monkeypatch
):
    runtime, built, clock, publisher, _reader = sqlite_contract
    _prior_session, prior = _publish_all(publisher, built)
    Path(runtime.binding.project_dir, "src", "pkg", "service.py").write_text(
        "def replacement_symbol():\n    return None\n",
        encoding="utf-8",
    )
    replacement = runtime.runtime._indexer.build_rows()
    replacement_publisher = _publisher(
        runtime,
        replacement,
        clock,
        max_batch_rows=1,
        max_batch_bytes=4096,
        publication_session_ttl_seconds=10,
        staging_retention_seconds=20,
        staging_cleanup_limit=1,
    )
    graph_write_held = False
    graph_write_lock = sqlite_adapter_module.code_graph_write_lock
    prepare_staging = CodeGraphStore.prepare_staging
    backup_copy = tmp_path / "prior-backup.sqlite3"
    backup_lock_states = []

    @contextmanager
    def tracked_graph_write_lock(*args, **kwargs):
        nonlocal graph_write_held
        with graph_write_lock(*args, **kwargs):
            graph_write_held = True
            try:
                yield
            finally:
                graph_write_held = False

    def capture_prepared_backup(store, staging, **kwargs):
        result = prepare_staging(store, staging, **kwargs)
        if kwargs["expected_revision"] == prior["snapshot_revision"]:
            backup_lock_states.append(graph_write_held)
            shutil.copy2(staging, backup_copy)
        return result

    monkeypatch.setattr(
        sqlite_adapter_module,
        "code_graph_write_lock",
        tracked_graph_write_lock,
    )
    monkeypatch.setattr(
        CodeGraphStore, "prepare_staging", capture_prepared_backup
    )

    _session, ready = _publish_all(replacement_publisher, replacement)

    assert ready["state"] == "ready"
    assert ready["snapshot_revision"] != prior["snapshot_revision"]
    assert backup_lock_states == [False]
    backup_reader = SqliteCodeGraphReader(
        store=CodeGraphStore(backup_copy),
        domain=runtime.binding.primary,
        private_root=replacement.private_root,
        lock_path=tmp_path / "prior-backup.lock",
        max_file_bytes=runtime.runtime._indexer.config.max_file_bytes,
        selector_resolver=WikiSelectorResolver(runtime.binding.base),
    )
    assert backup_reader.status()["snapshot_revision"] == prior[
        "snapshot_revision"
    ]
    assert not tuple(runtime.paths.database.parent.glob(
        f"{runtime.paths.database.name}.staging-*"
    ))


def test_reader_rejects_incomplete_raw_database_copy(sqlite_contract, tmp_path):
    runtime, built, _clock, publisher, _reader = sqlite_contract
    _publish_all(publisher, built)
    copied = tmp_path / "incomplete.sqlite3"
    payload = runtime.paths.database.read_bytes()
    copied.write_bytes(payload[: len(payload) // 2])
    indexer = runtime.runtime._indexer
    reader = SqliteCodeGraphReader(
        store=CodeGraphStore(copied),
        domain=runtime.binding.primary,
        private_root=built.private_root,
        lock_path=indexer.paths.lock,
        max_file_bytes=indexer.config.max_file_bytes,
        selector_resolver=WikiSelectorResolver(runtime.binding.base),
    )

    assert reader.status() == {
        "state": "missing",
        "fresh": False,
        "error": "missing_snapshot",
    }


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("domain", "wrong-domain"),
        ("repository_id", "wrong-repository"),
        ("snapshot_revision", "sha256:" + "0" * 64),
    ],
)
def test_reader_rejects_wrong_embedded_identity_or_revision(
    sqlite_contract, column, value
):
    runtime, built, _clock, publisher, reader = sqlite_contract
    _publish_all(publisher, built)
    with closing(sqlite3.connect(runtime.paths.database)) as connection:
        connection.execute(
            f"UPDATE code_graph_publication SET {column} = ?",
            (value,),
        )
        connection.commit()

    assert reader.status()["error"] == "missing_snapshot"


def test_publication_reader_validation_cache_is_storage_stamp_bounded(
    sqlite_contract, monkeypatch
):
    runtime, built, _clock, publisher, reader = sqlite_contract
    _publish_all(publisher, built)
    read_envelope = sqlite_adapter_module._read_publication_envelope
    calls = 0

    def counted_read(connection):
        nonlocal calls
        calls += 1
        return read_envelope(connection)

    monkeypatch.setattr(
        sqlite_adapter_module,
        "_read_publication_envelope",
        counted_read,
    )

    assert reader.status()["state"] == "ready"
    assert reader.status()["state"] == "ready"
    assert calls == 1

    restarted = SqliteCodeGraphReader(
        store=runtime.runtime._indexer.store,
        domain=runtime.binding.primary,
        private_root=built.private_root,
        lock_path=runtime.runtime._indexer.paths.lock,
        max_file_bytes=runtime.runtime._indexer.config.max_file_bytes,
        selector_resolver=WikiSelectorResolver(runtime.binding.base),
    )
    assert restarted.status()["state"] == "ready"
    assert calls == 2

    with closing(sqlite3.connect(runtime.paths.database)) as connection:
        connection.execute(
            "UPDATE repositories SET root_path = root_path || '.changed'"
        )
        connection.commit()

    assert reader.status()["error"] == "missing_snapshot"
    assert calls == 3


def test_markdown_change_during_link_derivation_conflicts(
    sqlite_contract, monkeypatch
):
    runtime, built, clock, publisher, reader = sqlite_contract
    _session, prior = _publish_all(publisher, built)
    replacement = _publisher(runtime, built, clock)
    session = replacement.begin(built.header)
    for batch in _batches(built):
        assert replacement.publish_batch(session, batch)["accepted"] is True
    resolve_snapshot = replacement._selector_resolver.resolve_snapshot

    def resolve_then_mutate(*args, **kwargs):
        links = resolve_snapshot(*args, **kwargs)
        Path(runtime.binding.base, "project", "overview.md").write_text(
            "# Changed during link derivation\n",
            encoding="utf-8",
        )
        return links

    monkeypatch.setattr(
        replacement._selector_resolver,
        "resolve_snapshot",
        resolve_then_mutate,
    )

    result = replacement.finalize(session)

    assert result == {"error": "snapshot_conflict"}
    assert replacement.finalize(session) == result
    assert reader.status()["snapshot_revision"] == prior["snapshot_revision"]


def test_markdown_writer_cannot_interleave_final_check_and_replace(
    sqlite_contract, monkeypatch
):
    runtime, built, _clock, publisher, _reader = sqlite_contract
    session = publisher.begin(built.header)
    for batch in _batches(built):
        assert publisher.publish_batch(session, batch)["accepted"] is True
    replace_staging = publisher._store.replace_staging_logical
    attempt = threading.Event()
    finished = threading.Event()
    outcome = {}
    wiki_page = Path(runtime.binding.base, "project", "overview.md")

    def mutate_markdown():
        attempt.wait()
        try:
            with mutation_lock(runtime.binding.base, timeout=0):
                wiki_page.write_text(
                    "# Concurrent mutation\n",
                    encoding="utf-8",
                )
                outcome["mutated"] = True
        except Timeout:
            outcome["blocked"] = True
        finally:
            finished.set()

    writer = threading.Thread(target=mutate_markdown)
    writer.start()

    def replace_after_final_check(staging):
        attempt.set()
        assert finished.wait(2)
        return replace_staging(staging)

    monkeypatch.setattr(
        publisher._store,
        "replace_staging_logical",
        replace_after_final_check,
    )
    result = publisher.finalize(session)
    writer.join(timeout=2)

    assert result["state"] == "ready"
    assert outcome == {"blocked": True}
    assert "Concurrent mutation" not in wiki_page.read_text(encoding="utf-8")


def test_persistent_metadata_failure_is_recoverable_after_restart(
    sqlite_contract, monkeypatch
):
    runtime, built, clock, publisher, _reader = sqlite_contract
    _session, prior = _publish_all(publisher, built)
    prior_symbol_id = next(
        row["symbol_id"] for row in built.tables["symbols"]
        if row["qualified_name"] == "pkg.service.Service.run"
    )
    Path(runtime.binding.project_dir, "src", "pkg", "service.py").write_text(
        "def replacement_symbol():\n    return None\n",
        encoding="utf-8",
    )
    replacement = runtime.runtime._indexer.build_rows()
    replacement_publisher = _publisher(runtime, replacement, clock)
    session = replacement_publisher.begin(replacement.header)
    for batch in _batches(replacement):
        assert replacement_publisher.publish_batch(session, batch)[
            "accepted"
        ] is True

    def fail_metadata(*_args, **_kwargs):
        raise CodeGraphStoreError("persistent metadata failure")

    monkeypatch.setattr(
        replacement_publisher._store,
        "publish_metadata",
        fail_metadata,
    )

    result = replacement_publisher.finalize(session)
    indexer = runtime.runtime._indexer
    restarted = SqliteCodeGraphReader(
        store=indexer.store,
        domain=runtime.binding.primary,
        private_root=built.private_root,
        lock_path=indexer.paths.lock,
        max_file_bytes=indexer.config.max_file_bytes,
        selector_resolver=WikiSelectorResolver(runtime.binding.base),
    )

    assert result["state"] == "ready"
    assert result["snapshot_revision"] != prior["snapshot_revision"]
    assert restarted.status()["snapshot_revision"] == result[
        "snapshot_revision"
    ]
    assert all(
        item["entity_id"] != prior_symbol_id
        for item in restarted.search(
            validate_search_request("Service")
        )["results"]
    )
    assert restarted.search(validate_search_request("replacement_symbol"))[
        "results"
    ]


@pytest.mark.parametrize("sidecar_state", ["missing", "corrupt"])
def test_sidecar_is_only_a_cache_for_embedded_ready_metadata(
    sqlite_contract, sidecar_state
):
    runtime, built, _clock, publisher, reader = sqlite_contract
    _session, ready = _publish_all(publisher, built)
    if sidecar_state == "missing":
        runtime.paths.metadata.unlink()
    else:
        runtime.paths.metadata.write_text("{corrupt", encoding="utf-8")

    status = reader.status()

    assert status["state"] == "ready"
    assert status["snapshot_revision"] == ready["snapshot_revision"]


def test_reader_during_sidecar_pause_observes_new_embedded_ready(
    sqlite_contract, monkeypatch
):
    runtime, built, clock, publisher, reader = sqlite_contract
    _session, prior = _publish_all(publisher, built)
    Path(runtime.binding.project_dir, "src", "pkg", "service.py").write_text(
        "def replacement_symbol():\n    return None\n",
        encoding="utf-8",
    )
    replacement = runtime.runtime._indexer.build_rows()
    replacement_publisher = _publisher(runtime, replacement, clock)
    session = replacement_publisher.begin(replacement.header)
    for batch in _batches(replacement):
        assert replacement_publisher.publish_batch(session, batch)[
            "accepted"
        ] is True
    publish_metadata = replacement_publisher._store.publish_metadata
    paused = threading.Event()
    resume = threading.Event()
    outcome = {}

    def pause_sidecar(*args, **kwargs):
        paused.set()
        assert resume.wait(2)
        return publish_metadata(*args, **kwargs)

    def finalize():
        try:
            outcome["result"] = replacement_publisher.finalize(session)
        except BaseException as exc:  # pragma: no cover - assertion evidence
            outcome["error"] = exc

    monkeypatch.setattr(
        replacement_publisher._store,
        "publish_metadata",
        pause_sidecar,
    )
    worker = threading.Thread(target=finalize)
    worker.start()
    assert paused.wait(2)
    try:
        observed = reader.status()
    finally:
        resume.set()
        worker.join(timeout=2)

    assert not worker.is_alive()
    assert "error" not in outcome
    assert observed["state"] == "ready"
    assert observed["snapshot_revision"] != prior["snapshot_revision"]
    assert outcome["result"]["snapshot_revision"] == observed[
        "snapshot_revision"
    ]


def test_crash_after_database_replace_restarts_from_embedded_ready(
    sqlite_contract, monkeypatch
):
    runtime, built, clock, publisher, _reader = sqlite_contract
    _publish_all(publisher, built)
    Path(runtime.binding.project_dir, "src", "pkg", "service.py").write_text(
        "def replacement_symbol():\n    return None\n",
        encoding="utf-8",
    )
    replacement = runtime.runtime._indexer.build_rows()
    replacement_publisher = _publisher(runtime, replacement, clock)
    session = replacement_publisher.begin(replacement.header)
    for batch in _batches(replacement):
        assert replacement_publisher.publish_batch(session, batch)[
            "accepted"
        ] is True

    class CrashAfterReplace(BaseException):
        pass

    def crash_before_sidecar(*_args, **_kwargs):
        raise CrashAfterReplace

    monkeypatch.setattr(
        replacement_publisher._store,
        "publish_metadata",
        crash_before_sidecar,
    )

    with pytest.raises(CrashAfterReplace):
        replacement_publisher.finalize(session)

    indexer = runtime.runtime._indexer
    restarted = SqliteCodeGraphReader(
        store=indexer.store,
        domain=runtime.binding.primary,
        private_root=replacement.private_root,
        lock_path=indexer.paths.lock,
        max_file_bytes=indexer.config.max_file_bytes,
        selector_resolver=WikiSelectorResolver(runtime.binding.base),
    )
    status = restarted.status()

    assert status["state"] == "ready"
    assert (
        status["graph_payload_revision"]
        == replacement.header.graph_payload_revision
    )
    assert restarted.search(validate_search_request("replacement_symbol"))[
        "results"
    ]


def test_reader_holds_wiki_lock_through_context_materialization(
    sqlite_contract, monkeypatch
):
    runtime, built, _clock, publisher, reader = sqlite_contract
    _publish_all(publisher, built)
    symbol_id = next(
        row["symbol_id"] for row in built.tables["symbols"]
        if row["qualified_name"] == "pkg.service.Service.run"
    )
    checked = threading.Event()
    finished = threading.Event()
    outcome = {}
    current_revision = reader._current_markdown_revision
    context = CodeGraphContext.context

    def signal_after_check():
        revision = current_revision()
        checked.set()
        return revision

    def mutate_markdown():
        checked.wait()
        try:
            with mutation_lock(runtime.binding.base, timeout=0):
                Path(runtime.binding.base, "project", "overview.md").write_text(
                    "# Concurrent reader mutation\n",
                    encoding="utf-8",
                )
                outcome["mutated"] = True
        except Timeout:
            outcome["blocked"] = True
        finally:
            finished.set()

    def context_after_writer(*args, **kwargs):
        assert finished.wait(2)
        return context(*args, **kwargs)

    monkeypatch.setattr(reader, "_current_markdown_revision", signal_after_check)
    monkeypatch.setattr(CodeGraphContext, "context", context_after_writer)
    writer = threading.Thread(target=mutate_markdown)
    writer.start()
    response = reader.context(validate_context_request([symbol_id]))
    writer.join(timeout=2)

    assert outcome == {"blocked": True}
    assert response["wiki_pages"]


def test_finalize_heavy_work_precedes_graph_write_lock(
    sqlite_contract, monkeypatch
):
    _runtime, built, _clock, publisher, _reader = sqlite_contract
    session = publisher.begin(built.header)
    for batch in _batches(built):
        assert publisher.publish_batch(session, batch)["accepted"] is True
    graph_write_held = False
    wiki_read_held = False
    observed = []
    graph_write_lock = sqlite_adapter_module.code_graph_write_lock
    wiki_read_lock = sqlite_adapter_module._wiki_read_lock
    read_batches = publisher._read_batches
    capture = publisher._selector_resolver.capture
    resolve_snapshot = publisher._selector_resolver.resolve_snapshot
    insert_snapshot = CodeGraphStore.insert_snapshot
    prepare_staging = CodeGraphStore.prepare_staging
    graph_revision = sqlite_adapter_module.graph_payload_revision
    snapshot_revision = sqlite_adapter_module._snapshot_revision
    content_digest = store_module._publication_content_digest
    sync_directory = publisher._sync_canonical_directory
    record_terminal = publisher._record_external_terminal

    @contextmanager
    def tracked_wiki_read_lock(*args, **kwargs):
        nonlocal wiki_read_held
        with wiki_read_lock(*args, **kwargs):
            wiki_read_held = True
            try:
                yield
            finally:
                wiki_read_held = False

    @contextmanager
    def tracked_graph_write_lock(*args, **kwargs):
        nonlocal graph_write_held
        with graph_write_lock(*args, **kwargs):
            assert wiki_read_held is True
            graph_write_held = True
            try:
                yield
            finally:
                graph_write_held = False

    def observed_read_batches(connection):
        observed.append(("batches", graph_write_held))
        return read_batches(connection)

    def observed_capture(*args, **kwargs):
        observed.append(("markdown", graph_write_held))
        return capture(*args, **kwargs)

    def observed_resolve_snapshot(*args, **kwargs):
        observed.append(("links", graph_write_held))
        return resolve_snapshot(*args, **kwargs)

    def observed_insert_snapshot(store, snapshot):
        observed.append(("build", graph_write_held))
        return insert_snapshot(store, snapshot)

    def observed_prepare_staging(store, *args, **kwargs):
        observed.append(("prepare", graph_write_held))
        return prepare_staging(store, *args, **kwargs)

    def observed_graph_revision(*args, **kwargs):
        observed.append(("graph_revision", graph_write_held))
        return graph_revision(*args, **kwargs)

    def observed_snapshot_revision(*args, **kwargs):
        observed.append(("snapshot_revision", graph_write_held))
        return snapshot_revision(*args, **kwargs)

    def observed_content_digest(*args, **kwargs):
        observed.append(("content_digest", graph_write_held))
        return content_digest(*args, **kwargs)

    def observed_sync_directory():
        observed.append(("directory_sync", graph_write_held))
        return sync_directory()

    def observed_record_terminal(*args, **kwargs):
        observed.append(("external_terminal", graph_write_held))
        return record_terminal(*args, **kwargs)

    monkeypatch.setattr(
        sqlite_adapter_module,
        "code_graph_write_lock",
        tracked_graph_write_lock,
    )
    monkeypatch.setattr(
        sqlite_adapter_module,
        "_wiki_read_lock",
        tracked_wiki_read_lock,
    )
    monkeypatch.setattr(publisher, "_read_batches", observed_read_batches)
    monkeypatch.setattr(
        publisher._selector_resolver,
        "capture",
        observed_capture,
    )
    monkeypatch.setattr(
        publisher._selector_resolver,
        "resolve_snapshot",
        observed_resolve_snapshot,
    )
    monkeypatch.setattr(
        CodeGraphStore,
        "insert_snapshot",
        observed_insert_snapshot,
    )
    monkeypatch.setattr(
        CodeGraphStore,
        "prepare_staging",
        observed_prepare_staging,
    )
    monkeypatch.setattr(
        sqlite_adapter_module,
        "graph_payload_revision",
        observed_graph_revision,
    )
    monkeypatch.setattr(
        sqlite_adapter_module,
        "_snapshot_revision",
        observed_snapshot_revision,
    )
    monkeypatch.setattr(
        store_module,
        "_publication_content_digest",
        observed_content_digest,
    )
    monkeypatch.setattr(
        publisher,
        "_sync_canonical_directory",
        observed_sync_directory,
    )
    monkeypatch.setattr(
        publisher,
        "_record_external_terminal",
        observed_record_terminal,
    )
    assert publisher.finalize(session)["state"] == "ready"
    assert {name for name, _held in observed} >= {
        "batches",
        "markdown",
        "links",
        "build",
        "prepare",
        "graph_revision",
        "snapshot_revision",
        "content_digest",
        "directory_sync",
        "external_terminal",
    }
    assert all(
        not held
        for name, held in observed
        if name not in {"directory_sync"}
    )
    assert ("directory_sync", True) in observed
    assert ("external_terminal", False) in observed


def test_begin_captures_markdown_before_graph_writer_lock(
    sqlite_contract, monkeypatch
):
    _runtime, built, _clock, publisher, _reader = sqlite_contract
    graph_write_held = False
    observed = []
    graph_write_lock = sqlite_adapter_module.code_graph_write_lock
    capture = publisher._selector_resolver.capture

    @contextmanager
    def tracked_graph_write_lock(*args, **kwargs):
        nonlocal graph_write_held
        with graph_write_lock(*args, **kwargs):
            graph_write_held = True
            try:
                yield
            finally:
                graph_write_held = False

    def observed_capture(*args, **kwargs):
        observed.append(graph_write_held)
        return capture(*args, **kwargs)

    monkeypatch.setattr(
        sqlite_adapter_module,
        "code_graph_write_lock",
        tracked_graph_write_lock,
    )
    monkeypatch.setattr(
        publisher._selector_resolver,
        "capture",
        observed_capture,
    )

    publisher.begin(built.header)

    assert observed == [False]


def test_expired_lease_cannot_activate_after_heavy_work(
    sqlite_contract, monkeypatch
):
    _runtime, built, clock, publisher, reader = sqlite_contract
    _session, prior = _publish_all(publisher, built)
    session = publisher.begin(built.header)
    for batch in _batches(built):
        assert publisher.publish_batch(session, batch)["accepted"] is True
    graph_write_lock = sqlite_adapter_module.code_graph_write_lock
    advanced = False

    @contextmanager
    def expire_at_activation(*args, **kwargs):
        nonlocal advanced
        with graph_write_lock(*args, **kwargs):
            if not advanced:
                clock.advance(11)
                advanced = True
            yield

    monkeypatch.setattr(
        sqlite_adapter_module,
        "code_graph_write_lock",
        expire_at_activation,
    )

    assert publisher.finalize(session) == {"error": "session_expired"}
    assert reader.status()["snapshot_revision"] == prior["snapshot_revision"]


@pytest.mark.parametrize(
    ("column", "value", "expected"),
    [
        ("owner_id", "replacement-owner", "unauthorized"),
        ("state", "aborted", "session_expired"),
    ],
)
def test_activation_revalidates_session_owner_and_state(
    sqlite_contract, monkeypatch, column, value, expected
):
    _runtime, built, _clock, publisher, reader = sqlite_contract
    _session, prior = _publish_all(publisher, built)
    session = publisher.begin(built.header)
    for batch in _batches(built):
        assert publisher.publish_batch(session, batch)["accepted"] is True
    record = publisher._sessions[session.session_id]
    resolve_snapshot = publisher._selector_resolver.resolve_snapshot

    def mutate_session_after_heavy_work(*args, **kwargs):
        links = resolve_snapshot(*args, **kwargs)
        with closing(sqlite3.connect(record.staging)) as connection:
            connection.execute(
                f"UPDATE publication_session SET {column} = ?",
                (value,),
            )
            connection.commit()
        return links

    monkeypatch.setattr(
        publisher._selector_resolver,
        "resolve_snapshot",
        mutate_session_after_heavy_work,
    )

    assert publisher.finalize(session) == {"error": expected}
    assert reader.status()["snapshot_revision"] == prior["snapshot_revision"]


def test_lock_timeouts_are_sanitized_busy_results(
    sqlite_contract, monkeypatch
):
    _runtime, built, _clock, publisher, reader = sqlite_contract
    publish_session = publisher.begin(built.header)
    finalize_session = publisher.begin(built.header)
    abort_session = publisher.begin(built.header)
    for batch in _batches(built):
        assert publisher.publish_batch(finalize_session, batch)[
            "accepted"
        ] is True

    @contextmanager
    def busy_lock(*_args, **_kwargs):
        raise Timeout("/secret/internal/lock")
        yield

    monkeypatch.setattr(sqlite_adapter_module, "code_graph_read_lock", busy_lock)
    assert publisher.begin(built.header) == {"error": "busy"}

    monkeypatch.setattr(
        publisher,
        "_critical_section",
        busy_lock,
    )
    assert publisher.publish_batch(
        publish_session, _batches(built)[0]
    ) == {"error": "busy"}
    assert publisher.abort(abort_session) == {"error": "busy"}

    monkeypatch.setattr(
        sqlite_adapter_module,
        "code_graph_write_lock",
        busy_lock,
    )
    assert publisher.finalize(finalize_session) == {"error": "busy"}

    monkeypatch.setattr(
        sqlite_adapter_module,
        "_selector_read_lock",
        busy_lock,
    )

    status = reader.status()
    search = reader.search(validate_search_request("Service"))
    symbol_id = next(
        row["symbol_id"] for row in built.tables["symbols"]
        if row["qualified_name"] == "pkg.service.Service.run"
    )
    context = reader.context(validate_context_request([symbol_id]))

    assert status == {"state": "missing", "fresh": False, "error": "busy"}
    assert search["error"] == "busy"
    assert search["results"] == []
    assert context["error"] == "busy"
    assert context["nodes"] == []
    assert "/secret" not in repr((status, search, context))


def test_sqlite_session_lock_timeout_returns_sanitized_busy(
    sqlite_contract, monkeypatch
):
    _runtime, built, _clock, publisher, _reader = sqlite_contract
    session = publisher.begin(built.header)
    record = publisher._sessions[session.session_id]
    blocker = sqlite3.connect(record.staging, timeout=0)
    blocker.execute("BEGIN EXCLUSIVE")

    def connect_without_wait(_record):
        connection = sqlite3.connect(record.staging, timeout=0)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    monkeypatch.setattr(publisher, "_connect", connect_without_wait)
    try:
        result = publisher.publish_batch(session, _batches(built)[0])
    finally:
        blocker.rollback()
        blocker.close()

    assert result == {"error": "busy"}
    assert str(record.staging) not in repr(result)


def test_begin_performs_bounded_retention_cleanup(sqlite_contract):
    runtime, built, clock, publisher, _reader = sqlite_contract
    _publish_all(publisher, built)
    first = publisher.begin(built.header)
    second = publisher.begin(built.header)
    assert publisher.abort(first)["state"] == "aborted"
    assert publisher.abort(second)["state"] == "aborted"
    clock.advance(21)

    publisher.begin(built.header)

    retained = tuple(runtime.paths.database.parent.glob(
        f"{runtime.paths.database.name}.staging-*"
    ))
    assert len(retained) == 2
    with closing(sqlite3.connect(runtime.paths.database)) as connection:
        assert connection.execute(
            "SELECT state FROM repositories"
        ).fetchall() in ([], [("ready",)])


def test_replacement_publisher_cleans_retained_session(sqlite_contract):
    runtime, built, clock, publisher, _reader = sqlite_contract
    abandoned = publisher.begin(built.header)
    assert publisher.abort(abandoned) == {"state": "aborted"}
    clock.advance(21)
    replacement = _publisher(
        runtime,
        built,
        clock,
        max_batch_rows=1,
        max_batch_bytes=4096,
        publication_session_ttl_seconds=10,
        staging_retention_seconds=20,
        staging_cleanup_limit=1,
    )

    current = replacement.begin(built.header)

    retained = tuple(runtime.paths.database.parent.glob(
        f"{runtime.paths.database.name}.staging-*"
    ))
    assert len(retained) == 1
    assert current.session_id != abandoned.session_id


def test_replacement_publisher_cleans_retained_uncertain_session(
    sqlite_contract, monkeypatch
):
    runtime, built, clock, publisher, reader = sqlite_contract
    session = publisher.begin(built.header)
    for batch in _batches(built):
        assert publisher.publish_batch(session, batch)["accepted"] is True
    monkeypatch.setattr(
        publisher,
        "_sync_canonical_directory",
        lambda: (_ for _ in ()).throw(CodeGraphStoreError("sync failed")),
    )
    uncertain = publisher.finalize(session)
    journal = publisher._sessions[session.session_id].staging
    assert uncertain["error"] == "commit_uncertain"
    assert journal.exists()
    assert reader.status()["snapshot_revision"] == uncertain[
        "snapshot_revision"
    ]
    clock.advance(21)

    same_process = publisher.begin(built.header)

    assert isinstance(same_process, sqlite_adapter_module.PublicationSession)
    assert journal.exists()
    assert publisher.abort(same_process) == {"state": "aborted"}
    replacement = _publisher(
        runtime,
        built,
        clock,
        max_batch_rows=1,
        max_batch_bytes=4096,
        publication_session_ttl_seconds=10,
        staging_retention_seconds=20,
        staging_cleanup_limit=1,
    )

    current = replacement.begin(built.header)

    assert isinstance(current, sqlite_adapter_module.PublicationSession)
    assert not journal.exists()
    assert reader.status()["snapshot_revision"] == uncertain[
        "snapshot_revision"
    ]


def test_built_snapshot_rows_are_portable_and_source_free(sqlite_contract):
    runtime, built, _clock, _publisher, _reader = sqlite_contract
    encoded = json.dumps(built.tables, sort_keys=True)

    assert built.private_root == Path(runtime.binding.project_dir).resolve()
    assert str(built.private_root) not in encoded
    assert "root_path" not in encoded
    assert "git_remote" not in encoded
    assert "source_text" not in encoded
    assert set(built.tables) == {"repositories", "files", "symbols", "relations"}
