"""SQLite lifecycle and publication primitives for the code graph cache."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from typing import Iterator, Mapping, Sequence
from urllib.parse import quote

from .schema import (
    BUSY_TIMEOUT_MS,
    INDEXES,
    SCHEMA_VERSION,
    TABLES,
    CodeGraphSchemaError,
    CodeGraphStoreError,
    configure,
    create_schema,
    validate_integrity,
    validate_schema,
)


_PRIMARY_KEYS = {
    "repositories": "repository_id",
    "files": "file_id",
    "symbols": "symbol_id",
    "relations": "relation_id",
    "wiki_code_links": "link_id",
}


@dataclass(frozen=True)
class _StagingIdentity:
    directory: Path
    directory_dev: int
    directory_ino: int
    file_dev: int
    file_ino: int


_INSERTS = {
    "repositories": """
        INSERT INTO repositories (
            repository_id, root_path, git_remote, git_commit,
            source_fingerprint, config_fingerprint, parser_fingerprint,
            revision, state, indexed_at
        ) VALUES (
            :repository_id, :root_path, :git_remote, :git_commit,
            :source_fingerprint, :config_fingerprint, :parser_fingerprint,
            :revision, :state, :indexed_at
        )
    """,
    "files": """
        INSERT INTO files (
            file_id, repository_id, path, language, content_hash,
            parser_version, size_bytes
        ) VALUES (
            :file_id, :repository_id, :path, :language, :content_hash,
            :parser_version, :size_bytes
        )
    """,
    "symbols": """
        INSERT INTO symbols (
            symbol_id, file_id, kind, qualified_name, local_name,
            start_line, end_line, start_byte, end_byte, signature,
            visibility, content_hash, metadata_json
        ) VALUES (
            :symbol_id, :file_id, :kind, :qualified_name, :local_name,
            :start_line, :end_line, :start_byte, :end_byte, :signature,
            :visibility, :content_hash, :metadata_json
        )
    """,
    "relations": """
        INSERT INTO relations (
            relation_id, source_symbol_id, source_file_id, target_symbol_id,
            target_reference, relation_type, source_line, confidence,
            resolution_state, metadata_json
        ) VALUES (
            :relation_id, :source_symbol_id, :source_file_id, :target_symbol_id,
            :target_reference, :relation_type, :source_line, :confidence,
            :resolution_state, :metadata_json
        )
    """,
    "wiki_code_links": """
        INSERT INTO wiki_code_links (
            link_id, domain, page_id, symbol_id, file_id, selector_kind,
            relation_type, confidence, source
        ) VALUES (
            :link_id, :domain, :page_id, :symbol_id, :file_id, :selector_kind,
            :relation_type, :confidence, :source
        )
    """,
}


class CodeGraphStore:
    """Own one separate, rebuildable code graph SQLite cache."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._staging_identities: dict[Path, _StagingIdentity] = {}

    def connect(self) -> sqlite3.Connection:
        """Return a configured raw connection; the caller must close it."""
        connection = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self.path,
                timeout=BUSY_TIMEOUT_MS / 1000,
            )
            configure(connection)
            objects = tuple(
                connection.execute(
                    "SELECT type, name FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%'"
                )
            )
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if not objects and version == 0:
                create_schema(connection)
            else:
                validate_schema(connection)
            return connection
        except CodeGraphStoreError:
            if connection is not None:
                connection.close()
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            if connection is not None:
                connection.close()
            raise CodeGraphSchemaError("incompatible code graph schema") from exc

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def insert_snapshot(
        self, snapshot: Mapping[str, Sequence[Mapping[str, object]]]
    ) -> None:
        """Insert normalized rows in dependency and stable-ID order."""
        if set(snapshot) != set(TABLES):
            raise CodeGraphStoreError("invalid code graph snapshot")
        try:
            with self._transaction() as connection:
                for table in TABLES:
                    primary_key = _PRIMARY_KEYS[table]
                    rows = sorted(snapshot[table], key=lambda row: str(row[primary_key]))
                    connection.executemany(_INSERTS[table], rows)
        except CodeGraphStoreError:
            raise
        except (KeyError, sqlite3.DatabaseError) as exc:
            raise CodeGraphStoreError("cannot insert code graph snapshot") from exc

    def stable_rows(self, table: str) -> tuple[dict[str, object], ...]:
        """Read one schema table in stable primary-ID order."""
        try:
            primary_key = _PRIMARY_KEYS[table]
        except KeyError as exc:
            raise ValueError("unknown code graph table") from exc
        connection = self.connect()
        try:
            cursor = connection.execute(
                f"SELECT * FROM {table} ORDER BY {primary_key}"
            )
            columns = tuple(column[0] for column in cursor.description)
            return tuple(dict(zip(columns, row)) for row in cursor)
        finally:
            connection.close()

    def delete_repository(self, repository_id: str) -> None:
        """Delete one repository and all rows linked through schema cascades."""
        try:
            with self._transaction() as connection:
                connection.execute(
                    "DELETE FROM repositories WHERE repository_id = ?",
                    (repository_id,),
                )
        except CodeGraphStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            raise CodeGraphStoreError("cannot delete code graph repository") from exc

    def inspect_state(self, repository_id: str | None = None) -> str:
        """Return a persisted lifecycle state, or missing for no usable cache."""
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return "missing"
        try:
            connection = self.connect()
            try:
                if repository_id is None:
                    row = connection.execute(
                        "SELECT state FROM repositories ORDER BY repository_id LIMIT 1"
                    ).fetchone()
                else:
                    row = connection.execute(
                        "SELECT state FROM repositories WHERE repository_id = ?",
                        (repository_id,),
                    ).fetchone()
                return "missing" if row is None else str(row[0])
            finally:
                connection.close()
        except (CodeGraphStoreError, OSError):
            return "missing"

    def reconstruct_metadata(self, repository_id: str | None = None) -> dict[str, object]:
        """Reconstruct cache metadata with SQL revision as authority."""
        if not self.path.is_file() or self.path.stat().st_size == 0:
            raise CodeGraphStoreError("code graph metadata unavailable")
        connection = self.connect()
        try:
            columns = (
                "repository_id, git_commit, source_fingerprint, "
                "config_fingerprint, parser_fingerprint, revision, state, indexed_at"
            )
            if repository_id is None:
                row = connection.execute(
                    f"SELECT {columns} FROM repositories "
                    "ORDER BY repository_id LIMIT 1"
                ).fetchone()
            else:
                row = connection.execute(
                    f"SELECT {columns} FROM repositories WHERE repository_id = ?",
                    (repository_id,),
                ).fetchone()
            if row is None:
                raise CodeGraphStoreError("code graph metadata unavailable")
            metadata = dict(zip(columns.split(", "), row))
            metadata["schema_version"] = SCHEMA_VERSION
            return metadata
        finally:
            connection.close()

    def validate(self) -> None:
        """Validate schema, foreign keys, and SQLite integrity."""
        connection = self.connect()
        try:
            validate_integrity(connection)
        finally:
            connection.close()

    def quarantine_corrupt(self) -> Path:
        """Move an unusable code cache to a deterministic diagnostic sibling."""
        try:
            payload = self.path.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()[:16]
            sources = [
                (candidate, suffix)
                for suffix in ("", "-wal", "-shm")
                if (candidate := Path(f"{self.path}{suffix}")).exists()
            ]
            attempt = 0
            while True:
                collision_suffix = "" if attempt == 0 else f"-{attempt}"
                quarantined = self.path.with_name(
                    f"{self.path.name}.corrupt-{digest}{collision_suffix}"
                )
                targets = {
                    suffix: Path(f"{quarantined}{suffix}")
                    for suffix in ("", "-wal", "-shm")
                }
                reserved = []
                try:
                    for target in targets.values():
                        descriptor = os.open(
                            target,
                            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                            0o600,
                        )
                        reserved.append(target)
                        os.close(descriptor)
                except FileExistsError:
                    for target in reserved:
                        target.unlink()
                    attempt += 1
                    continue
                except OSError:
                    for target in reserved:
                        try:
                            target.unlink()
                        except FileNotFoundError:
                            pass
                    raise
                break

            pending = set(reserved)
            moved = []
            try:
                for source, suffix in sources:
                    target = targets[suffix]
                    os.replace(source, target)
                    pending.remove(target)
                    moved.append((source, target))
            except OSError:
                for source, target in reversed(moved):
                    if source.exists():
                        continue
                    try:
                        os.replace(target, source)
                    except OSError:
                        pass
                raise
            finally:
                for target in pending:
                    try:
                        target.unlink()
                    except FileNotFoundError:
                        pass
            return quarantined
        except OSError as exc:
            raise CodeGraphStoreError("cannot quarantine code graph cache") from exc

    @staticmethod
    def _absolute(path: Path) -> Path:
        return Path(os.path.abspath(path))

    def create_staging_path(self) -> Path:
        """Securely reserve and register one staging file beside the canonical."""
        canonical = self._absolute(self.path)
        private_directory = None
        staging = None
        try:
            canonical.parent.mkdir(parents=True, exist_ok=True)
            private_directory = Path(tempfile.mkdtemp(
                prefix=f"{canonical.name}.staging-",
                dir=canonical.parent,
            ))
            os.chmod(private_directory, 0o700)
            directory_status = os.lstat(private_directory)
            staging = private_directory / "snapshot.sqlite3"
            descriptor = os.open(
                staging,
                os.O_CREAT | os.O_EXCL | os.O_RDWR,
                0o600,
            )
            os.close(descriptor)
            status = os.lstat(staging)
            if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                raise OSError("invalid staging reservation")
            self._staging_identities[staging] = _StagingIdentity(
                directory=private_directory,
                directory_dev=directory_status.st_dev,
                directory_ino=directory_status.st_ino,
                file_dev=status.st_dev,
                file_ino=status.st_ino,
            )
            return staging
        except OSError as exc:
            if staging is not None:
                try:
                    staging.unlink()
                except FileNotFoundError:
                    pass
            if private_directory is not None:
                try:
                    private_directory.rmdir()
                except OSError:
                    pass
            raise CodeGraphStoreError("cannot create code graph staging") from exc

    @staticmethod
    def _directory_identity_matches(identity: _StagingIdentity) -> bool:
        try:
            status = os.lstat(identity.directory)
        except OSError:
            return False
        return (
            stat.S_ISDIR(status.st_mode)
            and (status.st_dev, status.st_ino)
            == (identity.directory_dev, identity.directory_ino)
        )

    @staticmethod
    def _file_identity_matches(
        staging: Path,
        identity: _StagingIdentity,
    ) -> bool:
        try:
            status = os.lstat(staging)
        except OSError:
            return False
        return (
            stat.S_ISREG(status.st_mode)
            and (status.st_dev, status.st_ino)
            == (identity.file_dev, identity.file_ino)
        )

    def _validate_staging_identity(
        self,
        staging: Path,
        identity: _StagingIdentity,
    ) -> None:
        canonical = self._absolute(self.path)
        prefix = f"{canonical.name}.staging-"
        try:
            directory_status = os.lstat(identity.directory)
            status = os.lstat(staging)
        except OSError:
            raise CodeGraphStoreError("invalid code graph staging database")
        valid = (
            identity.directory.parent == canonical.parent
            and identity.directory.name.startswith(prefix)
            and len(identity.directory.name) > len(prefix)
            and staging.parent == identity.directory
            and stat.S_ISDIR(directory_status.st_mode)
            and stat.S_IMODE(directory_status.st_mode) == 0o700
            and (directory_status.st_dev, directory_status.st_ino)
            == (identity.directory_dev, identity.directory_ino)
            and stat.S_ISREG(status.st_mode)
            and status.st_nlink == 1
            and (status.st_dev, status.st_ino)
            == (identity.file_dev, identity.file_ino)
        )
        if not valid:
            raise CodeGraphStoreError("invalid code graph staging database")

    @staticmethod
    def _remove_empty_staging_directory(identity: _StagingIdentity) -> None:
        if not CodeGraphStore._directory_identity_matches(identity):
            return
        try:
            identity.directory.rmdir()
        except OSError:
            pass

    def discard_staging(self, staging: str | Path) -> None:
        """Discard only identity-matching artifacts registered by this store."""
        staging_path = self._absolute(Path(staging))
        identity = self._staging_identities.get(staging_path)
        if identity is None:
            return
        try:
            if not self._directory_identity_matches(identity):
                return
            if not self._file_identity_matches(staging_path, identity):
                return
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{staging_path}{suffix}")
                try:
                    status = os.lstat(sidecar)
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(status.st_mode) and status.st_nlink == 1:
                    sidecar.unlink()
            staging_path.unlink()
            self._remove_empty_staging_directory(identity)
        except OSError as exc:
            raise CodeGraphStoreError("cannot discard code graph staging") from exc
        finally:
            self._staging_identities.pop(staging_path, None)

    @staticmethod
    def _connect_existing(path: Path) -> sqlite3.Connection:
        connection = None
        try:
            resolved = path.resolve().as_posix()
            uri = f"file:{quote(resolved, safe='/:')}?mode=rw"
            connection = sqlite3.connect(
                uri,
                uri=True,
                timeout=BUSY_TIMEOUT_MS / 1000,
            )
            configure(connection)
            validate_schema(connection)
            return connection
        except CodeGraphStoreError:
            if connection is not None:
                connection.close()
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            if connection is not None:
                connection.close()
            raise CodeGraphSchemaError("incompatible code graph schema") from exc

    def publish_staging(
        self,
        staging: str | Path,
        *,
        repository_id: str,
        expected_revision: str,
    ) -> None:
        """Publish registered staging while caller holds the per-domain writer lock."""
        staging_path = self._absolute(Path(staging))
        identity = self._staging_identities.get(staging_path)
        if identity is None:
            raise CodeGraphStoreError("invalid code graph staging database")
        try:
            self._validate_staging_identity(staging_path, identity)
            connection = self._connect_existing(staging_path)
            try:
                validate_integrity(connection)
                repositories = connection.execute(
                    "SELECT repository_id, revision, state "
                    "FROM repositories ORDER BY repository_id"
                ).fetchall()
                if repositories != [(repository_id, expected_revision, "ready")]:
                    raise CodeGraphStoreError(
                        "code graph staging snapshot mismatch"
                    )
                checkpoint = connection.execute(
                    "PRAGMA wal_checkpoint(TRUNCATE)"
                ).fetchone()
                if checkpoint is None or checkpoint[0] != 0:
                    raise CodeGraphStoreError("cannot checkpoint code graph staging")
            finally:
                connection.close()

            if any(Path(f"{self.path}{suffix}").exists() for suffix in ("-wal", "-shm")):
                raise CodeGraphStoreError("code graph canonical database is in use")
            canonical = self._absolute(self.path)
            canonical.parent.mkdir(parents=True, exist_ok=True)
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            source_descriptor = os.open(identity.directory, directory_flags)
            try:
                destination_descriptor = os.open(canonical.parent, directory_flags)
                try:
                    self._validate_staging_identity(staging_path, identity)
                    try:
                        os.replace(
                            staging_path.name,
                            canonical.name,
                            src_dir_fd=source_descriptor,
                            dst_dir_fd=destination_descriptor,
                        )
                    except (NotImplementedError, TypeError):
                        self._validate_staging_identity(staging_path, identity)
                        os.replace(staging_path, canonical)
                finally:
                    os.close(destination_descriptor)
            finally:
                os.close(source_descriptor)
            self._remove_empty_staging_directory(identity)
        except CodeGraphStoreError:
            self.discard_staging(staging_path)
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            self.discard_staging(staging_path)
            raise CodeGraphStoreError("cannot publish code graph staging") from exc
        finally:
            self._staging_identities.pop(staging_path, None)


__all__ = [
    "BUSY_TIMEOUT_MS",
    "INDEXES",
    "SCHEMA_VERSION",
    "TABLES",
    "CodeGraphSchemaError",
    "CodeGraphStore",
    "CodeGraphStoreError",
]
