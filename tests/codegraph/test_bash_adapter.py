"""Contract tests for static Bash declaration extraction."""
from __future__ import annotations

import json

from iwiki_mcp.codegraph.languages.bash import BashAdapter
from iwiki_mcp.codegraph.resolver import SymbolIndex


def _adapter():
    return BashAdapter("domain", (), parser_version="test-parser")


def test_identity_and_module_record():
    adapter = _adapter()

    parsed = adapter.parse_file(b"run() { :; }\n", "scripts/lib.sh")

    assert (adapter.language, adapter.prefix, adapter.extensions) == (
        "bash", "sh", (".sh",)
    )
    assert adapter.adapter_version == "bash-adapter-v1"
    assert parsed.file.language == "bash"
    assert parsed.file.module_key == "scripts/lib.sh"
    assert parsed.file.module_local_name == "lib"
    assert parsed.file.module_qualified_name == "scripts.lib"
    assert parsed.file.file_id.startswith("sh:file:")
    assert parsed.file.module_id is not None
    assert parsed.file.module_id.startswith("sh:module:")


def test_function_forms_have_exact_ranges_and_no_source_metadata():
    parsed = _adapter().parse_file(
        b"first() { :; }\nfunction second { :; }\n", "bin/main.sh"
    )

    assert [symbol.local_name for symbol in parsed.symbols] == ["first", "second"]
    assert [symbol.qualified_name for symbol in parsed.symbols] == [
        "bin.main.first", "bin.main.second"
    ]
    first = parsed.symbols[0]
    second = parsed.symbols[1]
    assert (first.start_line, second.start_line) == (1, 2)
    assert first.end_byte == len(b"first() { :; }")
    assert b"first() { :; }" == b"first() { :; }\nfunction second { :; }\n"[
        first.start_byte:first.end_byte
    ]
    assert (second.start_byte, second.end_byte) == (15, 37)
    assert second.end_line == 2
    assert b"function second { :; }" == b"first() { :; }\nfunction second { :; }\n"[
        second.start_byte:second.end_byte
    ]
    assert all(symbol.signature is None for symbol in parsed.symbols)
    assert all("source" not in json.loads(symbol.metadata_json) for symbol in parsed.symbols)


def test_duplicate_functions_keep_occurrence_symbols_and_declarations():
    source = b"run() { :; }\nrun() { :; }\n"
    adapter = _adapter()
    parsed = adapter.parse_file(source, "scripts/lib.sh")
    repeated = adapter.parse_file(source, "scripts/lib.sh")

    assert [symbol.local_name for symbol in parsed.symbols] == ["run", "run"]
    symbol_ids = [symbol.symbol_id for symbol in parsed.symbols]
    assert len(set(symbol_ids)) == 2
    assert symbol_ids == [symbol.symbol_id for symbol in repeated.symbols]

    result = adapter.resolve_references(parsed, SymbolIndex.from_parsed_files((parsed,)))
    assert parsed.references == ()
    assert [relation.relation_type for relation in result.relations] == [
        "DECLARES", "DECLARES"
    ]
    assert all(
        relation.resolution_state == "resolved" for relation in result.relations
    )
    assert {relation.source_module_id for relation in result.relations} == {
        parsed.file.module_id
    }
    assert {relation.target_symbol_id for relation in result.relations} == set(symbol_ids)


def test_syntax_errors_keep_file_warn_and_suppress_error_intersections():
    parsed = _adapter().parse_file(
        b"good() { :; }\nbroken() { if; }\n", "scripts/lib.sh"
    )

    assert parsed.file.path == "scripts/lib.sh"
    assert parsed.warnings == ("parse_error",)
    assert [symbol.local_name for symbol in parsed.symbols] == ["good"]
