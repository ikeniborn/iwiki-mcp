"""Runtime coordination for the base-local SQLite graph cache."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import tempfile
from typing import Iterable

from filelock import Timeout

from .base import domain_dir, list_domains
from .engine import graph_store
from .engine.links import parse_link_targets, slugify_heading
from .engine.okf_artifacts import RESERVED_OKF
from .lock import base_lock
from .sync import _run, is_git_repo


class GraphRuntimeError(RuntimeError):
    """Raised when graph freshness cannot be established safely."""


@dataclass(frozen=True)
class MarkdownFingerprint:
    value: str
    indexed_commit: str | None


@dataclass(frozen=True)
class RevisionChange:
    old_revision: str
    new_revision: str
    domains: tuple[str, ...]
    complete: bool = True


@dataclass(frozen=True)
class IncomingCandidate:
    domain: str
    file: str


@dataclass(frozen=True)
class MarkdownCandidateSnapshot:
    candidates: tuple[IncomingCandidate, ...]
    expected_hashes: tuple[tuple[str, str, str], ...]


class MarkdownSnapshotChanged(RuntimeError):
    """Raised when a Markdown discovery snapshot changes during capture."""


def _target_identity(target_page_id: str) -> tuple[str, str] | None:
    domain, separator, page = target_page_id.partition("/")
    if not separator or not domain or not page:
        return None
    return domain, page


def _has_incoming_target(
    content: str,
    source_domain: str,
    target_page_id: str,
    target_anchor: str | None,
) -> bool:
    identity = _target_identity(target_page_id)
    if identity is None:
        return False
    target_domain, target_page = identity
    normalized_anchor = (
        slugify_heading(target_anchor) if target_anchor is not None else None
    )
    return any(
        target.kind == "cross"
        and target.target_domain == target_domain
        and target.target_page == target_page
        and (
            normalized_anchor is None
            or target.target_anchor == normalized_anchor
        )
        for target in parse_link_targets(content, source_domain)
    )


def _read_scoped_markdown(
    base: str, domain: str, file: str
) -> bytes | None:
    relative = PurePosixPath(file)
    parts = relative.parts
    if (
        relative.is_absolute()
        or not parts
        or any(part in ("", ".", "..") for part in parts)
        or not file.endswith(".md")
        or file in RESERVED_OKF
    ):
        return None
    try:
        directory_flags = (
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
        )
        file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    except AttributeError:
        return None
    descriptors: list[int] = []
    try:
        current = os.open(domain_dir(base, domain), directory_flags)
        descriptors.append(current)
        for part in parts[:-1]:
            current = os.open(part, directory_flags, dir_fd=current)
            descriptors.append(current)
        final = os.open(parts[-1], file_flags, dir_fd=current)
        descriptors.append(final)
        before = os.fstat(final)
        if not stat.S_ISREG(before.st_mode):
            return None
        payload = bytearray()
        while block := os.read(final, 1024 * 1024):
            payload.extend(block)
        after = os.fstat(final)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            return None
        return bytes(payload)
    except (OSError, NotImplementedError):
        return None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@dataclass(frozen=True)
class ScopedGraph:
    store: graph_store.GraphStore
    domains: tuple[str, ...]
    expected_fingerprints: tuple[tuple[str, str], ...]

    def neighbors(self, page_id: str) -> tuple[str, ...]:
        """Return indexed incoming and outgoing neighbors inside this scope."""
        page_domain = page_id.split("/", 1)[0]
        if page_domain not in self.domains:
            return ()
        placeholders = ", ".join("?" for _ in self.domains)
        expected = dict(self.expected_fingerprints)
        try:
            with self.store.read_snapshot() as connection:
                metadata = {
                    row[0]: (row[1], row[2])
                    for row in connection.execute(
                        "SELECT domain, markdown_fingerprint, state FROM domains "
                        f"WHERE domain IN ({placeholders})",
                        self.domains,
                    )
                }
                if any(
                    metadata.get(domain) != (expected[domain], "ready")
                    for domain in self.domains
                ):
                    raise GraphRuntimeError("graph scope is unavailable")
                seed = connection.execute(
                    "SELECT 1 FROM pages WHERE page_id = ? "
                    f"AND domain IN ({placeholders})",
                    (page_id, *self.domains),
                ).fetchone()
                if seed is None:
                    return ()
                rows = connection.execute(
                    "SELECT edges.target_page_id AS neighbor FROM edges "
                    "JOIN pages AS target "
                    "ON target.page_id = edges.target_page_id "
                    "WHERE edges.source_page_id = ? "
                    f"AND target.domain IN ({placeholders}) "
                    "UNION "
                    "SELECT edges.source_page_id AS neighbor FROM edges "
                    "JOIN pages AS source "
                    "ON source.page_id = edges.source_page_id "
                    "WHERE edges.target_page_id = ? "
                    f"AND source.domain IN ({placeholders}) "
                    "ORDER BY neighbor",
                    (page_id, *self.domains, page_id, *self.domains),
                )
                return tuple(row[0] for row in rows)
        except GraphRuntimeError:
            raise
        except graph_store.GraphStoreError as exc:
            raise GraphRuntimeError("graph scope is unavailable") from exc

    def ensure_current(self) -> None:
        """Reject a snapshot when Markdown changed during graph traversal."""
        expected = dict(self.expected_fingerprints)
        try:
            changed = [
                domain
                for domain in self.domains
                if markdown_fingerprint(str(self.store.base), domain).value
                != expected[domain]
            ]
            if changed:
                for domain in changed:
                    try:
                        self.store.mark_domain_dirty(domain)
                    except graph_store.GraphStoreError:
                        pass
                raise GraphRuntimeError("graph scope is unavailable")
            with self.store.read_snapshot() as connection:
                placeholders = ", ".join("?" for _ in self.domains)
                metadata = {
                    row[0]: (row[1], row[2])
                    for row in connection.execute(
                        "SELECT domain, markdown_fingerprint, state FROM domains "
                        f"WHERE domain IN ({placeholders})",
                        self.domains,
                    )
                }
            if any(
                metadata.get(domain) != (expected[domain], "ready")
                for domain in self.domains
            ):
                raise GraphRuntimeError("graph scope is unavailable")
        except GraphRuntimeError:
            raise
        except (graph_store.GraphStoreError, OSError) as exc:
            raise GraphRuntimeError("graph scope is unavailable") from exc


def _is_graph_markdown(path: str, domain: str) -> bool:
    prefix = f"{domain}/"
    if not path.startswith(prefix) or not path.endswith(".md"):
        return False
    return path[len(prefix):] not in RESERVED_OKF


def _tracked_markdown(base: str, domain: str) -> dict[str, str]:
    result = _run(base, "ls-files", "-s", "-z", "--", domain)
    if result.returncode != 0:
        raise GraphRuntimeError("cannot inspect graph freshness")
    identities: dict[str, str] = {}
    for record in result.stdout.split("\0"):
        if not record:
            continue
        metadata, path = record.split("\t", 1)
        mode, object_id, stage = metadata.split()
        if stage == "0" and _is_graph_markdown(path, domain):
            identities[path] = f"git:{mode}:{object_id}"
    return identities


def _apply_worktree_changes(
    base: str, domain: str, identities: dict[str, str]
) -> None:
    result = _run(
        base,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        domain,
    )
    if result.returncode != 0:
        raise GraphRuntimeError("cannot inspect graph freshness")
    records = result.stdout.split("\0")
    index = 0
    root = Path(base)
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        status = record[:2]
        path = record[3:]
        if "R" in status or "C" in status:
            if index >= len(records):
                raise GraphRuntimeError("cannot inspect graph freshness")
            old_path = records[index]
            index += 1
            if _is_graph_markdown(old_path, domain):
                identities.pop(old_path, None)
        if not _is_graph_markdown(path, domain):
            continue
        file_path = root / path
        if "D" in status or not file_path.is_file():
            identities.pop(path, None)
            continue
        try:
            content_hash = sha256(file_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise GraphRuntimeError("cannot inspect graph freshness") from exc
        identities[path] = f"worktree:{content_hash}"


def _apply_ignored_markdown(
    base: str, domain: str, identities: dict[str, str]
) -> None:
    result = _run(
        base,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "-z",
        "--",
        domain,
    )
    if result.returncode != 0:
        raise GraphRuntimeError("cannot inspect graph freshness")
    root = Path(base)
    for path in result.stdout.split("\0"):
        if not path or not _is_graph_markdown(path, domain):
            continue
        try:
            content_hash = sha256((root / path).read_bytes()).hexdigest()
        except OSError as exc:
            raise GraphRuntimeError("cannot inspect graph freshness") from exc
        identities[path] = f"worktree:{content_hash}"


def markdown_fingerprint(base: str, domain: str) -> MarkdownFingerprint:
    """Hash sorted Markdown paths and Git/worktree content identities."""
    if not is_git_repo(base):
        raise GraphRuntimeError("graph freshness requires a Git base")
    identities = _tracked_markdown(base, domain)
    _apply_worktree_changes(base, domain, identities)
    _apply_ignored_markdown(base, domain, identities)
    head = _run(base, "rev-parse", "--verify", "HEAD")
    indexed_commit = head.stdout.strip() if head.returncode == 0 else None
    encoded = json.dumps(
        sorted(identities.items()), separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return MarkdownFingerprint(sha256(encoded).hexdigest(), indexed_commit)


def _domain_metadata(
    store: graph_store.GraphStore, domain: str
) -> tuple[str, str] | None:
    with store.read_snapshot() as connection:
        row = connection.execute(
            "SELECT markdown_fingerprint, state FROM domains WHERE domain = ?",
            (domain,),
        ).fetchone()
    return None if row is None else (row[0], row[1])


def _inspect_domains(
    base: str, store: graph_store.GraphStore, domains: tuple[str, ...]
) -> tuple[dict[str, str], tuple[str, ...]]:
    fingerprints: dict[str, str] = {}
    stale: list[str] = []
    for domain in domains:
        fingerprint = markdown_fingerprint(base, domain)
        metadata = _domain_metadata(store, domain)
        if metadata != (fingerprint.value, "ready"):
            stale.append(domain)
        else:
            fingerprints[domain] = fingerprint.value
    return fingerprints, tuple(stale)


def _load_fresh_domains(
    base: str, store: graph_store.GraphStore, domains: tuple[str, ...]
) -> dict[str, str] | None:
    fingerprints, stale = _inspect_domains(base, store, domains)
    return None if stale else fingerprints


def incoming_candidates(
    base: str,
    domains: tuple[str, ...],
    target_page_id: str,
    target_anchor: str | None = None,
) -> tuple[IncomingCandidate, ...] | None:
    """Return canonically verified incoming pages from a ready graph scope."""
    requested = tuple(sorted(set(domains)))
    store = graph_store.GraphStore(base)
    if not requested or _target_identity(target_page_id) is None:
        return ()
    if not store.path.is_file():
        return None
    normalized_anchor = (
        slugify_heading(target_anchor) if target_anchor is not None else None
    )
    try:
        expected = _load_fresh_domains(base, store, requested)
        if expected is None:
            return None
        indexed = store.query_incoming_pages(
            requested, target_page_id, normalized_anchor
        )
        candidates: list[IncomingCandidate] = []
        for page in indexed:
            content_bytes = _read_scoped_markdown(base, page.domain, page.file)
            if content_bytes is None:
                return None
            if sha256(content_bytes).hexdigest() != page.content_hash:
                return None
            content = content_bytes.decode("utf-8")
            if _has_incoming_target(
                content,
                page.domain,
                target_page_id,
                normalized_anchor,
            ):
                candidates.append(IncomingCandidate(page.domain, page.file))
        current = _load_fresh_domains(base, store, requested)
        if current != expected:
            return None
        return tuple(candidates)
    except (
        graph_store.GraphStoreError,
        GraphRuntimeError,
        OSError,
        UnicodeError,
    ):
        return None


def _snapshot_markdown_files(root: Path) -> tuple[Path, ...]:
    resolved_root = root.resolve()
    files: list[Path] = []
    for path in root.rglob("*.md"):
        try:
            relative = path.relative_to(root).as_posix()
            path.resolve().relative_to(resolved_root)
        except (OSError, ValueError):
            continue
        if path.is_file() and relative not in RESERVED_OKF:
            files.append(path)
    return tuple(sorted(files))


def markdown_incoming_snapshot(
    base: str,
    domains: tuple[str, ...],
    target_page_id: str,
    target_anchor: str | None = None,
) -> MarkdownCandidateSnapshot:
    """Capture and immediately revalidate one scoped Markdown snapshot."""
    requested = tuple(sorted(set(domains)))
    try:
        initial_fingerprints = {
            domain: markdown_fingerprint(base, domain).value
            for domain in requested
        }
    except (GraphRuntimeError, OSError) as exc:
        raise GraphRuntimeError("Markdown scope is unavailable") from exc
    expected: list[tuple[str, str, str]] = []
    candidates: list[IncomingCandidate] = []
    for domain in requested:
        root = Path(domain_dir(base, domain))
        if not root.is_dir():
            raise GraphRuntimeError("Markdown scope is unavailable")
        for path in _snapshot_markdown_files(root):
            file = path.relative_to(root).as_posix()
            try:
                content_bytes = _read_scoped_markdown(base, domain, file)
                if content_bytes is None:
                    raise OSError("unsafe Markdown path")
                content = content_bytes.decode("utf-8")
            except (OSError, UnicodeError) as exc:
                raise GraphRuntimeError("Markdown scope is unavailable") from exc
            content_hash = sha256(content_bytes).hexdigest()
            expected.append((domain, file, content_hash))
            if _has_incoming_target(
                content, domain, target_page_id, target_anchor
            ):
                candidates.append(IncomingCandidate(domain, file))

    for domain, file, content_hash in expected:
        current = _read_scoped_markdown(base, domain, file)
        if current is None:
            raise MarkdownSnapshotChanged
        current_hash = sha256(current).hexdigest()
        if current_hash != content_hash:
            raise MarkdownSnapshotChanged

    try:
        current_fingerprints = {
            domain: markdown_fingerprint(base, domain).value
            for domain in requested
        }
    except (GraphRuntimeError, OSError) as exc:
        raise MarkdownSnapshotChanged from exc
    if current_fingerprints != initial_fingerprints:
        raise MarkdownSnapshotChanged

    return MarkdownCandidateSnapshot(tuple(candidates), tuple(expected))


def _is_busy_error(error: BaseException) -> bool:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, sqlite3.Error):
            code = getattr(current, "sqlite_errorcode", None)
            if code is not None and code & 0xFF in {
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            }:
                return True
            if "locked" in str(current).casefold() or "busy" in str(current).casefold():
                return True
        current = current.__cause__
    return False


def _is_corrupt_error(error: BaseException) -> bool:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, sqlite3.Error):
            code = getattr(current, "sqlite_errorcode", None)
            if code is not None and code & 0xFF in {
                sqlite3.SQLITE_CORRUPT,
                sqlite3.SQLITE_NOTADB,
            }:
                return True
            message = str(current).casefold()
            if "malformed" in message or "not a database" in message:
                return True
        current = current.__cause__
    return False


def _build_replacement_store(store: graph_store.GraphStore) -> Path:
    store.path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(
        prefix=".graph-replacement-",
        suffix=".sqlite3",
        dir=store.path.parent,
    )
    os.close(descriptor)
    path = Path(raw_path)
    replacement = graph_store.GraphStore(store.base)
    replacement.path = path
    connection: sqlite3.Connection | None = None
    try:
        connection = replacement.connect()
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or checkpoint[0] != 0:
            raise GraphRuntimeError("cannot prepare graph replacement")
        mode = connection.execute("PRAGMA journal_mode = DELETE").fetchone()
        if mode is None or str(mode[0]).casefold() != "delete":
            raise GraphRuntimeError("cannot prepare graph replacement")
        connection.close()
        connection = None
        return path
    except Exception:
        if connection is not None:
            connection.close()
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _replace_quiescent_store(store: graph_store.GraphStore, temporary: Path) -> None:
    connection = sqlite3.connect(store.path, timeout=0.25, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout = 250")
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or checkpoint[0] != 0:
            raise GraphRuntimeError("graph store is busy")
        mode = connection.execute("PRAGMA journal_mode = DELETE").fetchone()
        if mode is None or str(mode[0]).casefold() != "delete":
            raise GraphRuntimeError("graph store is busy")
        connection.execute("BEGIN EXCLUSIVE")
        os.replace(temporary, store.path)
    except sqlite3.DatabaseError as exc:
        if _is_busy_error(exc):
            raise GraphRuntimeError("graph store is busy") from exc
        raise GraphRuntimeError("cannot replace graph store") from exc
    finally:
        connection.close()


def _replace_derived_store(
    store: graph_store.GraphStore, error: graph_store.GraphStoreError
) -> graph_store.GraphStore:
    if _is_busy_error(error):
        raise error
    temporary = _build_replacement_store(store)
    try:
        if isinstance(error, graph_store.GraphSchemaError):
            _replace_quiescent_store(store, temporary)
        elif _is_corrupt_error(error):
            if Path(f"{store.path}-wal").exists() or Path(
                f"{store.path}-shm"
            ).exists():
                raise GraphRuntimeError("cannot replace graph store")
            os.replace(temporary, store.path)
        else:
            raise error
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return graph_store.GraphStore(store.base)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _rebuild_locked(
    base: str,
    domains: tuple[str, ...],
    store: graph_store.GraphStore | None = None,
) -> dict[str, str]:
    current_store = store or graph_store.GraphStore(base)
    try:
        validation_connection = current_store.connect()
        validation_connection.close()
    except (graph_store.GraphSchemaError, graph_store.GraphStoreError) as exc:
        current_store = _replace_derived_store(current_store, exc)

    for domain in domains:
        fingerprint = markdown_fingerprint(base, domain)
        current_store.rebuild_domain(
            domain,
            domain_dir(base, domain),
            markdown_fingerprint=fingerprint.value,
            fingerprint_provider=lambda domain=domain: markdown_fingerprint(
                base, domain
            ).value,
            indexed_commit=fingerprint.indexed_commit,
            indexed_at=_timestamp(),
        )
    fingerprints = _load_fresh_domains(base, current_store, domains)
    if fingerprints is None:
        raise GraphRuntimeError("graph snapshot changed during rebuild")
    return fingerprints


def _provider(
    store: graph_store.GraphStore,
    domains: tuple[str, ...],
    fingerprints: dict[str, str],
) -> ScopedGraph:
    return ScopedGraph(store, domains, tuple(sorted(fingerprints.items())))


def scoped_graph(
    base: str, domains: Iterable[str], *, timeout: float = 15.0
) -> ScopedGraph | None:
    """Return a fresh scoped provider, or ``None`` for Markdown fallback."""
    requested = tuple(sorted(set(domains)))
    if not requested or not is_git_repo(base):
        return None
    store = graph_store.GraphStore(base)
    try:
        fingerprints, stale = _inspect_domains(base, store, requested)
        if not stale:
            return _provider(store, requested, fingerprints)
        for domain in stale:
            store.mark_domain_dirty(domain)
    except (
        graph_store.GraphDomainUnavailable,
        graph_store.GraphStoreError,
        GraphRuntimeError,
        OSError,
    ):
        stale = requested

    try:
        with base_lock(base, timeout):
            try:
                fingerprints, stale = _inspect_domains(base, store, requested)
            except (
                graph_store.GraphDomainUnavailable,
                graph_store.GraphStoreError,
                GraphRuntimeError,
                OSError,
            ):
                fingerprints, stale = {}, requested
            if stale:
                _rebuild_locked(base, stale, store)
                fingerprints = _load_fresh_domains(base, store, requested)
            if fingerprints is None:
                raise GraphRuntimeError("graph snapshot changed during rebuild")
            return _provider(store, requested, fingerprints)
    except (
        Timeout,
        graph_store.GraphDomainUnavailable,
        graph_store.GraphStoreError,
        GraphRuntimeError,
        OSError,
    ):
        return None


def _path_domain(path: str) -> str | None:
    parts = Path(path).parts
    if len(parts) < 2 or parts[0].startswith("."):
        return None
    relative = Path(*parts[1:]).as_posix()
    if not relative.endswith(".md") or relative in RESERVED_OKF:
        return None
    return parts[0]


def changed_markdown_domains(
    base: str, old_revision: str, new_revision: str
) -> RevisionChange:
    """Resolve affected domain names without exposing filesystem paths."""
    result = _run(
        base,
        "diff",
        "--name-status",
        "-z",
        old_revision,
        new_revision,
        "--",
        "*.md",
    )
    if result.returncode != 0:
        return RevisionChange(
            old_revision,
            new_revision,
            tuple(list_domains(base)),
            complete=False,
        )
    records = result.stdout.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(records):
        status = records[index]
        index += 1
        if not status:
            continue
        path_count = 2 if status.startswith(("R", "C")) else 1
        if index + path_count > len(records):
            return RevisionChange(
                old_revision,
                new_revision,
                tuple(list_domains(base)),
                complete=False,
            )
        paths.extend(records[index:index + path_count])
        index += path_count
    domains = {
        domain
        for path in paths
        if path and (domain := _path_domain(path)) is not None
    }
    return RevisionChange(old_revision, new_revision, tuple(sorted(domains)))


def refresh_revision_change(
    base: str,
    old_revision: str,
    new_revision: str,
    *,
    lock_held: bool,
) -> RevisionChange:
    """Refresh pulled domains while the sync caller still owns the base lock."""
    if not lock_held:
        raise GraphRuntimeError("pull refresh requires the existing base lock")
    change = changed_markdown_domains(base, old_revision, new_revision)
    if change.domains:
        store = graph_store.GraphStore(base)
        try:
            for domain in change.domains:
                store.mark_domain_dirty(domain)
        except graph_store.GraphStoreError:
            pass
        _rebuild_locked(base, change.domains, store)
    return change
