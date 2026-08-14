"""Safe one-time initialization for project-owned text files."""
from __future__ import annotations

import hashlib
import os
import stat
import tempfile

from filelock import FileLock, Timeout


def _lock_path(path: str) -> str:
    normalized = os.path.normcase(os.path.abspath(path))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return os.path.join(tempfile.gettempdir(), f"iwiki-mcp-init-{digest}.lock")


def _read_state(path: str) -> tuple[tuple[int, int], bytes] | None:
    """Return stable identity and content, None when missing, reject symlinks."""
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(path_stat.st_mode):
        raise OSError("project file must not be a symlink")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        file_stat = os.fstat(descriptor)
        identity = (file_stat.st_dev, file_stat.st_ino)
        if identity != (path_stat.st_dev, path_stat.st_ino):
            raise OSError("project file changed while being inspected")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
        return identity, b"".join(chunks)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, content: bytes) -> None:
    written = 0
    while written < len(content):
        count = os.write(descriptor, content[written:])
        if count == 0:
            raise OSError("project file write made no progress")
        written += count


def _fill_existing(
    path: str, initial: tuple[tuple[int, int], bytes], content: bytes
) -> bool:
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        file_stat = os.fstat(descriptor)
        if (file_stat.st_dev, file_stat.st_ino) != initial[0]:
            return False
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
        if b"".join(chunks).strip():
            return False
        path_stat = os.lstat(path)
        if stat.S_ISLNK(path_stat.st_mode) or (
            path_stat.st_dev,
            path_stat.st_ino,
        ) != initial[0]:
            return False

        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            _write_all(descriptor, content)
            os.ftruncate(descriptor, len(content))
            os.fsync(descriptor)
            final_stat = os.lstat(path)
            return (final_stat.st_dev, final_stat.st_ino) == initial[0]
        except OSError:
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                _write_all(descriptor, initial[1])
                os.ftruncate(descriptor, len(initial[1]))
                os.fsync(descriptor)
            except OSError:
                pass
            return False
    finally:
        os.close(descriptor)


def initialize_text_file(path: str, content: str) -> bool:
    """Write content once without replacing a populated project file."""
    directory = os.path.dirname(path)
    temporary_path: str | None = None
    try:
        os.makedirs(directory, exist_ok=True)
        with FileLock(_lock_path(path), timeout=1):
            initial = _read_state(path)
            if initial is not None and initial[1].strip():
                return False

            encoded = content.encode("utf-8")
            if initial is not None:
                return _fill_existing(path, initial, encoded)

            descriptor, temporary_path = tempfile.mkstemp(
                prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory
            )
            with os.fdopen(descriptor, "wb") as temporary:
                temporary.write(encoded)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.link(temporary_path, path)
            os.unlink(temporary_path)
            temporary_path = None
            return True
    except (OSError, Timeout):
        return False
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
