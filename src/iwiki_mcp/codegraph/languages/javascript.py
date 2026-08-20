"""Tree-sitter-only JavaScript declaration extraction.

Shares the Tree-sitter walker, heritage resolver and import extractor
with `typescript.py` via `_ecmascript`; unlike TypeScript, every
JavaScript file is unconditionally module-backed (no `is_module`
probe) and there is no opt-in type boost.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from pathlib import PurePosixPath
from typing import Mapping

from . import _ecmascript
from ..models import (
    FileRecord,
    ParsedFile,
    ReferenceRecord,
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


def enclosing_symbol_id(symbols, node):
    """The innermost symbol whose byte range contains `node`, or None.

    "Innermost" means the smallest span among all containing candidates;
    Tasks 9 and 10 reuse this exact function to attribute INHERITS/CALLS
    references the same way.
    """
    candidates = [
        symbol for symbol in symbols
        if symbol.start_byte <= node.start_byte and node.end_byte <= symbol.end_byte
    ]
    if not candidates:
        return None
    innermost = min(candidates, key=lambda symbol: symbol.end_byte - symbol.start_byte)
    return innermost.symbol_id


def attribute(reference, symbols, file_record, node):
    """Point a reference at its innermost enclosing symbol.

    `source_module_id` must be None whenever `source_symbol_id` is set:
    schema.py enforces
    CHECK (source_module_id IS NULL OR source_symbol_id IS NULL)
    and the snapshot insert aborts otherwise.
    """
    symbol_id = enclosing_symbol_id(symbols, node)
    return replace(
        reference,
        source_symbol_id=symbol_id,
        source_file_id=file_record.file_id,
        source_module_id=file_record.module_id if symbol_id is None else None,
    )


def _require_specifier(source, value):
    """The literal specifier a `require(...)` call targets, or None.

    Only a single-argument call whose sole argument is a string literal is
    trusted; a computed/dynamic argument (`require(name)`) is not guessed
    at.
    """
    if value.type != "call_expression":
        return None
    func = value.child_by_field_name("function")
    if func is None or func.type != "identifier":
        return None
    if _ecmascript.text(source, func) != "require":
        return None
    arguments = value.child_by_field_name("arguments")
    if arguments is None:
        return None
    named_arguments = arguments.named_children
    if len(named_arguments) != 1 or named_arguments[0].type != "string":
        return None
    return _ecmascript.text(source, named_arguments[0]).strip("\"'")


def _require_bindings(source, name_node):
    """(binding_name, binding_kind) pairs a `require(...)` LHS pattern binds.

    An `array_pattern` (`const [first] = require(...)`) yields no bindings
    -- positional destructuring off a CommonJS export is not a decidable
    named binding, so it produces no reference at all (trust rule).
    """
    if name_node.type == "identifier":
        return ((_ecmascript.text(source, name_node), "implicit_binding"),)
    if name_node.type == "object_pattern":
        bindings = []
        for item in name_node.children:
            if item.type == "shorthand_property_identifier_pattern":
                bindings.append((_ecmascript.text(source, item), "implicit_binding"))
            elif item.type == "pair_pattern":
                value = item.child_by_field_name("value")
                if value is not None and value.type == "identifier":
                    bindings.append((_ecmascript.text(source, value), "explicit_alias"))
        return tuple(bindings)
    return ()


def require_references(source, root, *, file_record, symbols):
    """CommonJS `require(...)` bindings, attributed to their enclosing scope.

    Unlike ESM imports (module-level only), a `require` can sit inside a
    function, so each reference is run through `attribute` to point it at
    its innermost enclosing symbol instead of the module.
    """
    references = []

    def walk(node):
        for child in node.children:
            if child.type in ("lexical_declaration", "variable_declaration"):
                for declarator in child.children:
                    if declarator.type != "variable_declarator":
                        continue
                    value = declarator.child_by_field_name("value")
                    name_node = declarator.child_by_field_name("name")
                    if value is None or name_node is None:
                        continue
                    specifier = _require_specifier(source, value)
                    if specifier is None:
                        continue
                    for binding_name, binding_kind in _require_bindings(source, name_node):
                        reference = ReferenceRecord(
                            source_symbol_id=None,
                            source_file_id=file_record.file_id,
                            relation_type="IMPORTS",
                            target_reference=specifier,
                            source_line=declarator.start_point[0] + 1,
                            source_byte=declarator.start_byte,
                            source_end_line=declarator.end_point[0] + 1,
                            source_end_byte=declarator.end_byte,
                            binding_name=binding_name,
                            binding_kind=binding_kind,
                            binding_name_tokens_casefold=token_key(binding_name),
                            resolution_hint="unresolved",
                        )
                        references.append(
                            attribute(reference, symbols, file_record, declarator)
                        )
            walk(child)

    walk(root)
    return tuple(references)


_SPECIFIER_EXTENSIONS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")


def dotted_candidate(importer_path: str, specifier: str) -> str | None:
    """Normalize a relative specifier into a project dotted module name.

    Returns None for anything whose target is not decidable from the path
    alone -- bare package names, subpath imports, URLs, Node builtins, and
    paths escaping the repository root. Guessing those would invent an edge.
    """
    if not specifier.startswith("."):
        return None
    parts = list(PurePosixPath(importer_path).parent.parts)
    for segment in PurePosixPath(specifier).parts:
        if segment == ".":
            continue
        if segment == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(segment)
    if not parts:
        return None
    tail = parts[-1]
    for extension in _SPECIFIER_EXTENSIONS:
        if tail.casefold().endswith(extension):
            tail = tail[: -len(extension)]
            break
    parts[-1] = tail.split(".", 1)[0]
    return ".".join(parts)


@dataclass(frozen=True)
class ImportAlias:
    """A local binding name's source specifier and what shape it binds.

    `binding_shape` decides how `expand_alias` treats a member chain whose
    head is this binding:

      * `"named"` -- `{ a }` / `{ a as b }` / `const { a } = require(...)`.
        `imported_name` carries the name in the exporting module; the chain
        expands to `<module>.<imported name>` plus its remaining segments.
      * `"namespace"` -- `* as ns`, and a whole-module `const m =
        require(...)`. Both genuinely bind the module object, so the chain
        expands to `<module>` plus its remaining segments.
      * `"default"` -- `import thing from './m'`. `thing` is the value the
        module default-exports, whose shape is not statically known;
        `thing.build` is a property of that value and has nothing to do
        with a named export `build`. Never expanded (trust rule).

    `imported_name` is `None` for every shape other than `"named"`.
    """

    specifier: str
    imported_name: str | None
    binding_shape: str = "named"


def _require_alias_bindings(source, name_node):
    """(local_name, imported_name) pairs a `require(...)` LHS pattern binds.

    Mirrors `_require_bindings` but also keeps the exporting-module name for
    a `{ a: b }` pair pattern, which `expand_alias` needs to build the
    dotted target. An `array_pattern` yields no bindings, same trust rule
    as `_require_bindings`.
    """
    if name_node.type == "identifier":
        return ((_ecmascript.text(source, name_node), None),)
    if name_node.type == "object_pattern":
        bindings = []
        for item in name_node.children:
            if item.type == "shorthand_property_identifier_pattern":
                local = _ecmascript.text(source, item)
                bindings.append((local, local))
            elif item.type == "pair_pattern":
                key = item.child_by_field_name("key")
                value = item.child_by_field_name("value")
                if key is not None and value is not None and value.type == "identifier":
                    bindings.append(
                        (_ecmascript.text(source, value), _ecmascript.text(source, key))
                    )
        return tuple(bindings)
    return ()


def import_aliases(source: bytes, root) -> Mapping[str, "ImportAlias"]:
    """Local binding name -> `ImportAlias`, for ESM imports and `require`.

    Module-level declarations only, matching `esm_import_references`'s and
    `require_references`'s own top-level import surface for this file: a
    heritage target is a class-level identifier, which can only be bound by
    a module-level import/require in scope for the class declaration.
    """
    aliases: dict[str, ImportAlias] = {}
    for child in root.children:
        if child.type == "import_statement":
            source_node = child.child_by_field_name("source")
            if source_node is None:
                continue
            specifier = _ecmascript.text(source, source_node).strip("\"'")
            clause = next(
                (grandchild for grandchild in child.children
                 if grandchild.type == "import_clause"),
                None,
            )
            if clause is None:
                continue
            for item in clause.children:
                if item.type == "identifier":
                    local = _ecmascript.text(source, item)
                    aliases[local] = ImportAlias(specifier, None, "default")
                elif item.type == "named_imports":
                    for specifier_node in item.children:
                        if specifier_node.type != "import_specifier":
                            continue
                        name_node = specifier_node.child_by_field_name("name")
                        alias_node = specifier_node.child_by_field_name("alias")
                        if name_node is None:
                            continue
                        imported_name = _ecmascript.text(source, name_node)
                        local = (
                            _ecmascript.text(source, alias_node)
                            if alias_node is not None else imported_name
                        )
                        aliases[local] = ImportAlias(specifier, imported_name)
                elif item.type == "namespace_import":
                    name_node = next(
                        (grandchild for grandchild in item.children
                         if grandchild.type == "identifier"),
                        None,
                    )
                    if name_node is not None:
                        local = _ecmascript.text(source, name_node)
                        aliases[local] = ImportAlias(specifier, None, "namespace")
        elif child.type in ("lexical_declaration", "variable_declaration"):
            for declarator in child.children:
                if declarator.type != "variable_declarator":
                    continue
                value = declarator.child_by_field_name("value")
                name_node = declarator.child_by_field_name("name")
                if value is None or name_node is None:
                    continue
                specifier = _require_specifier(source, value)
                if specifier is None:
                    continue
                for local, imported_name in _require_alias_bindings(source, name_node):
                    # A whole-module `const m = require(...)` really does bind
                    # the module object under CommonJS, so it is namespace-like
                    # -- unlike an ESM default import.
                    aliases[local] = ImportAlias(
                        specifier, imported_name,
                        "named" if imported_name is not None else "namespace",
                    )
    return aliases


def expand_alias(aliases, importer_path, parts):
    """Dotted target for a member chain whose head is an alias, or None.

    `None` when `parts[0]` is not an alias, when the alias binds an ESM
    default export (whose value's shape is not statically known), or when
    the alias's specifier is not resolvable by path arithmetic (a bare
    package name) -- the trust rule `dotted_candidate` already enforces.
    """
    if not parts:
        return None
    alias = aliases.get(parts[0])
    if alias is None or alias.binding_shape == "default":
        return None
    module = dotted_candidate(importer_path, alias.specifier)
    if module is None:
        return None
    segments = tuple(parts[1:])
    if alias.imported_name is not None:
        segments = (alias.imported_name, *segments)
    if not segments:
        return module
    return ".".join((module, *segments))


def _walk(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _call_reference(node, target, scope, hint, file_record):
    return ReferenceRecord(
        source_symbol_id=None,
        source_file_id=file_record.file_id,
        relation_type="CALLS",
        target_reference=target,
        source_line=node.start_point[0] + 1,
        source_byte=node.start_byte,
        source_end_line=node.end_point[0] + 1,
        source_end_byte=node.end_byte,
        resolution_scope=scope,
        resolution_hint=hint,
    )


def _call_target(
    parts, *, aliases, importer_path, qualified_names, symbols, node, module_dotted_name,
):
    expanded = expand_alias(aliases, importer_path, parts)
    if expanded is not None:
        return expanded, "project", None
    enclosing_id = enclosing_symbol_id(symbols, node)
    enclosing_qualified_name = next(
        (
            symbol.qualified_name for symbol in symbols
            if symbol.symbol_id == enclosing_id
        ),
        None,
    )
    # Only a function-like body is a lexical scope for a bare identifier
    # call. A class body and an object literal are not: `helper()` inside a
    # method never binds to a sibling member without `this.`/the object
    # name, unlike an `extends` target, which IS a lexical name. So admit a
    # candidate rung only when it is the module scope or the qualified name
    # of a function-like symbol; everything else -- class bodies, object
    # literals, any future non-lexical scope -- is skipped rather than
    # enumerated as an exclusion.
    lexical_scope_names = {
        symbol.qualified_name for symbol in symbols
        if symbol.kind in ("function", "async_function", "method")
    }
    candidates = _ecmascript.heritage_scope_candidates(
        enclosing_qualified_name, module_dotted_name,
    )
    tail = ".".join(parts)
    for scope in candidates:
        if scope != module_dotted_name and scope not in lexical_scope_names:
            continue
        candidate = f"{scope}.{tail}"
        if candidate in qualified_names:
            return candidate, "file", None
    return tail, None, "unresolved"


def call_references(
    root, *, file_record, symbols, aliases, importer_path, module_dotted_name,
):
    """CALLS edges for statically decidable callees only.

    Skipped, deliberately:
      * a `type_arguments` child -- the tsx grammar parses the plain-JS
        comparison chain `a < b > (c)` as a call with type arguments, so
        extracting it would invent an edge that does not exist in JS;
      * a missing `arguments` field or one shaped as a `template_string` --
        covers both a tagged template (``tag`text` ``), whose `arguments`
        field the grammar fills with the template_string instead of an
        argument list, and a paren-less `new Foo;`, whose `arguments` field
        is simply absent;
      * a computed callee (`o[k]()`) and a callee that is itself a call
        (`f()()`) -- neither reaches member_chain;
      * `require(...)`, already modelled as IMPORTS.
    """
    references = []
    qualified_names = {symbol.qualified_name for symbol in symbols}
    for node in _walk(root):
        if node.type not in ("call_expression", "new_expression"):
            continue
        if any(child.type == "type_arguments" for child in node.children):
            continue
        arguments = node.child_by_field_name("arguments")
        if arguments is None or arguments.type == "template_string":
            continue
        field = "function" if node.type == "call_expression" else "constructor"
        callee = node.child_by_field_name(field)
        if callee is None or callee.type not in ("identifier", "member_expression"):
            continue
        parts = member_chain(callee)
        if parts is None or parts == ("require",):
            continue
        target, scope, hint = _call_target(
            parts,
            aliases=aliases,
            importer_path=importer_path,
            qualified_names=qualified_names,
            symbols=symbols,
            node=node,
            module_dotted_name=module_dotted_name,
        )
        references.append(attribute(
            _call_reference(node, target, scope, hint, file_record),
            symbols, file_record, node,
        ))
    return tuple(references)


def resolve_specifier(reference, importer_path, index):
    """Bind a relative specifier to a project module, or leave it alone."""
    candidate = dotted_candidate(importer_path, reference.target_reference or "")
    if candidate is None:
        return reference
    for probe in (candidate, f"{candidate}.index"):
        if probe in index.modules_by_qualified:
            return replace(
                reference,
                target_reference=probe,
                resolution_hint=None,
                resolution_scope="project",
                target_kind_hint="module",
            )
    return reference


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
        aliases = import_aliases(source, root)

        def heritage_rewriter(name):
            expanded = expand_alias(aliases, relative_path, (name,))
            if expanded is None:
                return None
            return (expanded, "project")

        symbols, pending_heritage = _ecmascript.extract_symbols(
            source, root,
            profile=JAVASCRIPT_PROFILE,
            repository_id=self.repository_id, relative_path=relative_path,
            file_record=file, module_dotted_name=module_dotted_name,
            heritage_rewriter=heritage_rewriter,
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
            *require_references(source, root, file_record=file, symbols=symbols),
            *heritage_references,
            *call_references(
                root, file_record=file, symbols=symbols, aliases=aliases,
                importer_path=relative_path, module_dotted_name=module_dotted_name,
            ),
        )
        return _JavaScriptParsedFile(
            file=file, symbols=tuple(symbols), references=references,
            warnings=warnings,
        )

    def resolve_references(self, parsed, project_index) -> ResolutionResult:
        declares = declaration_relations(
            self.language, self.prefix, self.repository_id, parsed,
        )
        references = tuple(
            resolve_specifier(item, parsed.file.path, project_index)
            if item.relation_type == "IMPORTS" else item
            for item in parsed.references
        )
        resolved = resolve_references(
            self.language, self.prefix, self.repository_id,
            references, project_index,
        )
        return ResolutionResult(
            relations=sort_relations((*declares, *resolved)), warnings=(),
        )
