"""Contract tests for conservative code-graph reference resolution."""
from __future__ import annotations

import builtins
import hashlib
import inspect
import json
from pathlib import Path

import pytest

from iwiki_mcp.codegraph.languages.python import PythonAdapter as _PythonAdapter
from iwiki_mcp.codegraph.models import (
    FileRecord,
    ParsedFile,
    ReferenceRecord,
    SymbolRecord,
    compact_casefold,
    file_id,
    module_id,
    token_key,
)
from iwiki_mcp.codegraph import resolver as resolver_module
from iwiki_mcp.codegraph.resolver import resolve_references
from iwiki_mcp.codegraph.resolver import SymbolIndex


_MODULE_NAMES = {
    "dynamic.py": "dynamic",
    "models.py": "models",
    "use.py": "use",
    "pkg/a.py": "pkg.a",
    "pkg/b.py": "pkg.b",
    "pkg/bad.py": "pkg.bad",
    "pkg/before.py": "pkg.before",
    "pkg/empty.py": "pkg.empty",
    "pkg/factory.py": "pkg.factory",
    "pkg/local.py": "pkg.local",
    "pkg/nested.py": "pkg.nested",
    "pkg/sibling.py": "pkg.sibling",
    "pkg/use.py": "pkg.use",
}


class PythonAdapter(_PythonAdapter):
    """Resolver fixture adapter with explicit deterministic module evidence."""

    def __init__(self):
        super().__init__("domain", _MODULE_NAMES)


def _parse(adapter, path, source):
    return adapter.parse_file(source.encode(), path)


def _target_name(relation, index):
    if relation.target_reference is not None:
        return relation.target_reference
    for qualified_name, candidates in index.by_qualified.items():
        if any(item.symbol_id == relation.target_symbol_id for item in candidates):
            return qualified_name
    for qualified_name, candidates in index.modules_by_qualified.items():
        if any(item.module_id == relation.target_module_id for item in candidates):
            return qualified_name
    return None


def _language_file_record(*, language, prefix, path, module_qualified_name):
    local_name = module_qualified_name.rsplit(".", 1)[-1]
    return FileRecord(
        file_id=file_id(language, prefix, "domain", path),
        repository_id="domain",
        path=path,
        path_casefold=compact_casefold(path),
        file_local_name=path.rsplit("/", 1)[-1],
        file_name_tokens_casefold=token_key(path.rsplit("/", 1)[-1]),
        language=language,
        content_hash="0" * 64,
        parser_version="test-parser",
        size_bytes=0,
        start_line=1,
        end_line=1,
        start_byte=0,
        end_byte=0,
        module_key=path,
        module_id=module_id(language, prefix, "domain", path, module_qualified_name),
        module_qualified_name=module_qualified_name,
        module_local_name=local_name,
        module_name_tokens_casefold=token_key(module_qualified_name, local_name),
    )


def _empty_parsed(file):
    return ParsedFile(file=file, symbols=(), references=(), warnings=())


def _module_reference(*, source_file_id, target_reference):
    return ReferenceRecord(
        source_symbol_id=None,
        source_file_id=source_file_id,
        source_module_id=None,
        relation_type="IMPORTS",
        target_reference=target_reference,
        source_line=1,
        source_byte=0,
        source_end_line=1,
        source_end_byte=1,
        binding_name="thing",
        binding_kind="implicit_binding",
        binding_name_tokens_casefold=token_key("thing"),
        target_kind_hint="module",
        resolution_scope="project",
    )


def test_python_import_ignores_same_named_javascript_module():
    python_file = _language_file_record(
        language="python", prefix="py", path="src/utils.py",
        module_qualified_name="src.utils",
    )
    javascript_file = _language_file_record(
        language="javascript", prefix="js", path="src/utils.js",
        module_qualified_name="src.utils",
    )
    index = SymbolIndex.from_parsed_files(
        (_empty_parsed(python_file), _empty_parsed(javascript_file))
    )
    reference = _module_reference(
        source_file_id=python_file.file_id, target_reference="src.utils",
    )
    relations = resolve_references("python", "py", "domain", (reference,), index)
    assert len(relations) == 1
    assert relations[0].resolution_state == "resolved"
    assert relations[0].target_module_id == python_file.module_id


def test_javascript_import_resolves_into_a_typescript_module():
    typescript_file = _language_file_record(
        language="typescript", prefix="ts", path="src/shapes.ts",
        module_qualified_name="src.shapes",
    )
    javascript_file = _language_file_record(
        language="javascript", prefix="js", path="src/app.js",
        module_qualified_name="src.app",
    )
    index = SymbolIndex.from_parsed_files(
        (_empty_parsed(typescript_file), _empty_parsed(javascript_file))
    )
    reference = _module_reference(
        source_file_id=javascript_file.file_id, target_reference="src.shapes",
    )
    relations = resolve_references("javascript", "js", "domain", (reference,), index)
    assert relations[0].resolution_state == "resolved"
    assert relations[0].target_module_id == typescript_file.module_id


def test_bash_references_only_match_bash_symbols():
    bash_file = _language_file_record(
        language="bash", prefix="sh", path="bin/run.sh",
        module_qualified_name="bin.run",
    )
    python_file = _language_file_record(
        language="python", prefix="py", path="bin/run.py",
        module_qualified_name="bin.run",
    )
    index = SymbolIndex.from_parsed_files((
        _empty_parsed(bash_file), _empty_parsed(python_file),
    ))
    reference = _module_reference(
        source_file_id=bash_file.file_id, target_reference="bin.run",
    )

    relations = resolve_references("bash", "sh", "domain", (reference,), index)

    assert relations[0].resolution_state == "resolved"
    assert relations[0].target_module_id == bash_file.module_id


def test_unknown_language_is_not_filtered():
    other_file = _language_file_record(
        language="ruby", prefix="rb", path="src/thing.rb",
        module_qualified_name="src.thing",
    )
    index = SymbolIndex.from_parsed_files((_empty_parsed(other_file),))
    reference = _module_reference(
        source_file_id="rb-source", target_reference="src.thing",
    )
    relations = resolve_references("ruby", "rb", "domain", (reference,), index)
    assert relations[0].resolution_state == "resolved"


def test_resolves_known_and_keeps_ambiguous_and_external_references():
    adapter = PythonAdapter()
    provider = _parse(
        adapter,
        "pkg/factory.py",
        "def make(value):\n    pass\n",
    )
    duplicate = _parse(
        adapter, "pkg/factory.py", "def make(other, value):\n    pass\n"
    )
    consumer = _parse(
        adapter,
        "pkg/use.py",
        ("from pkg.factory import make\n\ndef run():\n    make()\n"
         "    factory.make()\n    external.call()\n"),
    )

    index = SymbolIndex.from_parsed_files((provider, duplicate, consumer))
    result = adapter.resolve_references(consumer, index)

    assert {reference.target_reference for reference in consumer.references} >= {
        "pkg.factory.make", "factory.make", "external.call"
    }
    states = {
        (_target_name(relation, index), relation.resolution_state)
        for relation in result.relations
    }
    assert ("pkg.factory.make", "ambiguous") in states
    assert ("factory.make", "unresolved") in states
    assert ("external.call", "unresolved") in states
    assert all(
        (relation.target_module_id is not None)
        + (relation.target_symbol_id is not None)
        + (relation.target_reference is not None) >= 1
        for relation in result.relations
    )


def test_known_module_missing_member_is_partial_and_relations_are_deterministic():
    adapter = PythonAdapter()
    provider = _parse(adapter, "pkg/a.py", "def known():\n    pass\n")
    consumer = _parse(
        adapter,
        "pkg/use.py",
        "from pkg.a import missing\n\ndef run():\n    missing()\n",
    )
    index = SymbolIndex.from_parsed_files((consumer, provider))
    first = adapter.resolve_references(consumer, index)
    second = adapter.resolve_references(consumer, index)

    assert any(item.target_reference == "pkg.a.missing"
               and item.resolution_state == "partially_resolved"
               for item in first.relations)
    assert first == second
    assert list(first.relations) == sorted(
        first.relations,
        key=lambda item: (
            item.source_symbol_id or item.source_module_id or item.source_file_id,
            item.relation_type,
            item.source_start_line,
            item.source_end_line,
            item.source_start_byte,
            item.source_end_byte,
            item.target_module_id or "",
            item.target_symbol_id or "",
            item.target_reference or "",
            item.binding_kind or "",
            item.binding_name or "",
            item.relation_id,
        ),
    )


FIXTURES = Path(__file__).parents[1] / "fixtures" / "codegraph"


def test_import_inheritance_and_dynamic_references_are_static_and_conservative(monkeypatch):
    adapter = PythonAdapter()
    imported = _parse(
        adapter, "pkg/a.py", (FIXTURES / "python_imports/pkg/a.py").read_text()
    )
    consumer = _parse(
        adapter, "pkg/b.py", (FIXTURES / "python_imports/pkg/b.py").read_text()
    )
    models = _parse(
        adapter, "models.py", (FIXTURES / "python_inheritance/models.py").read_text()
    )
    dynamic_source = (FIXTURES / "python_dynamic/dynamic.py").read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("source execution is forbidden")

    monkeypatch.setattr(builtins, "compile", forbidden)
    monkeypatch.setattr(builtins, "eval", forbidden)
    monkeypatch.setattr(builtins, "exec", forbidden)
    dynamic = adapter.parse_file(dynamic_source, "dynamic.py")
    index = SymbolIndex.from_parsed_files((imported, consumer, models, dynamic))
    relations = adapter.resolve_references(consumer, index).relations
    inheritance = adapter.resolve_references(models, index).relations
    dynamic_relations = adapter.resolve_references(dynamic, index).relations

    assert any(_target_name(item, index) == "pkg.a.known"
               and item.resolution_state == "resolved"
               for item in relations)
    assert any(item.target_reference == "pkg.a.missing"
               and item.resolution_state == "partially_resolved"
               for item in relations)
    assert any(item.target_reference == "external.package.foreign"
               and item.resolution_state == "unresolved"
               for item in relations)
    assert any(item.target_reference == "pkg.a.*"
               and item.resolution_state == "unresolved"
               and item.target_module_id is None
               and item.target_symbol_id is None
               for item in relations)
    assert any(_target_name(item, index) == "models.Base"
               and item.resolution_state == "resolved"
               for item in inheritance)
    assert all(item.resolution_state != "resolved" for item in dynamic_relations
               if item.target_reference in {"factory.make", "external.call"})
    assert all("/home/" not in (item.target_reference or "") for item in dynamic_relations)


def test_import_forms_are_canonical_and_relation_ids_survive_relocation():
    adapter = PythonAdapter()
    source = (
        "import pkg.a\nimport external.package as external_alias\n"
        "from pkg.a import known\nfrom .a import local_known\n"
    )
    first = _parse(adapter, "pkg/use.py", source)
    second = _parse(adapter, "pkg/use.py", source)
    references = {item.target_reference for item in first.references}

    assert references == {"pkg.a", "external.package", "pkg.a.known", "pkg.a.local_known"}
    index = SymbolIndex.from_parsed_files((first,))
    assert [item.relation_id for item in adapter.resolve_references(first, index).relations] == [
        item.relation_id for item in adapter.resolve_references(second, index).relations
    ]


def test_inheritance_never_resolves_non_class_symbols():
    adapter = PythonAdapter()
    definitions = _parse(adapter, "models.py", "def Base():\n    pass\n")
    use = _parse(adapter, "use.py", "class Child(models.Base):\n    pass\n")
    index = SymbolIndex.from_parsed_files((definitions, use))

    relation = next(item for item in adapter.resolve_references(use, index).relations
                    if item.relation_type == "INHERITS")
    assert relation.target_symbol_id is None
    assert relation.resolution_state == "partially_resolved"


def test_plain_import_binding_and_local_class_calls_resolve_canonically():
    adapter = PythonAdapter()
    imported = _parse(adapter, "pkg/a.py", "def known():\n    pass\n")
    consumer = _parse(
        adapter,
        "pkg/use.py",
        "import pkg.a\n\ndef use():\n    pkg.a.known()\n",
    )
    local = _parse(
        adapter,
        "pkg/local.py",
        "class A:\n    def method(self):\n        pass\n\ndef use():\n    A.method()\n",
    )
    index = SymbolIndex.from_parsed_files((imported, consumer, local))
    imported_relations = adapter.resolve_references(consumer, index).relations
    local_relations = adapter.resolve_references(local, index).relations

    assert any(_target_name(item, index) == "pkg.a.known"
               and item.resolution_state == "resolved"
               for item in imported_relations)
    assert any(_target_name(item, index) == "pkg.local.A.method"
               and item.resolution_state == "resolved"
               for item in local_relations)
    module_only = _parse(adapter, "pkg/empty.py", "import pkg.a\n")
    module_symbol = _parse(adapter, "pkg/a.py", "def x():\n pass\n")
    module_index = SymbolIndex.from_parsed_files((module_symbol, module_only))
    module_relation = next(
        item for item in adapter.resolve_references(module_only, module_index).relations
        if _target_name(item, module_index) == "pkg.a"
    )
    assert module_relation.target_symbol_id is None
    assert module_relation.target_module_id == module_symbol.file.module_id
    assert module_relation.target_reference is None
    assert module_relation.resolution_state == "resolved"


