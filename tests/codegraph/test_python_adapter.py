"""Contract tests for static Python declaration extraction."""
from __future__ import annotations

import builtins
from contextlib import closing
import hashlib
import importlib
import inspect
import sqlite3
import sys
import types
from pathlib import Path

import pytest

from iwiki_mcp.codegraph.languages.python import PythonAdapter, derive_module_names
from iwiki_mcp.codegraph.models import file_id, module_id, symbol_id, token_key


FIXTURES = Path(__file__).parents[1] / "fixtures" / "codegraph"


def _by_name(parsed):
    return {symbol.qualified_name: symbol for symbol in parsed.symbols}


def test_module_names_require_deterministic_package_evidence():
    root = FIXTURES / "python_duplicate_modules"
    paths = tuple(sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
    ))

    names = derive_module_names(paths + ("src/service.py", "__init__.py"))

    assert names["root_a/pkg/__init__.py"] == "pkg"
    assert names["root_a/pkg/service.py"] == "pkg.service"
    assert names["root_b/pkg/__init__.py"] == "pkg"
    assert names["root_b/pkg/service.py"] == "pkg.service"
    assert names["namespace/pkg/service.py"] is None
    assert names["src/service.py"] is None
    assert names["__init__.py"] is None


def test_direct_adapter_emits_final_occurrence_aware_file_module_and_symbol_ids():
    path = "pkg/__init__.py"
    source = b"async def work(value: int = 1) -> str:\n    return str(value)\n"
    adapter = PythonAdapter(
        "domain",
        {path: "pkg"},
        parser_version="tree-sitter-python:test",
    )

    parsed = adapter.parse_file(source, path)

    expected_file_id = file_id("python", "py", "domain", path)
    expected_module_id = module_id("python", "py", "domain", path, "pkg")
    expected_signature = "async_function|async(value:int=1)->str"
    assert parsed.file.file_id == expected_file_id
    assert parsed.file.repository_id == "domain"
    assert parsed.file.parser_version == "tree-sitter-python:test"
    assert parsed.file.module_key == path
    assert parsed.file.module_id == expected_module_id
    assert parsed.file.module_qualified_name == "pkg"
    assert parsed.file.module_local_name == "pkg"
    assert parsed.file.module_name_tokens_casefold == token_key("pkg", "pkg")
    assert parsed.symbols[0].file_id == expected_file_id
    assert parsed.symbols[0].signature == expected_signature
    assert parsed.symbols[0].symbol_id == symbol_id(
        "python", "py", "domain", path, "pkg.work", expected_signature
    )
    assert not parsed.file.file_id.startswith("parse:")
    assert not parsed.symbols[0].symbol_id.startswith("parse:")


@pytest.mark.parametrize(
    ("source", "end_line"),
    [
        (b"", 1),
        (b"value = 1", 1),
        (b"value = 1\n", 2),
        (b"value = 1\r\n", 2),
        (b"value = b'\\xff'\n", 2),
        (b"# \xff\n", 2),
    ],
)
def test_whole_file_line_range_counts_line_feed_boundaries(source, end_line):
    path = "unknown.py"

    parsed = PythonAdapter("domain", {path: None}).parse_file(source, path)

    assert (parsed.file.start_line, parsed.file.end_line) == (1, end_line)
    assert (parsed.file.start_byte, parsed.file.end_byte) == (0, len(source))


def test_signatures_preserve_literal_semantics_and_encode_kind():
    path = "pkg/signatures.py"
    first = (
        b'def spaced(value: str = "a b") -> str:\n    return value\n\n'
        b'def compact(value: str = "ab") -> str:\n    return value\n\n'
        b'async def asynchronous(value: str = "a b") -> str:\n    return value\n'
    )
    formatted = (
        b'def spaced( value : str = "a b" ) -> str:\n return "body changed"\n\n'
        b'def compact(value:str="ab")->str:\n return value\n\n'
        b'async def asynchronous(value:str="a b")->str:\n return value\n'
    )
    adapter = PythonAdapter("domain", {path: "pkg.signatures"})

    left = _by_name(adapter.parse_file(first, path))
    right = _by_name(adapter.parse_file(formatted, path))

    assert left["pkg.signatures.spaced"].signature == (
        'function|(value:str="a b")->str'
    )
    assert left["pkg.signatures.compact"].signature == (
        'function|(value:str="ab")->str'
    )
    assert left["pkg.signatures.asynchronous"].signature == (
        'async_function|async(value:str="a b")->str'
    )
    assert left["pkg.signatures.spaced"].symbol_id != (
        left["pkg.signatures.compact"].symbol_id
    )
    assert left["pkg.signatures.spaced"].symbol_id == (
        right["pkg.signatures.spaced"].symbol_id
    )
    assert left["pkg.signatures.asynchronous"].symbol_id == (
        right["pkg.signatures.asynchronous"].symbol_id
    )

    synchronous = adapter.parse_file(
        b'def same(value: str = "a b") -> str:\n    return value\n',
        path,
    ).symbols[0]
    asynchronous = adapter.parse_file(
        b'async def same(value: str = "a b") -> str:\n    return value\n',
        path,
    ).symbols[0]
    assert synchronous.qualified_name == asynchronous.qualified_name
    assert synchronous.signature != asynchronous.signature
    assert synchronous.symbol_id != asynchronous.symbol_id


