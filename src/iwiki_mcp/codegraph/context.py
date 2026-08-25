"""Typed, bounded code graph context composition."""
from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
from typing import Any, Literal

from .models import CodeGraphError, ContextNode, ContextRelation


KNOWN_RELATIONS = frozenset({"DECLARES", "IMPORTS", "CALLS", "INHERITS"})
_MAX_DEPTH = 3
_MAX_NODES = 50
_MAX_FILES = 20
_MAX_SOURCE_BYTES = 200_000
# Prefix alternation must stay in sync with the language prefixes the
# codegraph.application.code_graph_adapter_factories registers
# ("py", "ts", "js").
_CANONICAL_ENTITY_ID = re.compile(
    r"(?:py|ts|js):(?:file|module|symbol):[0-9a-f]{64}\Z"
)


class CodeGraphContextError(CodeGraphError):
    """Raised when a context request violates the public contract."""

    code = "invalid_config"


@dataclass(frozen=True)
class ContextRequest:
    seeds: tuple[str, ...]
    direction: Literal["in", "out", "both"] = "both"
    depth: int = 1
    relations: tuple[str, ...] | None = None
    include_source: bool = False
    include_wiki: bool = True
    max_nodes: int = 50
    max_files: int = 20
    max_source_bytes: int = 200_000


@dataclass(frozen=True)
class ProjectRootIdentity:
    """Private trusted identity for the original bound project directory."""

    path: Path
    device: int
    inode: int


def _entity_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return _CANONICAL_ENTITY_ID.fullmatch(value) is not None


def validate_context_request(
    seeds: list[str],
    *,
    direction: str = "both",
    depth: int = 1,
    relations: list[str] | None = None,
    include_source: bool = False,
    include_wiki: bool = True,
    max_nodes: int = 50,
    max_files: int = 20,
    max_source_bytes: int = 200_000,
) -> ContextRequest:
    """Validate context inputs without binding, filesystem, or SQLite access."""
    if (
        not isinstance(seeds, list)
        or not seeds
        or len(seeds) > _MAX_NODES
        or any(not _entity_id(seed) for seed in seeds)
    ):
        raise CodeGraphContextError("seeds must contain typed entity IDs")
    if direction not in ("in", "out", "both"):
        raise CodeGraphContextError("invalid context direction")
    if type(depth) is not int or not 0 <= depth <= _MAX_DEPTH:
        raise CodeGraphContextError("invalid context depth")
    if relations is None:
        normalized_relations = None
    elif (
        not isinstance(relations, list)
        or not relations
        or any(type(item) is not str or item not in KNOWN_RELATIONS
               for item in relations)
    ):
        raise CodeGraphContextError("invalid context relations")
    else:
        normalized_relations = tuple(sorted(set(relations)))
    if type(include_source) is not bool or type(include_wiki) is not bool:
        raise CodeGraphContextError("context flags must be boolean")
    if type(max_nodes) is not int or not 1 <= max_nodes <= _MAX_NODES:
        raise CodeGraphContextError("invalid context node budget")
    if type(max_files) is not int or not 1 <= max_files <= _MAX_FILES:
        raise CodeGraphContextError("invalid context file budget")
    if (
        type(max_source_bytes) is not int
        or not 0 <= max_source_bytes <= _MAX_SOURCE_BYTES
    ):
        raise CodeGraphContextError("invalid context source budget")
    return ContextRequest(
        seeds=tuple(seeds),
        direction=direction,
        depth=depth,
        relations=normalized_relations,
        include_source=include_source,
        include_wiki=include_wiki,
        max_nodes=max_nodes,
        max_files=max_files,
        max_source_bytes=max_source_bytes,
    )


_ENTITY_SQL = """
    SELECT f.file_id, 'file', f.file_id, NULL, NULL, 'file', f.path,
           f.file_local_name, NULL, f.path, f.start_line, f.end_line,
           f.start_byte, f.end_byte
    FROM files AS f WHERE f.repository_id = ? AND f.file_id IN ({slots})
    UNION ALL
    SELECT f.module_id, 'module', f.file_id, f.module_id, NULL, 'module',
           f.module_qualified_name, f.module_local_name, NULL, f.path,
           f.start_line, f.end_line, f.start_byte, f.end_byte
    FROM files AS f WHERE f.repository_id = ? AND f.module_id IN ({slots})
    UNION ALL
    SELECT s.symbol_id, 'symbol', f.file_id, f.module_id, s.symbol_id, s.kind,
           s.qualified_name, s.local_name, s.signature, f.path,
           s.start_line, s.end_line, s.start_byte, s.end_byte
    FROM symbols AS s JOIN files AS f ON f.file_id = s.file_id
    WHERE f.repository_id = ? AND s.symbol_id IN ({slots})
"""


