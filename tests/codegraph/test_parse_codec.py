"""Round-trip the exact inputs consumed by fresh project-wide resolution."""
from dataclasses import replace
import hashlib
import json

import pytest

from iwiki_mcp.codegraph.discovery import SourceFile
from iwiki_mcp.codegraph.indexer import AdapterBinding
from iwiki_mcp.codegraph.languages.bash import BashAdapter
from iwiki_mcp.codegraph.languages.javascript import JavaScriptAdapter
from iwiki_mcp.codegraph.languages.python import PythonAdapter
from iwiki_mcp.codegraph.languages.typescript import TypeScriptAdapter
from iwiki_mcp.codegraph.models import ParsedFile
from iwiki_mcp.codegraph.parse_codec import decode_parsed, encode_parsed, parse_identity
from iwiki_mcp.codegraph.resolver import SymbolIndex


def identity(adapter, path, data, **overrides):
    source = SourceFile(path, data, hashlib.sha256(data).hexdigest(), len(data))
    binding = AdapterBinding(adapter, adapter.parser_version, "grammar", "adapter")
    options = dict(checkout="checkout", domain="domain", normalization_versions=("n", "u"))
    return parse_identity(binding, source, **(options | overrides))


def restore(parsed, key):
    return decode_parsed(json.loads(json.dumps(encode_parsed(parsed, key))), key)


@pytest.mark.parametrize("cls,path,data", [
    (PythonAdapter, "pkg.py", b"class A:\n    pass\n"),
    (TypeScriptAdapter, "pkg.tsx", b"export interface A { value: string }"),
    (JavaScriptAdapter, "pkg.jsx", b"import A from './a'; class B extends A {}"),
    (BashAdapter, "pkg.sh", b"work () { echo ok; }"),
])
def test_round_trip_and_resolve(cls, path, data):
    adapter = cls("domain", (path,))
    parsed = adapter.parse_file(data, path)
    parsed = replace(parsed, warnings=(*parsed.warnings, "same", "same"))
    clone = restore(parsed, identity(adapter, path, data))
    assert type(clone) is type(parsed)
    assert clone == parsed
    assert adapter.resolve_references(clone, SymbolIndex.from_parsed_files((clone,))) == (
        adapter.resolve_references(parsed, SymbolIndex.from_parsed_files((parsed,)))
    )


def test_context_uses_bound_module_names_once(monkeypatch):
    from iwiki_mcp.codegraph.languages import python

    calls = []
    derive = python.derive_module_names
    def count(paths):
        calls.append(paths)
        return derive(paths)
    monkeypatch.setattr(python, "derive_module_names", count)
    path = "pkg/mod.py"
    adapter = PythonAdapter("domain", (path,))
    key = identity(adapter, path, b"")
    assert identity(adapter, path, b"") == key
    assert identity(PythonAdapter("domain", (path, "pkg/__init__.py")), path, b"") != key
    assert len(calls) == 2
    assert identity(adapter, path, b"", normalization_versions=("changed", "u")) != key
    class Unknown(PythonAdapter):
        pass
    assert identity(Unknown("domain", (path,)), path, b"") is None


def test_restore_python_base_variant(monkeypatch):
    def fail():
        raise ValueError("parser error")
    adapter = PythonAdapter("domain", ("a.py",))
    monkeypatch.setattr(adapter, "_get_parser", fail)
    parsed = adapter.parse_file(b"", "a.py")
    assert type(parsed) is ParsedFile
    assert restore(parsed, identity(adapter, "a.py", b"")) == parsed


def test_cross_file_mutation_survives_restore():
    sources = {
        "pkg/base.py": b"class Base:\n    def work(self): pass\n    def call(self): self.work()\n",
        "pkg/patch.py": b"from pkg.base import Base\nBase.__getattribute__ = override\n",
    }
    adapter = PythonAdapter("domain", (*sources, "pkg/__init__.py"))
    fresh = [adapter.parse_file(data, path) for path, data in sources.items()]
    assert fresh[1]._adapter_evidence
    restored = [restore(item, identity(adapter, path, data))
                for item, (path, data) in zip(fresh, sources.items())]
    def resolve(items):
        return adapter.resolve_references(items[0], SymbolIndex.from_parsed_files(items))
    assert resolve(fresh) == resolve(restored)
    assert resolve(fresh) != resolve([fresh[0], replace(fresh[1], _adapter_evidence=())])


@pytest.mark.parametrize("damage", [
    lambda p: p.update(variant="unknown"),
    lambda p: p.update(extra="field"),
    lambda p: p.update(warnings="wrong"),
    lambda p: p.update(warnings=[True]),
    lambda p: p.update(evidence=[["too", "short"]]),
    lambda p: p["file"].update(size_bytes=True),
    lambda p: p["file"].update(path="other.py"),
    lambda p: p["file"].update(file_id="foreign"),
    lambda p: p["symbols"][0].update(kind="unknown"),
    lambda p: p["symbols"][0].update(file_id="foreign"),
    lambda p: p["references"][0].update(source_file_id="foreign"),
    lambda p: p["references"][0].update(binding_kind=10),
])
def test_malformed_payload_raises_value_error(damage):
    path = "pkg.py"
    data = b"import sys\nclass C: pass\n"
    adapter = PythonAdapter("domain", (path,))
    key = identity(adapter, path, data)
    payload = encode_parsed(adapter.parse_file(data, path), key)
    damage(payload)
    with pytest.raises(ValueError):
        decode_parsed(payload, key)


@pytest.mark.parametrize("field", ["parser_version", "grammar_version", "adapter_version"])
def test_binding_version_drift_invalidates_identity(field):
    adapter = BashAdapter("domain", ("script.sh",))
    binding = AdapterBinding(adapter, "parser", "grammar", "adapter")
    source = SourceFile("script.sh", b"", hashlib.sha256(b"").hexdigest(), 0)
    kwargs = dict(checkout="checkout", domain="domain", normalization_versions=("n", "u"))
    assert parse_identity(binding, source, **kwargs) != parse_identity(
        replace(binding, **{field: "new"}), source, **kwargs
    )


def test_typescript_boost_runs_on_current_resolver_after_restore(monkeypatch):
    path, data = "a.ts", b"export function f() {}"
    plain = TypeScriptAdapter("domain", (path,))
    boosted = TypeScriptAdapter("domain", (path,), type_boost_enabled=True)
    key = identity(plain, path, data)
    assert identity(boosted, path, data) == key
    parsed = restore(plain.parse_file(data, path), key)
    calls = []
    def probe(item):
        calls.append(item.file.path)
        return ("probe_warning",)
    monkeypatch.setattr(boosted, "_probe_boost_once", probe)
    result = boosted.resolve_references(parsed, SymbolIndex.from_parsed_files((parsed,)))
    assert calls == [path]
    assert result.warnings == ("probe_warning",)
