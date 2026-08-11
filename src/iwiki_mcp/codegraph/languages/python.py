"""Tree-sitter-only Python declaration extraction."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import io
import json
import keyword
from pathlib import PurePosixPath, PureWindowsPath
import token
import tokenize
from typing import Any, Mapping

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
    return "/".join(part for part in posix_path.parts if part != ".")


def _dotted_module(parts: list[str]) -> str | None:
    if parts and parts[-1] == "__init__.py":
        parts.pop()
    elif parts:
        parts[-1] = PurePosixPath(parts[-1]).stem
    if not parts or any(
        not part.isidentifier() or keyword.iskeyword(part)
        for part in parts
    ):
        return None
    return ".".join(part for part in parts if part)


def derive_module_names(source_paths: tuple[str, ...]) -> dict[str, str | None]:
    """Derive dotted names only from source-root or package evidence."""
    paths = tuple(sorted({_relative_path(path) for path in source_paths}))
    package_dirs = {
        PurePosixPath(path).parent.as_posix()
        for path in paths
        if PurePosixPath(path).name == "__init__.py"
    }
    result: dict[str, str | None] = {}
    for path in paths:
        parts = list(PurePosixPath(path).parts)
        module_parts: list[str] | None = None
        for index in range(1, len(parts)):
            directory = PurePosixPath(*parts[:index]).as_posix()
            descendants = (
                PurePosixPath(*parts[:depth]).as_posix()
                for depth in range(index, len(parts))
            )
            if directory in package_dirs and all(
                item in package_dirs for item in descendants
            ):
                module_parts = parts[index - 1:]
                break
        result[path] = (
            _dotted_module(module_parts.copy()) if module_parts is not None else None
        )
    return result


def _compact(source: bytes) -> str:
    """Normalize token spacing while preserving literal token contents."""
    text = source.decode("utf-8", "replace")
    ignored = {
        token.ENDMARKER,
        token.INDENT,
        token.DEDENT,
        token.NEWLINE,
        tokenize.NL,
        token.ENCODING,
    }
    values = []
    previous_type = None
    for item in tokenize.generate_tokens(io.StringIO(text).readline):
        if item.type in ignored or item.type == token.COMMENT:
            continue
        if previous_type in {token.NAME, token.NUMBER} and item.type in {
            token.NAME,
            token.NUMBER,
        }:
            values.append(" ")
        values.append(item.string)
        previous_type = item.type
    return "".join(values)


def _visibility(name: str) -> str:
    return "private" if name.startswith("_") else "public"


@dataclass(frozen=True)
class _PythonParsedFile(ParsedFile):
    _adapter_evidence: tuple[tuple[str, str, str, str, str], ...] = ()


class PythonAdapter:
    language = "python"
    prefix = "py"
    extensions = (".py",)

    def __init__(
        self,
        repository_id: str,
        source_paths_or_module_names: (
            tuple[str, ...] | Mapping[str, str | None]
        ),
        *,
        parser_version: str = "tree-sitter-python",
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
        if isinstance(source_paths_or_module_names, Mapping):
            self._module_names = {
                _relative_path(path): module_name
                for path, module_name in source_paths_or_module_names.items()
            }
        else:
            self._module_names = derive_module_names(
                tuple(source_paths_or_module_names)
            )

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
        stable_file_id = file_id(
            self.language,
            self.prefix,
            self.repository_id,
            relative_path,
        )
        module = self._module_names.get(relative_path)
        module_local_name = module.rsplit(".", 1)[-1] if module else None
        stable_module_id = (
            module_id(
                self.language,
                self.prefix,
                self.repository_id,
                relative_path,
                module,
            )
            if module else None
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
            module_qualified_name=module,
            module_local_name=module_local_name,
            module_name_tokens_casefold=(
                token_key(module, module_local_name) if module else None
            ),
        )
        try:
            root = self._get_parser().parse(source).root_node
        except (TypeError, ValueError):
            warnings = {"parse_error"}
            if module is None:
                warnings.add("module_name_unavailable")
            return ParsedFile(
                file=file,
                symbols=(),
                references=(),
                warnings=tuple(sorted(warnings)),
            )

        error_ranges = sorted(
            (node.start_byte, node.end_byte)
            for node in self._walk(root)
            if node.type == "ERROR" or node.is_missing
        )
        package = PurePosixPath(relative_path).name == "__init__.py"
        import_aliases = self._import_aliases(
            root, source, module or "", package
        )
        symbols: list[SymbolRecord] = []
        owners = []
        self._collect(
            root,
            source,
            file.file_id,
            file.module_key,
            module or "",
            package,
            import_aliases,
            (),
            error_ranges,
            symbols,
            owners,
        )
        symbols_by_id = {}
        duplicate_symbol_ids = set()
        for symbol in symbols:
            previous = symbols_by_id.get(symbol.symbol_id)
            if previous is not None:
                duplicate_symbol_ids.add(symbol.symbol_id)
            if previous is None or symbol.start_byte >= previous.start_byte:
                symbols_by_id[symbol.symbol_id] = symbol
        symbols = list(symbols_by_id.values())
        warning_codes = {
            *({"parse_error"} if error_ranges or root.has_error else set()),
            *({"module_name_unavailable"} if module is None else set()),
        }
        warnings = tuple(sorted((
            *warning_codes,
            *("duplicate_symbol_identity" for _item in duplicate_symbol_ids),
        )))
        symbols.sort(key=lambda item: (item.start_byte or -1, item.qualified_name))
        references = self._references(
            root, source, file, module or "", symbols, owners, error_ranges
        )
        return _PythonParsedFile(
            file=file,
            symbols=tuple(symbols),
            references=references,
            warnings=warnings,
            _adapter_evidence=self._project_class_lookup_mutations(
                root,
                source,
                module or "",
                package,
            ),
        )

    def resolve_references(
        self, parsed: ParsedFile, project_index: Any
    ) -> ResolutionResult:
        declarations = declaration_relations(
            self.language,
            self.prefix,
            parsed.file.repository_id,
            parsed,
        )
        return ResolutionResult(
            relations=sort_relations((
                *declarations,
                *resolve_references(
                    self.language,
                    self.prefix,
                    parsed.file.repository_id,
                    self._project_safe_references(parsed, project_index),
                    project_index,
                ),
            )),
            warnings=(),
        )

    def _project_safe_references(self, parsed, project_index):
        sources = {item.symbol_id: item for item in parsed.symbols}
        project_symbols = {
            item.symbol_id: item
            for candidates in project_index.by_qualified.values()
            for item in candidates
        }
        mutated_class_ids = {
            item.symbol_id
            for language, kind, target_kind, target, member
            in project_index._adapter_evidence
            if language == self.language
            and kind == "class_member_mutation"
            and target_kind == "member"
            and member == "__getattribute__"
            for item in project_index.by_qualified.get(target, ())
            if item.kind == "class"
            and item.file_id in project_index.module_backed_file_ids
            and self._metadata(item).get("language") == self.language
        }
        references = []
        for reference in parsed.references:
            source = sources.get(reference.source_symbol_id)
            target = reference.target_reference or ""
            owner = (
                source.qualified_name.rsplit(".", 1)[0]
                if source is not None and source.kind == "method" else ""
            )
            member = target.rsplit(".", 1)[-1]
            receiver_dispatch = (
                reference.relation_type == "CALLS"
                and owner
                and target.startswith(owner + ".")
            )
            owner_symbol_id = (
                self._metadata(source).get("owner_symbol_id")
                if source is not None else None
            )
            subclass_owner_ids = self._project_subclasses(
                owner_symbol_id, project_symbols.values()
            )
            receiver_owner_ids = set(subclass_owner_ids)
            if isinstance(owner_symbol_id, str):
                receiver_owner_ids.add(owner_symbol_id)
            custom_attribute_lookup = any(
                item.kind == "class"
                and item.symbol_id in receiver_owner_ids
                and (
                    item.symbol_id in mutated_class_ids
                    or "__getattribute__" in self._metadata(item).get(
                        "member_bindings", []
                    )
                )
                for item in project_symbols.values()
            )
            has_override = any(
                item.kind == "method"
                and item.local_name == member
                and self._metadata(item).get("owner_symbol_id")
                in subclass_owner_ids
                for item in project_symbols.values()
            )
            if not has_override:
                for item in project_symbols.values():
                    if (item.kind != "class"
                            or item.symbol_id not in subclass_owner_ids):
                        continue
                    metadata = self._metadata(item)
                    bindings = (
                        metadata.get("member_bindings", [])
                        if isinstance(metadata, dict) else []
                    )
                    if member in bindings:
                        has_override = True
                        break
            if receiver_dispatch and (
                has_override or custom_attribute_lookup
            ):
                reference = replace(
                    reference, resolution_hint="unresolved"
                )
            references.append(reference)
        return tuple(references)

    @staticmethod
    def _project_subclasses(owner_symbol_id, symbols):
        symbols = list(symbols)
        classes = [item for item in symbols if item.kind == "class"]
        symbols_by_id = {item.symbol_id: item for item in symbols}
        classes_by_qualified = {}
        for item in classes:
            classes_by_qualified.setdefault(
                item.qualified_name, []
            ).append(item)
        if owner_symbol_id not in symbols_by_id:
            return frozenset()
        reachable = {owner_symbol_id}
        subclasses = set()
        remaining = classes
        while remaining:
            changed = False
            pending = []
            for candidate in remaining:
                metadata = PythonAdapter._metadata(candidate)
                bases = (
                    metadata.get("base_references", [])
                    if isinstance(metadata, dict) else []
                )
                local_prefixes = []
                lexical_owner = symbols_by_id.get(
                    metadata.get("owner_symbol_id")
                )
                if lexical_owner is not None:
                    local_prefixes.append(lexical_owner.qualified_name)
                module = metadata.get("module")
                if isinstance(module, str) and module:
                    local_prefixes.append(module)
                parent_ids = set()
                for base in bases:
                    if not isinstance(base, str):
                        continue
                    if "." in base:
                        candidates = classes_by_qualified.get(base, [])
                        parent_ids.update(
                            item.symbol_id for item in candidates
                        )
                        continue
                    else:
                        candidates = []
                        for prefix in local_prefixes:
                            candidates = [
                                item for item in classes_by_qualified.get(
                                    prefix + "." + base, []
                                )
                                if item.file_id == candidate.file_id
                            ]
                            if candidates:
                                break
                    if len(candidates) == 1:
                        parent_ids.add(candidates[0].symbol_id)
                if not (parent_ids & reachable):
                    pending.append(candidate)
                    continue
                changed = True
                subclasses.add(candidate.symbol_id)
                reachable.add(candidate.symbol_id)
            if not changed:
                break
            remaining = pending
        return frozenset(subclasses)

    @staticmethod
    def _metadata(symbol):
        try:
            value = json.loads(symbol.metadata_json)
        except (TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _references(
        self, root, source, file, module, symbols, owners, error_ranges
    ):
        package = PurePosixPath(file.path).name == "__init__.py"
        aliases = self._import_aliases(root, source, module, package)
        references: list[ReferenceRecord] = []
        for node in self._walk(root):
            if self._intersects(node, error_ranges):
                continue
            target = None
            if node.type in {"import_statement", "import_from_statement"}:
                for binding, imported, binding_kind, resolution_hint in self._import_entries(
                    node, source, module, package
                ):
                    references.append(self._reference(
                        node,
                        imported,
                        "IMPORTS",
                        file,
                        symbols,
                        owners,
                        binding_name=binding,
                        binding_kind=binding_kind,
                        target_kind_hint=(
                            "module" if node.type == "import_statement"
                            else "member"
                        ),
                        resolution_hint=resolution_hint,
                        resolution_scope="project",
                    ))
                continue
            if node.type == "class_definition":
                arguments = next((child for child in node.named_children
                                  if child.type == "argument_list"), None)
                if arguments:
                    for base in arguments.named_children:
                        if base.type in {"identifier", "attribute"}:
                            base_aliases = self._visible_aliases(
                                root, base, source, module, package, aliases
                            )
                            target = self._target_text(
                                base, source,
                                base_aliases,
                                module, symbols, owners=owners, root=root,
                                package=package,
                            )
                            references.append(self._reference(
                                base,
                                target,
                                "INHERITS",
                                file,
                                symbols,
                                owners,
                                resolution_scope="project",
                            ))
                continue
            if node.type == "call":
                function = node.child_by_field_name("function")
                if function is not None and function.type in {"identifier", "attribute"}:
                    call_aliases = self._visible_aliases(
                        root, node, source, module, package, aliases
                    )
                    target = self._target_text(
                        function,
                        source,
                        call_aliases,
                        module,
                        symbols,
                        owners,
                        node,
                        root,
                        package,
                    )
                    references.append(self._reference(
                        function,
                        target,
                        "CALLS",
                        file,
                        symbols,
                        owners,
                        resolution_scope=(
                            self._call_resolution_scope(
                                function, source, target, call_aliases, root,
                                module, package,
                            )
                        ),
                    ))
        return tuple(sorted(references, key=lambda item: (
            item.source_symbol_id or item.source_file_id, item.relation_type,
            item.source_line or -1, item.source_byte or -1,
            item.target_reference or "",
        )))

    def _visible_aliases(
        self, root, call, source, module, package, aliases
    ):
        """Return aliases visible at a call's lexical position."""
        visible = {
            name: next(
                (target for start, target in reversed(bindings)
                 if start < call.start_byte),
                None,
            )
            for name, bindings in aliases.items()
        }
        visible = {name: target for name, target in visible.items() if target is not None}
        scopes = self._enclosing_scopes(call)
        function_body_scopes = tuple(
            scope for scope in scopes
            if scope.type == "function_definition"
            and self._in_function_body(scope, call)
        )
        visible_class_scopes = self._visible_class_scopes(scopes, call)
        module_limit = (
            call.start_byte if function_body_scopes
            else scopes[0].start_byte if scopes else call.start_byte
        )
        self._apply_scope_bindings(
            root, module_limit, source, module, package, visible
        )
        for index, scope in enumerate(scopes):
            if (
                scope.type == "class_definition"
                and (
                    scope.start_byte,
                    scope.end_byte,
                ) not in visible_class_scopes
            ):
                continue
            if (scope.type == "lambda"
                    and not self._in_lambda_body(scope, call)):
                continue
            if scope.type == "function_definition":
                if not self._in_function_body(scope, call):
                    continue
                for name in self._function_local_bindings(
                    scope, source, module, package
                ):
                    visible.pop(name, None)
            parameters = scope.child_by_field_name("parameters")
            if parameters is not None:
                for name in self._parameter_names(parameters, source):
                    visible.pop(name, None)
            if scope.type in {
                "list_comprehension", "set_comprehension", "dictionary_comprehension",
                "generator_expression",
            }:
                for clause in self._comprehension_clauses(scope):
                    if not self._comprehension_binding_applies(
                        scope, clause, call
                    ):
                        continue
                    left = clause.child_by_field_name("left")
                    if left is not None:
                        for item in self._walk(left):
                            if item.type == "identifier":
                                visible.pop(self._text(item, source), None)
            limit = (
                call.start_byte if index == len(scopes) - 1
                else scopes[index + 1].start_byte
            )
            self._apply_scope_bindings(
                scope, limit, source, module, package, visible
            )
        return visible

    def _visible_class_scopes(self, scopes, node):
        visible = set()
        skip_classes = False
        comprehension_types = {
            "list_comprehension", "set_comprehension",
            "dictionary_comprehension", "generator_expression",
        }
        for scope in reversed(scopes):
            if (
                scope.type == "function_definition"
                and self._in_function_body(scope, node)
            ) or (
                scope.type == "lambda"
                and self._in_lambda_body(scope, node)
            ):
                skip_classes = True
                continue
            if scope.type in comprehension_types:
                clauses = self._comprehension_clauses(scope)
                if not (
                    clauses
                    and self._contains(
                        clauses[0].child_by_field_name("right"), node
                    )
                ):
                    skip_classes = True
                continue
            if scope.type != "class_definition" or not self._contains(
                scope.child_by_field_name("body"), node
            ):
                continue
            if not skip_classes:
                visible.add((scope.start_byte, scope.end_byte))
            skip_classes = True
        return visible

    @staticmethod
    def _in_function_body(function, node):
        body = function.child_by_field_name("body")
        return (
            body is not None
            and body.start_byte <= node.start_byte < body.end_byte
        )

    @staticmethod
    def _in_lambda_body(lambda_node, node):
        body = lambda_node.child_by_field_name("body")
        return (
            body is not None
            and body.start_byte <= node.start_byte < body.end_byte
        )

    def _parameter_names(self, parameters, source):
        names = []
        for parameter in parameters.named_children:
            name = parameter.child_by_field_name("name")
            if name is not None:
                names.extend(self._binding_names(name, source))
            elif parameter.type == "identifier":
                names.append(self._text(parameter, source))
            elif parameter.type in {"list_splat", "dictionary_splat"}:
                names.extend(
                    self._text(item, source)
                    for item in self._walk(parameter)
                    if item.type == "identifier"
                )
        return tuple(names)

    def _function_local_bindings(self, scope, source, module, package):
        """Return names local to a function for its entire compile-time scope."""
        names = set()
        for node in self._scope_nodes(scope):
            if node is scope:
                continue
            if node.type in {"import_statement", "import_from_statement"}:
                names.update(
                    name for name, _target in self._import_pairs(
                        node, source, module, package
                    )
                )
            else:
                names.update(self._bound_names(node, source))
        names.update(self._parameter_names(
            scope.child_by_field_name("parameters"), source
        ))
        names.difference_update(
            self._scope_declaration_names(scope, source, "global_statement")
        )
        names.difference_update(
            self._scope_declaration_names(scope, source, "nonlocal_statement")
        )
        return names

    def _scope_declaration_names(self, scope, source, statement_type):
        return {
            self._text(item, source)
            for node in self._scope_nodes(scope) if node.type == statement_type
            for item in self._walk(node) if item.type == "identifier"
        }

    def _apply_scope_bindings(
        self, scope, limit, source, module, package, visible
    ):
        for node in self._scope_nodes(scope):
            if node is scope or node.start_byte >= limit:
                continue
            if node.type in {"import_statement", "import_from_statement"}:
                entries = self._import_entries(
                    node, source, module, package
                )
                if self._binding_is_conditional(node, scope):
                    for name, _target, _kind, _hint in entries:
                        visible[name] = (None, False, None)
                    continue
                for name, target, kind, _hint in entries:
                    visible[name] = (
                        target,
                        node.type == "import_statement"
                        and kind == "implicit_binding"
                        and name == target.split(".")[0],
                        (
                            "module" if node.type == "import_statement"
                            else "member"
                        ),
                    )
                continue
            for name in self._bound_names(node, source):
                visible.pop(name, None)

    @staticmethod
    def _binding_is_conditional(node, scope):
        current = node.parent
        while current is not None and current is not scope:
            if current.type in {
                "if_statement", "for_statement", "while_statement",
                "try_statement", "match_statement", "case_clause",
            }:
                return True
            current = current.parent
        return False

    def _bound_names(self, node, source):
        if node.type in {"function_definition", "class_definition"}:
            name = node.child_by_field_name("name")
            return (self._text(name, source),) if name is not None else ()
        if node.type in {"assignment", "augmented_assignment", "for_statement",
                         "named_expression"}:
            left = node.child_by_field_name("left")
            if left is None and node.type == "named_expression":
                left = node.named_children[0] if node.named_children else None
            return self._binding_names(left, source)
        if node.type == "delete_statement":
            return tuple(
                name for child in node.named_children
                for name in self._binding_names(child, source)
            )
        if node.type in {"with_statement", "except_clause"}:
            return tuple(self._text(item, source) for item in self._walk(node)
                         if item.type == "identifier")
        if node.type == "case_pattern":
            return self._pattern_binding_names(node, source)
        return ()

    def _pattern_binding_names(self, node, source):
        if node.type == "identifier":
            name = self._text(node, source)
            return () if name == "_" else (name,)
        if node.type == "dotted_name":
            identifiers = [
                item for item in node.named_children
                if item.type == "identifier"
            ]
            if len(identifiers) != 1:
                return ()
            name = self._text(identifiers[0], source)
            return () if name == "_" else (name,)
        if node.type == "class_pattern":
            children = node.named_children[1:]
        elif node.type == "keyword_pattern":
            children = node.named_children[1:]
        else:
            children = node.named_children
        return tuple(
            name for child in children
            for name in self._pattern_binding_names(child, source)
        )

    def _binding_names(self, node, source):
        if node is None or node.type in {"attribute", "subscript"}:
            return ()
        if node.type == "identifier":
            return (self._text(node, source),)
        return tuple(
            name for child in node.named_children
            for name in self._binding_names(child, source)
        )

    @staticmethod
    def _enclosing_scopes(node):
        scope_types = {
            "function_definition", "class_definition", "lambda", "list_comprehension",
            "set_comprehension",
            "dictionary_comprehension", "generator_expression",
        }
        scopes = []
        current = node.parent
        while current is not None:
            if current.type in scope_types:
                scopes.append(current)
            current = current.parent
        return tuple(reversed(scopes))

    @staticmethod
    def _enclosing_function(root, node):
        enclosing = None
        for candidate in PythonAdapter._walk(root):
            if (candidate.type == "function_definition"
                    and candidate.start_byte <= node.start_byte < candidate.end_byte):
                if enclosing is None or candidate.start_byte >= enclosing.start_byte:
                    enclosing = candidate
        return enclosing

    @staticmethod
    def _enclosing_class(root, node):
        for candidate in PythonAdapter._walk(root):
            if (candidate.type == "class_definition"
                    and candidate.start_byte <= node.start_byte < candidate.end_byte):
                return candidate
        return None

    @staticmethod
    def _scope_nodes(scope):
        """Walk a lexical scope without descending into nested definitions."""
        stack = [(scope, True)]
        while stack:
            node, descend = stack.pop()
            yield node
            if not descend:
                continue
            for child in reversed(node.children):
                nested = child.type in {
                    "function_definition", "class_definition", "lambda",
                    "list_comprehension", "set_comprehension", "dictionary_comprehension",
                    "generator_expression",
                }
                stack.append((child, not nested))

    def _reference(
        self,
        node,
        target,
        relation_type,
        file,
        symbols,
        owners,
        *,
        binding_name=None,
        binding_kind=None,
        target_kind_hint=None,
        resolution_hint=None,
        resolution_scope=None,
    ):
        lexical_owner = self._lexical_owner(node, owners)
        owner = self._owner(node, symbols)
        source_symbol_id = (
            lexical_owner[2] if lexical_owner is not None
            else owner.symbol_id if owner else None
        )
        return ReferenceRecord(
            source_symbol_id=source_symbol_id,
            source_file_id=file.file_id,
            relation_type=relation_type,
            target_reference=target,
            source_line=node.start_point[0] + 1,
            source_byte=node.start_byte,
            source_module_id=(
                file.module_id if source_symbol_id is None else None
            ),
            source_end_line=node.end_point[0] + 1,
            source_end_byte=node.end_byte,
            binding_name=binding_name,
            binding_kind=binding_kind,
            binding_name_tokens_casefold=(
                token_key(binding_name) if binding_name is not None else None
            ),
            target_kind_hint=target_kind_hint,
            resolution_hint=resolution_hint,
            resolution_scope=resolution_scope,
        )

    def _has_import_evidence(self, node, source, aliases):
        raw = self._text(node, source)
        first = raw.partition(".")[0]
        binding = aliases.get(first)
        if binding is None:
            return False
        target, rooted, _target_kind = binding
        if target is None:
            return False
        if not rooted:
            return True
        return raw == target or raw.startswith(target + ".")

    def _call_resolution_scope(
        self, node, source, target, aliases, root, module, package
    ):
        raw = self._text(node, source)
        first = raw.partition(".")[0]
        binding = aliases.get(first)
        if binding is not None and binding[0] is None:
            return None
        if ("." in raw
                and self._attribute_is_rebound(root, node, source, raw)):
            return None
        if ("." not in raw
                and self._name_is_rebound(root, node, source, raw)):
            return None
        if self._has_import_evidence(node, source, aliases):
            return "project"
        if (target == raw and self._name_is_compile_time_local(
                node, source, raw, module, package)):
            return None
        if "." not in raw or target != raw:
            return "file"
        return None

    @staticmethod
    def _owner(node, symbols):
        matches = [symbol for symbol in symbols if symbol.start_byte is not None
                   and symbol.end_byte is not None
                   and symbol.start_byte <= node.start_byte < symbol.end_byte]
        return min(matches, key=lambda item: item.end_byte - item.start_byte) if matches else None

    @staticmethod
    def _lexical_owner(node, owners):
        matches = [
            owner for owner in owners
            if owner[0] <= node.start_byte < owner[1]
        ]
        return min(
            matches,
            key=lambda item: (item[1] - item[0], -item[0], item[2]),
            default=None,
        )

    def _import_aliases(self, root, source, module, package):
        aliases = {}
        for node in self._walk(root):
            if node.type not in {"import_statement", "import_from_statement"}:
                continue
            if (self._enclosing_function(root, node) is not None
                    or self._enclosing_class(root, node) is not None):
                continue
            for child, target, kind, _hint in self._import_entries(
                node, source, module, package
            ):
                rooted = (
                    node.type == "import_statement"
                    and kind == "implicit_binding"
                    and child == target.split(".")[0]
                )
                aliases.setdefault(child, []).append((
                    node.start_byte,
                    (
                        target,
                        rooted,
                        (
                            "module" if node.type == "import_statement"
                            else "member"
                        ),
                    ),
                ))
        return aliases

    def _import_pairs(self, node, source, module, package):
        return [
            (binding, target)
            for binding, target, _kind, _resolution_hint
            in self._import_entries(node, source, module, package)
        ]

    def _import_entries(self, node, source, module, package):
        if node.type == "import_statement":
            entries = []
            for item in node.named_children:
                if item.type not in {"dotted_name", "aliased_import"}:
                    continue
                name_node = (
                    item.child_by_field_name("name")
                    if item.type == "aliased_import" else item
                )
                raw = self._text(name_node, source)
                alias = (self._text(item.named_children[-1], source)
                         if item.type == "aliased_import" else raw.split(".")[0])
                kind = (
                    "explicit_alias" if item.type == "aliased_import"
                    else "implicit_binding"
                )
                entries.append((alias, raw, kind, None))
            return entries
        module_node = node.child_by_field_name("module_name")
        raw_module = self._text(module_node, source)
        base = self._relative_module(raw_module, module, package)
        entries = []
        for item in node.named_children:
            if (module_node is not None
                    and item.start_byte == module_node.start_byte
                    and item.end_byte == module_node.end_byte) or item.type == "relative_import":
                continue
            if item.type not in {"dotted_name", "aliased_import", "wildcard_import"}:
                continue
            name_node = item.child_by_field_name("name") if item.type == "aliased_import" else item
            raw = self._text(name_node, source)
            alias = (
                self._text(item.named_children[-1], source)
                if item.type == "aliased_import"
                else raw.split(".")[0]
            )
            target = (
                raw_module + "." + raw
                if base is None else base + "." + raw
            )
            kind = (
                "explicit_alias" if item.type == "aliased_import"
                else "implicit_binding"
            )
            resolution_hint = (
                "unresolved"
                if item.type == "wildcard_import" or base is None else None
            )
            entries.append((alias, target, kind, resolution_hint))
        return entries

    @staticmethod
    def _relative_module(raw, module, package):
        if not raw or not raw.startswith("."):
            return raw
        dots = len(raw) - len(raw.lstrip("."))
        tail = raw[dots:]
        parent = module.split(".") if package else module.split(".")[:-1]
        ascend = dots - 1
        if ascend >= len(parent):
            return None
        if ascend:
            parent = parent[:-ascend]
        return ".".join((*parent, tail) if tail else parent)

    def _canonical_base_references(
        self, node, source, aliases, root, module, package
    ):
        raw = self._text(node, source)
        first, dot, rest = raw.partition(".")
        binding = aliases.get(first)
        references = {raw}
        if binding is not None:
            target, rooted, _target_kind = binding
            if target is not None and not rooted:
                references = {target + (dot + rest if dot else "")}
        references.update(
            target + (dot + rest if dot else "")
            for target, rooted in self._possible_alias_targets(
                root, node, source, module, package, first
            )
            if not rooted
        )
        return references

    def _possible_alias_targets(
        self, root, node, source, module, package, binding
    ):
        targets = set()
        scopes = [
            scope for scope in reversed(self._enclosing_scopes(node))
            if self._contains(scope.child_by_field_name("body"), node)
        ]
        scopes.append(root)
        for scope in scopes:
            found, blocks_outer = self._scope_alias_targets(
                scope, node, source, module, package, binding
            )
            targets.update(found)
            if blocks_outer:
                break
        return targets

    def _scope_alias_targets(
        self, scope, node, source, module, package, binding
    ):
        targets = set()
        blocks_outer = scope.type == "module"
        candidates = sorted(
            (
                candidate for candidate in self._scope_nodes(scope)
                if candidate is not scope
                and candidate.start_byte < node.start_byte
            ),
            key=lambda candidate: candidate.start_byte,
            reverse=True,
        )
        for candidate in candidates:
            conditional = self._binding_is_conditional(candidate, scope)
            if candidate.type in {
                "import_statement", "import_from_statement",
            }:
                entries = self._import_entries(
                    candidate, source, module, package
                )
                matching = [
                    (
                        target,
                        candidate.type == "import_statement"
                        and kind == "implicit_binding"
                        and name == target.split(".")[0],
                    )
                    for name, target, kind, _hint in entries
                    if name == binding
                ]
                if matching:
                    targets.update(matching)
                    if not conditional:
                        blocks_outer = True
                        break
                continue
            if (binding in self._bound_names(candidate, source)
                    and not conditional):
                blocks_outer = True
                break
        if (scope.type == "function_definition"
                and binding in self._function_local_bindings(
                    scope, source, module, package
                )):
            blocks_outer = True
        return targets, blocks_outer

    def _target_text(
        self,
        node,
        source,
        aliases,
        module,
        symbols,
        owners=(),
        call=None,
        root=None,
        package=False,
    ):
        raw = self._text(node, source)
        first, dot, rest = raw.partition(".")
        if (dot and first not in {"self", "cls"}
                and self._attribute_is_rebound(root, call or node, source, raw)):
            return raw.rpartition(".")[2]
        if first in aliases:
            target, rooted, _target_kind = aliases[first]
            if target is None:
                return raw
            if rooted:
                return raw
            return target + (dot + rest if dot else "")
        if first in {"self", "cls"} and dot:
            receiver_target = self._receiver_target(
                root, call or node, source, first, symbols, owners
            )
            if receiver_target is not None:
                return receiver_target + "." + rest
        if dot:
            local_class = module + "." + first if module else first
            if (any(symbol.kind == "class" and symbol.qualified_name == local_class
                    and symbol.start_byte is not None
                    and symbol.start_byte < (call or node).start_byte
                    for symbol in symbols)
                    and not self._name_is_shadowed(
                        root, call or node, source, first
                    )):
                return local_class + "." + rest
        if not dot:
            compile_time_local = self._name_is_compile_time_local(
                call or node, source, raw, module, package
            )
            local = module + "." + raw if module else raw
            owner = self._owner(call or node, symbols)
            lexical_owner = self._lexical_owner(call or node, owners)
            owner_qualified_name = (
                lexical_owner[3] if lexical_owner is not None
                else owner.qualified_name if owner is not None else None
            )
            requires_source_order = (
                owner is None
                or any(self._in_class_header(scope, call or node)
                       for scope in self._enclosing_scopes(call or node))
            )
            if (not compile_time_local
                    and not self._name_is_rebound(
                        root, call or node, source, raw
                    )
                    and any(symbol.qualified_name == local
                            and (not requires_source_order
                                 or (symbol.start_byte is not None
                                     and symbol.start_byte < (call or node).start_byte))
                            for symbol in symbols)):
                return local
            if owner_qualified_name is not None:
                nested = owner_qualified_name + "." + raw
                if any(symbol.qualified_name == nested
                       and symbol.start_byte is not None
                       and symbol.start_byte < (call or node).start_byte
                       for symbol in symbols):
                    return nested
            return raw
        return raw

    def _name_is_compile_time_local(
        self, node, source, name, module, package
    ):
        if "." in name:
            return False
        for scope in reversed(self._enclosing_scopes(node)):
            if scope.type == "lambda":
                if not self._in_lambda_body(scope, node):
                    continue
                parameters = scope.child_by_field_name("parameters")
                if parameters is not None and name in self._parameter_names(
                    parameters, source
                ):
                    return True
                continue
            if scope.type in {
                "list_comprehension", "set_comprehension",
                "dictionary_comprehension", "generator_expression",
            }:
                if any(
                    name in self._binding_names(
                        clause.child_by_field_name("left"), source
                    )
                    for clause in self._comprehension_clauses(scope)
                    if self._comprehension_binding_applies(
                        scope, clause, node
                    )
                ):
                    return True
                continue
            if scope.type != "function_definition":
                continue
            if not self._in_function_body(scope, node):
                continue
            if name in self._scope_declaration_names(
                scope, source, "global_statement"
            ):
                return False
            if name in self._scope_declaration_names(
                scope, source, "nonlocal_statement"
            ):
                continue
            if name in self._function_local_bindings(
                scope, source, module, package
            ):
                return True
        return False

    @staticmethod
    def _contains(container, node):
        return (
            container is not None
            and container.start_byte <= node.start_byte
            and node.end_byte <= container.end_byte
        )

    def _comprehension_binding_applies(self, scope, clause, node):
        clauses = sorted(
            self._comprehension_clauses(scope),
            key=lambda item: item.start_byte,
        )
        if self._contains(clause.child_by_field_name("right"), node):
            return False
        in_iterable = any(
            self._contains(item.child_by_field_name("right"), node)
            for item in clauses
        )
        if clauses and not in_iterable and node.start_byte < clauses[0].start_byte:
            return True
        return clause.start_byte < node.start_byte

    def _comprehension_clauses(self, scope):
        comprehension_types = {
            "list_comprehension", "set_comprehension",
            "dictionary_comprehension", "generator_expression",
        }
        clauses = []
        for clause in self._walk(scope):
            if clause.type != "for_in_clause":
                continue
            owner = clause.parent
            while owner is not None and owner.type not in comprehension_types:
                owner = owner.parent
            if (owner is not None
                    and owner.start_byte == scope.start_byte
                    and owner.end_byte == scope.end_byte):
                clauses.append(clause)
        return tuple(clauses)

    def _attribute_is_rebound(self, root, call, source, target):
        if root is None:
            return False
        for scope in (*self._enclosing_scopes(call), root):
            for node in self._scope_nodes(scope):
                if node is scope or node.start_byte >= call.start_byte:
                    continue
                if node.type not in {
                    "assignment", "augmented_assignment", "delete_statement",
                }:
                    continue
                left = node.child_by_field_name("left")
                candidates = (left,) if left is not None else node.named_children
                if any(
                    candidate.type == "attribute"
                    and (
                        self._text(candidate, source) == target
                        or target.startswith(
                            self._text(candidate, source) + "."
                        )
                    )
                    for candidate in candidates
                ):
                    return True
        return False

    def _name_is_shadowed(self, root, call, source, name):
        if root is None:
            return True
        for scope in self._enclosing_scopes(call):
            if scope.type not in {"function_definition", "lambda"}:
                continue
            parameters = scope.child_by_field_name("parameters")
            if parameters is not None and any(
                self._text(node, source) == name for node in self._walk(parameters)
                if node.type == "identifier"
            ):
                return True
            if any(name in self._bound_names(node, source)
                   for node in self._scope_nodes(scope)
                   if node is not scope and node.start_byte < call.start_byte):
                return True
        module_bindings = [
            node for node in self._scope_nodes(root)
            if node is not root and node.start_byte < call.start_byte
            and name in self._bound_names(node, source)
        ]
        return bool(module_bindings and module_bindings[-1].type != "class_definition")

    def _name_is_rebound(self, root, call, source, name):
        if root is None:
            return False
        for scope in (*self._enclosing_scopes(call), root):
            for node in self._scope_nodes(scope):
                if (node is scope or node.start_byte >= call.start_byte
                        or node.type in {"function_definition", "class_definition"}):
                    continue
                if name in self._bound_names(node, source):
                    return True
        return False

    def _receiver_target(
        self, root, call, source, receiver, symbols, owners
    ):
        if root is None:
            return None
        function = self._enclosing_function(root, call)
        if function is None:
            return None
        class_node = self._method_class(function)
        if class_node is None or self._receiver_is_rebound(function, call, source, receiver):
            return None
        member = self._text(call.child_by_field_name("function"), source).partition(".")[2]
        if self._class_member_is_mutated(root, class_node, source, member):
            return None
        if self._known_subclass_override(root, class_node, source, member):
            return None
        parameters = function.child_by_field_name("parameters")
        if parameters is None or not parameters.named_children:
            return None
        first_parameter = parameters.named_children[0]
        first_name = next((self._text(node, source) for node in self._walk(first_parameter)
                           if node.type == "identifier"), None)
        decorator_names = self._decorator_names(function, source)
        if receiver == "self":
            if first_name != "self" or {"staticmethod", "classmethod"} & decorator_names:
                return None
        elif receiver == "cls":
            if first_name != "cls" or "classmethod" not in decorator_names:
                return None
        else:
            return None
        owner = self._owner(call, symbols)
        lexical_owner = self._lexical_owner(call, owners)
        owner_kind = (
            lexical_owner[4] if lexical_owner is not None
            else owner.kind if owner is not None else None
        )
        owner_qualified_name = (
            lexical_owner[3] if lexical_owner is not None
            else owner.qualified_name if owner is not None else ""
        )
        if owner_kind != "method" or "." not in owner_qualified_name:
            return None
        return owner_qualified_name.rsplit(".", 1)[0]

    def _known_subclass_override(
        self, root, class_node, source, member
    ):
        name = class_node.child_by_field_name("name")
        if name is None:
            return True
        reachable = {self._text(name, source)}
        classes = [
            node for node in self._walk(root)
            if node.type == "class_definition" and node is not class_node
        ]
        pending = classes
        while pending:
            remaining = []
            changed = False
            for candidate in pending:
                bases = next((
                    child for child in candidate.named_children
                    if child.type == "argument_list"
                ), None)
                base_names = {
                    self._text(base, source)
                    for base in bases.named_children
                    if base.type in {"identifier", "attribute"}
                } if bases is not None else set()
                if not (base_names & reachable):
                    remaining.append(candidate)
                    continue
                changed = True
                candidate_name = candidate.child_by_field_name("name")
                if candidate_name is not None:
                    reachable.add(self._text(candidate_name, source))
                if self._class_defines_member(candidate, source, member):
                    return True
            if not changed:
                break
            pending = remaining
        return False

    def _class_defines_member(self, class_node, source, member):
        body = class_node.child_by_field_name("body")
        if body is None:
            return False
        for child in body.named_children:
            if (child.type in {"import_statement", "import_from_statement"}
                    and any(
                        name == member
                        for name, _target, _kind, _hint
                        in self._import_entries(child, source, "", False)
                    )):
                return True
            direct_nodes = (child, *child.named_children)
            if any(
                member in self._bound_names(node, source)
                for node in direct_nodes
            ):
                return True
            declaration = child
            if child.type == "decorated_definition":
                declaration = next((
                    item for item in reversed(child.named_children)
                    if item.type == "function_definition"
                ), child)
            if declaration.type != "function_definition":
                continue
            name = declaration.child_by_field_name("name")
            if name is not None and self._text(name, source) == member:
                return True
        return False

    def _project_class_lookup_mutations(
        self, root, source, module, package
    ):
        evidence = set()
        member = "__getattribute__"
        for node in self._walk(root):
            receiver = self._supported_class_mutation_receiver(
                root, node, source, member
            )
            if receiver is None:
                continue
            binding_name = self._text(receiver, source)
            bindings, _other, _unbound = self._lexical_binding_options(
                root, node, source, binding_name
            )
            for binding in bindings:
                if binding.type != "import_from_statement":
                    continue
                for name, target, _kind, hint in self._import_entries(
                    binding, source, module, package
                ):
                    if (
                        name == binding_name
                        and hint is None
                        and self._is_qualified_identifier(target)
                    ):
                        evidence.add((
                            self.language,
                            "class_member_mutation",
                            "member",
                            target,
                            member,
                        ))
        return tuple(sorted(evidence))

    def _class_member_is_mutated(self, root, class_node, source, member):
        return member in self._class_mutated_member_names(
            root, class_node, source, {member}
        )

    def _class_mutated_member_names(
        self, root, class_node, source, allowed_members=frozenset()
    ):
        name = class_node.child_by_field_name("name")
        if name is None:
            return set()
        class_name = self._text(name, source)
        members = set()
        for node in self._walk(root):
            if node.start_byte < class_node.end_byte:
                continue
            for member in allowed_members:
                receiver = self._supported_class_mutation_receiver(
                    root, node, source, member
                )
                if (
                    receiver is not None
                    and self._text(receiver, source) == class_name
                    and self._class_binding_matches(
                        root, node, source, class_name, class_node
                    )
                ):
                    members.add(member)
        return members

    def _supported_class_mutation_receiver(
        self, root, node, source, member
    ):
        if node.type in {"assignment", "augmented_assignment"}:
            left = node.child_by_field_name("left")
            if left is None or left.type != "attribute":
                return None
            receiver = left.child_by_field_name("object")
            attribute = left.child_by_field_name("attribute")
            if self._is_exact_identifier(attribute, source, member):
                return receiver if receiver.type == "identifier" else None
            return None
        if node.type != "call":
            return None
        function = node.child_by_field_name("function")
        helper = ({
            "setattr": "setattr",
            "type.__setattr__": "type",
        }.get(self._text(function, source)) if function is not None else None)
        if helper is None or not self._name_may_be_builtin(
            root, node, source, helper
        ):
            return None
        arguments = node.child_by_field_name("arguments")
        if arguments is None or len(arguments.named_children) < 2:
            return None
        receiver, name = arguments.named_children[:2]
        if (
            receiver.type == "identifier"
            and self._is_exact_string_literal(name, source, member)
        ):
            return receiver
        return None

    def _class_binding_matches(
        self, root, node, source, name, class_node
    ):
        bindings, _other, _unbound = self._lexical_binding_options(
            root, node, source, name
        )
        return any(self._same_node(binding, class_node) for binding in bindings)

    def _name_may_be_builtin(self, root, node, source, name):
        _bindings, _other, unbound = self._lexical_binding_options(
            root, node, source, name
        )
        return unbound

    def _lexical_binding_options(self, root, node, source, name):
        """Return possible source-order bindings for one lexical name."""
        bindings = []
        other = False
        skip_classes = False
        module_only = False
        scopes = [*reversed(self._enclosing_scopes(node)), root]
        for scope in scopes:
            if scope.type == "module":
                found, retains_outer = self._scope_binding_options(
                    scope, node, source, name
                )
                bindings.extend(found)
                return self._unique_nodes(bindings), other, retains_outer
            if module_only:
                continue
            if scope.type == "function_definition":
                if not self._in_function_body(scope, node):
                    continue
                if name in self._scope_declaration_names(
                    scope, source, "global_statement"
                ):
                    module_only = True
                    skip_classes = True
                    continue
                if name in self._scope_declaration_names(
                    scope, source, "nonlocal_statement"
                ):
                    skip_classes = True
                    continue
                if name in self._function_local_bindings(
                    scope, source, "", False
                ):
                    found, retains_initial = self._scope_binding_options(
                        scope, node, source, name
                    )
                    bindings.extend(found)
                    return (
                        self._unique_nodes(bindings),
                        other or retains_initial,
                        False,
                    )
                skip_classes = True
                continue
            if scope.type == "lambda":
                if not self._in_lambda_body(scope, node):
                    continue
                if name in self._lambda_local_bindings(scope, source):
                    found, retains_initial = self._scope_binding_options(
                        scope, node, source, name
                    )
                    bindings.extend(found)
                    return (
                        self._unique_nodes(bindings),
                        other or retains_initial,
                        False,
                    )
                skip_classes = True
                continue
            if scope.type in {
                "list_comprehension", "set_comprehension",
                "dictionary_comprehension", "generator_expression",
            }:
                clauses = self._comprehension_clauses(scope)
                if any(
                    name in self._binding_names(
                        clause.child_by_field_name("left"), source
                    )
                    for clause in clauses
                    if self._comprehension_binding_applies(scope, clause, node)
                ):
                    return self._unique_nodes(bindings), True, False
                if not (
                    clauses
                    and self._contains(
                        clauses[0].child_by_field_name("right"), node
                    )
                ):
                    skip_classes = True
                continue
            if scope.type != "class_definition" or skip_classes:
                continue
            body = scope.child_by_field_name("body")
            if not self._contains(body, node):
                continue
            found, retains_outer = self._scope_binding_options(
                scope, node, source, name
            )
            bindings.extend(found)
            if not retains_outer:
                return self._unique_nodes(bindings), other, False
            skip_classes = True
        return self._unique_nodes(bindings), other, True

    def _scope_binding_options(self, scope, node, source, name):
        bindings = []
        retains_initial = True
        candidates = sorted(
            (
                candidate for candidate in self._scope_nodes(scope)
                if candidate is not scope
                and candidate.start_byte < node.start_byte
                and self._node_binds_name(candidate, source, name)
            ),
            key=lambda candidate: (candidate.start_byte, candidate.end_byte),
        )
        for candidate in candidates:
            if self._binding_is_conditional(candidate, scope):
                bindings.append(candidate)
                continue
            bindings = [candidate]
            retains_initial = False
        return self._unique_nodes(bindings), retains_initial

    def _node_binds_name(self, node, source, name):
        if node.type in {"import_statement", "import_from_statement"}:
            return any(
                binding == name for binding, _target, _kind, _hint
                in self._import_entries(node, source, "", False)
            )
        return name in self._bound_names(node, source)

    def _lambda_local_bindings(self, scope, source):
        names = set()
        parameters = scope.child_by_field_name("parameters")
        if parameters is not None:
            names.update(self._parameter_names(parameters, source))
        for node in self._scope_nodes(scope):
            if node is not scope:
                names.update(self._bound_names(node, source))
        return names

    @staticmethod
    def _same_node(left, right):
        return (
            left.type == right.type
            and left.start_byte == right.start_byte
            and left.end_byte == right.end_byte
        )

    @staticmethod
    def _unique_nodes(nodes):
        unique = []
        seen = set()
        for node in nodes:
            identity = (node.type, node.start_byte, node.end_byte)
            if identity not in seen:
                seen.add(identity)
                unique.append(node)
        return tuple(unique)

    @staticmethod
    def _is_exact_string_literal(node, source, value):
        if node.type != "string":
            return False
        encoded = value.encode("utf-8")
        literal = source[node.start_byte:node.end_byte]
        return literal in {b"'" + encoded + b"'", b'"' + encoded + b'"'}

    @staticmethod
    def _is_exact_identifier(node, source, value):
        return (
            node is not None
            and node.type == "identifier"
            and source[node.start_byte:node.end_byte] == value.encode("utf-8")
        )

    @staticmethod
    def _is_qualified_identifier(value):
        return all(
            part.isidentifier() and not keyword.iskeyword(part)
            for part in value.split(".")
        )

    @staticmethod
    def _method_class(function):
        current = function.parent
        while current is not None:
            if current.type == "class_definition":
                return current
            if current.type == "function_definition":
                return None
            current = current.parent
        return None

    def _receiver_is_rebound(self, function, call, source, receiver):
        inside_method = False
        for scope in self._enclosing_scopes(call):
            if (scope.start_byte == function.start_byte
                    and scope.end_byte == function.end_byte):
                inside_method = True
                continue
            if not inside_method:
                continue
            parameters = scope.child_by_field_name("parameters")
            if parameters is not None and any(
                self._text(node, source) == receiver for node in self._walk(parameters)
                if node.type == "identifier"
            ):
                return True
            if scope.type in {
                "list_comprehension", "set_comprehension", "dictionary_comprehension",
                "generator_expression",
            } and any(
                self._text(item, source) == receiver
                for node in self._walk(scope) if node.type == "for_in_clause"
                for left in (node.child_by_field_name("left"),) if left is not None
                for item in self._walk(left) if item.type == "identifier"
            ):
                return True
        for node in self._scope_nodes(function):
            if node.start_byte >= call.start_byte:
                continue
            if node.type not in {
                "assignment", "augmented_assignment", "for_statement", "with_statement",
                "except_clause",
            }:
                continue
            left = node.child_by_field_name("left")
            if left is not None and self._text(left, source).startswith(receiver + "."):
                return True
            if receiver in self._bound_names(node, source):
                return True
        return False

    def _decorator_names(self, function, source):
        wrapper = function.parent
        if wrapper is None or wrapper.type != "decorated_definition":
            return set()
        return {self._text(node, source).lstrip("@").split("(", 1)[0]
                for node in wrapper.named_children if node.type == "decorator"}

    @staticmethod
    def _in_class_header(class_node, node):
        arguments = next((child for child in class_node.named_children
                          if child.type == "argument_list"), None)
        return (arguments is not None
                and arguments.start_byte <= node.start_byte < arguments.end_byte)

    @staticmethod
    def _text(node, source):
        return source[node.start_byte:node.end_byte].decode("utf-8", "replace") if node else ""

    @staticmethod
    def _walk(node):
        stack = [node]
        while stack:
            current = stack.pop()
            yield current
            stack.extend(reversed(current.children))

    def _collect(
        self,
        node,
        source,
        file_id,
        module_key,
        module,
        package,
        import_aliases,
        parents,
        error_ranges,
        symbols,
        owners,
    ):
        stack = [(node, parents)]
        while stack:
            current, current_parents = stack.pop()
            for child in reversed(current.named_children):
                entity = child
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
                    entity, error_ranges
                )
                stable_symbol_id = None
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
                        else self._signature(
                            declaration, source, is_async, kind
                        )
                    )
                    own = source[entity.start_byte:entity.end_byte]
                    qualified_name = ".".join(qualified_parts)
                    stable_symbol_id = symbol_id(
                        self.language,
                        self.prefix,
                        self.repository_id,
                        module_key,
                        qualified_name,
                        signature or "",
                    )
                    metadata_fields = {
                        "language": "python",
                        "module": module,
                    }
                    owner_symbol_id = (
                        current_parents[-1][2] if current_parents else None
                    )
                    if owner_symbol_id is not None:
                        metadata_fields["owner_symbol_id"] = owner_symbol_id
                    if is_class:
                        arguments = next((
                            child for child in declaration.named_children
                            if child.type == "argument_list"
                        ), None)
                        base_references = sorted({
                            reference
                            for base in arguments.named_children
                            if base.type in {"identifier", "attribute"}
                            for reference in self._canonical_base_references(
                                base,
                                source,
                                self._visible_aliases(
                                    node, base, source, module, package,
                                    import_aliases,
                                ),
                                node,
                                module,
                                package,
                            )
                        }) if arguments is not None else []
                        if base_references:
                            metadata_fields["base_references"] = base_references
                        member_bindings = self._class_member_binding_names(
                            declaration, source, module, package
                        )
                        member_bindings.update(
                            self._class_mutated_member_names(
                                node, declaration, source,
                                {"__getattribute__"},
                            )
                        )
                        if member_bindings:
                            metadata_fields["member_bindings"] = sorted(
                                member_bindings
                            )
                    metadata = json.dumps(
                        metadata_fields,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    owners.append((
                        entity.start_byte,
                        entity.end_byte,
                        stable_symbol_id,
                        qualified_name,
                        kind,
                    ))
                    symbols.append(
                        SymbolRecord(
                            symbol_id=stable_symbol_id,
                            file_id=file_id,
                            kind=kind,
                            qualified_name=qualified_name,
                            local_name=name,
                            name_tokens_casefold=token_key(qualified_name, name),
                            start_line=entity.start_point[0] + 1,
                            end_line=entity.end_point[0] + 1,
                            start_byte=entity.start_byte,
                            end_byte=entity.end_byte,
                            signature=signature,
                            signature_casefold=compact_casefold(signature),
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
                        (
                            name,
                            declaration.type == "class_definition",
                            stable_symbol_id,
                        ),
                    )
                    stack.append((declaration, next_parents))

    def _class_member_binding_names(
        self, class_node, source, module, package
    ):
        body = class_node.child_by_field_name("body")
        if body is None:
            return set()
        names = set()
        for node in self._scope_nodes(class_node):
            if node is class_node:
                continue
            if node.type in {"import_statement", "import_from_statement"}:
                names.update(
                    name for name, _target, _kind, _hint
                    in self._import_entries(node, source, module, package)
                )
            names.update(self._bound_names(node, source))
        return names

    @staticmethod
    def _intersects(node, ranges):
        return any(
            (node.start_byte <= start <= node.end_byte)
            if start == end
            else node.start_byte < end and start < node.end_byte
            for start, end in ranges
        )

    @staticmethod
    def _signature(node, source: bytes, is_async: bool, kind: str) -> str:
        parameters = node.child_by_field_name("parameters")
        result = node.child_by_field_name("return_type")
        value = _compact(source[parameters.start_byte:parameters.end_byte]) if parameters else "()"
        if is_async:
            value = "async" + value
        if result is not None:
            value += "->" + _compact(source[result.start_byte:result.end_byte])
        return kind + "|" + value
