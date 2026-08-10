"""Contract tests for conservative code-graph reference resolution."""
from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from iwiki_mcp.codegraph.languages.python import PythonAdapter
from iwiki_mcp.codegraph.models import ReferenceRecord, SymbolRecord
from iwiki_mcp.codegraph.resolver import resolve_references
from iwiki_mcp.codegraph.resolver import SymbolIndex


def _parse(adapter, path, source):
    return adapter.parse_file(source.encode(), path)


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
        (relation.target_reference, relation.resolution_state)
        for relation in result.relations
    }
    assert ("pkg.factory.make", "ambiguous") in states
    assert ("factory.make", "unresolved") in states
    assert ("external.call", "unresolved") in states
    assert all(relation.target_reference for relation in result.relations)


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
            item.source_symbol_id or item.source_file_id,
            item.relation_type,
            item.source_line or -1,
            item.target_symbol_id or item.target_reference or "",
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

    assert any(item.target_reference == "pkg.a.known"
               and item.resolution_state == "resolved"
               for item in relations)
    assert any(item.target_reference == "pkg.a.missing"
               and item.resolution_state == "partially_resolved"
               for item in relations)
    assert any(item.target_reference == "external.package.foreign"
               and item.resolution_state == "unresolved"
               for item in relations)
    assert any(item.target_reference == "pkg.a.*"
               and item.resolution_state == "partially_resolved"
               for item in relations)
    assert any(item.target_reference == "models.Base" and item.resolution_state == "resolved"
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

    assert any(item.target_reference == "pkg.a.known"
               and item.resolution_state == "resolved"
               for item in imported_relations)
    assert any(item.target_reference == "pkg.local.A.method"
               and item.resolution_state == "resolved"
               for item in local_relations)
    module_only = _parse(adapter, "pkg/empty.py", "import pkg.a\n")
    module_symbol = _parse(adapter, "pkg/a.py", "def x():\n pass\n")
    module_index = SymbolIndex.from_parsed_files((module_symbol, module_only))
    module_relation = next(
        item for item in adapter.resolve_references(module_only, module_index).relations
        if item.target_reference == "pkg.a"
    )
    assert module_relation.target_symbol_id is None
    assert module_relation.resolution_state == "resolved"


def test_same_line_and_file_scoped_references_have_stable_distinct_relation_ids():
    index = SymbolIndex.from_symbols(())
    references = (
        ReferenceRecord(None, "parse:file", "CALLS", "external.a", 4, 10),
        ReferenceRecord(None, "parse:file", "CALLS", "external.b", 4, 20),
        ReferenceRecord(None, "parse:file", "CALLS", "external.c", None, None),
    )
    first = resolve_references("python", references, index)
    second = resolve_references("python", references, index)

    assert len({item.relation_id for item in first}) == 3
    assert first == second


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
    assert [(item.source_line, item.target_reference, item.resolution_state)
            for item in relations if item.relation_type == "CALLS"] == [
                (4, "f", "unresolved"),
                (6, "f", "unresolved"),
                (9, "f", "unresolved"),
                (12, "pkg.use.defined.f", "resolved"),
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
                (2, "pkg.a.f", "resolved"),
                (3, "f", "unresolved"),
                (4, "pkg.a.f", "resolved"),
                (5, "f", "unresolved"),
                (8, "pkg.a.f", "resolved"),
            ]


def test_relation_sorting_uses_byte_offset_within_same_line():
    relations = resolve_references("python", (
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
                (3, "pkg.a.f", "resolved"),
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
                (3, "pkg.a.f", "resolved"),
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
    assert all(item.target_reference == "pkg.a.f"
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
    assert all(item.target_reference != "pkg.a.f" for item in relations
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
        ("pkg.a.f" if state == "resolved" else "f", state),
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
                (3, "pkg.a.f", "resolved"),
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
                (2, "pkg.a.f", "resolved"),
                (4, "pkg.b.f", "resolved"),
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
                (2, "pkg.a.f", "resolved"),
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
    calls = [item for item in relations if item.relation_type == "CALLS"]

    assert [(item.source_line, item.target_reference, item.resolution_state)
            for item in calls] == [
                (6, "pkg.use.C.method", "resolved"),
                (9, "self.method", "unresolved"),
                (13, "self.method", "unresolved"),
                (17, "pkg.use.C.method", "resolved"),
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

    calls = [item for item in relations if item.relation_type == "CALLS"]
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
                (6, "pkg.use.C.method", "resolved"),
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

    assert [(item.source_line, item.target_reference, item.resolution_state)
            for item in relations if item.relation_type == "CALLS"] == [
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
                ("pkg.use.ordinary", "resolved"),
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
                (15, "pkg.use.A.method", "resolved"),
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

    assert [(item.target_reference, item.resolution_state)
            for item in relations if item.relation_type == "CALLS"] == [
                ("self.method", "unresolved"),
                ("setattr", "unresolved")
                if monkeypatch.startswith("setattr")
                else ("type.__setattr__", "unresolved"),
            ]