def _nodes(
    connection: sqlite3.Connection, domain: str, entity_ids: tuple[str, ...]
) -> dict[str, ContextNode]:
    if not entity_ids:
        return {}
    slots = ",".join("?" for _item in entity_ids)
    sql = _ENTITY_SQL.format(slots=slots)
    parameters = [value for _arm in range(3) for value in (domain, *entity_ids)]
    return {
        str(row[0]): ContextNode(
            entity_id=str(row[0]),
            entity_type=str(row[1]),
            file_id=None if row[2] is None else str(row[2]),
            module_id=None if row[3] is None else str(row[3]),
            symbol_id=None if row[4] is None else str(row[4]),
            kind=str(row[5]),
            qualified_name=str(row[6]),
            local_name=str(row[7]),
            signature=None if row[8] is None else str(row[8]),
            path=str(row[9]),
            start_line=int(row[10]),
            end_line=int(row[11]),
            start_byte=int(row[12]),
            end_byte=int(row[13]),
        )
        for row in connection.execute(sql, parameters)
    }


def relation_from_row(row: sqlite3.Row | tuple[Any, ...]) -> ContextRelation:
    source_entity_id = row[3] or row[2] or row[1]
    target_entity_id = row[4] or row[5]
    return ContextRelation(
        relation_id=str(row[0]),
        source_entity_id=str(source_entity_id),
        source_file_id=str(row[1]),
        source_module_id=None if row[2] is None else str(row[2]),
        source_symbol_id=None if row[3] is None else str(row[3]),
        target_module_id=None if row[4] is None else str(row[4]),
        target_symbol_id=None if row[5] is None else str(row[5]),
        target_entity_id=None if target_entity_id is None else str(target_entity_id),
        target_reference=None if row[6] is None else str(row[6]),
        relation_type=str(row[7]),
        source_start_line=int(row[8]),
        source_end_line=int(row[9]),
        source_start_byte=int(row[10]),
        source_end_byte=int(row[11]),
        binding_name=None if row[12] is None else str(row[12]),
        binding_kind=None if row[13] is None else str(row[13]),
        binding_name_tokens_casefold=None if row[14] is None else str(row[14]),
        confidence=float(row[15]),
        resolution_state=str(row[16]),
        metadata_json=str(row[17]),
    )


_RELATION_COLUMNS = """
    relation_id, source_file_id, source_module_id, source_symbol_id,
    target_module_id, target_symbol_id, target_reference, relation_type,
    source_start_line, source_end_line, source_start_byte, source_end_byte,
    binding_name, binding_kind, binding_name_tokens_casefold, confidence,
    resolution_state, metadata_json
"""


def _frontier_relations(
    connection: sqlite3.Connection,
    frontier: tuple[ContextNode, ...],
    request: ContextRequest,
) -> tuple[ContextRelation, ...]:
    candidates: dict[str, ContextRelation] = {}
    relation_filter = set(request.relations or KNOWN_RELATIONS)
    for node in frontier:
        allowed = (
            {"DECLARES", "IMPORTS"}
            if node.entity_type == "module"
            else KNOWN_RELATIONS
            if node.entity_type == "symbol"
            else KNOWN_RELATIONS
        ) & relation_filter
        if not allowed:
            continue
        slots = ",".join("?" for _item in allowed)
        arms = []
        parameters: list[object] = [*sorted(allowed)]
        if request.direction in ("out", "both"):
            if node.entity_type == "file":
                arms.append(
                    "(source_file_id = ? AND source_module_id IS NULL "
                    "AND source_symbol_id IS NULL)"
                )
            elif node.entity_type == "module":
                arms.append("source_module_id = ?")
            else:
                arms.append("source_symbol_id = ?")
            parameters.append(node.entity_id)
        if request.direction in ("in", "both"):
            if node.entity_type == "module":
                arms.append("target_module_id = ?")
                parameters.append(node.entity_id)
            elif node.entity_type == "symbol":
                arms.append("target_symbol_id = ?")
                parameters.append(node.entity_id)
        if not arms:
            continue
        sql = (
            f"SELECT {_RELATION_COLUMNS} FROM relations "
            f"WHERE relation_type IN ({slots}) AND ({' OR '.join(arms)})"
        )
        for row in connection.execute(sql, parameters):
            item = relation_from_row(row)
            candidates[item.relation_id] = item
    return tuple(sorted(candidates.values(), key=lambda item: (
        item.relation_type,
        item.source_entity_id,
        item.target_entity_id or item.target_reference or "",
        item.source_start_byte,
        item.relation_id,
    )))


