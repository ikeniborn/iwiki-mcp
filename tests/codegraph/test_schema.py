"""Exact legacy and publication SQLite schema profiles."""
from contextlib import closing
import sqlite3

import pytest

from iwiki_mcp.codegraph.schema import (
    INDEXES,
    TABLES,
    CodeGraphSchemaError,
    create_publication_schema,
    create_schema,
    validate_schema,
)


def _objects(connection: sqlite3.Connection) -> dict[str, set[str]]:
    rows = connection.execute(
        "SELECT type, name FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%'"
    )
    result: dict[str, set[str]] = {}
    for kind, name in rows:
        result.setdefault(kind, set()).add(name)
    return result


def test_schema_v2_accepts_exact_legacy_and_publication_profiles(tmp_path):
    with closing(sqlite3.connect(tmp_path / "legacy.db")) as legacy:
        create_schema(legacy)
        validate_schema(legacy)
        assert _objects(legacy) == {
            "table": set(TABLES),
            "index": set(INDEXES),
        }

    with closing(sqlite3.connect(tmp_path / "publication.db")) as published:
        create_publication_schema(published)
        validate_schema(published)
        assert _objects(published) == {
            "table": {*TABLES, "code_graph_publication"},
            "index": set(INDEXES),
        }

    assert TABLES == (
        "repositories",
        "files",
        "symbols",
        "relations",
        "wiki_code_links",
    )


@pytest.mark.parametrize(
    "ddl",
    [
        "CREATE TABLE unexpected(value TEXT)",
        "CREATE INDEX unexpected ON repositories(repository_id)",
        "CREATE VIEW unexpected AS SELECT repository_id FROM repositories",
        (
            "CREATE TRIGGER unexpected AFTER INSERT ON repositories "
            "BEGIN SELECT 1; END"
        ),
    ],
)
@pytest.mark.parametrize("publication", [False, True])
def test_schema_v2_rejects_every_additional_schema_object(
    tmp_path, ddl, publication
):
    with closing(sqlite3.connect(tmp_path / "profile.db")) as connection:
        creator = create_publication_schema if publication else create_schema
        creator(connection)
        connection.execute(ddl)
        connection.commit()

        with pytest.raises(CodeGraphSchemaError):
            validate_schema(connection)


@pytest.mark.parametrize(
    "ddl_change",
    [
        "ALTER TABLE code_graph_publication ADD COLUMN unexpected TEXT",
        "ALTER TABLE code_graph_publication RENAME TO unexpected_publication",
    ],
)
def test_publication_profile_rejects_internal_ddl_changes(tmp_path, ddl_change):
    with closing(sqlite3.connect(tmp_path / "publication.db")) as connection:
        create_publication_schema(connection)
        connection.execute(ddl_change)
        connection.commit()

        with pytest.raises(CodeGraphSchemaError):
            validate_schema(connection)


def test_publication_table_has_exact_typed_singleton_envelope(tmp_path):
    with closing(sqlite3.connect(tmp_path / "publication.db")) as connection:
        create_publication_schema(connection)

        assert tuple(connection.execute(
            "PRAGMA table_info(code_graph_publication)"
        )) == (
            (0, "singleton", "INTEGER", 0, None, 1),
            (1, "format_version", "INTEGER", 1, None, 0),
            (2, "state", "TEXT", 1, None, 0),
            (3, "domain", "TEXT", 1, None, 0),
            (4, "repository_id", "TEXT", 1, None, 0),
            (5, "session_id", "TEXT", 1, None, 0),
            (6, "graph_payload_revision", "TEXT", 1, None, 0),
            (7, "snapshot_revision", "TEXT", 1, None, 0),
            (8, "markdown_revision", "TEXT", 1, None, 0),
            (9, "counts_json", "TEXT", 1, None, 0),
            (10, "indexed_at", "TEXT", 1, None, 0),
            (11, "terminal_result_json", "TEXT", 1, None, 0),
            (12, "content_digest", "TEXT", 1, None, 0),
            (13, "envelope_digest", "TEXT", 1, None, 0),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO code_graph_publication ("
                "singleton, format_version, state, domain, repository_id, "
                "session_id, graph_payload_revision, snapshot_revision, "
                "markdown_revision, counts_json, indexed_at, "
                "terminal_result_json, content_digest, envelope_digest"
                ") VALUES (2, 1, 'ready', 'docs', 'docs', 'session', "
                "'sha256:g', 'sha256:s', 'sha256:m', '{}', 'now', '{}', "
                "'sha256:c', 'sha256:e')"
            )
