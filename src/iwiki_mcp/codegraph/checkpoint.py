"""Private, crash-consistent local storage for one recoverable MCP upload."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import time

from .canonical import canonical_bytes_sha256, canonical_json_bytes, canonical_sha256
from .location import CodeGraphLocationError, open_cache_directory
from .publication import (
    _ROW_KINDS,
    _projected_rows,
    PublicationSession,
    SnapshotBatch,
    SnapshotHeader,
    header_payload,
)
from .store import _is_canonical_revision


# Keep five minutes of clock skew below the server's 24-hour cap.
_CLIENT_WINDOW_SECONDS = (23 * 60 + 55) * 60


class CodeGraphResumeError(Exception):
    def __init__(self, code: str, *, publish_mode: str | None = "mcp") -> None:
        super().__init__(code)
        self.code = code
        self.exit_code = 2 if code in ("invalid_config", "resume_incompatible") else 1
        self.publish_mode = publish_mode


def _invalid():
    return CodeGraphResumeError("resume_invalid_checkpoint")


class Checkpoint:
    """Access fixed filenames through a locked, owner-only directory descriptor."""

    def __init__(self, directory: int, target: str) -> None:
        self.directory = directory
        self.target = target
        self.manifest = "manifest-" + target.removeprefix("sha256:")
        self.record = None

    @classmethod
    @contextmanager
    def open(cls, base, checkout, primary, endpoint, *, forget=False):
        import fcntl

        owner = [str(Path(checkout).resolve()), primary]
        name = "resume-" + canonical_sha256(owner, prefix=False)
        target = canonical_sha256([owner, endpoint], prefix=True)
        try:
            with open_cache_directory(base, create=True) as cache:
                folder = cache / name
                try:
                    folder.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    info = os.fstat(fd)
                    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                        raise _invalid()
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        raise CodeGraphResumeError("resume_busy") from None
                    # Include both newly created directory entries in the durability boundary.
                    for parent in (cache, cache / ".."):
                        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                        try:
                            os.fsync(parent_fd)
                        finally:
                            os.close(parent_fd)
                    pin = cls(fd, target)
                    pin._load(forget=forget)
                    yield pin
                finally:
                    os.close(fd)
        except CodeGraphLocationError:
            raise _invalid() from None

    def _file(self, name, *, create=False):
        flags = os.O_NOFOLLOW | os.O_NONBLOCK
        flags |= (os.O_CREAT | os.O_EXCL | os.O_WRONLY) if create else os.O_RDONLY
        fd = os.open(name, flags, 0o600, dir_fd=self.directory)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            os.close(fd)
            raise _invalid()
        return os.fdopen(fd, "wb" if create else "rb")

    def _unlink(self, name):
        try:
            os.unlink(name, dir_fd=self.directory)
        except FileNotFoundError:
            pass

    def _load(self, *, forget):
        for name in os.listdir(self.directory):
            info = os.stat(name, dir_fd=self.directory, follow_symlinks=False)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
            ):
                raise _invalid()
            if name.startswith("manifest-") and name != self.manifest:
                raise CodeGraphResumeError("resume_target_mismatch")
        try:
            with self._file(self.manifest) as stream:
                envelope = json.load(stream)
            value = envelope["record"]
            if (
                envelope["sha256"] != canonical_sha256(value, prefix=True)
                or type(value["version"]) is not int
                or type(value["codec"]) is not int
                or value["version"] != 1
                or value["codec"] != 1
                or value["state"] not in ("ready", "pinned")
            ):
                raise _invalid()
            if value["target"] != self.target:
                raise CodeGraphResumeError("resume_target_mismatch")
            self.record = value
            if value["state"] == "ready":
                if not _is_canonical_revision(value["revision"]):
                    raise _invalid()
                self._unlink("rows")
        except FileNotFoundError:
            # No remote mutation can precede the first durable manifest.
            self._unlink("rows")
        except (KeyError, TypeError, ValueError, OSError, CodeGraphResumeError) as exc:
            if not forget or (
                isinstance(exc, CodeGraphResumeError) and exc.code == "resume_target_mismatch"
            ):
                raise _invalid() from None
            # The target-specific filename identifies the selected record even
            # when its contents are unreadable. Forget only unlinks fixed names.
            self.record = {"state": "unknown"}
        self._unlink("rows.tmp")
        self._unlink("manifest.tmp")

    @contextmanager
    def _atomic(self, name):
        temporary = "rows.tmp" if name == "rows" else "manifest.tmp"
        self._unlink(temporary)
        with self._file(temporary, create=True) as stream:
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=self.directory, dst_dir_fd=self.directory)
        os.fsync(self.directory)

    def _save(self, record):
        record.update(version=1, codec=1, target=self.target)
        envelope = {"record": record, "sha256": canonical_sha256(record, prefix=True)}
        with self._atomic(self.manifest) as stream:
            stream.write(canonical_json_bytes(envelope))
        self.record = record

    @staticmethod
    def _credential_proof(credential: str, salt: str) -> str:
        message = b"iwiki-mcp.resume.authorization.v1:" + salt.encode("ascii")
        return hmac.digest(credential.encode(), message, "sha256").hex()

    def verify_credential(self, credential):
        salt, proof = self.record["credential"]
        if not hmac.compare_digest(self._credential_proof(credential, salt), proof):
            raise CodeGraphResumeError("resume_access_denied")

    def prepare(self, header: SnapshotHeader, rows, *, credential: str):
        digest = hashlib.sha256()
        with self._atomic("rows") as stream:
            for kind in _ROW_KINDS:
                if len(rows[kind]) != header.expected_counts[kind]:
                    raise _invalid()
                for row in _projected_rows(kind, rows[kind]):
                    line = canonical_json_bytes(row) + b"\n"
                    stream.write(line)
                    digest.update(line)
        salt = secrets.token_hex(16)
        self._save(
            {
                "credential": [salt, self._credential_proof(credential, salt)],
                "state": "pinned",
                "header": header_payload(header),
                "rows_hash": digest.hexdigest(),
                "key": secrets.token_hex(32),
                "deadline": int(time.time()) + _CLIENT_WINDOW_SECONDS,
            }
        )

    def verify(self):
        if self.record["state"] == "ready":
            return
        try:
            digest = hashlib.sha256()
            with self._file("rows") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != self.record["rows_hash"]:
                raise _invalid()
        except OSError:
            raise _invalid() from None

    def _layout(self, session):
        row_limit, byte_limit = session.max_batch_rows, session.max_batch_bytes
        if (
            type(row_limit) is not int
            or not 1 <= row_limit <= 5000
            or type(byte_limit) is not int
            or not 1 <= byte_limit <= 5_000_000
        ):
            raise CodeGraphResumeError("resume_incompatible")
        header = SnapshotHeader(**self.record["header"])
        with self._file("rows") as stream:
            for kind in _ROW_KINDS:
                ordinal = 0
                remaining = header.expected_counts[kind]
                while remaining:
                    start = stream.tell()
                    size = 2
                    rows = []
                    while remaining and len(rows) < row_limit:
                        position = stream.tell()
                        line = stream.readline(byte_limit + 1)
                        if not line or not line.endswith(b"\n") or len(line) + 1 > byte_limit:
                            raise CodeGraphResumeError("resume_incompatible")
                        length = len(line) - 1 + bool(rows)
                        if size + length > byte_limit:
                            stream.seek(position)
                            break
                        rows.append(line[:-1])
                        size += length
                        remaining -= 1
                    digest = canonical_bytes_sha256(b"[" + b",".join(rows) + b"]", prefix=True)
                    yield {
                        "kind": kind,
                        "ordinal": ordinal,
                        "offset": start,
                        "length": stream.tell() - start,
                        "rows": len(rows),
                        "hash": digest,
                    }
                    ordinal += 1
            if stream.read(1):
                raise _invalid()

    def save_session(self, session: PublicationSession):
        self._save(
            {**self.record, "session": asdict(session), "batches": list(self._layout(session))}
        )

    def batch(self, item):
        try:
            with self._file("rows") as stream:
                stream.seek(item["offset"])
                data = stream.read(item["length"])
            lines = data.splitlines()
            payload = b"[" + b",".join(lines) + b"]"
            if (
                len(lines) != item["rows"]
                or len(data) != item["length"]
                or canonical_bytes_sha256(payload, prefix=True) != item["hash"]
            ):
                raise _invalid()
            return SnapshotBatch(
                item["kind"], item["ordinal"], len(lines), len(payload), item["hash"], payload
            )
        except (KeyError, ValueError, TypeError, OSError):
            raise _invalid() from None

    def complete(self, revision):
        if not _is_canonical_revision(revision):
            raise CodeGraphResumeError("resume_failed")
        self._save(
            {
                "state": "ready",
                "revision": revision,
                "credential": self.record["credential"],
                "counts": self.record["header"]["expected_counts"],
            }
        )
        self._unlink("rows")
        os.fsync(self.directory)

    def forget(self):
        unknown = self.record is not None and self.record["state"] != "ready"
        self._unlink(self.manifest)
        os.fsync(self.directory)
        self._unlink("rows")
        os.fsync(self.directory)
        self.record = None
        return unknown
