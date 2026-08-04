"""Versioned SQLite storage for the local wiki graph cache."""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Callable, Iterable, Iterator, Literal

from iwiki_mcp.base import ensure_graph_store_excluded
from iwiki_mcp.engine.links import parse_heading_anchors, parse_link_targets
from iwiki_mcp.engine.okf_artifacts import RESERVED_OKF


SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000
_WAL_RETRY_DEADLINE_SECONDS = 0.25
_WAL_RETRY_INTERVAL_SECONDS = 0.01


class GraphStoreError(RuntimeError):
    """Raised when the local graph store cannot be opened safely."""


class GraphSchemaError(GraphStoreError):
    """Raised when the graph schema cannot be used by this version."""


class GraphDomainUnavailable(GraphStoreError):
    """Raised when a domain graph has no ready snapshot."""

    def __init__(self, domain: str, state: str) -> None:
        super().__init__("domain graph is unavailable")
        self.domain = domain
        self.state = state


@dataclass(frozen=True)
class PageRecord:
    page_id: str
    domain: str
    file: str
    content_hash: str
    link_hash: str


@dataclass(frozen=True)
class AnchorRecord:
    page_id: str
    anchor: str
    heading: str


@dataclass(frozen=True)
class EdgeRecord:
    source_page_id: str
    target_page_id: str
    target_anchor: str
    kind: Literal["intra", "cross"]
    raw_target: str


@dataclass(frozen=True)
class DomainSnapshot:
    domain: str
    pages: tuple[PageRecord, ...]
    anchors: tuple[AnchorRecord, ...]
    edges: tuple[EdgeRecord, ...]


