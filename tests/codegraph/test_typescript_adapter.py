"""Contract tests for static TypeScript/TSX declaration extraction."""
from __future__ import annotations

import pytest

from iwiki_mcp.codegraph.languages.typescript import TypeScriptAdapter
from iwiki_mcp.codegraph.models import file_id, token_key


def test_adapter_identity():
    assert TypeScriptAdapter.language == "typescript"
    assert TypeScriptAdapter.prefix == "ts"
    assert TypeScriptAdapter.extensions == (".ts", ".tsx")


def test_empty_file_produces_valid_file_record():
    adapter = TypeScriptAdapter("domain", ("a.ts",), parser_version="test")

    parsed = adapter.parse_file(b"", "a.ts")

    expected_file_id = file_id("typescript", "ts", "domain", "a.ts")
    assert parsed.file.file_id == expected_file_id
    assert parsed.file.repository_id == "domain"
    assert parsed.file.language == "typescript"
    assert parsed.file.path == "a.ts"
    assert parsed.file.start_line == 1
    assert parsed.file.end_line == 1
    assert parsed.file.module_id is None  # no import/export -> not an ES module
    assert parsed.symbols == ()
    assert parsed.references == ()


def test_tsx_extension_uses_tsx_grammar():
    adapter = TypeScriptAdapter("domain", ("a.tsx",), parser_version="test")

    parsed = adapter.parse_file(b"", "a.tsx")

    assert parsed.file.language == "typescript"


def test_source_must_be_bytes():
    adapter = TypeScriptAdapter("domain", (), parser_version="test")
    with pytest.raises(TypeError):
        adapter.parse_file("not bytes", "a.ts")


def test_function_declaration_extracted():
    source = b"export function greet(name: string): string {\n  return name;\n}\n"
    adapter = TypeScriptAdapter("domain", ("a.ts",), parser_version="test")

    parsed = adapter.parse_file(source, "a.ts")

    by_name = {symbol.qualified_name: symbol for symbol in parsed.symbols}
    assert "a.greet" in by_name
    symbol = by_name["a.greet"]
    assert symbol.kind == "function"
    assert symbol.local_name == "greet"
    assert symbol.visibility == "public"


def test_class_with_method_extracted():
    source = (
        b"export class Animal {\n"
        b"  speak(sound: string): void {}\n"
        b"}\n"
    )
    adapter = TypeScriptAdapter("domain", ("a.ts",), parser_version="test")

    parsed = adapter.parse_file(source, "a.ts")

    kinds = {symbol.qualified_name: symbol.kind for symbol in parsed.symbols}
    assert kinds["a.Animal"] == "class"
    assert kinds["a.Animal.speak"] == "method"


def test_interface_type_alias_enum_extracted():
    source = (
        b"interface Named { name: string }\n"
        b"type Alias = string | number;\n"
        b"enum Color { Red, Green, Blue }\n"
    )
    adapter = TypeScriptAdapter("domain", ("a.ts",), parser_version="test")

    parsed = adapter.parse_file(source, "a.ts")

    kinds = {symbol.qualified_name: symbol.kind for symbol in parsed.symbols}
    assert kinds["a.Named"] == "interface"
    assert kinds["a.Alias"] == "type_alias"
    assert kinds["a.Color"] == "enum"


def test_arrow_function_const_extracted():
    source = b"export const add = (a: number, b: number): number => a + b;\n"
    adapter = TypeScriptAdapter("domain", ("a.ts",), parser_version="test")

    parsed = adapter.parse_file(source, "a.ts")

    by_name = {symbol.qualified_name: symbol for symbol in parsed.symbols}
    assert "a.add" in by_name
    assert by_name["a.add"].kind == "function"


def test_import_statement_produces_reference():
    source = b"import { foo } from \"./foo\";\nexport function use() { return foo; }\n"
    adapter = TypeScriptAdapter("domain", ("a.ts",), parser_version="test")

    parsed = adapter.parse_file(source, "a.ts")

    assert len(parsed.references) == 1
    reference = parsed.references[0]
    assert reference.relation_type == "IMPORTS"
    assert reference.target_reference == "./foo"
    assert reference.source_file_id == parsed.file.file_id
    assert reference.binding_name == "foo"
    assert reference.binding_kind == "implicit_binding"
    assert reference.binding_name_tokens_casefold == token_key("foo")


def test_named_import_with_alias_produces_explicit_alias_binding():
    source = b"import { foo, bar as baz } from \"./m\";\n"
    adapter = TypeScriptAdapter("domain", ("a.ts",), parser_version="test")

    parsed = adapter.parse_file(source, "a.ts")

    imports = [r for r in parsed.references if r.relation_type == "IMPORTS"]
    bindings = {(r.binding_name, r.binding_kind) for r in imports}
    assert bindings == {
        ("foo", "implicit_binding"),
        ("baz", "explicit_alias"),
    }
    assert all(r.target_reference == "./m" for r in imports)


