"""Contract tests for static Python declaration extraction."""
from __future__ import annotations

import builtins
import hashlib
import importlib
import sys
import types
from pathlib import Path

import pytest

from iwiki_mcp.codegraph.languages.python import PythonAdapter


FIXTURES = Path(__file__).parents[1] / "fixtures" / "codegraph"


def _by_name(parsed):
    return {symbol.qualified_name: symbol for symbol in parsed.symbols}


def test_extracts_service_method_vector_with_normalized_signature():
    source = (FIXTURES / "python_basic" / "sample.py").read_bytes()

    parsed = PythonAdapter().parse_file(source, "src/service.py")
    symbols = _by_name(parsed)

    assert list(symbols) == ["service.Service", "service.Service.run"]
    assert symbols["service.Service"].kind == "class"
    method = symbols["service.Service.run"]
    assert method.kind == "method"
    assert (method.start_line, method.end_line) == (2, 3)
    assert method.signature == "(self,value:int=1)->str"
    assert method.start_byte == source.index(b"def run")
    assert method.end_byte == len(source.rstrip(b"\n"))


def test_extracts_nested_async_and_parameter_forms_without_absolute_path_leakage():
    source = (
        b"@decorate(value = 1)\nclass Outer:\n    class Inner:\n"
        b"        async def run(self, first, /, typed: list[str] = [1, 2], "
        b"*args: str, flag: bool = True, **kwargs: object) -> dict[str, int]:\n"
        b"            return {}\n\ndef top(value: int = 1):\n"
        b"    def nested(item: str = \"x\"):\n        return item\n    return nested\n"
    )

    parsed = PythonAdapter().parse_file(source, "pkg/__init__.py")
    symbols = _by_name(parsed)

    assert list(symbols) == [
        "pkg.Outer",
        "pkg.Outer.Inner",
        "pkg.Outer.Inner.run",
        "pkg.top",
        "pkg.top.nested",
    ]
    assert symbols["pkg.Outer.Inner.run"].kind == "method"
    assert symbols["pkg.Outer.Inner.run"].signature == (
        "async(self,first,/,typed:list[str]=[1,2],*args:str,flag:bool=True,"
        "**kwargs:object)->dict[str,int]"
    )
    assert symbols["pkg.top.nested"].kind == "function"
    assert all("/private" not in symbol.qualified_name for symbol in parsed.symbols)
    assert all(
        symbol.start_byte is not None and symbol.end_byte is not None
        for symbol in parsed.symbols
    )
    assert parsed.file.path == "pkg/__init__.py"


def test_signature_is_stable_across_whitespace_and_uses_safe_metadata():
    first = b"def run( self , value : int = 1 ) -> str:\n    return 'a'\n"
    second = b"def run(self,value:int=1)->str:\n return 'a'\n"

    left = PythonAdapter().parse_file(first, "src/service.py")
    right = PythonAdapter().parse_file(second, "src/service.py")

    assert left.symbols[0].signature == right.symbols[0].signature == "(self,value:int=1)->str"
    assert "return" not in left.symbols[0].metadata_json
    assert left.file.content_hash == hashlib.sha256(first).hexdigest()


def test_syntax_errors_warn_and_exclude_intersecting_declarations_only():
    source = (FIXTURES / "python_syntax_errors" / "broken.py").read_bytes()

    parsed = PythonAdapter().parse_file(source, "src/broken.py")

    assert parsed.warnings == ("parse_error",)
    assert [symbol.qualified_name for symbol in parsed.symbols] == [
        "broken.valid_before",
        "broken.valid_after",
    ]


def test_malformed_declaration_does_not_block_valid_declaration_after_it():
    parsed = PythonAdapter().parse_file(
        b"def bad(:\n    pass\n\ndef valid():\n    return None\n",
        "src/broken.py",
    )

    assert parsed.warnings == ("parse_error",)
    assert [symbol.qualified_name for symbol in parsed.symbols] == ["broken.valid"]


def test_valid_child_survives_syntax_error_in_enclosing_class():
    source = (
        b"class C:\n"
        b"    def good(self):\n"
        b"        return None\n\n"
        b"    def broken(:\n"
        b"        pass\n"
    )

    parsed = PythonAdapter().parse_file(source, "src/nested.py")

    assert parsed.warnings == ("parse_error",)
    assert [symbol.qualified_name for symbol in parsed.symbols] == ["nested.C.good"]


