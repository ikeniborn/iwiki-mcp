import pytest

from iwiki_mcp.codegraph.languages.javascript import JavaScriptAdapter
from iwiki_mcp.codegraph.resolver import SymbolIndex


def _adapter():
    return JavaScriptAdapter("domain", (), parser_version="test-parser")


def test_adapter_identity():
    adapter = _adapter()
    assert adapter.language == "javascript"
    assert adapter.prefix == "js"
    assert adapter.extensions == (".js", ".jsx", ".mjs", ".cjs")


def test_every_javascript_file_is_module_backed():
    parsed = _adapter().parse_file(b"module.exports = { a: 1 };\n", "src/legacy.cjs")
    assert parsed.file.module_qualified_name == "src.legacy"
    assert parsed.file.module_local_name == "legacy"
    assert parsed.file.module_id
    assert parsed.file.module_name_tokens_casefold
    assert parsed.file.module_key == "src/legacy.cjs"


def test_multi_dot_basename_stem_stops_at_first_dot():
    parsed = _adapter().parse_file(b"export const a = 1;\n", "src/util.test.js")
    assert parsed.file.module_qualified_name == "src.util"


def test_function_class_and_arrow_declarations_extracted():
    source = (
        b"export function outer(a, b) {\n"
        b"  function inner(c) { return c; }\n"
        b"  return inner;\n"
        b"}\n"
        b"export const arrow = async (x) => x;\n"
        b"var expr = function (y) { return y; };\n"
        b"export class Widget { async render(z) { return z; } }\n"
    )
    parsed = _adapter().parse_file(source, "src/app.js")
    found = {(symbol.kind, symbol.qualified_name) for symbol in parsed.symbols}
    assert ("function", "src.app.outer") in found
    assert ("function", "src.app.outer.inner") in found
    assert ("async_function", "src.app.arrow") in found
    assert ("function", "src.app.expr") in found
    assert ("class", "src.app.Widget") in found
    assert ("method", "src.app.Widget.render") in found


def test_private_names_are_marked_private():
    source = b"function _internal() {}\nclass A { #secret() {} }\n"
    parsed = _adapter().parse_file(source, "src/app.js")
    visibility = {s.local_name: s.visibility for s in parsed.symbols}
    assert visibility["_internal"] == "private"
    assert visibility["#secret"] == "private"


def test_anonymous_declarations_emit_no_symbol():
    source = b"export default function () { return 1; }\nconst C = class {};\n"
    parsed = _adapter().parse_file(source, "a.js")
    assert not any(symbol.kind == "class" for symbol in parsed.symbols)


def test_declares_relations_cover_module_and_class_ownership():
    source = b"export class Widget { render() {} }\nexport function go() {}\n"
    parsed = _adapter().parse_file(source, "src/app.js")
    index = SymbolIndex.from_parsed_files((parsed,))
    relations = _adapter().resolve_references(parsed, index).relations
    declares = {
        r.target_symbol_id for r in relations if r.relation_type == "DECLARES"
    }
    by_name = {s.qualified_name: s.symbol_id for s in parsed.symbols}
    assert by_name["src.app.Widget"] in declares
    assert by_name["src.app.go"] in declares
    assert by_name["src.app.Widget.render"] in declares
    method_declare = next(
        r for r in relations
        if r.relation_type == "DECLARES"
        and r.target_symbol_id == by_name["src.app.Widget.render"]
    )
    assert method_declare.source_symbol_id == by_name["src.app.Widget"]


def test_jsx_parses_without_error():
    source = b"export const App = () => <div className='x'><Child {...p} /></div>;\n"
    parsed = _adapter().parse_file(source, "src/App.jsx")
    assert any(symbol.qualified_name == "src.App.App" for symbol in parsed.symbols)


def test_source_must_be_bytes():
    with pytest.raises(TypeError):
        _adapter().parse_file("export const a = 1;", "a.js")


def test_object_literal_methods_are_scoped_under_the_declarator():
    source = (
        b"export const api = {\n"
        b"  get(u) { return u; },\n"
        b"  post: function (u) { return u; },\n"
        b"  put: (u) => u,\n"
        b"  'quoted': (u) => u,\n"
        b"  [dynamic]: (u) => u,\n"
        b"  ...spread,\n"
        b"  plain: 1,\n"
        b"};\n"
    )
    parsed = _adapter().parse_file(source, "src/api.js")
    names = {symbol.qualified_name for symbol in parsed.symbols}
    assert {
        "src.api.api.get", "src.api.api.post", "src.api.api.put", "src.api.api.quoted",
    } <= names
    assert not any(name.endswith(".plain") for name in names)
    assert not any("dynamic" in name or "spread" in name for name in names)


