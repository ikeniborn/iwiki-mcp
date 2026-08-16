"""PostgreSQL code-graph publication sessions and atomic snapshot activation."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, replace
import datetime
import json
import secrets
from typing import Any, Iterator

import psycopg
from psycopg.types.json import Jsonb

from ..codegraph.canonical import canonical_sha256
from ..codegraph.context import (
    KNOWN_RELATIONS,
    ContextRequest,
    relation_from_row,
)
from ..codegraph.linking import (
    MarkdownPageSnapshot,
    markdown_revision,
    resolve_selectors,
)
from ..codegraph.models import ContextNode, ContextRelation, SearchResult
from ..codegraph.publication import (
    PublicationSession,
    SnapshotBatch,
    SnapshotHeader,
    graph_payload_revision,
    header_payload,
)
from ..codegraph.query import (
    MATCH_RANK,
    ValidatedSearchRequest,
    result_key,
    search_result_from_row,
)
from ..codegraph.reader import EMPTY_CONTEXT_RESULT, MISSING_READ_RESULT
from .store import validate_direct_principal


_LOCK_NAMESPACE = 0x4957494B
_LOCK_NOT_AVAILABLE = "55P03"
_ROW_KINDS = ("repositories", "files", "symbols", "relations")


def _error(code: str, hint: str, **extra: object) -> dict[str, object]:
    return {"error": code, "hint": hint, **extra}


_UNAUTHORIZED = _error(
    "unauthorized", "this publisher does not own the session"
)
_EXPIRED = _error("session_expired", "begin a new publication session")
_INCOMPLETE = _error(
    "snapshot_incomplete", "publish every expected batch before finalizing"
)
_BATCH_CONFLICT = _error(
    "batch_conflict", "resend the identical batch or begin a new session"
)
_SNAPSHOT_CONFLICT = _error(
    "snapshot_conflict", "begin a new publication session and retry"
)
_MARKDOWN_UNAVAILABLE = _error(
    "markdown_unavailable", "reindex against the current Markdown revision"
)
_BUSY = _error(
    "busy", "another publication holds this domain", retryable=True
)
_REVISION_MISMATCH = _error(
    "revision_mismatch", "rebuild the snapshot from the current graph payload"
)
_INVALID_BATCH = _error(
    "invalid_batch", "send batches that match the declared header"
)


class PostgresCodeGraphStore:
    """Publish tenant-scoped graph snapshots with fixed non-transferable owners."""

    def __init__(
        self,
        dsn: str,
        iwiki_id: str,
        domain: str,
        owner_id: str,
        *,
        lock_timeout_ms: int,
        session_ttl_seconds: int,
        staging_retention_seconds: int,
        staging_cleanup_limit: int,
        connection_factory: Callable[[], psycopg.Connection] | None = None,
        require_database_principal: bool = False,
        clock: Callable[[], datetime.datetime] | None = None,
    ) -> None:
        self._dsn = dsn
        self.iwiki_id = iwiki_id
        self.domain = domain
        self.owner_id = owner_id
        self._lock_timeout_ms = lock_timeout_ms
        self._session_ttl_seconds = session_ttl_seconds
        self._staging_retention_seconds = staging_retention_seconds
        self._staging_cleanup_limit = staging_cleanup_limit
        self._connection_factory = connection_factory or (
            lambda: psycopg.connect(dsn)
        )
        self._clock = clock or (
            lambda: datetime.datetime.now(datetime.timezone.utc)
        )
        if require_database_principal and validate_direct_principal(
            dsn,
            iwiki_id=iwiki_id,
            read_domains=(domain,),
            write_domains=(domain,),
        ) is not None:
            raise ValueError("invalid_config")

    # -- infrastructure -------------------------------------------------

    @contextmanager
    def _transaction(self) -> Iterator[psycopg.Cursor]:
        connection = self._connection_factory()
        try:
            with connection.transaction():
                with connection.cursor() as cursor:
                    yield cursor
        finally:
            connection.close()

    def _domain_id(self, cursor) -> int:
        cursor.execute(
            "SELECT domain_id FROM iwiki.domains "
            "WHERE iwiki_id = %s AND slug = %s",
            (self.iwiki_id, self.domain),
        )
        row = cursor.fetchone()
        if row is None:
            raise ValueError("scope_mismatch")
        return row[0]

    def _domain_state(self, cursor, domain_id: int) -> tuple[int, str | None]:
        cursor.execute(
            "INSERT INTO iwiki.code_graph_domain_state (iwiki_id, domain_id) "
            "VALUES (%s, %s) ON CONFLICT (iwiki_id, domain_id) DO NOTHING",
            (self.iwiki_id, domain_id),
        )
        cursor.execute(
            "SELECT domain_lock_id, active_snapshot_id "
            "FROM iwiki.code_graph_domain_state "
            "WHERE iwiki_id = %s AND domain_id = %s",
            (self.iwiki_id, domain_id),
        )
        return cursor.fetchone()

    def _markdown_generation(self, cursor, domain_id: int) -> int:
        cursor.execute(
            "SELECT markdown_generation FROM iwiki.domains "
            "WHERE iwiki_id = %s AND domain_id = %s",
            (self.iwiki_id, domain_id),
        )
        return cursor.fetchone()[0]

    def _active_revision(self, cursor, domain_id: int) -> str | None:
        cursor.execute(
            "SELECT s.snapshot_revision "
            "FROM iwiki.code_graph_domain_state d "
            "JOIN iwiki.code_graph_snapshots s "
            "ON s.iwiki_id = d.iwiki_id AND s.domain_id = d.domain_id "
            "AND s.snapshot_id = d.active_snapshot_id "
            "WHERE d.iwiki_id = %s AND d.domain_id = %s",
            (self.iwiki_id, domain_id),
        )
        row = cursor.fetchone()
        return row[0] if row is not None else None

    def _load_session(self, cursor, domain_id: int, session_id: str):
        cursor.execute(
            "SELECT snapshot_id, owner_id, state, lease_expires_at, "
            "base_snapshot_revision, captured_markdown_generation, "
            "terminal_result "
            "FROM iwiki.code_graph_publication_sessions "
            "WHERE iwiki_id = %s AND domain_id = %s AND session_id = %s "
            "FOR UPDATE",
            (self.iwiki_id, domain_id, session_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "snapshot_id": row[0],
            "owner_id": row[1],
            "state": row[2],
            "lease_expires_at": row[3],
            "base_snapshot_revision": row[4],
            "captured_markdown_generation": row[5],
            "terminal_result": row[6],
        }

    def _guard(self, record, now: datetime.datetime) -> dict[str, object] | None:
        if record is None or record["owner_id"] != self.owner_id:
            return dict(_UNAUTHORIZED)
        if record["state"] != "staging":
            return None
        if record["lease_expires_at"] <= now:
            return dict(_EXPIRED)
        return None

    def _renew(self, cursor, domain_id: int, session_id: str, now):
        expires = now + datetime.timedelta(seconds=self._session_ttl_seconds)
        cursor.execute(
            "UPDATE iwiki.code_graph_publication_sessions "
            "SET lease_expires_at = %s, updated_at = %s "
            "WHERE iwiki_id = %s AND domain_id = %s AND session_id = %s",
            (expires, now, self.iwiki_id, domain_id, session_id),
        )

    # -- lifecycle ------------------------------------------------------

    def begin(self, header: SnapshotHeader) -> PublicationSession:
        """Stage one owned session after bounded expired-staging cleanup."""
        now = self._clock()
        expires = now + datetime.timedelta(seconds=self._session_ttl_seconds)
        session_id = secrets.token_hex(16)
        snapshot_id = secrets.token_hex(16)
        with self._transaction() as cursor:
            domain_id = self._domain_id(cursor)
            self._domain_state(cursor, domain_id)
            self._cleanup_staging(cursor, domain_id, now)
            generation = self._markdown_generation(cursor, domain_id)
            base_revision = self._active_revision(cursor, domain_id)
            cursor.execute(
                "INSERT INTO iwiki.code_graph_snapshots "
                "(iwiki_id, domain_id, snapshot_id, state, header, "
                "graph_payload_revision, markdown_revision, "
                "markdown_generation, counts, created_at) "
                "VALUES (%s, %s, %s, 'staging', %s, %s, %s, %s, %s, %s)",
                (
                    self.iwiki_id,
                    domain_id,
                    snapshot_id,
                    Jsonb(header_payload(header)),
                    header.graph_payload_revision,
                    f"generation:{generation}",
                    generation,
                    Jsonb(dict(header.expected_counts)),
                    now,
                ),
            )
            cursor.execute(
                "INSERT INTO iwiki.code_graph_publication_sessions "
                "(iwiki_id, domain_id, session_id, snapshot_id, owner_id, "
                "state, lease_expires_at, base_snapshot_revision, "
                "captured_markdown_generation, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, 'staging', %s, %s, %s, %s, %s)",
                (
                    self.iwiki_id,
                    domain_id,
                    session_id,
                    snapshot_id,
                    self.owner_id,
                    expires,
                    base_revision,
                    generation,
                    now,
                    now,
                ),
            )
        return PublicationSession(
            session_id=session_id,
            lease_expires_at=expires.isoformat(),
            base_snapshot_revision=base_revision,
            base_markdown_token=generation,
        )

    def publish_batch(
        self, session: PublicationSession, batch: SnapshotBatch
    ) -> dict[str, object]:
        """Accept one idempotent batch under owner, state, and lease checks."""
        now = self._clock()
        with self._transaction() as cursor:
            domain_id = self._domain_id(cursor)
            record = self._load_session(cursor, domain_id, session.session_id)
            rejection = self._guard(record, now)
            if rejection is not None:
                return rejection
            if record["state"] != "staging":
                return dict(_SNAPSHOT_CONFLICT)
            if batch.kind not in _ROW_KINDS or batch.ordinal < 0:
                return dict(_INVALID_BATCH)
            cursor.execute(
                "SELECT payload_hash FROM iwiki.code_graph_batches "
                "WHERE iwiki_id = %s AND domain_id = %s AND session_id = %s "
                "AND kind = %s AND ordinal = %s",
                (
                    self.iwiki_id,
                    domain_id,
                    session.session_id,
                    batch.kind,
                    batch.ordinal,
                ),
            )
            stored = cursor.fetchone()
            if stored is not None:
                if stored[0] != batch.payload_hash:
                    return dict(_BATCH_CONFLICT)
                self._renew(cursor, domain_id, session.session_id, now)
                return {"accepted": True}
            cursor.execute(
                "INSERT INTO iwiki.code_graph_batches "
                "(iwiki_id, domain_id, session_id, kind, ordinal, "
                "payload_hash, row_count, byte_count, payload, accepted_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    self.iwiki_id,
                    domain_id,
                    session.session_id,
                    batch.kind,
                    batch.ordinal,
                    batch.payload_hash,
                    batch.row_count,
                    batch.byte_count,
                    batch.payload,
                    now,
                ),
            )
            self._renew(cursor, domain_id, session.session_id, now)
        return {"accepted": True}

    def finalize(self, session: PublicationSession) -> dict[str, object]:
        """Activate one complete snapshot atomically or return an exact code."""
        now = self._clock()
        try:
            with self._transaction() as cursor:
                domain_id = self._domain_id(cursor)
                record = self._load_session(
                    cursor, domain_id, session.session_id
                )
                rejection = self._guard(record, now)
                if rejection is not None:
                    return rejection
                if record["state"] != "staging":
                    terminal = record["terminal_result"]
                    return dict(terminal) if terminal else dict(
                        _SNAPSHOT_CONFLICT
                    )
                rows, failure = self._materialize(
                    cursor, domain_id, session.session_id
                )
                if failure is not None:
                    return failure
                header = self._header(cursor, domain_id, record["snapshot_id"])
                if graph_payload_revision(rows) != header[
                    "graph_payload_revision"
                ]:
                    return dict(_REVISION_MISMATCH)
                pages = self._markdown_pages(cursor, domain_id)
                if pages is None:
                    return dict(_MARKDOWN_UNAVAILABLE)
                revision = markdown_revision(
                    tuple(
                        MarkdownPageSnapshot(slug=slug, markdown=markdown)
                        for _page_id, slug, markdown in pages
                    )
                )
                links = _derive_links(pages, rows, self.domain)
                lock_id, _active = self._domain_state(cursor, domain_id)
                cursor.execute(
                    "SELECT set_config('lock_timeout', %s, true)",
                    (f"{self._lock_timeout_ms}ms",),
                )
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    (_LOCK_NAMESPACE, lock_id),
                )
                if self._active_revision(cursor, domain_id) != record[
                    "base_snapshot_revision"
                ]:
                    return dict(_SNAPSHOT_CONFLICT)
                if self._markdown_generation(cursor, domain_id) != record[
                    "captured_markdown_generation"
                ]:
                    return dict(_SNAPSHOT_CONFLICT)
                return self._activate(
                    cursor,
                    domain_id,
                    session,
                    record,
                    rows,
                    header,
                    now,
                    revision,
                    links,
                )
        except psycopg.errors.LockNotAvailable:
            return dict(_BUSY)
        except psycopg.Error as exc:
            if getattr(exc, "sqlstate", None) == _LOCK_NOT_AVAILABLE:
                return dict(_BUSY)
            raise

    def abort(self, session: PublicationSession) -> dict[str, object]:
        """Discard one owned staging session and its staged snapshot."""
        now = self._clock()
        with self._transaction() as cursor:
            domain_id = self._domain_id(cursor)
            record = self._load_session(cursor, domain_id, session.session_id)
            if record is None or record["owner_id"] != self.owner_id:
                return dict(_UNAUTHORIZED)
            if record["state"] != "staging":
                terminal = record["terminal_result"]
                return dict(terminal) if terminal else {"state": record["state"]}
            cursor.execute(
                "UPDATE iwiki.code_graph_publication_sessions "
                "SET state = 'aborted', terminal_result = %s, updated_at = %s "
                "WHERE iwiki_id = %s AND domain_id = %s AND session_id = %s",
                (
                    Jsonb({"state": "aborted"}),
                    now,
                    self.iwiki_id,
                    domain_id,
                    session.session_id,
                ),
            )
            cursor.execute(
                "DELETE FROM iwiki.code_graph_batches "
                "WHERE iwiki_id = %s AND domain_id = %s AND session_id = %s",
                (self.iwiki_id, domain_id, session.session_id),
            )
            self._fail_snapshot(cursor, domain_id, record["snapshot_id"])
        return {"state": "aborted"}

    def cleanup_staging(self, now: datetime.datetime) -> int:
        """Drop at most the configured number of expired staging sessions."""
        with self._transaction() as cursor:
            domain_id = self._domain_id(cursor)
            return self._cleanup_staging(cursor, domain_id, now)

    def status(self) -> dict[str, object]:
        """Report the active ready snapshot without exposing staged rows."""
        with self._transaction() as cursor:
            domain_id = self._domain_id(cursor)
            generation = self._markdown_generation(cursor, domain_id)
            cursor.execute(
                "SELECT s.snapshot_id, s.snapshot_revision, s.ready_at, "
                "s.counts, s.markdown_revision, s.markdown_generation "
                "FROM iwiki.code_graph_domain_state d "
                "JOIN iwiki.code_graph_snapshots s "
                "ON s.iwiki_id = d.iwiki_id AND s.domain_id = d.domain_id "
                "AND s.snapshot_id = d.active_snapshot_id "
                "WHERE d.iwiki_id = %s AND d.domain_id = %s "
                "AND s.state = 'ready'",
                (self.iwiki_id, domain_id),
            )
            row = cursor.fetchone()
        if row is None:
            return {
                "domain": self.domain,
                "state": "missing",
                "snapshot_revision": None,
            }
        return {
            "domain": self.domain,
            "state": "ready",
            "snapshot_id": row[0],
            "snapshot_revision": row[1],
            "ready_at": row[2].isoformat(),
            "counts": row[3],
            "markdown_revision": row[4],
            "stored_markdown_generation": row[5],
            "current_markdown_generation": generation,
            "wiki_links_stale": row[5] != generation,
        }

    # -- helpers --------------------------------------------------------

    def _cleanup_staging(self, cursor, domain_id: int, now) -> int:
        threshold = now - datetime.timedelta(
            seconds=self._staging_retention_seconds
        )
        cursor.execute(
            "SELECT session_id, snapshot_id "
            "FROM iwiki.code_graph_publication_sessions "
            "WHERE iwiki_id = %s AND domain_id = %s AND state = 'staging' "
            "AND lease_expires_at <= %s "
            "ORDER BY lease_expires_at LIMIT %s",
            (self.iwiki_id, domain_id, threshold, self._staging_cleanup_limit),
        )
        expired = cursor.fetchall()
        for session_id, snapshot_id in expired:
            cursor.execute(
                "DELETE FROM iwiki.code_graph_publication_sessions "
                "WHERE iwiki_id = %s AND domain_id = %s AND session_id = %s",
                (self.iwiki_id, domain_id, session_id),
            )
            self._discard_snapshot(cursor, domain_id, snapshot_id)
        return len(expired)

    def _fail_snapshot(self, cursor, domain_id: int, snapshot_id: str):
        cursor.execute(
            "UPDATE iwiki.code_graph_snapshots SET state = 'failed' "
            "WHERE iwiki_id = %s AND domain_id = %s AND snapshot_id = %s "
            "AND state = 'staging'",
            (self.iwiki_id, domain_id, snapshot_id),
        )

    def _discard_snapshot(self, cursor, domain_id: int, snapshot_id: str):
        cursor.execute(
            "DELETE FROM iwiki.code_graph_snapshots "
            "WHERE iwiki_id = %s AND domain_id = %s AND snapshot_id = %s "
            "AND state = 'staging'",
            (self.iwiki_id, domain_id, snapshot_id),
        )

    def _markdown_pages(self, cursor, domain_id: int):
        """Read authoritative target Markdown, or None when it is unreadable."""
        try:
            cursor.execute(
                "SELECT page_id, slug, markdown FROM iwiki.pages "
                "WHERE iwiki_id = %s AND domain_id = %s ORDER BY slug",
                (self.iwiki_id, domain_id),
            )
        except psycopg.errors.InsufficientPrivilege:
            return None
        return cursor.fetchall()

    def _header(self, cursor, domain_id: int, snapshot_id: str):
        cursor.execute(
            "SELECT header FROM iwiki.code_graph_snapshots "
            "WHERE iwiki_id = %s AND domain_id = %s AND snapshot_id = %s",
            (self.iwiki_id, domain_id, snapshot_id),
        )
        return cursor.fetchone()[0]

    def _materialize(self, cursor, domain_id: int, session_id: str):
        cursor.execute(
            "SELECT kind, ordinal, payload, row_count "
            "FROM iwiki.code_graph_batches "
            "WHERE iwiki_id = %s AND domain_id = %s AND session_id = %s "
            "ORDER BY kind, ordinal",
            (self.iwiki_id, domain_id, session_id),
        )
        collected: dict[str, list] = {kind: [] for kind in _ROW_KINDS}
        ordinals: dict[str, list[int]] = {kind: [] for kind in _ROW_KINDS}
        for kind, ordinal, payload, row_count in cursor.fetchall():
            rows = json.loads(bytes(payload).decode("utf-8"))
            if not isinstance(rows, list) or len(rows) != row_count:
                return None, dict(_INVALID_BATCH)
            collected[kind].extend(rows)
            ordinals[kind].append(ordinal)
        for kind in _ROW_KINDS:
            if ordinals[kind] != list(range(len(ordinals[kind]))):
                return None, dict(_INVALID_BATCH)
        cursor.execute(
            "SELECT counts FROM iwiki.code_graph_snapshots s "
            "JOIN iwiki.code_graph_publication_sessions p "
            "ON p.iwiki_id = s.iwiki_id AND p.domain_id = s.domain_id "
            "AND p.snapshot_id = s.snapshot_id "
            "WHERE p.iwiki_id = %s AND p.domain_id = %s AND p.session_id = %s",
            (self.iwiki_id, domain_id, session_id),
        )
        expected = cursor.fetchone()[0]
        if any(len(collected[kind]) != expected[kind] for kind in _ROW_KINDS):
            return None, dict(_INCOMPLETE)
        failure = _validate_references(collected)
        if failure is not None:
            return None, failure
        return collected, None

    def _activate(
        self,
        cursor,
        domain_id,
        session,
        record,
        rows,
        header,
        now,
        markdown_revision_value,
        links,
    ) -> dict[str, object]:
        snapshot_id = record["snapshot_id"]
        snapshot_revision = canonical_sha256(
            {
                "graph_payload_revision": header["graph_payload_revision"],
                "markdown_revision": markdown_revision_value,
                "repository_id": header["repository_id"],
            },
            prefix=True,
        )
        for row in rows["files"]:
            cursor.execute(
                "INSERT INTO iwiki.code_graph_files "
                "(iwiki_id, domain_id, snapshot_id, file_id, repository_id, "
                "row_data) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    self.iwiki_id,
                    domain_id,
                    snapshot_id,
                    row["file_id"],
                    row["repository_id"],
                    Jsonb(row),
                ),
            )
        for row in rows["symbols"]:
            cursor.execute(
                "INSERT INTO iwiki.code_graph_symbols "
                "(iwiki_id, domain_id, snapshot_id, symbol_id, file_id, "
                "row_data) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    self.iwiki_id,
                    domain_id,
                    snapshot_id,
                    row["symbol_id"],
                    row["file_id"],
                    Jsonb(row),
                ),
            )
        for row in rows["relations"]:
            cursor.execute(
                "INSERT INTO iwiki.code_graph_relations "
                "(iwiki_id, domain_id, snapshot_id, relation_id, "
                "source_file_id, source_symbol_id, target_symbol_id, row_data) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    self.iwiki_id,
                    domain_id,
                    snapshot_id,
                    row["relation_id"],
                    row["source_file_id"],
                    row.get("source_symbol_id"),
                    row.get("target_symbol_id"),
                    Jsonb(row),
                ),
            )
        for link in links:
            cursor.execute(
                "INSERT INTO iwiki.code_graph_wiki_links "
                "(iwiki_id, domain_id, snapshot_id, relation_id, page_id, "
                "selector, provenance) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (
                    self.iwiki_id,
                    domain_id,
                    snapshot_id,
                    link["relation_id"],
                    link["page_id"],
                    Jsonb(link["selector"]),
                    Jsonb(link["provenance"]),
                ),
            )
        cursor.execute(
            "UPDATE iwiki.code_graph_snapshots "
            "SET state = 'ready', snapshot_revision = %s, ready_at = %s, "
            "markdown_revision = %s, "
            "header = header || jsonb_build_object('repositories', %s) "
            "WHERE iwiki_id = %s AND domain_id = %s AND snapshot_id = %s",
            (
                snapshot_revision,
                now,
                markdown_revision_value,
                Jsonb(rows["repositories"]),
                self.iwiki_id,
                domain_id,
                snapshot_id,
            ),
        )
        cursor.execute(
            "UPDATE iwiki.code_graph_domain_state "
            "SET active_snapshot_id = %s, active_snapshot_state = 'ready', "
            "updated_at = %s WHERE iwiki_id = %s AND domain_id = %s",
            (snapshot_id, now, self.iwiki_id, domain_id),
        )
        result = {
            "state": "ready",
            "snapshot_id": snapshot_id,
            "snapshot_revision": snapshot_revision,
            "markdown_revision": markdown_revision_value,
            "counts": {kind: len(rows[kind]) for kind in _ROW_KINDS},
            "wiki_links": len(links),
        }
        cursor.execute(
            "UPDATE iwiki.code_graph_publication_sessions "
            "SET state = 'ready', terminal_result = %s, updated_at = %s "
            "WHERE iwiki_id = %s AND domain_id = %s AND session_id = %s",
            (
                Jsonb(result),
                now,
                self.iwiki_id,
                domain_id,
                session.session_id,
            ),
        )
        return result


def _validate_references(rows) -> dict[str, object] | None:
    files = {row.get("file_id") for row in rows["files"]}
    symbols = {row.get("symbol_id") for row in rows["symbols"]}
    if any(row.get("file_id") not in files for row in rows["symbols"]):
        return dict(_INVALID_BATCH)
    for relation in rows["relations"]:
        if relation.get("source_file_id") not in files:
            return dict(_INVALID_BATCH)
        source = relation.get("source_symbol_id")
        target = relation.get("target_symbol_id")
        if source is not None and source not in symbols:
            return dict(_INVALID_BATCH)
        if target is not None and target not in symbols:
            return dict(_INVALID_BATCH)
    return None


_SNAPSHOT_FILTER = "iwiki_id = %s AND domain_id = %s AND snapshot_id = %s"

_FILES_CTE = f"""files AS (
    SELECT
        row_data->>'file_id' AS file_id,
        row_data->>'repository_id' AS repository_id,
        row_data->>'path' AS path,
        row_data->>'path_casefold' AS path_casefold,
        row_data->>'file_local_name' AS file_local_name,
        row_data->>'file_name_tokens_casefold' AS file_name_tokens_casefold,
        row_data->>'language' AS language,
        row_data->>'content_hash' AS content_hash,
        (row_data->>'size_bytes')::bigint AS size_bytes,
        row_data->>'module_id' AS module_id,
        row_data->>'module_qualified_name' AS module_qualified_name,
        row_data->>'module_local_name' AS module_local_name,
        row_data->>'module_name_tokens_casefold' AS module_name_tokens_casefold,
        (row_data->>'start_line')::bigint AS start_line,
        (row_data->>'end_line')::bigint AS end_line,
        (row_data->>'start_byte')::bigint AS start_byte,
        (row_data->>'end_byte')::bigint AS end_byte
    FROM iwiki.code_graph_files
    WHERE {_SNAPSHOT_FILTER}
)"""

_SYMBOLS_CTE = f"""symbols AS (
    SELECT
        row_data->>'symbol_id' AS symbol_id,
        row_data->>'file_id' AS file_id,
        row_data->>'kind' AS kind,
        row_data->>'qualified_name' AS qualified_name,
        row_data->>'local_name' AS local_name,
        row_data->>'name_tokens_casefold' AS name_tokens_casefold,
        row_data->>'signature' AS signature,
        row_data->>'signature_casefold' AS signature_casefold,
        (row_data->>'start_line')::bigint AS start_line,
        (row_data->>'end_line')::bigint AS end_line,
        (row_data->>'start_byte')::bigint AS start_byte,
        (row_data->>'end_byte')::bigint AS end_byte
    FROM iwiki.code_graph_symbols
    WHERE {_SNAPSHOT_FILTER}
)"""

_RELATIONS_CTE = f"""relations AS (
    SELECT
        row_data->>'relation_id' AS relation_id,
        row_data->>'source_file_id' AS source_file_id,
        row_data->>'source_module_id' AS source_module_id,
        row_data->>'source_symbol_id' AS source_symbol_id,
        row_data->>'target_module_id' AS target_module_id,
        row_data->>'target_symbol_id' AS target_symbol_id,
        row_data->>'target_reference' AS target_reference,
        row_data->>'relation_type' AS relation_type,
        (row_data->>'source_start_line')::bigint AS source_start_line,
        (row_data->>'source_end_line')::bigint AS source_end_line,
        (row_data->>'source_start_byte')::bigint AS source_start_byte,
        (row_data->>'source_end_byte')::bigint AS source_end_byte,
        row_data->>'binding_name' AS binding_name,
        row_data->>'binding_kind' AS binding_kind,
        row_data->>'binding_name_tokens_casefold'
            AS binding_name_tokens_casefold,
        (row_data->>'confidence')::double precision AS confidence,
        row_data->>'resolution_state' AS resolution_state,
        row_data->>'metadata_json' AS metadata_json
    FROM iwiki.code_graph_relations
    WHERE {_SNAPSHOT_FILTER}
)"""

_RELATION_COLUMNS = """
    relation_id, source_file_id, source_module_id, source_symbol_id,
    target_module_id, target_symbol_id, target_reference, relation_type,
    source_start_line, source_end_line, source_start_byte, source_end_byte,
    binding_name, binding_kind, binding_name_tokens_casefold, confidence,
    resolution_state, metadata_json