def test_same_line_and_file_scoped_references_have_stable_distinct_relation_ids():
    index = SymbolIndex.from_symbols(())
    references = (
        ReferenceRecord(None, "parse:file", "CALLS", "external.a", 4, 10),
        ReferenceRecord(None, "parse:file", "CALLS", "external.b", 4, 20),
        ReferenceRecord(None, "parse:file", "CALLS", "external.c", None, None),
    )
    first = resolve_references("python", "py", "domain", references, index)
    second = resolve_references("python", "py", "domain", references, index)

    assert len({item.relation_id for item in first}) == 3
    assert first == second
    digest = hashlib.sha256("\0".join((
        "relation",
        "python",
        "domain",
        "parse:file",
        "CALLS",
        "4",
        "4",
        "10",
        "10",
        "",
        "external.a",
        "",
        "",
    )).encode()).hexdigest()
    external_a = next(item for item in first if item.target_reference == "external.a")
    assert external_a.relation_id == f"py:relation:{digest}"


def test_index_ignores_bad_metadata_and_methods_as_module_exports():
    adapter = PythonAdapter()
    parsed = _parse(adapter, "pkg/a.py", "class C:\n    def method(self):\n        pass\n")
    method = parsed.symbols[1]
    malformed = SymbolRecord(
        **{**method.__dict__, "metadata_json": "not-json"}
    )
    index = SymbolIndex.from_symbols((method, malformed))

    assert ("pkg.a", "method") not in index.by_module_local


def test_parse_errors_do_not_emit_spurious_references():
    adapter = PythonAdapter()
    parsed = _parse(adapter, "pkg/bad.py", "from pkg.a import ( bogus(\ndef f(: mistaken()\n")

    assert parsed.warnings == ("parse_error",)
    assert parsed.references == ()


def test_parameter_and_assignment_shadow_import_aliases():
    adapter = PythonAdapter()
    provider = _parse(adapter, "pkg/a.py", "def f():\n    pass\n")
    consumer = _parse(
        adapter,
        "pkg/use.py",
        ("from pkg.a import f\n\ndef parameter(f):\n    f()\n"
         "\ndef assigned():\n    f = factory\n    f()\n"),
    )
    relations = adapter.resolve_references(
        consumer, SymbolIndex.from_parsed_files((provider, consumer))
    ).relations

    calls = [item for item in relations if item.relation_type == "CALLS"]
    assert all(item.resolution_state != "resolved" for item in calls)


def test_function_bindings_hide_imported_alias_calls_before_binding():
    adapter = PythonAdapter()
    provider = _parse(adapter, "pkg/a.py", "def f():\n    pass\n")
    consumer = _parse(
        adapter, "pkg/use.py",
        ("from pkg.a import f\n\ndef assigned():\n    f()\n    f = other\n"
         "    f()\n\ndef defined():\n    f()\n    def f():\n        pass\n    f()\n"),
    )
    relations = adapter.resolve_references(
        consumer, SymbolIndex.from_parsed_files((provider, consumer))
    ).relations
    calls = sorted(
        (item for item in relations if item.relation_type == "CALLS"),
        key=lambda item: item.source_line,
    )
    assert [(item.source_line, item.target_reference, item.resolution_state)
            for item in calls] == [
                (4, "f", "unresolved"),
                (6, "f", "unresolved"),
                (9, "f", "unresolved"),
                (12, None, "resolved"),
            ]


def test_lambda_comprehension_and_nested_function_inherit_unshadowed_aliases():
    adapter = PythonAdapter()
    provider = _parse(adapter, "pkg/a.py", "def f():\n    pass\n")
    consumer = _parse(
        adapter,
        "pkg/use.py",
        ("from pkg.a import f\n"
         "plain = lambda: f()\n"
         "shadowed = lambda f: f()\n"
         "inherited = [f() for item in items]\n"
         "shadowed_items = [f() for f in items]\n"
         "def outer():\n"
         "    def inner():\n"
         "        f()\n"),
    )
    relations = adapter.resolve_references(
        consumer, SymbolIndex.from_parsed_files((provider, consumer))
    ).relations

    assert [(item.source_line, item.target_reference, item.resolution_state)
            for item in relations if item.relation_type == "CALLS"] == [
                (2, None, "resolved"),
                (3, "f", "unresolved"),
                (4, None, "resolved"),
                (5, "f", "unresolved"),
                (8, None, "resolved"),
            ]


def test_relation_sorting_uses_byte_offset_within_same_line():
    relations = resolve_references("python", "py", "domain", (
        ReferenceRecord(None, "parse:file", "CALLS", "z", 1, 20),
        ReferenceRecord(None, "parse:file", "CALLS", "a", 1, 10),
    ), SymbolIndex.from_symbols(()))
    assert [item.source_byte for item in relations] == [10, 20]


def test_relation_ids_match_for_same_relative_file_under_distinct_roots(tmp_path):
    adapter = PythonAdapter()
    source = b"def f():\n    external.call()\n"
    roots = (tmp_path / "one", tmp_path / "two")
    parsed = []
    for root in roots:
        path = root / "pkg" / "use.py"
        path.parent.mkdir(parents=True)
        path.write_bytes(source)
        relative = path.relative_to(root).as_posix()
        parsed.append(adapter.parse_file(path.read_bytes(), relative))
    index = SymbolIndex.from_parsed_files(parsed)
    first_ids = [item.relation_id
                 for item in adapter.resolve_references(parsed[0], index).relations]
    assert first_ids == [
        item.relation_id for item in adapter.resolve_references(parsed[1], index).relations
    ]


def test_typed_targets_and_ranges_fan_out_duplicate_module_imports():
    adapter = _PythonAdapter(
        "domain",
        {
            "root_a/pkg/a.py": "pkg.a",
            "root_b/pkg/a.py": "pkg.a",
            "pkg/b.py": "pkg.b",
        },
    )
    provider_source = (
        FIXTURES / "python_imports/pkg/a.py"
    ).read_bytes()
    providers = tuple(
        adapter.parse_file(provider_source, path)
        for path in ("root_a/pkg/a.py", "root_b/pkg/a.py")
    )
    source = (
        b"import pkg.a as svc\n"
        b"from pkg.a import helper\n"
        b"import external.pkg\n"
    )
    consumer = adapter.parse_file(source, "pkg/b.py")

    relations = adapter.resolve_references(
        consumer, SymbolIndex.from_parsed_files((*providers, consumer))
    ).relations
    imports = [item for item in relations if item.relation_type == "IMPORTS"]
    module_imports = [item for item in imports if item.binding_name == "svc"]
    helper_imports = [item for item in imports if item.binding_name == "helper"]
    external_import = next(
        item for item in imports if item.target_reference == "external.pkg"
    )

    assert len(module_imports) == 2
    assert all(item.resolution_state == "ambiguous" for item in module_imports)
    assert {item.target_module_id for item in module_imports} == {
        parsed.file.module_id for parsed in providers
    }
    assert all(item.target_symbol_id is None and item.target_reference is None
               for item in module_imports)
    assert all(
        (item.binding_kind, item.binding_name_tokens_casefold)
        == ("explicit_alias", token_key("svc"))
        for item in module_imports
    )
    assert all(
        (item.source_start_line, item.source_end_line,
         item.source_start_byte, item.source_end_byte)
        == (1, 1, 0, len(b"import pkg.a as svc"))
        for item in module_imports
    )

    assert len(helper_imports) == 2
    assert all(item.resolution_state == "ambiguous" for item in helper_imports)
    assert {item.target_symbol_id for item in helper_imports} == {
        next(symbol.symbol_id for symbol in parsed.symbols
             if symbol.local_name == "helper")
        for parsed in providers
    }
    assert all(item.target_module_id is None and item.target_reference is None
               for item in helper_imports)
    assert all(
        (item.binding_kind, item.binding_name_tokens_casefold)
        == ("implicit_binding", token_key("helper"))
        for item in helper_imports
    )
    assert all(
        (item.source_start_line, item.source_end_line,
         item.source_start_byte, item.source_end_byte)
        == (2, 2, source.index(b"from pkg.a"),
            source.index(b"from pkg.a") + len(b"from pkg.a import helper"))
        for item in helper_imports
    )

    assert external_import.resolution_state == "unresolved"
    assert external_import.target_module_id is None
    assert external_import.target_symbol_id is None
    assert external_import.binding_name == "external"
    assert external_import.binding_kind == "implicit_binding"


def test_typed_targets_and_ranges_cover_exact_four_resolution_states():
    adapter = PythonAdapter()
    provider = _parse(
        adapter,
        "pkg/a.py",
        "def known():\n    pass\n\ndef overloaded(value):\n    pass\n",
    )
    duplicate = _parse(
        adapter,
        "pkg/a.py",
        "def overloaded(value, other):\n    pass\n",
    )
    source = (
        "from pkg.a import known\n"
        "from pkg.a import missing\n"
        "from pkg.a import overloaded\n"
        "import external.pkg\n"
    )
    consumer = _parse(adapter, "pkg/use.py", source)

    imports = [
        item for item in adapter.resolve_references(
            consumer, SymbolIndex.from_parsed_files((provider, duplicate, consumer))
        ).relations
        if item.relation_type == "IMPORTS"
    ]
    by_binding = {}
    for relation in imports:
        by_binding.setdefault(relation.binding_name, []).append(relation)

    resolved = by_binding["known"]
    assert len(resolved) == 1
    assert resolved[0].resolution_state == "resolved"
    assert resolved[0].target_symbol_id is not None
    assert resolved[0].target_reference is None

    partial = by_binding["missing"]
    assert len(partial) == 1
    assert partial[0].resolution_state == "partially_resolved"
    assert partial[0].target_module_id == provider.file.module_id
    assert partial[0].target_symbol_id is None
    assert partial[0].target_reference == "pkg.a.missing"

    ambiguous = by_binding["overloaded"]
    assert len(ambiguous) == 2
    assert all(item.resolution_state == "ambiguous" for item in ambiguous)
    assert all(item.target_symbol_id is not None and item.target_reference is None
               for item in ambiguous)

    unresolved = by_binding["external"]
    assert len(unresolved) == 1
    assert unresolved[0].resolution_state == "unresolved"
    assert unresolved[0].target_reference == "external.pkg"
    assert unresolved[0].target_module_id is None
    assert unresolved[0].target_symbol_id is None


def test_plain_import_prefers_exact_module_over_same_named_symbol():
    adapter = _PythonAdapter(
        "domain",
        {
            "pkg/__init__.py": "pkg",
            "pkg/a.py": "pkg.a",
            "pkg/use.py": "pkg.use",
        },
    )
    same_named_symbol = adapter.parse_file(
        b"class a:\n    pass\n", "pkg/__init__.py"
    )
    module = adapter.parse_file(b"", "pkg/a.py")
    consumer = adapter.parse_file(b"import pkg.a\n", "pkg/use.py")

    relation = next(
        item for item in adapter.resolve_references(
            consumer,
            SymbolIndex.from_parsed_files(
                (same_named_symbol, module, consumer)
            ),
        ).relations
        if item.relation_type == "IMPORTS"
    )

    assert relation.resolution_state == "resolved"
    assert relation.target_module_id == module.file.module_id
    assert relation.target_symbol_id is None
    assert relation.target_reference is None


def test_plain_import_never_falls_back_to_same_named_symbol():
    adapter = _PythonAdapter(
        "domain",
        {"pkg/__init__.py": "pkg", "pkg/use.py": "pkg.use"},
    )
    package = adapter.parse_file(
        b"class a:\n    pass\n", "pkg/__init__.py"
    )
    consumer = adapter.parse_file(b"import pkg.a\n", "pkg/use.py")

    relation = next(
        item for item in adapter.resolve_references(
            consumer,
            SymbolIndex.from_parsed_files((package, consumer)),
        ).relations
        if item.relation_type == "IMPORTS"
    )

    assert relation.resolution_state == "partially_resolved"
    assert relation.target_module_id == package.file.module_id
    assert relation.target_symbol_id is None
    assert relation.target_reference == "pkg.a"


def test_wildcard_import_remains_unresolved_reference_only():
    adapter = PythonAdapter()
    provider = _parse(adapter, "pkg/a.py", "def helper():\n    pass\n")
    consumer = _parse(adapter, "pkg/use.py", "from pkg.a import *\n")

    relation = next(
        item for item in adapter.resolve_references(
            consumer, SymbolIndex.from_parsed_files((provider, consumer))
        ).relations
        if item.relation_type == "IMPORTS"
    )

    assert relation.resolution_state == "unresolved"
    assert relation.target_reference == "pkg.a.*"
    assert relation.target_module_id is None
    assert relation.target_symbol_id is None
    assert relation.binding_name == "*"
    assert relation.binding_kind == "implicit_binding"


def test_qualified_cross_file_call_requires_visible_import_evidence():
    adapter = PythonAdapter()
    provider = _parse(adapter, "pkg/a.py", "def helper():\n    pass\n")
    imported = _parse(
        adapter,
        "pkg/use.py",
        "import pkg.a\npkg.a.helper()\n",
    )
    unimported = _parse(adapter, "pkg/b.py", "pkg.a.helper()\n")
    wrong_import = _parse(
        adapter,
        "pkg/wrong.py",
        "import pkg.b\npkg.a.helper()\n",
    )
    index = SymbolIndex.from_parsed_files((
        provider, imported, unimported, wrong_import,
    ))

    imported_call = next(
        item for item in adapter.resolve_references(imported, index).relations
        if item.relation_type == "CALLS"
    )
    unimported_call = next(
        item for item in adapter.resolve_references(unimported, index).relations
        if item.relation_type == "CALLS"
    )
    wrong_import_call = next(
        item for item in adapter.resolve_references(
            wrong_import, index
        ).relations
        if item.relation_type == "CALLS"
    )

    assert imported_call.resolution_state == "resolved"
    assert imported_call.target_symbol_id == provider.symbols[0].symbol_id
    assert imported_call.target_reference is None
    assert unimported_call.resolution_state == "unresolved"
    assert unimported_call.target_symbol_id is None
    assert unimported_call.target_module_id is None
    assert unimported_call.target_reference == "pkg.a.helper"
    assert wrong_import_call.resolution_state == "unresolved"
    assert wrong_import_call.target_symbol_id is None
    assert wrong_import_call.target_module_id is None
    assert wrong_import_call.target_reference == "pkg.a.helper"


