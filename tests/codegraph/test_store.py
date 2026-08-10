from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import stat

import pytest

from iwiki_mcp.codegraph.schema import (
    BUSY_TIMEOUT_MS,
    INDEXES,
    SCHEMA_VERSION,
    TABLES,
    configure,
    validate_integrity,
)
from iwiki_mcp.codegraph.store import (
    CodeGraphSchemaError,
    CodeGraphStore,
    CodeGraphStoreError,
)
from iwiki_mcp.engine.graph_store import GraphStore


EXPECTED_INDEXES = {
    "idx_files_repository_path",
    "idx_files_content_hash",
    "idx_symbols_file",
    "idx_symbols_qualified",
    "idx_symbols_local",
    "idx_symbols_kind",
    "idx_relations_source_type",
    "idx_relations_target_type",
    "idx_relations_reference",
    "idx_wiki_links_page",
    "idx_wiki_links_symbol",
    "idx_wiki_links_file",
}


def snapshot_with_symbol_file_and_wiki_links(
    *, repository_id="backend", revision="revision-1", state="ready"
):
    file_id = f"file-{repository_id}"
    symbol_id = f"symbol-{repository_id}"
    return {
        "repositories": (
            {
                "repository_id": repository_id,
                "root_path": "/diagnostic/project",
                "git_remote": None,
                "git_commit": "commit-1",
                "source_fingerprint": "source-1",
                "config_fingerprint": "config-1",
                "parser_fingerprint": "parser-1",
                "revision": revision,
                "state": state,
                "indexed_at": "2026-08-10T00:00:00Z",
            },
        ),
        "files": (
            {
                "file_id": file_id,
                "repository_id": repository_id,
                "path": "pkg/module.py",
                "language": "python",
                "content_hash": "file-hash",
                "parser_version": "python-v1",
                "size_bytes": 64,
            },
        ),
        "symbols": (
            {
                "symbol_id": symbol_id,
                "file_id": file_id,
                "kind": "function",
                "qualified_name": "pkg.module.run",
                "local_name": "run",
                "start_line": 2,
                "end_line": 3,
                "start_byte": 10,
                "end_byte": 30,
                "signature": "()",
                "visibility": "public",
                "content_hash": "symbol-hash",
                "metadata_json": "{}",
            },
        ),
        "relations": (
            {
                "relation_id": f"relation-{repository_id}",
                "source_symbol_id": symbol_id,
                "source_file_id": file_id,
                "target_symbol_id": symbol_id,
                "target_reference": None,
                "relation_type": "calls",
                "source_line": 3,
                "confidence": 1.0,
                "resolution_state": "resolved",
                "metadata_json": "{}",
            },
        ),
        "wiki_code_links": (
            {
                "link_id": f"link-symbol-{repository_id}",
                "domain": "wiki",
                "page_id": "page.md",
                "symbol_id": symbol_id,
                "file_id": None,
                "selector_kind": "symbol",
                "relation_type": "documents",
                "confidence": 1.0,
                "source": "frontmatter",
            },
            {
                "link_id": f"link-file-{repository_id}",
                "domain": "wiki",
                "page_id": "page.md",
                "symbol_id": None,
                "file_id": file_id,
                "selector_kind": "file",
                "relation_type": "documents",
                "confidence": 1.0,
                "source": "frontmatter",
            },
        ),
    }


def test_schema_v1_has_exact_tables_indexes_and_configuration(tmp_path):
    store = CodeGraphStore(tmp_path / "code-backend.sqlite3")

    with closing(store.connect()) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert tables == set(TABLES) == {
            "repositories", "files", "symbols", "relations", "wiki_code_links"
        }
        assert indexes == set(INDEXES) == EXPECTED_INDEXES
        assert connection.execute("PRAGMA user_version").fetchone() == (
            SCHEMA_VERSION,
        )
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone() == (
            BUSY_TIMEOUT_MS,
        )
        validate_integrity(connection)


