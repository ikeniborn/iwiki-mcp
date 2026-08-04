from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


_VALID_V1_SCHEMA = """
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


def _graph_store_type():
    from iwiki_mcp.engine.graph_store import GraphStore

    return GraphStore


def _open_store(tmp_path: Path):
    store = _graph_store_type()(tmp_path)
    connection = store.connect()
    return store, connection


def test_graph_store_uses_base_local_graph_path_and_creates_directory(tmp_path):
    GraphStore = _graph_store_type()
    store = GraphStore(tmp_path)

    assert store.path == tmp_path / ".iwiki" / "graph.sqlite3"
    assert not store.path.parent.exists()

    connection = store.connect()
    connection.close()

    assert store.path.is_file()
    assert store.path.parent.is_dir()


def test_graph_store_initializes_schema_version_one(tmp_path):
    _, connection = _open_store(tmp_path)

    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert tables == {"domains", "pages", "anchors", "edges"}


def test_graph_store_configures_connection_pragmas(tmp_path):
    _, connection = _open_store(tmp_path)

    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("PRAGMA synchronous").fetchone()[0] == 1
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_schema_enforces_domain_state_and_page_uniqueness(tmp_path):
    _, connection = _open_store(tmp_path)

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO domains VALUES (?, ?, ?, ?, ?)",
            ("docs", None, "fingerprint", "unknown", "2026-08-04T00:00:00Z"),
        )

    connection.execute(
        "INSERT INTO domains VALUES (?, ?, ?, ?, ?)",
        ("docs", None, "fingerprint", "ready", "2026-08-04T00:00:00Z"),
    )
    connection.execute(
        "INSERT INTO pages VALUES (?, ?, ?, ?, ?)",
        ("page-1", "docs", "one.md", "content", "links"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO pages VALUES (?, ?, ?, ?, ?)",
            ("page-2", "docs", "one.md", "other-content", "other-links"),
        )


def test_schema_enforces_edges_and_cascades_source_page_rows(tmp_path):
    _, connection = _open_store(tmp_path)
    connection.execute(
        "INSERT INTO pages VALUES (?, ?, ?, ?, ?)",
        ("source", "docs", "source.md", "content", "links"),
    )
    connection.execute(
        "INSERT INTO pages VALUES (?, ?, ?, ?, ?)",
        ("target", "docs", "target.md", "content", "links"),
    )
    connection.execute(
        "INSERT INTO anchors VALUES (?, ?, ?)", ("source", "intro", "Intro")
    )
    connection.execute(
        "INSERT INTO edges(source_page_id, target_page_id, kind, raw_target) "
        "VALUES (?, ?, ?, ?)",
        ("source", "target", "intra", "target"),
    )

    edge = connection.execute(
        "SELECT target_anchor FROM edges WHERE source_page_id = 'source'"
    ).fetchone()
    assert edge[0] == ""

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO edges VALUES (?, ?, ?, ?, ?)",
            ("source", "elsewhere", "anchor", "external", "elsewhere"),
        )

    connection.execute("DELETE FROM pages WHERE page_id = 'source'")
    assert connection.execute("SELECT count(*) FROM anchors").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM edges").fetchone()[0] == 0


def test_edge_target_is_not_a_foreign_key(tmp_path):
    _, connection = _open_store(tmp_path)
    connection.execute(
        "INSERT INTO pages VALUES (?, ?, ?, ?, ?)",
        ("source", "docs", "source.md", "content", "links"),
    )
    connection.execute(
        "INSERT INTO pages VALUES (?, ?, ?, ?, ?)",
        ("target", "docs", "target.md", "content", "links"),
    )

    connection.execute(
        "INSERT INTO edges VALUES (?, ?, ?, ?, ?)",
        ("source", "target", "", "cross", "other/page"),
    )
    connection.execute("DELETE FROM pages WHERE page_id = 'target'")

    assert connection.execute("SELECT count(*) FROM edges").fetchone()[0] == 1


def test_schema_has_only_target_lookup_index_and_no_pages_domain_index(tmp_path):
    _, connection = _open_store(tmp_path)

    explicit_indexes = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'index' AND sql IS NOT NULL"
        )
    }
    assert explicit_indexes == {"edges_target_idx"}
    indexed_columns = [
        row[2] for row in connection.execute("PRAGMA index_info(edges_target_idx)")
    ]
    assert indexed_columns == ["target_page_id"]


def test_empty_version_zero_database_initializes(tmp_path):
    graph_path = tmp_path / ".iwiki" / "graph.sqlite3"
    graph_path.parent.mkdir()
    sqlite3.connect(graph_path).close()

    _, connection = _open_store(tmp_path)

    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_concurrent_version_zero_initialization_rechecks_schema_after_lock(
    tmp_path, monkeypatch
):
    from iwiki_mcp.engine import graph_store

    graph_path = tmp_path / ".iwiki" / "graph.sqlite3"
    graph_path.parent.mkdir()
    connection = sqlite3.connect(graph_path)
    assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    connection.close()

    migration_barrier = threading.Barrier(2)
    original_migrate_schema = graph_store.GraphStore._migrate_schema

    def synchronized_migrate_schema(self, connection, source_version):
        migration_barrier.wait(timeout=5)
        return original_migrate_schema(self, connection, source_version)

    monkeypatch.setattr(
        graph_store.GraphStore, "_migrate_schema", synchronized_migrate_schema
    )

    def connect_and_read_version():
        connection = graph_store.GraphStore(tmp_path).connect()
        try:
            return connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(connect_and_read_version) for _ in range(2)]

    assert [future.result() for future in futures] == [1, 1]


def test_version_zero_does_not_check_tables_before_migration_lock(
    tmp_path, monkeypatch
):
    from iwiki_mcp.engine import graph_store

    graph_path = tmp_path / ".iwiki" / "graph.sqlite3"
    graph_path.parent.mkdir()
    connection = sqlite3.connect(graph_path)
    assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    connection.close()

    second_outer_check_started = threading.Event()
    winner_connected = threading.Event()
    outer_check_lock = threading.Lock()
    outer_check_count = 0
    original_user_tables = graph_store.GraphStore._user_tables

    def synchronized_user_tables(connection):
        nonlocal outer_check_count
        if connection.in_transaction:
            return original_user_tables(connection)
        with outer_check_lock:
            outer_check_count += 1
            is_winner = outer_check_count == 1
        if is_winner:
            assert second_outer_check_started.wait(timeout=5)
        else:
            second_outer_check_started.set()
            assert winner_connected.wait(timeout=5)
        return original_user_tables(connection)

    monkeypatch.setattr(
        graph_store.GraphStore, "_user_tables", staticmethod(synchronized_user_tables)
    )

    def connect_and_read_version():
        connection = graph_store.GraphStore(tmp_path).connect()
        winner_connected.set()
        try:
            return connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(connect_and_read_version) for _ in range(2)]

    assert [future.result() for future in futures] == [1, 1]


class _PragmaResult:
    def __init__(self, value):
        self.value = value

    def fetchone(self):
        return (self.value,)


class _WalRetryConnection:
    def __init__(self, locked_attempts):
        self.locked_attempts = locked_attempts
        self.journal_attempts = 0
        self.row_factory = None
        self.statements = []

    def execute(self, statement):
        self.statements.append(statement)
        if statement == "PRAGMA journal_mode = WAL":
            self.journal_attempts += 1
            if self.journal_attempts <= self.locked_attempts:
                error = sqlite3.OperationalError("database is locked")
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise error
            return _PragmaResult("wal")
        return _PragmaResult(None)


def test_graph_store_sets_busy_timeout_before_bounded_wal_retry():
    from iwiki_mcp.engine import graph_store

    connection = _WalRetryConnection(locked_attempts=2)

    graph_store.GraphStore._configure(connection)

    assert connection.statements[0] == "PRAGMA busy_timeout = 5000"
    assert connection.journal_attempts == 3


def test_version_zero_with_unknown_tables_is_rejected_without_deleting_database(
    tmp_path,
):
    from iwiki_mcp.engine.graph_store import GraphSchemaError

    graph_path = tmp_path / ".iwiki" / "graph.sqlite3"
    graph_path.parent.mkdir()
    connection = sqlite3.connect(graph_path)
    connection.execute("CREATE TABLE foreign_data(value TEXT)")
    connection.commit()
    connection.close()

    with pytest.raises(GraphSchemaError, match="^unrecognized graph schema$"):
        _graph_store_type()(tmp_path).connect()

    assert graph_path.is_file()
    connection = sqlite3.connect(graph_path)
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchone()[0] == "foreign_data"


def test_newer_schema_is_rejected_with_sanitized_error(tmp_path):
    from iwiki_mcp.engine.graph_store import GraphSchemaError

    graph_path = tmp_path / ".iwiki" / "graph.sqlite3"
    graph_path.parent.mkdir()
    connection = sqlite3.connect(graph_path)
    connection.execute("PRAGMA user_version = 2")
    connection.close()

    with pytest.raises(GraphSchemaError, match="^unsupported graph schema version$") as exc:
        _graph_store_type()(tmp_path).connect()

    assert str(graph_path) not in str(exc.value)
    assert graph_path.is_file()


def test_corrupt_database_raises_sanitized_store_error_without_replacing_file(tmp_path):
    from iwiki_mcp.engine.graph_store import GraphStoreError

    graph_path = tmp_path / ".iwiki" / "graph.sqlite3"
    graph_path.parent.mkdir()
    contents = b"not a sqlite database\x00private payload"
    graph_path.write_bytes(contents)

    with pytest.raises(GraphStoreError, match="^cannot open graph store$") as exc:
        _graph_store_type()(tmp_path).connect()

    assert str(graph_path) not in str(exc.value)
    assert "private payload" not in str(exc.value)
    assert graph_path.read_bytes() == contents


def test_transaction_commits_and_rolls_back_as_one_unit(tmp_path):
    store, connection = _open_store(tmp_path)
    connection.close()

    with store.transaction() as transaction:
        transaction.execute(
            "INSERT INTO domains VALUES (?, ?, ?, ?, ?)",
            ("kept", None, "one", "ready", "2026-08-04T00:00:00Z"),
        )

    with pytest.raises(RuntimeError, match="stop"):
        with store.transaction() as transaction:
            transaction.execute(
                "INSERT INTO domains VALUES (?, ?, ?, ?, ?)",
                ("rolled-back", None, "two", "dirty", "2026-08-04T00:00:00Z"),
            )
            raise RuntimeError("stop")

    connection = store.connect()
    domains = {
        row[0] for row in connection.execute("SELECT domain FROM domains")
    }
    assert domains == {"kept"}


def test_transaction_sanitizes_database_errors_and_rolls_back(tmp_path):
    from iwiki_mcp.engine.graph_store import GraphStoreError

    store, connection = _open_store(tmp_path)
    connection.close()

    with pytest.raises(GraphStoreError, match="^graph transaction failed$") as exc:
        with store.transaction() as transaction:
            transaction.execute(
                "INSERT INTO domains VALUES (?, ?, ?, ?, ?)",
                (
                    "rolled-back",
                    None,
                    "one",
                    "ready",
                    "2026-08-04T00:00:00Z",
                ),
            )
            transaction.execute("SELECT private_transaction_token")

    assert isinstance(exc.value.__cause__, sqlite3.DatabaseError)
    assert "private_transaction_token" in str(exc.value.__cause__)
    assert "private_transaction_token" not in str(exc.value)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        transaction.execute("SELECT 1")

    connection = store.connect()
    assert connection.execute("SELECT count(*) FROM domains").fetchone()[0] == 0
    connection.close()


def test_read_snapshot_is_read_only_and_uses_immutable_rows(tmp_path):
    from iwiki_mcp.engine.graph_store import GraphStoreError

    store, connection = _open_store(tmp_path)
    connection.execute(
        "INSERT INTO domains VALUES (?, ?, ?, ?, ?)",
        ("docs", None, "one", "rebuilding", "2026-08-04T00:00:00Z"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(GraphStoreError, match="^graph read failed$") as exc:
        with store.read_snapshot() as snapshot:
            row = snapshot.execute(
                "SELECT domain, state FROM domains WHERE domain = ?", ("docs",)
            ).fetchone()
            assert tuple(row) == ("docs", "rebuilding")
            with pytest.raises(TypeError):
                row[0] = "changed"
            snapshot.execute("DELETE FROM domains")

    assert isinstance(exc.value.__cause__, sqlite3.DatabaseError)


def test_read_snapshot_sanitizes_database_errors_and_closes(tmp_path):
    from iwiki_mcp.engine.graph_store import GraphStoreError

    store, connection = _open_store(tmp_path)
    connection.close()

    with pytest.raises(GraphStoreError, match="^graph read failed$") as exc:
        with store.read_snapshot() as snapshot:
            snapshot.execute("SELECT private_read_token")

    assert isinstance(exc.value.__cause__, sqlite3.DatabaseError)
    assert "private_read_token" in str(exc.value.__cause__)
    assert "private_read_token" not in str(exc.value)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        snapshot.execute("SELECT 1")


def test_read_snapshot_preserves_non_database_exceptions(tmp_path):
    store, connection = _open_store(tmp_path)
    connection.close()
    user_error = RuntimeError("user callback failed")

    with pytest.raises(RuntimeError, match="^user callback failed$") as exc:
        with store.read_snapshot() as snapshot:
            raise user_error

    assert exc.value is user_error
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        snapshot.execute("SELECT 1")


def _write_version_one_database(graph_path: Path, schema: str) -> None:
    graph_path.parent.mkdir()
    connection = sqlite3.connect(graph_path)
    connection.executescript(schema)
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()


def test_version_one_without_tables_is_rejected_and_preserved(tmp_path):
    from iwiki_mcp.engine.graph_store import GraphSchemaError

    graph_path = tmp_path / ".iwiki" / "graph.sqlite3"
    _write_version_one_database(graph_path, "")

    with pytest.raises(GraphSchemaError, match="^incompatible graph schema$"):
        _graph_store_type()(tmp_path).connect()

    connection = sqlite3.connect(graph_path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    assert connection.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
    ).fetchone()[0] == 0
    connection.close()


@pytest.mark.parametrize(
    "schema",
    [
        _VALID_V1_SCHEMA.replace(
            "link_hash TEXT NOT NULL", "link_hash BLOB NOT NULL"
        ),
        _VALID_V1_SCHEMA.replace(
            "    link_hash TEXT NOT NULL,\n    UNIQUE(domain, file)\n",
            "    link_hash TEXT NOT NULL\n",
        ),
        _VALID_V1_SCHEMA.replace(
            "    PRIMARY KEY(page_id, anchor),\n"
            "    FOREIGN KEY(page_id) REFERENCES pages(page_id) ON DELETE CASCADE\n",
            "    PRIMARY KEY(page_id, anchor)\n",
        ),
        _VALID_V1_SCHEMA.replace(
            "    FOREIGN KEY(source_page_id) REFERENCES pages(page_id) ON DELETE CASCADE\n",
            "    FOREIGN KEY(target_page_id) REFERENCES pages(page_id) ON DELETE CASCADE\n",
        ),
        _VALID_V1_SCHEMA.replace(" DEFAULT ''", ""),
        _VALID_V1_SCHEMA.replace(
            "CHECK (state IN ('ready', 'dirty', 'rebuilding'))",
            "CHECK (state IN ('ready', 'dirty'))",
        ),
        _VALID_V1_SCHEMA.replace(
            "CHECK (kind IN ('intra', 'cross'))", "CHECK (kind IN ('intra'))"
        ),
        _VALID_V1_SCHEMA.replace(
            "    indexed_at TEXT NOT NULL\n);",
            "    indexed_at TEXT NOT NULL,\n"
            "    CHECK (state != 'dirty')\n);",
        ),
        _VALID_V1_SCHEMA.replace(
            "    raw_target TEXT NOT NULL,\n",
            "    raw_target TEXT NOT NULL,\n"
            "    CHECK (kind != 'cross'),\n",
        ),
        _VALID_V1_SCHEMA.replace(
            "CREATE INDEX edges_target_idx ON edges(target_page_id);", ""
        ),
        _VALID_V1_SCHEMA.replace(
            "CREATE INDEX edges_target_idx", "CREATE UNIQUE INDEX edges_target_idx"
        ),
        _VALID_V1_SCHEMA + "CREATE TABLE unexpected(value TEXT);",
    ],
    ids=[
        "wrong-column-type",
        "missing-page-unique",
        "missing-anchor-foreign-key",
        "wrong-edge-foreign-key",
        "missing-target-anchor-default",
        "wrong-domain-state-check",
        "wrong-edge-kind-check",
        "extra-domain-state-check",
        "extra-edge-kind-check",
        "missing-target-index",
        "unique-target-index",
        "extra-table",
    ],
)
def test_malformed_version_one_schema_is_rejected(tmp_path, schema):
    from iwiki_mcp.engine.graph_store import GraphSchemaError

    graph_path = tmp_path / ".iwiki" / "graph.sqlite3"
    _write_version_one_database(graph_path, schema)

    with pytest.raises(GraphSchemaError, match="^incompatible graph schema$"):
        _graph_store_type()(tmp_path).connect()

    assert graph_path.is_file()


def test_failing_known_migration_rolls_back_schema_and_version(tmp_path, monkeypatch):
    from iwiki_mcp.engine import graph_store

    graph_path = tmp_path / ".iwiki" / "graph.sqlite3"
    graph_path.parent.mkdir()
    sqlite3.connect(graph_path).close()
    monkeypatch.setitem(
        graph_store._MIGRATIONS,
        0,
        (
            1,
            (
                "CREATE TABLE partial(value TEXT)",
                "THIS IS NOT VALID SQL",
            ),
        ),
    )

    with pytest.raises(graph_store.GraphStoreError, match="^cannot open graph store$"):
        graph_store.GraphStore(tmp_path).connect()

    connection = sqlite3.connect(graph_path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    assert connection.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
    ).fetchone()[0] == 0
    connection.close()