def test_same_file_qualified_call_requires_lexical_binding_evidence():
    adapter = PythonAdapter()
    parsed = _parse(
        adapter,
        "pkg/use.py",
        "def local():\n"
        "    pass\n\n"
        "class C:\n"
        "    def m(self):\n"
        "        pass\n\n"
        "pkg.use.local()\n"
        "pkg.use.C.m()\n"
        "local()\n",
    )

    calls = {
        item.source_start_line: item
        for item in adapter.resolve_references(
            parsed, SymbolIndex.from_parsed_files((parsed,))
        ).relations
        if item.relation_type == "CALLS"
    }

    assert calls[8].resolution_state == "unresolved"
    assert calls[8].target_reference == "pkg.use.local"
    assert calls[8].target_symbol_id is None
    assert calls[9].resolution_state == "unresolved"
    assert calls[9].target_reference == "pkg.use.C.m"
    assert calls[9].target_symbol_id is None
    assert calls[10].resolution_state == "resolved"
    assert calls[10].target_symbol_id == next(
        item.symbol_id for item in parsed.symbols
        if item.qualified_name == "pkg.use.local"
    )


def test_rebound_attribute_never_supplies_same_file_lexical_evidence():
    adapter = _PythonAdapter("domain", {"namespace/use.py": None})
    parsed = adapter.parse_file(
        b"def f():\n"
        b"    pass\n\n"
        b"obj.f = replacement\n"
        b"obj.f()\n",
        "namespace/use.py",
    )

    call = next(
        item for item in adapter.resolve_references(
            parsed, SymbolIndex.from_parsed_files((parsed,))
        ).relations
        if item.relation_type == "CALLS"
    )

    assert call.resolution_state == "unresolved"
    assert call.target_reference == "f"
    assert call.target_symbol_id is None
    assert call.target_module_id is None


@pytest.mark.parametrize("binding", ["f = other", "del f"])
def test_file_only_bare_rebinding_never_resolves_stale_declaration(binding):
    adapter = _PythonAdapter("domain", {"namespace/use.py": None})
    parsed = adapter.parse_file(
        (
            "def f():\n"
            "    pass\n\n"
            + binding + "\n"
            "f()\n"
        ).encode(),
        "namespace/use.py",
    )

    call = next(
        item for item in adapter.resolve_references(
            parsed, SymbolIndex.from_parsed_files((parsed,))
        ).relations
        if item.relation_type == "CALLS"
    )

    assert call.resolution_state == "unresolved"
    assert call.target_reference == "f"
    assert call.target_symbol_id is None
    assert call.target_module_id is None


def test_declares_uses_module_nested_and_file_only_owners():
    adapter = _PythonAdapter(
        "domain",
        {"pkg/owned.py": "pkg.owned", "namespace/a.py": None},
    )
    module_parsed = adapter.parse_file(
        b"class Service:\n    def run(self):\n        def nested():\n            pass\n",
        "pkg/owned.py",
    )
    file_only = adapter.parse_file(
        b"def serve():\n    pass\n", "namespace/a.py"
    )
    index = SymbolIndex.from_parsed_files((module_parsed, file_only))

    module_relations = adapter.resolve_references(module_parsed, index).relations
    file_relations = adapter.resolve_references(file_only, index).relations
    declares = [
        item for item in (*module_relations, *file_relations)
        if item.relation_type == "DECLARES"
    ]
    symbols = {
        symbol.qualified_name: symbol
        for parsed in (module_parsed, file_only)
        for symbol in parsed.symbols
    }

    ownership = {
        item.target_symbol_id: (
            item.source_module_id, item.source_symbol_id, item.source_file_id
        )
        for item in declares
    }
    assert ownership[symbols["pkg.owned.Service"].symbol_id] == (
        module_parsed.file.module_id, None, module_parsed.file.file_id
    )
    assert ownership[symbols["pkg.owned.Service.run"].symbol_id] == (
        None, symbols["pkg.owned.Service"].symbol_id, module_parsed.file.file_id
    )
    assert ownership[symbols["pkg.owned.Service.run.nested"].symbol_id] == (
        None, symbols["pkg.owned.Service.run"].symbol_id,
        module_parsed.file.file_id
    )
    assert ownership[symbols["serve"].symbol_id] == (
        None, None, file_only.file.file_id
    )
    assert len(ownership) == len(symbols)
    assert all(item.resolution_state == "resolved" for item in declares)
    assert all(item.target_reference is None for item in declares)
    assert all(
        (item.source_start_line, item.source_end_line,
         item.source_start_byte, item.source_end_byte)
        == (
            next(symbol for symbol in symbols.values()
                 if symbol.symbol_id == item.target_symbol_id).start_line,
            next(symbol for symbol in symbols.values()
                 if symbol.symbol_id == item.target_symbol_id).end_line,
            next(symbol for symbol in symbols.values()
                 if symbol.symbol_id == item.target_symbol_id).start_byte,
            next(symbol for symbol in symbols.values()
                 if symbol.symbol_id == item.target_symbol_id).end_byte,
        )
        for item in declares
    )


def test_duplicate_owner_collapse_preserves_canonical_nested_provenance():
    adapter = PythonAdapter()
    source = (
        "def outer():\n"
        "    def first():\n"
        "        pass\n"
        "    first()\n\n"
        "def outer():\n"
        "    def last():\n"
        "        pass\n"
        "    last()\n"
    )
    parsed = _parse(adapter, "pkg/a.py", source)
    repeated = _parse(adapter, "pkg/a.py", source)
    index = SymbolIndex.from_parsed_files((parsed,))
    relations = adapter.resolve_references(parsed, index).relations
    symbols = {item.qualified_name: item for item in parsed.symbols}
    outer = symbols["pkg.a.outer"]
    nested = (
        symbols["pkg.a.outer.first"],
        symbols["pkg.a.outer.last"],
    )

    assert parsed == repeated
    assert parsed.warnings.count("duplicate_symbol_identity") == 1
    for symbol in nested:
        metadata = json.loads(symbol.metadata_json)
        assert metadata == {
            "language": "python",
            "module": "pkg.a",
            "owner_symbol_id": outer.symbol_id,
        }
        assert source not in symbol.metadata_json
        assert "pkg/a.py" not in symbol.metadata_json

    declares = [
        item for item in relations
        if item.relation_type == "DECLARES"
        and item.target_symbol_id in {symbol.symbol_id for symbol in nested}
    ]
    assert len(declares) == 2
    assert {item.source_symbol_id for item in declares} == {outer.symbol_id}
    calls = [item for item in relations if item.relation_type == "CALLS"]
    assert len(calls) == 2
    assert {item.source_symbol_id for item in calls} == {outer.symbol_id}
    assert {item.target_symbol_id for item in calls} == {
        symbol.symbol_id for symbol in nested
    }


def test_file_only_cross_file_reference_remains_unresolved_without_import():
    adapter = _PythonAdapter(
        "domain", {"namespace/a.py": None, "namespace/b.py": None}
    )
    provider = adapter.parse_file(
        b"def serve():\n    pass\n", "namespace/a.py"
    )
    consumer = adapter.parse_file(
        b"serve()\n", "namespace/b.py"
    )
    index = SymbolIndex.from_parsed_files((provider, consumer))

    call = next(
        item for item in adapter.resolve_references(consumer, index).relations
        if item.relation_type == "CALLS"
    )

    assert call.resolution_state == "unresolved"
    assert call.target_reference == "serve"
    assert call.target_module_id is None
    assert call.target_symbol_id is None


def test_local_import_does_not_leak_to_sibling_function_or_before_its_statement():
    adapter = PythonAdapter()
    provider = _parse(adapter, "pkg/a.py", "def f():\n    pass\n")
    sibling = _parse(
        adapter, "pkg/sibling.py",
        "def one():\n    from pkg.a import f\n    f()\n\ndef two():\n    f()\n",
    )
    before = _parse(
        adapter, "pkg/before.py",
        "def use():\n    f()\n    from pkg.a import f\n",
    )
    index = SymbolIndex.from_parsed_files((provider, sibling, before))
    sibling_calls = [item for item in adapter.resolve_references(sibling, index).relations
                     if item.relation_type == "CALLS"]
    before_calls = [item for item in adapter.resolve_references(before, index).relations
                    if item.relation_type == "CALLS"]
    assert sorted((item.source_line, item.target_reference, item.resolution_state)
                  for item in sibling_calls) == [
                (3, None, "resolved"),
                (6, "f", "unresolved"),
            ]
    assert all(item.resolution_state == "unresolved" for item in before_calls)


def test_local_import_resolves_call_after_its_statement():
    adapter = PythonAdapter()
    provider = _parse(adapter, "pkg/a.py", "def f():\n    pass\n")
    consumer = _parse(
        adapter, "pkg/use.py", "def run():\n    from pkg.a import f\n    f()\n",
    )

    relations = adapter.resolve_references(
        consumer, SymbolIndex.from_parsed_files((provider, consumer))
    ).relations

    assert [(item.source_line, item.target_reference, item.resolution_state)
            for item in relations if item.relation_type == "CALLS"] == [
                (3, None, "resolved"),
            ]


def test_nested_scope_bindings_do_not_affect_outer_scope():
    adapter = PythonAdapter()
    provider = _parse(adapter, "pkg/a.py", "def f():\n    pass\n")
    consumer = _parse(
        adapter, "pkg/nested.py",
        ("def outer():\n    def inner():\n        from pkg.a import f\n"
         "        f = other\n        f()\n    f()\n"),
    )
    relations = adapter.resolve_references(
        consumer, SymbolIndex.from_parsed_files((provider, consumer))
    ).relations
    calls = [item for item in relations if item.relation_type == "CALLS"]
    assert len(calls) == 2
    assert all(item.resolution_state == "unresolved" for item in calls)


@pytest.mark.parametrize("body", [
    "for item in items:\n        pass",
    "with resource as item:\n        pass",
    "try:\n        pass\n    except Error as item:\n        pass",
    "inner = lambda item: item",
    "items = [item for item in values]",
    "def inner():\n        pass",
    "class Inner:\n        pass",
])
def test_unrelated_function_bindings_do_not_hide_module_aliases(body):
    adapter = PythonAdapter()
    provider = _parse(adapter, "pkg/a.py", "def f():\n    pass\n")
    consumer = _parse(
        adapter, "pkg/use.py",
        "from pkg.a import f\n\ndef use():\n    " + body.replace("\n", "\n    ") + "\n    f()\n",
    )
    relations = adapter.resolve_references(
        consumer, SymbolIndex.from_parsed_files((provider, consumer))
    ).relations
    assert all(item.target_symbol_id == provider.symbols[0].symbol_id
               and item.resolution_state == "resolved"
               for item in relations if item.relation_type == "CALLS")


@pytest.mark.parametrize("binding", [
    "f = other", "for f in items:\n    pass", "def f():\n    pass",
])
def test_module_scope_binding_invalidates_import_alias(binding):
    adapter = PythonAdapter()
    provider = _parse(adapter, "pkg/a.py", "def f():\n    pass\n")
    consumer = _parse(
        adapter, "pkg/use.py",
        "from pkg.a import f\n" + binding + "\nf()\n",
    )
    relations = adapter.resolve_references(
        consumer, SymbolIndex.from_parsed_files((provider, consumer))
    ).relations
    assert all(item.target_symbol_id != provider.symbols[0].symbol_id for item in relations
               if item.relation_type == "CALLS")


@pytest.mark.parametrize(("binding", "state"), [
    ("with resource as f:\n    pass", "unresolved"),
    ("try:\n    pass\nexcept Error as f:\n    pass", "unresolved"),
    ("value = lambda f: f", "resolved"),
    ("values = [f for f in items]", "resolved"),
])
def test_module_complex_bindings_shadow_only_their_own_scope(binding, state):
    adapter = PythonAdapter()
    provider = _parse(adapter, "pkg/a.py", "def f():\n    pass\n")
    consumer = _parse(adapter, "pkg/use.py", "from pkg.a import f\n" + binding + "\nf()\n")
    relations = adapter.resolve_references(
        consumer, SymbolIndex.from_parsed_files((provider, consumer))
    ).relations
    calls = [item for item in relations if item.relation_type == "CALLS"]
    assert [(item.target_reference, item.resolution_state) for item in calls] == [
        (None if state == "resolved" else "f", state),
    ]


def test_module_import_alias_is_not_visible_before_its_statement():
    adapter = PythonAdapter()
    provider = _parse(adapter, "pkg/a.py", "def f():\n    pass\n")
    consumer = _parse(
        adapter, "pkg/use.py", "f()\nfrom pkg.a import f\nf()\n",
    )
    relations = adapter.resolve_references(
        consumer, SymbolIndex.from_parsed_files((provider, consumer))
    ).relations
    calls = [item for item in relations if item.relation_type == "CALLS"]

    assert [(item.source_line, item.target_reference, item.resolution_state)
            for item in calls] == [
                (1, "f", "unresolved"),
                (3, None, "resolved"),
            ]