def test_file_and_repository_deletes_cascade_without_touching_wiki_store(tmp_path):
    store = CodeGraphStore(tmp_path / "code-backend.sqlite3")
    snapshot = snapshot_with_symbol_file_and_wiki_links()
    store.insert_snapshot(snapshot)

    with closing(store.connect()) as connection:
        connection.execute("DELETE FROM files WHERE file_id = 'file-backend'")
        assert connection.execute(
            "SELECT count(*) FROM wiki_code_links"
        ).fetchone() == (0,)

    store.delete_repository("backend")
    with closing(store.connect()) as connection:
        for table in ("files", "symbols", "relations", "wiki_code_links"):
            assert connection.execute(
                f"SELECT count(*) FROM {table}"
            ).fetchone() == (0,)

    wiki_store = GraphStore(tmp_path / "wiki")
    with closing(wiki_store.connect()) as connection:
        wiki_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert wiki_tables == {"domains", "pages", "anchors", "edges"}


def test_missing_inspection_and_corrupt_cache_quarantine(tmp_path):
    path = tmp_path / "code-backend.sqlite3"
    store = CodeGraphStore(path)
    assert store.inspect_state() == "missing"

    path.write_bytes(b"not sqlite")
    assert store.inspect_state() == "missing"
    quarantined = store.quarantine_corrupt()

    assert quarantined.parent == path.parent
    assert quarantined.name.startswith("code-backend.sqlite3.corrupt-")
    assert quarantined.read_bytes() == b"not sqlite"
    assert not path.exists()


def test_corrupt_cache_quarantine_never_overwrites_existing_diagnostic(tmp_path):
    path = tmp_path / "code-backend.sqlite3"
    payload = b"not sqlite"
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()[:16]
    existing = path.with_name(f"{path.name}.corrupt-{digest}")
    existing.write_bytes(b"existing diagnostic")

    quarantined = CodeGraphStore(path).quarantine_corrupt()

    assert existing.read_bytes() == b"existing diagnostic"
    assert quarantined != existing
    assert quarantined.read_bytes() == payload
    assert not path.exists()


def test_quarantine_sidecar_failure_best_effort_restores_canonical(
    tmp_path, monkeypatch
):
    path = tmp_path / "code-backend.sqlite3"
    payload = b"not sqlite"
    path.write_bytes(payload)
    wal = path.with_name(f"{path.name}-wal")
    wal.write_bytes(b"wal bytes")
    real_replace = os.replace

    def fail_wal_move(source, target):
        if Path(source) == wal:
            raise OSError("secret sidecar diagnostic")
        return real_replace(source, target)

    monkeypatch.setattr("iwiki_mcp.codegraph.store.os.replace", fail_wal_move)

    with pytest.raises(CodeGraphStoreError) as raised:
        CodeGraphStore(path).quarantine_corrupt()

    assert str(raised.value) == "cannot quarantine code graph cache"
    assert "secret" not in str(raised.value)
    assert path.read_bytes() == payload
    assert wal.read_bytes() == b"wal bytes"


def test_quarantine_reservation_failure_removes_owned_placeholders(
    tmp_path, monkeypatch
):
    path = tmp_path / "code-backend.sqlite3"
    payload = b"not sqlite"
    path.write_bytes(payload)
    real_open = os.open
    calls = 0

    def fail_second_reservation(target, flags, mode):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("secret reservation diagnostic")
        return real_open(target, flags, mode)

    monkeypatch.setattr("iwiki_mcp.codegraph.store.os.open", fail_second_reservation)

    with pytest.raises(CodeGraphStoreError) as raised:
        CodeGraphStore(path).quarantine_corrupt()

    assert str(raised.value) == "cannot quarantine code graph cache"
    assert "secret" not in str(raised.value)
    assert path.read_bytes() == payload
    assert list(tmp_path.glob("code-backend.sqlite3.corrupt-*")) == []


