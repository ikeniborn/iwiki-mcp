"""Shared publication protocol adapter for the local SQLite code graph."""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import threading
from typing import Callable, Iterator, Mapping
import uuid

from .canonical import canonical_bytes_sha256, canonical_json_bytes, canonical_sha256
from .config import CodeGraphConfig
from .context import (
    CodeGraphContext,
    ContextRequest,
    capture_project_root,
)
from .linking import (
    SelectorError,
    SelectorSnapshotChanged,
    WikiSelectorResolver,
    selector_capture_budget,
)
from .publication import (
    PublicationSession,
    RowKind,
    SnapshotBatch,
    SnapshotHeader,
    _portable_row,
    graph_payload_revision,
)
from .query import CodeGraphQuery, ValidatedSearchRequest
from .schema import SCHEMA_VERSION, CodeGraphStoreError
from .store import (
    CodeGraphStore,
    _snapshot_revision,
    _validate_normalized_rows,
    code_graph_read_lock,
    code_graph_write_lock,
)


_ROW_KINDS: tuple[RowKind, ...] = (
    "repositories",
    "files",
    "symbols",
    "relations",
)
_KIND_ORDER = {kind: index for index, kind in enumerate(_ROW_KINDS)}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _header_mapping(header: SnapshotHeader) -> dict[str, object]:
    return {
        "protocol_version": header.protocol_version,
        "schema_version": header.schema_version,
        "repository_id": header.repository_id,
        "source_fingerprint": header.source_fingerprint,
        "parser_fingerprint": header.parser_fingerprint,
        "normalizer_version": header.normalizer_version,
        "unicode_data_version": header.unicode_data_version,
        "languages": list(header.languages),
        "expected_counts": dict(header.expected_counts),
        "graph_payload_revision": header.graph_payload_revision,
    }


def _markdown_revision(snapshot) -> str:
    rows = tuple(
        (page.relative[:-3], page.content_hash)
        for page in snapshot.pages
    )
    return canonical_sha256(rows, prefix=True)


@dataclass
class _SessionRecord:
    session_id: str
    staging: Path
    selector_snapshot: object | None
    created_at: datetime
    updated_at: datetime