def test_import_aliases_follow_source_order_after_rebinding():
    adapter = PythonAdapter()
    first_provider = _parse(adapter, "pkg/a.py", "def f():\n    pass\n")
    second_provider = _parse(adapter, "pkg/b.py", "def f():\n    pass\n")
    consumer = _parse(
        adapter,
        "pkg/use.py",
        ("from pkg.a import f\n"
         "f()\n"
         "from pkg.b import f\n"
         "f()\n"),
    )
    relations = adapter.resolve_references(
        consumer, SymbolIndex.from_parsed_files(
            (first_provider, second_provider, consumer)
        )
    ).relations

    calls = [item for item in relations if item.relation_type == "CALLS"]
    assert [(item.source_line, item.target_reference, item.resolution_state)
            for item in calls] == [
                (2, None, "resolved"),
                (4, None, "resolved"),
            ]


def test_later_module_definition_does_not_hide_earlier_import_call():
    adapter = PythonAdapter()
    provider = _parse(adapter, "pkg/a.py", "def f():\n    pass\n")
    consumer = _parse(
        adapter,
        "pkg/use.py",
        "from pkg.a import f\nf()\ndef f():\n    pass\n",
    )

    relations = adapter.resolve_references(
        consumer, SymbolIndex.from_parsed_files((provider, consumer))
    ).relations

    assert [(item.source_line, item.target_reference, item.resolution_state)
            for item in relations if item.relation_type == "CALLS"] == [
                (2, None, "resolved"),
            ]


def test_class_bindings_shadow_module_aliases_and_bases():
    adapter = PythonAdapter()
    provider = _parse(
        adapter, "pkg/a.py", "def f():\n    pass\n\nclass Base:\n    pass\n",
    )
    consumer = _parse(
        adapter,
        "pkg/use.py",
        ("from pkg.a import Base, f\nBase = make()\n\nclass C(Base):\n"
         "    f = other\n    f()\n"),
    )
    relations = adapter.resolve_references(
        consumer, SymbolIndex.from_parsed_files((provider, consumer))
    ).relations

    assert all(item.target_reference not in {"pkg.a.Base", "pkg.a.f"}
               for item in relations if item.relation_type in {"CALLS", "INHERITS"})


def test_receiver_calls_require_matching_unshadowed_method_receiver():
    adapter = PythonAdapter()
    parsed = _parse(
        adapter,
        "pkg/use.py",
        ("class C:\n    def method(self):\n        pass\n\n"
         "    def instance(self):\n        self.method()\n\n"
         "    def no_receiver():\n        self.method()\n\n"
         "    @staticmethod\n    def static(self):\n        self.method()\n\n"
         "    @classmethod\n    def class_method(cls):\n        cls.method()\n\n"
         "    def rebound(self):\n        self = other\n        self.method()\n"),
    )
    relations = adapter.resolve_references(
        parsed, SymbolIndex.from_parsed_files((parsed,))
    ).relations
    calls = sorted(
        (item for item in relations if item.relation_type == "CALLS"),
        key=lambda item: item.source_line,
    )

    assert [(item.source_line, item.target_reference, item.resolution_state)
            for item in calls] == [
                (6, None, "resolved"),
                (9, "self.method", "unresolved"),
                (13, "self.method", "unresolved"),
                (17, None, "resolved"),
                (21, "self.method", "unresolved"),
            ]


def test_receiver_member_rebindings_suppress_method_resolution():
    adapter = PythonAdapter()
    parsed = _parse(
        adapter,
        "pkg/use.py",
        ("class C:\n"
         "    def method(self):\n"
         "        pass\n\n"
         "    def instance(self):\n"
         "        self.method = external\n"
         "        self.method()\n\n"
         "    @classmethod\n"
         "    def class_method(cls):\n"
         "        cls.method = external\n"
         "        cls.method()\n"),
    )
    relations = adapter.resolve_references(
        parsed, SymbolIndex.from_parsed_files((parsed,))
    ).relations

    calls = sorted(
        (item for item in relations if item.relation_type == "CALLS"),
        key=lambda item: item.source_line,
    )
    assert [(item.source_line, item.target_reference, item.resolution_state)
            for item in calls] == [
                (7, "self.method", "unresolved"),
                (12, "cls.method", "unresolved"),
            ]


def test_later_receiver_member_rebinding_does_not_hide_earlier_call():
    adapter = PythonAdapter()
    parsed = _parse(
        adapter,
        "pkg/use.py",
        ("class C:\n"
         "    def method(self):\n"
         "        pass\n\n"
         "    def instance(self):\n"
         "        self.method()\n"
         "        self.method = external\n"
         "        self.method()\n"),
    )

    relations = adapter.resolve_references(
        parsed, SymbolIndex.from_parsed_files((parsed,))
    ).relations

    assert [(item.source_line, item.target_reference, item.resolution_state)
            for item in relations if item.relation_type == "CALLS"] == [
                (6, None, "resolved"),
                (8, "self.method", "unresolved"),
            ]


def test_external_class_member_rebinding_invalidates_receiver_calls():
    adapter = PythonAdapter()
    parsed = _parse(
        adapter,
        "pkg/use.py",
        ("class C:\n"
         "    def method(self):\n"
         "        pass\n\n"
         "    def instance(self):\n"
         "        self.method()\n\n"
         "    @classmethod\n"
         "    def class_method(cls):\n"
         "        cls.method()\n\n"
         "C.method = external\n"),
    )

    relations = adapter.resolve_references(
        parsed, SymbolIndex.from_parsed_files((parsed,))
    ).relations

    calls = sorted(
        (item for item in relations if item.relation_type == "CALLS"),
        key=lambda item: item.source_line,
    )
    assert [(item.source_line, item.target_reference, item.resolution_state)
            for item in calls] == [
                (6, "self.method", "unresolved"),
                (10, "cls.method", "unresolved"),
            ]


def test_module_function_rebinding_invalidates_later_direct_call_only():
    adapter = PythonAdapter()
    parsed = _parse(
        adapter,
        "pkg/use.py",
        ("def ordinary():\n"
         "    pass\n\n"
         "ordinary()\n\n"
         "def f():\n"
         "    pass\n\n"
         "f = external\n"
         "f()\n"),
    )

    relations = adapter.resolve_references(
        parsed, SymbolIndex.from_parsed_files((parsed,))
    ).relations

    assert [(item.target_reference, item.resolution_state)
            for item in relations if item.relation_type == "CALLS"] == [
                (None, "resolved"),
                ("f", "unresolved"),
            ]


def test_module_member_rebinding_invalidates_imported_module_attribute_call():
    adapter = PythonAdapter()
    provider = _parse(adapter, "pkg/a.py", "def f():\n    pass\n")
    consumer = _parse(
        adapter,
        "pkg/use.py",
        "import pkg.a\npkg.a.f = replacement\npkg.a.f()\n",
    )

    relations = adapter.resolve_references(
        consumer, SymbolIndex.from_parsed_files((provider, consumer))
    ).relations

    assert [(item.target_reference, item.resolution_state)
            for item in relations if item.relation_type == "CALLS"] == [
                ("f", "unresolved"),
            ]


def test_rebound_imported_receiver_prefix_invalidates_attribute_call():
    adapter = PythonAdapter()
    provider = _parse(adapter, "pkg/a.py", "def f():\n    pass\n")
    consumer = _parse(
        adapter,
        "pkg/use.py",
        "import pkg.a\npkg.a = replacement\npkg.a.f()\n",
    )

    call = next(
        item for item in adapter.resolve_references(
            consumer, SymbolIndex.from_parsed_files((provider, consumer))
        ).relations
        if item.relation_type == "CALLS"
    )

    assert call.resolution_state == "unresolved"
    assert call.target_reference == "f"
    assert call.target_symbol_id is None
    assert call.target_module_id is None


def test_package_initializer_relative_imports_resolve_without_over_ascending():
    adapter = _PythonAdapter(
        "domain",
        {"pkg/__init__.py": "pkg", "pkg/a.py": "pkg.a"},
    )
    provider = adapter.parse_file(
        b"def f():\n    pass\n", "pkg/a.py"
    )
    package = adapter.parse_file(
        b"from . import a\n"
        b"from .a import f\n"
        b"from .. import missing\n",
        "pkg/__init__.py",
    )

    imports = [
        item for item in adapter.resolve_references(
            package, SymbolIndex.from_parsed_files((provider, package))
        ).relations
        if item.relation_type == "IMPORTS"
    ]
    by_binding = {item.binding_name: item for item in imports}

    assert by_binding["a"].resolution_state == "resolved"
    assert by_binding["a"].target_module_id == provider.file.module_id
    assert by_binding["a"].target_reference is None
    assert by_binding["f"].resolution_state == "resolved"
    assert by_binding["f"].target_symbol_id == provider.symbols[0].symbol_id
    assert by_binding["f"].target_reference is None
    assert by_binding["missing"].resolution_state == "unresolved"
    assert by_binding["missing"].target_module_id is None
    assert by_binding["missing"].target_symbol_id is None
    assert by_binding["missing"].target_reference is not None


def test_conditional_aliases_never_choose_textual_last_branch():
    adapter = PythonAdapter()
    first = _parse(adapter, "pkg/a.py", "def f():\n    pass\n")
    second = _parse(adapter, "pkg/b.py", "def f():\n    pass\n")
    conditional = _parse(
        adapter,
        "pkg/conditional.py",
        "def f():\n"
        "    pass\n"
        "if flag:\n"
        "    from pkg.a import f\n"
        "else:\n"
        "    from pkg.b import f\n"
        "f()\n",
    )
    sequential = _parse(
        adapter,
        "pkg/sequential.py",
        "from pkg.a import f\n"
        "from pkg.b import f\n"
        "f()\n",
    )
    index = SymbolIndex.from_parsed_files((
        first, second, conditional, sequential,
    ))

    conditional_call = next(
        item for item in adapter.resolve_references(
            conditional, index
        ).relations
        if item.relation_type == "CALLS"
    )
    sequential_call = next(
        item for item in adapter.resolve_references(
            sequential, index
        ).relations
        if item.relation_type == "CALLS"
    )

    assert conditional_call.resolution_state == "unresolved"
    assert conditional_call.target_reference == "f"
    assert conditional_call.target_symbol_id is None
    assert sequential_call.resolution_state == "resolved"
    assert sequential_call.target_symbol_id == second.symbols[0].symbol_id


def test_loop_and_match_imports_never_choose_textual_binding():
    adapter = PythonAdapter()
    first = _parse(
        adapter,
        "pkg/a.py",
        "def f():\n    pass\n\ndef g():\n    pass\n",
    )
    second = _parse(adapter, "pkg/b.py", "def g():\n    pass\n")
    consumer = _parse(
        adapter,
        "pkg/use.py",
        "while flag:\n"
        "    from pkg.a import f\n"
        "f()\n"
        "match value:\n"
        "    case 1:\n"
        "        from pkg.a import g\n"
        "    case _:\n"
        "        from pkg.b import g\n"
        "g()\n",
    )

    calls = [
        item for item in adapter.resolve_references(
            consumer, SymbolIndex.from_parsed_files((first, second, consumer))
        ).relations
        if item.relation_type == "CALLS"
    ]

    assert len(calls) == 2
    assert all(item.resolution_state == "unresolved" for item in calls)
    assert {item.target_reference for item in calls} == {"f", "g"}


def test_method_body_uses_class_rebinding_not_stale_module_alias():
    adapter = PythonAdapter()
    provider = _parse(
        adapter,
        "pkg/a.py",
        "class C:\n"
        "    def f(self):\n"
        "        pass\n",
    )
    consumer = _parse(
        adapter,
        "pkg/use.py",
        "from pkg.a import C\n"
        "class C:\n"
        "    def method(self):\n"
        "        C.f()\n",
    )

    call = next(
        item for item in adapter.resolve_references(
            consumer, SymbolIndex.from_parsed_files((provider, consumer))
        ).relations
        if item.relation_type == "CALLS"
    )

    assert call.resolution_state == "unresolved"
    assert call.target_symbol_id is None
    assert call.target_reference in {"C.f", "pkg.use.C.f"}


def test_class_body_alias_does_not_leak_into_method_scope():
    adapter = PythonAdapter()
    module_provider = _parse(
        adapter, "pkg/a.py", "def f():\n    pass\n"
    )
    class_provider = _parse(
        adapter, "pkg/b.py", "def f():\n    pass\n"
    )
    consumer = _parse(
        adapter,
        "pkg/use.py",
        "from pkg.a import f\n"
        "class C:\n"
        "    from pkg.b import f\n"
        "    f()\n"
        "    @f()\n"
        "    def method(self, value=f()):\n"
        "        f()\n",
    )
    relations = adapter.resolve_references(
        consumer,
        SymbolIndex.from_parsed_files((
            module_provider, class_provider, consumer,
        )),
    ).relations
    calls = {
        item.source_start_line: item
        for item in relations if item.relation_type == "CALLS"
    }

    for line in (4, 5, 6):
        assert calls[line].resolution_state == "resolved"
        assert calls[line].target_symbol_id == class_provider.symbols[0].symbol_id
    assert calls[7].resolution_state == "resolved"
    assert calls[7].target_symbol_id == module_provider.symbols[0].symbol_id


