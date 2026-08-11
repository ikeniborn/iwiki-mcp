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

from ..models import (
    FileRecord,
    ParsedFile,
    ReferenceRecord,
    ResolutionResult,
    SymbolRecord,
    compact_casefold,
    token_key,
)
from ..resolver import resolve_references


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
    prefix = "py"
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
            path_casefold=compact_casefold(relative_path),
            file_local_name=PurePosixPath(relative_path).name,
            file_name_tokens_casefold=token_key(PurePosixPath(relative_path).name),
            language=self.language,
            content_hash=content_hash,
            parser_version="tree-sitter-python",
            size_bytes=len(source),
            start_line=1,
            end_line=1,
            start_byte=0,
            end_byte=len(source),
            module_key=relative_path,
            module_id=None,
            module_qualified_name=None,
            module_local_name=None,
            module_name_tokens_casefold=None,
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
        references = self._references(root, source, file, module, symbols, error_ranges)
        return ParsedFile(
            file=file,
            symbols=tuple(symbols),
            references=references,
            warnings=warnings,
        )

    def resolve_references(
        self, parsed: ParsedFile, project_index: Any
    ) -> ResolutionResult:
        return ResolutionResult(
            relations=resolve_references(
                self.language,
                self.prefix,
                parsed.file.repository_id,
                parsed.references,
                project_index,
            ),
            warnings=(),
        )

    def _references(self, root, source, file, module, symbols, error_ranges):
        aliases = self._import_aliases(root, source, module)
        references: list[ReferenceRecord] = []
        for node in self._walk(root):
            if self._intersects(node, error_ranges):
                continue
            target = None
            if node.type in {"import_statement", "import_from_statement"}:
                for imported in self._import_targets(node, source, module):
                    references.append(self._reference(
                        node, imported, "IMPORTS", file.file_id, symbols))
                continue
            if node.type == "class_definition":
                arguments = next((child for child in node.named_children
                                  if child.type == "argument_list"), None)
                if arguments:
                    for base in arguments.named_children:
                        if base.type in {"identifier", "attribute"}:
                            target = self._target_text(
                                base, source,
                                self._visible_aliases(root, base, source, module, aliases),
                                module, symbols, root=root,
                            )
                            references.append(self._reference(
                                base, target, "INHERITS", file.file_id, symbols))
                continue
            if node.type == "call":
                function = node.child_by_field_name("function")
                if function is not None and function.type in {"identifier", "attribute"}:
                    call_aliases = self._visible_aliases(
                        root, node, source, module, aliases
                    )
                    target = self._target_text(
                        function, source, call_aliases, module, symbols, node, root
                    )
                    references.append(self._reference(
                        function, target, "CALLS", file.file_id, symbols))
        return tuple(sorted(references, key=lambda item: (
            item.source_symbol_id or item.source_file_id, item.relation_type,
            item.source_line or -1, item.source_byte or -1,
            item.target_reference or "",
        )))

    def _visible_aliases(self, root, call, source, module, aliases):
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
        module_limit = scopes[0].start_byte if scopes else call.start_byte
        self._apply_scope_bindings(root, module_limit, source, module, visible)
        for index, scope in enumerate(scopes):
            if scope.type == "function_definition":
                for name in self._function_local_bindings(scope, source, module):
                    visible.pop(name, None)
            parameters = scope.child_by_field_name("parameters")
            if parameters is not None:
                for node in self._walk(parameters):
                    if node.type == "identifier":
                        visible.pop(self._text(node, source), None)
            if scope.type in {
                "list_comprehension", "set_comprehension", "dictionary_comprehension",
                "generator_expression",
            }:
                for node in self._walk(scope):
                    if node.type == "for_in_clause":
                        left = node.child_by_field_name("left")
                        if left is not None:
                            for item in self._walk(left):
                                if item.type == "identifier":
                                    visible.pop(self._text(item, source), None)
            limit = (
                call.start_byte if index == len(scopes) - 1
                else scopes[index + 1].start_byte
            )
            self._apply_scope_bindings(scope, limit, source, module, visible)
        return visible

    def _function_local_bindings(self, scope, source, module):
        """Return names local to a function for its entire compile-time scope."""
        names = set()
        for node in self._scope_nodes(scope):
            if node is scope:
                continue
            if node.type in {"import_statement", "import_from_statement"}:
                names.update(name for name, _target in self._import_pairs(node, source, module))
            else:
                names.update(self._bound_names(node, source))
        return names

    def _apply_scope_bindings(self, scope, limit, source, module, visible):
        for node in self._scope_nodes(scope):
            if node is scope or node.start_byte >= limit:
                continue
            if node.type in {"import_statement", "import_from_statement"}:
                for name, target in self._import_pairs(node, source, module):
                    visible[name] = (
                        name if node.type == "import_statement"
                        and name == target.split(".")[0] else target
                    )
                continue
            for name in self._bound_names(node, source):
                visible.pop(name, None)

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
        return ()

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

    def _reference(self, node, target, relation_type, file_id, symbols):
        owner = self._owner(node, symbols)
        return ReferenceRecord(owner.symbol_id if owner else None, file_id, relation_type,
                               target, node.start_point[0] + 1, node.start_byte)

    @staticmethod
    def _owner(node, symbols):
        matches = [symbol for symbol in symbols if symbol.start_byte is not None
                   and symbol.end_byte is not None
                   and symbol.start_byte <= node.start_byte < symbol.end_byte]
        return min(matches, key=lambda item: item.end_byte - item.start_byte) if matches else None

    def _import_aliases(self, root, source, module):
        aliases = {}
        for node in self._walk(root):
            if node.type not in {"import_statement", "import_from_statement"}:
                continue
            if (self._enclosing_function(root, node) is not None
                    or self._enclosing_class(root, node) is not None):
                continue
            for child, target in self._import_pairs(node, source, module):
                aliases.setdefault(child, []).append((
                    node.start_byte,
                    child if node.type == "import_statement" and child == target.split(".")[0]
                    else target,
                ))
        return aliases

    def _import_targets(self, node, source, module):
        return [target for _local, target in self._import_pairs(node, source, module)]

    def _import_pairs(self, node, source, module):
        if node.type == "import_statement":
            pairs = []
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
                pairs.append((alias, raw))
            return pairs
        module_node = node.child_by_field_name("module_name")
        base = self._relative_module(self._text(module_node, source), module)
        pairs = []
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
            target = base + "." + raw
            pairs.append((alias, target))
        return pairs

    @staticmethod
    def _relative_module(raw, module):
        if not raw or not raw.startswith("."):
            return raw
        dots = len(raw) - len(raw.lstrip("."))
        tail = raw[dots:]
        parent = module.split(".")[:-dots]
        return ".".join((*parent, tail) if tail else parent)

    def _target_text(self, node, source, aliases, module, symbols, call=None, root=None):
        raw = self._text(node, source)
        first, dot, rest = raw.partition(".")
        if (dot and first not in {"self", "cls"}
                and self._attribute_is_rebound(root, call or node, source, raw)):
            return raw.rpartition(".")[2]
        if first in aliases:
            return aliases[first] + (dot + rest if dot else "")
        if first in {"self", "cls"} and dot:
            receiver_target = self._receiver_target(
                root, call or node, source, first, symbols
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
            local = module + "." + raw if module else raw
            owner = self._owner(call or node, symbols)
            requires_source_order = (
                owner is None
                or any(self._in_class_header(scope, call or node)
                       for scope in self._enclosing_scopes(call or node))
            )
            if (not self._name_is_rebound(root, call or node, source, raw)
                    and any(symbol.qualified_name == local
                            and (not requires_source_order
                                 or (symbol.start_byte is not None
                                     and symbol.start_byte < (call or node).start_byte))
                            for symbol in symbols)):
                return local
            if owner is not None:
                nested = owner.qualified_name + "." + raw
                if any(symbol.qualified_name == nested
                       and symbol.start_byte is not None
                       and symbol.start_byte < (call or node).start_byte
                       for symbol in symbols):
                    return nested
            return raw
        return raw

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
                if any(candidate.type == "attribute"
                       and self._text(candidate, source) == target
                       for candidate in candidates):
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

    def _receiver_target(self, root, call, source, receiver, symbols):
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
        if owner is None or owner.kind != "method" or "." not in owner.qualified_name:
            return None
        return owner.qualified_name.rsplit(".", 1)[0]

    def _class_member_is_mutated(self, root, class_node, source, member):
        name = class_node.child_by_field_name("name")
        if name is None:
            return True
        class_name = self._text(name, source)
        target = class_name + "." + member
        return any(
            node.type in {"assignment", "augmented_assignment"}
            and (left := node.child_by_field_name("left")) is not None
            and self._text(left, source) == target
            for node in self._walk(root)
        ) or any(
            self._is_class_member_monkeypatch(node, source, class_name, member)
            for node in self._walk(root) if node.type == "call"
        )

    def _is_class_member_monkeypatch(self, call, source, class_name, member):
        function = call.child_by_field_name("function")
        if function is None or self._text(function, source) not in {
            "setattr", "type.__setattr__",
        }:
            return False
        arguments = call.child_by_field_name("arguments")
        if arguments is None or len(arguments.named_children) < 2:
            return False
        receiver, name = arguments.named_children[:2]
        return (
            receiver.type == "identifier"
            and self._text(receiver, source) == class_name
            and name.type == "string"
            and self._text(name, source)[1:-1] == member
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
                    qualified_name = ".".join(qualified_parts)
                    symbols.append(
                        SymbolRecord(
                            symbol_id=(
                                f"parse:{hashlib.sha256(identity.encode()).hexdigest()}"
                            ),
                            file_id=file_id,
                            kind=kind,
                            qualified_name=qualified_name,
                            local_name=name,
                            name_tokens_casefold=token_key(qualified_name, name),
                            start_line=declaration.start_point[0] + 1,
                            end_line=declaration.end_point[0] + 1,
                            start_byte=declaration.start_byte,
                            end_byte=declaration.end_byte,
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