"""

_FILE_ENTITY_SELECT = """
    f.file_id AS entity_id, 'file' AS entity_type, f.file_id AS file_id,
    NULL::text AS module_id, NULL::text AS symbol_id, 'file' AS kind,
    f.path AS qualified_name, f.file_local_name AS local_name,
    f.file_name_tokens_casefold AS name_tokens_casefold,
    NULL::text AS signature, NULL::text AS signature_casefold,
    f.path AS path, f.path_casefold AS path_casefold,
    f.start_line AS start_line, f.end_line AS end_line,
    f.start_byte AS start_byte, f.end_byte AS end_byte
"""

_MODULE_ENTITY_SELECT = """
    f.module_id AS entity_id, 'module' AS entity_type, f.file_id AS file_id,
    f.module_id AS module_id, NULL::text AS symbol_id, 'module' AS kind,
    f.module_qualified_name AS qualified_name,
    f.module_local_name AS local_name,
    f.module_name_tokens_casefold AS name_tokens_casefold,
    NULL::text AS signature, NULL::text AS signature_casefold,
    f.path AS path, f.path_casefold AS path_casefold,
    f.start_line AS start_line, f.end_line AS end_line,
    f.start_byte AS start_byte, f.end_byte AS end_byte
"""

_SYMBOL_ENTITY_SELECT = """
    s.symbol_id AS entity_id, 'symbol' AS entity_type, f.file_id AS file_id,
    f.module_id AS module_id, s.symbol_id AS symbol_id, s.kind AS kind,
    s.qualified_name AS qualified_name, s.local_name AS local_name,
    s.name_tokens_casefold AS name_tokens_casefold,
    s.signature AS signature, s.signature_casefold AS signature_casefold,
    f.path AS path, f.path_casefold AS path_casefold,
    s.start_line AS start_line, s.end_line AS end_line,
    s.start_byte AS start_byte, s.end_byte AS end_byte