def test_compile_time_locals_shadow_same_file_candidates_inner_first():
    adapter = PythonAdapter()
    parsed = _parse(
        adapter,
        "pkg/use.py",
        "def f():\n"
        "    pass\n\n"
        "def use():\n"
        "    f()\n"
        "    f = other\n\n"
        "def outer():\n"
        "    f()\n"
        "    def inner():\n"
        "        f()\n"
        "        f = other\n\n"
        "def explicit_global():\n"
        "    global f\n"
        "    f()\n\n"
        "def enclosure():\n"
        "    f = other\n"
        "    def nested():\n"
        "        nonlocal f\n"
        "        f()\n",
    )
    calls = {
        item.source_start_line: item
        for item in adapter.resolve_references(
            parsed, SymbolIndex.from_parsed_files((parsed,))
        ).relations
        if item.relation_type == "CALLS"
    }

    assert calls[5].resolution_state == "unresolved"
    assert calls[9].resolution_state == "resolved"
    assert calls[11].resolution_state == "unresolved"
    assert calls[16].resolution_state == "resolved"
    assert calls[22].resolution_state == "unresolved"


def test_lambda_and_comprehension_bindings_shadow_same_file_candidates():
    adapter = PythonAdapter()
    parsed = _parse(
        adapter,
        "pkg/use.py",
        "def f():\n"
        "    pass\n"
        "value = lambda f: f()\n"
        "items = [f() for f in values]\n",
    )

    calls = [
        item for item in adapter.resolve_references(
            parsed, SymbolIndex.from_parsed_files((parsed,))
        ).relations
        if item.relation_type == "CALLS"
    ]

    assert len(calls) == 2
    assert all(item.resolution_state == "unresolved" for item in calls)
    assert all(item.target_symbol_id is None for item in calls)


def test_defaults_annotations_and_comprehensions_use_evaluation_scope():
    adapter = PythonAdapter()
    provider = _parse(adapter, "pkg/a.py", "def f():\n    pass\n")
    consumer = _parse(
        adapter,
        "pkg/use.py",
        "from pkg.a import f\n"
        "def run(value: f(), other=f()):\n"
        "    f()\n"
        "    f = other\n"
        "outermost = [f for f in f()]\n"
        "nested = [other for f in values for other in f()]\n"
        "element = [f() for f in values]\n",
    )

    calls = [
        item for item in adapter.resolve_references(
            consumer, SymbolIndex.from_parsed_files((provider, consumer))
        ).relations
        if item.relation_type == "CALLS"
    ]
    by_line = {}
    for call in calls:
        by_line.setdefault(call.source_start_line, []).append(call)

    assert len(by_line[2]) == 2
    assert all(
        item.resolution_state == "resolved"
        and item.target_symbol_id == provider.symbols[0].symbol_id
        for item in by_line[2]
    )
    assert by_line[3][0].resolution_state == "unresolved"
    assert by_line[5][0].resolution_state == "resolved"
    assert by_line[5][0].target_symbol_id == provider.symbols[0].symbol_id
    assert by_line[6][0].resolution_state == "unresolved"
    assert by_line[7][0].resolution_state == "unresolved"


def test_lambda_default_uses_enclosing_scope_but_body_parameter_is_local():
    adapter = PythonAdapter()
    provider = _parse(adapter, "pkg/a.py", "def f():\n    pass\n")
    consumer = _parse(
        adapter,
        "pkg/use.py",
        "from pkg.a import f\n"
        "value = lambda f=f(): f()\n",
    )

    calls = sorted(
        (
            item for item in adapter.resolve_references(
                consumer, SymbolIndex.from_parsed_files((provider, consumer))
            ).relations
            if item.relation_type == "CALLS"
        ),
        key=lambda item: item.source_start_byte,
    )

    assert len(calls) == 2
    assert calls[0].resolution_state == "resolved"
    assert calls[0].target_symbol_id == provider.symbols[0].symbol_id
    assert calls[1].resolution_state == "unresolved"
    assert calls[1].target_reference == "f"


def test_comprehension_targets_follow_exact_clause_evaluation_order():
    adapter = PythonAdapter()
    provider = _parse(
        adapter,
        "pkg/a.py",
        "def f():\n    pass\n\ndef x():\n    pass\n",
    )
    consumer = _parse(
        adapter,
        "pkg/use.py",
        "from pkg.a import f, x\n"
        "outer = [item for item in f() for f in values]\n"
        "prior = [item for f in values for item in f()]\n"
        "body = [f() for f in values]\n"
        "nested = [[y for y in x()] for x in values]\n",
    )

    calls = {
        item.source_start_line: item
        for item in adapter.resolve_references(
            consumer, SymbolIndex.from_parsed_files((provider, consumer))
        ).relations
        if item.relation_type == "CALLS"
    }

    assert calls[2].resolution_state == "resolved"
    assert calls[2].target_symbol_id == provider.symbols[0].symbol_id
    assert calls[3].resolution_state == "unresolved"
    assert calls[4].resolution_state == "unresolved"
    assert calls[5].resolution_state == "unresolved"


def test_receiver_dispatch_with_known_subclass_override_is_not_unique():
    adapter = PythonAdapter()
    parsed = _parse(
        adapter,
        "pkg/use.py",
        "class Base:\n"
        "    def target(self):\n"
        "        pass\n"
        "    def invoke(self):\n"
        "        self.target()\n"
        "    @classmethod\n"
        "    def invoke_class(cls):\n"
        "        cls.target()\n\n"
        "class Sub(Base):\n"
        "    def target(self):\n"
        "        pass\n\n"
        "class Unique:\n"
        "    def target(self):\n"
        "        pass\n"
        "    def invoke(self):\n"
        "        self.target()\n",
    )
    calls = {
        item.source_start_line: item
        for item in adapter.resolve_references(
            parsed, SymbolIndex.from_parsed_files((parsed,))
        ).relations
        if item.relation_type == "CALLS"
    }

    assert calls[5].resolution_state == "unresolved"
    assert calls[8].resolution_state == "unresolved"
    assert calls[18].resolution_state == "resolved"


@pytest.mark.parametrize("lookup_binding", [
    "    def __getattribute__(self, name):\n        return external\n",
    "    __getattribute__ = external\n",
    "    from pkg.external import __getattribute__\n",
])
def test_receiver_dispatch_with_custom_owner_attribute_lookup_is_unresolved(
    lookup_binding,
):
    adapter = PythonAdapter()
    parsed = _parse(
        adapter,
        "pkg/use.py",
        (
            "class Base:\n"
            + lookup_binding
            + "    def target(self):\n"
            + "        pass\n"
            + "    def invoke(self):\n"
            + "        self.target()\n"
            + "    @classmethod\n"
            + "    def invoke_class(cls):\n"
            + "        cls.target()\n"
        ),
    )

    calls = [
        item for item in adapter.resolve_references(
            parsed, SymbolIndex.from_parsed_files((parsed,))
        ).relations
        if item.relation_type == "CALLS"
    ]

    assert len(calls) == 2
    assert {item.resolution_state for item in calls} == {"unresolved"}
    assert {item.target_symbol_id for item in calls} == {None}


@pytest.mark.parametrize("lookup_binding", [
    "    def __getattribute__(self, name):\n        return external\n",
    "    __getattribute__ = external\n",
    "    from pkg.external import __getattribute__\n",
])
def test_reachable_subclass_custom_attribute_lookup_suppresses_dispatch(
    lookup_binding,
):
    adapter = _PythonAdapter(
        "domain",
        {"pkg/base.py": "pkg.base", "pkg/sub.py": "pkg.sub"},
    )
    base = adapter.parse_file(
        b"class Base:\n"
        b"    def target(self):\n"
        b"        pass\n"
        b"    def invoke(self):\n"
        b"        self.target()\n"
        b"    @classmethod\n"
        b"    def invoke_class(cls):\n"
        b"        cls.target()\n",
        "pkg/base.py",
    )
    subclass = adapter.parse_file(
        (
            "from pkg.base import Base\n"
            "class Sub(Base):\n"
            + lookup_binding
        ).encode(),
        "pkg/sub.py",
    )

    calls = [
        item for item in adapter.resolve_references(
            base, SymbolIndex.from_parsed_files((base, subclass))
        ).relations
        if item.relation_type == "CALLS"
    ]

    assert len(calls) == 2
    assert {item.resolution_state for item in calls} == {"unresolved"}
    assert {item.target_symbol_id for item in calls} == {None}


def test_unrelated_custom_attribute_lookup_does_not_suppress_dispatch():
    adapter = PythonAdapter()
    parsed = _parse(
        adapter,
        "pkg/use.py",
        "class Base:\n"
        "    def target(self):\n"
        "        pass\n"
        "    def invoke(self):\n"
        "        self.target()\n\n"
        "class Unrelated:\n"
        "    def __getattribute__(self, name):\n"
        "        return external\n",
    )

    call = next(
        item for item in adapter.resolve_references(
            parsed, SymbolIndex.from_parsed_files((parsed,))
        ).relations
        if item.relation_type == "CALLS"
        and item.source_symbol_id == next(
            symbol.symbol_id for symbol in parsed.symbols
            if symbol.local_name == "invoke"
        )
    )

    assert call.resolution_state == "resolved"
    assert call.target_symbol_id is not None
    assert call.target_reference is None


@pytest.mark.parametrize("unrelated_source", [
    (
        "class Base:\n"
        "    def __getattribute__(self, name):\n"
        "        return external\n"
    ),
    (
        "class Base:\n"
        "    pass\n"
        "class Sub(Base):\n"
        "    def __getattribute__(self, name):\n"
        "        return external\n"
    ),
])
def test_duplicate_qname_interceptor_does_not_cross_occurrences(
    unrelated_source,
):
    adapter = _PythonAdapter(
        "domain",
        {"left/base.py": "pkg.same", "right/base.py": "pkg.same"},
    )
    owner = adapter.parse_file(
        b"class Base:\n"
        b"    def target(self):\n"
        b"        pass\n"
        b"    def invoke(self):\n"
        b"        self.target()\n",
        "left/base.py",
    )
    unrelated = adapter.parse_file(
        unrelated_source.encode(),
        "right/base.py",
    )

    call = next(
        item for item in adapter.resolve_references(
            owner, SymbolIndex.from_parsed_files((owner, unrelated))
        ).relations
        if item.relation_type == "CALLS"
    )

    assert call.resolution_state == "resolved"
    assert call.target_symbol_id == next(
        item.symbol_id for item in owner.symbols
        if item.qualified_name == "pkg.same.Base.target"
    )
    assert call.target_reference is None


def test_qualified_duplicate_base_fans_out_receiver_uncertainty():
    adapter = _PythonAdapter(
        "domain",
        {
            "left/base.py": "pkg.base",
            "right/base.py": "pkg.base",
            "pkg/sub.py": "pkg.sub",
        },
    )
    source = (
        b"class Base:\n"
        b"    def target(self):\n"
        b"        pass\n"
        b"    def invoke(self):\n"
        b"        self.target()\n"
    )
    left = adapter.parse_file(source, "left/base.py")
    right = adapter.parse_file(source, "right/base.py")
    subclass = adapter.parse_file(
        b"from pkg.base import Base\n"
        b"class Sub(Base):\n"
        b"    __getattribute__ = external\n",
        "pkg/sub.py",
    )
    index = SymbolIndex.from_parsed_files((left, right, subclass))

    calls = [
        next(
            item for item in adapter.resolve_references(
                owner, index
            ).relations
            if item.relation_type == "CALLS"
        )
        for owner in (left, right)
    ]

    assert {item.resolution_state for item in calls} == {"unresolved"}
    assert {item.target_symbol_id for item in calls} == {None}
    assert {item.target_reference for item in calls} == {
        "pkg.base.Base.target"
    }


@pytest.mark.parametrize("mutation", [
    "Base.__getattribute__ = custom\n",
    "setattr(Base, '__getattribute__', custom)\n",
    "type.__setattr__(Base, '__getattribute__', custom)\n",
])
def test_post_class_owner_lookup_mutation_suppresses_receiver_dispatch(
    mutation,
):
    adapter = PythonAdapter()
    parsed = _parse(
        adapter,
        "pkg/use.py",
        (
            "class Base:\n"
            "    def target(self):\n"
            "        pass\n"
            "    def invoke(self):\n"
            "        self.target()\n"
            "    @classmethod\n"
            "    def invoke_class(cls):\n"
            "        cls.target()\n"
            + mutation
        ),
    )

    calls = [
        item for item in adapter.resolve_references(
            parsed, SymbolIndex.from_parsed_files((parsed,))
        ).relations
        if item.relation_type == "CALLS"
        and item.source_symbol_id in {
            symbol.symbol_id for symbol in parsed.symbols
            if symbol.local_name in {"invoke", "invoke_class"}
        }
    ]
    metadata = json.loads(next(
        item.metadata_json for item in parsed.symbols
        if item.qualified_name == "pkg.use.Base"
    ))

    assert "__getattribute__" in metadata["member_bindings"]
    assert len(calls) == 2
    assert {item.resolution_state for item in calls} == {"unresolved"}
    assert {item.target_symbol_id for item in calls} == {None}


@pytest.mark.parametrize("mutation", [
    "Sub.__getattribute__ = custom\n",
    "setattr(Sub, '__getattribute__', custom)\n",
    "type.__setattr__(Sub, '__getattribute__', custom)\n",
])
def test_post_class_subclass_lookup_mutation_suppresses_cross_file_dispatch(
    mutation,
):
    adapter = _PythonAdapter(
        "domain",
        {"pkg/base.py": "pkg.base", "pkg/sub.py": "pkg.sub"},
    )
    base = adapter.parse_file(
        b"class Base:\n"
        b"    def target(self):\n"
        b"        pass\n"
        b"    def invoke(self):\n"
        b"        self.target()\n"
        b"    @classmethod\n"
        b"    def invoke_class(cls):\n"
        b"        cls.target()\n",
        "pkg/base.py",
    )
    subclass = adapter.parse_file(
        (
            "from pkg.base import Base\n"
            "class Sub(Base):\n"
            "    pass\n"
            + mutation
        ).encode(),
        "pkg/sub.py",
    )

    calls = [
        item for item in adapter.resolve_references(
            base, SymbolIndex.from_parsed_files((base, subclass))
        ).relations
        if item.relation_type == "CALLS"
    ]
    metadata = json.loads(next(
        item.metadata_json for item in subclass.symbols
        if item.qualified_name == "pkg.sub.Sub"
    ))

    assert "__getattribute__" in metadata["member_bindings"]
    assert len(calls) == 2
    assert {item.resolution_state for item in calls} == {"unresolved"}
    assert {item.target_symbol_id for item in calls} == {None}


