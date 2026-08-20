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
