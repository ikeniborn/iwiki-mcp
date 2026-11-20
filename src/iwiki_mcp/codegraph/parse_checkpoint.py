"""Private parse records accessed only while the caller holds the graph writer lock."""

from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import re
import stat

from .canonical import canonical_json_bytes, canonical_sha256
from .location import CodeGraphLocationError, open_cache_directory
from .models import CodeGraphError


MAX_RECORD_BYTES = 16 * 1024 * 1024
_RECORD = re.compile(r"[a-f0-9]{64}")
_TEMP = "record.tmp"


class ParseCheckpointError(CodeGraphError):
    code = "parse_checkpoint_storage_failed"

    def __init__(self):
        super().__init__(self.code)


@contextmanager
def _storage():
    try:
        yield
    except (CodeGraphLocationError, OSError):
        raise ParseCheckpointError() from None


def _name(path):
    return canonical_sha256(path, prefix=False)


def _validate_file(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise ParseCheckpointError()


def _private_ancestor(directory):
    """A private ancestor prevents other users entering a legacy shared cache."""
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        while True:
            info = os.fstat(fd)
            if info.st_uid == os.geteuid() and not info.st_mode & 0o011:
                return True
            parent = os.open("..", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = parent
            if os.path.samestat(info, os.fstat(fd)):
                return False
    finally:
        os.close(fd)


class ParseCheckpoint:
    def __init__(self, directory):
        self.directory = directory

    @classmethod
    @contextmanager
    def open(cls, base, checkout, domain, *, create=True):
        scope = "parse-" + canonical_sha256([str(Path(checkout).resolve()), domain], prefix=False)
        with ExitStack() as stack:
            with _storage():
                cache = stack.enter_context(open_cache_directory(base, create=create))
                fd = None
                if cache is not None:
                    parent = cache.stat()
                    if (parent.st_uid != os.geteuid() or
                            (parent.st_mode & 0o022 and not parent.st_mode & stat.S_ISVTX
                             and not _private_ancestor(cache))):
                        raise ParseCheckpointError()
                    folder = cache / scope
                    if create:
                        try:
                            folder.mkdir(mode=0o700)
                        except FileExistsError:
                            pass
                        # A prior process may have died before syncing either entry.
                        for path in (cache, cache / ".."):
                            parent_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
                            try:
                                os.fsync(parent_fd)
                            finally:
                                os.close(parent_fd)
                    try:
                        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    except FileNotFoundError:
                        if create:
                            raise
                    if fd is not None:
                        stack.callback(os.close, fd)
                        info = os.fstat(fd)
                        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                            raise ParseCheckpointError()
            # Do not translate exceptions from the build into storage failures.
            try:
                yield cls(fd) if fd is not None else None
            finally:
                stack.close()

    def _checked(self, name):
        try:
            info = os.stat(name, dir_fd=self.directory, follow_symlinks=False)
        except FileNotFoundError:
            return None
        _validate_file(info)
        return info

    def load(self, path, identity):
        name = _name(path)
        with _storage():
            if self._checked(name) is None:
                return None
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            fd = os.open(name, flags, dir_fd=self.directory)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(fd)
                _validate_file(info)
                if info.st_size > MAX_RECORD_BYTES:
                    return None
                data = stream.read(info.st_size)
                if len(data) != info.st_size or stream.read(1):
                    return None
        try:
            envelope = json.loads(data)
            if type(envelope) is not dict or envelope.keys() != {"record", "digest"}:
                return None
            record = envelope["record"]
            if (type(record) is not dict or record.keys() != {"version", "identity", "payload"}
                    or type(record["version"]) is not int or record["version"] != 1
                    or canonical_json_bytes(record["identity"]) != canonical_json_bytes(identity)
                    or envelope["digest"] != canonical_sha256(record, prefix=False)):
                return None
            return record["payload"]
        except (ValueError, RecursionError):
            return None

    def save(self, path, identity, payload):
        record = {"version": 1, "identity": identity, "payload": payload}
        digest = canonical_sha256(record, prefix=False)
        data = canonical_json_bytes({"record": record, "digest": digest})
        if len(data) > MAX_RECORD_BYTES:
            return False
        name = _name(path)
        with _storage():
            self._checked(name)
            if self._checked(_TEMP) is not None:
                os.unlink(_TEMP, dir_fd=self.directory)
            fd = os.open(_TEMP, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=self.directory)
            with os.fdopen(fd, "wb") as stream:
                _validate_file(os.fstat(fd))
                stream.write(data)
                stream.flush()
                os.fsync(fd)
            os.replace(_TEMP, name, src_dir_fd=self.directory, dst_dir_fd=self.directory)
            os.fsync(self.directory)
        return True

    def prune(self, paths):
        keep = {_name(path) for path in paths}
        with _storage():
            remove = []
            for name in os.listdir(self.directory):
                if name == _TEMP or _RECORD.fullmatch(name):
                    self._checked(name)
                    if name not in keep:
                        remove.append(name)
            for name in remove:
                os.unlink(name, dir_fd=self.directory)
            if remove:
                os.fsync(self.directory)

    def clear(self):
        self.prune(())