@pytest.mark.parametrize("mutation", [
    "Unrelated.__getattribute__ = custom\n",
    "setattr(Unrelated, '__getattribute__', custom)\n",
    "type.__setattr__(Unrelated, '__getattribute__', custom)\n",
])
def test_unrelated_post_class_lookup_mutation_does_not_suppress_dispatch(
    mutation,
):
    adapter = PythonAdapter()
    parsed = _parse(
        adapter,
        "pkg/use.py",
        (
            "class Base:\n"
            "    def target(self):\n"
            "        pass\n"
            "    def invoke(self):\n"
            "        self.target()\n"
            "class Unrelated:\n"
            "    pass\n"
            + mutation
        ),
    )

    call = next(
        item for item in adapter.resolve_references(
            parsed, SymbolIndex.from_parsed_files((parsed,))
        ).relations
        if item.relation_type == "CALLS"
        and item.source_symbol_id == next(
            symbol.symbol_id for symbol in parsed.symbols
            if symbol.local_name == "invoke"
        )
    )

    assert call.resolution_state == "resolved"
    assert call.target_symbol_id is not None
    assert call.target_reference is None


@pytest.mark.parametrize("mutation", [
    (
        "def mutate(Base):\n"
        "    Base.__getattribute__ = custom\n"
    ),
    (
        "def mutate():\n"
        "    Base = external\n"
        "    Base.__getattribute__ = custom\n"
    ),
])
def test_rebound_class_name_mutation_is_not_owner_evidence(mutation):
    adapter = PythonAdapter()
    parsed = _parse(
        adapter,
        "pkg/use.py",
        (
            "class Base:\n"
            "    def target(self):\n"
            "        pass\n"
            "    def invoke(self):\n"
            "        self.target()\n"
            + mutation
        ),
    )

    call = next(
        item for item in adapter.resolve_references(
            parsed, SymbolIndex.from_parsed_files((parsed,))
        ).relations
        if item.relation_type == "CALLS"
        and item.source_symbol_id == next(
            symbol.symbol_id for symbol in parsed.symbols
            if symbol.local_name == "invoke"
        )
    )
    metadata = json.loads(next(
        item.metadata_json for item in parsed.symbols
        if item.qualified_name == "pkg.use.Base"
    ))

    assert "__getattribute__" not in metadata["member_bindings"]
    assert call.resolution_state == "resolved"


def test_nested_same_name_class_does_not_truncate_module_mutation_binding():
    adapter = PythonAdapter()
    parsed = _parse(
        adapter,
        "pkg/use.py",
        "class Base:\n"
        "    def target(self):\n"
        "        pass\n"
        "    def invoke(self):\n"
        "        self.target()\n"
        "def factory():\n"
        "    class Base:\n"
        "        pass\n"
        "Base.__getattribute__ = custom\n",
    )

    call = next(
        item for item in adapter.resolve_references(
            parsed, SymbolIndex.from_parsed_files((parsed,))
        ).relations
        if item.relation_type == "CALLS"
    )
    metadata = json.loads(next(
        item.metadata_json for item in parsed.symbols
        if item.qualified_name == "pkg.use.Base"
    ))

    assert "__getattribute__" in metadata["member_bindings"]
    assert call.resolution_state == "unresolved"


@pytest.mark.parametrize("source", [
    (
        "def setattr(*args):\n"
        "    pass\n"
        "setattr(Base, '__getattribute__', custom)\n"
    ),
    (
        "setattr = custom\n"
        "setattr(Base, '__getattribute__', custom)\n"
    ),
    (
        "from hooks import setattr\n"
        "setattr(Base, '__getattribute__', custom)\n"
    ),
    (
        "def mutate(setattr):\n"
        "    setattr(Base, '__getattribute__', custom)\n"
    ),
    (
        "class type:\n"
        "    pass\n"
        "type.__setattr__(Base, '__getattribute__', custom)\n"
    ),
    (
        "type = custom\n"
        "type.__setattr__(Base, '__getattribute__', custom)\n"
    ),
    (
        "from hooks import type\n"
        "type.__setattr__(Base, '__getattribute__', custom)\n"
    ),
    (
        "def mutate(type):\n"
        "    type.__setattr__(Base, '__getattribute__', custom)\n"
    ),
])
def test_shadowed_mutation_helpers_do_not_produce_class_evidence(source):
    adapter = PythonAdapter()
    parsed = _parse(
        adapter,
        "pkg/use.py",
        (
            "class Base:\n"
            "    def target(self):\n"
            "        pass\n"
            "    def invoke(self):\n"
            "        self.target()\n"
            + source
        ),
    )

    call = next(
        item for item in adapter.resolve_references(
            parsed, SymbolIndex.from_parsed_files((parsed,))
        ).relations
        if item.relation_type == "CALLS"
        and item.source_symbol_id == next(
            symbol.symbol_id for symbol in parsed.symbols
            if symbol.local_name == "invoke"
        )
    )
    metadata = json.loads(next(
        item.metadata_json for item in parsed.symbols
        if item.qualified_name == "pkg.use.Base"
    ))

    assert "__getattribute__" not in metadata["member_bindings"]
    assert call.resolution_state == "resolved"


@pytest.mark.parametrize("unsafe_name", [
    "/private/secret.txt",
    "private_secret",
])
def test_post_class_mutation_never_persists_arbitrary_string(unsafe_name):
    adapter = PythonAdapter()
    parsed = _parse(
        adapter,
        "pkg/use.py",
        (
            "class Base:\n"
            "    def target(self):\n"
            "        pass\n"
            "    def invoke(self):\n"
            "        self.target()\n"
            f"setattr(Base, {unsafe_name!r}, custom)\n"
        ),
    )

    metadata_json = next(
        item.metadata_json for item in parsed.symbols
        if item.qualified_name == "pkg.use.Base"
    )
    call = next(
        item for item in adapter.resolve_references(
            parsed, SymbolIndex.from_parsed_files((parsed,))
        ).relations
        if item.relation_type == "CALLS"
        and item.source_symbol_id == next(
            symbol.symbol_id for symbol in parsed.symbols
            if symbol.local_name == "invoke"
        )
    )

    assert unsafe_name not in metadata_json
    assert getattr(parsed, "_adapter_evidence", ()) == ()
    assert call.resolution_state == "resolved"


@pytest.mark.parametrize("mutation", [
    (
        "    class Mutator:\n"
        "        Base.__getattribute__ = custom\n"
    ),
    (
        "    evidence = [setattr(Base, '__getattribute__', custom)\n"
        "                for item in values]\n"
    ),
])
def test_nested_execution_scope_skips_enclosing_class_namespace(mutation):
    adapter = PythonAdapter()
    parsed = _parse(
        adapter,
        "pkg/use.py",
        (
            "class Outer:\n"
            "    class Base:\n"
            "        def target(self):\n"
            "            pass\n"
            "        def invoke(self):\n"
            "            self.target()\n"
            + mutation
        ),
    )

    owner = next(
        item for item in parsed.symbols
        if item.qualified_name == "pkg.use.Outer.Base"
    )
    metadata = json.loads(owner.metadata_json)
    call = next(
        item for item in adapter.resolve_references(
            parsed, SymbolIndex.from_parsed_files((parsed,))
        ).relations
        if item.relation_type == "CALLS"
        and item.source_symbol_id == next(
            symbol.symbol_id for symbol in parsed.symbols
            if symbol.qualified_name == "pkg.use.Outer.Base.invoke"
        )
    )

    assert "__getattribute__" not in metadata["member_bindings"]
    assert call.resolution_state == "resolved"


def test_comprehension_outermost_iterable_uses_enclosing_class_namespace():
    adapter = PythonAdapter()
    parsed = _parse(
        adapter,
        "pkg/use.py",
        (
            "class Outer:\n"
            "    class Base:\n"
            "        def target(self):\n"
            "            pass\n"
            "        def invoke(self):\n"
            "            self.target()\n"
            "    evidence = [item for item in "
            "(setattr(Base, '__getattribute__', custom),)]\n"
        ),
    )

    metadata = json.loads(next(
        item.metadata_json for item in parsed.symbols
        if item.qualified_name == "pkg.use.Outer.Base"
    ))
    call = next(
        item for item in adapter.resolve_references(
            parsed, SymbolIndex.from_parsed_files((parsed,))
        ).relations
        if item.relation_type == "CALLS"
        and item.source_symbol_id == next(
            symbol.symbol_id for symbol in parsed.symbols
            if symbol.qualified_name == "pkg.use.Outer.Base.invoke"
        )
    )

    assert "__getattribute__" in metadata["member_bindings"]
    assert call.resolution_state == "unresolved"


@pytest.mark.parametrize("mutation", [
    "Base.__getattribute__ = custom\n",
    "setattr(Base, '__getattribute__', custom)\n",
    "type.__setattr__(Base, '__getattribute__', custom)\n",
])
def test_imported_post_class_lookup_mutation_suppresses_dispatch(mutation):
    adapter = _PythonAdapter(
        "domain",
        {"pkg/base.py": "pkg.base", "pkg/mutate.py": "pkg.mutate"},
    )
    base = adapter.parse_file(
        b"class Base:\n"
        b"    def target(self):\n"
        b"        pass\n"
        b"    def invoke(self):\n"
        b"        self.target()\n",
        "pkg/base.py",
    )
    mutation_file = adapter.parse_file(
        ("from pkg.base import Base\n" + mutation).encode(),
        "pkg/mutate.py",
    )

    call = next(
        item for item in adapter.resolve_references(
            base, SymbolIndex.from_parsed_files((base, mutation_file))
        ).relations
        if item.relation_type == "CALLS"
    )

    assert call.resolution_state == "unresolved"
    assert call.target_symbol_id is None
    assert call.target_reference == "pkg.base.Base.target"


def test_imported_lookup_mutation_fans_out_duplicate_class_occurrences():
    adapter = _PythonAdapter(
        "domain",
        {
            "left/base.py": "pkg.base",
            "right/base.py": "pkg.base",
            "pkg/mutate.py": "pkg.mutate",
        },
    )
    source = (
        b"class Base:\n"
        b"    def target(self):\n"
        b"        pass\n"
        b"    def invoke(self):\n"
        b"        self.target()\n"
    )
    left = adapter.parse_file(source, "left/base.py")
    right = adapter.parse_file(source, "right/base.py")
    mutation_file = adapter.parse_file(
        b"from pkg.base import Base\n"
        b"Base.__getattribute__ = custom\n",
        "pkg/mutate.py",
    )
    index = SymbolIndex.from_parsed_files((left, right, mutation_file))

    calls = [
        next(
            item for item in adapter.resolve_references(owner, index).relations
            if item.relation_type == "CALLS"
        )
        for owner in (left, right)
    ]

    assert {item.resolution_state for item in calls} == {"unresolved"}
    assert {item.target_symbol_id for item in calls} == {None}


@pytest.mark.parametrize("mutation_source", [
    (
        "if condition:\n"
        "    from pkg.left import Base\n"
        "else:\n"
        "    from pkg.right import Base\n"
        "Base.__getattribute__ = custom\n"
    ),
    (
        "try:\n"
        "    from pkg.left import Base\n"
        "except ImportError:\n"
        "    from pkg.right import Base\n"
        "Base.__getattribute__ = custom\n"
    ),
])
def test_conditional_imported_mutation_fans_out_possible_class_bindings(
    mutation_source,
):
    adapter = _PythonAdapter(
        "domain",
        {
            "pkg/left.py": "pkg.left",
            "pkg/right.py": "pkg.right",
            "pkg/mutate.py": "pkg.mutate",
        },
    )
    source = (
        b"class Base:\n"
        b"    def target(self):\n"
        b"        pass\n"
        b"    def invoke(self):\n"
        b"        self.target()\n"
    )
    left = adapter.parse_file(source, "pkg/left.py")
    right = adapter.parse_file(source, "pkg/right.py")
    mutation_file = adapter.parse_file(
        mutation_source.encode(), "pkg/mutate.py"
    )
    index = SymbolIndex.from_parsed_files((left, right, mutation_file))

    calls = [
        next(
            item for item in adapter.resolve_references(owner, index).relations
            if item.relation_type == "CALLS"
        )
        for owner in (left, right)
    ]

    assert {item.resolution_state for item in calls} == {"unresolved"}
    assert {item.target_symbol_id for item in calls} == {None}
    assert {
        evidence[3] for evidence in mutation_file._adapter_evidence
    } == {"pkg.left.Base", "pkg.right.Base"}