def test_incompatible_schema_and_exact_ddl_mismatch_are_rejected(tmp_path):
    incompatible = tmp_path / "incompatible.sqlite3"
    with closing(sqlite3.connect(incompatible)) as connection:
        connection.execute(
            "CREATE TABLE repositories (repository_id TEXT PRIMARY KEY)"
        )
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()

    with pytest.raises(CodeGraphSchemaError, match="incompatible code graph schema"):
        CodeGraphStore(incompatible).connect()

    mismatched = tmp_path / "mismatched.sqlite3"
    mismatch_store = CodeGraphStore(mismatched)
    with closing(mismatch_store.connect()) as connection:
        connection.execute("DROP INDEX idx_symbols_qualified")
        connection.execute(
            "CREATE INDEX idx_symbols_qualified ON symbols(local_name)"
        )
        connection.commit()

    with pytest.raises(CodeGraphSchemaError, match="incompatible code graph schema"):
        mismatch_store.connect()


def test_integrity_failure_messages_are_sanitized(tmp_path):
    path = tmp_path / "foreign-key.sqlite3"
    store = CodeGraphStore(path)
    with closing(store.connect()):
        pass
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "INSERT INTO files VALUES "
            "('secret-file', 'missing-repository', 'a.py', 'python', "
            "'hash', 'v1', 1)"
        )
        connection.commit()
        with pytest.raises(CodeGraphStoreError) as raised:
            validate_integrity(connection)

    assert str(raised.value) == "code graph foreign key check failed"
    assert "secret-file" not in str(raised.value)

    class IntegrityFailureConnection:
        def execute(self, statement):
            if statement == "PRAGMA foreign_key_check":
                return EmptyResult()
            raise sqlite3.DatabaseError("database disk image contains secret-row")

    class EmptyResult:
        @staticmethod
        def fetchall():
            return []

    with pytest.raises(CodeGraphStoreError) as raised:
        validate_integrity(IntegrityFailureConnection())
    assert str(raised.value) == "code graph integrity check failed"
    assert "secret-row" not in str(raised.value)


def test_configure_rejects_non_wal_journal_mode():
    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def execute(self, statement):
            if statement == "PRAGMA journal_mode = WAL":
                return Result(("delete",))
            return Result()

    with pytest.raises(CodeGraphStoreError) as raised:
        configure(Connection())

    assert str(raised.value) == "cannot enable code graph WAL mode"


def test_lifecycle_metadata_and_stable_id_ordering(tmp_path):
    store = CodeGraphStore(tmp_path / "states.sqlite3")
    for index, state in enumerate(("ready", "dirty", "rebuilding", "failed")):
        store.insert_snapshot(
            snapshot_with_symbol_file_and_wiki_links(
                repository_id=f"repo-{state}",
                revision=f"revision-{index}",
                state=state,
            )
        )

    for state in ("ready", "dirty", "rebuilding", "failed"):
        assert store.inspect_state(f"repo-{state}") == state
    assert store.inspect_state("absent") == "missing"

    metadata = store.reconstruct_metadata("repo-dirty")
    assert metadata["repository_id"] == "repo-dirty"
    assert metadata["revision"] == "revision-1"
    assert metadata["state"] == "dirty"
    assert metadata["schema_version"] == SCHEMA_VERSION

    repository_ids = [row["repository_id"] for row in store.stable_rows("repositories")]
    file_ids = [row["file_id"] for row in store.stable_rows("files")]
    symbol_ids = [row["symbol_id"] for row in store.stable_rows("symbols")]
    relation_ids = [row["relation_id"] for row in store.stable_rows("relations")]
    link_ids = [row["link_id"] for row in store.stable_rows("wiki_code_links")]
    assert repository_ids == sorted(repository_ids)
    assert file_ids == sorted(file_ids)
    assert symbol_ids == sorted(symbol_ids)
    assert relation_ids == sorted(relation_ids)
    assert link_ids == sorted(link_ids)