"""

_EMPTY_ENTITY_SELECT = """
    NULL::text AS entity_id, NULL::text AS entity_type,
    NULL::text AS file_id, NULL::text AS module_id, NULL::text AS symbol_id,
    NULL::text AS kind, NULL::text AS qualified_name,
    NULL::text AS local_name, NULL::text AS name_tokens_casefold,
    NULL::text AS signature, NULL::text AS signature_casefold,
    NULL::text AS path, NULL::text AS path_casefold,
    NULL::bigint AS start_line, NULL::bigint AS end_line,
    NULL::bigint AS start_byte, NULL::bigint AS end_byte
"""

_ENTITY_OUTPUT_COLUMNS = (
    "entity_id, entity_type, file_id, module_id, symbol_id, kind, "
    "qualified_name, local_name, name_tokens_casefold, signature, "
    "signature_casefold, path, path_casefold, start_line, end_line, "
    "start_byte, end_byte"
)

_ENTITY_ORDER = 'qualified_name COLLATE "C", entity_id COLLATE "C"'


def _lexical(column: str, tokens: tuple[str, ...]) -> tuple[str, list[Any]]:
    if not tokens:
        return "false", []
    return (
        " AND ".join(f"strpos({column}, %s) > 0" for _token in tokens),
        [f"\x1f{token}\x1f" for token in tokens],
    )


def _path_filter(
    request: ValidatedSearchRequest, column: str
) -> tuple[str, list[Any]]:
    if request.path is None:
        return "", []
    return f"AND starts_with({column}, %s)", [request.path]


def _excluded_filter(
    excluded_ids: tuple[str, ...], column: str
) -> tuple[str, list[Any]]:
    if not excluded_ids:
        return "", []
    return f"AND {column} <> ALL(%s)", [list(excluded_ids)]


def _canonical_predicate(
    request: ValidatedSearchRequest,
    name: str,
    *,
    qualified: str,
    local: str,
    tokens: str,
    signature: str,
    raw_signature: str,
    path: str,
    raw_path: str,
) -> tuple[str, list[Any]]:
    prefix = f"starts_with({qualified}, %s) OR starts_with({local}, %s)"
    exact = f"{qualified} = %s OR {local} = %s"
    tail = [request.query] * 4
    if name == "qualified_exact":
        return f"{qualified} = %s", [request.query]
    if name == "local_exact":
        return f"{local} = %s AND {qualified} <> %s", [request.query] * 2
    if name == "canonical_lexical":
        lexical, parameters = _lexical(tokens, request.tokens)
        return (
            f"({lexical}) AND NOT ({prefix}) AND NOT ({exact})",
            [*parameters, *tail],
        )
    if name == "signature":
        return (
            f"{raw_signature} IS NOT NULL AND "
            f"strpos(COALESCE({signature}, lower({raw_signature})), %s) > 0 "
            f"AND NOT ({prefix}) AND NOT ({exact})",
            [request.query.casefold(), *tail],
        )
    if name == "path":
        return (
            f"strpos(COALESCE({path}, lower({raw_path})), %s) > 0 "
            f"AND NOT ({prefix}) AND NOT ({exact})",
            [request.query.casefold(), *tail],
        )
    raise AssertionError(f"unsupported canonical rank: {name}")


def _prefix_predicates(
    request: ValidatedSearchRequest, *, qualified: str, local: str
) -> tuple[tuple[str, list[Any]], tuple[str, list[Any]]]:
    return (
        (
            f"starts_with({qualified}, %s) AND {qualified} <> %s "
            f"AND {local} <> %s",
            [request.query] * 3,
        ),
        (
            f"starts_with({local}, %s) AND {local} <> %s "
            f"AND {qualified} <> %s AND NOT starts_with({qualified}, %s)",
            [request.query] * 4,
        ),
    )


def _alias_predicate(
    request: ValidatedSearchRequest, name: str
) -> tuple[str, list[Any]]:
    if name == "alias_exact":
        return "r.binding_name = %s", [request.query]
    if name == "alias_prefix":
        return (
            "starts_with(r.binding_name, %s) AND r.binding_name <> %s",
            [request.query] * 2,
        )
    lexical, parameters = _lexical(
        "r.binding_name_tokens_casefold", request.tokens
    )
    return (
        f"({lexical}) AND r.binding_name <> %s "
        "AND NOT starts_with(r.binding_name, %s)",
        [*parameters, request.query, request.query],
    )


def _entity_specs(
    request: ValidatedSearchRequest, name: str, repository_id: str
) -> list[dict[str, Any]]:
    symbol_kinds = [
        kind for kind in request.kinds if kind not in {"file", "module"}
    ]
    specs: list[dict[str, Any]] = []
    if "file" in request.kinds and name != "signature":
        specs.append({
            "select": _FILE_ENTITY_SELECT,
            "from": "files AS f",
            "common": "f.repository_id = %s AND f.language = %s",
            "common_parameters": [repository_id, request.language],
            "qualified": "f.path",
            "local": "f.file_local_name",
            "tokens": "f.file_name_tokens_casefold",
            "signature": "NULL::text",
            "raw_signature": "NULL::text",
            "path": "f.path_casefold",
            "raw_path": "f.path",
            "entity_id": "f.file_id",
        })
    if "module" in request.kinds and name != "signature":
        specs.append({
            "select": _MODULE_ENTITY_SELECT,
            "from": "files AS f",
            "common": (
                "f.repository_id = %s AND f.language = %s "
                "AND f.module_id IS NOT NULL"
            ),
            "common_parameters": [repository_id, request.language],
            "qualified": "f.module_qualified_name",
            "local": "f.module_local_name",
            "tokens": "f.module_name_tokens_casefold",
            "signature": "NULL::text",
            "raw_signature": "NULL::text",
            "path": "f.path_casefold",
            "raw_path": "f.path",
            "entity_id": "f.module_id",
        })
    if symbol_kinds:
        specs.append({
            "select": _SYMBOL_ENTITY_SELECT,
            "from": "symbols AS s JOIN files AS f ON f.file_id = s.file_id",
            "common": (
                "f.repository_id = %s AND f.language = %s "
                "AND s.kind = ANY(%s)"
            ),
            "common_parameters": [repository_id, request.language, symbol_kinds],
            "qualified": "s.qualified_name",
            "local": "s.local_name",
            "tokens": "s.name_tokens_casefold",
            "signature": "s.signature_casefold",
            "raw_signature": "s.signature",
            "path": "f.path_casefold",
            "raw_path": "f.path",
            "entity_id": "s.symbol_id",
        })
    return specs


def _canonical_rank_query(
    repository_id: str,
    request: ValidatedSearchRequest,
    name: str,
    excluded_ids: tuple[str, ...],
    scope: tuple[str, int, str],
) -> tuple[str, list[Any]]:
    branches: list[str] = []
    parameters: list[Any] = [*scope, *scope]
    for spec in _entity_specs(request, name, repository_id):
        path_sql, path_parameters = _path_filter(request, "f.path")
        excluded_sql, excluded_parameters = _excluded_filter(
            excluded_ids, str(spec["entity_id"])
        )
        if name == "canonical_prefix":
            predicates = _prefix_predicates(
                request,
                qualified=str(spec["qualified"]),
                local=str(spec["local"]),
            )
        else:
            predicates = (_canonical_predicate(
                request,
                name,
                qualified=str(spec["qualified"]),
                local=str(spec["local"]),
                tokens=str(spec["tokens"]),
                signature=str(spec["signature"]),
                raw_signature=str(spec["raw_signature"]),
                path=str(spec["path"]),
                raw_path=str(spec["raw_path"]),
            ),)
        for predicate, predicate_parameters in predicates:
            branches.append(
                f"SELECT {spec['select']}\n"
                f"FROM {spec['from']}\n"
                f"WHERE {spec['common']} {path_sql}\n"
                f"  AND {predicate} {excluded_sql}"
            )
            parameters.extend(spec["common_parameters"])
            parameters.extend(path_parameters)
            parameters.extend(predicate_parameters)
            parameters.extend(excluded_parameters)
    if not branches:
        branches.append(f"SELECT {_EMPTY_ENTITY_SELECT} WHERE false")
    branch_sql = "\n    UNION ALL\n    ".join(branches)
    sql = f"""WITH {_FILES_CTE}, {_SYMBOLS_CTE}