class SqliteSnapshotPublisher:
    """Publish one local snapshot through leased row-native batches."""

    def __init__(
        self,
        *,
        store: CodeGraphStore,
        domain: str,
        private_root: Path,
        selector_resolver: WikiSelectorResolver,
        lock_path: Path,
        config: CodeGraphConfig,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._store = store
        self._domain = domain
        self._private_root = Path(private_root).resolve()
        self._selector_resolver = selector_resolver
        self._lock_path = Path(lock_path)
        self._config = config
        self._clock = clock
        self._owner_id = uuid.uuid4().hex
        self._sessions: dict[str, _SessionRecord] = {}
        self._terminal: dict[str, dict[str, object]] = {}
        self._mutex = threading.RLock()

    @contextmanager
    def _critical_section(self) -> Iterator[None]:
        with self._mutex, code_graph_write_lock(
            self._lock_path,
            timeout=self._config.max_rebuild_seconds,
        ):
            yield

    def _close_selector(self, record: _SessionRecord) -> None:
        if record.selector_snapshot is not None:
            self._selector_resolver.close_snapshot(record.selector_snapshot)
            record.selector_snapshot = None

    def _cleanup(self, now: datetime) -> None:
        removed = 0
        retention = timedelta(seconds=self._config.staging_retention_seconds)
        for session_id, record in sorted(
            self._sessions.items(), key=lambda item: item[1].updated_at
        ):
            if removed >= self._config.staging_cleanup_limit:
                break
            state = self._session_state(record)
            if state == "staging" and now >= self._lease_expiry(record):
                self._set_state(record, "expired", now)
                state = "expired"
            if (
                state in {"aborted", "expired", "conflicted", "failed"}
                and now - record.updated_at >= retention
            ):
                self._close_selector(record)
                self._store.discard_staging(record.staging)
                self._sessions.pop(session_id, None)
                removed += 1
        self._store.cleanup_retained_publication_staging(
            now=now,
            retention_seconds=self._config.staging_retention_seconds,
            limit=self._config.staging_cleanup_limit - removed,
            exclude=tuple(record.staging for record in self._sessions.values()),
        )

    @staticmethod
    def _connect(record: _SessionRecord) -> sqlite3.Connection:
        connection = sqlite3.connect(record.staging)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _session_row(self, connection: sqlite3.Connection) -> tuple[object, ...]:
        row = connection.execute(
            "SELECT owner_id, state, lease_expires_at, header_json, "
            "base_snapshot_revision, base_markdown_token "
            "FROM publication_session"
        ).fetchone()
        if row is None:
            raise CodeGraphStoreError("publication session unavailable")
        return row

    def _session_state(self, record: _SessionRecord) -> str:
        with closing(self._connect(record)) as connection:
            return str(self._session_row(connection)[1])

    def _lease_expiry(self, record: _SessionRecord) -> datetime:
        with closing(self._connect(record)) as connection:
            return _parse_timestamp(str(self._session_row(connection)[2]))

    def _set_state(
        self, record: _SessionRecord, state: str, now: datetime
    ) -> None:
        with closing(self._connect(record)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE publication_session SET state = ?, updated_at = ?",
                (state, _timestamp(now)),
            )
            connection.commit()
        record.updated_at = now
        if state != "staging":
            self._close_selector(record)

    def _base_snapshot_revision(self) -> str | None:
        if not self._store.path.exists():
            return None
        try:
            with self._store.read_lease() as connection:
                row = self._store.repository_state(connection, self._domain)
        except CodeGraphStoreError:
            return None
        return row[1] if row is not None and row[0] == "ready" else None

    def begin(self, header: SnapshotHeader) -> PublicationSession:
        if (
            header.protocol_version != 1
            or header.schema_version != SCHEMA_VERSION
            or header.repository_id != self._domain
            or tuple(header.languages) != ("python",)
        ):
            raise ValueError("invalid snapshot header")
        now = self._clock()
        with self._critical_section():
            self._cleanup(now)
            try:
                selector_snapshot = self._selector_resolver.capture(
                    domain=self._domain,
                    max_bytes=selector_capture_budget(
                        self._config.max_file_bytes,
                        self._config.max_total_files,
                    ),
                )
            except SelectorError as exc:
                raise CodeGraphStoreError(
                    "authoritative Markdown is unavailable"
                ) from exc
            markdown_revision = _markdown_revision(selector_snapshot)
            staging = self._store.create_staging_path()
            session_id = uuid.uuid4().hex
            lease_expires = now + timedelta(
                seconds=self._config.publication_session_ttl_seconds
            )
            try:
                with closing(sqlite3.connect(staging)) as connection:
                    connection.executescript(
                        """
                        CREATE TABLE publication_session (
                            session_id TEXT PRIMARY KEY,
                            owner_id TEXT NOT NULL,
                            state TEXT NOT NULL,
                            lease_expires_at TEXT NOT NULL,
                            header_json TEXT NOT NULL,
                            base_snapshot_revision TEXT,
                            base_markdown_token TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        );
                        CREATE TABLE publication_batches (
                            kind TEXT NOT NULL,
                            ordinal INTEGER NOT NULL,
                            row_count INTEGER NOT NULL,
                            byte_count INTEGER NOT NULL,
                            payload_hash TEXT NOT NULL,
                            payload BLOB NOT NULL,
                            PRIMARY KEY(kind, ordinal)
                        );
                        """
                    )
                    connection.execute(
                        "INSERT INTO publication_session VALUES "
                        "(?, ?, 'staging', ?, ?, ?, ?, ?, ?)",
                        (
                            session_id,
                            self._owner_id,
                            _timestamp(lease_expires),
                            canonical_json_bytes(_header_mapping(header)).decode(
                                "utf-8"
                            ),
                            self._base_snapshot_revision(),
                            markdown_revision,
                            _timestamp(now),
                            _timestamp(now),
                        ),
                    )
                    connection.commit()
            except BaseException:
                self._selector_resolver.close_snapshot(selector_snapshot)
                self._store.discard_staging(staging)
                raise
            self._sessions[session_id] = _SessionRecord(
                session_id=session_id,
                staging=staging,
                selector_snapshot=selector_snapshot,
                created_at=now,
                updated_at=now,
            )
            return PublicationSession(
                session_id=session_id,
                lease_expires_at=_timestamp(lease_expires),
                base_snapshot_revision=self._base_snapshot_revision(),
                base_markdown_token=markdown_revision,
            )

    def _owned_record(
        self, session: PublicationSession
    ) -> _SessionRecord | None:
        if not isinstance(session, PublicationSession):
            return None
        return self._sessions.get(session.session_id)

    def _validate_batch(
        self, batch: SnapshotBatch
    ) -> tuple[dict[str, object], ...] | None:
        if (
            batch.kind not in _ROW_KINDS
            or type(batch.ordinal) is not int
            or batch.ordinal < 0
            or type(batch.row_count) is not int
            or not 1 <= batch.row_count <= self._config.max_batch_rows
            or type(batch.byte_count) is not int
            or batch.byte_count != len(batch.payload)
            or batch.byte_count > self._config.max_batch_bytes
            or canonical_bytes_sha256(batch.payload, prefix=True)
            != batch.payload_hash
        ):
            return None
        try:
            rows = json.loads(batch.payload.decode("utf-8"))
            if (
                not isinstance(rows, list)
                or len(rows) != batch.row_count
                or canonical_json_bytes(rows) != batch.payload
            ):
                return None
            projected = tuple(_portable_row(batch.kind, row) for row in rows)
            if any(dict(row) != projected[index] for index, row in enumerate(rows)):
                return None
            return tuple(dict(row) for row in rows)
        except (KeyError, TypeError, UnicodeError, ValueError):
            return None

    def publish_batch(
        self, session: PublicationSession, batch: SnapshotBatch
    ) -> dict[str, object]:
        record = self._owned_record(session)
        if record is None:
            return {"error": "unauthorized"}
        now = self._clock()
        with self._critical_section(), closing(self._connect(record)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            owner_id, state, lease_text, header_json, _base, _markdown = (
                self._session_row(connection)
            )
            if owner_id != self._owner_id:
                connection.rollback()
                return {"error": "unauthorized"}
            if state != "staging":
                connection.rollback()
                return {"error": "session_expired"}
            if now >= _parse_timestamp(str(lease_text)):
                connection.execute(
                    "UPDATE publication_session SET state = 'expired', "
                    "updated_at = ?",
                    (_timestamp(now),),
                )
                connection.commit()
                record.updated_at = now
                self._close_selector(record)
                return {"error": "session_expired"}
            rows = self._validate_batch(batch)
            if rows is None:
                connection.rollback()
                return {"error": "invalid_batch"}
            existing = connection.execute(
                "SELECT payload_hash FROM publication_batches "
                "WHERE kind = ? AND ordinal = ?",
                (batch.kind, batch.ordinal),
            ).fetchone()
            if existing is not None:
                if existing[0] != batch.payload_hash:
                    connection.rollback()
                    return {"error": "batch_conflict"}
            else:
                next_ordinal = connection.execute(
                    "SELECT COUNT(*) FROM publication_batches WHERE kind = ?",
                    (batch.kind,),
                ).fetchone()[0]
                highest_kind = connection.execute(
                    "SELECT kind FROM publication_batches"
                ).fetchall()
                if (
                    batch.ordinal != next_ordinal
                    or any(
                        _KIND_ORDER[str(item[0])] > _KIND_ORDER[batch.kind]
                        for item in highest_kind
                    )
                ):
                    connection.rollback()
                    return {"error": "invalid_batch"}
                header = json.loads(str(header_json))
                accepted_rows = connection.execute(
                    "SELECT COALESCE(SUM(row_count), 0) "
                    "FROM publication_batches WHERE kind = ?",
                    (batch.kind,),
                ).fetchone()[0]
                if (
                    accepted_rows + batch.row_count
                    > header["expected_counts"][batch.kind]
                ):
                    connection.rollback()
                    return {"error": "invalid_batch"}
                connection.execute(
                    "INSERT INTO publication_batches VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        batch.kind,
                        batch.ordinal,
                        batch.row_count,
                        batch.byte_count,
                        batch.payload_hash,
                        batch.payload,
                    ),
                )
            lease_expires = now + timedelta(
                seconds=self._config.publication_session_ttl_seconds
            )
            connection.execute(
                "UPDATE publication_session SET lease_expires_at = ?, "
                "updated_at = ?",
                (_timestamp(lease_expires), _timestamp(now)),
            )
            connection.commit()
            record.updated_at = now
            return {
                "accepted": True,
                "kind": batch.kind,
                "ordinal": batch.ordinal,
                "lease_expires_at": _timestamp(lease_expires),
            }

    def _finish_error(
        self,
        record: _SessionRecord,
        *,
        state: str,
        error: str,
        now: datetime,
    ) -> dict[str, object]:
        self._set_state(record, state, now)
        result = {"error": error}
        self._terminal[record.session_id] = result
        return result

    def _read_batches(
        self, connection: sqlite3.Connection
    ) -> dict[RowKind, tuple[Mapping[str, object], ...]]:
        tables: dict[RowKind, tuple[Mapping[str, object], ...]] = {}
        for kind in _ROW_KINDS:
            rows: list[Mapping[str, object]] = []
            for payload, in connection.execute(
                "SELECT payload FROM publication_batches WHERE kind = ? "
                "ORDER BY ordinal",
                (kind,),
            ):
                decoded = json.loads(bytes(payload).decode("utf-8"))
                rows.extend(decoded)
            tables[kind] = tuple(rows)
        return tables

    def finalize(self, session: PublicationSession) -> dict[str, object]:
        terminal = self._terminal.get(getattr(session, "session_id", ""))
        if terminal is not None:
            return dict(terminal)
        record = self._owned_record(session)
        if record is None:
            return {"error": "unauthorized"}
        now = self._clock()
        with self._critical_section(), closing(self._connect(record)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            owner_id, state, lease_text, header_json, base_revision, markdown = (
                self._session_row(connection)
            )
            if owner_id != self._owner_id:
                connection.rollback()
                return {"error": "unauthorized"}
            if state != "staging" or now >= _parse_timestamp(str(lease_text)):
                connection.rollback()
                return self._finish_error(
                    record,
                    state="expired",
                    error="session_expired",
                    now=now,
                )
            header = SnapshotHeader(**json.loads(str(header_json)))
            tables = self._read_batches(connection)
            connection.commit()

            if any(
                len(tables[kind]) != header.expected_counts[kind]
                for kind in _ROW_KINDS
            ):
                return self._finish_error(
                    record,
                    state="failed",
                    error="snapshot_incomplete",
                    now=now,
                )
            if graph_payload_revision(tables) != header.graph_payload_revision:
                return self._finish_error(
                    record,
                    state="failed",
                    error="revision_mismatch",
                    now=now,
                )
            repositories = tables["repositories"]
            if len(repositories) != 1:
                return self._finish_error(
                    record,
                    state="failed",
                    error="snapshot_incomplete",
                    now=now,
                )
            repository = dict(repositories[0])
            expected_repository = (
                header.repository_id,
                header.source_fingerprint,
                header.parser_fingerprint,
                header.normalizer_version,
                header.unicode_data_version,
            )
            actual_repository = (
                repository.get("repository_id"),
                repository.get("source_fingerprint"),
                repository.get("parser_fingerprint"),
                repository.get("normalizer_version"),
                repository.get("unicode_data_version"),
            )
            try:
                if actual_repository != expected_repository:
                    raise CodeGraphStoreError("repository header mismatch")
                _validate_normalized_rows(
                    header.repository_id,
                    tables["files"],
                    tables["symbols"],
                    tables["relations"],
                )
            except (KeyError, TypeError, ValueError, CodeGraphStoreError):
                return self._finish_error(
                    record,
                    state="failed",
                    error="snapshot_incomplete",
                    now=now,
                )

            current_revision = self._base_snapshot_revision()
            if current_revision != base_revision:
                return self._finish_error(
                    record,
                    state="conflicted",
                    error="snapshot_conflict",
                    now=now,
                )
            try:
                current_snapshot = self._selector_resolver.capture(
                    domain=self._domain,
                    max_bytes=selector_capture_budget(
                        self._config.max_file_bytes,
                        self._config.max_total_files,
                    ),
                )
                try:
                    current_markdown = _markdown_revision(current_snapshot)
                finally:
                    self._selector_resolver.close_snapshot(current_snapshot)
                self._selector_resolver.verify_snapshot(record.selector_snapshot)
            except SelectorSnapshotChanged:
                current_markdown = None
            except SelectorError:
                return self._finish_error(
                    record,
                    state="failed",
                    error="markdown_unavailable",
                    now=now,
                )
            if current_markdown != markdown:
                return self._finish_error(
                    record,
                    state="conflicted",
                    error="snapshot_conflict",
                    now=now,
                )
            try:
                links = self._selector_resolver.resolve_snapshot(
                    record.selector_snapshot,
                    domain=self._domain,
                    project_dir=str(self._private_root),
                    parsed_files=(),
                    relations=(),
                    snapshot={
                        "files": tables["files"],
                        "symbols": tables["symbols"],
                    },
                )
            except SelectorError:
                return self._finish_error(
                    record,
                    state="failed",
                    error="markdown_unavailable",
                    now=now,
                )

            persisted_repository = {
                **repository,
                "root_path": str(self._private_root),
                "git_remote": None,
                "revision": header.graph_payload_revision,
                "state": "rebuilding",
            }
            snapshot_revision = _snapshot_revision(
                persisted_repository,
                tables["files"],
                tables["symbols"],
                tables["relations"],
                links,
            )
            try:
                with closing(self._connect(record)) as staging_connection:
                    staging_connection.executescript(
                        "DROP TABLE publication_batches;"
                        "DROP TABLE publication_session;"
                        "PRAGMA user_version = 0;"
                    )
                    staging_connection.commit()
                staging_store = CodeGraphStore(record.staging)
                staging_store.insert_snapshot({
                    "repositories": (persisted_repository,),
                    "files": tables["files"],
                    "symbols": tables["symbols"],
                    "relations": tables["relations"],
                    "wiki_code_links": (),
                })
                staging_store.finalize_snapshot(
                    repository_id=self._domain,
                    revision=snapshot_revision,
                    indexed_at=str(repository["indexed_at"]),
                    wiki_code_links=links,
                )
                self._store.publish_staging(
                    record.staging,
                    repository_id=self._domain,
                    expected_revision=snapshot_revision,
                )
            except (KeyError, OSError, sqlite3.DatabaseError, CodeGraphStoreError):
                return self._finish_error(
                    record,
                    state="failed",
                    error="snapshot_incomplete",
                    now=now,
                )
            self._close_selector(record)
            result = {
                "state": "ready",
                "snapshot_revision": snapshot_revision,
                "graph_payload_revision": header.graph_payload_revision,
                "markdown_revision": markdown,
                "counts": dict(header.expected_counts),
                "indexed_at": repository["indexed_at"],
            }
            self._terminal[record.session_id] = result
            self._sessions.pop(record.session_id, None)
            return dict(result)

    def abort(self, session: PublicationSession) -> dict[str, object]:
        terminal = self._terminal.get(getattr(session, "session_id", ""))
        if terminal is not None:
            return dict(terminal)
        record = self._owned_record(session)
        if record is None:
            return {"error": "unauthorized"}
        now = self._clock()
        with self._critical_section(), closing(self._connect(record)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            owner_id, state, lease_text, _header, _base, _markdown = (
                self._session_row(connection)
            )
            if owner_id != self._owner_id:
                connection.rollback()
                return {"error": "unauthorized"}
            if state == "aborted":
                connection.rollback()
                return {"state": "aborted"}
            if state != "staging" or now >= _parse_timestamp(str(lease_text)):
                if state == "staging":
                    connection.execute(
                        "UPDATE publication_session SET state = 'expired', "
                        "updated_at = ?",
                        (_timestamp(now),),
                    )
                    connection.commit()
                    record.updated_at = now
                    self._close_selector(record)
                else:
                    connection.rollback()
                return {"error": "session_expired"}
            connection.execute(
                "UPDATE publication_session SET state = 'aborted', updated_at = ?",
                (_timestamp(now),),
            )
            connection.commit()
            record.updated_at = now
            self._close_selector(record)
            result = {"state": "aborted"}
            self._terminal[record.session_id] = result
            return result


class SqliteCodeGraphReader:
    """Read one active ready local SQLite snapshot through shared requests."""

    def __init__(
        self,
        *,
        store: CodeGraphStore,
        domain: str,
        private_root: Path,
        lock_path: Path,
        max_file_bytes: int,
        selector_resolver: WikiSelectorResolver,
    ) -> None:
        self._store = store
        self._domain = domain
        self._lock_path = Path(lock_path)
        self._context_root = capture_project_root(str(private_root))
        self._max_file_bytes = max_file_bytes
        self._selector_resolver = selector_resolver

    def _status(self, connection: sqlite3.Connection) -> dict[str, object]:
        repository = connection.execute(
            "SELECT state, revision FROM repositories WHERE repository_id = ?",
            (self._domain,),
        ).fetchone()
        if repository is None or repository[0] != "ready":
            return {
                "state": "missing",
                "fresh": False,
                "error": "missing_snapshot",
            }
        counts = {
            kind: connection.execute(
                f"SELECT COUNT(*) FROM {kind}"
            ).fetchone()[0]
            for kind in _ROW_KINDS
        }
        return {
            "domain": self._domain,
            "state": "ready",
            "fresh": True,
            "revision": str(repository[1]),
            "snapshot_revision": str(repository[1]),
            "counts": counts,
        }

    def status(self) -> dict[str, object]:
        try:
            with code_graph_read_lock(self._lock_path):
                with self._store.read_lease() as connection:
                    return self._status(connection)
        except (OSError, sqlite3.DatabaseError, CodeGraphStoreError):
            return {
                "state": "missing",
                "fresh": False,
                "error": "missing_snapshot",
            }

    def search(self, request: ValidatedSearchRequest) -> dict[str, object]:
        try:
            with code_graph_read_lock(self._lock_path):
                with self._store.read_lease() as connection:
                    status = self._status(connection)
                    if status.get("state") != "ready":
                        return {**status, "results": []}
                    results = CodeGraphQuery(self._domain).search(
                        connection, request
                    )
        except (OSError, sqlite3.DatabaseError, CodeGraphStoreError):
            return {
                "state": "missing",
                "fresh": False,
                "error": "missing_snapshot",
                "results": [],
            }
        return {**status, "results": [asdict(item) for item in results]}

    def context(self, request: ContextRequest) -> dict[str, object]:
        try:
            engine = CodeGraphContext(
                self._domain,
                self._context_root,
                self._max_file_bytes,
            )
            with code_graph_read_lock(self._lock_path):
                with self._store.read_lease() as connection:
                    status = self._status(connection)
                    if status.get("state") != "ready":
                        return {
                            **status,
                            "seeds": list(request.seeds),
                            "nodes": [],
                            "relations": [],
                            "files": [],
                            "wiki_pages": [],
                            "warnings": [],
                        }
                    response = engine.context(connection, request)
        except (OSError, sqlite3.DatabaseError, CodeGraphStoreError):
            return {
                "state": "missing",
                "fresh": False,
                "error": "missing_snapshot",
                "seeds": list(request.seeds),
                "nodes": [],
                "relations": [],
                "files": [],
                "wiki_pages": [],
                "warnings": [],
            }
        return {**status, **response}


__all__ = ["SqliteCodeGraphReader", "SqliteSnapshotPublisher"]
