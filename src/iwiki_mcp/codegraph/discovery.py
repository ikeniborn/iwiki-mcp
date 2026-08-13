"""Language-neutral, contained project source discovery."""
from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

import pathspec

from .config import CodeGraphConfig


class DiscoveryError(RuntimeError):
    """Raised with a stable code when project discovery cannot start safely."""


_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]*$")
_EXCLUDED_DIRECTORIES = {
    ".git",
    ".iwiki",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "coverage",
    "__pycache__",
    "vendor",
    "generated",
}
_TEST_DIRECTORIES = {"test", "tests"}
_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd
_SCANDIR_SUPPORTS_FD = os.scandir in os.supports_fd
_STAT_SUPPORTS_DIR_FD = os.stat in os.supports_dir_fd
_STAT_SUPPORTS_FOLLOW_SYMLINKS = os.stat in os.supports_follow_symlinks
_MAX_DIRECTORY_DEPTH = 256
_MAX_SCANNED_DIRECTORIES = 10_000
_MAX_SCANNED_ENTRIES = 100_000


def _safe_relative_path(value: str) -> bool:
    if not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return (
        path.parts
        and not path.is_absolute()
        and ".." not in path.parts
        and value == path.as_posix()
    )


@dataclass(frozen=True)
class DiscoveryWarning:
    code: str
    path: str
    detail: str

    def __post_init__(self) -> None:
        if not _SAFE_CODE.fullmatch(self.code):
            raise ValueError("invalid discovery warning code")
        if self.path != "." and not _safe_relative_path(self.path):
            raise ValueError("invalid discovery warning path")
        if not _SAFE_CODE.fullmatch(self.detail):
            raise ValueError("invalid discovery warning detail")


@dataclass(frozen=True)
class SourceFile:
    path: str
    content: bytes
    content_hash: str
    size_bytes: int

    def __post_init__(self) -> None:
        if not _safe_relative_path(self.path):
            raise ValueError("invalid source path")


@dataclass(frozen=True)
class DiscoverySnapshot:
    files: tuple[SourceFile, ...]
    warnings: tuple[DiscoveryWarning, ...]
    truncated: bool


@dataclass
class _DirectoryFrame:
    descriptor: int
    relative_path: str
    depth: int
    names: tuple[str, ...] | None = None
    index: int = 0


class _CandidateRejected(Exception):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail


def _warning(code: str, relative_path: str, detail: str) -> DiscoveryWarning:
    return DiscoveryWarning(code=code, path=relative_path, detail=detail)


def _canonical_root(project: os.PathLike[str] | str) -> Path:
    try:
        root = Path(project).resolve(strict=True)
        if not root.is_dir():
            raise DiscoveryError("project_root_unavailable")
    except DiscoveryError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise DiscoveryError("project_root_unavailable") from None
    return root


def _normalize_extensions(extensions: Iterable[str]) -> frozenset[str]:
    normalized = set()
    try:
        values = tuple(extensions)
    except Exception:
        raise DiscoveryError("invalid_extensions") from None
    for value in values:
        if type(value) is not str or not value or "/" in value or "\\" in value:
            raise DiscoveryError("invalid_extensions")
        suffix = value if value.startswith(".") else f".{value}"
        if suffix == ".":
            raise DiscoveryError("invalid_extensions")
        normalized.add(suffix.casefold())
    if not normalized:
        raise DiscoveryError("invalid_extensions")
    return frozenset(normalized)


def _is_secret_like(relative_path: str) -> bool:
    name = PurePosixPath(relative_path).name.casefold()
    return (
        name == ".env"
        or name.startswith(".env.")
        or name.endswith(".pem")
        or name.endswith(".key")
        or name in {"id_rsa", "id_ed25519"}
        or name.startswith("credentials")
        or name.startswith("secrets")
    )


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_size,
        left.st_mtime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_size,
        right.st_mtime_ns,
    )


def _same_directory(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        stat.S_IFMT(left.st_mode),
    ) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
    )


def _open_flags(*, directory: bool = False) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if directory and hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if not directory and hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    return flags


def _open_root_directory(root: Path) -> int:
    try:
        expected = root.stat(follow_symlinks=False)
        descriptor = os.open(root, _open_flags(directory=True))
    except OSError as exc:
        raise _CandidateRejected("project_root_unavailable", "open_failed") from exc
    try:
        actual = os.fstat(descriptor)
        if not stat.S_ISDIR(actual.st_mode) or not _same_directory(expected, actual):
            raise _CandidateRejected(
                "project_root_unavailable", "identity_changed"
            )
    except _CandidateRejected:
        os.close(descriptor)
        raise
    except Exception:
        os.close(descriptor)
        raise _CandidateRejected(
            "project_root_unavailable", "validation_failed"
        ) from None
    return descriptor


