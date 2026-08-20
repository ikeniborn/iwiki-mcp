"""Tree-sitter-only JavaScript declaration extraction.

Shares the Tree-sitter walker, heritage resolver and import extractor
with `typescript.py` via `_ecmascript`; unlike TypeScript, every
JavaScript file is unconditionally module-backed (no `is_module`
probe) and there is no opt-in type boost.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import PurePosixPath

from . import _ecmascript
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


JAVASCRIPT_PROFILE = _ecmascript.LanguageProfile(
    language="javascript",
    prefix="js",
    kind_by_node={},
    handles_interface=False,
    handles_namespace=False,
    object_literal_scope=True,
    declaration_hooks=(),          # Task 6 fills this in
)


@dataclass(frozen=True)
class _JavaScriptParsedFile(ParsedFile):
    pass


class JavaScriptAdapter:
    language = "javascript"
    prefix = "js"
    extensions = (".js", ".jsx", ".mjs", ".cjs")

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
        relative_path = _ecmascript.relative_path(path)
        content_hash = hashlib.sha256(source).hexdigest()
        stable_file_id = file_id(
            self.language, self.prefix, self.repository_id, relative_path,
        )
        parser = _ecmascript.get_parser("tsx")
        tree = parser.parse(source)
        root = tree.root_node

        posix_path = PurePosixPath(relative_path)
        local_name = posix_path.name.split(".", 1)[0]
        module_dotted_name = ".".join((*posix_path.parent.parts, local_name))
        stable_module_id = module_id(
            self.language, self.prefix, self.repository_id,
            relative_path, module_dotted_name,
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
            module_qualified_name=module_dotted_name,
            module_local_name=local_name,
            module_name_tokens_casefold=token_key(module_dotted_name, local_name),
        )
        symbols, pending_heritage = _ecmascript.extract_symbols(
            source, root,
            profile=JAVASCRIPT_PROFILE,
            repository_id=self.repository_id, relative_path=relative_path,
            file_record=file, module_dotted_name=module_dotted_name,
        )
        # Safety net for genuine `symbol_id` collisions that scoping alone
        # cannot prevent: declarations inside anonymous/block scopes (arrow
        # callback bodies, if/else branches, try/catch blocks, IIFEs) get no
        # named scope segment of their own, so same-named siblings there
        # still flatten to the same qualified_name. This does not attempt to
        # invent a semantically correct qualified_name for those cases -- it
        # only guarantees the build never crashes on the PRIMARY KEY
        # constraint, keeping the last-by-position declaration and surfacing
        # a warning so the degradation is visible. Mirrors typescript.py's
        # equivalent dedup in `parse_file`.
        symbols, warnings = _ecmascript.dedupe_symbols(symbols)
        # Heritage targets resolve against the final, post-dedup symbol set
        # (see `_ecmascript.resolve_heritage_references`): name resolution
        # is lexical, so a class's `extends` target may live in any
        # enclosing scope outward from where the class itself is declared,
        # not just the class's own immediate scope.
        heritage_references = _ecmascript.resolve_heritage_references(
            pending_heritage,
            {symbol.qualified_name for symbol in symbols},
            module_dotted_name,
        )
        references = (
            *_ecmascript.esm_import_references(source, root, file_record=file),
            *heritage_references,
        )
        return _JavaScriptParsedFile(
            file=file, symbols=tuple(symbols), references=references,
            warnings=warnings,
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
