"""Tree-sitter-only Python declaration extraction.

The ``FileRecord`` returned here is an explicitly non-persisted placeholder:
its path and content hash allow Task 6 to reconstruct repository-bound identity.
Neither its empty repository id nor its ``parse:`` file id may be stored.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from ..models import FileRecord, ParsedFile, ResolutionResult, SymbolRecord


_PARSER: Any | None = None


def _relative_path(path: str) -> str:
    """Keep only a safe POSIX source-relative spelling, never an absolute path."""
    if not isinstance(path, str) or not path or "\\" in path:
        raise ValueError("invalid source path")
    windows_path = PureWindowsPath(path)
    posix_path = PurePosixPath(path)
    if (
        windows_path.is_absolute()
        or windows_path.drive
        or posix_path.is_absolute()
        or ".." in posix_path.parts
    ):
        raise ValueError("invalid source path")
    parts = [part for part in posix_path.parts if part != "."]
    if parts and parts[0] == "src":
        parts = parts[1:]
    return "/".join(parts)


def _module_name(path: str) -> str:
    relative = _relative_path(path)
    file_path = PurePosixPath(relative)
    parts = list(file_path.parts)
    if parts and parts[-1] == "__init__.py":
        parts.pop()
    elif parts:
        parts[-1] = PurePosixPath(parts[-1]).stem
    return ".".join(part for part in parts if part)


def _compact(source: bytes) -> str:
    """Normalize syntax spelling without interpreting Python values."""
    return b"".join(source.split()).decode("utf-8", "replace")


def _visibility(name: str) -> str:
    return "private" if name.startswith("_") else "public"


class PythonAdapter:
    language = "python"
    extensions = (".py",)

    def __init__(self) -> None:
        self._parser: Any | None = None

    def _get_parser(self):
        global _PARSER
        if self._parser is None:
            if _PARSER is not None:
                self._parser = _PARSER
                return self._parser
            # Import lives here, not at module import or server startup.
            from tree_sitter_language_pack import DownloadError, get_parser
            try:
                self._parser = get_parser("python")
            except DownloadError:
                # The pack may be installed without a downloaded grammar (for
                # example in offline deployments).  This is still Tree-sitter,
                # and remains lazy; the direct grammar keeps parsing fail-soft.
                from tree_sitter import Language, Parser
                import tree_sitter_python

                self._parser = Parser(Language(tree_sitter_python.language()))
            _PARSER = self._parser
        return self._parser

    def parse_file(self, source: bytes, path: str) -> ParsedFile:
        if not isinstance(source, bytes):
            raise TypeError("source must be bytes")
        relative_path = _relative_path(path)
        content_hash = hashlib.sha256(source).hexdigest()
        placeholder_id = f"parse:{hashlib.sha256(relative_path.encode()).hexdigest()}"
        file = FileRecord(
            file_id=placeholder_id,
            repository_id="",
            path=relative_path,
            language=self.language,
            content_hash=content_hash,
            parser_version="tree-sitter-python",
            size_bytes=len(source),
        )
        try:
            root = self._get_parser().parse(source).root_node
        except (TypeError, ValueError):
            return ParsedFile(
                file=file, symbols=(), references=(), warnings=("parse_error",)
            )

        error_ranges = sorted(
            (node.start_byte, node.end_byte)
            for node in self._walk(root)
            if node.type == "ERROR" or node.is_missing
        )
        module = _module_name(relative_path)
        symbols: list[SymbolRecord] = []
        self._collect(root, source, file.file_id, module, (), error_ranges, symbols)
        warnings = ("parse_error",) if error_ranges or root.has_error else ()
        symbols.sort(key=lambda item: (item.start_byte or -1, item.qualified_name))
        return ParsedFile(
            file=file,
            symbols=tuple(symbols),
            references=(),
            warnings=warnings,
        )

    def resolve_references(
        self, parsed: ParsedFile, project_index: Any
    ) -> ResolutionResult:
        """Reference resolution is deliberately owned by the later index task."""
        return ResolutionResult(relations=(), warnings=("resolution_unavailable",))

    @staticmethod
    def _walk(node):
        stack = [node]
        while stack:
            current = stack.pop()
            yield current
            stack.extend(reversed(current.children))

    def _collect(
        self, node, source, file_id, module, parents, error_ranges, symbols
    ):
        stack = [(node, parents)]
        while stack:
            current, current_parents = stack.pop()
            for child in reversed(current.named_children):
                declaration = child
                if child.type == "decorated_definition":
                    definition = child.child_by_field_name("definition")
                    if definition is not None:
                        declaration = definition
                if declaration.type not in {"class_definition", "function_definition"}:
                    stack.append((child, current_parents))
                    continue
                name_node = declaration.child_by_field_name("name")
                name = (
                    source[name_node.start_byte:name_node.end_byte].decode(
                        "utf-8", "replace"
                    )
                    if name_node is not None
                    else None
                )
                is_valid = name is not None and not self._intersects(
                    declaration, error_ranges
                )
                if is_valid:
                    is_class = declaration.type == "class_definition"
                    declaration_source = source[
                        declaration.start_byte:declaration.end_byte
                    ]
                    is_async = not is_class and declaration_source.lstrip().startswith(
                        b"async def"
                    )
                    if is_class:
                        kind = "class"
                    elif current_parents and current_parents[-1][1]:
                        kind = "method"
                    elif is_async:
                        kind = "async_function"
                    else:
                        kind = "function"
                    qualified_parts = [
                        part
                        for part in (module, *(item[0] for item in current_parents), name)
                        if part
                    ]
                    signature = (
                        None
                        if is_class
                        else self._signature(declaration, source, is_async)
                    )
                    own = source[declaration.start_byte:declaration.end_byte]
                    identity = ":".join(
                        (file_id, ".".join(qualified_parts), signature or "")
                    )
                    metadata = json.dumps(
                        {"language": "python", "module": module},
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    symbols.append(
                        SymbolRecord(
                            symbol_id=(
                                f"parse:{hashlib.sha256(identity.encode()).hexdigest()}"
                            ),
                            file_id=file_id,
                            kind=kind,
                            qualified_name=".".join(qualified_parts),
                            local_name=name,
                            start_line=declaration.start_point[0] + 1,
                            end_line=declaration.end_point[0] + 1,
                            start_byte=declaration.start_byte,
                            end_byte=declaration.end_byte,
                            signature=signature,
                            visibility=_visibility(name),
                            content_hash=hashlib.sha256(own).hexdigest(),
                            metadata_json=metadata,
                        )
                    )
                if name is None:
                    stack.append((declaration, current_parents))
                else:
                    next_parents = (
                        *current_parents,
                        (name, declaration.type == "class_definition"),
                    )
                    stack.append((declaration, next_parents))

    @staticmethod
    def _intersects(node, ranges):
        return any(
            (node.start_byte <= start <= node.end_byte)
            if start == end
            else node.start_byte < end and start < node.end_byte
            for start, end in ranges
        )

    @staticmethod
    def _signature(node, source: bytes, is_async: bool) -> str:
        parameters = node.child_by_field_name("parameters")
        result = node.child_by_field_name("return_type")
        value = _compact(source[parameters.start_byte:parameters.end_byte]) if parameters else "()"
        if is_async:
            value = "async" + value
        if result is not None:
            value += "->" + _compact(source[result.start_byte:result.end_byte])
        return value