SELECT candidates.*, {MATCH_RANK[name]} AS match_rank,
       NULL::text AS matched_alias, 0 AS alias_target_count
FROM (
    {branch_sql}
) AS candidates
ORDER BY {_ENTITY_ORDER} LIMIT %s"""
    return sql, parameters


def _alias_rank_query(
    repository_id: str,
    request: ValidatedSearchRequest,
    name: str,
    excluded_ids: tuple[str, ...],
    scope: tuple[str, int, str],
) -> tuple[str, list[Any]]:
    symbol_kinds = [
        kind for kind in request.kinds if kind not in {"file", "module"}
    ]
    predicate, predicate_parameters = _alias_predicate(request, name)
    alias_columns = (
        "r.binding_name AS binding_name, r.binding_name_tokens_casefold "
        "AS binding_name_tokens_casefold,"
    )
    alias_common = (
        "r.relation_type = 'IMPORTS' AND r.binding_kind = 'explicit_alias' "
        "AND r.resolution_state IN "
        "('resolved', 'ambiguous', 'partially_resolved')"
    )
    branches: list[str] = []
    parameters: list[Any] = [*scope, *scope, *scope]
    if "module" in request.kinds:
        path_sql, path_parameters = _path_filter(request, "f.path")
        excluded_sql, excluded_parameters = _excluded_filter(
            excluded_ids, "f.module_id"
        )
        branches.append(f"""SELECT DISTINCT {alias_columns}
{_MODULE_ENTITY_SELECT}
FROM relations AS r JOIN files AS f ON f.module_id = r.target_module_id
WHERE {alias_common}
  AND f.repository_id = %s AND f.language = %s {path_sql}
  AND {predicate} {excluded_sql}""")
        parameters.extend([repository_id, request.language, *path_parameters])
        parameters.extend(predicate_parameters)
        parameters.extend(excluded_parameters)
    if symbol_kinds:
        path_sql, path_parameters = _path_filter(request, "f.path")
        excluded_sql, excluded_parameters = _excluded_filter(
            excluded_ids, "s.symbol_id"
        )
        branches.append(f"""SELECT DISTINCT {alias_columns}
{_SYMBOL_ENTITY_SELECT}
FROM relations AS r JOIN symbols AS s ON s.symbol_id = r.target_symbol_id
JOIN files AS f ON f.file_id = s.file_id
WHERE {alias_common}
  AND f.repository_id = %s AND f.language = %s
  AND s.kind = ANY(%s) {path_sql}
  AND {predicate} {excluded_sql}""")
        parameters.extend([
            repository_id,
            request.language,
            symbol_kinds,
            *path_parameters,
        ])
        parameters.extend(predicate_parameters)
        parameters.extend(excluded_parameters)
    if not branches:
        branches.append(
            "SELECT NULL::text AS binding_name, "
            "NULL::text AS binding_name_tokens_casefold, "
            f"{_EMPTY_ENTITY_SELECT} WHERE false"
        )
    branch_sql = "\n    UNION ALL\n    ".join(branches)
    sql = f"""WITH {_FILES_CTE}, {_SYMBOLS_CTE}, {_RELATIONS_CTE},