def test_atomic_staging_publication_preserves_prior_until_validation(tmp_path):
    canonical = tmp_path / "code-backend.sqlite3"
    store = CodeGraphStore(canonical)
    store.insert_snapshot(snapshot_with_symbol_file_and_wiki_links(revision="old"))

    invalid_staging = store.create_staging_path()
    with closing(sqlite3.connect(invalid_staging)) as connection:
        connection.execute("CREATE TABLE wrong (id TEXT PRIMARY KEY)")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()

    before = store.reconstruct_metadata("backend")
    with pytest.raises(CodeGraphSchemaError):
        store.publish_staging(
            invalid_staging,
            repository_id="backend",
            expected_revision="new",
        )
    assert store.reconstruct_metadata("backend") == before
    assert not invalid_staging.exists()

    valid_staging = store.create_staging_path()
    CodeGraphStore(valid_staging).insert_snapshot(
        snapshot_with_symbol_file_and_wiki_links(revision="new")
    )
    store.publish_staging(
        valid_staging,
        repository_id="backend",
        expected_revision="new",
    )

    assert not valid_staging.exists()
    assert store.reconstruct_metadata("backend")["revision"] == "new"


def test_publication_rejects_external_incompatible_database_without_deleting_it(
    tmp_path,
):
    canonical = tmp_path / "cache" / "code-backend.sqlite3"
    store = CodeGraphStore(canonical)
    store.insert_snapshot(snapshot_with_symbol_file_and_wiki_links(revision="old"))
    external = tmp_path / "external.sqlite3"
    external.write_bytes(b"not sqlite")

    with pytest.raises(CodeGraphStoreError, match="invalid code graph staging"):
        store.publish_staging(
            external,
            repository_id="backend",
            expected_revision="external",
        )

    assert external.read_bytes() == b"not sqlite"
    assert store.reconstruct_metadata("backend")["revision"] == "old"


def test_publication_rejects_external_valid_database_without_moving_it(tmp_path):
    canonical = tmp_path / "cache" / "code-backend.sqlite3"
    store = CodeGraphStore(canonical)
    store.insert_snapshot(snapshot_with_symbol_file_and_wiki_links(revision="old"))
    external = tmp_path / "external.sqlite3"
    CodeGraphStore(external).insert_snapshot(
        snapshot_with_symbol_file_and_wiki_links(revision="external")
    )

    with pytest.raises(CodeGraphStoreError, match="invalid code graph staging"):
        store.publish_staging(
            external,
            repository_id="backend",
            expected_revision="external",
        )

    assert external.is_file()
    assert CodeGraphStore(external).reconstruct_metadata("backend")["revision"] == (
        "external"
    )
    assert store.reconstruct_metadata("backend")["revision"] == "old"


def test_publication_rejects_owned_symlink_without_touching_target(tmp_path):
    canonical = tmp_path / "cache" / "code-backend.sqlite3"
    store = CodeGraphStore(canonical)
    store.insert_snapshot(snapshot_with_symbol_file_and_wiki_links(revision="old"))
    external = tmp_path / "external.sqlite3"
    CodeGraphStore(external).insert_snapshot(
        snapshot_with_symbol_file_and_wiki_links(revision="external")
    )
    staging = canonical.with_name(f"{canonical.name}.staging-owned")
    staging.symlink_to(external)

    with pytest.raises(CodeGraphStoreError, match="invalid code graph staging"):
        store.publish_staging(
            staging,
            repository_id="backend",
            expected_revision="external",
        )

    assert staging.is_symlink()
    assert external.is_file()
    assert CodeGraphStore(external).reconstruct_metadata("backend")["revision"] == (
        "external"
    )
    assert store.reconstruct_metadata("backend")["revision"] == "old"


