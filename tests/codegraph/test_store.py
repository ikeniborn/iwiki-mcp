from contextlib import closing, contextmanager
import hashlib
import os
from pathlib import Path
import sqlite3
import stat

import pytest

from iwiki_mcp.codegraph.models import NORMALIZER_VERSION, UNICODE_DATA_VERSION
from iwiki_mcp.codegraph.schema import (
    BUSY_TIMEOUT_MS,
    INDEXES,
    SCHEMA_VERSION,
    TABLES,
    configure,
    validate_integrity,
    validate_schema,
)
from iwiki_mcp.codegraph.store import (
    CodeGraphSchemaError,
    CodeGraphStore,
    CodeGraphStoreError,
)
from iwiki_mcp.engine.graph_store import GraphStore


EXPECTED_INDEXES = {
    "idx_files_repository_path",
    "idx_files_repository_local",
    "idx_files_content_hash",
    "idx_files_repository_module_key",
    "idx_files_repository_module_qualified",
    "idx_files_repository_module_local",
    "idx_symbols_file",
    "idx_symbols_qualified",
    "idx_symbols_local",
    "idx_symbols_kind",
    "idx_relations_source_file_type",
    "idx_relations_source_module_type",
    "idx_relations_source_symbol_type",
    "idx_relations_target_module_type",
    "idx_relations_target_symbol_type",
    "idx_relations_reference",
    "idx_relations_explicit_alias",
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
                "normalizer_version": NORMALIZER_VERSION,
                "unicode_data_version": UNICODE_DATA_VERSION,
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
                "path_casefold": None,
                "file_local_name": "module.py",
                "file_name_tokens_casefold": "\x1fmodule\x1fpy\x1f",
                "language": "python",
                "content_hash": "file-hash",
                "parser_version": "python-v1",
                "size_bytes": 64,
                "start_line": 1,
                "end_line": 4,
                "start_byte": 0,
                "end_byte": 64,
                "module_key": "pkg/module.py",
                "module_id": f"module-{repository_id}",
                "module_qualified_name": "pkg.module",
                "module_local_name": "module",
                "module_name_tokens_casefold": "\x1fmodule\x1fpkg\x1f",
            },
        ),
        "symbols": (
            {
                "symbol_id": symbol_id,
                "file_id": file_id,
                "kind": "function",
                "qualified_name": "pkg.module.run",
                "local_name": "run",
                "name_tokens_casefold": "\x1fmodule\x1fpkg\x1frun\x1f",
                "start_line": 2,
                "end_line": 3,
                "start_byte": 10,
                "end_byte": 30,
                "signature": "()",
                "signature_casefold": None,
                "visibility": "public",
                "content_hash": "symbol-hash",
                "metadata_json": "{}",
            },
        ),
        "relations": (
            {
                "relation_id": f"relation-{repository_id}",
                "source_file_id": file_id,
                "source_module_id": None,
                "source_symbol_id": symbol_id,
                "target_module_id": None,
                "target_symbol_id": symbol_id,
                "target_reference": None,
                "relation_type": "CALLS",
                "source_start_line": 3,
                "source_end_line": 3,
                "source_start_byte": 20,
                "source_end_byte": 24,
                "binding_name": None,
                "binding_kind": None,
                "binding_name_tokens_casefold": None,
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


def test_schema_v2_has_exact_five_tables_and_twenty_indexes(tmp_path):
    store = CodeGraphStore(tmp_path / "code-backend.sqlite3")

    with closing(store.connect()) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        explicit_indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND sql IS NOT NULL"
            )
        }
        implicit_unique_indexes = {
            (
                table,
                tuple(
                    column[2]
                    for column in connection.execute(
                        f"PRAGMA index_info({index_name})"
                    )
                ),
            )
            for table in TABLES
            for _, index_name, unique, origin, _ in connection.execute(
                f"PRAGMA index_list({table})"
            )
            if unique and origin in {"pk", "u"}
        }
        assert tables == set(TABLES) == {
            "repositories", "files", "symbols", "relations", "wiki_code_links"
        }
        assert explicit_indexes == set(INDEXES) == EXPECTED_INDEXES
        assert len(explicit_indexes) == 20
        assert implicit_unique_indexes == {
            ("repositories", ("repository_id",)),
            ("files", ("file_id",)),
            ("files", ("module_id",)),
            ("files", ("repository_id", "path")),
            ("symbols", ("symbol_id",)),
            ("symbols", ("file_id", "qualified_name", "start_line")),
            ("relations", ("relation_id",)),
            ("wiki_code_links", ("link_id",)),
        }
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone() == (
            BUSY_TIMEOUT_MS,
        )
        validate_integrity(connection)


