"""SQLite schema and integrity checks for the code graph cache."""
from __future__ import annotations

import re
import sqlite3

from .models import CodeGraphError


SCHEMA_VERSION = 2
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
    """Raised when a code graph database does not match schema v2."""


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
            normalizer_version TEXT NOT NULL,
            unicode_data_version TEXT NOT NULL,
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
            path TEXT NOT NULL COLLATE BINARY,
            path_casefold TEXT,
            file_local_name TEXT NOT NULL COLLATE BINARY,
            file_name_tokens_casefold TEXT NOT NULL,
            language TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
            start_line INTEGER NOT NULL CHECK (start_line = 1),
            end_line INTEGER NOT NULL CHECK (end_line >= start_line),
            start_byte INTEGER NOT NULL CHECK (start_byte = 0),
            end_byte INTEGER NOT NULL CHECK (end_byte = size_bytes),
            module_key TEXT NOT NULL COLLATE BINARY,
            module_id TEXT UNIQUE,
            module_qualified_name TEXT COLLATE BINARY,
            module_local_name TEXT COLLATE BINARY,
            module_name_tokens_casefold TEXT,
            UNIQUE(repository_id, path),
            CHECK (
                (module_id IS NULL
                 AND module_qualified_name IS NULL
                 AND module_local_name IS NULL
                 AND module_name_tokens_casefold IS NULL)
                OR
                (module_id IS NOT NULL
                 AND module_qualified_name IS NOT NULL
                 AND module_local_name IS NOT NULL
                 AND module_name_tokens_casefold IS NOT NULL)
            )
        )
    """,
    "symbols": """
        CREATE TABLE symbols (
            symbol_id TEXT PRIMARY KEY,
            file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
            kind TEXT NOT NULL
                CHECK (kind IN (
                    'class', 'function', 'async_function', 'method',
                    'interface', 'type_alias', 'enum'
                )),
            qualified_name TEXT NOT NULL COLLATE BINARY,
            local_name TEXT NOT NULL COLLATE BINARY,
            name_tokens_casefold TEXT NOT NULL,
            start_line INTEGER NOT NULL CHECK (start_line >= 1),
            end_line INTEGER NOT NULL CHECK (end_line >= start_line),
            start_byte INTEGER NOT NULL CHECK (start_byte >= 0),
            end_byte INTEGER NOT NULL CHECK (end_byte >= start_byte),
            signature TEXT,
            signature_casefold TEXT,
            visibility TEXT,
            content_hash TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(file_id, qualified_name, start_line)
        )
    """,
    "relations": """
        CREATE TABLE relations (
            relation_id TEXT PRIMARY KEY,
            source_file_id TEXT NOT NULL
                REFERENCES files(file_id) ON DELETE CASCADE,
            source_module_id TEXT
                REFERENCES files(module_id) ON DELETE CASCADE,
            source_symbol_id TEXT
                REFERENCES symbols(symbol_id) ON DELETE CASCADE,
            target_module_id TEXT
                REFERENCES files(module_id) ON DELETE CASCADE,
            target_symbol_id TEXT
                REFERENCES symbols(symbol_id) ON DELETE CASCADE,
            target_reference TEXT,
            relation_type TEXT NOT NULL
                CHECK (relation_type IN ('DECLARES', 'IMPORTS', 'CALLS', 'INHERITS')),
            source_start_line INTEGER NOT NULL CHECK (source_start_line >= 1),
            source_end_line INTEGER NOT NULL CHECK (source_end_line >= source_start_line),
            source_start_byte INTEGER NOT NULL CHECK (source_start_byte >= 0),
            source_end_byte INTEGER NOT NULL CHECK (source_end_byte >= source_start_byte),
            binding_name TEXT COLLATE BINARY,
            binding_kind TEXT
                CHECK (binding_kind IN ('implicit_binding', 'explicit_alias')),
            binding_name_tokens_casefold TEXT,
            confidence REAL NOT NULL DEFAULT 1.0
                CHECK (confidence >= 0.0 AND confidence <= 1.0),
            resolution_state TEXT NOT NULL
                CHECK (resolution_state IN (
                    'resolved', 'partially_resolved', 'unresolved', 'ambiguous'
                )),
            metadata_json TEXT NOT NULL DEFAULT '{}',
            CHECK (source_module_id IS NULL OR source_symbol_id IS NULL),
            CHECK (
                (target_module_id IS NOT NULL)
                + (target_symbol_id IS NOT NULL) <= 1
            ),
            CHECK (
                (resolution_state IN ('resolved', 'ambiguous')
                 AND target_reference IS NULL
                 AND ((target_module_id IS NOT NULL)
                      + (target_symbol_id IS NOT NULL) = 1))
                OR
                (resolution_state = 'partially_resolved'
                 AND target_reference IS NOT NULL
                 AND ((target_module_id IS NOT NULL)
                      + (target_symbol_id IS NOT NULL) = 1))
                OR
                (resolution_state = 'unresolved'
                 AND target_reference IS NOT NULL
                 AND target_module_id IS NULL
                 AND target_symbol_id IS NULL)
            ),
            CHECK (
                (relation_type = 'IMPORTS'
                 AND binding_name IS NOT NULL
                 AND binding_kind IS NOT NULL
                 AND binding_name_tokens_casefold IS NOT NULL)
                OR
                (relation_type <> 'IMPORTS'
                 AND binding_name IS NULL
                 AND binding_kind IS NULL
                 AND binding_name_tokens_casefold IS NULL)
            )
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

PUBLICATION_TABLE_DDL = """
    CREATE TABLE code_graph_publication (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        format_version INTEGER NOT NULL CHECK (format_version = 1),
        state TEXT NOT NULL CHECK (state = 'ready'),
        domain TEXT NOT NULL,
        repository_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        graph_payload_revision TEXT NOT NULL,
        snapshot_revision TEXT NOT NULL,
        markdown_revision TEXT NOT NULL,
        counts_json TEXT NOT NULL,
        indexed_at TEXT NOT NULL,
        terminal_result_json TEXT NOT NULL,
        content_digest TEXT NOT NULL,
        envelope_digest TEXT NOT NULL
    )
"""

INDEX_DDL = {
    "idx_files_repository_path": (
        "CREATE INDEX idx_files_repository_path ON files(repository_id, path)"
    ),
    "idx_files_repository_local": (
        "CREATE INDEX idx_files_repository_local "
        "ON files(repository_id, file_local_name)"
    ),
    "idx_files_content_hash": (
        "CREATE INDEX idx_files_content_hash ON files(content_hash)"
    ),
    "idx_files_repository_module_key": (
        "CREATE INDEX idx_files_repository_module_key "
        "ON files(repository_id, module_key)"
    ),
    "idx_files_repository_module_qualified": (
        "CREATE INDEX idx_files_repository_module_qualified "
        "ON files(repository_id, module_qualified_name)"
    ),
    "idx_files_repository_module_local": (
        "CREATE INDEX idx_files_repository_module_local "
        "ON files(repository_id, module_local_name)"
    ),
    "idx_symbols_file": "CREATE INDEX idx_symbols_file ON symbols(file_id)",
    "idx_symbols_qualified": (
        "CREATE INDEX idx_symbols_qualified ON symbols(qualified_name)"
    ),
    "idx_symbols_local": "CREATE INDEX idx_symbols_local ON symbols(local_name)",
    "idx_symbols_kind": "CREATE INDEX idx_symbols_kind ON symbols(kind)",
    "idx_relations_source_file_type": (
        "CREATE INDEX idx_relations_source_file_type "
        "ON relations(source_file_id, relation_type)"
    ),
    "idx_relations_source_module_type": (
        "CREATE INDEX idx_relations_source_module_type "
        "ON relations(source_module_id, relation_type)"
    ),
    "idx_relations_source_symbol_type": (
        "CREATE INDEX idx_relations_source_symbol_type "
        "ON relations(source_symbol_id, relation_type)"
    ),
    "idx_relations_target_module_type": (
        "CREATE INDEX idx_relations_target_module_type "
        "ON relations(target_module_id, relation_type)"
    ),
    "idx_relations_target_symbol_type": (
        "CREATE INDEX idx_relations_target_symbol_type "
        "ON relations(target_symbol_id, relation_type)"
    ),
    "idx_relations_reference": (
        "CREATE INDEX idx_relations_reference ON relations(target_reference)"
    ),
    "idx_relations_explicit_alias": (
        "CREATE INDEX idx_relations_explicit_alias "
        "ON relations(binding_kind, binding_name)"
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
EXPECTED_IMPLICIT_UNIQUE_INDEXES = tuple(sorted((
    ("repositories", ("repository_id",)),
    ("files", ("file_id",)),
    ("files", ("module_id",)),
    ("files", ("repository_id", "path")),
    ("symbols", ("symbol_id",)),
    ("symbols", ("file_id", "qualified_name", "start_line")),
    ("relations", ("relation_id",)),
    ("wiki_code_links", ("link_id",)),
)))

EXPECTED_COLUMNS = {
    "repositories": (
        ("repository_id", "TEXT", 0, None, 1),
        ("root_path", "TEXT", 1, None, 0),
        ("git_remote", "TEXT", 0, None, 0),
        ("git_commit", "TEXT", 0, None, 0),
        ("source_fingerprint", "TEXT", 1, None, 0),
        ("config_fingerprint", "TEXT", 1, None, 0),
        ("parser_fingerprint", "TEXT", 1, None, 0),
        ("normalizer_version", "TEXT", 1, None, 0),
        ("unicode_data_version", "TEXT", 1, None, 0),
        ("revision", "TEXT", 1, None, 0),
        ("state", "TEXT", 1, None, 0),
        ("indexed_at", "TEXT", 1, None, 0),
    ),
    "files": (
        ("file_id", "TEXT", 0, None, 1),
        ("repository_id", "TEXT", 1, None, 0),
        ("path", "TEXT", 1, None, 0),
        ("path_casefold", "TEXT", 0, None, 0),
        ("file_local_name", "TEXT", 1, None, 0),
        ("file_name_tokens_casefold", "TEXT", 1, None, 0),
        ("language", "TEXT", 1, None, 0),
        ("content_hash", "TEXT", 1, None, 0),
        ("parser_version", "TEXT", 1, None, 0),
        ("size_bytes", "INTEGER", 1, None, 0),
        ("start_line", "INTEGER", 1, None, 0),
        ("end_line", "INTEGER", 1, None, 0),
        ("start_byte", "INTEGER", 1, None, 0),
        ("end_byte", "INTEGER", 1, None, 0),
        ("module_key", "TEXT", 1, None, 0),
        ("module_id", "TEXT", 0, None, 0),
        ("module_qualified_name", "TEXT", 0, None, 0),
        ("module_local_name", "TEXT", 0, None, 0),
        ("module_name_tokens_casefold", "TEXT", 0, None, 0),
    ),
    "symbols": (
        ("symbol_id", "TEXT", 0, None, 1),
        ("file_id", "TEXT", 1, None, 0),
        ("kind", "TEXT", 1, None, 0),
        ("qualified_name", "TEXT", 1, None, 0),
        ("local_name", "TEXT", 1, None, 0),
        ("name_tokens_casefold", "TEXT", 1, None, 0),
        ("start_line", "INTEGER", 1, None, 0),
        ("end_line", "INTEGER", 1, None, 0),
        ("start_byte", "INTEGER", 1, None, 0),
        ("end_byte", "INTEGER", 1, None, 0),
        ("signature", "TEXT", 0, None, 0),
        ("signature_casefold", "TEXT", 0, None, 0),
        ("visibility", "TEXT", 0, None, 0),
        ("content_hash", "TEXT", 1, None, 0),
        ("metadata_json", "TEXT", 1, "'{}'", 0),
    ),
    "relations": (
        ("relation_id", "TEXT", 0, None, 1),
        ("source_file_id", "TEXT", 1, None, 0),
        ("source_module_id", "TEXT", 0, None, 0),
        ("source_symbol_id", "TEXT", 0, None, 0),
        ("target_module_id", "TEXT", 0, None, 0),
        ("target_symbol_id", "TEXT", 0, None, 0),
        ("target_reference", "TEXT", 0, None, 0),
        ("relation_type", "TEXT", 1, None, 0),
        ("source_start_line", "INTEGER", 1, None, 0),
        ("source_end_line", "INTEGER", 1, None, 0),
        ("source_start_byte", "INTEGER", 1, None, 0),
        ("source_end_byte", "INTEGER", 1, None, 0),
        ("binding_name", "TEXT", 0, None, 0),
        ("binding_kind", "TEXT", 0, None, 0),
        ("binding_name_tokens_casefold", "TEXT", 0, None, 0),
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

EXPECTED_PUBLICATION_COLUMNS = (
    ("singleton", "INTEGER", 0, None, 1),
    ("format_version", "INTEGER", 1, None, 0),
    ("state", "TEXT", 1, None, 0),
    ("domain", "TEXT", 1, None, 0),
    ("repository_id", "TEXT", 1, None, 0),
    ("session_id", "TEXT", 1, None, 0),
    ("graph_payload_revision", "TEXT", 1, None, 0),
    ("snapshot_revision", "TEXT", 1, None, 0),
    ("markdown_revision", "TEXT", 1, None, 0),
    ("counts_json", "TEXT", 1, None, 0),
    ("indexed_at", "TEXT", 1, None, 0),
    ("terminal_result_json", "TEXT", 1, None, 0),
    ("content_digest", "TEXT", 1, None, 0),
    ("envelope_digest", "TEXT", 1, None, 0),
)


def normalize_ddl(statement: str) -> str:
    """Normalize harmless SQLite formatting while preserving the SQL contract."""
    normalized = re.sub(r"\s+", " ", statement.strip().rstrip(";"))
    normalized = re.sub(r"\s*([(),])\s*", r"\1", normalized)
    return normalized


EXPECTED_TABLE_DDL = {
    name: normalize_ddl(statement) for name, statement in TABLE_DDL.items()
}
EXPECTED_PUBLICATION_TABLE_DDL = normalize_ddl(PUBLICATION_TABLE_DDL)
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
    """Create exact schema v2 in a verified-empty database."""
    for statement in TABLE_DDL.values():
        connection.execute(statement)
    for statement in INDEX_DDL.values():
        connection.execute(statement)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    connection.commit()


def create_publication_schema(connection: sqlite3.Connection) -> None:
    """Create exact schema-v2 publication profile in an empty database."""
    for statement in TABLE_DDL.values():
        connection.execute(statement)
    for statement in INDEX_DDL.values():
        connection.execute(statement)
    connection.execute(PUBLICATION_TABLE_DDL)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    connection.commit()


def validate_schema(connection: sqlite3.Connection) -> None:
    """Reject every table, index, column, or version mismatch."""
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        raise CodeGraphSchemaError("incompatible code graph schema")

    schema_objects = tuple(connection.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%'"
    ))
    if any(kind not in {"table", "index"} for kind, _name, _sql in schema_objects):
        raise CodeGraphSchemaError("incompatible code graph schema")
    table_ddl = {
        name: normalize_ddl(sql)
        for kind, name, sql in schema_objects
        if kind == "table"
    }
    index_ddl = {
        row[0]: normalize_ddl(row[1])
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND sql IS NOT NULL"
        )
    }
    expected_publication = {
        **EXPECTED_TABLE_DDL,
        "code_graph_publication": EXPECTED_PUBLICATION_TABLE_DDL,
    }
    if (
        table_ddl not in (EXPECTED_TABLE_DDL, expected_publication)
        or index_ddl != EXPECTED_INDEX_DDL
    ):
        raise CodeGraphSchemaError("incompatible code graph schema")

    implicit_unique_indexes = tuple(sorted(
        (
            table,
            tuple(
                column[0]
                for column in connection.execute(
                    "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                    (index_name,),
                )
            ),
        )
        for index_name, table in connection.execute(
            "SELECT name, tbl_name FROM sqlite_master "
            "WHERE type = 'index' AND sql IS NULL"
        )
    ))
    if implicit_unique_indexes != EXPECTED_IMPLICIT_UNIQUE_INDEXES:
        raise CodeGraphSchemaError("incompatible code graph schema")

    expected_columns = dict(EXPECTED_COLUMNS)
    if "code_graph_publication" in table_ddl:
        expected_columns["code_graph_publication"] = EXPECTED_PUBLICATION_COLUMNS
    for table, expected in expected_columns.items():
        actual = tuple(
            (row[1], row[2].upper(), row[3], row[4], row[5])
            for row in connection.execute(f"PRAGMA table_info({table})")
        )
        if actual != expected:
            raise CodeGraphSchemaError("incompatible code graph schema")


def inspect_compatibility(connection: sqlite3.Connection) -> str:
    """Classify schema compatibility without issuing any write operation."""
    try:
        validate_schema(connection)
    except (CodeGraphStoreError, sqlite3.DatabaseError):
        return "incompatible"
    return "compatible"


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

    try:
        provenance_row = connection.execute(
            "SELECT 1 FROM relations AS relation "
            "JOIN symbols AS symbol "
            "ON symbol.symbol_id = relation.source_symbol_id "
            "WHERE symbol.file_id <> relation.source_file_id "
            "UNION ALL "
            "SELECT 1 FROM relations AS relation "
            "JOIN files AS module_file "
            "ON module_file.module_id = relation.source_module_id "
            "WHERE module_file.file_id <> relation.source_file_id "
            "LIMIT 1"
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise CodeGraphStoreError(
            "code graph relation provenance check failed"
        ) from exc
    if provenance_row is not None:
        raise CodeGraphStoreError(
            "code graph relation provenance check failed"
        )