def _open_directory(
    parent_descriptor: int, name: str, expected_stat: os.stat_result
) -> int:
    try:
        descriptor = os.open(
            name,
            _open_flags(directory=True),
            dir_fd=parent_descriptor,
        )
    except OSError as exc:
        raise _CandidateRejected("directory_unavailable", "open_failed") from exc
    try:
        actual = os.fstat(descriptor)
        if not stat.S_ISDIR(actual.st_mode) or not _same_directory(
            expected_stat, actual
        ):
            raise _CandidateRejected("directory_changed", "identity_changed")
    except _CandidateRejected:
        os.close(descriptor)
        raise
    except Exception:
        os.close(descriptor)
        raise _CandidateRejected(
            "directory_unavailable", "validation_failed"
        ) from None
    return descriptor


def _secure_capabilities_available() -> bool:
    return (
        _OPEN_SUPPORTS_DIR_FD
        and _SCANDIR_SUPPORTS_FD
        and _STAT_SUPPORTS_DIR_FD
        and _STAT_SUPPORTS_FOLLOW_SYMLINKS
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NONBLOCK")
    )


def _read_contained_file(
    directory_descriptor: int, name: str, expected_stat: os.stat_result
) -> bytes:
    try:
        descriptor = os.open(
            name,
            _open_flags(),
            dir_fd=directory_descriptor,
        )
    except OSError as exc:
        raise _CandidateRejected("file_unavailable", "open_failed") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not _same_file(expected_stat, before):
            raise _CandidateRejected("file_changed", "identity_changed")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            content = handle.read(expected_stat.st_size + 1)
        after = os.fstat(descriptor)
        try:
            current = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise _CandidateRejected("file_changed", "identity_changed") from exc
        if (
            not _same_file(before, after)
            or not _same_file(after, current)
            or len(content) != expected_stat.st_size
        ):
            raise _CandidateRejected("file_changed", "identity_changed")
        return content
    finally:
        os.close(descriptor)


