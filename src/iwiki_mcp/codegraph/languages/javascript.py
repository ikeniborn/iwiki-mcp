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


def member_chain(node):
    """Dotted parts of a non-computed member chain, or None.

    Returns None for anything whose target is not decidable statically:
    a computed access (`a[k]`), a call in the chain (`f().b`), or any
    non-identifier link. Callers must not guess past a None.
    """
    parts = []
    current = node
    while current.type == "member_expression":
        prop = current.child_by_field_name("property")
        if prop is None or prop.type != "property_identifier":
            return None
        parts.append(prop)
        current = current.child_by_field_name("object")
        if current is None:
            return None
    if current.type != "identifier":
        return None
    parts.append(current)
    return tuple(
        part.text.decode("utf-8", "replace") for part in reversed(parts)
    )


def object_pair_hook(node, owner_qualified, make_symbol, symbols):
    """Claim `key: function () {}` / `key: () => {}` inside an object literal.

    Shorthand methods are already claimed by the walker's
    `method_definition` branch; only `pair` nodes need this. Computed keys
    and spread properties are skipped -- their target is not statically
    decidable.
    """
    if node.type != "pair" or owner_qualified is None:
        return False
    key = node.child_by_field_name("key")
    value = node.child_by_field_name("value")
    if key is None or value is None:
        return False
    if key.type not in ("property_identifier", "string"):
        return False
    if value.type not in ("function_expression", "arrow_function"):
        return False
    make_symbol(
        node, "method", key,
        owner_qualified=owner_qualified,
        params_node=value.child_by_field_name("parameters"),
        is_async=any(child.type == "async" for child in value.children),
        local_name=key.text.decode("utf-8", "replace").strip("\"'"),
    )
    return True


def prototype_method_hook(node, owner_qualified, make_symbol, symbols):
    """Attach `C.prototype.m = function () {}` to an already-known `C`.

    Deliberately narrow: the owner must resolve to a symbol already
    extracted from this file, so a prototype patch on an imported or
    runtime-built object is skipped instead of guessed at.
    """
    if node.type != "expression_statement":
        return False
    assignment = next(
        (child for child in node.children if child.type == "assignment_expression"),
        None,
    )
    if assignment is None:
        return False
    left = assignment.child_by_field_name("left")
    value = assignment.child_by_field_name("right")
    if left is None or value is None:
        return False
    if value.type not in ("function_expression", "arrow_function"):
        return False
    parts = member_chain(left)
    if parts is None or len(parts) != 3 or parts[1] != "prototype":
        return False
    owner = next(
        (
            item for item in symbols
            if item.local_name == parts[0] and item.kind in ("class", "function")
        ),
        None,
    )
    if owner is None:
        return False
    make_symbol(
        node, "method", left.child_by_field_name("property"),
        owner_qualified=owner.qualified_name,
        params_node=value.child_by_field_name("parameters"),
        is_async=any(child.type == "async" for child in value.children),
    )
    return True


JAVASCRIPT_PROFILE = _ecmascript.LanguageProfile(
    language="javascript",
    prefix="js",
    kind_by_node={},
    handles_interface=False,
    handles_namespace=False,
    object_literal_scope=True,
    declaration_hooks=(object_pair_hook, prototype_method_hook),
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