def test_preserves_interior_src_path_segment_for_file_and_module():
    parsed = PythonAdapter().parse_file(
        b"def nested():\n    pass\n", "pkg/src/nested.py"
    )

    assert parsed.file.path == "pkg/src/nested.py"
    assert [symbol.qualified_name for symbol in parsed.symbols] == ["pkg.src.nested.nested"]


def test_deep_tree_parses_iteratively_without_recursion_error():
    source = (
        b"def valid():\n    value = "
        + (b"(" * 1100)
        + b"1"
        + (b")" * 1100)
        + b"\n"
    )

    parsed = PythonAdapter().parse_file(source, "src/deep.py")

    assert parsed == PythonAdapter().parse_file(source, "src/deep.py")
    assert [symbol.qualified_name for symbol in parsed.symbols] == ["deep.valid"]


@pytest.mark.parametrize(
    "path",
    ["../outside/item.py", r"C:\\private\\repo\\x.py", r"\\\\host\\share\\x.py"],
)
def test_rejects_unsafe_source_path(path):
    with pytest.raises(ValueError, match="^invalid source path$"):
        PythonAdapter().parse_file(b"def safe():\n    pass\n", path)


def test_parser_package_is_lazy_and_static_parse_never_executes_python(monkeypatch):
    sys.modules.pop("tree_sitter_language_pack", None)
    module = importlib.reload(sys.modules["iwiki_mcp.codegraph.languages.python"])
    assert "tree_sitter_language_pack" not in sys.modules

    calls = []
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "tree_sitter_language_pack":
            calls.append(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    adapter = module.PythonAdapter()
    assert calls == []
    adapter.parse_file(b"def safe():\n    pass\n", "src/safe.py")
    assert calls == ["tree_sitter_language_pack"]


def test_falls_back_to_direct_tree_sitter_grammar_when_pack_download_fails(monkeypatch):
    module = importlib.reload(sys.modules["iwiki_mcp.codegraph.languages.python"])
    from tree_sitter_language_pack import DownloadError

    def unavailable_parser(_language):
        raise DownloadError("grammar download unavailable")

    monkeypatch.setitem(
        sys.modules,
        "tree_sitter_language_pack",
        types.SimpleNamespace(
            DownloadError=DownloadError,
            get_parser=unavailable_parser,
        ),
    )

    parsed = module.PythonAdapter().parse_file(b"def safe():\n    pass\n", "src/safe.py")

    assert [symbol.qualified_name for symbol in parsed.symbols] == ["safe.safe"]
    assert parsed.warnings == ()


def test_unexpected_pack_error_is_not_treated_as_offline_fallback(monkeypatch):
    module = importlib.reload(sys.modules["iwiki_mcp.codegraph.languages.python"])
    from tree_sitter_language_pack import DownloadError

    def unexpected_failure(_language):
        raise RuntimeError("adapter defect")

    monkeypatch.setitem(
        sys.modules,
        "tree_sitter_language_pack",
        types.SimpleNamespace(
            DownloadError=DownloadError,
            get_parser=unexpected_failure,
        ),
    )

    with pytest.raises(RuntimeError, match="adapter defect"):
        module.PythonAdapter().parse_file(b"def safe():\n    pass\n", "src/safe.py")


def test_reparse_is_deterministic_and_never_uses_compile_eval_or_exec(monkeypatch):
    source = b"def safe(value: int = 1):\n    return value\n"

    def forbidden(*args, **kwargs):
        raise AssertionError("Python execution is forbidden during static parsing")

    monkeypatch.setattr(builtins, "compile", forbidden)
    monkeypatch.setattr(builtins, "eval", forbidden)
    monkeypatch.setattr(builtins, "exec", forbidden)
    adapter = PythonAdapter()
    assert adapter.parse_file(source, "src/safe.py") == adapter.parse_file(source, "src/safe.py")


def test_protocol_defers_resolution_types_without_importing_resolver():
    from iwiki_mcp.codegraph.languages.base import LanguageAdapter

    assert getattr(LanguageAdapter, "__annotations__", {})
