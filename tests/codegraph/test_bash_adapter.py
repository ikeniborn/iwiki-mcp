"""Contract tests for static Bash declaration extraction."""
from __future__ import annotations

import json

from iwiki_mcp.codegraph.languages.bash import BashAdapter
from iwiki_mcp.codegraph.resolver import SymbolIndex


def _adapter():
    return BashAdapter("domain", (), parser_version="test-parser")


def _calls(adapter, parsed):
    result = adapter.resolve_references(
        parsed, SymbolIndex.from_parsed_files((parsed,))
    )
    return tuple(item for item in result.relations if item.relation_type == "CALLS")


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
    assert {reference.target_reference for reference in parsed.references} == {":"}
    assert [relation.relation_type for relation in result.relations].count("CALLS") == 2
    assert [relation.relation_type for relation in result.relations].count("DECLARES") == 2
    declarations = tuple(
        relation for relation in result.relations
        if relation.relation_type == "DECLARES"
    )
    assert all(
        relation.resolution_state == "resolved" for relation in declarations
    )
    assert {relation.source_module_id for relation in declarations} == {
        parsed.file.module_id
    }
    assert {relation.target_symbol_id for relation in declarations} == set(symbol_ids)


def test_syntax_errors_keep_file_warn_and_suppress_error_intersections():
    parsed = _adapter().parse_file(
        b"good() { :; }\nbroken() { if; }\n", "scripts/lib.sh"
    )

    assert parsed.file.path == "scripts/lib.sh"
    assert parsed.warnings == ("parse_error",)
    assert [symbol.local_name for symbol in parsed.symbols] == ["good"]


def test_literal_calls_have_function_owners_and_resolve_locally():
    adapter = _adapter()
    source = b"helper() { :; }\nrun() { helper; external-tool; }\n"
    parsed = adapter.parse_file(source, "scripts/lib.sh")

    calls = _calls(adapter, parsed)
    helper = next(item for item in calls if item.target_symbol_id is not None)
    external = next(item for item in calls if item.target_reference == "external-tool")
    run = next(item for item in parsed.symbols if item.local_name == "run")
    target = next(item for item in parsed.symbols if item.local_name == "helper")

    assert helper.source_symbol_id == run.symbol_id
    assert helper.target_symbol_id == target.symbol_id
    assert helper.resolution_state == "resolved"
    assert external.source_symbol_id == run.symbol_id
    assert external.resolution_state == "unresolved"
    assert (external.source_start_byte, external.source_end_byte) == (32, 45)


def test_dynamic_outer_command_is_omitted_but_nested_literal_is_kept():
    adapter = _adapter()
    parsed = adapter.parse_file(
        b"run() { $COMMAND; $(inner); }\n", "scripts/lib.sh"
    )

    references = {item.target_reference for item in parsed.references}

    assert "$COMMAND" not in references
    assert "$(inner)" not in references
    assert references == {"inner"}
    assert _calls(adapter, parsed)[0].resolution_state == "unresolved"


def test_source_and_dot_commands_are_skipped_without_skipping_other_calls():
    adapter = _adapter()
    parsed = adapter.parse_file(
        b"run() { source config.sh; . other.sh; helper; }\n", "scripts/lib.sh"
    )

    references = {item.target_reference for item in parsed.references}

    assert references == {"helper"}
    assert _calls(adapter, parsed)[0].resolution_state == "unresolved"


def test_top_level_literal_call_is_owned_by_its_module():
    adapter = _adapter()
    parsed = adapter.parse_file(b"external-tool\n", "scripts/lib.sh")

    call = _calls(adapter, parsed)[0]

    assert call.source_symbol_id is None
    assert call.source_module_id == parsed.file.module_id
    assert call.target_reference == "external-tool"
    assert call.resolution_state == "unresolved"


def test_bash_call_does_not_resolve_same_name_in_another_file():
    adapter = _adapter()
    caller = adapter.parse_file(b"run() { helper; }\n", "scripts/caller.sh")
    provider = adapter.parse_file(b"helper() { :; }\n", "scripts/provider.sh")

    result = adapter.resolve_references(
        caller, SymbolIndex.from_parsed_files((caller, provider))
    )
    call = next(item for item in result.relations if item.relation_type == "CALLS")

    assert call.target_symbol_id is None
    assert call.target_reference == "helper"
    assert call.resolution_state == "unresolved"


def test_nested_function_commands_use_the_innermost_function_owner():
    adapter = _adapter()
    parsed = adapter.parse_file(
        b"outer() { inner() { leaf; }; inner; }\n", "scripts/lib.sh"
    )

    calls = _calls(adapter, parsed)
    outer = next(item for item in parsed.symbols if item.local_name == "outer")
    inner = next(item for item in parsed.symbols if item.local_name == "inner")
    leaf = next(item for item in calls if item.target_reference == "leaf")
    inner_call = next(item for item in calls if item.target_symbol_id == inner.symbol_id)

    assert leaf.source_symbol_id == inner.symbol_id
    assert inner_call.source_symbol_id == outer.symbol_id


def test_deeply_nested_command_substitutions_do_not_recurse():
    depth = 500
    source = ("$(echo " * depth + "leaf" + ")" * depth).encode()

    parsed = _adapter().parse_file(source, "scripts/deep.sh")

    assert parsed.file.path == "scripts/deep.sh"
    assert len(parsed.references) == depth
    assert all(reference.target_reference == "echo" for reference in parsed.references)


def test_duplicate_local_functions_produce_ambiguous_calls():
    adapter = _adapter()
    parsed = adapter.parse_file(
        b"dup() { :; }\ndup() { :; }\nrun() { dup; }\n", "scripts/lib.sh"
    )

    calls = tuple(
        item for item in _calls(adapter, parsed)
        if item.target_symbol_id is not None
    )

    assert len(calls) == 2
    assert {item.resolution_state for item in calls} == {"ambiguous"}
    assert len({item.target_symbol_id for item in calls}) == 2