def test_duplicate_symbol_identities_collapse_to_last_declaration(seed_runtime):
    path = "src/pkg/duplicates.py"
    source = (
        b"@overload\n"
        b"def convert(value: int) -> str: ...\n\n"
        b"def convert(value: int) -> str:\n    return str(value)\n\n"
        b"def repeated():\n    return 'first'\n\n"
        b"def repeated():\n    return 'last'\n\n"
        b"class Service:\n    first = 1\n\n"
        b"class Service:\n    last = 2\n\n"
        b"def variant(value: int):\n    return value\n\n"
        b"def variant(value: str):\n    return value\n"
    )
    adapter = PythonAdapter("domain", {path: "pkg.duplicates"})

    parsed = adapter.parse_file(source, path)
    by_name = {}
    for symbol in parsed.symbols:
        by_name.setdefault(symbol.qualified_name, []).append(symbol)

    assert len(by_name["pkg.duplicates.convert"]) == 1
    assert len(by_name["pkg.duplicates.repeated"]) == 1
    assert len(by_name["pkg.duplicates.Service"]) == 1
    assert len(by_name["pkg.duplicates.variant"]) == 2
    convert = by_name["pkg.duplicates.convert"][0]
    implementation = (
        b"def convert(value: int) -> str:\n    return str(value)"
    )
    assert convert.start_byte == source.rindex(b"def convert")
    assert convert.end_byte == convert.start_byte + len(implementation)
    assert convert.content_hash == hashlib.sha256(implementation).hexdigest()
    assert parsed.warnings.count("duplicate_symbol_identity") == 3

    seed_runtime.project_file(path).write_bytes(source)
    built = seed_runtime.index(force=True)
    assert built["state"] == "ready"
    assert built["warnings"].count("duplicate_symbol_identity") == 1
    with closing(seed_runtime.runtime._store.open_existing()) as connection:
        rows = dict(connection.execute(
            "SELECT qualified_name, COUNT(*) FROM symbols "
            "WHERE qualified_name LIKE 'pkg.duplicates.%' "
            "GROUP BY qualified_name"
        ))
    assert rows == {
        "pkg.duplicates.Service": 1,
        "pkg.duplicates.convert": 1,
        "pkg.duplicates.repeated": 1,
        "pkg.duplicates.variant": 2,
    }


def test_decorated_symbol_range_and_hash_include_all_decorators():
    path = "pkg/decorated.py"
    first = (
        b"@first\n@second(value=1)\n"
        b"async def work(item: int) -> str:\n    return str(item)\n"
    )
    edited = first.replace(b"@first", b"@replacement")
    adapter = PythonAdapter("domain", {path: "pkg.decorated"})

    original = adapter.parse_file(first, path).symbols[0]
    changed = adapter.parse_file(edited, path).symbols[0]

    assert (original.start_line, original.start_byte) == (1, 0)
    assert original.end_line == 4
    assert original.end_byte == len(first.rstrip(b"\n"))
    assert original.content_hash == hashlib.sha256(
        first.rstrip(b"\n")
    ).hexdigest()
    assert changed.start_byte == 0
    assert changed.end_byte == len(edited.rstrip(b"\n"))
    assert changed.content_hash != original.content_hash
    assert changed.symbol_id == original.symbol_id


