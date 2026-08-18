"""Contract tests for static TypeScript/TSX declaration extraction."""
from __future__ import annotations

import pytest

from iwiki_mcp.codegraph.languages.typescript import TypeScriptAdapter
from iwiki_mcp.codegraph.models import file_id


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
    assert "a.ts/greet" in by_name
    symbol = by_name["a.ts/greet"]
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
    assert kinds["a.ts/Animal"] == "class"
    assert kinds["a.ts/Animal.speak"] == "method"


def test_interface_type_alias_enum_extracted():
    source = (
        b"interface Named { name: string }\n"
        b"type Alias = string | number;\n"
        b"enum Color { Red, Green, Blue }\n"
    )
    adapter = TypeScriptAdapter("domain", ("a.ts",), parser_version="test")

    parsed = adapter.parse_file(source, "a.ts")

    kinds = {symbol.qualified_name: symbol.kind for symbol in parsed.symbols}
    assert kinds["a.ts/Named"] == "interface"
    assert kinds["a.ts/Alias"] == "type_alias"
    assert kinds["a.ts/Color"] == "enum"


def test_arrow_function_const_extracted():
    source = b"export const add = (a: number, b: number): number => a + b;\n"
    adapter = TypeScriptAdapter("domain", ("a.ts",), parser_version="test")

    parsed = adapter.parse_file(source, "a.ts")

    by_name = {symbol.qualified_name: symbol for symbol in parsed.symbols}
    assert "a.ts/add" in by_name
    assert by_name["a.ts/add"].kind == "function"