def _secret_like(path: str) -> bool:
    name = PurePosixPath(path).name.casefold()
    return (
        name == ".env"
        or name.startswith(".env.")
        or name.endswith((".pem", ".key"))
        or name in {"id_rsa", "id_ed25519"}
        or name.startswith(("credentials", "secrets"))
    )


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _same_root(
    value: os.stat_result, expected: ProjectRootIdentity
) -> bool:
    return (
        stat.S_ISDIR(value.st_mode)
        and not stat.S_ISLNK(value.st_mode)
        and value.st_dev == expected.device
        and value.st_ino == expected.inode
    )


def _directory_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink)


def _directory_chain_matches(
    root_descriptor: int,
    components: tuple[tuple[str, tuple[int, int, int, int]], ...],
) -> bool:
    directory_descriptor = None
    try:
        directory_descriptor = os.dup(root_descriptor)
        for name, expected in components:
            child = None
            try:
                child = os.open(
                    name, _directory_flags(), dir_fd=directory_descriptor
                )
                opened_identity = _directory_identity(os.fstat(child))
                named_identity = _directory_identity(os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                ))
                if (
                    opened_identity != expected
                    or named_identity != expected
                ):
                    return False
                os.close(directory_descriptor)
                directory_descriptor = child
                child = None
            finally:
                if child is not None:
                    os.close(child)
        return True
    except OSError:
        return False
    finally:
        if directory_descriptor is not None:
            os.close(directory_descriptor)


def capture_project_root(
    project_dir: str | Path,
) -> ProjectRootIdentity | None:
    """Capture the unsymlinked bound root identity without exposing its path."""
    descriptor = None
    try:
        path = Path(os.path.abspath(os.fspath(project_dir)))
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            return None
        descriptor = os.open(path, _directory_flags())
        opened = os.fstat(descriptor)
        current = path.lstat()
        expected = ProjectRootIdentity(path, before.st_dev, before.st_ino)
        if (
            not _same_root(opened, expected)
            or not _same_root(current, expected)
            or _directory_identity(before) != _directory_identity(opened)
            or _directory_identity(opened) != _directory_identity(current)
        ):
            return None
        return expected
    except (OSError, TypeError, ValueError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_source(
    project_root: ProjectRootIdentity | None,
    path: str,
    expected_hash: str,
    max_file_bytes: int,
    read_cap: int,
) -> tuple[str | None, str | None, int]:
    if project_root is None or _secret_like(path):
        return None, "source_unavailable", 0
    relative = PurePosixPath(path)
    if relative.is_absolute() or ".." in relative.parts or "\\" in path:
        return None, "source_unavailable", 0
    effective_cap = min(max_file_bytes, read_cap)
    root_descriptor = None
    directory_descriptor = None
    file_descriptor = None
    bytes_consumed = 0
    try:
        before_root = project_root.path.lstat()
        if not _same_root(before_root, project_root):
            return None, "source_unavailable", bytes_consumed
        root_descriptor = os.open(project_root.path, _directory_flags())
        opened_root = os.fstat(root_descriptor)
        root_identity = _directory_identity(opened_root)
        if (
            not _same_root(opened_root, project_root)
            or _directory_identity(before_root) != root_identity
        ):
            return None, "source_unavailable", bytes_consumed
        directory_descriptor = os.dup(root_descriptor)
        directory_components = []
        for part in relative.parts[:-1]:
            child = None
            try:
                child = os.open(
                    part, _directory_flags(), dir_fd=directory_descriptor
                )
                child_stat = os.fstat(child)
                if not stat.S_ISDIR(child_stat.st_mode):
                    return None, "source_unavailable", bytes_consumed
                directory_components.append(
                    (part, _directory_identity(child_stat))
                )
                os.close(directory_descriptor)
                directory_descriptor = child
                child = None
            finally:
                if child is not None:
                    os.close(child)
        components = tuple(directory_components)
        expected_directory = (
            components[-1][1]
            if components
            else root_identity
        )
        if (
            _directory_identity(os.fstat(directory_descriptor))
            != expected_directory
            or not _directory_chain_matches(root_descriptor, components)
        ):
            return None, "source_unavailable", bytes_consumed
        name = relative.parts[-1]
        before = os.stat(
            name, dir_fd=directory_descriptor, follow_symlinks=False
        )
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
        ):
            return None, "source_unavailable", bytes_consumed
        if before.st_size > max_file_bytes:
            return None, "source_unavailable", bytes_consumed
        if before.st_size > effective_cap:
            return None, "max_source_bytes_exhausted", bytes_consumed
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        file_descriptor = os.open(name, flags, dir_fd=directory_descriptor)
        opened = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            return None, "source_unavailable", bytes_consumed
        if (
            _directory_identity(os.fstat(directory_descriptor))
            != expected_directory
            or not _directory_chain_matches(root_descriptor, components)
        ):
            return None, "source_unavailable", bytes_consumed
        content = (
            os.read(file_descriptor, effective_cap)
            if effective_cap
            else b""
        )
        bytes_consumed = len(content)
        after = os.fstat(file_descriptor)
        current = os.stat(
            name, dir_fd=directory_descriptor, follow_symlinks=False
        )

        def identity(value):
            return (
                value.st_dev,
                value.st_ino,
                value.st_nlink,
                value.st_size,
                value.st_mtime_ns,
            )
        if (
            identity(before) != identity(opened)
            or identity(opened) != identity(after)
        ):
            return None, "source_unavailable", bytes_consumed
        if (
            identity(after) != identity(current)
            or len(content) != opened.st_size
        ):
            return None, "source_unavailable", bytes_consumed
        after_root = os.fstat(root_descriptor)
        current_root = project_root.path.lstat()
        if (
            not _same_root(after_root, project_root)
            or not _same_root(current_root, project_root)
            or _directory_identity(after_root) != root_identity
            or _directory_identity(current_root) != root_identity
            or _directory_identity(os.fstat(directory_descriptor))
            != expected_directory
            or not _directory_chain_matches(root_descriptor, components)
        ):
            return None, "source_unavailable", bytes_consumed
        if hashlib.sha256(content).hexdigest() != expected_hash:
            return None, "source_changed", bytes_consumed
        return content.decode("utf-8"), None, bytes_consumed
    except (OSError, UnicodeError):
        return None, "source_unavailable", bytes_consumed
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if directory_descriptor is not None:
            os.close(directory_descriptor)
        if root_descriptor is not None:
            os.close(root_descriptor)