def test_module_names_reject_invalid_or_keyword_segments_without_collision():
    paths = (
        "pkg/__init__.py",
        "pkg/foo.bar.py",
        "pkg/foo/__init__.py",
        "pkg/foo/bar.py",
        "pkg/class.py",
        "pkg/def/__init__.py",
        "pkg/def/value.py",
        "my-pkg/__init__.py",
        "my-pkg/service.py",
        "пакет/__init__.py",
        "пакет/модуль.py",
    )

    names = derive_module_names(paths)

    assert names["pkg/foo.bar.py"] is None
    assert names["pkg/foo/bar.py"] == "pkg.foo.bar"
    assert names["pkg/class.py"] is None
    assert names["pkg/def/__init__.py"] is None
    assert names["pkg/def/value.py"] is None
    assert names["my-pkg/__init__.py"] is None
    assert names["my-pkg/service.py"] is None
    assert names["пакет/__init__.py"] == "пакет"
    assert names["пакет/модуль.py"] == "пакет.модуль"
    invalid = PythonAdapter("domain", paths).parse_file(
        b"def value():\n    pass\n",
        "pkg/foo.bar.py",
    )
    assert invalid.file.module_id is None
    assert invalid.warnings == ("module_name_unavailable",)


def test_schema_v2_file_module_and_symbol_extraction(seed_runtime):
    source = (
        b"async def work(value: int = 1) -> str:\n"
        b"    result = str(value)\n"
        b"    return result\n"
    )
    empty_path = FIXTURES / "python_basic" / "empty.py"
    package = seed_runtime.project_file("pkg")
    package.mkdir()
    package.joinpath("__init__.py").write_bytes(source)
    package.joinpath("empty.py").write_bytes(empty_path.read_bytes())

    result = seed_runtime.index(force=True)

    assert result["state"] == "ready"
    expected_file_id = file_id("python", "py", "project", "pkg/__init__.py")
    expected_module_id = module_id(
        "python", "py", "project", "pkg/__init__.py", "pkg"
    )
    with closing(sqlite3.connect(seed_runtime.paths.database)) as connection:
        file_row = connection.execute(
            "SELECT file_id, repository_id, path, path_casefold, "
            "file_local_name, file_name_tokens_casefold, language, "
            "size_bytes, start_line, end_line, start_byte, end_byte, "
            "module_key, module_id, module_qualified_name, "
            "module_local_name, module_name_tokens_casefold "
            "FROM files WHERE path = 'pkg/__init__.py'"
        ).fetchone()
        symbol_row = connection.execute(
            "SELECT kind, qualified_name, local_name, name_tokens_casefold, "
            "start_line, end_line, start_byte, end_byte, signature, "
            "signature_casefold, metadata_json "
            "FROM symbols WHERE qualified_name = 'pkg.work'"
        ).fetchone()
        empty_row = connection.execute(
            "SELECT module_qualified_name FROM files WHERE path = 'pkg/empty.py'"
        ).fetchone()

    assert file_row == (
        expected_file_id,
        "project",
        "pkg/__init__.py",
        None,
        "__init__.py",
        token_key("__init__.py"),
        "python",
        len(source),
        1,
        4,
        0,
        len(source),
        "pkg/__init__.py",
        expected_module_id,
        "pkg",
        "pkg",
        token_key("pkg", "pkg"),
    )
    assert symbol_row == (
        "async_function",
        "pkg.work",
        "work",
        token_key("pkg.work", "work"),
        1,
        3,
        0,
        len(source) - 1,
        "async_function|async(value:int=1)->str",
        None,
        '{"language":"python","module":"pkg"}',
    )
    assert empty_row == ("pkg.empty",)


def test_unprovable_module_keeps_file_and_symbols_with_warning():
    path = "namespace/pkg/service.py"
    source = (
        FIXTURES / "python_duplicate_modules" / path
    ).read_bytes()

    parsed = PythonAdapter("domain", {path: None}).parse_file(source, path)

    assert parsed.file.module_key == path
    assert parsed.file.module_id is None
    assert parsed.file.module_qualified_name is None
    assert parsed.file.module_local_name is None
    assert parsed.file.module_name_tokens_casefold is None
    assert [symbol.qualified_name for symbol in parsed.symbols] == ["serve"]
    assert parsed.warnings == ("module_name_unavailable",)


@pytest.mark.parametrize("error_type", [TypeError, ValueError])
def test_unprovable_module_parser_failure_keeps_both_warning_codes(error_type):
    class FailingParser:
        def parse(self, _source):
            raise error_type("source text must not escape")

    path = "namespace/pkg/service.py"
    adapter = PythonAdapter("domain", {path: None})
    adapter._parser = FailingParser()

    parsed = adapter.parse_file(b"def serve():\n    pass\n", path)

    assert parsed.file.module_key == path
    assert parsed.file.module_id is None
    assert parsed.symbols == ()
    assert parsed.references == ()
    assert parsed.warnings == ("module_name_unavailable", "parse_error")
    assert all("source text" not in warning for warning in parsed.warnings)


