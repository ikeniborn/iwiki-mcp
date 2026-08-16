"""PostgreSQL code-graph publication sessions and atomic snapshot activation."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
import datetime
import json
import secrets
from typing import Iterator

import psycopg
from psycopg.types.json import Jsonb

from ..codegraph.canonical import canonical_sha256
from ..codegraph.linking import (
    MarkdownPageSnapshot,
    markdown_revision,
    resolve_selectors,
)
from ..codegraph.publication import (
    PublicationSession,
    SnapshotBatch,
    SnapshotHeader,
    graph_payload_revision,
)
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
                    Jsonb(_header_json(header)),
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


def _header_json(header: SnapshotHeader) -> dict[str, object]:
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