@pytest.mark.parametrize(("helper_setup", "mutation", "expected_state"), [
    (
        "if condition:\n    setattr = custom\n",
        "setattr(Base, '__getattribute__', custom)\n",
        "unresolved",
    ),
    (
        "setattr = custom\n",
        "setattr(Base, '__getattribute__', custom)\n",
        "resolved",
    ),
    (
        "try:\n    type = custom\nexcept LookupError:\n    pass\n",
        "type.__setattr__(Base, '__getattribute__', custom)\n",
        "unresolved",
    ),
    (
        "type = custom\n",
        "type.__setattr__(Base, '__getattribute__', custom)\n",
        "resolved",
    ),
])
def test_lookup_mutation_accepts_only_possible_builtin_helper_paths(
    helper_setup,
    mutation,
    expected_state,
):
    adapter = PythonAdapter()
    parsed = _parse(
        adapter,
        "pkg/use.py",
        (
            "class Base:\n"
            "    def target(self):\n"
            "        pass\n"
            "    def invoke(self):\n"
            "        self.target()\n"
            + helper_setup
            + mutation
        ),
    )

    call = next(
        item for item in adapter.resolve_references(
            parsed, SymbolIndex.from_parsed_files((parsed,))
        ).relations
        if item.relation_type == "CALLS"
        and item.source_symbol_id == next(
            symbol.symbol_id for symbol in parsed.symbols
            if symbol.qualified_name == "pkg.use.Base.invoke"
        )
    )

    assert call.resolution_state == expected_state


@pytest.mark.parametrize("mutation_source", [
    (
        "from external.pkg import Base\n"
        "Base.__getattribute__ = custom\n"
    ),
    (
        "from pkg.base import Base\n"
        "Base = external\n"
        "Base.__getattribute__ = custom\n"
    ),
    (
        "class Outer:\n"
        "    from pkg.base import Base\n"
        "    class Mutator:\n"
        "        Base.__getattribute__ = custom\n"
    ),
    (
        "class Outer:\n"
        "    from pkg.base import Base\n"
        "    evidence = [setattr(Base, '__getattribute__', custom)\n"
        "                for item in values]\n"
    ),
])
def test_unproven_imported_lookup_mutation_is_not_project_evidence(
    mutation_source,
):
    adapter = _PythonAdapter(
        "domain",
        {"pkg/base.py": "pkg.base", "pkg/mutate.py": "pkg.mutate"},
    )
    base = adapter.parse_file(
        b"class Base:\n"
        b"    def target(self):\n"
        b"        pass\n"
        b"    def invoke(self):\n"
        b"        self.target()\n",
        "pkg/base.py",
    )
    mutation_file = adapter.parse_file(
        mutation_source.encode(), "pkg/mutate.py"
    )

    call = next(
        item for item in adapter.resolve_references(
            base, SymbolIndex.from_parsed_files((base, mutation_file))
        ).relations
        if item.relation_type == "CALLS"
    )

    assert call.resolution_state == "resolved"
    assert call.target_symbol_id is not None


def test_imported_mutation_in_comprehension_outer_iterable_is_evidence():
    adapter = _PythonAdapter(
        "domain",
        {"pkg/base.py": "pkg.base", "pkg/mutate.py": "pkg.mutate"},
    )
    base = adapter.parse_file(
        b"class Base:\n"
        b"    def target(self):\n"
        b"        pass\n"
        b"    def invoke(self):\n"
        b"        self.target()\n",
        "pkg/base.py",
    )
    mutation_file = adapter.parse_file(
        b"class Outer:\n"
        b"    from pkg.base import Base\n"
        b"    evidence = [item for item in "
        b"(setattr(Base, '__getattribute__', custom),)]\n",
        "pkg/mutate.py",
    )

    call = next(
        item for item in adapter.resolve_references(
            base, SymbolIndex.from_parsed_files((base, mutation_file))
        ).relations
        if item.relation_type == "CALLS"
    )

    assert call.resolution_state == "unresolved"


@pytest.mark.parametrize(("import_statement", "expected_state"), [
    ("import pkg.base as Base\n", "resolved"),
    ("from pkg import base as Base\n", "unresolved"),
])
def test_imported_mutation_preserves_module_or_member_target_kind(
    import_statement,
    expected_state,
):
    adapter = _PythonAdapter(
        "domain",
        {
            "pkg.py": "pkg",
            "pkg/base.py": "pkg.base",
            "pkg/mutate.py": "pkg.mutate",
        },
    )
    owner = adapter.parse_file(
        b"class base:\n"
        b"    def target(self):\n"
        b"        pass\n"
        b"    def invoke(self):\n"
        b"        self.target()\n",
        "pkg.py",
    )
    module = adapter.parse_file(b"VALUE = 1\n", "pkg/base.py")
    mutation_file = adapter.parse_file(
        (import_statement + "Base.__getattribute__ = custom\n").encode(),
        "pkg/mutate.py",
    )

    call = next(
        item for item in adapter.resolve_references(
            owner,
            SymbolIndex.from_parsed_files((owner, module, mutation_file)),
        ).relations
        if item.relation_type == "CALLS"
    )

    assert call.resolution_state == expected_state
    if import_statement.startswith("import "):
        assert mutation_file._adapter_evidence == ()


def test_adapter_evidence_transport_is_neutral_deterministic_and_source_free():
    adapter = _PythonAdapter(
        "domain",
        {"pkg/base.py": "pkg.base", "pkg/mutate.py": "pkg.mutate"},
    )
    mutation_file = adapter.parse_file(
        b"from pkg.base import Base\n"
        b"Base.__getattribute__ = first\n"
        b"Base.__getattribute__ = second\n",
        "pkg/mutate.py",
    )
    index = SymbolIndex.from_parsed_files((mutation_file,))

    assert index._adapter_evidence == (
        (
            "python",
            "class_member_mutation",
            "member",
            "pkg.base.Base",
            "__getattribute__",
        ),
    )
    assert all(
        "/" not in field and "\\" not in field
        for evidence in index._adapter_evidence for field in evidence
    )
    core_source = inspect.getsource(resolver_module)
    assert "class_member_mutation" not in core_source
    assert "__getattribute__" not in core_source
    assert "PythonAdapter" not in core_source


def test_receiver_dispatch_with_subclass_assignment_override_is_not_unique():
    adapter = PythonAdapter()
    parsed = _parse(
        adapter,
        "pkg/use.py",
        "class Base:\n"
        "    def target(self):\n"
        "        pass\n"
        "    def invoke(self):\n"
        "        self.target()\n\n"
        "class Child(Base):\n"
        "    target = external\n",
    )

    call = next(
        item for item in adapter.resolve_references(
            parsed, SymbolIndex.from_parsed_files((parsed,))
        ).relations
        if item.relation_type == "CALLS"
    )

    assert call.resolution_state == "unresolved"
    assert call.target_reference == "self.target"
    assert call.target_symbol_id is None


def test_receiver_dispatch_with_subclass_import_override_is_not_unique():
    adapter = PythonAdapter()
    parsed = _parse(
        adapter,
        "pkg/use.py",
        "class Base:\n"
        "    def target(self):\n"
        "        pass\n"
        "    def invoke(self):\n"
        "        self.target()\n\n"
        "class Child(Base):\n"
        "    from pkg.external import target\n",
    )

    call = next(
        item for item in adapter.resolve_references(
            parsed, SymbolIndex.from_parsed_files((parsed,))
        ).relations
        if item.relation_type == "CALLS"
    )

    assert call.resolution_state == "unresolved"
    assert call.target_reference == "self.target"
    assert call.target_symbol_id is None


@pytest.mark.parametrize(("import_line", "base_name"), [
    ("from pkg.base import Base", "Base"),
    ("from pkg.base import Base as Parent", "Parent"),
])
def test_receiver_dispatch_cross_file_override_uses_project_uniqueness_gate(
    import_line, base_name
):
    adapter = _PythonAdapter(
        "domain",
        {"pkg/base.py": "pkg.base", "pkg/sub.py": "pkg.sub"},
    )
    base = adapter.parse_file(
        b"class Base:\n"
        b"    def target(self):\n"
        b"        pass\n"
        b"    def invoke(self):\n"
        b"        self.target()\n",
        "pkg/base.py",
    )
    subclass = adapter.parse_file(
        (
            import_line + "\n"
            + "class Sub(" + base_name + "):\n"
            + "    def target(self):\n"
            + "        pass\n"
        ).encode(),
        "pkg/sub.py",
    )

    overridden = next(
        item for item in adapter.resolve_references(
            base, SymbolIndex.from_parsed_files((base, subclass))
        ).relations
        if item.relation_type == "CALLS"
    )
    unique = next(
        item for item in adapter.resolve_references(
            base, SymbolIndex.from_parsed_files((base,))
        ).relations
        if item.relation_type == "CALLS"
    )

    assert overridden.resolution_state == "unresolved"
    assert overridden.target_reference == "pkg.base.Base.target"
    assert overridden.target_symbol_id is None
    assert unique.resolution_state == "resolved"
    assert unique.target_symbol_id == next(
        item.symbol_id for item in base.symbols
        if item.qualified_name == "pkg.base.Base.target"
    )


@pytest.mark.parametrize("class_body", [
    "    target = external\n",
    "    target = external\n    del target\n",
    "    from pkg.external import target\n",
    "    if enabled:\n        target = external\n",
])
def test_cross_file_subclass_class_binding_suppresses_receiver_dispatch(
    class_body
):
    adapter = _PythonAdapter(
        "domain",
        {"pkg/base.py": "pkg.base", "pkg/sub.py": "pkg.sub"},
    )
    base = adapter.parse_file(
        b"class Base:\n"
        b"    def target(self):\n"
        b"        pass\n"
        b"    def invoke(self):\n"
        b"        self.target()\n",
        "pkg/base.py",
    )
    subclass = adapter.parse_file(
        (
            "from pkg.base import Base as Parent\n"
            "class Sub(Parent):\n"
            + class_body
        ).encode(),
        "pkg/sub.py",
    )

    call = next(
        item for item in adapter.resolve_references(
            base, SymbolIndex.from_parsed_files((base, subclass))
        ).relations
        if item.relation_type == "CALLS"
    )

    assert call.resolution_state == "unresolved"
    assert call.target_symbol_id is None
    assert call.target_reference == "pkg.base.Base.target"


def test_cross_file_subclass_method_local_does_not_override_class_member():
    adapter = _PythonAdapter(
        "domain",
        {"pkg/base.py": "pkg.base", "pkg/sub.py": "pkg.sub"},
    )
    base = adapter.parse_file(
        b"class Base:\n"
        b"    def target(self):\n"
        b"        pass\n"
        b"    def invoke(self):\n"
        b"        self.target()\n",
        "pkg/base.py",
    )
    subclass = adapter.parse_file(
        b"from pkg.base import Base as Parent\n"
        b"class Sub(Parent):\n"
        b"    def other(self):\n"
        b"        target = external\n",
        "pkg/sub.py",
    )

    call = next(
        item for item in adapter.resolve_references(
            base, SymbolIndex.from_parsed_files((base, subclass))
        ).relations
        if item.relation_type == "CALLS"
    )

    assert call.resolution_state == "resolved"
    assert call.target_symbol_id is not None
    assert call.target_reference is None


@pytest.mark.parametrize("conditional_import", [
    "if enabled:\n    from pkg.base import Base as Parent\n",
    "while enabled:\n    from pkg.base import Base as Parent\n",
    "try:\n    from pkg.base import Base as Parent\nexcept ImportError:\n    pass\n",
    "match enabled:\n    case True:\n        from pkg.base import Base as Parent\n",
])
def test_conditional_base_alias_remains_possible_subclass_evidence(
    conditional_import,
):
    adapter = _PythonAdapter(
        "domain",
        {"pkg/base.py": "pkg.base", "pkg/sub.py": "pkg.sub"},
    )
    base = adapter.parse_file(
        b"class Base:\n"
        b"    def target(self):\n"
        b"        pass\n"
        b"    def invoke(self):\n"
        b"        self.target()\n",
        "pkg/base.py",
    )
    subclass = adapter.parse_file(
        (
            conditional_import
            + "class Sub(Parent):\n"
            + "    def target(self):\n"
            + "        pass\n"
        ).encode(),
        "pkg/sub.py",
    )

    call = next(
        item for item in adapter.resolve_references(
            base, SymbolIndex.from_parsed_files((base, subclass))
        ).relations
        if item.relation_type == "CALLS"
    )
    subclass_metadata = json.loads(next(
        item.metadata_json for item in subclass.symbols
        if item.qualified_name == "pkg.sub.Sub"
    ))

    assert "pkg.base.Base" in subclass_metadata["base_references"]
    assert call.resolution_state == "unresolved"
    assert call.target_symbol_id is None
    assert call.target_reference == "pkg.base.Base.target"


def test_function_local_conditional_base_alias_is_subclass_evidence():
    adapter = _PythonAdapter(
        "domain",
        {"pkg/base.py": "pkg.base", "pkg/sub.py": "pkg.sub"},
    )
    base = adapter.parse_file(
        b"class Base:\n"
        b"    def target(self):\n"
        b"        pass\n"
        b"    def invoke(self):\n"
        b"        self.target()\n",
        "pkg/base.py",
    )
    subclass = adapter.parse_file(
        b"def factory(enabled):\n"
        b"    if enabled:\n"
        b"        from pkg.base import Base as Parent\n"
        b"    class Sub(Parent):\n"
        b"        def target(self):\n"
        b"            pass\n",
        "pkg/sub.py",
    )

    call = next(
        item for item in adapter.resolve_references(
            base, SymbolIndex.from_parsed_files((base, subclass))
        ).relations
        if item.relation_type == "CALLS"
    )

    assert call.resolution_state == "unresolved"
    assert call.target_symbol_id is None


