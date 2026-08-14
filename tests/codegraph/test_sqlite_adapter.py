"""Real SQLite adapter contract over portable code-graph row batches."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from iwiki_mcp.codegraph.canonical import (
    canonical_bytes_sha256,
    canonical_json_bytes,
)
from iwiki_mcp.codegraph.context import validate_context_request
from iwiki_mcp.codegraph.linking import WikiSelectorResolver
from iwiki_mcp.codegraph.publication import (
    SnapshotBatch,
    SnapshotHeader,
    graph_payload_revision,
    iter_snapshot_batches,
)
from iwiki_mcp.codegraph.query import validate_search_request
from iwiki_mcp.codegraph.sqlite_adapter import (
    SqliteCodeGraphReader,
    SqliteSnapshotPublisher,
)


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _publisher(seed_runtime, built, clock, **config_overrides):
    indexer = seed_runtime.runtime._indexer
    config = replace(indexer.config, **config_overrides)
    return SqliteSnapshotPublisher(
        store=indexer.store,
        domain=seed_runtime.binding.primary,
        private_root=built.private_root,
        selector_resolver=WikiSelectorResolver(seed_runtime.binding.base),
        lock_path=indexer.paths.lock,
        config=config,
        clock=clock,
    )


@pytest.fixture
def sqlite_contract(seed_runtime):
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


def test_built_snapshot_rows_are_portable_and_source_free(sqlite_contract):
    runtime, built, _clock, _publisher, _reader = sqlite_contract
    encoded = json.dumps(built.tables, sort_keys=True)

    assert built.private_root == Path(runtime.binding.project_dir).resolve()
    assert str(built.private_root) not in encoded
    assert "root_path" not in encoded
    assert "git_remote" not in encoded
    assert "source_text" not in encoded
    assert set(built.tables) == {"repositories", "files", "symbols", "relations"}