def _read_ignore_file(
    root_descriptor: int, name: str, max_file_bytes: int
) -> tuple[list[str], DiscoveryWarning | None]:
    try:
        candidate_stat = os.stat(
            name,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return [], None
    except OSError:
        raise DiscoveryError("ignore_file_unavailable") from None
    if stat.S_ISLNK(candidate_stat.st_mode):
        raise DiscoveryError("ignore_file_unavailable")
    if not stat.S_ISREG(candidate_stat.st_mode):
        raise DiscoveryError("ignore_file_unavailable")
    if candidate_stat.st_size > max_file_bytes:
        raise DiscoveryError("ignore_file_too_large")
    try:
        content = _read_contained_file(root_descriptor, name, candidate_stat)
        return content.decode("utf-8").splitlines(), None
    except UnicodeDecodeError:
        raise DiscoveryError("ignore_file_invalid") from None
    except _CandidateRejected:
        raise DiscoveryError("ignore_file_unavailable") from None


def _ignore_spec(
    root_descriptor: int,
    configured: Iterable[str],
    max_file_bytes: int,
) -> tuple[pathspec.GitIgnoreSpec, list[DiscoveryWarning]]:
    lines = []
    warnings = []
    for name in (".gitignore", ".iwikiignore"):
        loaded, warning = _read_ignore_file(
            root_descriptor, name, max_file_bytes
        )
        lines.extend(loaded)
        if warning is not None:
            warnings.append(warning)
    lines.extend(item.replace("\\", "/") for item in configured)
    try:
        return pathspec.GitIgnoreSpec.from_lines(lines), warnings
    except (TypeError, ValueError):
        raise DiscoveryError("ignore_rules_invalid") from None


def _relative_child(parent: str, name: str) -> str:
    return f"{parent}/{name}" if parent else name


def discover_sources(
    project: os.PathLike[str] | str,
    config: CodeGraphConfig,
    *,
    extensions: Iterable[str],
) -> DiscoverySnapshot:
    """Return a stable snapshot of allowed, regular source files under project."""
    root = _canonical_root(project)
    allowed_extensions = _normalize_extensions(extensions)
    if not _secure_capabilities_available():
        raise DiscoveryError("secure_traversal_unavailable")
    try:
        root_descriptor = _open_root_directory(root)
    except _CandidateRejected:
        raise DiscoveryError("project_root_unavailable") from None
    try:
        ignore_spec, initial_warnings = _ignore_spec(
            root_descriptor, config.exclude, config.max_file_bytes
        )
        warnings = set(initial_warnings)
        files = []
        truncated = False
        scanned_directories = 1
        scanned_entries = 0
        stop = False
        frames = [_DirectoryFrame(root_descriptor, "", 0)]
        try:
            while frames and not stop:
                frame = frames[-1]
                if frame.names is None:
                    try:
                        remaining_entries = (
                            _MAX_SCANNED_ENTRIES - scanned_entries
                        )
                        observed_names = []
                        overflow = remaining_entries <= 0
                        with os.scandir(frame.descriptor) as iterator:
                            if not overflow:
                                for item in iterator:
                                    if len(observed_names) >= remaining_entries:
                                        overflow = True
                                        break
                                    observed_names.append(item.name)
                    except OSError:
                        if not frame.relative_path:
                            raise DiscoveryError(
                                "project_root_unavailable"
                            ) from None
                        warnings.add(
                            _warning(
                                "directory_unavailable",
                                frame.relative_path,
                                "scan_failed",
                            )
                        )
                        finished = frames.pop()
                        os.close(finished.descriptor)
                        continue
                    if overflow:
                        truncated = True
                        warnings.add(
                            _warning(
                                "entry_limit_reached",
                                frame.relative_path or ".",
                                "max_scanned_entries",
                            )
                        )
                        stop = True
                        continue
                    scanned_entries += len(observed_names)
                    frame.names = tuple(sorted(observed_names))
                if frame.index >= len(frame.names):
                    finished = frames.pop()
                    if finished.descriptor != root_descriptor:
                        os.close(finished.descriptor)
                    continue

                name = frame.names[frame.index]
                frame.index += 1
                relative_path = _relative_child(frame.relative_path, name)
                try:
                    entry_stat = os.stat(
                        name,
                        dir_fd=frame.descriptor,
                        follow_symlinks=False,
                    )
                except OSError:
                    warnings.add(
                        _warning("entry_unavailable", relative_path, "stat_failed")
                    )
                    continue
                if stat.S_ISLNK(entry_stat.st_mode):
                    warnings.add(
                        _warning("symlink_excluded", relative_path, "symlink")
                    )
                    continue
                if _is_secret_like(relative_path):
                    warnings.add(
                        _warning("secret_excluded", relative_path, "secret_like")
                    )
                    continue
                if stat.S_ISDIR(entry_stat.st_mode):
                    if name.casefold() in _EXCLUDED_DIRECTORIES:
                        continue
                    if ignore_spec.match_file(f"{relative_path}/"):
                        continue
                    if not config.include_tests and (
                        name.casefold() in _TEST_DIRECTORIES
                    ):
                        continue
                    child_depth = frame.depth + 1
                    if child_depth > _MAX_DIRECTORY_DEPTH:
                        truncated = True
                        warnings.add(
                            _warning(
                                "directory_depth_limit",
                                relative_path,
                                "max_directory_depth",
                            )
                        )
                        continue
                    if scanned_directories >= _MAX_SCANNED_DIRECTORIES:
                        truncated = True
                        warnings.add(
                            _warning(
                                "directory_limit_reached",
                                relative_path,
                                "max_scanned_directories",
                            )
                        )
                        continue
                    try:
                        child_descriptor = _open_directory(
                            frame.descriptor, name, entry_stat
                        )
                    except _CandidateRejected as exc:
                        warnings.add(_warning(exc.code, relative_path, exc.detail))
                        continue
                    scanned_directories += 1
                    frames.append(
                        _DirectoryFrame(
                            child_descriptor,
                            relative_path,
                            child_depth,
                        )
                    )
                    continue

                if not stat.S_ISREG(entry_stat.st_mode):
                    warnings.add(
                        _warning("entry_excluded", relative_path, "not_regular")
                    )
                    continue
                if PurePosixPath(name).suffix.casefold() not in allowed_extensions:
                    continue
                if ignore_spec.match_file(relative_path):
                    warnings.add(_warning("ignored", relative_path, "ignore_rule"))
                    continue
                if len(files) >= config.max_total_files:
                    truncated = True
                    warnings.add(
                        _warning("file_limit_reached", relative_path, "max_total_files")
                    )
                    stop = True
                    continue
                if entry_stat.st_size > config.max_file_bytes:
                    warnings.add(
                        _warning("file_too_large", relative_path, "max_file_bytes")
                    )
                    continue
                try:
                    content = _read_contained_file(
                        frame.descriptor, name, entry_stat
                    )
                except _CandidateRejected as exc:
                    warnings.add(_warning(exc.code, relative_path, exc.detail))
                    continue
                files.append(
                    SourceFile(
                        path=relative_path,
                        content=content,
                        content_hash=hashlib.sha256(content).hexdigest(),
                        size_bytes=len(content),
                    )
                )
        finally:
            for frame in reversed(frames):
                if frame.descriptor != root_descriptor:
                    os.close(frame.descriptor)
        return DiscoverySnapshot(
            files=tuple(sorted(files, key=lambda item: item.path)),
            warnings=tuple(
                sorted(warnings, key=lambda item: (item.path, item.code, item.detail))
            ),
            truncated=truncated,
        )
    finally:
        os.close(root_descriptor)