def test_schema_v1_is_incompatible_not_migrated(tmp_path):
    path = tmp_path / "schema-v1.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "CREATE TABLE repositories (repository_id TEXT PRIMARY KEY)"
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
        before_journal_mode = connection.execute(
            "PRAGMA journal_mode"
        ).fetchone()
        before = tuple(connection.iterdump())
    before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    before_sidecars = {
        candidate.name for candidate in tmp_path.glob(f"{path.name}-*")
    }

    store = CodeGraphStore(path)

    compatibility = store.inspect_compatibility()
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute("PRAGMA journal_mode").fetchone() == (
            before_journal_mode
        )
        assert tuple(connection.iterdump()) == before
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash
    assert {
        candidate.name for candidate in tmp_path.glob(f"{path.name}-*")
    } == before_sidecars
    assert compatibility == "incompatible"

    compatible = CodeGraphStore(tmp_path / "schema-v2.sqlite3")
    with closing(compatible.connect()):
        pass
    assert compatible.inspect_compatibility() == "compatible"


def test_validate_schema_rejects_missing_implicit_unique_index(tmp_path):
    store = CodeGraphStore(tmp_path / "missing-autoindex.sqlite3")
    with closing(store.connect()) as connection:
        module_index = next(
            row[1]
            for row in connection.execute("PRAGMA index_list(files)")
            if tuple(
                column[2]
                for column in connection.execute(
                    f"PRAGMA index_info({row[1]})"
                )
            ) == ("module_id",)
        )
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "DELETE FROM sqlite_master WHERE type = 'index' AND name = ?",
            (module_index,),
        )
        connection.execute("PRAGMA writable_schema = OFF")

        with pytest.raises(
            CodeGraphSchemaError,
            match="incompatible code graph schema",
        ):
            validate_schema(connection)


@pytest.mark.parametrize(
    ("column", "value"),
    [("source_end_line", 2), ("source_end_byte", 19)],
)
def test_schema_v2_rejects_invalid_relation_ranges(tmp_path, column, value):
    store = CodeGraphStore(tmp_path / "invalid-range.sqlite3")
    store.insert_snapshot(snapshot_with_symbol_file_and_wiki_links())

    with closing(store.connect()) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"UPDATE relations SET {column} = ? WHERE relation_id = ?",
                (value, "relation-backend"),
            )


def test_canonical_verification_reopens_and_checks_revision(tmp_path):
    store = CodeGraphStore(tmp_path / "canonical.sqlite3")
    store.insert_snapshot(snapshot_with_symbol_file_and_wiki_links())

    store.verify_canonical("backend", "revision-1")
    with closing(store.connect()) as connection:
        connection.execute(
            "UPDATE repositories SET revision = 'changed' "
            "WHERE repository_id = 'backend'"
        )
        connection.commit()

    with pytest.raises(CodeGraphStoreError, match="canonical verification failed"):
        store.verify_canonical("backend", "revision-1")


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
            "INSERT INTO files ("
            "file_id, repository_id, path, file_local_name, "
            "file_name_tokens_casefold, language, content_hash, parser_version, "
            "size_bytes, start_line, end_line, start_byte, end_byte, module_key"
            ") VALUES ("
            "'secret-file', 'missing-repository', 'a.py', 'a.py', "
            "char(31) || 'a' || char(31) || 'py' || char(31), 'python', "
            "'hash', 'v1', 1, 1, 1, 0, 1, 'a.py'"
            ")"
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