alias_targets AS (
    {branch_sql}
), alias_counts AS (
    SELECT binding_name, COUNT(DISTINCT entity_id) AS target_count
    FROM alias_targets GROUP BY binding_name
), ranked AS (
    SELECT a.*, c.target_count, ROW_NUMBER() OVER (
        PARTITION BY a.entity_id ORDER BY a.binding_name COLLATE "C"
    ) AS row_number
    FROM alias_targets AS a JOIN alias_counts AS c USING (binding_name)
)
SELECT {_ENTITY_OUTPUT_COLUMNS}, {MATCH_RANK[name]} AS match_rank,
       binding_name AS matched_alias, target_count AS alias_target_count
FROM ranked WHERE row_number = 1
ORDER BY {_ENTITY_ORDER}, binding_name COLLATE "C" LIMIT %s"""
    return sql, parameters


def _rank_query(
    repository_id: str,
    request: ValidatedSearchRequest,
    name: str,
    excluded_ids: tuple[str, ...],
    scope: tuple[str, int, str],
) -> tuple[str, list[Any]]:
    if name.startswith("alias_"):
        return _alias_rank_query(
            repository_id, request, name, excluded_ids, scope
        )
    return _canonical_rank_query(
        repository_id, request, name, excluded_ids, scope
    )


def _context_node_from_row(row: tuple[Any, ...]) -> ContextNode:
    return ContextNode(
        entity_id=str(row[0]),
        entity_type=str(row[1]),
        file_id=None if row[2] is None else str(row[2]),
        module_id=None if row[3] is None else str(row[3]),
        symbol_id=None if row[4] is None else str(row[4]),
        kind=str(row[5]),
        qualified_name=str(row[6]),
        local_name=str(row[7]),
        signature=None if row[9] is None else str(row[9]),
        path=str(row[11]),
        start_line=int(row[13]),
        end_line=int(row[14]),
        start_byte=int(row[15]),
        end_byte=int(row[16]),
    )


_SELECTOR_SPECIFICITY = {"source_glob": 0, "file": 1, "symbol": 2}


class PostgresCodeGraphReader:
    """Answer bounded reads from one active PostgreSQL graph snapshot."""

    def __init__(
        self,
        dsn: str,
        iwiki_id: str,
        domain: str,
        *,
        max_snapshot_age_seconds: int,
        connection_factory: Callable[[], psycopg.Connection] | None = None,
        clock: Callable[[], datetime.datetime] | None = None,
    ) -> None:
        self._dsn = dsn
        self.iwiki_id = iwiki_id
        self.domain = domain
        self._max_snapshot_age_seconds = max_snapshot_age_seconds
        self._connection_factory = connection_factory or (
            lambda: psycopg.connect(dsn)
        )
        self._clock = clock or (
            lambda: datetime.datetime.now(datetime.timezone.utc)
        )

    # -- infrastructure -------------------------------------------------

    @contextmanager
    def _read(self) -> Iterator[psycopg.Cursor]:
        connection = self._connection_factory()
        try:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    yield cursor
        finally:
            connection.close()

    def _domain_id(self, cursor) -> int:
        cursor.execute(
            "SELECT domain_id FROM iwiki.domains "
            "WHERE iwiki_id = %s AND slug = %s",
            (self.iwiki_id, self.domain),
        )
        row = cursor.fetchone()
        if row is None:
            raise ValueError("scope_mismatch")
        return row[0]

    def _status(self, cursor) -> tuple[dict[str, object], tuple | None]:
        domain_id = self._domain_id(cursor)
        cursor.execute(
            "SELECT s.snapshot_id, s.snapshot_revision, "
            "s.graph_payload_revision, s.ready_at, s.counts, "
            "s.markdown_revision, s.markdown_generation, d.markdown_generation "
            "FROM iwiki.code_graph_domain_state st "
            "JOIN iwiki.code_graph_snapshots s "
            "ON s.iwiki_id = st.iwiki_id AND s.domain_id = st.domain_id "
            "AND s.snapshot_id = st.active_snapshot_id AND s.state = 'ready' "
            "JOIN iwiki.domains d "
            "ON d.iwiki_id = st.iwiki_id AND d.domain_id = st.domain_id "
            "WHERE st.iwiki_id = %s AND st.domain_id = %s",
            (self.iwiki_id, domain_id),
        )
        row = cursor.fetchone()
        if row is None:
            return {"domain": self.domain, **MISSING_READ_RESULT}, None
        age = int((self._clock() - row[3]).total_seconds())
        limit = self._max_snapshot_age_seconds
        fresh = limit == 0 or age <= limit
        status: dict[str, object] = {
            "domain": self.domain,
            "state": "ready",
            "fresh": fresh,
            "revision": row[1],
            "snapshot_revision": row[1],
            "graph_payload_revision": row[2],
            "markdown_revision": row[5],
            "stored_markdown_revision": row[5],
            "stored_markdown_generation": row[6],
            "current_markdown_generation": row[7],
            "wiki_links_stale": row[6] != row[7],
            "counts": row[4],
            "age_seconds": age,
            "max_snapshot_age_seconds": limit,
            "warnings": [],
        }
        if not fresh:
            status["error"] = "stale_snapshot"
            return status, None
        return status, (self.iwiki_id, domain_id, row[0])

    # -- public reads ---------------------------------------------------

    def status(self) -> dict[str, object]:
        """Report snapshot readiness, freshness, and stored Markdown binding."""
        try:
            with self._read() as cursor:
                return self._status(cursor)[0]
        except (ValueError, psycopg.Error):
            return {"domain": self.domain, **MISSING_READ_RESULT}

    def search(self, request: ValidatedSearchRequest) -> dict[str, object]:
        """Rank entities of one fresh snapshot without reading project source."""
        try:
            with self._read() as cursor:
                status, scope = self._status(cursor)
                if scope is None:
                    return {**status, "results": []}
                results = self._search(cursor, scope, request)
        except (ValueError, psycopg.Error):
            return {"domain": self.domain, **MISSING_READ_RESULT, "results": []}
        return {**status, "results": [asdict(item) for item in results]}

    def context(self, request: ContextRequest) -> dict[str, object]:
        """Traverse bounded typed context without exposing any file source."""
        try:
            with self._read() as cursor:
                status, scope = self._status(cursor)
                if scope is None:
                    return {
                        **status,
                        "seeds": list(request.seeds),
                        **EMPTY_CONTEXT_RESULT,
                    }
                effective = request
                stale = bool(status["wiki_links_stale"])
                if stale and request.include_wiki:
                    effective = replace(request, include_wiki=False)
                response = self._context(cursor, scope, effective)
                if stale and request.include_wiki:
                    response["warnings"].append("wiki_links_stale")
        except (ValueError, psycopg.Error):
            return {
                "domain": self.domain,
                **MISSING_READ_RESULT,
                "seeds": list(request.seeds),
                **EMPTY_CONTEXT_RESULT,
            }
        return {**status, **response}

    # -- search ---------------------------------------------------------

    def _search(
        self,
        cursor,
        scope: tuple,
        request: ValidatedSearchRequest,
    ) -> tuple[SearchResult, ...]:
        results: dict[str, SearchResult] = {}
        winner_keys: dict[str, tuple[int, str, str, str]] = {}
        for name in MATCH_RANK:
            remaining = request.limit - len(results)
            if remaining == 0:
                break
            sql, parameters = _rank_query(
                self.domain, request, name, tuple(results), scope
            )
            cursor.execute(sql, [*parameters, remaining])
            for row in cursor.fetchall():
                item = search_result_from_row(row)
                winner_key = (
                    MATCH_RANK[item.match],
                    item.qualified_name,
                    item.entity_id,
                    item.matched_alias or "",
                )
                if winner_key < winner_keys.get(
                    item.entity_id, (10, "", "", "")
                ):
                    results[item.entity_id] = item
                    winner_keys[item.entity_id] = winner_key
        return tuple(sorted(results.values(), key=result_key)[:request.limit])

    # -- context --------------------------------------------------------

    def _nodes(
        self, cursor, scope: tuple, entity_ids: tuple[str, ...]
    ) -> dict[str, ContextNode]:
        if not entity_ids:
            return {}
        identifiers = list(entity_ids)
        cursor.execute(
            f"""WITH {_FILES_CTE}, {_SYMBOLS_CTE}