def test_class_local_conditional_base_alias_is_subclass_evidence():
    adapter = _PythonAdapter(
        "domain",
        {"pkg/base.py": "pkg.base", "pkg/sub.py": "pkg.sub"},
    )
    base = adapter.parse_file(
        b"class Base:\n"
        b"    def target(self):\n"
        b"        pass\n"
        b"    def invoke(self):\n"
        b"        self.target()\n",
        "pkg/base.py",
    )
    subclass = adapter.parse_file(
        b"class Namespace:\n"
        b"    if enabled:\n"
        b"        from pkg.base import Base as Parent\n"
        b"    class Sub(Parent):\n"
        b"        def target(self):\n"
        b"            pass\n",
        "pkg/sub.py",
    )

    call = next(
        item for item in adapter.resolve_references(
            base, SymbolIndex.from_parsed_files((base, subclass))
        ).relations
        if item.relation_type == "CALLS"
    )

    assert call.resolution_state == "unresolved"
    assert call.target_symbol_id is None


def test_class_match_capture_is_recorded_as_override_binding():
    adapter = _PythonAdapter(
        "domain",
        {"pkg/base.py": "pkg.base", "pkg/sub.py": "pkg.sub"},
    )
    base = adapter.parse_file(
        b"class Base:\n"
        b"    def target(self):\n"
        b"        pass\n"
        b"    def invoke(self):\n"
        b"        self.target()\n",
        "pkg/base.py",
    )
    subclass = adapter.parse_file(
        b"from pkg.base import Base\n"
        b"class Sub(Base):\n"
        b"    match value:\n"
        b"        case {'target': target}:\n"
        b"            pass\n",
        "pkg/sub.py",
    )

    call = next(
        item for item in adapter.resolve_references(
            base, SymbolIndex.from_parsed_files((base, subclass))
        ).relations
        if item.relation_type == "CALLS"
    )
    subclass_metadata = json.loads(next(
        item.metadata_json for item in subclass.symbols
        if item.qualified_name == "pkg.sub.Sub"
    ))

    assert "target" in subclass_metadata["member_bindings"]
    assert call.resolution_state == "unresolved"
    assert call.target_symbol_id is None


@pytest.mark.parametrize("pattern", [
    "Point() as target",
    "[*target]",
    "{**target}",
])
def test_class_match_capture_forms_are_override_bindings(pattern):
    adapter = _PythonAdapter(
        "domain",
        {"pkg/base.py": "pkg.base", "pkg/sub.py": "pkg.sub"},
    )
    base = adapter.parse_file(
        b"class Base:\n"
        b"    def target(self):\n"
        b"        pass\n"
        b"    def invoke(self):\n"
        b"        self.target()\n",
        "pkg/base.py",
    )
    subclass = adapter.parse_file(
        (
            "from pkg.base import Base\n"
            "class Sub(Base):\n"
            "    match value:\n"
            f"        case {pattern}:\n"
            "            pass\n"
        ).encode(),
        "pkg/sub.py",
    )

    call = next(
        item for item in adapter.resolve_references(
            base, SymbolIndex.from_parsed_files((base, subclass))
        ).relations
        if item.relation_type == "CALLS"
    )
    metadata = json.loads(next(
        item.metadata_json for item in subclass.symbols
        if item.qualified_name == "pkg.sub.Sub"
    ))

    assert "target" in metadata["member_bindings"]
    assert "Point" not in metadata["member_bindings"]
    assert call.resolution_state == "unresolved"
    assert call.target_symbol_id is None


@pytest.mark.parametrize("pattern", [
    "_",
    "Point(label=Color.RED)",
])
def test_non_capture_match_names_are_not_class_bindings(pattern):
    adapter = _PythonAdapter(
        "domain",
        {"pkg/base.py": "pkg.base", "pkg/sub.py": "pkg.sub"},
    )
    base = adapter.parse_file(
        b"class Base:\n"
        b"    def target(self):\n"
        b"        pass\n"
        b"    def invoke(self):\n"
        b"        self.target()\n",
        "pkg/base.py",
    )
    subclass = adapter.parse_file(
        (
            "from pkg.base import Base\n"
            "class Sub(Base):\n"
            "    match value:\n"
            f"        case {pattern}:\n"
            "            pass\n"
        ).encode(),
        "pkg/sub.py",
    )

    call = next(
        item for item in adapter.resolve_references(
            base, SymbolIndex.from_parsed_files((base, subclass))
        ).relations
        if item.relation_type == "CALLS"
    )
    metadata = json.loads(next(
        item.metadata_json for item in subclass.symbols
        if item.qualified_name == "pkg.sub.Sub"
    ))

    assert not set(metadata.get("member_bindings", ())) & {
        "_", "Point", "label", "Color", "RED",
    }
    assert call.resolution_state == "resolved"
    assert call.target_symbol_id is not None


def test_qualified_base_matching_never_uses_last_name_segment():
    adapter = _PythonAdapter(
        "domain",
        {
            "pkg/base.py": "pkg.base",
            "pkg/sub.py": "pkg.sub",
            "other/mod.py": "other.mod",
        },
    )
    base = adapter.parse_file(
        b"class Base:\n"
        b"    def target(self):\n"
        b"        pass\n"
        b"    def invoke(self):\n"
        b"        self.target()\n",
        "pkg/base.py",
    )
    unrelated_base = adapter.parse_file(
        b"class Base:\n    pass\n",
        "other/mod.py",
    )
    subclass = adapter.parse_file(
        b"import other.mod\n"
        b"class Sub(other.mod.Base):\n"
        b"    def target(self):\n"
        b"        pass\n",
        "pkg/sub.py",
    )

    call = next(
        item for item in adapter.resolve_references(
            base,
            SymbolIndex.from_parsed_files((base, unrelated_base, subclass)),
        ).relations
        if item.relation_type == "CALLS"
    )

    assert call.resolution_state == "resolved"
    assert call.target_symbol_id == next(
        item.symbol_id for item in base.symbols
        if item.qualified_name == "pkg.base.Base.target"
    )


def test_rooted_import_base_never_duplicates_qualified_suffix():
    adapter = _PythonAdapter(
        "domain",
        {
            "other/mod.py": "other.mod",
            "other/mod/mod.py": "other.mod.mod",
            "pkg/sub.py": "pkg.sub",
        },
    )
    direct_base = adapter.parse_file(
        b"class Base:\n    pass\n",
        "other/mod.py",
    )
    unrelated_base = adapter.parse_file(
        b"class Base:\n"
        b"    def target(self):\n"
        b"        pass\n"
        b"    def invoke(self):\n"
        b"        self.target()\n",
        "other/mod/mod.py",
    )
    subclass = adapter.parse_file(
        b"import other.mod\n"
        b"class Sub(other.mod.Base):\n"
        b"    def target(self):\n"
        b"        pass\n",
        "pkg/sub.py",
    )

    call = next(
        item for item in adapter.resolve_references(
            unrelated_base,
            SymbolIndex.from_parsed_files((
                direct_base, unrelated_base, subclass,
            )),
        ).relations
        if item.relation_type == "CALLS"
    )
    metadata = json.loads(next(
        item.metadata_json for item in subclass.symbols
        if item.qualified_name == "pkg.sub.Sub"
    ))

    assert metadata["base_references"] == ["other.mod.Base"]
    assert call.resolution_state == "resolved"
    assert call.target_symbol_id == next(
        item.symbol_id for item in unrelated_base.symbols
        if item.qualified_name == "other.mod.mod.Base.target"
    )


def test_nested_class_method_is_not_direct_subclass_override():
    adapter = _PythonAdapter(
        "domain",
        {"pkg/base.py": "pkg.base", "pkg/sub.py": "pkg.sub"},
    )
    base = adapter.parse_file(
        b"class Base:\n"
        b"    def target(self):\n"
        b"        pass\n"
        b"    def invoke(self):\n"
        b"        self.target()\n",
        "pkg/base.py",
    )
    subclass = adapter.parse_file(
        b"from pkg.base import Base\n"
        b"class Sub(Base):\n"
        b"    class Nested:\n"
        b"        def target(self):\n"
        b"            pass\n",
        "pkg/sub.py",
    )

    call = next(
        item for item in adapter.resolve_references(
            base, SymbolIndex.from_parsed_files((base, subclass))
        ).relations
        if item.relation_type == "CALLS"
    )

    assert call.resolution_state == "resolved"
    assert call.target_symbol_id == next(
        item.symbol_id for item in base.symbols
        if item.qualified_name == "pkg.base.Base.target"
    )


def test_local_module_member_rebinding_invalidates_attribute_call():
    adapter = PythonAdapter()
    provider = _parse(adapter, "pkg/a.py", "def f():\n    pass\n")
    consumer = _parse(
        adapter,
        "pkg/use.py",
        "import pkg.a\ndef run():\n    pkg.a.f = replacement\n    pkg.a.f()\n",
    )

    relations = adapter.resolve_references(
        consumer, SymbolIndex.from_parsed_files((provider, consumer))
    ).relations

    assert [(item.target_reference, item.resolution_state)
            for item in relations if item.relation_type == "CALLS"] == [
                ("f", "unresolved"),
            ]


@pytest.mark.parametrize("binding", [
    "del f",
    "f, other = values",
    "[f, other] = values",
    "(f := other)",
])
def test_imported_alias_is_invalidated_by_all_local_rebindings(binding):
    adapter = PythonAdapter()
    provider = _parse(adapter, "pkg/a.py", "def f():\n    pass\n")
    consumer = _parse(
        adapter,
        "pkg/use.py",
        "from pkg.a import f\n" + binding + "\nf()\n",
    )

    relations = adapter.resolve_references(
        consumer, SymbolIndex.from_parsed_files((provider, consumer))
    ).relations

    assert [(item.target_reference, item.resolution_state)
            for item in relations if item.relation_type == "CALLS"] == [
                ("f", "unresolved"),
            ]


@pytest.mark.parametrize("body", [
    "(lambda {receiver}: {receiver}.method())(other)",
    "[{receiver}.method() for {receiver} in items]",
    "with resource as {receiver}:\n        {receiver}.method()",
    "try:\n        pass\n    except Error as {receiver}:\n        {receiver}.method()",
])
@pytest.mark.parametrize(("receiver", "decorator"), [
    ("self", ""),
    ("cls", "@classmethod\n    "),
])
def test_receiver_shadows_in_nested_and_local_bindings_are_unresolved(
    body, receiver, decorator
):
    adapter = PythonAdapter()
    parsed = _parse(
        adapter,
        "pkg/use.py",
        ("class C:\n"
         "    def method(self):\n"
         "        pass\n\n"
         "    " + decorator + "def instance(" + receiver + "):\n        "
         + body.format(receiver=receiver).replace("\n", "\n        ") + "\n"),
    )

    relations = adapter.resolve_references(
        parsed, SymbolIndex.from_parsed_files((parsed,))
    ).relations

    assert all(item.target_reference == receiver + ".method"
               and item.resolution_state == "unresolved"
               for item in relations if item.relation_type == "CALLS")


def test_module_class_member_fallback_respects_lexical_source_bindings():
    adapter = PythonAdapter()
    parsed = _parse(
        adapter,
        "pkg/use.py",
        ("A.method()\n\n"
         "class A:\n"
         "    @staticmethod\n"
         "    def method():\n"
         "        pass\n\n"
         "def parameter(A):\n"
         "    A.method()\n\n"
         "def local():\n"
         "    A = other\n"
         "    A.method()\n\n"
         "A.method()\n"),
    )

    relations = adapter.resolve_references(
        parsed, SymbolIndex.from_parsed_files((parsed,))
    ).relations

    assert sorted((item.source_line, item.target_reference, item.resolution_state)
                  for item in relations if item.relation_type == "CALLS") == [
                (1, "A.method", "unresolved"),
                (9, "A.method", "unresolved"),
                (13, "A.method", "unresolved"),
                (15, None, "resolved"),
            ]


def test_forward_top_level_function_call_is_unresolved():
    adapter = PythonAdapter()
    parsed = _parse(
        adapter,
        "pkg/use.py",
        "later()\n\ndef later():\n    pass\n",
    )

    relations = adapter.resolve_references(
        parsed, SymbolIndex.from_parsed_files((parsed,))
    ).relations

    assert [(item.target_reference, item.resolution_state)
            for item in relations if item.relation_type == "CALLS"] == [
                ("later", "unresolved"),
            ]


def test_forward_class_base_is_unresolved():
    adapter = PythonAdapter()
    parsed = _parse(
        adapter,
        "pkg/use.py",
        "class Child(Base):\n    pass\n\nclass Base:\n    pass\n",
    )

    relations = adapter.resolve_references(
        parsed, SymbolIndex.from_parsed_files((parsed,))
    ).relations

    assert [(item.target_reference, item.resolution_state)
            for item in relations if item.relation_type == "INHERITS"] == [
                ("Base", "unresolved"),
            ]


@pytest.mark.parametrize("monkeypatch", [
    "setattr(C, 'method', external)",
    "type.__setattr__(C, 'method', external)",
])
def test_dynamic_class_member_monkeypatch_invalidates_receiver_calls(monkeypatch):
    adapter = PythonAdapter()
    parsed = _parse(
        adapter,
        "pkg/use.py",
        ("class C:\n"
         "    def method(self):\n"
         "        pass\n\n"
         "    def instance(self):\n"
         "        self.method()\n\n"
         + monkeypatch + "\n"),
    )

    relations = adapter.resolve_references(
        parsed, SymbolIndex.from_parsed_files((parsed,))
    ).relations

    calls = sorted(
        (item for item in relations if item.relation_type == "CALLS"),
        key=lambda item: item.source_line,
    )
    assert [(item.target_reference, item.resolution_state)
            for item in calls] == [
                ("self.method", "unresolved"),
                ("setattr", "unresolved")
                if monkeypatch.startswith("setattr")
                else ("type.__setattr__", "unresolved"),
            ]