def test_integrity_rejects_relation_source_from_another_file(tmp_path):
    path = tmp_path / "relation-provenance.sqlite3"
    store = CodeGraphStore(path)
    snapshot = snapshot_with_symbol_file_and_wiki_links()
    other_file = dict(snapshot["files"][0])
    other_file.update({
        "file_id": "file-other",
        "path": "pkg/other.py",
        "file_local_name": "other.py",
        "file_name_tokens_casefold": "\x1fother\x1fpy\x1f",
        "module_key": "pkg/other.py",
        "module_id": "module-other",
        "module_qualified_name": "pkg.other",
        "module_local_name": "other",
        "module_name_tokens_casefold": "\x1fother\x1fpkg\x1f",
    })
    other_symbol = dict(snapshot["symbols"][0])
    other_symbol.update({
        "symbol_id": "symbol-other",
        "file_id": "file-other",
        "qualified_name": "pkg.other.run",
    })
    relation = dict(snapshot["relations"][0])
    relation["source_symbol_id"] = "symbol-other"
    snapshot["files"] += (other_file,)
    snapshot["symbols"] += (other_symbol,)
    snapshot["relations"] = (relation,)
    store.insert_snapshot(snapshot)

    with closing(store.connect()) as connection:
        with pytest.raises(
            CodeGraphStoreError,
            match="relation provenance check failed",
        ):
            validate_integrity(connection)


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


def test_staging_directory_cleanup_failure_keeps_publication_successful(
    tmp_path, monkeypatch
):
    canonical = tmp_path / "code-backend.sqlite3"
    store = CodeGraphStore(canonical)
    store.insert_snapshot(snapshot_with_symbol_file_and_wiki_links(revision="old"))
    staging = store.create_staging_path()
    CodeGraphStore(staging).insert_snapshot(
        snapshot_with_symbol_file_and_wiki_links(revision="new")
    )
    staging_directory = staging.parent
    real_rmdir = Path.rmdir

    def fail_staging_rmdir(path):
        if path == staging_directory:
            raise OSError("post-replace cleanup fixture")
        return real_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", fail_staging_rmdir)

    store.publish_staging(
        staging,
        repository_id="backend",
        expected_revision="new",
    )

    assert store.reconstruct_metadata("backend")["revision"] == "new"
    assert not staging.exists()
    assert staging_directory.is_dir()


def test_database_replace_fsyncs_namespace_transitions_in_order(
    tmp_path, monkeypatch
):
    canonical = tmp_path / "code-backend.sqlite3"
    store = CodeGraphStore(canonical)
    store.insert_snapshot(snapshot_with_symbol_file_and_wiki_links(revision="old"))
    staging = store.create_staging_path()
    CodeGraphStore(staging).insert_snapshot(
        snapshot_with_symbol_file_and_wiki_links(revision="new")
    )
    store.prepare_staging(
        staging,
        repository_id="backend",
        expected_revision="new",
    )
    fsynced = []
    monkeypatch.setattr(
        store,
        "_fsync_directory",
        lambda path: fsynced.append(Path(path)),
        raising=False,
    )

    store.replace_staging(staging)

    assert fsynced == [staging.parent, canonical.parent, canonical.parent]