SELECT {_FILE_ENTITY_SELECT}
FROM files AS f WHERE f.repository_id = %s AND f.file_id = ANY(%s)
UNION ALL
SELECT {_MODULE_ENTITY_SELECT}
FROM files AS f WHERE f.repository_id = %s AND f.module_id = ANY(%s)
UNION ALL
SELECT {_SYMBOL_ENTITY_SELECT}
FROM symbols AS s JOIN files AS f ON f.file_id = s.file_id
WHERE f.repository_id = %s AND s.symbol_id = ANY(%s)""",
            [
                *scope,
                *scope,
                self.domain,
                identifiers,
                self.domain,
                identifiers,
                self.domain,
                identifiers,
            ],
        )
        return {
            str(row[0]): _context_node_from_row(row)
            for row in cursor.fetchall()
        }

    def _frontier_relations(
        self,
        cursor,
        scope: tuple,
        frontier: tuple[ContextNode, ...],
        request: ContextRequest,
    ) -> tuple[ContextRelation, ...]:
        candidates: dict[str, ContextRelation] = {}
        relation_filter = set(request.relations or KNOWN_RELATIONS)
        for node in frontier:
            allowed = (
                {"DECLARES", "IMPORTS"}
                if node.entity_type == "module"
                else set(KNOWN_RELATIONS)
            ) & relation_filter
            if not allowed:
                continue
            arms: list[str] = []
            parameters: list[Any] = [*scope, sorted(allowed)]
            if request.direction in ("out", "both"):
                if node.entity_type == "file":
                    arms.append(
                        "(source_file_id = %s AND source_module_id IS NULL "
                        "AND source_symbol_id IS NULL)"
                    )
                elif node.entity_type == "module":
                    arms.append("source_module_id = %s")
                else:
                    arms.append("source_symbol_id = %s")
                parameters.append(node.entity_id)
            if request.direction in ("in", "both"):
                if node.entity_type == "module":
                    arms.append("target_module_id = %s")
                    parameters.append(node.entity_id)
                elif node.entity_type == "symbol":
                    arms.append("target_symbol_id = %s")
                    parameters.append(node.entity_id)
            if not arms:
                continue
            cursor.execute(
                f"WITH {_RELATIONS_CTE} SELECT {_RELATION_COLUMNS} "
                "FROM relations WHERE relation_type = ANY(%s) "
                f"AND ({' OR '.join(arms)})",
                parameters,
            )
            for row in cursor.fetchall():
                item = relation_from_row(row)
                candidates[item.relation_id] = item
        return tuple(sorted(candidates.values(), key=lambda item: (
            item.relation_type,
            item.source_entity_id,
            item.target_entity_id or item.target_reference or "",
            item.source_start_byte,
            item.relation_id,
        )))

    def _file_row(
        self, cursor, scope: tuple, file_id: str
    ) -> dict[str, object] | None:
        cursor.execute(
            f"""WITH {_FILES_CTE}