def test_extracts_service_method_vector_with_normalized_signature():
    source = (FIXTURES / "python_basic" / "sample.py").read_bytes()

    parsed = PythonAdapter(
        "domain", {"src/service.py": "service"}
    ).parse_file(source, "src/service.py")
    symbols = _by_name(parsed)

    assert list(symbols) == ["service.Service", "service.Service.run"]
    assert symbols["service.Service"].kind == "class"
    method = symbols["service.Service.run"]
    assert method.kind == "method"
    assert (method.start_line, method.end_line) == (2, 3)
    assert method.signature == "method|(self,value:int=1)->str"
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

    parsed = PythonAdapter(
        "domain", {"pkg/__init__.py": "pkg"}
    ).parse_file(source, "pkg/__init__.py")
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
        "method|async(self,first,/,typed:list[str]=[1,2],*args:str,flag:bool=True,"
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

    adapter = PythonAdapter("domain", {"src/service.py": "service"})
    left = adapter.parse_file(first, "src/service.py")
    right = adapter.parse_file(second, "src/service.py")

    assert left.symbols[0].signature == right.symbols[0].signature == (
        "function|(self,value:int=1)->str"
    )
    assert "return" not in left.symbols[0].metadata_json
    assert left.file.content_hash == hashlib.sha256(first).hexdigest()


def test_syntax_errors_warn_and_exclude_intersecting_declarations_only():
    source = (FIXTURES / "python_syntax_errors" / "broken.py").read_bytes()

    parsed = PythonAdapter(
        "domain", {"src/broken.py": "broken"}
    ).parse_file(source, "src/broken.py")

    assert parsed.warnings == ("parse_error",)
    assert [symbol.qualified_name for symbol in parsed.symbols] == [
        "broken.valid_before",
        "broken.valid_after",
    ]


def test_malformed_declaration_does_not_block_valid_declaration_after_it():
    parsed = PythonAdapter("domain", {"src/broken.py": "broken"}).parse_file(
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

    parsed = PythonAdapter(
        "domain", {"src/nested.py": "nested"}
    ).parse_file(source, "src/nested.py")

    assert parsed.warnings == ("parse_error",)
    assert [symbol.qualified_name for symbol in parsed.symbols] == ["nested.C.good"]


def test_preserves_interior_src_path_segment_for_file_and_module():
    parsed = PythonAdapter(
        "domain", {"pkg/src/nested.py": "pkg.src.nested"}
    ).parse_file(
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

    adapter = PythonAdapter("domain", {"src/deep.py": "deep"})
    parsed = adapter.parse_file(source, "src/deep.py")

    assert parsed == adapter.parse_file(source, "src/deep.py")
    assert [symbol.qualified_name for symbol in parsed.symbols] == ["deep.valid"]


@pytest.mark.parametrize(
    "path",
    ["../outside/item.py", r"C:\\private\\repo\\x.py", r"\\\\host\\share\\x.py"],
)
def test_rejects_unsafe_source_path(path):
    with pytest.raises(ValueError, match="^invalid source path$"):
        PythonAdapter("domain", {path: None}).parse_file(
            b"def safe():\n    pass\n", path
        )


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
    adapter = module.PythonAdapter("domain", {"src/safe.py": "safe"})
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

    parsed = module.PythonAdapter(
        "domain", {"src/safe.py": "safe"}
    ).parse_file(b"def safe():\n    pass\n", "src/safe.py")

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
        module.PythonAdapter(
            "domain", {"src/safe.py": "safe"}
        ).parse_file(b"def safe():\n    pass\n", "src/safe.py")


def test_reparse_is_deterministic_and_never_uses_compile_eval_or_exec(monkeypatch):
    source = b"def safe(value: int = 1):\n    return value\n"

    def forbidden(*args, **kwargs):
        raise AssertionError("Python execution is forbidden during static parsing")

    monkeypatch.setattr(builtins, "compile", forbidden)
    monkeypatch.setattr(builtins, "eval", forbidden)
    monkeypatch.setattr(builtins, "exec", forbidden)
    adapter = PythonAdapter("domain", {"src/safe.py": "safe"})
    assert adapter.parse_file(source, "src/safe.py") == adapter.parse_file(source, "src/safe.py")


def test_protocol_defers_resolution_types_without_importing_resolver():
    from iwiki_mcp.codegraph.languages.base import LanguageAdapter

    assert getattr(LanguageAdapter, "__annotations__", {})


def test_core_language_boundary_has_no_python_adapter_or_grammar_rules():
    from iwiki_mcp.codegraph import indexer, models
    from iwiki_mcp.codegraph.languages import base

    for module in (base, indexer, models):
        source = inspect.getsource(module)
        assert "PythonAdapter" not in source
        assert "derive_module_names" not in source
        assert "tree_sitter_python" not in source
