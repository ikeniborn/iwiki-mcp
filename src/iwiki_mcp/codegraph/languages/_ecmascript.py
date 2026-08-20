"""Shared Tree-sitter ECMAScript declaration extraction.

The TypeScript and JavaScript adapters share one walker, one heritage
resolver and one import extractor; the per-language differences are
carried by `LanguageProfile` rather than by duplicated code. Every
profile default reproduces TypeScript's behaviour, so this module is a
byte-for-byte extraction of what `typescript.py` used to do on its own.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Callable, Mapping

from ..models import (
    FileRecord,
    ReferenceRecord,
    SymbolRecord,
    compact_casefold,
    file_id,
    symbol_id,
    token_key,
)


_PARSERS: dict[str, Any] = {}


@dataclass(frozen=True)
class LanguageProfile:
    """Per-language switches for the shared ECMAScript walker.

    Every default reproduces TypeScript's current behaviour, so a profile
    built with only language/prefix/kind_by_node drives the walker exactly
    as `typescript.py` did before the extraction.
    """

    language: str
    prefix: str
    kind_by_node: Mapping[str, str] = field(default_factory=dict)
    handles_interface: bool = True
    handles_namespace: bool = True
    object_literal_scope: bool = False
    declaration_hooks: tuple = ()


def text(source: bytes, node) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", "replace")


def param_signature(source: bytes, params_node) -> str:
    if params_node is None:
        return "()"
    return text(source, params_node)


def return_type_signature(source: bytes, return_type_node) -> str:
    if return_type_node is None:
        return ""
    return "->" + text(source, return_type_node).lstrip(":").strip()


def visibility(name: str) -> str:
    return "private" if name.startswith("_") or name.startswith("#") else "public"


@dataclass(frozen=True)
class PendingHeritage:
    """A class/interface heritage clause target awaiting scope resolution.

    TypeScript name resolution is lexical: the target of `extends`/
    `implements` may be declared in the declaring class/interface's own
    enclosing scope, or in any scope outward from there, down to the
    module -- it is not necessarily a sibling in the *same* scope as the
    declaring class. Building the final `target_reference` therefore needs
    the full (deduplicated) symbol set for the file, which isn't known
    until `walk()` finishes. This record carries just enough to resolve it
    in that later pass: `owner_qualified` is the qualified name of the
    scope the declaring class/interface itself sits in (the innermost
    scope to probe first), and `name` is the heritage target's bare
    identifier. See `resolve_heritage_references`.

    `target_reference_override` short-circuits that probe entirely when a
    language supplies its own mapping (a JavaScript import alias resolves
    to another module, not to a scope in this file), and
    `resolution_scope` records which space that answer lives in.
    """

    owner_symbol_id: str
    source_file_id: str
    owner_qualified: str | None
    name: str
    source_line: int
    source_byte: int
    source_end_line: int
    source_end_byte: int
    resolution_scope: str = "file"
    target_reference_override: str | None = None


def pending_heritage_references(
    source: bytes, node, *, owner_symbol_id: str, source_file_id: str,
    owner_qualified: str | None = None,
    target_rewriter: Callable[[str], tuple[str, str] | None] | None = None,
):
    pending = []
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
            name = text(source, target)
            rewritten = target_rewriter(name) if target_rewriter is not None else None
            override = rewritten[0] if rewritten is not None else None
            scope = rewritten[1] if rewritten is not None else "file"
            pending.append(PendingHeritage(
                owner_symbol_id=owner_symbol_id,
                source_file_id=source_file_id,
                owner_qualified=owner_qualified,
                name=name,
                source_line=clause.start_point[0] + 1,
                source_byte=clause.start_byte,
                source_end_line=clause.end_point[0] + 1,
                source_end_byte=clause.end_byte,
                resolution_scope=scope,
                target_reference_override=override,
            ))
    return tuple(pending)


def heritage_scope_candidates(
    owner_qualified: str | None, module_dotted_name: str,
) -> tuple[str, ...]:
    """Ordered scope-qualified-name prefixes to probe, innermost first.

    Always ends at `module_dotted_name`, the outermost (module) scope --
    the same fallback target `pending_heritage_references` used to build
    unconditionally before this fix.
    """
    if not owner_qualified:
        return (module_dotted_name,)
    candidates = [owner_qualified]
    scope = owner_qualified
    while scope != module_dotted_name and "." in scope:
        scope = scope.rsplit(".", 1)[0]
        candidates.append(scope)
    if candidates[-1] != module_dotted_name:
        candidates.append(module_dotted_name)
    return tuple(candidates)


def resolve_heritage_references(
    pending: tuple[PendingHeritage, ...],
    qualified_names: set[str],
    module_dotted_name: str,
) -> tuple[ReferenceRecord, ...]:
    """Resolve each pending heritage target to the innermost matching scope.

    Tries `owner_qualified` (the declaring class/interface's own enclosing
    scope) first, then walks outward one `.`-separated scope level at a
    time down to `module_dotted_name`, using the first candidate that
    matches an actually-collected symbol's `qualified_name`. If none
    match, falls back to the module-scoped candidate (last in the probe
    order) -- an unresolved-but-present reference, same as before this fix,
    for heritage targets that are genuinely external/cross-file.

    A record carrying `target_reference_override` skips the probe: the
    language already knows where that target lives.
    """
    references = []
    for item in pending:
        if item.target_reference_override is not None:
            target_reference = item.target_reference_override
        else:
            candidates = heritage_scope_candidates(item.owner_qualified, module_dotted_name)
            target_reference = f"{candidates[-1]}.{item.name}"
            for scope in candidates:
                candidate = f"{scope}.{item.name}"
                if candidate in qualified_names:
                    target_reference = candidate
                    break
        references.append(ReferenceRecord(
            source_symbol_id=item.owner_symbol_id,
            source_file_id=item.source_file_id,
            relation_type="INHERITS",
            target_reference=target_reference,
            source_line=item.source_line,
            source_byte=item.source_byte,
            source_end_line=item.source_end_line,
            source_end_byte=item.source_end_byte,
            resolution_scope=item.resolution_scope,
        ))
    return tuple(references)


def extract_symbols(
    source: bytes,
    root,
    *,
    profile: LanguageProfile,
    repository_id: str,
    relative_path: str,
    file_record: FileRecord,
    module_dotted_name: str,
    heritage_rewriter: Callable[[str], tuple[str, str] | None] | None = None,
):
    language = profile.language
    prefix = profile.prefix
    kind_by_node = profile.kind_by_node
    symbols: list[SymbolRecord] = []
    pending_heritage: list[PendingHeritage] = []

    def make_symbol(
        node, kind, name_node, *, owner_qualified=None,
        params_node=None, return_type_node=None, is_async=False,
        local_name=None,
    ):
        local_name = local_name if local_name is not None else text(source, name_node)
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
                f"{param_signature(source, params_node)}"
                f"{return_type_signature(source, return_type_node)}"
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
            visibility=visibility(local_name),
            content_hash=hashlib.sha256(
                source[node.start_byte:node.end_byte]
            ).hexdigest(),
            metadata_json="{}",
        ))
        return qualified, stable_id

    def _namespace_qualified(child, owner_qualified):
        """Scope a `namespace X { ... }` / `declare module "x" { ... }` body.

        Neither construct emits its own SymbolRecord (extraction is scoped to
        the plan's declaration kinds only), but nested declarations must
        still be narrowed under the namespace/module's own name -- otherwise
        two same-named declarations in different namespaces/ambient modules
        flatten to the same qualified_name and collide, the same root cause
        as C1's per-function collision.
        """
        name_node = child.child_by_field_name("name")
        if name_node is None:
            return owner_qualified
        local_name = text(source, name_node).strip("\"'")
        if not local_name:
            return owner_qualified
        return (
            f"{owner_qualified}.{local_name}" if owner_qualified
            else f"{module_dotted_name}.{local_name}"
        )

    def walk(node, owner_qualified=None):
        for child in node.children:
            ctype = child.type
            if ctype in kind_by_node:
                name_node = child.child_by_field_name("name")
                qualified = owner_qualified
                if name_node is not None:
                    qualified, _ = make_symbol(
                        child, kind_by_node[ctype], name_node,
                        owner_qualified=owner_qualified,
                    )
                walk(child, qualified)
            elif ctype == "class_declaration":
                name_node = child.child_by_field_name("name")
                if name_node is None:
                    walk(child, owner_qualified)
                    continue
                qualified, stable_id = make_symbol(
                    child, "class", name_node, owner_qualified=owner_qualified,
                )
                pending_heritage.extend(pending_heritage_references(
                    source, child, owner_symbol_id=stable_id,
                    source_file_id=file_record.file_id, owner_qualified=owner_qualified,
                    target_rewriter=heritage_rewriter,
                ))
                body = child.child_by_field_name("body")
                if body is not None:
                    walk(body, qualified)
            elif ctype == "interface_declaration" and profile.handles_interface:
                name_node = child.child_by_field_name("name")
                qualified = owner_qualified
                if name_node is not None:
                    qualified, stable_id = make_symbol(
                        child, "interface", name_node, owner_qualified=owner_qualified,
                    )
                    pending_heritage.extend(pending_heritage_references(
                        source, child, owner_symbol_id=stable_id,
                        source_file_id=file_record.file_id, owner_qualified=owner_qualified,
                        target_rewriter=heritage_rewriter,
                    ))
                walk(child, qualified)
            elif ctype == "method_definition":
                name_node = child.child_by_field_name("name")
                qualified = owner_qualified
                if name_node is not None and owner_qualified is not None:
                    qualified, _ = make_symbol(
                        child, "method", name_node,
                        owner_qualified=owner_qualified,
                        params_node=child.child_by_field_name("parameters"),
                        return_type_node=child.child_by_field_name("return_type"),
                        is_async=any(
                            grandchild.type == "async"
                            for grandchild in child.children
                        ),
                    )
                walk(child, qualified)
            elif ctype == "function_declaration":
                name_node = child.child_by_field_name("name")
                qualified = owner_qualified
                if name_node is not None:
                    qualified, _ = make_symbol(
                        child, "function", name_node,
                        owner_qualified=owner_qualified,
                        params_node=child.child_by_field_name("parameters"),
                        return_type_node=child.child_by_field_name("return_type"),
                        is_async=any(
                            grandchild.type == "async" for grandchild in child.children
                        ),
                    )
                walk(child, qualified)
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
                        qualified, _ = make_symbol(
                            declarator, "function", name_node,
                            owner_qualified=owner_qualified,
                            params_node=value.child_by_field_name("parameters"),
                            return_type_node=value.child_by_field_name("return_type"),
                            is_async=any(
                                grandchild.type == "async"
                                for grandchild in value.children
                            ),
                        )
                        walk(value, qualified)
                    elif (
                        profile.object_literal_scope
                        and value is not None
                        and value.type == "object"
                        and name_node is not None
                        and name_node.type not in ("object_pattern", "array_pattern")
                    ):
                        local = text(source, name_node)
                        scope = (
                            f"{owner_qualified}.{local}" if owner_qualified
                            else f"{module_dotted_name}.{local}"
                        )
                        walk(value, scope)
                    else:
                        walk(declarator, owner_qualified)
            elif ctype == "internal_module" and profile.handles_namespace:
                # `namespace X { ... }` / `declare namespace X.Y { ... }`.
                walk(child, _namespace_qualified(child, owner_qualified))
            elif (
                ctype == "module"
                and profile.handles_namespace
                and child.child_by_field_name("body") is not None
            ):
                # Ambient `declare module "specifier" { ... }`; the plain
                # `module` node type only carries a body in this ambient
                # shape, so this can't misfire on an unrelated grammar node.
                walk(child, _namespace_qualified(child, owner_qualified))
            else:
                claimed = False
                for hook in profile.declaration_hooks:
                    if hook(child, owner_qualified, make_symbol, symbols):
                        claimed = True
                        break
                if not claimed:
                    walk(child, owner_qualified)

    walk(root)
    return tuple(symbols), tuple(pending_heritage)


def dedupe_symbols(symbols):
    """Collapse colliding symbol_ids, keeping the last by start byte.

    Anonymous scopes (IIFEs, callback bodies, if/try blocks) contribute no
    qualified-name segment, so same-named siblings there genuinely collide.
    This guarantees the build never hits the PRIMARY KEY constraint and
    surfaces the degradation instead of hiding it.
    """
    symbols_by_id: dict[str, SymbolRecord] = {}
    duplicate_symbol_ids: set[str] = set()
    for symbol in symbols:
        previous = symbols_by_id.get(symbol.symbol_id)
        if previous is not None:
            duplicate_symbol_ids.add(symbol.symbol_id)
        if previous is None or symbol.start_byte >= previous.start_byte:
            symbols_by_id[symbol.symbol_id] = symbol
    deduped = list(symbols_by_id.values())
    deduped.sort(key=lambda item: (item.start_byte or -1, item.qualified_name))
    warnings = tuple(sorted((
        *("duplicate_symbol_identity" for _item in duplicate_symbol_ids),
    )))
    return deduped, warnings


def import_bindings(source: bytes, clause) -> tuple[tuple[str, str], ...]:
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
            bindings.append((text(source, item), "implicit_binding"))
        elif item.type == "named_imports":
            for specifier in item.children:
                if specifier.type != "import_specifier":
                    continue
                alias_node = specifier.child_by_field_name("alias")
                if alias_node is not None:
                    bindings.append((text(source, alias_node), "explicit_alias"))
                    continue
                name_node = specifier.child_by_field_name("name")
                if name_node is not None:
                    bindings.append((text(source, name_node), "implicit_binding"))
        elif item.type == "namespace_import":
            name_node = next(
                (grandchild for grandchild in item.children
                 if grandchild.type == "identifier"),
                None,
            )
            if name_node is not None:
                # "* as ns" always renames the whole module namespace, the
                # same semantics as Python's "import x as y".
                bindings.append((text(source, name_node), "explicit_alias"))
    return tuple(bindings)


def esm_import_references(source: bytes, root, *, file_record: FileRecord):
    references: list[ReferenceRecord] = []
    for child in root.children:
        if child.type != "import_statement":
            continue
        source_node = child.child_by_field_name("source")
        if source_node is None:
            continue
        specifier = text(source, source_node).strip("\"'")
        clause = next(
            (grandchild for grandchild in child.children
             if grandchild.type == "import_clause"),
            None,
        )
        for binding_name, binding_kind in import_bindings(source, clause):
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


def relative_path(path: str) -> str:
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


def get_parser(grammar: str) -> Any:
    parser = _PARSERS.get(grammar)
    if parser is not None:
        return parser
    from tree_sitter import Language, Parser

    try:
        from tree_sitter_language_pack import get_parser as _pack_get_parser
        parser = _pack_get_parser(grammar)
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
