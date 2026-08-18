"""Tree-sitter-only TypeScript/TSX declaration extraction."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from ..models import (
    FileRecord,
    ParsedFile,
    ReferenceRecord,
    ResolutionResult,
    SymbolRecord,
    compact_casefold,
    file_id,
    module_id,
    symbol_id,
    token_key,
)
from ..resolver import declaration_relations, resolve_references, sort_relations


_PARSERS: dict[str, Any] = {}

_KIND_BY_NODE = {
    "type_alias_declaration": "type_alias",
    "enum_declaration": "enum",
}


def _text(source: bytes, node) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _param_signature(source: bytes, params_node) -> str:
    if params_node is None:
        return "()"
    return _text(source, params_node)


def _return_type_signature(source: bytes, return_type_node) -> str:
    if return_type_node is None:
        return ""
    return "->" + _text(source, return_type_node).lstrip(":").strip()


def _visibility(name: str) -> str:
    return "private" if name.startswith("_") or name.startswith("#") else "public"


def _heritage_references(
    source: bytes, node, *, owner_symbol_id: str, file_record,
    module_dotted_name: str,
):
    references = []
    heritage = next(
        (child for child in node.children if child.type == "class_heritage"), None
    )
    clauses = []
    if heritage is not None:
        clauses.extend(heritage.children)
    extends_type = next(
        (child for child in node.children if child.type == "extends_type_clause"),
        None,
    )
    if extends_type is not None:
        clauses.append(extends_type)
    for clause in clauses:
        if clause.type not in ("extends_clause", "implements_clause", "extends_type_clause"):
            continue
        for target in clause.children:
            if target.type not in ("identifier", "type_identifier", "nested_type_identifier"):
                continue
            name = _text(source, target)
            references.append(ReferenceRecord(
                source_symbol_id=owner_symbol_id,
                source_file_id=file_record.file_id,
                relation_type="INHERITS",
                target_reference=f"{module_dotted_name}.{name}",
                source_line=clause.start_point[0] + 1,
                source_byte=clause.start_byte,
                source_end_line=clause.end_point[0] + 1,
                source_end_byte=clause.end_byte,
                resolution_scope="file",
            ))
    return tuple(references)


def _extract_symbols(
    source: bytes,
    root,
    *,
    language: str,
    prefix: str,
    repository_id: str,
    relative_path: str,
    file_record: FileRecord,
    module_dotted_name: str,
):
    symbols: list[SymbolRecord] = []
    references: list[ReferenceRecord] = []

    def make_symbol(
        node, kind, name_node, *, owner_qualified=None,
        params_node=None, return_type_node=None, is_async=False,
    ):
        local_name = _text(source, name_node)
        qualified = (
            f"{owner_qualified}.{local_name}" if owner_qualified
            else f"{module_dotted_name}.{local_name}"
        )
        record_kind = kind
        signature = None
        if kind in ("function", "method"):
            record_kind = "async_function" if is_async and kind == "function" else kind
            signature = (
                f"{record_kind}|{'async' if is_async else ''}"
                f"{_param_signature(source, params_node)}"
                f"{_return_type_signature(source, return_type_node)}"
            )
        stable_id = symbol_id(
            language, prefix, repository_id, relative_path,
            qualified, signature or "",
        )
        symbols.append(SymbolRecord(
            symbol_id=stable_id,
            file_id=file_id(language, prefix, repository_id, relative_path),
            kind=record_kind,
            qualified_name=qualified,
            local_name=local_name,
            name_tokens_casefold=token_key(qualified, local_name),
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            start_byte=node.start_byte,
            end_byte=node.end_byte,
            signature=signature,
            signature_casefold=compact_casefold(signature),
            visibility=_visibility(local_name),
            content_hash=hashlib.sha256(
                source[node.start_byte:node.end_byte]
            ).hexdigest(),
            metadata_json="{}",
        ))
        return qualified, stable_id

    def walk(node, owner_qualified=None):
        for child in node.children:
            ctype = child.type
            if ctype in _KIND_BY_NODE:
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    make_symbol(child, _KIND_BY_NODE[ctype], name_node,
                                owner_qualified=owner_qualified)
                walk(child, owner_qualified)
            elif ctype == "class_declaration":
                name_node = child.child_by_field_name("name")
                if name_node is None:
                    walk(child, owner_qualified)
                    continue
                qualified, stable_id = make_symbol(
                    child, "class", name_node, owner_qualified=owner_qualified,
                )
                references.extend(_heritage_references(
                    source, child, owner_symbol_id=stable_id, file_record=file_record,
                    module_dotted_name=module_dotted_name,
                ))
                body = child.child_by_field_name("body")
                if body is not None:
                    walk(body, qualified)
            elif ctype == "interface_declaration":
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    qualified, stable_id = make_symbol(
                        child, "interface", name_node, owner_qualified=owner_qualified,
                    )
                    references.extend(_heritage_references(
                        source, child, owner_symbol_id=stable_id, file_record=file_record,
                        module_dotted_name=module_dotted_name,
                    ))
                walk(child, owner_qualified)
            elif ctype == "method_definition":
                name_node = child.child_by_field_name("name")
                if name_node is not None and owner_qualified is not None:
                    make_symbol(
                        child, "method", name_node,
                        owner_qualified=owner_qualified,
                        params_node=child.child_by_field_name("parameters"),
                        return_type_node=child.child_by_field_name("return_type"),
                        is_async=any(
                            grandchild.type == "async"
                            for grandchild in child.children
                        ),
                    )
                walk(child, owner_qualified)
            elif ctype == "function_declaration":
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    make_symbol(
                        child, "function", name_node,
                        owner_qualified=owner_qualified,
                        params_node=child.child_by_field_name("parameters"),
                        return_type_node=child.child_by_field_name("return_type"),
                        is_async=any(
                            grandchild.type == "async" for grandchild in child.children
                        ),
                    )
                walk(child, owner_qualified)
            elif ctype in ("lexical_declaration", "variable_declaration"):
                for declarator in child.children:
                    if declarator.type != "variable_declarator":
                        continue
                    value = declarator.child_by_field_name("value")
                    name_node = declarator.child_by_field_name("name")
                    if (
                        value is not None
                        and value.type in ("arrow_function", "function_expression")
                        and name_node is not None
                    ):
                        make_symbol(
                            declarator, "function", name_node,
                            owner_qualified=owner_qualified,
                            params_node=value.child_by_field_name("parameters"),
                            return_type_node=value.child_by_field_name("return_type"),
                            is_async=any(
                                grandchild.type == "async"
                                for grandchild in value.children
                            ),
                        )
                walk(child, owner_qualified)
            else:
                walk(child, owner_qualified)

    walk(root)
    return tuple(symbols), tuple(references)


def _import_bindings(source: bytes, clause) -> tuple[tuple[str, str], ...]:
    """Return (binding_name, binding_kind) pairs one import clause binds.

    ``import_clause`` children (no field names in the grammar) are one of:
    a bare ``identifier`` (default import), a ``named_imports`` block of
    ``import_specifier`` nodes (each with a ``name`` field and an optional
    ``alias`` field), a ``namespace_import`` (``* as name``), or a default
    identifier followed by a ``named_imports`` block (combined form). A
    side-effect-only import (``import "./m"``) has no clause at all.
    """
    if clause is None:
        return ()
    bindings: list[tuple[str, str]] = []
    for item in clause.children:
        if item.type == "identifier":
            bindings.append((_text(source, item), "implicit_binding"))
        elif item.type == "named_imports":
            for specifier in item.children:
                if specifier.type != "import_specifier":
                    continue
                alias_node = specifier.child_by_field_name("alias")
                if alias_node is not None:
                    bindings.append((_text(source, alias_node), "explicit_alias"))
                    continue
                name_node = specifier.child_by_field_name("name")
                if name_node is not None:
                    bindings.append((_text(source, name_node), "implicit_binding"))
        elif item.type == "namespace_import":
            name_node = next(
                (grandchild for grandchild in item.children
                 if grandchild.type == "identifier"),
                None,
            )
            if name_node is not None:
                # "* as ns" always renames the whole module namespace, the
                # same semantics as Python's "import x as y".
                bindings.append((_text(source, name_node), "explicit_alias"))
    return tuple(bindings)


def _extract_references(source: bytes, root, *, file_record: FileRecord):
    references: list[ReferenceRecord] = []
    for child in root.children:
        if child.type != "import_statement":
            continue
        source_node = child.child_by_field_name("source")
        if source_node is None:
            continue
        specifier = _text(source, source_node).strip("\"'")
        clause = next(
            (grandchild for grandchild in child.children
             if grandchild.type == "import_clause"),
            None,
        )
        for binding_name, binding_kind in _import_bindings(source, clause):
            references.append(ReferenceRecord(
                source_symbol_id=None,
                source_file_id=file_record.file_id,
                source_module_id=file_record.module_id,
                relation_type="IMPORTS",
                target_reference=specifier,
                source_line=child.start_point[0] + 1,
                source_byte=child.start_byte,
                source_end_line=child.end_point[0] + 1,
                source_end_byte=child.end_byte,
                binding_name=binding_name,
                binding_kind=binding_kind,
                binding_name_tokens_casefold=token_key(binding_name),
                resolution_hint="unresolved",
            ))
    return tuple(references)


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
        module_dotted_name = ".".join(
            PurePosixPath(relative_path).with_suffix("").parts
        )
        module_qualified_name = module_dotted_name if is_module else None
        module_local_name = PurePosixPath(relative_path).stem if is_module else None
        stable_module_id = (
            module_id(
                self.language, self.prefix, self.repository_id,
                relative_path, module_qualified_name,
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
            module_qualified_name=module_qualified_name,
            module_local_name=module_local_name,
            module_name_tokens_casefold=(
                token_key(module_qualified_name, module_local_name)
                if module_qualified_name else None
            ),
        )
        symbols, heritage_references = _extract_symbols(
            source, root,
            language=self.language, prefix=self.prefix,
            repository_id=self.repository_id, relative_path=relative_path,
            file_record=file, module_dotted_name=module_dotted_name,
        )
        references = (
            *_extract_references(source, root, file_record=file),
            *heritage_references,
        )
        return _TypeScriptParsedFile(
            file=file, symbols=symbols, references=references, warnings=(),
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
