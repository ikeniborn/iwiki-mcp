"""Resolve human-authored Wiki selectors into derived code-graph links."""
from __future__ import annotations

import ctypes
from dataclasses import asdict, dataclass, field
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import sys
from typing import Callable, Iterator, Mapping, Sequence

from filelock import Timeout

from iwiki_mcp.engine import frontmatter
from iwiki_mcp.engine.okf_artifacts import RESERVED_OKF

from .models import ParsedFile


_SELECTOR_KEYS = frozenset({"symbols", "files", "source_globs"})
_SPECIFICITY = {"source_glob": 0, "file": 1, "symbol": 2}
_MAX_SELECTORS = 256
_MAX_SELECTOR_BYTES = 4096
_MAX_SELECTOR_SEGMENTS = 256
_MAX_WIKI_PAGES = 10_000
_MAX_WIKI_ENTRIES = 10_000
_MAX_SELECTOR_YAML_OVERHEAD = len("    - qualified_name: ".encode("utf-8"))
_MAX_FRONTMATTER_BYTES = (
    _MAX_SELECTORS * (
        6 * _MAX_SELECTOR_BYTES + _MAX_SELECTOR_YAML_OVERHEAD
    )
    + 4096
)
_MAX_WIKI_PAGE_BYTES = _MAX_FRONTMATTER_BYTES + 1_000_000
_MAX_CAPTURE_BYTES = 64_000_000
_HAS_SAFE_DESCRIPTOR_TRAVERSAL = (
    os.open in os.supports_dir_fd
    and os.scandir in os.supports_fd
    and bool(getattr(os, "O_NOFOLLOW", 0))
)


class SelectorError(ValueError):
    """Raised when authored ``code`` frontmatter is outside MVP grammar."""


class SelectorSnapshotChanged(SelectorError):
    """Raised when Wiki page bytes drift during one graph rebuild."""


def selector_capture_budget(max_file_bytes: int, max_total_files: int) -> int:
    """Derive aggregate work while allowing one maximum legal selector page."""
    configured = max_file_bytes * max_total_files
    return min(
        _MAX_CAPTURE_BYTES,
        max(_MAX_WIKI_PAGE_BYTES, configured),
    )


