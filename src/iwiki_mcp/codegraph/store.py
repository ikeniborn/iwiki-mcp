"""SQLite lifecycle and publication primitives for the code graph cache."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import time
from typing import Callable, Iterator, Mapping, Sequence
from urllib.parse import quote
import uuid

from filelock import ReadWriteLock, Timeout

from .canonical import canonical_json_bytes, canonical_sha256
from .location import CodeGraphLocationError, open_cache_directory
from .models import (
    compact_casefold,
    file_id,
    module_key as normalize_module_key,
    module_id,
    relation_id,
    symbol_id,
    token_key,
)
from .schema import (
    BUSY_TIMEOUT_MS,
    INDEXES,
    SCHEMA_VERSION,
    TABLES,
    CodeGraphSchemaError,
    CodeGraphStoreError,
    configure,
    create_schema,
    inspect_compatibility as inspect_schema_compatibility,
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
_CANONICAL_REVISION = re.compile(r"^sha256:[0-9a-f]{64}$")
_SQLITE_HEADER = b"SQLite format 3\x00"


@dataclass(frozen=True)
class _SealedReadState:
    storage_stamp: Mapping[str, object]
    wal_exists: bool
    shm_exists: bool


def _is_canonical_revision(value: object) -> bool:
    return isinstance(value, str) and _CANONICAL_REVISION.fullmatch(value) is not None


def _snapshot_revision(
    repository: Mapping[str, object],
    files: Sequence[Mapping[str, object]],
    symbols: Sequence[Mapping[str, object]],
    relations: Sequence[Mapping[str, object]],
    wiki_code_links: Sequence[Mapping[str, object]],
) -> str:
    """Hash persisted deterministic inputs and normalized output rows."""
    repository_inputs = {
        key: repository[key]
        for key in (
            "repository_id",
            "git_commit",
            "source_fingerprint",
            "config_fingerprint",
            "parser_fingerprint",
            "normalizer_version",
            "unicode_data_version",
        )
    }
    rows = {
        "repository": repository_inputs,
        "files": sorted(files, key=lambda row: str(row["file_id"])),
        "symbols": sorted(symbols, key=lambda row: str(row["symbol_id"])),
        "relations": sorted(
            relations, key=lambda row: str(row["relation_id"])
        ),
        "wiki_code_links": sorted(
            wiki_code_links, key=lambda row: str(row["link_id"])
        ),
    }
    return canonical_sha256(rows, prefix=True)


def _table_rows(
    connection: sqlite3.Connection, table: str
) -> tuple[dict[str, object], ...]:
    columns = tuple(
        row[1] for row in connection.execute(f"PRAGMA table_info({table})")
    )
    primary_key = _PRIMARY_KEYS[table]
    return tuple(
        dict(zip(columns, row))
        for row in connection.execute(
            f'SELECT * FROM "{table}" ORDER BY "{primary_key}"'
        )
    )


def _timestamp_ns(status: os.stat_result, name: str) -> int:
    value = getattr(status, f"st_{name}_ns", None)
    if type(value) is int:
        return value
    return int(getattr(status, f"st_{name}") * 1_000_000_000)


@contextmanager
def _held_regular_file(path: Path) -> Iterator[os.stat_result]:
    """Hold and identify one final regular file without following symlinks."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not _same_regular_file(path, opened):
            raise OSError("unsafe code graph storage file")
        yield opened
    finally:
        os.close(descriptor)