def test_destructuring_declarator_with_object_initializer_is_skipped():
    parsed = _adapter().parse_file(b"const { a } = { a() { return 1; } };\n", "src/d.js")
    assert parsed.symbols == ()


def test_prototype_method_attaches_to_a_known_local_symbol():
    source = (
        b"function Widget() {}\n"
        b"Widget.prototype.render = function (x) { return x; };\n"
        b"Unknown.prototype.hidden = function () {};\n"
    )
    parsed = _adapter().parse_file(source, "src/widget.js")
    names = {symbol.qualified_name for symbol in parsed.symbols}
    assert "src.widget.Widget.render" in names
    assert not any("hidden" in name for name in names)


def test_object_literal_method_signature_has_no_return_segment():
    parsed = _adapter().parse_file(b"const api = { get(a, b) { return a; } };\n", "s.js")
    symbol = next(item for item in parsed.symbols if item.local_name == "get")
    assert symbol.kind == "method"
    assert symbol.signature == "method|(a, b)"
    assert symbol.visibility == "public"


def test_async_object_literal_method_signature_marks_async():
    parsed = _adapter().parse_file(b"const api = { async get(a) { return a; } };\n", "s.js")
    symbol = next(item for item in parsed.symbols if item.local_name == "get")
    assert symbol.signature == "method|async(a)"


def test_duplicate_symbol_identity_warns():
    source = (
        b"if (x) { function dup() { return 1; } }\n"
        b"else { function dup() { return 2; } }\n"
    )
    parsed = _adapter().parse_file(source, "src/dup.js")
    assert "duplicate_symbol_identity" in parsed.warnings


def _references_by_binding(parsed):
    return {ref.binding_name: ref for ref in parsed.references}


def test_esm_imports_produce_one_reference_per_binding():
    source = (
        b"import thing, { named as renamed, other } from './shapes';\n"
        b"import * as ns from './shapes';\n"
    )
    parsed = _adapter().parse_file(source, "src/app.js")
    bindings = _references_by_binding(parsed)
    assert bindings["thing"].binding_kind == "implicit_binding"
    assert bindings["renamed"].binding_kind == "explicit_alias"
    assert bindings["other"].binding_kind == "implicit_binding"
    assert bindings["ns"].binding_kind == "explicit_alias"
    assert all(ref.relation_type == "IMPORTS" for ref in parsed.references)


def test_side_effect_import_produces_no_reference():
    parsed = _adapter().parse_file(b"import './shapes';\n", "src/app.js")
    assert parsed.references == ()


def test_require_produces_import_references():
    source = (
        b"const shapes = require('./shapes');\n"
        b"const { named, other: aliased } = require('./more');\n"
    )
    parsed = _adapter().parse_file(source, "src/app.js")
    bindings = _references_by_binding(parsed)
    assert bindings["shapes"].target_reference == "./shapes"
    assert bindings["shapes"].binding_kind == "implicit_binding"
    assert bindings["named"].binding_kind == "implicit_binding"
    assert bindings["aliased"].binding_kind == "explicit_alias"


def test_dynamic_bare_and_array_pattern_requires_produce_no_reference():
    source = (
        b"const a = require(name);\n"
        b"require('./side');\n"
        b"const [first] = require('./tuple');\n"
    )
    parsed = _adapter().parse_file(source, "src/app.js")
    assert parsed.references == ()


def test_module_level_reference_sets_module_id_not_symbol_id():
    parsed = _adapter().parse_file(b"import a from './b';\n", "src/app.js")
    reference = parsed.references[0]
    assert reference.source_symbol_id is None
    assert reference.source_module_id == parsed.file.module_id


def test_reference_inside_a_function_sets_symbol_id_not_module_id():
    source = b"function go() { const m = require('./m'); return m; }\n"
    parsed = _adapter().parse_file(source, "src/app.js")
    reference = parsed.references[0]
    go = next(s for s in parsed.symbols if s.local_name == "go")
    assert reference.source_symbol_id == go.symbol_id
    assert reference.source_module_id is None
