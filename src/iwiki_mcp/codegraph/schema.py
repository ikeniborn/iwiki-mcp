"""SQLite schema and integrity checks for the code graph cache."""
from __future__ import annotations

import re
import sqlite3

from .models import CodeGraphError


SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5_000
TABLES = (
    "repositories",
    "files",
    "symbols",
    "relations",
    "wiki_code_links",
)


class CodeGraphStoreError(CodeGraphError):
    """Raised when code graph storage cannot be read or written safely."""


class CodeGraphSchemaError(CodeGraphStoreError):
    """Raised when a code graph database does not match schema v1."""


TABLE_DDL = {
    "repositories": """
        CREATE TABLE repositories (
            repository_id TEXT PRIMARY KEY,
            root_path TEXT NOT NULL,
            git_remote TEXT,
            git_commit TEXT,
            source_fingerprint TEXT NOT NULL,
            config_fingerprint TEXT NOT NULL,
            parser_fingerprint TEXT NOT NULL,
            revision TEXT NOT NULL,
            state TEXT NOT NULL
                CHECK (state IN ('ready', 'dirty', 'rebuilding', 'failed')),
            indexed_at TEXT NOT NULL
        )
    """,
    "files": """
        CREATE TABLE files (
            file_id TEXT PRIMARY KEY,
            repository_id TEXT NOT NULL
                REFERENCES repositories(repository_id) ON DELETE CASCADE,
            path TEXT NOT NULL,
            language TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
            UNIQUE(repository_id, path)
        )
    """,
    "symbols": """
        CREATE TABLE symbols (
            symbol_id TEXT PRIMARY KEY,
            file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            qualified_name TEXT NOT NULL,
            local_name TEXT NOT NULL,
            start_line INTEGER NOT NULL CHECK (start_line >= 1),
            end_line INTEGER NOT NULL CHECK (end_line >= start_line),
            start_byte INTEGER,
            end_byte INTEGER,
            signature TEXT,
            visibility TEXT,
            content_hash TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(file_id, qualified_name, start_line)
        )
    """,
    "relations": """
        CREATE TABLE relations (
            relation_id TEXT PRIMARY KEY,
            source_symbol_id TEXT REFERENCES symbols(symbol_id) ON DELETE CASCADE,
            source_file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
            target_symbol_id TEXT REFERENCES symbols(symbol_id) ON DELETE CASCADE,
            target_reference TEXT,
            relation_type TEXT NOT NULL,
            source_line INTEGER,
            confidence REAL NOT NULL DEFAULT 1.0
                CHECK (confidence >= 0.0 AND confidence <= 1.0),
            resolution_state TEXT NOT NULL
                CHECK (resolution_state IN (
                    'resolved', 'partially_resolved', 'unresolved', 'ambiguous'
                )),
            metadata_json TEXT NOT NULL DEFAULT '{}',
            CHECK (target_symbol_id IS NOT NULL OR target_reference IS NOT NULL)
        )
    """,
    "wiki_code_links": """
        CREATE TABLE wiki_code_links (
            link_id TEXT PRIMARY KEY,
            domain TEXT NOT NULL,
            page_id TEXT NOT NULL,
            symbol_id TEXT REFERENCES symbols(symbol_id) ON DELETE CASCADE,
            file_id TEXT REFERENCES files(file_id) ON DELETE CASCADE,
            selector_kind TEXT NOT NULL
                CHECK (selector_kind IN ('symbol', 'file', 'source_glob')),
            relation_type TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 1.0
                CHECK (confidence >= 0.0 AND confidence <= 1.0),
            source TEXT NOT NULL,
            CHECK ((symbol_id IS NOT NULL) <> (file_id IS NOT NULL))
        )
    """,
}

INDEX_DDL = {
    "idx_files_repository_path": (
        "CREATE INDEX idx_files_repository_path ON files(repository_id, path)"
    ),
    "idx_files_content_hash": (
        "CREATE INDEX idx_files_content_hash ON files(content_hash)"
    ),
    "idx_symbols_file": "CREATE INDEX idx_symbols_file ON symbols(file_id)",
    "idx_symbols_qualified": (
        "CREATE INDEX idx_symbols_qualified ON symbols(qualified_name)"
    ),
    "idx_symbols_local": "CREATE INDEX idx_symbols_local ON symbols(local_name)",
    "idx_symbols_kind": "CREATE INDEX idx_symbols_kind ON symbols(kind)",
    "idx_relations_source_type": (
        "CREATE INDEX idx_relations_source_type "
        "ON relations(source_symbol_id, relation_type)"
    ),
    "idx_relations_target_type": (
        "CREATE INDEX idx_relations_target_type "
        "ON relations(target_symbol_id, relation_type)"
    ),
    "idx_relations_reference": (
        "CREATE INDEX idx_relations_reference ON relations(target_reference)"
    ),
    "idx_wiki_links_page": (
        "CREATE INDEX idx_wiki_links_page ON wiki_code_links(domain, page_id)"
    ),
    "idx_wiki_links_symbol": (
        "CREATE INDEX idx_wiki_links_symbol ON wiki_code_links(symbol_id)"
    ),
    "idx_wiki_links_file": (
        "CREATE INDEX idx_wiki_links_file ON wiki_code_links(file_id)"
    ),
}
INDEXES = tuple(INDEX_DDL)

