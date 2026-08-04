"""Versioned SQLite storage for the local wiki graph cache."""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from iwiki_mcp.base import ensure_graph_store_excluded


SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000
_WAL_RETRY_DEADLINE_SECONDS = 0.25
_WAL_RETRY_INTERVAL_SECONDS = 0.01


class GraphStoreError(RuntimeError):
    """Raised when the local graph store cannot be opened safely."""


class GraphSchemaError(GraphStoreError):
    """Raised when the graph schema cannot be used by this version."""


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