def _page_snapshot(domain: str, file: str, content: str) -> tuple[
    PageRecord, tuple[AnchorRecord, ...], tuple[EdgeRecord, ...]
]:
    page_id = f"{domain}/{file[:-3]}"
    anchors = tuple(
        AnchorRecord(page_id, anchor.anchor, anchor.heading)
        for anchor in parse_heading_anchors(content)
    )
    edges = tuple(
        sorted(
            (
                EdgeRecord(
                    page_id,
                    f"{target.target_domain}/{target.target_page}",
                    target.target_anchor,
                    target.kind,
                    target.raw_target,
                )
                for target in parse_link_targets(content, domain)
                if not target.is_reserved
            ),
            key=lambda edge: (
                edge.target_page_id,
                edge.target_anchor,
                edge.raw_target,
            ),
        )
    )
    normalized_links = [
        (
            edge.target_page_id,
            edge.target_anchor,
            edge.kind,
        )
        for edge in edges
    ]
    link_hash = sha256(
        json.dumps(normalized_links, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    page = PageRecord(
        page_id=page_id,
        domain=domain,
        file=file,
        content_hash=sha256(content.encode("utf-8")).hexdigest(),
        link_hash=link_hash,
    )
    return page, anchors, edges


def build_domain_snapshot(domain: str, domain_dir: str | Path) -> DomainSnapshot:
    """Parse one domain into a deterministic, embedding-free graph snapshot."""
    root = Path(domain_dir)
    if not root.is_dir():
        raise OSError("domain directory is unavailable")
    pages: list[PageRecord] = []
    anchors: list[AnchorRecord] = []
    edges: list[EdgeRecord] = []
    files = sorted(
        path
        for path in root.rglob("*.md")
        if path.is_file() and path.relative_to(root).as_posix() not in RESERVED_OKF
    )
    if not root.is_dir():
        raise OSError("domain directory is unavailable")
    for path in files:
        file = path.relative_to(root).as_posix()
        content = path.read_bytes().decode("utf-8")
        page, page_anchors, page_edges = _page_snapshot(domain, file, content)
        pages.append(page)
        anchors.extend(page_anchors)
        edges.extend(page_edges)
    if not root.is_dir():
        raise OSError("domain directory is unavailable")
    return DomainSnapshot(domain, tuple(pages), tuple(anchors), tuple(edges))


_SCHEMA_V1 = """
CREATE TABLE domains (
    domain TEXT PRIMARY KEY,
    indexed_commit TEXT,
    markdown_fingerprint TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('ready', 'dirty', 'rebuilding')),
    indexed_at TEXT NOT NULL
);

CREATE TABLE pages (
    page_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    file TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    link_hash TEXT NOT NULL,
    UNIQUE(domain, file)
);

CREATE TABLE anchors (
    page_id TEXT NOT NULL,
    anchor TEXT NOT NULL,
    heading TEXT NOT NULL,
    PRIMARY KEY(page_id, anchor),
    FOREIGN KEY(page_id) REFERENCES pages(page_id) ON DELETE CASCADE
);

CREATE TABLE edges (
    source_page_id TEXT NOT NULL,
    target_page_id TEXT NOT NULL,
    target_anchor TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL CHECK (kind IN ('intra', 'cross')),
    raw_target TEXT NOT NULL,
    PRIMARY KEY(source_page_id, target_page_id, target_anchor),
    FOREIGN KEY(source_page_id) REFERENCES pages(page_id) ON DELETE CASCADE
);

CREATE INDEX edges_target_idx ON edges(target_page_id);
"""

_SCHEMA_V1_STATEMENTS = tuple(
    statement.strip()
    for statement in _SCHEMA_V1.split(";")
    if statement.strip()
)


def _normalize_ddl(sql: str) -> str:
    """Normalize SQL case and layout while preserving string literal values."""
    normalized: list[str] = []
    in_literal = False
    i = 0
    while i < len(sql):
        character = sql[i]
        if character == "'":
            normalized.append(character)
            if in_literal and i + 1 < len(sql) and sql[i + 1] == "'":
                normalized.append("'")
                i += 2
                continue
            in_literal = not in_literal
        elif character.isspace() and not in_literal:
            i += 1
            continue
        elif in_literal:
            normalized.append(character)
        else:
            normalized.append(character.casefold())
        i += 1
    return "".join(normalized)


_EXPECTED_TABLE_DDL = {
    name: _normalize_ddl(statement)
    for name, statement in zip(
        ("domains", "pages", "anchors", "edges"),
        _SCHEMA_V1_STATEMENTS[:4],
    )
}

# Migration keys are the source version. Version 0 is SQLite's empty database.
_MIGRATIONS: dict[int, tuple[int, tuple[str, ...]]] = {
    0: (1, _SCHEMA_V1_STATEMENTS),
}

_EXPECTED_COLUMNS = {
    "domains": (
        ("domain", "TEXT", 0, None, 1),
        ("indexed_commit", "TEXT", 0, None, 0),
        ("markdown_fingerprint", "TEXT", 1, None, 0),
        ("state", "TEXT", 1, None, 0),
        ("indexed_at", "TEXT", 1, None, 0),
    ),
    "pages": (
        ("page_id", "TEXT", 0, None, 1),
        ("domain", "TEXT", 1, None, 0),
        ("file", "TEXT", 1, None, 0),
        ("content_hash", "TEXT", 1, None, 0),
        ("link_hash", "TEXT", 1, None, 0),
    ),
    "anchors": (
        ("page_id", "TEXT", 1, None, 1),
        ("anchor", "TEXT", 1, None, 2),
        ("heading", "TEXT", 1, None, 0),
    ),
    "edges": (
        ("source_page_id", "TEXT", 1, None, 1),
        ("target_page_id", "TEXT", 1, None, 2),
        ("target_anchor", "TEXT", 1, "''", 3),
        ("kind", "TEXT", 1, None, 0),
        ("raw_target", "TEXT", 1, None, 0),
    ),
}


class GraphStore:
    """Own the base-local, rebuildable SQLite graph cache."""

    def __init__(self, base: str | Path) -> None:
        self.base = Path(base)
        self.path = self.base / ".iwiki" / "graph.sqlite3"

    def connect(self) -> sqlite3.Connection:
        """Open a configured connection and initialize an empty schema."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            ensure_graph_store_excluded(str(self.base))
            connection = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000)
            try:
                self._configure(connection)
                self._ensure_schema(connection)
            except Exception:
                connection.close()
                raise
            return connection
        except GraphStoreError:
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            raise GraphStoreError("cannot open graph store") from exc

    @staticmethod
    def _insert_snapshot(
        connection: sqlite3.Connection, snapshot: DomainSnapshot
    ) -> None:
        connection.executemany(
            "INSERT INTO pages VALUES (?, ?, ?, ?, ?)",
            (
                (
                    page.page_id,
                    page.domain,
                    page.file,
                    page.content_hash,
                    page.link_hash,
                )
                for page in snapshot.pages
            ),
        )
        connection.executemany(
            "INSERT INTO anchors VALUES (?, ?, ?)",
            (
                (anchor.page_id, anchor.anchor, anchor.heading)
                for anchor in snapshot.anchors
            ),
        )
        connection.executemany(
            "INSERT INTO edges VALUES (?, ?, ?, ?, ?)",
            (
                (
                    edge.source_page_id,
                    edge.target_page_id,
                    edge.target_anchor,
                    edge.kind,
                    edge.raw_target,
                )
                for edge in snapshot.edges
            ),
        )

    def rebuild_domain(
        self,
        domain: str,
        domain_dir: str | Path,
        *,
        markdown_fingerprint: str,
        fingerprint_provider: Callable[[], str],
        indexed_commit: str | None = None,
        indexed_at: str,
    ) -> None:
        """Replace one domain while the caller holds the base lock.

        Fingerprint resolution belongs to the caller. The supplied provider is
        rechecked inside the final write transaction before rows are replaced.
        """
        try:
            self._mark_domain_rebuilding(domain)
            snapshot = build_domain_snapshot(domain, domain_dir)
            with self.transaction() as connection:
                if fingerprint_provider() != markdown_fingerprint:
                    raise GraphStoreError("markdown fingerprint changed")
                connection.execute(
                    "DELETE FROM pages WHERE domain = ?", (domain,)
                )
                self._insert_snapshot(connection, snapshot)
                connection.execute(
                    "INSERT INTO domains VALUES (?, ?, ?, 'ready', ?) "
                    "ON CONFLICT(domain) DO UPDATE SET "
                    "indexed_commit = excluded.indexed_commit, "
                    "markdown_fingerprint = excluded.markdown_fingerprint, "
                    "state = excluded.state, indexed_at = excluded.indexed_at",
                    (
                        domain,
                        indexed_commit,
                        markdown_fingerprint,
                        indexed_at,
                    ),
                )
        except Exception as exc:
            try:
                self.mark_domain_dirty(domain)
            except Exception:
                pass
            cause = (
                exc.__cause__
                if isinstance(exc, GraphStoreError) and exc.__cause__ is not None
                else exc
            )
            raise GraphStoreError("cannot rebuild domain graph") from cause

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def mark_domain_dirty(self, domain: str) -> None:
        """Commit a short dirty transition without acquiring the base lock."""
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO domains VALUES (?, NULL, '', 'dirty', ?) "
                "ON CONFLICT(domain) DO UPDATE SET state = excluded.state",
                (domain, self._now()),
            )

    def _mark_domain_rebuilding(self, domain: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO domains VALUES (?, NULL, '', 'rebuilding', ?) "
                "ON CONFLICT(domain) DO UPDATE SET state = excluded.state",
                (domain, self._now()),
            )

    @staticmethod
    def _replace_page_rows(
        connection: sqlite3.Connection,
        page: PageRecord,
        anchors: tuple[AnchorRecord, ...],
        edges: tuple[EdgeRecord, ...],
    ) -> None:
        connection.execute(
            "INSERT INTO pages VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(page_id) DO UPDATE SET "
            "domain = excluded.domain, file = excluded.file, "
            "content_hash = excluded.content_hash, "
            "link_hash = excluded.link_hash",
            (
                page.page_id,
                page.domain,
                page.file,
                page.content_hash,
                page.link_hash,
            ),
        )
        connection.execute(
            "DELETE FROM anchors WHERE page_id = ?", (page.page_id,)
        )
        connection.execute(
            "DELETE FROM edges WHERE source_page_id = ?", (page.page_id,)
        )
        connection.executemany(
            "INSERT INTO anchors VALUES (?, ?, ?)",
            (
                (anchor.page_id, anchor.anchor, anchor.heading)
                for anchor in anchors
            ),
        )
        connection.executemany(
            "INSERT INTO edges VALUES (?, ?, ?, ?, ?)",
            (
                (
                    edge.source_page_id,
                    edge.target_page_id,
                    edge.target_anchor,
                    edge.kind,
                    edge.raw_target,
                )
                for edge in edges
            ),
        )

    def refresh_pages(
        self,
        domain: str,
        pages: Iterable[tuple[str, str]],
        *,
        delete_files: Iterable[str] = (),
        finalize: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        """Atomically replace/delete pages and run final validation in one batch."""
        deletions = set(delete_files)
        prepared = []
        for file, content in pages:
            if file in RESERVED_OKF:
                deletions.add(file)
                continue
            prepared.append(_page_snapshot(domain, file, content))
        with self.transaction() as connection:
            connection.executemany(
                "DELETE FROM pages WHERE domain = ? AND file = ?",
                ((domain, file) for file in sorted(deletions)),
            )
            for page, anchors, edges in prepared:
                self._replace_page_rows(connection, page, anchors, edges)
            if finalize is not None:
                finalize(connection)

    def refresh_page(self, domain: str, file: str, content: str) -> None:
        """Atomically replace one page and all of its derived graph rows."""
        self.refresh_pages(domain, ((file, content),))

    def delete_page(self, domain: str, file: str) -> None:
        """Delete a page and its cascaded anchors/outgoing edges."""
        self.refresh_pages(domain, (), delete_files=(file,))

    def load_ready_domain(self, domain: str) -> DomainSnapshot:
        """Load a domain snapshot only when its committed state is ready."""
        with self.read_snapshot() as connection:
            state_row = connection.execute(
                "SELECT state FROM domains WHERE domain = ?", (domain,)
            ).fetchone()
            state = "missing" if state_row is None else state_row[0]
            if state != "ready":
                raise GraphDomainUnavailable(domain, state)
            pages = tuple(
                PageRecord(*row)
                for row in connection.execute(
                    "SELECT page_id, domain, file, content_hash, link_hash "
                    "FROM pages WHERE domain = ? ORDER BY file",
                    (domain,),
                )
            )
            anchors = tuple(
                AnchorRecord(*row)
                for row in connection.execute(
                    "SELECT anchors.page_id, anchors.anchor, anchors.heading "
                    "FROM anchors JOIN pages ON pages.page_id = anchors.page_id "
                    "WHERE pages.domain = ? ORDER BY pages.file, anchors.rowid",
                    (domain,),
                )
            )
            edges = tuple(
                EdgeRecord(*row)
                for row in connection.execute(
                    "SELECT edges.source_page_id, edges.target_page_id, "
                    "edges.target_anchor, edges.kind, edges.raw_target "
                    "FROM edges JOIN pages "
                    "ON pages.page_id = edges.source_page_id "
                    "WHERE pages.domain = ? "
                    "ORDER BY pages.file, edges.target_page_id, "
                    "edges.target_anchor",
                    (domain,),
                )
            )
        return DomainSnapshot(domain, pages, anchors, edges)

    def query_incoming_pages(
        self,
        domains: tuple[str, ...],
        target_page_id: str,
        target_anchor: str | None = None,
    ) -> tuple[PageRecord, ...]:
        """Return indexed source pages linking to one exact graph target."""
        requested = tuple(sorted(set(domains)))
        if not requested:
            return ()
        placeholders = ", ".join("?" for _ in requested)
        anchor_sql = "" if target_anchor is None else "AND edges.target_anchor = ? "
        parameters: tuple[str, ...] = (
            target_page_id,
            *((target_anchor,) if target_anchor is not None else ()),
            *requested,
        )
        with self.read_snapshot() as connection:
            rows = connection.execute(
                "SELECT DISTINCT pages.page_id, pages.domain, pages.file, "
                "pages.content_hash, pages.link_hash FROM edges "
                "JOIN pages ON pages.page_id = edges.source_page_id "
                "WHERE edges.target_page_id = ? AND edges.kind = 'cross' "
                f"{anchor_sql}AND pages.domain IN ({placeholders}) "
                "ORDER BY pages.domain, pages.file",
                parameters,
            )
            return tuple(PageRecord(*row) for row in rows)

    @staticmethod
    def _configure(connection: sqlite3.Connection) -> None:
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        GraphStore._enable_wal(connection)
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _enable_wal(connection: sqlite3.Connection) -> None:
        deadline = time.monotonic() + _WAL_RETRY_DEADLINE_SECONDS
        while True:
            try:
                row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
                if row is None or str(row[0]).casefold() != "wal":
                    raise GraphStoreError("cannot enable WAL mode")
                return
            except sqlite3.OperationalError as exc:
                error_code = getattr(exc, "sqlite_errorcode", None)
                is_locked = (
                    error_code is not None
                    and error_code & 0xFF
                    in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                ) or str(exc).casefold().startswith(
                    (
                        "database is busy",
                        "database is locked",
                        "database table is locked",
                        "database schema is locked",
                    )
                )
                remaining = deadline - time.monotonic()
                if not is_locked or remaining <= 0:
                    raise
                time.sleep(min(_WAL_RETRY_INTERVAL_SECONDS, remaining))

    @staticmethod
    def _user_tables(connection: sqlite3.Connection) -> set[str]:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise GraphSchemaError("unsupported graph schema version")
        if version == 0:
            self._migrate_schema(connection, version)
            return
        if version != SCHEMA_VERSION:
            raise GraphSchemaError("unrecognized graph schema")
        self._validate_v1(connection)

    def _migrate_schema(
        self, connection: sqlite3.Connection, source_version: int
    ) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = self._user_tables(connection)
            if version == SCHEMA_VERSION:
                self._validate_v1(connection)
                connection.commit()
                return
            if version != source_version or tables:
                raise GraphSchemaError("unrecognized graph schema")
            while version < SCHEMA_VERSION:
                migration = _MIGRATIONS.get(version)
                if migration is None:
                    raise GraphSchemaError("unrecognized graph schema")
                target_version, statements = migration
                if target_version <= version or target_version > SCHEMA_VERSION:
                    raise GraphSchemaError("unrecognized graph schema")
                for statement in statements:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {target_version}")
                version = target_version
            self._validate_v1(connection)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    @staticmethod
    def _index_columns(
        connection: sqlite3.Connection, index_name: str
    ) -> tuple[str, ...]:
        return tuple(
            row[2]
            for row in connection.execute(
                f"PRAGMA index_info({index_name})"
            )
        )

    def _validate_v1(self, connection: sqlite3.Connection) -> None:
        if self._user_tables(connection) != set(_EXPECTED_COLUMNS):
            raise GraphSchemaError("incompatible graph schema")

        for table, expected in _EXPECTED_COLUMNS.items():
            actual = tuple(
                (row[1], row[2].upper(), row[3], row[4], row[5])
                for row in connection.execute(f"PRAGMA table_info({table})")
            )
            if actual != expected:
                raise GraphSchemaError("incompatible graph schema")

        page_unique = {
            self._index_columns(connection, row[1])
            for row in connection.execute("PRAGMA index_list(pages)")
            if row[2] == 1 and row[3] == "u"
        }
        if page_unique != {("domain", "file")}:
            raise GraphSchemaError("incompatible graph schema")

        anchors_foreign_keys = tuple(
            (row[2], row[3], row[4], row[5], row[6], row[7])
            for row in connection.execute("PRAGMA foreign_key_list(anchors)")
        )
        edges_foreign_keys = tuple(
            (row[2], row[3], row[4], row[5], row[6], row[7])
            for row in connection.execute("PRAGMA foreign_key_list(edges)")
        )
        source_page_foreign_key = (
            "pages",
            "page_id",
            "page_id",
            "NO ACTION",
            "CASCADE",
            "NONE",
        )
        source_edge_foreign_key = (
            "pages",
            "source_page_id",
            "page_id",
            "NO ACTION",
            "CASCADE",
            "NONE",
        )
        if anchors_foreign_keys != (source_page_foreign_key,):
            raise GraphSchemaError("incompatible graph schema")
        if edges_foreign_keys != (source_edge_foreign_key,):
            raise GraphSchemaError("incompatible graph schema")

        explicit_indexes = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT name, tbl_name FROM sqlite_master "
                "WHERE type = 'index' AND sql IS NOT NULL"
            )
        }
        if explicit_indexes != {"edges_target_idx": "edges"}:
            raise GraphSchemaError("incompatible graph schema")
        target_index = tuple(
            row
            for row in connection.execute("PRAGMA index_list(edges)")
            if row[1] == "edges_target_idx"
        )
        if len(target_index) != 1 or (
            target_index[0][2], target_index[0][3], target_index[0][4]
        ) != (0, "c", 0):
            raise GraphSchemaError("incompatible graph schema")
        if self._index_columns(connection, "edges_target_idx") != (
            "target_page_id",
        ):
            raise GraphSchemaError("incompatible graph schema")

        actual_table_ddl = {
            row[0]: _normalize_ddl(row[1])
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if actual_table_ddl != _EXPECTED_TABLE_DDL:
            raise GraphSchemaError("incompatible graph schema")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Yield one immediate write transaction and close it afterward."""
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except sqlite3.DatabaseError as exc:
            self._rollback_quietly(connection)
            raise GraphStoreError("graph transaction failed") from exc
        except BaseException:
            self._rollback_quietly(connection)
            raise
        finally:
            connection.close()

    @contextmanager
    def read_snapshot(self) -> Iterator[sqlite3.Connection]:
        """Yield a query-only read transaction with immutable SQLite rows."""
        connection = self.connect()
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            yield connection
            connection.rollback()
        except sqlite3.DatabaseError as exc:
            self._rollback_quietly(connection)
            raise GraphStoreError("graph read failed") from exc
        except BaseException:
            self._rollback_quietly(connection)
            raise
        finally:
            connection.close()

    @staticmethod
    def _rollback_quietly(connection: sqlite3.Connection) -> None:
        try:
            connection.rollback()
        except sqlite3.DatabaseError:
            pass
