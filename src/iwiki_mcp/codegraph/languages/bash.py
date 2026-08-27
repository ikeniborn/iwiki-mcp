"""Tree-sitter-only Bash declaration extraction."""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from ..models import (
    FileRecord,
    ParsedFile,
    ResolutionResult,
    SymbolRecord,
    compact_casefold,
    file_id,
    module_id,
    symbol_id,
    token_key,
)
from ..resolver import declaration_relations


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
    return "/".join(part for part in posix_path.parts if part != ".")


def _module_name(path: str) -> str:
    parts = list(PurePosixPath(path).parts)
    parts[-1] = parts[-1][:-3]
    return ".".join(parts)


class BashAdapter:
    """Extract Bash function declarations without executing source bytes."""

    language = "bash"
    prefix = "sh"
    extensions = (".sh",)
    adapter_version = "bash-adapter-v1"

    def __init__(
        self,
        repository_id: str,
        _source_paths: tuple[str, ...],
        *,
        parser_version: str = "tree-sitter-bash",
    ) -> None:
        if (
            not isinstance(repository_id, str)
            or not repository_id
            or "\0" in repository_id
        ):
            raise ValueError("invalid repository id")
        self.repository_id = repository_id
        self.parser_version = parser_version
        self._parser: Any | None = None

    def _get_parser(self):
        if self._parser is None:
            from tree_sitter import Language, Parser
            import tree_sitter_bash

            self._parser = Parser(Language(tree_sitter_bash.language()))
        return self._parser

    @staticmethod
    def _walk(node):
        stack = [node]
        while stack:
            current = stack.pop()
            yield current
            stack.extend(reversed(current.named_children))

    def _error_ranges(self, root):
        return tuple(sorted(
            (node.start_byte, node.end_byte)
            for node in self._walk(root)
            if node.type == "ERROR" or node.is_missing
        ))

    @staticmethod
    def _intersects_error(node, ranges) -> bool:
        return any(
            node.start_byte <= start <= node.end_byte
            if start == end
            else node.start_byte < end and start < node.end_byte
            for start, end in ranges
        )

    def parse_file(self, source: bytes, path: str) -> ParsedFile:
        if not isinstance(source, bytes):
            raise TypeError("source must be bytes")
        relative_path = _relative_path(path)
        module = _module_name(relative_path)
        stable_file_id = file_id(
            self.language, self.prefix, self.repository_id, relative_path
        )
        file = FileRecord(
            file_id=stable_file_id,
            repository_id=self.repository_id,
            path=relative_path,
            path_casefold=compact_casefold(relative_path),
            file_local_name=PurePosixPath(relative_path).name,
            file_name_tokens_casefold=token_key(PurePosixPath(relative_path).name),
            language=self.language,
            content_hash=hashlib.sha256(source).hexdigest(),
            parser_version=self.parser_version,
            size_bytes=len(source),
            start_line=1,
            end_line=max(1, source.count(b"\n") + 1),
            start_byte=0,
            end_byte=len(source),
            module_key=relative_path,
            module_id=module_id(
                self.language, self.prefix, self.repository_id, relative_path, module
            ),
            module_qualified_name=module,
            module_local_name=module.rsplit(".", 1)[-1],
            module_name_tokens_casefold=token_key(module, module.rsplit(".", 1)[-1]),
        )
        try:
            root = self._get_parser().parse(source).root_node
        except (TypeError, ValueError):
            return ParsedFile(file=file, symbols=(), references=(), warnings=("parse_error",))

        error_ranges = self._error_ranges(root)
        declarations = []
        for node in self._walk(root):
            if node.type != "function_definition" or self._intersects_error(
                node, error_ranges
            ):
                continue
            name_node = node.child_by_field_name("name")
            if name_node is None:
                continue
            name = source[name_node.start_byte:name_node.end_byte].decode(
                "utf-8", "replace"
            )
            declarations.append((node, name, ".".join((module, name))))

        counts: dict[str, int] = {}
        for _node, _name, qualified_name in declarations:
            counts[qualified_name] = counts.get(qualified_name, 0) + 1
        symbols = []
        for node, name, qualified_name in declarations:
            normalized_signature = (
                "" if counts[qualified_name] == 1
                else f"occurrence:{node.start_byte}"
            )
            own_source = source[node.start_byte:node.end_byte]
            symbols.append(SymbolRecord(
                symbol_id=symbol_id(
                    self.language,
                    self.prefix,
                    self.repository_id,
                    relative_path,
                    qualified_name,
                    normalized_signature,
                ),
                file_id=file.file_id,
                kind="function",
                qualified_name=qualified_name,
                local_name=name,
                name_tokens_casefold=token_key(qualified_name, name),
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                start_byte=node.start_byte,
                end_byte=node.end_byte,
                signature=None,
                signature_casefold=None,
                visibility=None,
                content_hash=hashlib.sha256(own_source).hexdigest(),
                metadata_json=json.dumps(
                    {"module": module}, sort_keys=True, separators=(",", ":")
                ),
            ))
        symbols.sort(key=lambda item: (item.start_byte, item.qualified_name, item.symbol_id))
        return ParsedFile(
            file=file,
            symbols=tuple(symbols),
            references=(),
            warnings=("parse_error",) if error_ranges or root.has_error else (),
        )

    def resolve_references(self, parsed: ParsedFile, project_index: Any) -> ResolutionResult:
        return ResolutionResult(
            relations=declaration_relations(
                self.language, self.prefix, parsed.file.repository_id, parsed
            ),
            warnings=(),
        )