def test_publication_rejects_hard_link_to_canonical_as_not_owned(tmp_path):
    canonical = tmp_path / "code-backend.sqlite3"
    store = CodeGraphStore(canonical)
    store.insert_snapshot(snapshot_with_symbol_file_and_wiki_links(revision="old"))
    staging = canonical.with_name(f"{canonical.name}.staging-hard-link")
    os.link(canonical, staging)

    with pytest.raises(CodeGraphStoreError, match="invalid code graph staging"):
        store.publish_staging(
            staging,
            repository_id="backend",
            expected_revision="old",
        )

    assert staging.samefile(canonical)
    assert store.reconstruct_metadata("backend")["revision"] == "old"


def test_publication_rejects_prefixed_hard_link_to_unrelated_database(tmp_path):
    canonical = tmp_path / "cache" / "code-backend.sqlite3"
    store = CodeGraphStore(canonical)
    store.insert_snapshot(snapshot_with_symbol_file_and_wiki_links(revision="old"))
    external = tmp_path / "external.sqlite3"
    CodeGraphStore(external).insert_snapshot(
        snapshot_with_symbol_file_and_wiki_links(revision="external")
    )
    staging = canonical.with_name(f"{canonical.name}.staging-external-hard-link")
    os.link(external, staging)

    with pytest.raises(CodeGraphStoreError, match="invalid code graph staging"):
        store.publish_staging(
            staging,
            repository_id="backend",
            expected_revision="external",
        )

    assert staging.samefile(external)
    assert CodeGraphStore(external).reconstruct_metadata("backend")["revision"] == (
        "external"
    )
    assert store.reconstruct_metadata("backend")["revision"] == "old"


def test_publication_rejects_registered_staging_with_extra_hard_link(tmp_path):
    canonical = tmp_path / "code-backend.sqlite3"
    store = CodeGraphStore(canonical)
    store.insert_snapshot(snapshot_with_symbol_file_and_wiki_links(revision="old"))
    staging = store.create_staging_path()
    CodeGraphStore(staging).insert_snapshot(
        snapshot_with_symbol_file_and_wiki_links(revision="new")
    )
    retained_link = tmp_path / "retained-staging.sqlite3"
    os.link(staging, retained_link)

    with pytest.raises(CodeGraphStoreError, match="invalid code graph staging"):
        store.publish_staging(
            staging,
            repository_id="backend",
            expected_revision="new",
        )

    assert not staging.exists()
    assert CodeGraphStore(retained_link).reconstruct_metadata("backend")["revision"] == (
        "new"
    )
    assert store.reconstruct_metadata("backend")["revision"] == "old"


def test_staging_reservation_uses_private_mode_700_directory(tmp_path):
    canonical = tmp_path / "code-backend.sqlite3"
    store = CodeGraphStore(canonical)

    staging = store.create_staging_path()

    assert staging.parent.parent == canonical.parent
    assert staging.parent.name.startswith(f"{canonical.name}.staging-")
    assert stat.S_IMODE(staging.parent.stat().st_mode) == 0o700
    store.discard_staging(staging)
    assert not staging.parent.exists()


def test_discard_staging_removes_registered_database_and_sidecars(tmp_path):
    canonical = tmp_path / "code-backend.sqlite3"
    store = CodeGraphStore(canonical)
    staging = store.create_staging_path()
    staging.write_bytes(b"aborted build")
    wal = Path(f"{staging}-wal")
    shm = Path(f"{staging}-shm")
    wal.write_bytes(b"wal")
    shm.write_bytes(b"shm")
    private_directory = staging.parent

    store.discard_staging(staging)

    assert not staging.exists()
    assert not wal.exists()
    assert not shm.exists()
    assert not private_directory.exists()


def test_discard_staging_does_not_unlink_swapped_replacement(tmp_path):
    canonical = tmp_path / "code-backend.sqlite3"
    store = CodeGraphStore(canonical)
    store.insert_snapshot(snapshot_with_symbol_file_and_wiki_links(revision="old"))
    staging = store.create_staging_path()
    staging.write_bytes(b"original staging")
    replacement = tmp_path / "replacement.sqlite3"
    replacement.write_bytes(b"external replacement")
    os.replace(replacement, staging)

    store.discard_staging(staging)

    assert staging.read_bytes() == b"external replacement"
    assert staging.parent.exists()
    assert store.reconstruct_metadata("backend")["revision"] == "old"
    store.discard_staging(staging)
    assert staging.read_bytes() == b"external replacement"


