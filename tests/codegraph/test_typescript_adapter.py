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
