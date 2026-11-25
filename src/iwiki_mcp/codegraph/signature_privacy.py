"""Project stored Python declarations into value-free hosted navigation text."""
from __future__ import annotations

import ast
import warnings

from tree_sitter import Language, Node, Parser
import tree_sitter_python


_PYTHON = Language(tree_sitter_python.language())
_HIDDEN = "…"
_INVALID = "invalid stored Python signature"
_PREFIXES = ("function|", "async_function|async", "method|async", "method|", "")

# Only these constructors have known type positions. An unknown generic can
# interpret a quoted identifier as application data instead of a type.
_BUILTINS = {"list", "set", "frozenset", "type", "dict", "tuple"}
_ONE_ARG = {
    "List", "Set", "FrozenSet", "Type", "Optional", "Sequence", "MutableSequence",
    "Iterable", "Iterator", "AsyncIterable", "AsyncIterator", "Awaitable",
    "Collection", "ClassVar", "Final", "Required", "NotRequired", "TypeGuard",
    "Deque", "AbstractSet", "MutableSet",
}
_TWO_ARGS = {"Dict", "Mapping", "MutableMapping", "AsyncGenerator"}
_ABC = {
    "Sequence", "MutableSequence", "Iterable", "Iterator", "AsyncIterable",
    "AsyncIterator", "Awaitable", "Collection", "Set", "MutableSet", "Mapping",
    "MutableMapping", "AsyncGenerator", "Callable",
}


def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted(node.value)
        if prefix:
            return prefix + "." + node.attr
    return ""


def _read_annotation(text: str) -> str:
    try:
        # The host grammar can reject newer type syntax. Keep the parameter
        # and hide that annotation. Parser warnings can contain source text.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            expression = ast.parse(text, mode="eval").body
        return _annotation(expression)
    except (SyntaxError, ValueError, RecursionError):
        return _HIDDEN


def _annotation(node: ast.expr) -> str:
    name = _dotted(node)
    if name:
        return name
    if isinstance(node, ast.Constant):
        if node.value is None:
            return "None"
        if isinstance(node.value, str):
            return _read_annotation(node.value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _annotation(node.left) + "|" + _annotation(node.right)
    if not isinstance(node, ast.Subscript):
        return _HIDDEN

    name = _dotted(node.value)
    namespace, _, short = name.rpartition(".")
    if namespace == "collections.abc" and short not in _ABC:
        return _HIDDEN
    if namespace == "builtins" and short not in _BUILTINS:
        return _HIDDEN
    if namespace not in {"", "builtins", "typing", "typing_extensions", "collections.abc"}:
        return _HIDDEN
    if namespace in {"typing", "typing_extensions"} and short in _BUILTINS:
        return _HIDDEN
    args = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
    if short == "Literal":
        return "Literal[…]"
    if short == "Annotated" and len(args) >= 2:
        return _annotation(args[0])
    if short == "Callable" and len(args) == 2:
        params, result = args
        if isinstance(params, ast.List):
            sig = "[" + ",".join(_annotation(param) for param in params.elts) + "]"
        elif isinstance(params, ast.Constant) and params.value is Ellipsis:
            sig = "..."
        else:
            sig = _annotation(params)
        return f"{name}[{sig},{_annotation(result)}]"
    if short in {"tuple", "Tuple"} and len(args) == 2:
        if isinstance(args[1], ast.Constant) and args[1].value is Ellipsis:
            return f"{name}[{_annotation(args[0])},...]"
    if (
        (short in _ONE_ARG | {"list", "set", "frozenset", "type"} and len(args) == 1)
        or (short in _TWO_ARGS | {"dict"} and len(args) == 2)
        or (short in {"Tuple", "tuple", "Union"} and args)
    ):
        return name + "[" + ",".join(_annotation(arg) for arg in args) + "]"
    return _HIDDEN


def _parameter(node: Node) -> str:
    if node.type == "positional_separator":
        return "/"
    if node.type == "keyword_separator":
        return "*"
    name = node
    if node.type in {"default_parameter", "typed_default_parameter", "typed_parameter"}:
        name = node.named_children[0]
    stars = ""
    if name.type in {"list_splat_pattern", "dictionary_splat_pattern"}:
        stars = "*" if name.type == "list_splat_pattern" else "**"
        name = name.named_children[0]
    if name.type != "identifier":
        raise ValueError(_INVALID)
    display = stars + name.text.decode()
    annotation = node.child_by_field_name("type")
    if annotation is not None:
        display += ":" + _read_annotation(annotation.text.decode())
    if node.child_by_field_name("value") is not None:
        display += "=..."
    return display


def project_call_reference(reference: str) -> str:
    """Hide literal-bearing expressions retained as unresolved Python call labels."""
    if all(part.isidentifier() and part not in {"None", "True", "False"}
           for part in reference.split(".")):
        return reference
    root = Parser(_PYTHON).parse(reference.encode()).root_node
    if root.has_error or root.named_child_count != 1:
        return _HIDDEN
    nodes = [root]
    while nodes:
        node = nodes.pop()
        if node.type in {"string", "integer", "float", "true", "false", "none", "ellipsis",
                         "comment"}:
            return _HIDDEN
        nodes.extend(node.named_children)
    return reference


def project_python_signature(signature: str) -> str:
    """Return the hosted display, or fail without including the source in diagnostics."""
    for prefix in _PREFIXES:
        if signature.startswith(prefix + "("):
            break
    else:
        raise ValueError(_INVALID)
    try:
        declaration = b"def f" + signature[len(prefix):].encode()
    except UnicodeEncodeError:
        raise ValueError(_INVALID) from None
    # Parse the envelope with the adapter's grammar, including defaults that
    # use syntax newer than this interpreter. Never parse a default as a type.
    root = Parser(_PYTHON).parse(declaration + b": pass").root_node
    if root.has_error or root.named_child_count != 1:
        raise ValueError(_INVALID)
    function = root.named_children[0]
    params = function.child_by_field_name("parameters")
    result = function.child_by_field_name("return_type")
    if function.type != "function_definition" or params is None:
        raise ValueError(_INVALID)
    if (result or params).end_byte != len(declaration):
        raise ValueError(_INVALID)
    display = prefix + "(" + ",".join(_parameter(p) for p in params.named_children) + ")"
    if result is not None:
        display += "->" + _read_annotation(result.text.decode())
    return display
