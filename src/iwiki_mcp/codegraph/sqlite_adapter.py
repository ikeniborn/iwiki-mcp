"""Shared publication protocol adapter for the local SQLite code graph."""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from functools import wraps
import json
from pathlib import Path
import sqlite3
import threading
from typing import Callable, Iterator, Mapping
import uuid

from filelock import Timeout

from .canonical import canonical_bytes_sha256, canonical_json_bytes, canonical_sha256
from .config import CodeGraphConfig
from .context import (
    CodeGraphContext,
    ContextRequest,
    capture_project_root,
)
from .indexer import (
    _seal_ready_metadata,
    _selector_read_lock,
    _wiki_read_lock,
    exact_ready_metadata,
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
from .schema import (
    SCHEMA_VERSION,
    CodeGraphStoreError,
    configure,
    create_publication_schema,
)
from .store import (
    CodeGraphPublishedError,
    CodeGraphStore,
    _read_publication_envelope,
    _snapshot_revision,
    _table_rows,
    _validate_normalized_rows,
    _write_publication_envelope,
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


def _map_busy(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except Timeout:
            return {"error": "busy"}
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", None) in {
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            }:
                return {"error": "busy"}
            raise

    return wrapped


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


def _metadata_path(store: CodeGraphStore) -> Path:
    return store.path.with_name(f"{store.path.stem}.metadata.json")


def _publication_metadata(path: Path) -> Mapping[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _legacy_metadata_matches_snapshot(
    connection: sqlite3.Connection,
    domain: str,
    metadata: Mapping[str, object],
) -> bool:
    repositories = _table_rows(connection, "repositories")
    if len(repositories) != 1:
        return False
    repository = repositories[0]
    fingerprints = metadata.get("fingerprints")
    if not isinstance(fingerprints, Mapping):
        return False
    if (
        repository.get("repository_id") != domain
        or repository.get("state") != "ready"
        or repository.get("revision") != metadata.get("revision")
        or repository.get("indexed_at") != metadata.get("indexed_at")
        or repository.get("git_commit") != metadata.get("git_commit")
        or repository.get("source_fingerprint") != fingerprints.get("source")
        or repository.get("config_fingerprint") != fingerprints.get("config")
        or repository.get("parser_fingerprint") != fingerprints.get("parser")
        or repository.get("normalizer_version")
        != metadata.get("normalizer_version")
        or repository.get("unicode_data_version")
        != metadata.get("unicode_data_version")
    ):
        return False
    revision = _snapshot_revision(
        repository,
        _table_rows(connection, "files"),
        _table_rows(connection, "symbols"),
        _table_rows(connection, "relations"),
        _table_rows(connection, "wiki_code_links"),
    )
    return revision == metadata.get("revision")


def _ready_publication_metadata(
    connection: sqlite3.Connection,
    *,
    store: CodeGraphStore,
    metadata_path: Path,
    domain: str,
) -> Mapping[str, object] | None:
    embedded = _read_publication_envelope(connection)
    if embedded is not None:
        if (
            embedded["domain"] != domain
            or embedded["repository_id"] != domain
        ):
            raise CodeGraphStoreError(
                "invalid code graph publication identity"
            )
        terminal = dict(embedded["terminal_result"])
        return {
            **terminal,
            "domain": domain,
            "revision": embedded["snapshot_revision"],
            "graph_payload_revision": embedded[
                "graph_payload_revision"
            ],
            "markdown_revision": embedded["markdown_revision"],
        }
    metadata = _publication_metadata(metadata_path)
    repository = connection.execute(
        "SELECT state, revision FROM repositories WHERE repository_id = ?",
        (domain,),
    ).fetchone()
    if (
        metadata is None
        or repository is None
        or repository[0] != "ready"
        or not exact_ready_metadata(metadata)
        or metadata.get("domain") != domain
        or metadata.get("revision") != repository[1]
        or metadata.get("storage_stamp") != store.storage_stamp()
        or not _legacy_metadata_matches_snapshot(connection, domain, metadata)
    ):
        return None
    return metadata


@dataclass
class _SessionRecord:
    session_id: str
    staging: Path
    selector_snapshot: object | None
    created_at: datetime
    updated_at: datetime
    publication_directory: Path | None = None
    prior_backup: Path | None = None


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
        diagnostics: Mapping[str, object],
        git_remote: str | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._store = store
        self._domain = domain
        self._private_root = Path(private_root).resolve()
        self._selector_resolver = selector_resolver
        self._wiki_base = str(selector_resolver.base_dir)
        self._lock_path = Path(lock_path)
        self._config = config
        self._diagnostics = dict(diagnostics)
        self._git_remote = git_remote
        self._clock = clock
        self._metadata_path = _metadata_path(store)
        self._owner_id = uuid.uuid4().hex
        self._sessions: dict[str, _SessionRecord] = {}
        self._terminal: dict[str, dict[str, object]] = {}
        self._uncertain: dict[str, dict[str, object]] = {}
        self._mutex = threading.RLock()

    @contextmanager
    def _critical_section(self) -> Iterator[None]:
        with self._mutex:
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

    def _current_ready_metadata(self) -> Mapping[str, object]:
        if not self._store.path.exists():
            return {}
        try:
            with self._store.read_lease() as connection:
                return _ready_publication_metadata(
                    connection,
                    store=self._store,
                    metadata_path=self._metadata_path,
                    domain=self._domain,
                ) or {}
        except (OSError, sqlite3.DatabaseError, CodeGraphStoreError):
            return {}

    def _write_ready_envelope(
        self,
        staging: Path,
        *,
        session_id: str,
        result: Mapping[str, object],
    ) -> None:
        with closing(sqlite3.connect(staging)) as connection:
            persisted = _write_publication_envelope(
                connection,
                domain=self._domain,
                repository_id=self._domain,
                session_id=session_id,
                graph_payload_revision=str(
                    result["graph_payload_revision"]
                ),
                snapshot_revision=str(result["snapshot_revision"]),
                markdown_revision=str(result["markdown_revision"]),
                counts=dict(result["counts"]),
                indexed_at=str(result["indexed_at"]),
                terminal_result=result,
            )
            if _read_publication_envelope(connection) != {
                **persisted,
                "counts": dict(result["counts"]),
                "terminal_result": dict(result),
            }:
                raise CodeGraphStoreError(
                    "cannot verify code graph publication envelope"
                )

    def _sync_canonical_directory(self) -> None:
        self._store.sync_canonical_directory()

    def _record_external_terminal(
        self,
        record: _SessionRecord,
        *,
        state: str,
        now: datetime,
    ) -> None:
        self._set_state(record, state, now)

    def _publish_metadata_cache(
        self, metadata: Mapping[str, object]
    ) -> None:
        cached = {
            **metadata,
            "storage_stamp": self._store.storage_stamp(),
        }
        _seal_ready_metadata(cached)
        if not exact_ready_metadata(cached):
            raise CodeGraphStoreError(
                "invalid code graph publication metadata cache"
            )
        staging = self._store.prepare_metadata(self._metadata_path, cached)
        try:
            self._store.publish_metadata(self._metadata_path, staging)
        except CodeGraphStoreError:
            try:
                self._store.discard_metadata(staging)
            except CodeGraphStoreError:
                pass
            raise

    def _current_markdown_revision(self, record: _SessionRecord) -> str:
        snapshot = self._selector_resolver.capture(
            domain=self._domain,
            max_bytes=selector_capture_budget(
                self._config.max_file_bytes,
                self._config.max_total_files,
            ),
        )
        try:
            revision = _markdown_revision(snapshot)
        finally:
            self._selector_resolver.close_snapshot(snapshot)
        self._selector_resolver.verify_snapshot(record.selector_snapshot)
        return revision

    @_map_busy
    def begin(self, header: SnapshotHeader) -> PublicationSession:
        if header.repository_id != self._domain:
            return {"error": "scope_mismatch"}
        if (
            header.protocol_version != 1
            or header.schema_version != SCHEMA_VERSION
            or tuple(header.languages) != ("python",)
        ):
            return {"error": "snapshot_incomplete"}
        selector_snapshot = None
        with self._mutex:
            now = self._clock()
            self._cleanup(now)
            try:
                with _wiki_read_lock(
                    self._wiki_base,
                    self._config.max_rebuild_seconds,
                ):
                    selector_snapshot = self._selector_resolver.capture(
                        domain=self._domain,
                        max_bytes=selector_capture_budget(
                            self._config.max_file_bytes,
                            self._config.max_total_files,
                        ),
                    )
                    markdown_revision = _markdown_revision(selector_snapshot)
                    with code_graph_read_lock(self._lock_path):
                        base_revision = self._base_snapshot_revision()
                    self._selector_resolver.verify_snapshot(selector_snapshot)
            except SelectorError:
                if selector_snapshot is not None:
                    self._selector_resolver.close_snapshot(selector_snapshot)
                return {"error": "markdown_unavailable"}
            except BaseException:
                if selector_snapshot is not None:
                    self._selector_resolver.close_snapshot(selector_snapshot)
                raise
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
                            base_revision,
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
                base_snapshot_revision=base_revision,
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

    @_map_busy
    def publish_batch(
        self, session: PublicationSession, batch: SnapshotBatch
    ) -> dict[str, object]:
        with self._critical_section():
            terminal = self._terminal.get(
                getattr(session, "session_id", "")
            )
            if terminal is not None:
                return dict(terminal)
            record = self._owned_record(session)
            if record is None:
                return {"error": "unauthorized"}
            if record.session_id in self._uncertain:
                return {"error": "session_expired"}
            return self._publish_batch_locked(record, batch, self._clock())

    def _publish_batch_locked(
        self,
        record: _SessionRecord,
        batch: SnapshotBatch,
        now: datetime,
    ) -> dict[str, object]:
        with closing(self._connect(record)) as connection:
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
        self._discard_prior_backup(record)
        result = {"error": error}
        self._terminal[record.session_id] = result
        return result

    def _discard_prior_backup(self, record: _SessionRecord) -> None:
        if record.prior_backup is None:
            return
        try:
            self._store.discard_staging(record.prior_backup)
        except CodeGraphStoreError:
            pass
        record.prior_backup = None

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

    def _finish_ready(
        self,
        record: _SessionRecord,
        result: Mapping[str, object],
        *,
        now: datetime,
        ready_metadata: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        completed = dict(result)
        self._uncertain.pop(record.session_id, None)
        self._terminal[record.session_id] = completed
        self._close_selector(record)
        try:
            self._record_external_terminal(
                record,
                state="ready",
                now=now,
            )
        except (OSError, sqlite3.DatabaseError, CodeGraphStoreError):
            pass
        try:
            self._store.discard_staging(record.staging)
        except CodeGraphStoreError:
            pass
        self._discard_prior_backup(record)
        if record.publication_directory is not None:
            try:
                self._store.discard_published_staging_directory(
                    record.publication_directory
                )
            except CodeGraphStoreError:
                pass
            record.publication_directory = None
        self._sessions.pop(record.session_id, None)
        if ready_metadata is not None:
            try:
                self._publish_metadata_cache(ready_metadata)
            except (OSError, sqlite3.DatabaseError, CodeGraphStoreError):
                pass
        return completed

    def _reconcile_uncertain(
        self,
        record: _SessionRecord,
        result: Mapping[str, object],
    ) -> dict[str, object]:
        try:
            with self._store.read_lease() as connection:
                envelope = _read_publication_envelope(connection)
        except (OSError, sqlite3.DatabaseError, CodeGraphStoreError):
            envelope = None
        if (
            envelope is None
            or envelope.get("domain") != self._domain
            or envelope.get("repository_id") != self._domain
            or envelope.get("session_id") != record.session_id
            or envelope.get("snapshot_revision")
            != result.get("snapshot_revision")
        ):
            now = self._clock()
            self._uncertain.pop(record.session_id, None)
            return self._finish_error(
                record,
                state="conflicted",
                error="snapshot_conflict",
                now=now,
            )
        try:
            self._sync_canonical_directory()
        except (OSError, sqlite3.DatabaseError, CodeGraphStoreError):
            return {
                "error": "commit_uncertain",
                "snapshot_revision": result["snapshot_revision"],
            }
        return self._finish_ready(
            record,
            result,
            now=self._clock(),
        )

    @_map_busy
    def finalize(self, session: PublicationSession) -> dict[str, object]:
        with self._mutex:
            return self._finalize_serialized(session)

    def _finalize_serialized(
        self, session: PublicationSession
    ) -> dict[str, object]:
        terminal = self._terminal.get(getattr(session, "session_id", ""))
        if terminal is not None:
            return dict(terminal)
        record = self._owned_record(session)
        if record is None:
            return {"error": "unauthorized"}
        uncertain = self._uncertain.get(record.session_id)
        if uncertain is not None:
            return self._reconcile_uncertain(record, uncertain)
        with self._mutex:
            now = self._clock()
            with closing(self._connect(record)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                (
                    owner_id,
                    state,
                    lease_text,
                    header_json,
                    base_revision,
                    markdown,
                ) = self._session_row(connection)
                if owner_id != self._owner_id:
                    connection.rollback()
                    return {"error": "unauthorized"}
                if state != "staging" or now >= _parse_timestamp(
                    str(lease_text)
                ):
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

            if self._base_snapshot_revision() != base_revision:
                return self._finish_error(
                    record,
                    state="conflicted",
                    error="snapshot_conflict",
                    now=now,
                )
            try:
                current_markdown = self._current_markdown_revision(record)
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
                "git_remote": self._git_remote,
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
            previous_metadata = self._current_ready_metadata()
            previous_generation = previous_metadata.get("generation")
            generation = (
                previous_generation + 1
                if type(previous_generation) is int
                and previous_generation >= 0
                else 0
            )
            ready_metadata = {
                **self._diagnostics,
                "state": "ready",
                "fresh": True,
                "revision": snapshot_revision,
                "generation": generation,
                "publication_phase": "pending_final_verify",
                "pending_final_verify": True,
                "recovery_policy": "failed",
                "graph_payload_revision": header.graph_payload_revision,
                "markdown_revision": markdown,
            }
            ready_metadata.pop("metadata_digest", None)
            ready_metadata.pop("storage_stamp", None)
            result = {
                "state": "ready",
                "snapshot_revision": snapshot_revision,
                "graph_payload_revision": header.graph_payload_revision,
                "markdown_revision": markdown,
                "counts": dict(header.expected_counts),
                "indexed_at": repository["indexed_at"],
            }
            graph_staging = None
            try:
                graph_staging = self._store.create_staging_path()
                record.publication_directory = graph_staging.parent
                with closing(sqlite3.connect(graph_staging)) as connection:
                    configure(connection)
                    create_publication_schema(connection)
                staging_store = CodeGraphStore(graph_staging)
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
                self._write_ready_envelope(
                    graph_staging,
                    session_id=record.session_id,
                    result=result,
                )
                self._store.prepare_staging(
                    graph_staging,
                    repository_id=self._domain,
                    expected_revision=snapshot_revision,
                )
                record.prior_backup = self._store.prepare_prior_backup(
                    repository_id=self._domain,
                    expected_revision=base_revision,
                )
                activation_error = None
                activation_now = now
                sync_uncertain = False
                with self._mutex:
                    with _wiki_read_lock(
                        self._wiki_base,
                        self._config.max_rebuild_seconds,
                    ):
                        try:
                            final_markdown = self._current_markdown_revision(
                                record
                            )
                        except SelectorSnapshotChanged:
                            final_markdown = None
                        if final_markdown != markdown:
                            activation_error = "snapshot_conflict"
                        if activation_error is None:
                            with code_graph_write_lock(
                                self._lock_path,
                                timeout=self._config.max_rebuild_seconds,
                            ):
                                if (
                                    self._base_snapshot_revision()
                                    != base_revision
                                ):
                                    activation_error = "snapshot_conflict"
                                else:
                                    with closing(
                                        self._connect(record)
                                    ) as activation:
                                        activation.execute("BEGIN IMMEDIATE")
                                        (
                                            owner_id,
                                            state,
                                            lease_text,
                                            *_rest,
                                        ) = self._session_row(activation)
                                        activation_now = self._clock()
                                        if owner_id != self._owner_id:
                                            activation.rollback()
                                            activation_error = "unauthorized"
                                        elif (
                                            state != "staging"
                                            or activation_now
                                            >= _parse_timestamp(
                                                str(lease_text)
                                            )
                                        ):
                                            activation.rollback()
                                            activation_error = (
                                                "session_expired"
                                            )
                                        else:
                                            activation.rollback()
                                            try:
                                                self._store.replace_staging_logical(
                                                    graph_staging
                                                )
                                                graph_staging = None
                                                self._sync_canonical_directory()
                                            except CodeGraphPublishedError:
                                                graph_staging = None
                                                sync_uncertain = True
                                            except (
                                                OSError,
                                                sqlite3.DatabaseError,
                                                CodeGraphStoreError,
                                            ):
                                                if graph_staging is None:
                                                    sync_uncertain = True
                                                else:
                                                    raise
                if activation_error is not None:
                    if graph_staging is not None:
                        self._store.discard_staging(graph_staging)
                        graph_staging = None
                    if activation_error == "unauthorized":
                        return {"error": "unauthorized"}
                    return self._finish_error(
                        record,
                        state=(
                            "conflicted"
                            if activation_error == "snapshot_conflict"
                            else "expired"
                        ),
                        error=activation_error,
                        now=activation_now,
                    )
                if sync_uncertain:
                    self._uncertain[record.session_id] = dict(result)
                    record.updated_at = activation_now
                    try:
                        self._set_state(
                            record,
                            "commit_uncertain",
                            activation_now,
                        )
                    except (
                        OSError,
                        sqlite3.DatabaseError,
                        CodeGraphStoreError,
                    ):
                        pass
                    self._discard_prior_backup(record)
                    if record.publication_directory is not None:
                        try:
                            self._store.discard_published_staging_directory(
                                record.publication_directory
                            )
                        except CodeGraphStoreError:
                            pass
                        record.publication_directory = None
                    return {
                        "error": "commit_uncertain",
                        "snapshot_revision": snapshot_revision,
                    }
            except Timeout:
                if graph_staging is not None:
                    try:
                        self._store.discard_staging(graph_staging)
                    except CodeGraphStoreError:
                        pass
                self._discard_prior_backup(record)
                raise
            except (KeyError, OSError, sqlite3.DatabaseError, CodeGraphStoreError):
                if graph_staging is not None:
                    try:
                        self._store.discard_staging(graph_staging)
                    except CodeGraphStoreError:
                        pass
                return self._finish_error(
                    record,
                    state="failed",
                    error="snapshot_incomplete",
                    now=now,
                )
            return self._finish_ready(
                record,
                result,
                now=activation_now,
                ready_metadata=ready_metadata,
            )

    @_map_busy
    def abort(self, session: PublicationSession) -> dict[str, object]:
        with self._critical_section():
            terminal = self._terminal.get(
                getattr(session, "session_id", "")
            )
            if terminal is not None:
                return dict(terminal)
            record = self._owned_record(session)
            if record is None:
                return {"error": "unauthorized"}
            if record.session_id in self._uncertain:
                return {"error": "session_expired"}
            return self._abort_locked(record, self._clock())

    def _abort_locked(
        self, record: _SessionRecord, now: datetime
    ) -> dict[str, object]:
        with closing(self._connect(record)) as connection:
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
        self._wiki_base = str(selector_resolver.base_dir)
        self._metadata_path = _metadata_path(store)
        self._validated_storage_stamp: Mapping[str, object] | None = None
        self._validated_metadata: Mapping[str, object] | None = None

    def _ready_metadata(
        self, connection: sqlite3.Connection
    ) -> Mapping[str, object] | None:
        publication_profile = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'code_graph_publication'"
        ).fetchone() is not None
        if not publication_profile:
            return _ready_publication_metadata(
                connection,
                store=self._store,
                metadata_path=self._metadata_path,
                domain=self._domain,
            )
        stamp = self._store.storage_stamp()
        if (
            self._validated_metadata is not None
            and self._validated_storage_stamp == stamp
        ):
            return dict(self._validated_metadata)
        metadata = _ready_publication_metadata(
            connection,
            store=self._store,
            metadata_path=self._metadata_path,
            domain=self._domain,
        )
        self._validated_storage_stamp = stamp if metadata is not None else None
        self._validated_metadata = dict(metadata) if metadata is not None else None
        return metadata

    def _current_markdown_revision(self) -> str | None:
        try:
            snapshot = self._selector_resolver.capture(
                domain=self._domain,
            )
        except SelectorError:
            return None
        try:
            return _markdown_revision(snapshot)
        finally:
            self._selector_resolver.close_snapshot(snapshot)

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
        snapshot_revision = str(repository[1])
        metadata = self._ready_metadata(connection)
        if metadata is None or metadata.get("revision") != snapshot_revision:
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
        stored_markdown_revision = None
        graph_payload_revision = None
        if (
            metadata.get("domain") == self._domain
            and metadata.get("revision") == snapshot_revision
        ):
            stored = metadata.get("markdown_revision")
            payload_revision = metadata.get("graph_payload_revision")
            if isinstance(stored, str):
                stored_markdown_revision = stored
            if isinstance(payload_revision, str):
                graph_payload_revision = payload_revision
        current_markdown_revision = self._current_markdown_revision()
        return {
            "domain": self._domain,
            "state": "ready",
            "fresh": True,
            "revision": snapshot_revision,
            "snapshot_revision": snapshot_revision,
            "graph_payload_revision": graph_payload_revision,
            "markdown_revision": stored_markdown_revision,
            "stored_markdown_revision": stored_markdown_revision,
            "current_markdown_revision": current_markdown_revision,
            "wiki_links_stale": (
                stored_markdown_revision != current_markdown_revision
            ),
            "counts": counts,
        }

    def status(self) -> dict[str, object]:
        try:
            with _selector_read_lock(self._wiki_base, self._lock_path, 0):
                with self._store.read_lease() as connection:
                    return self._status(connection)
        except Timeout:
            return {
                "state": "missing",
                "fresh": False,
                "error": "busy",
            }
        except (OSError, sqlite3.DatabaseError, CodeGraphStoreError):
            return {
                "state": "missing",
                "fresh": False,
                "error": "missing_snapshot",
            }

    def search(self, request: ValidatedSearchRequest) -> dict[str, object]:
        try:
            with _selector_read_lock(self._wiki_base, self._lock_path, 0):
                with self._store.read_lease() as connection:
                    status = self._status(connection)
                    if status.get("state") != "ready":
                        return {**status, "results": []}
                    results = CodeGraphQuery(self._domain).search(
                        connection, request
                    )
        except Timeout:
            return {
                "state": "missing",
                "fresh": False,
                "error": "busy",
                "results": [],
            }
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
            with _selector_read_lock(self._wiki_base, self._lock_path, 0):
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
                    effective_request = request
                    if status["wiki_links_stale"] and request.include_wiki:
                        effective_request = replace(request, include_wiki=False)
                    response = engine.context(connection, effective_request)
                    if status["wiki_links_stale"] and request.include_wiki:
                        response["warnings"].append("wiki_links_stale")
        except Timeout:
            return {
                "state": "missing",
                "fresh": False,
                "error": "busy",
                "seeds": list(request.seeds),
                "nodes": [],
                "relations": [],
                "files": [],
                "wiki_pages": [],
                "warnings": [],
            }
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
