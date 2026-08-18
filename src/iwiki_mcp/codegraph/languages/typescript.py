"""Tree-sitter-only TypeScript/TSX declaration extraction."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from ..models import (
    FileRecord,
    ParsedFile,
    ResolutionResult,
    compact_casefold,
    file_id,
    module_id,
    token_key,
)
from ..resolver import declaration_relations, resolve_references, sort_relations


_PARSERS: dict[str, Any] = {}


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


def _grammar_name(path: str) -> str:
    return "tsx" if path.casefold().endswith(".tsx") else "typescript"


def _get_parser(grammar: str) -> Any:
    parser = _PARSERS.get(grammar)
    if parser is not None:
        return parser
    from tree_sitter import Language, Parser

    try:
        from tree_sitter_language_pack import get_parser
        parser = get_parser(grammar)
    except Exception:
        import tree_sitter_typescript as ts_typescript

        capsule = (
            ts_typescript.language_tsx()
            if grammar == "tsx"
            else ts_typescript.language_typescript()
        )
        parser = Parser(Language(capsule))
    _PARSERS[grammar] = parser
    return parser


@dataclass(frozen=True)
class _TypeScriptParsedFile(ParsedFile):
    pass


class TypeScriptAdapter:
    language = "typescript"
    prefix = "ts"
    extensions = (".ts", ".tsx")

    def __init__(
        self,
        repository_id: str,
        source_paths: tuple[str, ...],
        *,
        parser_version: str = "tree-sitter-typescript",
    ) -> None:
        if not isinstance(repository_id, str) or not repository_id:
            raise ValueError("invalid repository id")
        self.repository_id = repository_id
        self.parser_version = parser_version

    def parse_file(self, source: bytes, path: str) -> ParsedFile:
        if not isinstance(source, bytes):
            raise TypeError("source must be bytes")
        relative_path = _relative_path(path)
        content_hash = hashlib.sha256(source).hexdigest()
        stable_file_id = file_id(
            self.language, self.prefix, self.repository_id, relative_path,
        )
        parser = _get_parser(_grammar_name(relative_path))
        tree = parser.parse(source)
        root = tree.root_node

        is_module = any(
            child.type in ("import_statement", "export_statement")
            for child in root.children
        )
        module_local_name = PurePosixPath(relative_path).stem if is_module else None
        stable_module_id = (
            module_id(
                self.language, self.prefix, self.repository_id,
                relative_path, relative_path,
            )
            if is_module else None
        )

        file = FileRecord(
            file_id=stable_file_id,
            repository_id=self.repository_id,
            path=relative_path,
            path_casefold=compact_casefold(relative_path),
            file_local_name=PurePosixPath(relative_path).name,
            file_name_tokens_casefold=token_key(PurePosixPath(relative_path).name),
            language=self.language,
            content_hash=content_hash,
            parser_version=self.parser_version,
            size_bytes=len(source),
            start_line=1,
            end_line=max(1, source.count(b"\n") + 1),
            start_byte=0,
            end_byte=len(source),
            module_key=relative_path,
            module_id=stable_module_id,
            module_qualified_name=relative_path if is_module else None,
            module_local_name=module_local_name,
            module_name_tokens_casefold=(
                token_key(module_local_name) if module_local_name else None
            ),
        )
        return _TypeScriptParsedFile(
            file=file, symbols=(), references=(), warnings=(),
        )

    def resolve_references(self, parsed, project_index) -> ResolutionResult:
        declares = declaration_relations(
            self.language, self.prefix, self.repository_id, parsed,
        )
        resolved = resolve_references(
            self.language, self.prefix, self.repository_id,
            parsed.references, project_index,
        )
        return ResolutionResult(
            relations=sort_relations((*declares, *resolved)), warnings=(),
        )