def test_default_import_produces_implicit_binding():
    source = b"import Default from \"./m\";\n"
    adapter = TypeScriptAdapter("domain", ("a.ts",), parser_version="test")

    parsed = adapter.parse_file(source, "a.ts")

    imports = [r for r in parsed.references if r.relation_type == "IMPORTS"]
    assert len(imports) == 1
    assert imports[0].binding_name == "Default"
    assert imports[0].binding_kind == "implicit_binding"


def test_namespace_import_produces_explicit_alias_binding():
    source = b"import * as ns from \"./m\";\n"
    adapter = TypeScriptAdapter("domain", ("a.ts",), parser_version="test")

    parsed = adapter.parse_file(source, "a.ts")

    imports = [r for r in parsed.references if r.relation_type == "IMPORTS"]
    assert len(imports) == 1
    assert imports[0].binding_name == "ns"
    assert imports[0].binding_kind == "explicit_alias"


def test_combined_default_and_named_import_produces_two_bindings():
    source = b"import Default, { foo } from \"./m\";\n"
    adapter = TypeScriptAdapter("domain", ("a.ts",), parser_version="test")

    parsed = adapter.parse_file(source, "a.ts")

    imports = [r for r in parsed.references if r.relation_type == "IMPORTS"]
    bindings = {(r.binding_name, r.binding_kind) for r in imports}
    assert bindings == {
        ("Default", "implicit_binding"),
        ("foo", "implicit_binding"),
    }


def test_side_effect_only_import_produces_no_references():
    source = b"import \"./m\";\n"
    adapter = TypeScriptAdapter("domain", ("a.ts",), parser_version="test")

    parsed = adapter.parse_file(source, "a.ts")

    assert parsed.references == ()


def test_resolve_references_produces_declares_and_import_relations():
    from iwiki_mcp.codegraph.resolver import SymbolIndex

    source = b"import { foo } from \"./foo\";\n"
    adapter = TypeScriptAdapter("domain", ("a.ts",), parser_version="test")
    parsed = adapter.parse_file(source, "a.ts")
    index = SymbolIndex.from_parsed_files((parsed,))

    result = adapter.resolve_references(parsed, index)

    assert any(rel.relation_type == "IMPORTS" for rel in result.relations)


def test_class_extends_produces_inherits_reference():
    source = (
        b"class Base {}\n"
        b"class Derived extends Base {}\n"
    )
    adapter = TypeScriptAdapter("domain", ("a.ts",), parser_version="test")

    parsed = adapter.parse_file(source, "a.ts")

    inherits = [r for r in parsed.references if r.relation_type == "INHERITS"]
    assert len(inherits) == 1
    assert inherits[0].target_reference == "a.Base"


def test_interface_extends_and_class_implements_produce_inherits_references():
    source = (
        b"interface Base { }\n"
        b"interface Derived extends Base { }\n"
        b"class Impl implements Derived { }\n"
    )
    adapter = TypeScriptAdapter("domain", ("a.ts",), parser_version="test")

    parsed = adapter.parse_file(source, "a.ts")

    inherits_targets = {
        r.target_reference for r in parsed.references
        if r.relation_type == "INHERITS"
    }
    assert "a.Base" in inherits_targets
    assert "a.Derived" in inherits_targets


def test_type_boost_disabled_by_default_no_subprocess_call(monkeypatch):
    called = []
    monkeypatch.setattr(
        "iwiki_mcp.codegraph.languages.typescript._run_tsc_boost",
        lambda *a, **k: called.append(1) or None,
    )
    adapter = TypeScriptAdapter("domain", ("a.ts",), parser_version="test")
    parsed = adapter.parse_file(b"const x = 1;\n", "a.ts")
    from iwiki_mcp.codegraph.resolver import SymbolIndex

    adapter.resolve_references(parsed, SymbolIndex.from_parsed_files((parsed,)))

    assert called == []


def test_type_boost_failure_degrades_silently_and_warns(monkeypatch):
    monkeypatch.setattr(
        "iwiki_mcp.codegraph.languages.typescript._run_tsc_boost",
        lambda *a, **k: None,  # None == "boost unavailable" per its own contract
    )
    adapter = TypeScriptAdapter(
        "domain", ("a.ts",), parser_version="test", type_boost_enabled=True,
    )
    parsed = adapter.parse_file(b"const x = 1;\n", "a.ts")
    from iwiki_mcp.codegraph.resolver import SymbolIndex

    result = adapter.resolve_references(parsed, SymbolIndex.from_parsed_files((parsed,)))

    assert result is not None  # did not raise
    assert result.warnings == ("typescript_boost_unavailable",)


def test_type_boost_success_emits_no_warning(monkeypatch):
    monkeypatch.setattr(
        "iwiki_mcp.codegraph.languages.typescript._run_tsc_boost",
        lambda *a, **k: {},  # any non-None return == boost succeeded
    )
    adapter = TypeScriptAdapter(
        "domain", ("a.ts",), parser_version="test", type_boost_enabled=True,
    )
    parsed = adapter.parse_file(b"const x = 1;\n", "a.ts")
    from iwiki_mcp.codegraph.resolver import SymbolIndex

    result = adapter.resolve_references(parsed, SymbolIndex.from_parsed_files((parsed,)))

    assert result.warnings == ()