class _SelectorWatch:
    """One bounded Linux event queue covering the pinned Wiki directory tree."""

    _EVENT_MASK = (
        0x00000002  # IN_MODIFY
        | 0x00000004  # IN_ATTRIB
        | 0x00000008  # IN_CLOSE_WRITE
        | 0x00000040  # IN_MOVED_FROM
        | 0x00000080  # IN_MOVED_TO
        | 0x00000100  # IN_CREATE
        | 0x00000200  # IN_DELETE
        | 0x00000400  # IN_DELETE_SELF
        | 0x00000800  # IN_MOVE_SELF
        | 0x00004000  # IN_Q_OVERFLOW
    )

    def __init__(self, descriptor: int, add_watch) -> None:
        self._descriptor = descriptor
        self._add_watch = add_watch

    @classmethod
    def create(cls) -> "_SelectorWatch":
        if sys.platform != "linux" or not Path("/proc/self/fd").is_dir():
            raise SelectorError("safe Wiki selector watch is unavailable")
        try:
            library = ctypes.CDLL(None, use_errno=True)
            initialize = library.inotify_init1
            add_watch = library.inotify_add_watch
        except (AttributeError, OSError) as exc:
            raise SelectorError("safe Wiki selector watch is unavailable") from exc
        initialize.argtypes = [ctypes.c_int]
        initialize.restype = ctypes.c_int
        add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        add_watch.restype = ctypes.c_int
        descriptor = initialize(os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
        if descriptor < 0:
            raise SelectorError("safe Wiki selector watch is unavailable")
        return cls(descriptor, add_watch)

    def add_directory(self, descriptor: int) -> None:
        if self._descriptor < 0:
            raise SelectorSnapshotChanged("Wiki selector watch is invalid")
        target = f"/proc/self/fd/{descriptor}".encode("ascii")
        if self._add_watch(self._descriptor, target, self._EVENT_MASK) < 0:
            raise SelectorError("Wiki selector watch budget exceeded")

    def changed(self) -> bool:
        if self._descriptor < 0:
            raise SelectorSnapshotChanged("Wiki selector watch is invalid")
        try:
            return bool(os.read(self._descriptor, 65_536))
        except BlockingIOError:
            return False
        except OSError as exc:
            raise SelectorSnapshotChanged("Wiki selector watch failed") from exc

    def close(self) -> None:
        if self._descriptor >= 0:
            descriptor, self._descriptor = self._descriptor, -1
            os.close(descriptor)

    def __del__(self) -> None:
        try:
            self.close()
        except OSError:
            pass


def _selector_text(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise SelectorError(f"{label} selector must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SelectorError(f"invalid {label} selector") from exc
    if len(encoded) > _MAX_SELECTOR_BYTES or "\0" in value:
        raise SelectorError(f"invalid {label} selector")
    return value


def _relative_selector(value: object, label: str, *, glob: bool = False) -> str:
    text = _selector_text(value, label)
    posix = PurePosixPath(text)
    windows = PureWindowsPath(text)
    if (
        text != text.strip()
        or "\\" in text
        or posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or not posix.parts
        or len(posix.parts) > _MAX_SELECTOR_SEGMENTS
        or posix.as_posix() != text
        or any(part in {"", ".", ".."} for part in posix.parts)
    ):
        raise SelectorError(f"unsafe {label} selector")
    if not glob and any(character in text for character in "*?["):
        raise SelectorError(f"invalid {label} selector")
    return posix.as_posix()


def validate_code_mapping(value: object) -> dict[str, list[object]]:
    """Validate exactly the approved selector grammar without mutating it."""
    if not isinstance(value, Mapping):
        raise SelectorError("code frontmatter must be a mapping")
    if set(value) - _SELECTOR_KEYS:
        raise SelectorError("unsupported code selector key")
    total = 0
    validated: dict[str, list[object]] = {}
    for key in ("symbols", "files", "source_globs"):
        items = value.get(key, [])
        if type(items) is not list:
            raise SelectorError(f"code.{key} must be a list")
        total += len(items)
        if total > _MAX_SELECTORS:
            raise SelectorError("too many code selectors")
        if key == "symbols":
            normalized: list[object] = []
            for item in items:
                if not isinstance(item, Mapping) or set(item) != {"qualified_name"}:
                    raise SelectorError(
                        "symbol selectors require only qualified_name"
                    )
                _selector_text(item["qualified_name"], "symbol")
                normalized.append(item)
            validated[key] = normalized
        elif key == "files":
            for item in items:
                _relative_selector(item, "file")
            validated[key] = list(items)
        else:
            for item in items:
                _relative_selector(item, "source_glob", glob=True)
            validated[key] = list(items)
    return validated


def _snapshot_rows(
    snapshot: Mapping[str, Sequence[Mapping[str, object]]], key: str
) -> tuple[Mapping[str, object], ...]:
    rows = snapshot.get(key, ())
    if not isinstance(rows, Sequence):
        raise SelectorError("invalid code graph snapshot")
    return tuple(rows)


def _link_id(
    domain: str,
    page_id: str,
    selector_kind: str,
    target_id: str,
    source: str,
) -> str:
    digest = hashlib.sha256("\0".join((
        domain, page_id, selector_kind, target_id, source,
    )).encode("utf-8")).hexdigest()
    return f"wiki:link:{digest}"


def _glob_matches(path: str, pattern: str) -> bool:
    """Match POSIX path segments; only a complete ``**`` crosses slashes."""
    path_parts = tuple(PurePosixPath(path).parts)
    pattern_parts = tuple(PurePosixPath(pattern).parts)
    memo: dict[tuple[int, int], bool] = {}

    def matches(path_index: int, pattern_index: int) -> bool:
        key = (path_index, pattern_index)
        if key in memo:
            return memo[key]
        if pattern_index == len(pattern_parts):
            result = path_index == len(path_parts)
        elif pattern_parts[pattern_index] == "**":
            result = matches(path_index, pattern_index + 1) or (
                path_index < len(path_parts)
                and matches(path_index + 1, pattern_index)
            )
        else:
            result = (
                path_index < len(path_parts)
                and fnmatch.fnmatchcase(
                    path_parts[path_index], pattern_parts[pattern_index]
                )
                and matches(path_index + 1, pattern_index + 1)
            )
        memo[key] = result
        return result

    return matches(0, 0)


def _code_mapping(markdown: str | Mapping[str, object]) -> object | None:
    if isinstance(markdown, str):
        try:
            meta, _body = frontmatter.split(markdown, strict_code=True)
        except frontmatter.FrontmatterError as exc:
            raise SelectorError(str(exc)) from exc
    elif isinstance(markdown, Mapping):
        meta = markdown
    else:
        raise SelectorError("Wiki page must be Markdown or frontmatter mapping")
    return meta.get("code")


def resolve_selectors(
    markdown: str | Mapping[str, object],
    snapshot: Mapping[str, Sequence[Mapping[str, object]]],
    *,
    domain: str = "",
    page_id: str = "",
    check_control: Callable[[], None] | None = None,
) -> tuple[dict[str, object], ...]:
    """Resolve one page against one coherent schema-v2 snapshot."""
    code = _code_mapping(markdown)
    if code is None:
        return ()
    selectors = validate_code_mapping(code)
    work = 0

    def checkpoint() -> None:
        nonlocal work
        work += 1
        if check_control is not None and (work == 1 or work % 32 == 0):
            check_control()

    checkpoint()
    files = _snapshot_rows(snapshot, "files")
    symbols = _snapshot_rows(snapshot, "symbols")
    files_by_path: dict[str, Mapping[str, object]] = {}
    for row in files:
        checkpoint()
        if isinstance(row, Mapping) and "path" in row and "file_id" in row:
            files_by_path[str(row["path"])] = row
    symbols_by_name: dict[str, list[Mapping[str, object]]] = {}
    for row in symbols:
        checkpoint()
        if not isinstance(row, Mapping) or not {"qualified_name", "symbol_id"} <= set(row):
            continue
        symbols_by_name.setdefault(str(row["qualified_name"]), []).append(row)

    selected: dict[tuple[str, str], dict[str, object]] = {}

    def admit(
        selector_kind: str,
        source: str,
        *,
        symbol_id: str | None = None,
        file_id: str | None = None,
    ) -> None:
        target_type, target_id = (
            ("symbol", symbol_id) if symbol_id is not None else ("file", file_id)
        )
        assert target_id is not None
        key = (target_type, target_id)
        candidate = {
            "link_id": _link_id(
                domain, page_id, selector_kind, target_id, source
            ),
            "domain": domain,
            "page_id": page_id,
            "symbol_id": symbol_id,
            "file_id": file_id,
            "selector_kind": selector_kind,
            "relation_type": "DOCUMENTED_BY",
            "confidence": 1.0,
            "source": source,
        }
        current = selected.get(key)
        if current is None or (
            _SPECIFICITY[selector_kind], source
        ) > (
            _SPECIFICITY[str(current["selector_kind"])],
            str(current["source"]),
        ):
            selected[key] = candidate

    symbol_names = sorted({
        str(item["qualified_name"])
        for item in selectors["symbols"]
        if isinstance(item, Mapping)
    })
    file_paths = sorted({
        _relative_selector(item, "file") for item in selectors["files"]
    })
    source_globs = sorted({
        _relative_selector(item, "source_glob", glob=True)
        for item in selectors["source_globs"]
    })

    for qualified_name in symbol_names:
        checkpoint()
        for row in sorted(
            symbols_by_name.get(qualified_name, ()),
            key=lambda candidate: str(candidate["symbol_id"]),
        ):
            checkpoint()
            admit(
                "symbol", qualified_name, symbol_id=str(row["symbol_id"])
            )
    for path in file_paths:
        checkpoint()
        row = files_by_path.get(path)
        if row is not None:
            admit("file", path, file_id=str(row["file_id"]))
    for pattern in source_globs:
        checkpoint()
        for path, row in sorted(files_by_path.items()):
            checkpoint()
            if _glob_matches(path, pattern):
                admit("source_glob", pattern, file_id=str(row["file_id"]))

    if check_control is not None:
        check_control()

    return tuple(sorted(selected.values(), key=lambda row: str(row["link_id"])))


def _parsed_snapshot(
    parsed_files: tuple[ParsedFile, ...],
) -> dict[str, tuple[dict[str, object], ...]]:
    return {
        "files": tuple(asdict(item.file) for item in parsed_files),
        "symbols": tuple(
            asdict(symbol) for item in parsed_files for symbol in item.symbols
        ),
    }


def _rows_fingerprint(
    rows: Sequence[object],
    check_control: Callable[[], None] | None = None,
) -> str:
    """Hash deterministic JSON rows without one aggregate serialization."""
    digest = hashlib.sha256()
    digest.update(b"[")
    for index, row in enumerate(rows):
        if check_control is not None:
            check_control()
        if index:
            digest.update(b",")
        digest.update(json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"))
    digest.update(b"]")
    if check_control is not None:
        check_control()
    return digest.hexdigest()


@dataclass(frozen=True)
class WikiPageSnapshot:
    """Compact selector authority plus page-generation evidence."""

    relative: str
    page_id: str
    content_hash: str
    selectors: object | None


@dataclass(frozen=True)
class WikiSelectorSnapshot:
    """Exact Wiki page generation used by fingerprints and derived links."""

    domain: str
    pages: tuple[WikiPageSnapshot, ...]
    fingerprint: str
    generation_fingerprint: str
    max_bytes: int
    _watch: _SelectorWatch | None = field(
        default=None, repr=False, compare=False
    )


class WikiSelectorResolver:
    """Read authored pages and derive links during an explicit full rebuild."""

    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)

    def _page_descriptors(
        self,
        domain: str,
        check_control: Callable[[], None] | None = None,
        max_entries: int = _MAX_WIKI_ENTRIES,
        watch_directory: Callable[[int], None] | None = None,
        watch_file: Callable[[int], None] | None = None,
    ) -> Iterator[tuple[str, int, int | None, str, tuple[int, int]]]:
        if PurePosixPath(domain).parts != (domain,) or domain in {".", ".."}:
            raise SelectorError("unsafe Wiki domain")
        if not _HAS_SAFE_DESCRIPTOR_TRAVERSAL:
            raise SelectorError("safe Wiki selector capture is unavailable")
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        file_flags |= getattr(os, "O_NONBLOCK", 0)
        try:
            base_fd = os.open(self.base_dir, directory_flags)
            domain_fd = os.open(domain, directory_flags, dir_fd=base_fd)
        except OSError as exc:
            try:
                os.close(base_fd)
            except (NameError, OSError):
                pass
            if isinstance(exc, FileNotFoundError):
                raise SelectorError("Wiki domain unavailable") from exc
            raise SelectorError("unsafe Wiki page tree") from exc
        domain_status = os.fstat(domain_fd)
        accepted = 0
        traversed = 0
        watched = 0
        stack: list[tuple[str, int, int, str, tuple[int, int], list, int]] = []
        root_pending = True

        def entries(directory_fd: int) -> list:
            nonlocal traversed
            result = []
            with os.scandir(directory_fd) as iterator:
                for entry in iterator:
                    traversed += 1
                    if check_control is not None:
                        check_control()
                    if traversed > max_entries:
                        raise SelectorError(
                            "Wiki selector traversal budget exceeded"
                        )
                    result.append(entry)
            result.sort(key=lambda item: item.name)
            return result

        def register_watch(
            descriptor: int,
            callback: Callable[[int], None] | None,
        ) -> None:
            nonlocal watched
            if callback is None:
                return
            watched += 1
            if watched > max_entries + 1:
                raise SelectorError("Wiki selector watch budget exceeded")
            callback(descriptor)

        try:
            register_watch(domain_fd, watch_directory)
            stack.append((
                "", domain_fd, base_fd, domain,
                (domain_status.st_dev, domain_status.st_ino),
                entries(domain_fd), 0,
            ))
            root_pending = False
            while stack:
                (
                    prefix, directory_fd, parent_fd, name, opened_identity,
                    directory_entries, index,
                ) = stack[-1]
                if index >= len(directory_entries):
                    verified = os.stat(
                        name, dir_fd=parent_fd, follow_symlinks=False
                    )
                    if (
                        not stat.S_ISDIR(verified.st_mode)
                        or (verified.st_dev, verified.st_ino) != opened_identity
                    ):
                        raise SelectorSnapshotChanged(
                            "Wiki page tree changed during capture"
                        )
                    os.close(directory_fd)
                    os.close(parent_fd)
                    stack.pop()
                    continue
                entry = directory_entries[index]
                stack[-1] = (
                    prefix, directory_fd, parent_fd, name, opened_identity,
                    directory_entries, index + 1,
                )
                relative = f"{prefix}/{entry.name}" if prefix else entry.name
                if entry.is_dir(follow_symlinks=False):
                    child_fd = None
                    child_parent_fd = None
                    try:
                        child_fd = os.open(
                            entry.name, directory_flags, dir_fd=directory_fd
                        )
                        child_status = os.fstat(child_fd)
                        child_parent_fd = os.dup(directory_fd)
                        register_watch(child_fd, watch_directory)
                        child_entries = entries(child_fd)
                    except BaseException as exc:
                        try:
                            if child_fd is not None:
                                os.close(child_fd)
                            if child_parent_fd is not None:
                                os.close(child_parent_fd)
                        except OSError:
                            pass
                        if isinstance(exc, OSError):
                            raise SelectorError("unsafe Wiki page tree") from exc
                        raise
                    stack.append((
                        relative, child_fd, child_parent_fd, entry.name,
                        (child_status.st_dev, child_status.st_ino),
                        child_entries, 0,
                    ))
                    continue
                if (
                    not relative.endswith(".md")
                    or relative in RESERVED_OKF
                    or entry.is_symlink()
                ):
                    continue
                descriptor = None
                try:
                    descriptor = os.open(
                        entry.name, file_flags, dir_fd=directory_fd
                    )
                    status = os.fstat(descriptor)
                except OSError as exc:
                    try:
                        if descriptor is not None:
                            os.close(descriptor)
                    except OSError:
                        pass
                    raise SelectorError("unsafe Wiki page") from exc
                if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                    os.close(descriptor)
                    raise SelectorError("unsafe Wiki page")
                if status.st_size > _MAX_WIKI_PAGE_BYTES:
                    os.close(descriptor)
                    raise SelectorError("Wiki page byte budget exceeded")
                if watch_file is not None:
                    try:
                        register_watch(descriptor, watch_file)
                    except BaseException:
                        os.close(descriptor)
                        raise
                accepted += 1
                if accepted > _MAX_WIKI_PAGES:
                    os.close(descriptor)
                    raise SelectorError(
                        "too many Wiki pages for selector resolution"
                    )
                try:
                    page_parent_fd = os.dup(directory_fd)
                except OSError:
                    os.close(descriptor)
                    raise
                yield (
                    relative, descriptor, page_parent_fd, entry.name,
                    (status.st_dev, status.st_ino),
                )
        except Timeout:
            raise
        except OSError as exc:
            raise SelectorError("unsafe Wiki page tree") from exc
        finally:
            if root_pending:
                os.close(domain_fd)
                os.close(base_fd)
            for (
                _prefix, directory_fd, parent_fd, _name, _identity,
                _entries, _index,
            ) in stack:
                os.close(directory_fd)
                os.close(parent_fd)

    @staticmethod
    def _read_page(
        descriptor: int,
        *,
        remaining: int,
        check_control: Callable[[], None] | None,
    ) -> tuple[str, object | None, int]:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise SelectorSnapshotChanged("Wiki page identity changed during capture")
        if remaining <= 0:
            raise SelectorError("Wiki selector capture budget exceeded")
        digest = hashlib.sha256()
        frontmatter_bytes = bytearray()
        consumed = 0
        while True:
            if check_control is not None:
                check_control()
            chunk = os.read(descriptor, min(65_536, remaining - consumed + 1))
            if not chunk:
                break
            consumed += len(chunk)
            if consumed > remaining:
                raise SelectorError("Wiki selector capture budget exceeded")
            digest.update(chunk)
            if len(frontmatter_bytes) < _MAX_WIKI_PAGE_BYTES:
                room = _MAX_WIKI_PAGE_BYTES - len(frontmatter_bytes)
                frontmatter_bytes.extend(chunk[:room])
        after = os.fstat(descriptor)
        if not stat.S_ISREG(after.st_mode) or after.st_nlink != 1:
            raise SelectorSnapshotChanged("Wiki page identity changed during capture")

        def identity(value):
            return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns
        if identity(before) != identity(after):
            raise SelectorSnapshotChanged("Wiki page changed during capture")
        selectors: object | None = None
        prefix = bytes(frontmatter_bytes)
        if prefix.startswith(b"---\n"):
            end = prefix.find(b"\n---\n", 4)
            if end < 0:
                if before.st_size > len(prefix):
                    raise SelectorError("Wiki frontmatter capture budget exceeded")
            else:
                try:
                    block = prefix[:end + 5].decode("utf-8")
                    meta, _body = frontmatter.split(block, strict_code=True)
                    if "code" in meta:
                        selectors = validate_code_mapping(meta["code"])
                except (UnicodeError, frontmatter.FrontmatterError, SelectorError):
                    selectors = {"invalid": True}
        return digest.hexdigest(), selectors, consumed

    def capture(
        self,
        *,
        domain: str,
        check_control: Callable[[], None] | None = None,
        max_bytes: int = _MAX_CAPTURE_BYTES,
    ) -> WikiSelectorSnapshot:
        """Capture compact selectors plus bounded page-generation evidence."""
        pages: list[WikiPageSnapshot] = []
        consumed = 0
        watch = _SelectorWatch.create()
        descriptors = self._page_descriptors(
            domain,
            check_control,
            max_entries=_MAX_WIKI_ENTRIES,
            watch_directory=watch.add_directory,
            watch_file=watch.add_directory,
        )
        captured = False
        try:
            for (
                relative, descriptor, parent_fd, name, opened_identity
            ) in descriptors:
                try:
                    content_hash, selectors, used = self._read_page(
                        descriptor,
                        remaining=max_bytes - consumed,
                        check_control=check_control,
                    )
                    consumed += used
                    current = os.stat(
                        name, dir_fd=parent_fd, follow_symlinks=False
                    )
                    if (
                        not stat.S_ISREG(current.st_mode)
                        or current.st_nlink != 1
                        or (current.st_dev, current.st_ino) != opened_identity
                    ):
                        raise SelectorSnapshotChanged(
                            "Wiki page name changed during capture"
                        )
                except OSError as exc:
                    raise SelectorSnapshotChanged(
                        "Wiki page name changed during capture"
                    ) from exc
                finally:
                    os.close(descriptor)
                    os.close(parent_fd)
                pages.append(WikiPageSnapshot(
                    relative=relative,
                    page_id=f"{domain}/{relative[:-3]}",
                    content_hash=content_hash,
                    selectors=selectors,
                ))
            captured = True
        finally:
            try:
                descriptors.close()
            except BaseException:
                watch.close()
                raise
            if not captured:
                watch.close()
        try:
            changed = watch.changed()
        except BaseException:
            watch.close()
            raise
        if changed:
            watch.close()
            raise SelectorSnapshotChanged(
                "Wiki selector snapshot changed during capture"
            )
        pages.sort(key=lambda page: page.relative)
        selector_rows = [
            (page.relative, page.selectors)
            for page in pages
            if page.selectors is not None
        ]
        generation_rows = [
            (page.relative, page.content_hash) for page in pages
        ]
        try:
            return WikiSelectorSnapshot(
                domain=domain,
                pages=tuple(pages),
                fingerprint=_rows_fingerprint(selector_rows, check_control),
                generation_fingerprint=_rows_fingerprint(
                    generation_rows, check_control
                ),
                max_bytes=max_bytes,
                _watch=watch,
            )
        except BaseException:
            watch.close()
            raise

    def fingerprint(
        self,
        *,
        domain: str,
        snapshot: WikiSelectorSnapshot | None = None,
    ) -> str:
        """Return the exact captured page-generation fingerprint."""
        captured = snapshot or self.capture(domain=domain)
        owned = snapshot is None
        try:
            if captured.domain != domain:
                raise SelectorError("Wiki selector snapshot domain mismatch")
            return captured.fingerprint
        finally:
            if owned:
                self.close_snapshot(captured)

    def verify_snapshot(
        self,
        snapshot: WikiSelectorSnapshot,
        *,
        check_control: Callable[[], None] | None = None,
    ) -> None:
        """Check constant-work generation evidence without rescanning pages."""
        if check_control is not None:
            check_control()
        watch = snapshot._watch
        if (
            watch is None
            or watch.changed()
        ):
            raise SelectorSnapshotChanged(
                "Wiki selector snapshot changed during rebuild"
            )
        if check_control is not None:
            check_control()

    @staticmethod
    def close_snapshot(snapshot: WikiSelectorSnapshot) -> None:
        """Release ephemeral generation evidence owned by a snapshot."""
        if snapshot._watch is not None:
            try:
                snapshot._watch.close()
            except OSError:
                pass

    def resolve_snapshot(
        self,
        selector_snapshot: WikiSelectorSnapshot,
        *,
        domain: str,
        project_dir: str,
        parsed_files: tuple[ParsedFile, ...],
        relations: object,
        snapshot: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
        check_control: Callable[[], None] | None = None,
    ) -> tuple[dict[str, object], ...]:
        """Resolve links from captured bytes without rereading Markdown."""
        del project_dir, relations
        if selector_snapshot.domain != domain:
            raise SelectorError("Wiki selector snapshot domain mismatch")
        coherent = snapshot if snapshot is not None else _parsed_snapshot(parsed_files)
        links: list[dict[str, object]] = []
        for page in selector_snapshot.pages:
            if check_control is not None:
                check_control()
            if page.selectors is None:
                continue
            try:
                page_links = resolve_selectors(
                    {"code": page.selectors},
                    coherent,
                    domain=domain,
                    page_id=page.page_id,
                    check_control=check_control,
                )
            except SelectorError:
                continue
            links.extend(page_links)
        if check_control is not None:
            check_control()
        return tuple(sorted(links, key=lambda row: str(row["link_id"])))

    def resolve(
        self,
        *,
        domain: str,
        project_dir: str,
        parsed_files: tuple[ParsedFile, ...],
        relations: object,
        snapshot: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
    ) -> tuple[dict[str, object], ...]:
        captured = self.capture(domain=domain)
        try:
            return self.resolve_snapshot(
                captured,
                domain=domain,
                project_dir=project_dir,
                parsed_files=parsed_files,
                relations=relations,
                snapshot=snapshot,
            )
        finally:
            self.close_snapshot(captured)