def _validate_optional_sidecar(path: Path) -> None:
    """Reject an existing non-regular or linked SQLite sidecar."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return
    try:
        opened = os.fstat(descriptor)
        if not _same_regular_file(path, opened):
            raise OSError("unsafe code graph storage sidecar")
    finally:
        os.close(descriptor)


def _same_regular_file(path: Path, opened: os.stat_result) -> bool:
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISREG(opened.st_mode)
        and opened.st_nlink == 1
        and stat.S_ISREG(current.st_mode)
        and current.st_nlink == 1
        and (current.st_dev, current.st_ino)
        == (opened.st_dev, opened.st_ino)
    )


def _storage_file_stamp(path: Path, *, wal: bool) -> dict[str, object] | None:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if wal:
            return None
        raise
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise OSError("invalid code graph storage file")
        header_size = 32 if wal else 100
        header = os.read(descriptor, header_size)
        stamp = {
            "size": status.st_size,
            "mtime_ns": _timestamp_ns(status, "mtime"),
            "ctime_ns": _timestamp_ns(status, "ctime"),
            "device": status.st_dev,
            "inode": status.st_ino,
        }
        if wal:
            if status.st_size == 0:
                return None
            if len(header) != 32:
                raise OSError("invalid code graph WAL header")
            page_size = int.from_bytes(header[8:12], "big")
            frame_size = page_size + 24
            payload_size = status.st_size - 32
            if page_size <= 0 or payload_size < 0 or payload_size % frame_size:
                raise OSError("invalid code graph WAL frames")
            return {
                **stamp,
                "magic": int.from_bytes(header[0:4], "big"),
                "version": int.from_bytes(header[4:8], "big"),
                "page_size": page_size,
                "salt_1": int.from_bytes(header[16:20], "big"),
                "salt_2": int.from_bytes(header[20:24], "big"),
                "frames": payload_size // frame_size,
            }
        if len(header) != 100 or header[:16] != _SQLITE_HEADER:
            raise OSError("invalid code graph database header")
        return {
            **stamp,
            "change_counter": int.from_bytes(header[24:28], "big"),
            "page_count": int.from_bytes(header[28:32], "big"),
            "schema_cookie": int.from_bytes(header[40:44], "big"),
            "version_valid_for": int.from_bytes(header[92:96], "big"),
        }
    finally:
        os.close(descriptor)


def _language_prefix(entity_id: object, kind: str) -> str:
    if not isinstance(entity_id, str):
        raise CodeGraphStoreError("code graph deterministic identity mismatch")
    prefix, separator, stored_kind = entity_id.partition(":")
    if not prefix or separator != ":" or not stored_kind.startswith(f"{kind}:"):
        raise CodeGraphStoreError("code graph deterministic identity mismatch")
    return prefix


def _validate_normalized_rows(
    repository_id: str,
    files: Sequence[Mapping[str, object]],
    symbols: Sequence[Mapping[str, object]],
    relations: Sequence[Mapping[str, object]],
) -> None:
    files_by_id = {row["file_id"]: row for row in files}
    for row in files:
        path = row["path"]
        language = row["language"]
        ranges = (
            row["size_bytes"],
            row["start_line"],
            row["end_line"],
            row["start_byte"],
            row["end_byte"],
        )
        if any(type(value) is not int or value < 0 for value in ranges):
            raise CodeGraphStoreError("code graph file contract mismatch")
        if not isinstance(path, str) or not isinstance(language, str):
            raise CodeGraphStoreError("code graph normalized row mismatch")
        prefix = _language_prefix(row["file_id"], "file")
        normalized_path = normalize_module_key(path)
        local_name = PurePosixPath(path).name
        expected = (
            file_id(language, prefix, repository_id, path),
            compact_casefold(path),
            local_name,
            token_key(local_name),
            normalized_path,
        )
        actual = (
            row["file_id"],
            row["path_casefold"],
            row["file_local_name"],
            row["file_name_tokens_casefold"],
            row["module_key"],
        )
        if (
            actual != expected
            or path != normalized_path
            or _CANONICAL_REVISION.fullmatch(
                "sha256:" + str(row["content_hash"])
            ) is None
        ):
            raise CodeGraphStoreError("code graph normalized row mismatch")
        module_qualified_name = row["module_qualified_name"]
        if module_qualified_name is not None:
            if not isinstance(module_qualified_name, str):
                raise CodeGraphStoreError("code graph normalized row mismatch")
            module_local_name = module_qualified_name.rsplit(".", 1)[-1]
            module_expected = (
                module_id(
                    language,
                    prefix,
                    repository_id,
                    path,
                    module_qualified_name,
                ),
                module_local_name,
                token_key(module_qualified_name, module_local_name),
            )
            module_actual = (
                row["module_id"],
                row["module_local_name"],
                row["module_name_tokens_casefold"],
            )
            if module_actual != module_expected:
                raise CodeGraphStoreError("code graph normalized row mismatch")

    symbols_by_id = {row["symbol_id"]: row for row in symbols}
    for row in symbols:
        file_row = files_by_id.get(row["file_id"])
        if file_row is None:
            raise CodeGraphStoreError("code graph deterministic identity mismatch")
        qualified_name = row["qualified_name"]
        local_name = row["local_name"]
        signature = row["signature"]
        if not isinstance(qualified_name, str) or not isinstance(local_name, str):
            raise CodeGraphStoreError("code graph normalized row mismatch")
        prefix = _language_prefix(file_row["file_id"], "file")
        expected = (
            symbol_id(
                str(file_row["language"]),
                prefix,
                repository_id,
                str(file_row["module_key"]),
                qualified_name,
                signature or "",
            ),
            token_key(qualified_name, local_name),
            compact_casefold(signature if isinstance(signature, str) else None),
        )
        actual = (
            row["symbol_id"],
            row["name_tokens_casefold"],
            row["signature_casefold"],
        )
        ranges = (
            row["start_line"],
            row["end_line"],
            row["start_byte"],
            row["end_byte"],
        )
        if (
            actual != expected
            or local_name != qualified_name.rsplit(".", 1)[-1]
            or _CANONICAL_REVISION.fullmatch(
                "sha256:" + str(row["content_hash"])
            ) is None
            or row["end_line"] > file_row["end_line"]
            or row["end_byte"] > file_row["end_byte"]
            or any(type(value) is not int for value in ranges)
        ):
            raise CodeGraphStoreError("code graph normalized row mismatch")
        try:
            metadata = json.loads(str(row["metadata_json"]))
        except (TypeError, ValueError) as exc:
            raise CodeGraphStoreError("code graph row contract mismatch") from exc
        if canonical_json_bytes(metadata).decode("utf-8") != row["metadata_json"]:
            raise CodeGraphStoreError("code graph row contract mismatch")

    for row in relations:
        file_row = files_by_id.get(row["source_file_id"])
        if file_row is None:
            raise CodeGraphStoreError("code graph deterministic identity mismatch")
        ranges = (
            row["source_start_line"],
            row["source_end_line"],
            row["source_start_byte"],
            row["source_end_byte"],
        )
        if any(type(value) is not int or value < 0 for value in ranges):
            raise CodeGraphStoreError("code graph relation contract mismatch")
        source_identity = (
            row["source_symbol_id"]
            or row["source_module_id"]
            or row["source_file_id"]
        )
        target_identity = row["target_symbol_id"] or row["target_module_id"]
        prefix = _language_prefix(file_row["file_id"], "file")
        expected_id = relation_id(
            str(file_row["language"]),
            prefix,
            repository_id,
            str(source_identity),
            str(row["relation_type"]),
            row["source_start_line"],
            row["source_end_line"],
            row["source_start_byte"],
            row["source_end_byte"],
            str(target_identity) if target_identity is not None else None,
            (
                str(row["target_reference"])
                if row["target_reference"] is not None else None
            ),
            str(row["binding_kind"]) if row["binding_kind"] is not None else None,
            str(row["binding_name"]) if row["binding_name"] is not None else None,
        )
        expected_binding_tokens = (
            token_key(str(row["binding_name"]))
            if row["binding_name"] is not None else None
        )
        if (
            row["relation_id"] != expected_id
            or row["binding_name_tokens_casefold"] != expected_binding_tokens
            or row["source_end_line"] > file_row["end_line"]
            or row["source_end_byte"] > file_row["end_byte"]
        ):
            raise CodeGraphStoreError("code graph relation contract mismatch")
        if row["source_symbol_id"] not in (None, *symbols_by_id):
            raise CodeGraphStoreError("code graph relation contract mismatch")
        try:
            metadata = json.loads(str(row["metadata_json"]))
        except (TypeError, ValueError) as exc:
            raise CodeGraphStoreError("code graph row contract mismatch") from exc
        if canonical_json_bytes(metadata).decode("utf-8") != row["metadata_json"]:
            raise CodeGraphStoreError("code graph row contract mismatch")


def _validate_persisted_snapshot(
    connection: sqlite3.Connection,
    repository_id: str,
    expected_revision: str,
) -> None:
    """Validate and recompute one complete persisted schema-v2 snapshot."""
    validate_schema(connection)
    validate_integrity(connection)
    repositories = _table_rows(connection, "repositories")
    if len(repositories) != 1:
        raise CodeGraphStoreError("code graph staging snapshot mismatch")
    repository = repositories[0]
    if (
        repository["repository_id"] != repository_id
        or repository["state"] != "ready"
        or repository["revision"] != expected_revision
    ):
        raise CodeGraphStoreError("code graph staging snapshot mismatch")
    if not _is_canonical_revision(expected_revision):
        raise CodeGraphStoreError("code graph revision mismatch")
    if any(
        _CANONICAL_REVISION.fullmatch("sha256:" + str(repository[key])) is None
        for key in (
            "source_fingerprint",
            "config_fingerprint",
            "parser_fingerprint",
        )
    ):
        raise CodeGraphStoreError("code graph fingerprint mismatch")
    files = _table_rows(connection, "files")
    symbols = _table_rows(connection, "symbols")
    relations = _table_rows(connection, "relations")
    wiki_code_links = _table_rows(connection, "wiki_code_links")
    try:
        _validate_normalized_rows(repository_id, files, symbols, relations)
        if any(
            row["domain"] != repository_id
            or row["relation_type"] != "DOCUMENTED_BY"
            or row["selector_kind"] not in {"symbol", "file", "source_glob"}
            or not isinstance(row["page_id"], str)
            or not row["page_id"]
            or not isinstance(row["source"], str)
            or not row["source"]
            or (row["selector_kind"] == "symbol") != (
                row["symbol_id"] is not None
            )
            for row in wiki_code_links
        ):
            raise CodeGraphStoreError("code graph link contract mismatch")
        recomputed = _snapshot_revision(
            repository, files, symbols, relations, wiki_code_links
        )
    except CodeGraphStoreError:
        raise
    except (KeyError, OverflowError, TypeError, ValueError) as exc:
        raise CodeGraphStoreError("code graph snapshot mismatch") from exc
    if (
        recomputed != expected_revision
        or repository["revision"] != expected_revision
    ):
        raise CodeGraphStoreError("code graph revision mismatch")


class CodeGraphPublishedError(CodeGraphStoreError):
    """Report a durability error after the canonical namespace changed."""

    published = True


def run_publication_protocol(
    *,
    replace: Callable[[], None],
    metadata_rebuilding: Callable[[], None],
    verify_1: Callable[[], None],
    metadata_ready_pending: Callable[[], None],
    verify_2: Callable[[], None],
    timing_refresh: Callable[[], None],
) -> None:
    """Run the six ordered publication hooks without owning full-build work."""
    replace()
    metadata_rebuilding()
    verify_1()
    metadata_ready_pending()
    verify_2()
    timing_refresh()


def _discard_lock(lock: ReadWriteLock | None) -> None:
    if lock is None:
        return
    try:
        lock.close()
    except Exception:
        pass


@contextmanager
def _code_graph_lock(
    path: str | Path,
    *,
    timeout: float,
    write: bool,
) -> Iterator[None]:
    lock = None
    cache_context = open_cache_directory(
        CodeGraphStore._absolute(Path(path)).parent.parent,
        create=True,
    )
    try:
        cache_directory = cache_context.__enter__()
        assert cache_directory is not None
        lock_path = CodeGraphStore._absolute(Path(path))
        secure_lock_path = cache_directory / lock_path.name
        lock = ReadWriteLock(
            str(secure_lock_path),
            timeout=timeout,
            blocking=write,
            is_singleton=False,
        )
        if write:
            lock.acquire_write(timeout=timeout)
        else:
            lock.acquire_read(timeout=timeout, blocking=False)
    except Timeout:
        _discard_lock(lock)
        try:
            cache_context.__exit__(None, None, None)
        except Exception:
            pass
        raise
    except Exception as exc:
        _discard_lock(lock)
        try:
            cache_context.__exit__(None, None, None)
        except Exception:
            pass
        raise CodeGraphStoreError(
            "cannot acquire code graph lock"
        ) from exc
    try:
        yield
    finally:
        close_error = None
        try:
            lock.close()
        except Exception as exc:
            close_error = exc
        try:
            cache_context.__exit__(None, None, None)
        except Exception as exc:
            close_error = close_error or exc
        if close_error is not None:
            raise CodeGraphStoreError(
                "cannot release code graph lock"
            ) from close_error


@contextmanager
def code_graph_read_lock(path: str | Path) -> Iterator[None]:
    """Hold a nonblocking shared lock and close its SQLite handle."""
    with _code_graph_lock(path, timeout=0, write=False):
        yield


@contextmanager
def code_graph_write_lock(
    path: str | Path,
    *,
    timeout: float,
) -> Iterator[None]:
    """Hold a bounded exclusive lock and close its SQLite handle."""
    with _code_graph_lock(path, timeout=timeout, write=True):
        yield


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
            normalizer_version, unicode_data_version,
            revision, state, indexed_at
        ) VALUES (
            :repository_id, :root_path, :git_remote, :git_commit,
            :source_fingerprint, :config_fingerprint, :parser_fingerprint,
            :normalizer_version, :unicode_data_version,
            :revision, :state, :indexed_at
        )
    """,
    "files": """
        INSERT INTO files (
            file_id, repository_id, path, path_casefold, file_local_name,
            file_name_tokens_casefold, language, content_hash,
            parser_version, size_bytes, start_line, end_line,
            start_byte, end_byte, module_key, module_id,
            module_qualified_name, module_local_name,
            module_name_tokens_casefold
        ) VALUES (
            :file_id, :repository_id, :path, :path_casefold, :file_local_name,
            :file_name_tokens_casefold, :language, :content_hash,
            :parser_version, :size_bytes, :start_line, :end_line,
            :start_byte, :end_byte, :module_key, :module_id,
            :module_qualified_name, :module_local_name,
            :module_name_tokens_casefold
        )
    """,
    "symbols": """
        INSERT INTO symbols (
            symbol_id, file_id, kind, qualified_name, local_name,
            name_tokens_casefold, start_line, end_line, start_byte, end_byte,
            signature, signature_casefold, visibility, content_hash,
            metadata_json
        ) VALUES (
            :symbol_id, :file_id, :kind, :qualified_name, :local_name,
            :name_tokens_casefold, :start_line, :end_line, :start_byte, :end_byte,
            :signature, :signature_casefold, :visibility, :content_hash,
            :metadata_json
        )
    """,
    "relations": """
        INSERT INTO relations (
            relation_id, source_file_id, source_module_id, source_symbol_id,
            target_module_id, target_symbol_id, target_reference, relation_type,
            source_start_line, source_end_line, source_start_byte,
            source_end_byte, binding_name, binding_kind,
            binding_name_tokens_casefold, confidence, resolution_state,
            metadata_json
        ) VALUES (
            :relation_id, :source_file_id, :source_module_id, :source_symbol_id,
            :target_module_id, :target_symbol_id, :target_reference, :relation_type,
            :source_start_line, :source_end_line, :source_start_byte,
            :source_end_byte, :binding_name, :binding_kind,
            :binding_name_tokens_casefold, :confidence, :resolution_state,
            :metadata_json
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

    @staticmethod
    def repository_state(
        connection: sqlite3.Connection, repository_id: str
    ) -> tuple[str, str] | None:
        """Read lifecycle state and revision from a caller-held read lease."""
        row = connection.execute(
            "SELECT state, revision FROM repositories WHERE repository_id = ?",
            (repository_id,),
        ).fetchone()
        if row is None:
            return None
        return str(row[0]), str(row[1])

    def __init__(
        self,
        path: str | Path,
        *,
        cache_base: str | Path | None = None,
    ) -> None:
        self.path = Path(path)
        self.cache_base = (
            None
            if cache_base is None
            else self._absolute(Path(cache_base))
        )
        self._staging_identities: dict[Path, _StagingIdentity] = {}
        self._metadata_staging: dict[Path, Path] = {}

    def _cache_relative(self, path: Path) -> Path:
        absolute = self._absolute(path)
        if self.cache_base is None:
            return absolute
        try:
            relative = absolute.relative_to(self.cache_base / ".iwiki")
        except ValueError as exc:
            raise CodeGraphStoreError(
                "unsafe code graph cache path"
            ) from exc
        if relative == Path(".") or ".." in relative.parts:
            raise CodeGraphStoreError("unsafe code graph cache path")
        return relative

    @contextmanager
    def _secure_paths(
        self,
        *paths: Path,
        create: bool,
    ) -> Iterator[tuple[Path, ...]]:
        if self.cache_base is None:
            yield tuple(self._absolute(path) for path in paths)
            return
        relatives = tuple(self._cache_relative(path) for path in paths)
        try:
            with open_cache_directory(
                self.cache_base,
                create=create,
            ) as cache_directory:
                if cache_directory is None:
                    raise CodeGraphStoreError(
                        "code graph cache directory unavailable"
                    )
                yield tuple(
                    cache_directory.joinpath(*relative.parts)
                    for relative in relatives
                )
        except CodeGraphStoreError:
            raise
        except CodeGraphLocationError as exc:
            raise CodeGraphStoreError(
                "unsafe code graph cache path"
            ) from exc

    def connect(self) -> sqlite3.Connection:
        """Return a configured raw connection; the caller must close it."""
        connection = None
        try:
            with self._secure_paths(self.path, create=True) as secured:
                path = secured[0]
                if self.cache_base is None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(
                    path,
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

    def inspect_compatibility(self) -> str:
        """Inspect exact compatibility through a true read-only connection."""
        try:
            with self._read_connection(validate=False) as connection:
                return inspect_schema_compatibility(connection)
        except (CodeGraphStoreError, OSError, sqlite3.DatabaseError):
            return "incompatible"

    def inspect_schema_version(self) -> int | None:
        """Read only the canonical user version without creating or migrating it."""
        try:
            with self._read_connection(validate=False) as connection:
                row = connection.execute("PRAGMA user_version").fetchone()
                return (
                    row[0]
                    if row is not None and type(row[0]) is int
                    else None
                )
        except (CodeGraphStoreError, OSError, sqlite3.DatabaseError):
            return None

    def storage_stamp(self) -> dict[str, object]:
        """Return one stable bounded database/WAL mutation observation."""
        wal_path = Path(f"{self.path}-wal")
        try:
            with self._secure_paths(
                self.path, wal_path, create=False
            ) as secured:
                for _attempt in range(3):
                    database_before = _storage_file_stamp(
                        secured[0], wal=False
                    )
                    wal_before = _storage_file_stamp(secured[1], wal=True)
                    database_after = _storage_file_stamp(
                        secured[0], wal=False
                    )
                    wal_after = _storage_file_stamp(secured[1], wal=True)
                    if (
                        database_before == database_after
                        and wal_before == wal_after
                    ):
                        return {
                            "database": database_after,
                            "wal": wal_after,
                        }
                raise CodeGraphStoreError(
                    "code graph storage changed during inspection"
                )
        except (CodeGraphStoreError, OSError) as exc:
            raise CodeGraphStoreError(
                "cannot inspect code graph storage stamp"
            ) from exc

    def _sealed_read_state(self) -> _SealedReadState:
        wal_path = Path(f"{self.path}-wal")
        shm_path = Path(f"{self.path}-shm")
        for _attempt in range(3):
            wal_before = wal_path.exists()
            shm_before = shm_path.exists()
            stamp = self.storage_stamp()
            wal_after = wal_path.exists()
            shm_after = shm_path.exists()
            if (
                wal_before == wal_after
                and shm_before == shm_after
            ):
                return _SealedReadState(
                    storage_stamp=stamp,
                    wal_exists=wal_after,
                    shm_exists=shm_after,
                )
        raise CodeGraphStoreError(
            "cannot inspect code graph read state"
        )

    def verify_canonical(
        self,
        repository_id: str,
        expected_revision: str,
    ) -> None:
        """Independently reopen and verify one published canonical snapshot."""
        connection = None
        try:
            connection = self.open_existing()
            _validate_persisted_snapshot(
                connection, repository_id, expected_revision
            )
        except (CodeGraphStoreError, sqlite3.DatabaseError, ValueError) as exc:
            raise CodeGraphStoreError(
                "code graph canonical verification failed"
            ) from exc
        finally:
            if connection is not None:
                connection.close()

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
                    rows = [dict(row) for row in snapshot[table]]
                    rows.sort(key=lambda row: str(row[primary_key]))
                    connection.executemany(_INSERTS[table], rows)
        except CodeGraphStoreError:
            raise
        except (KeyError, sqlite3.DatabaseError) as exc:
            raise CodeGraphStoreError("cannot insert code graph snapshot") from exc

    def replace_relations(
        self, relations: Sequence[Mapping[str, object]]
    ) -> None:
        """Replace persisted unresolved inputs with resolved relation rows."""
        try:
            with self._transaction() as connection:
                connection.execute("DELETE FROM relations")
                rows = sorted(
                    relations, key=lambda row: str(row["relation_id"])
                )
                connection.executemany(_INSERTS["relations"], rows)
        except CodeGraphStoreError:
            raise
        except (KeyError, sqlite3.DatabaseError) as exc:
            raise CodeGraphStoreError(
                "cannot update code graph relations"
            ) from exc

    def finalize_snapshot(
        self,
        *,
        repository_id: str,
        revision: str,
        indexed_at: str,
        wiki_code_links: Sequence[Mapping[str, object]],
    ) -> None:
        """Persist selector links and mark a fully resolved snapshot ready."""
        try:
            with self._transaction() as connection:
                connection.execute("DELETE FROM wiki_code_links")
                rows = sorted(
                    wiki_code_links, key=lambda row: str(row["link_id"])
                )
                connection.executemany(_INSERTS["wiki_code_links"], rows)
                cursor = connection.execute(
                    "UPDATE repositories SET revision = ?, state = 'ready', "
                    "indexed_at = ? WHERE repository_id = ?",
                    (revision, indexed_at, repository_id),
                )
                if cursor.rowcount != 1:
                    raise CodeGraphStoreError(
                        "code graph repository is unavailable"
                    )
        except CodeGraphStoreError:
            raise
        except (KeyError, sqlite3.DatabaseError) as exc:
            raise CodeGraphStoreError(
                "cannot finalize code graph snapshot"
            ) from exc

    def set_repository_state(self, repository_id: str, state: str) -> None:
        """Materialize one lifecycle transition without exposing old rows."""
        try:
            with self._transaction() as connection:
                cursor = connection.execute(
                    "UPDATE repositories SET state = ? WHERE repository_id = ?",
                    (state, repository_id),
                )
                if cursor.rowcount != 1:
                    raise CodeGraphStoreError(
                        "code graph repository is unavailable"
                    )
        except CodeGraphStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            raise CodeGraphStoreError(
                "cannot update code graph state"
            ) from exc

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
        try:
            connection = self.open_existing()
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
        try:
            connection = self.open_existing()
        except CodeGraphStoreError as exc:
            raise CodeGraphStoreError(
                "code graph metadata unavailable"
            ) from exc
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
            candidates = tuple(
                Path(f"{self.path}{suffix}")
                for suffix in ("", "-wal", "-shm")
            )
            with self._secure_paths(*candidates, create=False) as secured:
                canonical = secured[0]
                payload = canonical.read_bytes()
                digest = hashlib.sha256(payload).hexdigest()[:16]
                sources = [
                    (candidate, suffix)
                    for candidate, suffix in zip(
                        secured, ("", "-wal", "-shm")
                    )
                    if candidate.exists()
                ]
                attempt = 0
                while True:
                    collision_suffix = "" if attempt == 0 else f"-{attempt}"
                    quarantined = canonical.with_name(
                        f"{canonical.name}.corrupt-{digest}{collision_suffix}"
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
                return self.path.with_name(quarantined.name)
        except OSError as exc:
            raise CodeGraphStoreError("cannot quarantine code graph cache") from exc

    @staticmethod
    def _absolute(path: Path) -> Path:
        return Path(os.path.abspath(path))

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        """Make one completed namespace transition durable on supported hosts."""
        if os.name == "nt":
            return
        descriptor = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            descriptor = os.open(path, flags)
            os.fsync(descriptor)
        except OSError as exc:
            raise CodeGraphStoreError(
                "cannot sync code graph directory"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def create_staging_path(self) -> Path:
        """Securely reserve and register one staging file beside the canonical."""
        canonical = self._absolute(self.path)
        private_directory = None
        staging = None
        try:
            private_directory = canonical.parent / (
                f"{canonical.name}.staging-{uuid.uuid4()}"
            )
            staging = private_directory / "snapshot.sqlite3"
            with self._secure_paths(
                private_directory, staging, create=True
            ) as secured:
                secure_directory, secure_staging = secured
                if self.cache_base is None:
                    secure_directory.parent.mkdir(parents=True, exist_ok=True)
                secure_directory.mkdir(mode=0o700)
                directory_status = os.lstat(secure_directory)
                descriptor = os.open(
                    secure_staging,
                    os.O_CREAT | os.O_EXCL | os.O_RDWR,
                    0o600,
                )
                os.close(descriptor)
                status = os.lstat(secure_staging)
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
                    with self._secure_paths(
                        staging, create=False
                    ) as secured:
                        secured[0].unlink()
                except (CodeGraphStoreError, FileNotFoundError):
                    pass
            if private_directory is not None:
                try:
                    with self._secure_paths(
                        private_directory, create=False
                    ) as secured:
                        secured[0].rmdir()
                except (CodeGraphStoreError, OSError):
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
            with self._secure_paths(
                identity.directory, staging, create=False
            ) as secured:
                directory_status = os.lstat(secured[0])
                status = os.lstat(secured[1])
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
            sidecars = tuple(
                Path(f"{staging_path}{suffix}")
                for suffix in ("-wal", "-shm")
            )
            with self._secure_paths(
                identity.directory, staging_path, *sidecars, create=False
            ) as secured:
                secure_directory, secure_staging, *secure_sidecars = secured
                directory_status = os.lstat(secure_directory)
                file_status = os.lstat(secure_staging)
                if (
                    not stat.S_ISDIR(directory_status.st_mode)
                    or (directory_status.st_dev, directory_status.st_ino)
                    != (identity.directory_dev, identity.directory_ino)
                    or not stat.S_ISREG(file_status.st_mode)
                    or (file_status.st_dev, file_status.st_ino)
                    != (identity.file_dev, identity.file_ino)
                ):
                    return
                for sidecar in secure_sidecars:
                    try:
                        status = os.lstat(sidecar)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(status.st_mode) and status.st_nlink == 1:
                        sidecar.unlink()
                secure_staging.unlink()
                secure_directory.rmdir()
        except OSError as exc:
            raise CodeGraphStoreError("cannot discard code graph staging") from exc
        finally:
            self._staging_identities.pop(staging_path, None)

    def cleanup_retained_publication_staging(
        self,
        *,
        now: datetime,
        retention_seconds: int,
        limit: int,
        exclude: Sequence[Path] = (),
    ) -> int:
        """Remove a bounded set of safely identified terminal session files."""
        if limit <= 0:
            return 0
        canonical = self._absolute(self.path)
        excluded = {self._absolute(path) for path in exclude}
        prefix = f"{canonical.name}.staging-"
        removed = 0
        try:
            candidates = sorted(canonical.parent.iterdir(), key=lambda path: path.name)
        except FileNotFoundError:
            return 0
        for directory in candidates:
            if removed >= limit or not directory.name.startswith(prefix):
                continue
            staging = directory / "snapshot.sqlite3"
            if staging in excluded:
                continue
            try:
                directory_status = os.lstat(directory)
                file_status = os.lstat(staging)
            except OSError:
                continue
            if (
                not stat.S_ISDIR(directory_status.st_mode)
                or stat.S_IMODE(directory_status.st_mode) != 0o700
                or not stat.S_ISREG(file_status.st_mode)
                or file_status.st_nlink != 1
            ):
                continue
            eligible = False
            connection = None
            try:
                uri = f"file:{quote(staging.as_posix(), safe='/:')}?mode=ro"
                connection = sqlite3.connect(uri, uri=True)
                row = connection.execute(
                    "SELECT state, lease_expires_at, updated_at "
                    "FROM publication_session"
                ).fetchone()
                if row is not None:
                    state = str(row[0])
                    terminal_at = datetime.fromisoformat(
                        str(row[2]).replace("Z", "+00:00")
                    )
                    if state == "staging":
                        lease = datetime.fromisoformat(
                            str(row[1]).replace("Z", "+00:00")
                        )
                        eligible = (
                            now >= lease
                            and (now - lease).total_seconds() >= retention_seconds
                        )
                    elif state in {"aborted", "expired", "conflicted", "failed"}:
                        eligible = (
                            now - terminal_at
                        ).total_seconds() >= retention_seconds
            except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
                eligible = (
                    now.timestamp() - file_status.st_mtime >= retention_seconds
                )
            finally:
                if connection is not None:
                    connection.close()
            if not eligible:
                continue
            self._staging_identities[staging] = _StagingIdentity(
                directory=directory,
                directory_dev=directory_status.st_dev,
                directory_ino=directory_status.st_ino,
                file_dev=file_status.st_dev,
                file_ino=file_status.st_ino,
            )
            self.discard_staging(staging)
            removed += 1
        return removed

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

    def open_existing(self) -> sqlite3.Connection:
        """Open the canonical database without creating a missing cache."""
        with self._secure_paths(self.path, create=False) as secured:
            return self._connect_existing(secured[0])

    @contextmanager
    def _read_connection(
        self, *, validate: bool
    ) -> Iterator[sqlite3.Connection]:
        """Open one sealed ordinary-path read connection."""
        connection = None
        wal_path = Path(f"{self.path}-wal")
        shm_path = Path(f"{self.path}-shm")
        try:
            with self._secure_paths(
                self.path, wal_path, shm_path, create=False
            ) as secured:
                secure_database, secure_wal, secure_shm = secured
                with _held_regular_file(
                    secure_database
                ) as database_identity:
                    canonical_database = self._absolute(self.path)
                    canonical_wal = Path(f"{canonical_database}-wal")
                    canonical_shm = Path(f"{canonical_database}-shm")
                    if not _same_regular_file(
                        canonical_database, database_identity
                    ):
                        raise OSError("code graph storage file changed")
                    _validate_optional_sidecar(secure_wal)
                    _validate_optional_sidecar(secure_shm)
                    _validate_optional_sidecar(canonical_wal)
                    _validate_optional_sidecar(canonical_shm)
                    pre_state = self._sealed_read_state()
                    database_uri = quote(
                        canonical_database.as_posix(), safe="/:"
                    )
                    immutable = (
                        not pre_state.wal_exists
                        and not pre_state.shm_exists
                    )
                    immutable_option = (
                        "&immutable=1" if immutable else ""
                    )
                    if self._sealed_read_state() != pre_state:
                        raise OSError("code graph storage changed")
                    connection = sqlite3.connect(
                        f"file:{database_uri}?mode=ro{immutable_option}",
                        uri=True,
                        timeout=BUSY_TIMEOUT_MS / 1000,
                        isolation_level=None,
                    )
                    if (
                        not _same_regular_file(
                            secure_database, database_identity
                        )
                        or not _same_regular_file(
                            canonical_database, database_identity
                        )
                    ):
                        raise OSError("code graph storage file changed")
                    _validate_optional_sidecar(secure_wal)
                    _validate_optional_sidecar(secure_shm)
                    _validate_optional_sidecar(canonical_wal)
                    _validate_optional_sidecar(canonical_shm)
                    if self._sealed_read_state() != pre_state:
                        raise OSError("code graph storage changed")
                    connection.execute("PRAGMA query_only = ON")
                    connection.execute("PRAGMA foreign_keys = ON")
                    connection.execute(
                        f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}"
                    )
                    if validate:
                        validate_schema(connection)
                    connection.execute("BEGIN")
                    if (
                        not _same_regular_file(
                            secure_database, database_identity
                        )
                        or not _same_regular_file(
                            canonical_database, database_identity
                        )
                    ):
                        raise OSError("code graph storage file changed")
                    _validate_optional_sidecar(secure_wal)
                    _validate_optional_sidecar(secure_shm)
                    _validate_optional_sidecar(canonical_wal)
                    _validate_optional_sidecar(canonical_shm)
                    if self._sealed_read_state() != pre_state:
                        raise OSError("code graph storage changed")
                    try:
                        yield connection
                    finally:
                        if connection.in_transaction:
                            connection.execute("ROLLBACK")
                        connection.close()
                        connection = None
                        if (
                            not _same_regular_file(
                                secure_database, database_identity
                            )
                            or not _same_regular_file(
                                canonical_database, database_identity
                            )
                        ):
                            raise CodeGraphStoreError(
                                "cannot hold code graph read snapshot"
                            )
                        _validate_optional_sidecar(secure_wal)
                        _validate_optional_sidecar(secure_shm)
                        _validate_optional_sidecar(canonical_wal)
                        _validate_optional_sidecar(canonical_shm)
                        if self._sealed_read_state() != pre_state:
                            raise CodeGraphStoreError(
                                "cannot hold code graph read snapshot"
                            )
        except CodeGraphStoreError:
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            raise CodeGraphStoreError(
                "cannot hold code graph read snapshot"
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    @contextmanager
    def read_lease(self) -> Iterator[sqlite3.Connection]:
        """Hold one read-only SQLite snapshot until caller completes checks."""
        with self._read_connection(validate=True) as connection:
            yield connection

    def prepare_staging(
        self,
        staging: str | Path,
        *,
        repository_id: str,
        expected_revision: str,
    ) -> None:
        """Validate and checkpoint a registered staging snapshot, then close it."""
        staging_path = self._absolute(Path(staging))
        identity = self._staging_identities.get(staging_path)
        if identity is None:
            raise CodeGraphStoreError("invalid code graph staging database")
        try:
            self._validate_staging_identity(staging_path, identity)
            with self._secure_paths(staging_path, create=False) as secured:
                connection = self._connect_existing(secured[0])
            try:
                _validate_persisted_snapshot(
                    connection, repository_id, expected_revision
                )
                checkpoint = connection.execute(
                    "PRAGMA wal_checkpoint(TRUNCATE)"
                ).fetchone()
                if checkpoint is None or checkpoint[0] != 0:
                    raise CodeGraphStoreError("cannot checkpoint code graph staging")
            finally:
                connection.close()
        except CodeGraphStoreError:
            self.discard_staging(staging_path)
            raise
        except (OSError, sqlite3.DatabaseError, ValueError) as exc:
            self.discard_staging(staging_path)
            raise CodeGraphStoreError("cannot prepare code graph staging") from exc

    def canonical_handles_available(self) -> bool:
        """Return whether no replace-incompatible canonical sidecar is present."""
        sidecars = tuple(
            Path(f"{self.path}{suffix}") for suffix in ("-wal", "-shm")
        )
        with self._secure_paths(
            self.path, *sidecars, create=True
        ) as secured:
            secure_database, *secure_sidecars = secured
            if not any(path.exists() for path in secure_sidecars):
                return True
            connection = None
            try:
                with _held_regular_file(
                    secure_database
                ) as database_identity:
                    canonical_database = self._absolute(self.path)
                    if not _same_regular_file(
                        canonical_database, database_identity
                    ):
                        return False
                    for sidecar in secure_sidecars:
                        _validate_optional_sidecar(sidecar)
                    uri = (
                        f"file:{quote(canonical_database.as_posix(), safe='/:')}"
                        "?mode=rw"
                    )
                    connection = sqlite3.connect(
                        uri,
                        uri=True,
                        timeout=BUSY_TIMEOUT_MS / 1000,
                        isolation_level=None,
                    )
                    connection.execute("PRAGMA query_only = ON")
                    connection.execute(
                        "SELECT COUNT(*) FROM sqlite_master"
                    ).fetchone()
            except (OSError, sqlite3.DatabaseError):
                return False
            finally:
                if connection is not None:
                    connection.close()
            return not any(path.exists() for path in secure_sidecars)

    def replace_staging(self, staging: str | Path) -> None:
        """Atomically replace the canonical database with prepared staging."""
        staging_path = self._absolute(Path(staging))
        identity = self._staging_identities.get(staging_path)
        if identity is None:
            raise CodeGraphStoreError("invalid code graph staging database")
        published = False
        try:
            self._validate_staging_identity(staging_path, identity)
            if not self.canonical_handles_available():
                raise CodeGraphStoreError("code graph canonical database is in use")
            canonical = self._absolute(self.path)
            with self._secure_paths(
                identity.directory, staging_path, canonical, create=True
            ) as secured:
                secure_directory, secure_staging, secure_canonical = secured
                directory_flags = (
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                source_descriptor = os.open(
                    secure_directory, directory_flags
                )
                try:
                    destination_flags = (
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                    )
                    destination_descriptor = os.open(
                        secure_canonical.parent, destination_flags
                    )
                    try:
                        self._validate_staging_identity(
                            staging_path, identity
                        )
                        os.replace(
                            secure_staging.name,
                            secure_canonical.name,
                            src_dir_fd=source_descriptor,
                            dst_dir_fd=destination_descriptor,
                        )
                        published = True
                    finally:
                        os.close(destination_descriptor)
                finally:
                    os.close(source_descriptor)
                self._fsync_directory(secure_directory)
                self._fsync_directory(secure_canonical.parent)
                try:
                    secure_directory.rmdir()
                except OSError:
                    pass
                else:
                    self._fsync_directory(secure_canonical.parent)
        except CodeGraphPublishedError:
            raise
        except CodeGraphStoreError as exc:
            if published:
                raise CodeGraphPublishedError(
                    "code graph snapshot published without durable namespace sync"
                ) from exc
            self.discard_staging(staging_path)
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            if published:
                raise CodeGraphPublishedError(
                    "code graph snapshot published without durable namespace sync"
                ) from exc
            self.discard_staging(staging_path)
            raise CodeGraphStoreError("cannot publish code graph staging") from exc
        finally:
            self._staging_identities.pop(staging_path, None)

    def prepare_metadata(
        self,
        metadata_path: str | Path,
        metadata: Mapping[str, object],
    ) -> Path:
        """Write and fsync metadata temp before canonical publication starts."""
        destination = self._absolute(Path(metadata_path))
        staging = destination.with_name(
            f"{destination.name}.staging-{uuid.uuid4()}"
        )
        try:
            with self._secure_paths(
                destination, staging, create=True
            ) as secured:
                secure_destination, secure_staging = secured
                if self.cache_base is None:
                    secure_destination.parent.mkdir(
                        parents=True, exist_ok=True
                    )
                payload = json.dumps(
                    metadata,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                descriptor = os.open(
                    secure_staging,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                try:
                    with os.fdopen(
                        descriptor, "wb", closefd=False
                    ) as handle:
                        handle.write(payload)
                        handle.flush()
                        os.fsync(handle.fileno())
                finally:
                    os.close(descriptor)
            self._metadata_staging[staging] = destination
            return staging
        except (OSError, TypeError, ValueError) as exc:
            try:
                with self._secure_paths(
                    staging, create=False
                ) as secured:
                    secured[0].unlink()
            except (CodeGraphStoreError, FileNotFoundError):
                pass
            raise CodeGraphStoreError("cannot prepare code graph metadata") from exc

    def discard_metadata(self, staging: str | Path) -> None:
        """Remove only a metadata temp created by this store instance."""
        staging_path = self._absolute(Path(staging))
        if staging_path not in self._metadata_staging:
            return
        try:
            with self._secure_paths(
                staging_path, create=False
            ) as secured:
                secured[0].unlink(missing_ok=True)
            self._metadata_staging.pop(staging_path, None)
        except OSError as exc:
            raise CodeGraphStoreError(
                "cannot discard code graph metadata"
            ) from exc

    def publish_metadata(
        self,
        metadata_path: str | Path,
        staging: str | Path,
        *,
        deadline: float | None = None,
    ) -> None:
        """Atomically replace metadata from a registered prepared temp."""
        destination = self._absolute(Path(metadata_path))
        staging_path = self._absolute(Path(staging))
        published = False
        try:
            with self._secure_paths(
                destination, staging_path, create=False
            ) as secured:
                self._replace_registered_metadata(
                    destination,
                    staging_path,
                    deadline=deadline,
                    secure_destination=secured[0],
                    secure_staging=secured[1],
                )
                published = True
        except CodeGraphPublishedError:
            raise
        except CodeGraphStoreError as exc:
            if published:
                raise CodeGraphPublishedError(
                    "code graph metadata published before context close failed"
                ) from exc
            raise
        except OSError as exc:
            if published:
                raise CodeGraphPublishedError(
                    "code graph metadata published before context close failed"
                ) from exc
            raise CodeGraphStoreError(
                "cannot publish code graph metadata"
            ) from exc

    def _replace_registered_metadata(
        self,
        destination: Path,
        staging_path: Path,
        *,
        deadline: float | None,
        secure_destination: Path | None = None,
        secure_staging: Path | None = None,
    ) -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise CodeGraphStoreError(
                "code graph metadata publication deadline exceeded"
            )
        registered = self._metadata_staging.pop(staging_path, None)
        if registered != destination:
            raise CodeGraphStoreError("invalid code graph metadata staging")
        actual_destination = secure_destination or destination
        actual_staging = secure_staging or staging_path
        published = False
        try:
            status = os.lstat(actual_staging)
            if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                raise OSError("invalid metadata staging")
            os.replace(actual_staging, actual_destination)
            published = True
            self._fsync_directory(actual_destination.parent)
        except CodeGraphStoreError as exc:
            if published:
                raise CodeGraphPublishedError(
                    "code graph metadata published without durable namespace sync"
                ) from exc
            raise
        except OSError as exc:
            if published:
                raise CodeGraphPublishedError(
                    "code graph metadata published without durable namespace sync"
                ) from exc
            try:
                actual_staging.unlink()
            except FileNotFoundError:
                pass
            raise CodeGraphStoreError("cannot publish code graph metadata") from exc

    def refresh_metadata_diagnostics(
        self,
        metadata_path: str | Path,
        staging: str | Path,
        *,
        deadline: float | None = None,
    ) -> None:
        """Add timings atomically; this refresh's own write cost is excluded."""
        destination = self._absolute(Path(metadata_path))
        staging_path = self._absolute(Path(staging))
        if deadline is not None and time.monotonic() >= deadline:
            raise CodeGraphStoreError(
                "code graph diagnostics refresh deadline exceeded"
            )
        if self._metadata_staging.get(staging_path) != destination:
            raise CodeGraphStoreError("invalid code graph metadata staging")
        published = False
        try:
            with self._secure_paths(
                destination, staging_path, create=False
            ) as secured:
                secure_destination, secure_staging = secured
                try:
                    current = json.loads(
                        secure_destination.read_text(encoding="utf-8")
                    )
                    candidate = json.loads(
                        secure_staging.read_text(encoding="utf-8")
                    )
                except (
                    FileNotFoundError,
                    OSError,
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                ) as exc:
                    raise CodeGraphStoreError(
                        "cannot validate code graph diagnostics refresh"
                    ) from exc
                if not isinstance(current, dict) or not isinstance(
                    candidate, dict
                ):
                    raise CodeGraphStoreError(
                        "invalid code graph diagnostics refresh"
                    )
                diagnostics = {"duration_ms", "phase_timings_ms"}
                current_lifecycle = {
                    key: value for key, value in current.items()
                    if key not in diagnostics
                }
                candidate_lifecycle = {
                    key: value for key, value in candidate.items()
                    if key not in diagnostics
                }
                duration = candidate.get("duration_ms")
                timings = candidate.get("phase_timings_ms")
                if (
                    current.get("state") != "ready"
                    or candidate_lifecycle != current_lifecycle
                    or type(duration) is not int
                    or duration < 0
                    or not isinstance(timings, dict)
                    or any(
                        type(value) is not int or value < 0
                        for value in timings.values()
                    )
                ):
                    raise CodeGraphStoreError(
                        "invalid code graph diagnostics refresh"
                    )
                self._replace_registered_metadata(
                    destination,
                    staging_path,
                    deadline=deadline,
                    secure_destination=secure_destination,
                    secure_staging=secure_staging,
                )
                published = True
        except CodeGraphPublishedError:
            raise
        except CodeGraphStoreError as exc:
            if published:
                raise CodeGraphPublishedError(
                    "code graph diagnostics published before context close failed"
                ) from exc
            raise
        except OSError as exc:
            if published:
                raise CodeGraphPublishedError(
                    "code graph diagnostics published before context close failed"
                ) from exc
            raise CodeGraphStoreError(
                "cannot refresh code graph diagnostics"
            ) from exc

    def publish_staging(
        self,
        staging: str | Path,
        *,
        repository_id: str,
        expected_revision: str,
    ) -> None:
        """Publish registered staging while caller holds the per-domain writer lock."""
        self.prepare_staging(
            staging,
            repository_id=repository_id,
            expected_revision=expected_revision,
        )
        self.replace_staging(staging)


__all__ = [
    "BUSY_TIMEOUT_MS",
    "INDEXES",
    "SCHEMA_VERSION",
    "TABLES",
    "CodeGraphSchemaError",
    "CodeGraphPublishedError",
    "CodeGraphStore",
    "CodeGraphStoreError",
    "code_graph_read_lock",
    "code_graph_write_lock",
    "run_publication_protocol",
]