EXPECTED_COLUMNS = {
    "repositories": (
        ("repository_id", "TEXT", 0, None, 1),
        ("root_path", "TEXT", 1, None, 0),
        ("git_remote", "TEXT", 0, None, 0),
        ("git_commit", "TEXT", 0, None, 0),
        ("source_fingerprint", "TEXT", 1, None, 0),
        ("config_fingerprint", "TEXT", 1, None, 0),
        ("parser_fingerprint", "TEXT", 1, None, 0),
        ("revision", "TEXT", 1, None, 0),
        ("state", "TEXT", 1, None, 0),
        ("indexed_at", "TEXT", 1, None, 0),
    ),
    "files": (
        ("file_id", "TEXT", 0, None, 1),
        ("repository_id", "TEXT", 1, None, 0),
        ("path", "TEXT", 1, None, 0),
        ("language", "TEXT", 1, None, 0),
        ("content_hash", "TEXT", 1, None, 0),
        ("parser_version", "TEXT", 1, None, 0),
        ("size_bytes", "INTEGER", 1, None, 0),
    ),
    "symbols": (
        ("symbol_id", "TEXT", 0, None, 1),
        ("file_id", "TEXT", 1, None, 0),
        ("kind", "TEXT", 1, None, 0),
        ("qualified_name", "TEXT", 1, None, 0),
        ("local_name", "TEXT", 1, None, 0),
        ("start_line", "INTEGER", 1, None, 0),
        ("end_line", "INTEGER", 1, None, 0),
        ("start_byte", "INTEGER", 0, None, 0),
        ("end_byte", "INTEGER", 0, None, 0),
        ("signature", "TEXT", 0, None, 0),
        ("visibility", "TEXT", 0, None, 0),
        ("content_hash", "TEXT", 1, None, 0),
        ("metadata_json", "TEXT", 1, "'{}'", 0),
    ),
    "relations": (
        ("relation_id", "TEXT", 0, None, 1),
        ("source_symbol_id", "TEXT", 0, None, 0),
        ("source_file_id", "TEXT", 1, None, 0),
        ("target_symbol_id", "TEXT", 0, None, 0),
        ("target_reference", "TEXT", 0, None, 0),
        ("relation_type", "TEXT", 1, None, 0),
        ("source_line", "INTEGER", 0, None, 0),
        ("confidence", "REAL", 1, "1.0", 0),
        ("resolution_state", "TEXT", 1, None, 0),
        ("metadata_json", "TEXT", 1, "'{}'", 0),
    ),
    "wiki_code_links": (
        ("link_id", "TEXT", 0, None, 1),
        ("domain", "TEXT", 1, None, 0),
        ("page_id", "TEXT", 1, None, 0),
        ("symbol_id", "TEXT", 0, None, 0),
        ("file_id", "TEXT", 0, None, 0),
        ("selector_kind", "TEXT", 1, None, 0),
        ("relation_type", "TEXT", 1, None, 0),
        ("confidence", "REAL", 1, "1.0", 0),
        ("source", "TEXT", 1, None, 0),
    ),
}


def normalize_ddl(statement: str) -> str:
    """Normalize harmless SQLite formatting while preserving the SQL contract."""
    normalized = re.sub(r"\s+", " ", statement.strip().rstrip(";"))
    normalized = re.sub(r"\s*([(),])\s*", r"\1", normalized)
    return normalized


EXPECTED_TABLE_DDL = {
    name: normalize_ddl(statement) for name, statement in TABLE_DDL.items()
}
EXPECTED_INDEX_DDL = {
    name: normalize_ddl(statement) for name, statement in INDEX_DDL.items()
}


def configure(connection: sqlite3.Connection) -> None:
    """Apply required SQLite behavior to one caller-owned connection."""
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    try:
        row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
    except sqlite3.DatabaseError as exc:
        raise CodeGraphStoreError("cannot enable code graph WAL mode") from exc
    if row is None or str(row[0]).casefold() != "wal":
        raise CodeGraphStoreError("cannot enable code graph WAL mode")


def create_schema(connection: sqlite3.Connection) -> None:
    """Create exact schema v1 in a verified-empty database."""
    for statement in TABLE_DDL.values():
        connection.execute(statement)
    for statement in INDEX_DDL.values():
        connection.execute(statement)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    connection.commit()


def validate_schema(connection: sqlite3.Connection) -> None:
    """Reject every table, index, column, or version mismatch."""
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        raise CodeGraphSchemaError("incompatible code graph schema")

    table_ddl = {
        row[0]: normalize_ddl(row[1])
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    index_ddl = {
        row[0]: normalize_ddl(row[1])
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND sql IS NOT NULL"
        )
    }
    if table_ddl != EXPECTED_TABLE_DDL or index_ddl != EXPECTED_INDEX_DDL:
        raise CodeGraphSchemaError("incompatible code graph schema")

    for table, expected in EXPECTED_COLUMNS.items():
        actual = tuple(
            (row[1], row[2].upper(), row[3], row[4], row[5])
            for row in connection.execute(f"PRAGMA table_info({table})")
        )
        if actual != expected:
            raise CodeGraphSchemaError("incompatible code graph schema")


def validate_integrity(connection: sqlite3.Connection) -> None:
    """Run SQLite integrity checks without exposing row or SQL details."""
    try:
        foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.DatabaseError as exc:
        raise CodeGraphStoreError("code graph foreign key check failed") from exc
    if foreign_key_rows:
        raise CodeGraphStoreError("code graph foreign key check failed")

    try:
        integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError as exc:
        raise CodeGraphStoreError("code graph integrity check failed") from exc
    if integrity_row != ("ok",):
        raise CodeGraphStoreError("code graph integrity check failed")