def test_publication_does_not_auto_create_schema_in_empty_staging(
    tmp_path, monkeypatch
):
    canonical = tmp_path / "code-backend.sqlite3"
    store = CodeGraphStore(canonical)
    store.insert_snapshot(snapshot_with_symbol_file_and_wiki_links(revision="old"))
    staging = store.create_staging_path()
    monkeypatch.setattr(
        "iwiki_mcp.codegraph.store.create_schema",
        lambda connection: pytest.fail("publication auto-created staging schema"),
    )

    with pytest.raises(CodeGraphStoreError):
        store.publish_staging(
            staging,
            repository_id="backend",
            expected_revision="new",
        )

    assert store.reconstruct_metadata("backend")["revision"] == "old"


def test_publication_rejects_existing_empty_schema(tmp_path):
    canonical = tmp_path / "code-backend.sqlite3"
    store = CodeGraphStore(canonical)
    store.insert_snapshot(snapshot_with_symbol_file_and_wiki_links(revision="old"))
    staging = store.create_staging_path()
    with closing(sqlite3.connect(staging)) as connection:
        connection.execute("CREATE TABLE temporary (id TEXT)")
        connection.execute("DROP TABLE temporary")
        connection.commit()

    with pytest.raises(CodeGraphSchemaError):
        store.publish_staging(
            staging,
            repository_id="backend",
            expected_revision="new",
        )

    assert store.reconstruct_metadata("backend")["revision"] == "old"


@pytest.mark.parametrize("state", ["dirty", "rebuilding", "failed"])
def test_publication_rejects_nonready_snapshot(tmp_path, state):
    canonical = tmp_path / "code-backend.sqlite3"
    store = CodeGraphStore(canonical)
    store.insert_snapshot(snapshot_with_symbol_file_and_wiki_links(revision="old"))
    staging = store.create_staging_path()
    CodeGraphStore(staging).insert_snapshot(
        snapshot_with_symbol_file_and_wiki_links(revision="new", state=state)
    )

    with pytest.raises(CodeGraphStoreError, match="staging snapshot mismatch"):
        store.publish_staging(
            staging,
            repository_id="backend",
            expected_revision="new",
        )

    assert store.reconstruct_metadata("backend")["revision"] == "old"


@pytest.mark.parametrize(
    ("staged_repository", "staged_revision", "expected_repository", "expected_revision"),
    [
        ("other", "new", "backend", "new"),
        ("backend", "wrong", "backend", "new"),
    ],
)
def test_publication_rejects_wrong_snapshot_identity(
    tmp_path,
    staged_repository,
    staged_revision,
    expected_repository,
    expected_revision,
):
    canonical = tmp_path / "code-backend.sqlite3"
    store = CodeGraphStore(canonical)
    store.insert_snapshot(snapshot_with_symbol_file_and_wiki_links(revision="old"))
    staging = store.create_staging_path()
    CodeGraphStore(staging).insert_snapshot(
        snapshot_with_symbol_file_and_wiki_links(
            repository_id=staged_repository,
            revision=staged_revision,
        )
    )

    with pytest.raises(CodeGraphStoreError, match="staging snapshot mismatch"):
        store.publish_staging(
            staging,
            repository_id=expected_repository,
            expected_revision=expected_revision,
        )

    assert store.reconstruct_metadata("backend")["revision"] == "old"