class CodeGraphContext:
    """Compose one deterministic context from a caller-held SQLite snapshot."""

    def __init__(
        self,
        domain: str,
        project_root: ProjectRootIdentity | None,
        max_file_bytes: int,
    ):
        self.domain = domain
        self.project_root = project_root
        self.max_file_bytes = max_file_bytes

    def context(
        self, connection: sqlite3.Connection, request: ContextRequest
    ) -> dict[str, object]:
        seed_nodes = _nodes(connection, self.domain, request.seeds)
        warnings = []
        if len(seed_nodes) != len(set(request.seeds)):
            warnings.append("unknown_seed")
        accepted: dict[str, ContextNode] = {}
        accepted_files: dict[str, dict[str, object]] = {}
        truncated = False

        def admit(node: ContextNode) -> bool:
            nonlocal truncated
            if node.entity_id in accepted:
                return True
            if len(accepted) >= request.max_nodes:
                warnings.append("max_nodes_exhausted")
                truncated = True
                return False
            assert node.file_id is not None
            if node.file_id not in accepted_files:
                if len(accepted_files) >= request.max_files:
                    warnings.append("max_files_exhausted")
                    truncated = True
                    return False
                row = connection.execute(
                    "SELECT file_id, path, language, content_hash, size_bytes, "
                    "start_line, end_line, start_byte, end_byte FROM files "
                    "WHERE repository_id = ? AND file_id = ?",
                    (self.domain, node.file_id),
                ).fetchone()
                if row is None:
                    return False
                accepted_files[node.file_id] = {
                    "file_id": str(row[0]),
                    "path": str(row[1]),
                    "language": str(row[2]),
                    "content_hash": str(row[3]),
                    "size_bytes": int(row[4]),
                    "start_line": int(row[5]),
                    "end_line": int(row[6]),
                    "start_byte": int(row[7]),
                    "end_byte": int(row[8]),
                }
            accepted[node.entity_id] = node
            return True

        initial: list[ContextNode] = []
        for seed in request.seeds:
            node = seed_nodes.get(seed)
            if node is not None and admit(node):
                initial.append(node)
                if node.entity_type == "file":
                    row = connection.execute(
                        "SELECT module_id FROM files WHERE file_id = ?",
                        (node.entity_id,),
                    ).fetchone()
                    if row and row[0]:
                        module = _nodes(connection, self.domain, (str(row[0]),)).get(
                            str(row[0])
                        )
                        if module is not None and admit(module):
                            initial.append(module)

        frontier = tuple(initial)
        accepted_relations: dict[str, ContextRelation] = {}
        for _level in range(0 if truncated else request.depth):
            next_frontier: list[ContextNode] = []
            for relation in _frontier_relations(connection, frontier, request):
                neighbor_id = None
                frontier_ids = {node.entity_id for node in frontier}
                if relation.source_entity_id in frontier_ids:
                    neighbor_id = relation.target_entity_id
                elif relation.target_entity_id in frontier_ids:
                    neighbor_id = relation.source_entity_id
                if neighbor_id is not None and neighbor_id not in accepted:
                    neighbor = _nodes(
                        connection, self.domain, (neighbor_id,)
                    ).get(neighbor_id)
                    if neighbor is None or not admit(neighbor):
                        if truncated:
                            break
                        continue
                    next_frontier.append(neighbor)
                accepted_relations[relation.relation_id] = relation
            frontier = tuple(next_frontier)
            if truncated or not frontier:
                break

        if request.include_source:
            used = 0
            unsafe_source = False
            for file_row in accepted_files.values():
                remaining = max(0, request.max_source_bytes - used)
                source, source_warning, bytes_consumed = _read_source(
                    self.project_root,
                    str(file_row["path"]),
                    str(file_row["content_hash"]),
                    self.max_file_bytes,
                    read_cap=min(self.max_file_bytes, remaining),
                )
                used += bytes_consumed
                if source is None:
                    warnings.append(source_warning or "source_unavailable")
                    if source_warning == "max_source_bytes_exhausted":
                        truncated = True
                    else:
                        unsafe_source = True
                    continue
                file_row["source"] = source
            if unsafe_source:
                for file_row in accepted_files.values():
                    file_row.pop("source", None)

        wiki_pages: list[dict[str, object]] = []
        if request.include_wiki and accepted:
            symbol_ids = tuple(sorted(
                node.symbol_id for node in accepted.values()
                if node.symbol_id is not None
            ))
            file_ids = tuple(sorted(accepted_files))
            arms: list[str] = []
            parameters: list[object] = [self.domain]
            if symbol_ids:
                arms.append(
                    "symbol_id IN (" + ",".join("?" for _ in symbol_ids) + ")"
                )
                parameters.extend(symbol_ids)
            if file_ids:
                arms.append(
                    "file_id IN (" + ",".join("?" for _ in file_ids) + ")"
                )
                parameters.extend(file_ids)
            if arms:
                rows = connection.execute(
                    "SELECT domain, page_id, selector_kind, relation_type, source "
                    "FROM wiki_code_links WHERE domain = ? AND ("
                    + " OR ".join(arms)
                    + ") ORDER BY page_id, selector_kind, source",
                    parameters,
                )
                specificity = {"source_glob": 0, "file": 1, "symbol": 2}
                selected_pages: dict[str, dict[str, object]] = {}
                for row in rows:
                    item = {
                        "domain": str(row[0]),
                        "page_id": str(row[1]),
                        "relation_type": str(row[3]),
                        "selector_kind": str(row[2]),
                        "source": str(row[4]),
                    }
                    current = selected_pages.get(item["page_id"])
                    if current is None or specificity[item["selector_kind"]] > (
                        specificity[str(current["selector_kind"])]
                    ):
                        selected_pages[str(item["page_id"])] = item
                wiki_pages = [selected_pages[key] for key in sorted(selected_pages)]

        return {
            "seeds": list(request.seeds),
            "nodes": [asdict(node) for node in accepted.values()],
            "relations": [asdict(item) for item in accepted_relations.values()],
            "files": list(accepted_files.values()),
            "wiki_pages": wiki_pages,
            "limits": {
                "depth": request.depth,
                "max_nodes": request.max_nodes,
                "max_files": request.max_files,
                "max_source_bytes": request.max_source_bytes,
            },
            "truncated": truncated,
            "warnings": list(dict.fromkeys(warnings)),
        }