def test_database_fsync_failure_reports_snapshot_already_published(
    tmp_path, monkeypatch
):
    canonical = tmp_path / "code-backend.sqlite3"
    store = CodeGraphStore(canonical)
    store.insert_snapshot(snapshot_with_symbol_file_and_wiki_links(revision="old"))
    staging = store.create_staging_path()
    CodeGraphStore(staging).insert_snapshot(
        snapshot_with_symbol_file_and_wiki_links(revision="new")
    )
    store.prepare_staging(
        staging,
        repository_id="backend",
        expected_revision="new",
    )

    def fail_directory_fsync(_path):
        raise CodeGraphStoreError("directory fsync fixture")

    monkeypatch.setattr(
        store,
        "_fsync_directory",
        fail_directory_fsync,
        raising=False,
    )

    with pytest.raises(CodeGraphStoreError) as raised:
        store.replace_staging(staging)

    assert getattr(raised.value, "published", False) is True
    assert CodeGraphStore(canonical).reconstruct_metadata("backend")[
        "revision"
    ] == "new"


def test_metadata_replace_fsyncs_parent_directory(tmp_path, monkeypatch):
    store = CodeGraphStore(tmp_path / "code-backend.sqlite3")
    metadata_path = tmp_path / "code-backend.json"
    metadata_path.write_text('{"state":"old"}', encoding="utf-8")
    staging = store.prepare_metadata(metadata_path, {"state": "rebuilding"})
    fsynced = []
    monkeypatch.setattr(
        store,
        "_fsync_directory",
        lambda path: fsynced.append(Path(path)),
        raising=False,
    )

    store.publish_metadata(metadata_path, staging)

    assert fsynced == [metadata_path.parent]


def test_publish_metadata_context_close_failure_reports_already_published(
    tmp_path, monkeypatch
):
    store = CodeGraphStore(tmp_path / "code-backend.sqlite3")
    metadata_path = tmp_path / "code-backend.json"
    metadata_path.write_text('{"state":"old"}', encoding="utf-8")
    staging = store.prepare_metadata(metadata_path, {"state": "rebuilding"})
    secure_paths = store._secure_paths

    @contextmanager
    def fail_context_close(*paths, create):
        with secure_paths(*paths, create=create) as secured:
            yield secured
        raise OSError("metadata context close fixture")

    monkeypatch.setattr(store, "_secure_paths", fail_context_close)

    with pytest.raises(CodeGraphStoreError) as raised:
        store.publish_metadata(metadata_path, staging)

    assert getattr(raised.value, "published", False) is True
    assert isinstance(raised.value.__cause__, OSError)
    assert metadata_path.read_text(encoding="utf-8") == '{"state":"rebuilding"}'
    assert not staging.exists()


def test_refresh_metadata_context_close_failure_reports_already_published(
    tmp_path, monkeypatch
):
    store = CodeGraphStore(tmp_path / "code-backend.sqlite3")
    metadata_path = tmp_path / "code-backend.json"
    current = {
        "state": "ready",
        "revision": "revision-1",
        "duration_ms": 1,
        "phase_timings_ms": {"build": 1},
    }
    candidate = {
        **current,
        "duration_ms": 2,
        "phase_timings_ms": {"build": 2},
    }
    metadata_path.write_text(
        '{"duration_ms":1,"phase_timings_ms":{"build":1},'
        '"revision":"revision-1","state":"ready"}',
        encoding="utf-8",
    )
    staging = store.prepare_metadata(metadata_path, candidate)
    secure_paths = store._secure_paths

    @contextmanager
    def fail_context_close(*paths, create):
        with secure_paths(*paths, create=create) as secured:
            yield secured
        raise OSError("diagnostics context close fixture")

    monkeypatch.setattr(store, "_secure_paths", fail_context_close)

    with pytest.raises(CodeGraphStoreError) as raised:
        store.refresh_metadata_diagnostics(metadata_path, staging)

    assert getattr(raised.value, "published", False) is True
    assert isinstance(raised.value.__cause__, OSError)
    assert metadata_path.read_text(encoding="utf-8") == (
        '{"duration_ms":2,"phase_timings_ms":{"build":2},'
        '"revision":"revision-1","state":"ready"}'
    )
    assert not staging.exists()


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