def test_publication_rejects_multiple_repository_rows(tmp_path):
    canonical = tmp_path / "code-backend.sqlite3"
    store = CodeGraphStore(canonical)
    store.insert_snapshot(snapshot_with_symbol_file_and_wiki_links(revision="old"))
    staging = store.create_staging_path()
    staging_store = CodeGraphStore(staging)
    staging_store.insert_snapshot(
        snapshot_with_symbol_file_and_wiki_links(revision="new")
    )
    staging_store.insert_snapshot(
        snapshot_with_symbol_file_and_wiki_links(
            repository_id="other",
            revision="other",
        )
    )

    with pytest.raises(CodeGraphStoreError, match="staging snapshot mismatch"):
        store.publish_staging(
            staging,
            repository_id="backend",
            expected_revision="new",
        )

    assert store.reconstruct_metadata("backend")["revision"] == "old"


def test_failed_owned_staging_validation_removes_its_sidecars(tmp_path):
    canonical = tmp_path / "code-backend.sqlite3"
    store = CodeGraphStore(canonical)
    store.insert_snapshot(snapshot_with_symbol_file_and_wiki_links(revision="old"))
    staging = store.create_staging_path()
    staging.write_bytes(b"not sqlite")
    wal = staging.with_name(f"{staging.name}-wal")
    shm = staging.with_name(f"{staging.name}-shm")
    wal.write_bytes(b"owned wal")
    shm.write_bytes(b"owned shm")

    with pytest.raises(CodeGraphStoreError):
        store.publish_staging(
            staging,
            repository_id="backend",
            expected_revision="new",
        )

    assert not staging.exists()
    assert not wal.exists()
    assert not shm.exists()
    assert store.reconstruct_metadata("backend")["revision"] == "old"


def test_replace_failure_keeps_canonical_and_returns_sanitized_error(
    tmp_path, monkeypatch
):
    canonical = tmp_path / "code-backend.sqlite3"
    store = CodeGraphStore(canonical)
    store.insert_snapshot(snapshot_with_symbol_file_and_wiki_links(revision="old"))
    staging = store.create_staging_path()
    CodeGraphStore(staging).insert_snapshot(
        snapshot_with_symbol_file_and_wiki_links(revision="new")
    )

    def fail_replace(source, target, **kwargs):
        raise OSError("secret replacement diagnostic")

    monkeypatch.setattr("iwiki_mcp.codegraph.store.os.replace", fail_replace)

    with pytest.raises(CodeGraphStoreError) as raised:
        store.publish_staging(
            staging,
            repository_id="backend",
            expected_revision="new",
        )

    assert str(raised.value) == "cannot publish code graph staging"
    assert "secret" not in str(raised.value)
    assert not staging.exists()
    assert store.reconstruct_metadata("backend")["revision"] == "old"


def test_publication_recheck_rejects_reserved_path_swapped_to_external_hardlink(
    tmp_path, monkeypatch
):
    canonical = tmp_path / "cache" / "code-backend.sqlite3"
    store = CodeGraphStore(canonical)
    store.insert_snapshot(snapshot_with_symbol_file_and_wiki_links(revision="old"))
    staging = store.create_staging_path()
    CodeGraphStore(staging).insert_snapshot(
        snapshot_with_symbol_file_and_wiki_links(revision="new")
    )
    external = tmp_path / "external.sqlite3"
    CodeGraphStore(external).insert_snapshot(
        snapshot_with_symbol_file_and_wiki_links(revision="external")
    )
    replacement_link = tmp_path / "replacement-link.sqlite3"
    os.link(external, replacement_link)
    original_validate = store._validate_staging_identity
    calls = 0

    def swap_before_second_validation(path, identity):
        nonlocal calls
        calls += 1
        if calls == 2:
            os.replace(replacement_link, path)
        return original_validate(path, identity)

    monkeypatch.setattr(
        store,
        "_validate_staging_identity",
        swap_before_second_validation,
    )

    with pytest.raises(CodeGraphStoreError, match="invalid code graph staging"):
        store.publish_staging(
            staging,
            repository_id="backend",
            expected_revision="new",
        )

    assert calls == 2
    assert store.reconstruct_metadata("backend")["revision"] == "old"
    assert staging.exists()
    assert staging.samefile(external)
    assert CodeGraphStore(external).reconstruct_metadata("backend")["revision"] == (
        "external"
    )
