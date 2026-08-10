"""Fixed, base-local locations for code graph files."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path, PureWindowsPath
import stat
from typing import Iterator

from iwiki_mcp import base as wiki_base

from .models import CodeGraphError


class CodeGraphLocationError(CodeGraphError):
    """Raised when a code graph location would be unsafe."""


def _directory_flags() -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    required_dir_fd = (os.open, os.stat, os.mkdir)
    if (
        no_follow is None
        or directory is None
        or any(item not in os.supports_dir_fd for item in required_dir_fd)
        or not _replace_supports_dir_fd()
    ):
        raise CodeGraphLocationError("safe cache directory access unavailable")
    return os.O_RDONLY | no_follow | directory


def _replace_supports_dir_fd() -> bool:
    """Account for CPython exposing replace through rename's capability."""
    return (
        os.replace in os.supports_dir_fd
        or (
            os.name == "posix"
            and os.rename in os.supports_dir_fd
        )
    )


def _descriptor_path(descriptor: int) -> Path:
    for directory in ("/proc/self/fd", "/dev/fd"):
        candidate = Path(directory) / str(descriptor)
        if candidate.is_dir():
            return candidate
    raise CodeGraphLocationError("safe cache descriptor unavailable")


def _open_directory_chain(path: Path, flags: int) -> int:
    """Open every absolute directory component without following symlinks."""
    if not path.is_absolute() or not path.anchor:
        raise CodeGraphLocationError("unsafe code graph cache base")
    descriptor = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            next_descriptor = os.open(
                component,
                flags,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def open_cache_directory(
    base: str | Path,
    *,
    create: bool,
) -> Iterator[Path | None]:
    """Open base/.iwiki without following its final path components."""
    base_path = Path(os.path.abspath(base))
    base_descriptor = None
    cache_descriptor = None
    try:
        flags = _directory_flags()
        base_descriptor = _open_directory_chain(base_path, flags)
        try:
            cache_status = os.stat(
                ".iwiki",
                dir_fd=base_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            if not create:
                yield None
                return
            os.mkdir(".iwiki", mode=0o700, dir_fd=base_descriptor)
            cache_status = os.stat(
                ".iwiki",
                dir_fd=base_descriptor,
                follow_symlinks=False,
            )
        if not stat.S_ISDIR(cache_status.st_mode):
            raise CodeGraphLocationError("unsafe code graph cache directory")
        cache_descriptor = os.open(
            ".iwiki", flags, dir_fd=base_descriptor
        )
        opened_status = os.fstat(cache_descriptor)
        if (
            opened_status.st_dev,
            opened_status.st_ino,
        ) != (cache_status.st_dev, cache_status.st_ino):
            raise CodeGraphLocationError("code graph cache directory changed")
        descriptor_path = _descriptor_path(cache_descriptor)
        yield descriptor_path
    except CodeGraphLocationError:
        raise
    except (NotImplementedError, OSError, TypeError) as exc:
        raise CodeGraphLocationError("unsafe code graph cache path") from exc
    finally:
        if cache_descriptor is not None:
            os.close(cache_descriptor)
        if base_descriptor is not None:
            os.close(base_descriptor)


def validate_cache_directory(base: str | Path) -> None:
    with open_cache_directory(base, create=False):
        pass


def _validate_domain(domain: str) -> str:
    if not domain:
        raise CodeGraphLocationError("invalid domain: empty")
    if domain.startswith(".") or "/" in domain or "\\" in domain:
        raise CodeGraphLocationError(f"invalid domain '{domain}'")
    if domain in (".", ".."):
        raise CodeGraphLocationError(f"invalid domain '{domain}'")
    if Path(domain).is_absolute() or PureWindowsPath(domain).is_absolute():
        raise CodeGraphLocationError(f"invalid domain '{domain}'")
    if PureWindowsPath(domain).drive:
        raise CodeGraphLocationError(f"invalid domain '{domain}'")
    return domain


@dataclass(frozen=True)
class CodeGraphPaths:
    database: Path
    wal: Path
    shm: Path
    lock: Path
    metadata: Path


class CodeGraphLocationResolver:
    def __init__(self, base: str, domain: str, project_dir: str) -> None:
        self.base = base
        self.domain = domain
        self.project_dir = project_dir

    def resolve(self, *, ensure_excluded: bool = True) -> CodeGraphPaths:
        domain = _validate_domain(self.domain)
        base_path = Path(os.path.abspath(self.base))
        validate_cache_directory(base_path)
        graph_dir = base_path / ".iwiki"
        database = graph_dir / f"code-{domain}.sqlite3"
        if ensure_excluded:
            wiki_base.ensure_graph_store_excluded(self.base)
        return CodeGraphPaths(
            database=database,
            wal=Path(f"{database}-wal"),
            shm=Path(f"{database}-shm"),
            lock=graph_dir / f"code-{domain}.lock",
            metadata=graph_dir / f"code-{domain}.metadata.json",
        )


__all__ = [
    "CodeGraphLocationError",
    "CodeGraphLocationResolver",
    "CodeGraphPaths",
    "open_cache_directory",
    "validate_cache_directory",
]