SELECT file_id, path, language, content_hash, size_bytes, start_line,
       end_line, start_byte, end_byte
FROM files WHERE repository_id = %s AND file_id = %s""",
            [*scope, self.domain, file_id],
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "file_id": str(row[0]),
            "path": str(row[1]),
            "language": str(row[2]),
            "content_hash": str(row[3]),
            "size_bytes": int(row[4]),
            "start_line": int(row[5]),
            "end_line": int(row[6]),
            "start_byte": int(row[7]),
            "end_byte": int(row[8]),
        }

    def _module_id(self, cursor, scope: tuple, file_id: str) -> str | None:
        cursor.execute(
            f"WITH {_FILES_CTE} SELECT module_id FROM files "
            "WHERE repository_id = %s AND file_id = %s",
            [*scope, self.domain, file_id],
        )
        row = cursor.fetchone()
        return None if row is None or row[0] is None else str(row[0])

    def _wiki_pages(
        self, cursor, scope: tuple, relation_ids: tuple[str, ...]
    ) -> list[dict[str, object]]:
        cursor.execute(
            "SELECT selector, provenance FROM iwiki.code_graph_wiki_links "
            f"WHERE {_SNAPSHOT_FILTER} AND relation_id = ANY(%s) "
            "ORDER BY relation_id",
            [*scope, list(relation_ids)],
        )
        selected: dict[str, dict[str, object]] = {}
        for selector, provenance in cursor.fetchall():
            item = {
                "domain": self.domain,
                "page_id": str(provenance["page"]),
                "relation_type": str(provenance["relation_type"]),
                "selector_kind": str(selector["kind"]),
                "source": str(selector["source"]),
            }
            current = selected.get(str(item["page_id"]))
            if current is None or _SELECTOR_SPECIFICITY[
                str(item["selector_kind"])
            ] > _SELECTOR_SPECIFICITY[str(current["selector_kind"])]:
                selected[str(item["page_id"])] = item
        return [selected[key] for key in sorted(selected)]

    def _context(
        self, cursor, scope: tuple, request: ContextRequest
    ) -> dict[str, object]:
        seed_nodes = self._nodes(cursor, scope, request.seeds)
        warnings: list[str] = []
        if len(seed_nodes) != len(set(request.seeds)):
            warnings.append("unknown_seed")
        accepted: dict[str, ContextNode] = {}
        accepted_files: dict[str, dict[str, object]] = {}
        truncated = False

        def admit(node: ContextNode) -> bool:
            nonlocal truncated
            if node.entity_id in accepted:
                return True
            if len(accepted) >= request.max_nodes:
                warnings.append("max_nodes_exhausted")
                truncated = True
                return False
            assert node.file_id is not None
            if node.file_id not in accepted_files:
                if len(accepted_files) >= request.max_files:
                    warnings.append("max_files_exhausted")
                    truncated = True
                    return False
                row = self._file_row(cursor, scope, node.file_id)
                if row is None:
                    return False
                accepted_files[node.file_id] = row
            accepted[node.entity_id] = node
            return True

        initial: list[ContextNode] = []
        for seed in request.seeds:
            node = seed_nodes.get(seed)
            if node is None or not admit(node):
                continue
            initial.append(node)
            if node.entity_type == "file":
                module_id = self._module_id(cursor, scope, node.entity_id)
                if module_id is not None:
                    neighbor = self._nodes(
                        cursor, scope, (module_id,)
                    ).get(module_id)
                    if neighbor is not None and admit(neighbor):
                        initial.append(neighbor)

        frontier = tuple(initial)
        accepted_relations: dict[str, ContextRelation] = {}
        for _level in range(0 if truncated else request.depth):
            next_frontier: list[ContextNode] = []
            frontier_ids = {node.entity_id for node in frontier}
            for relation in self._frontier_relations(
                cursor, scope, frontier, request
            ):
                neighbor_id = None
                if relation.source_entity_id in frontier_ids:
                    neighbor_id = relation.target_entity_id
                elif relation.target_entity_id in frontier_ids:
                    neighbor_id = relation.source_entity_id
                if neighbor_id is not None and neighbor_id not in accepted:
                    neighbor = self._nodes(
                        cursor, scope, (neighbor_id,)
                    ).get(neighbor_id)
                    if neighbor is None or not admit(neighbor):
                        if truncated:
                            break
                        continue
                    next_frontier.append(neighbor)
                accepted_relations[relation.relation_id] = relation
            frontier = tuple(next_frontier)
            if truncated or not frontier:
                break

        if request.include_source:
            warnings.append("source_unavailable")

        wiki_pages: list[dict[str, object]] = []
        if request.include_wiki and accepted_relations:
            wiki_pages = self._wiki_pages(
                cursor, scope, tuple(sorted(accepted_relations))
            )

        return {
            "seeds": list(request.seeds),
            "nodes": [asdict(node) for node in accepted.values()],
            "relations": [asdict(item) for item in accepted_relations.values()],
            "files": list(accepted_files.values()),
            "wiki_pages": wiki_pages,
            "source_unavailable": request.include_source,
            "limits": {
                "depth": request.depth,
                "max_nodes": request.max_nodes,
                "max_files": request.max_files,
                "max_source_bytes": request.max_source_bytes,
            },
            "truncated": truncated,
            "warnings": list(dict.fromkeys(warnings)),
        }


def _derive_links(pages, rows, domain: str) -> list[dict[str, object]]:
    """Resolve target page selectors against the staged graph snapshot."""
    snapshot = {"files": rows["files"], "symbols": rows["symbols"]}
    relations_by_symbol: dict[str, list[str]] = {}
    relations_by_file: dict[str, list[str]] = {}
    for relation in rows["relations"]:
        relation_id = str(relation["relation_id"])
        symbol = relation.get("source_symbol_id")
        if symbol is not None:
            relations_by_symbol.setdefault(str(symbol), []).append(relation_id)
        relations_by_file.setdefault(
            str(relation["source_file_id"]), []
        ).append(relation_id)
    derived: list[dict[str, object]] = []
    for page_id, slug, markdown in pages:
        for link in resolve_selectors(
            markdown, snapshot, domain=domain, page_id=slug
        ):
            symbol_id = link.get("symbol_id")
            file_id = link.get("file_id")
            candidates = (
                relations_by_symbol.get(str(symbol_id), ())
                if symbol_id is not None
                else relations_by_file.get(str(file_id), ())
            )
            for relation_id in candidates:
                derived.append(
                    {
                        "relation_id": relation_id,
                        "page_id": page_id,
                        "selector": {
                            "kind": link["selector_kind"],
                            "source": link["source"],
                        },
                        "provenance": {
                            "page": slug,
                            "link_id": link["link_id"],
                            "relation_type": link["relation_type"],
                            "confidence": link["confidence"],
                        },
                    }
                )
    return derived
