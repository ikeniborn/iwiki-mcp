"""End-to-end: one graph covers both Python and TypeScript sources."""
from __future__ import annotations

from pathlib import Path

from iwiki_mcp import server
from iwiki_mcp.codegraph.config import CodeGraphConfig
from iwiki_mcp.codegraph.indexer import CodeGraphIndexer
from iwiki_mcp.codegraph.linking import WikiSelectorResolver
from iwiki_mcp.codegraph.location import CodeGraphLocationResolver
from iwiki_mcp.codegraph.query import CodeGraphQuery, validate_search_request

FIXTURES = Path(__file__).parents[1] / "fixtures" / "codegraph"
_DOMAIN = "project"


def _build_indexer(
    cache_base: Path,
    project_dir: Path,
    *,
    languages: tuple[str, ...],
    adapter_factories=None,
    exclude: tuple[str, ...] = (),
) -> CodeGraphIndexer:
    """Construct one CodeGraphIndexer the way CodeGraphRuntime.__init__ does.

    Mirrors the composition performed at
    ``iwiki_mcp.codegraph.runtime.CodeGraphRuntime.__init__`` (see
    ``runtime.py`` around the ``CodeGraphIndexer(...)`` call): a
    ``CodeGraphLocationResolver`` resolves the on-disk cache paths, and
    ``server._code_graph_adapter_factories`` is the production composition
    root for the per-language adapter factories (also reused directly by
    ``tests/codegraph/test_server_tools.py::test_adapter_factories_include_typescript``).
    """
    (cache_base / _DOMAIN).mkdir(parents=True)
    config = CodeGraphConfig(languages=languages, exclude=exclude)
    paths = CodeGraphLocationResolver(
        str(cache_base), _DOMAIN, str(project_dir)
    ).resolve(ensure_excluded=False)
    factories = (
        adapter_factories
        if adapter_factories is not None
        else server._code_graph_adapter_factories(_DOMAIN)
    )
    return CodeGraphIndexer(
        cache_base=str(cache_base),
        project_dir=str(project_dir),
        domain=_DOMAIN,
        config=config,
        paths=paths,
        adapter_factories=factories,
        resolver_version="resolver-v1",
        wiki_selector_resolver=WikiSelectorResolver(str(cache_base)),
    )


def test_mixed_repo_builds_one_snapshot_with_both_languages(tmp_path):
    project_dir = FIXTURES / "mixed_python_typescript"
    indexer = _build_indexer(
        tmp_path / "cache", project_dir, languages=("python", "typescript")
    )

    built = indexer.build_rows()

    assert set(built.header.languages) == {"python", "typescript"}
    assert {row["language"] for row in built.tables["files"]} == {
        "python", "typescript",
    }
    assert {row["path"] for row in built.tables["files"]} == {
        "service.py", "__init__.py", "base.ts", "client.ts",
    }


def test_mixed_repo_search_returns_both_languages(tmp_path):
    project_dir = FIXTURES / "mixed_python_typescript"
    indexer = _build_indexer(
        tmp_path / "cache", project_dir, languages=("python", "typescript")
    )

    built = indexer.build(force=True)
    assert built["state"] == "ready"

    request = validate_search_request(
        "e", configured_languages=("python", "typescript"), limit=100
    )
    with indexer.store.read_lease() as connection:
        results = CodeGraphQuery(_DOMAIN).search(connection, request)

    languages_found = {result.entity_id.split(":", 1)[0] for result in results}
    assert {"py", "ts"} <= languages_found


def test_python_only_repo_search_unaffected(tmp_path):
    project_dir = FIXTURES / "python_basic"
    mixed_factories = server._code_graph_adapter_factories(_DOMAIN)
    python_only_factories = {"python": mixed_factories["python"]}

    # Build once the way the repository was indexed before TypeScript
    # support existed (only the Python factory registered), and once the
    # way it is indexed today (both factories registered, but the project
    # config still only requests "python"). If registering the TypeScript
    # adapter changed anything about Python-only indexing or search, these
    # two builds would diverge.
    baseline_indexer = _build_indexer(
        tmp_path / "baseline",
        project_dir,
        languages=("python",),
        adapter_factories=python_only_factories,
    )
    mixed_indexer = _build_indexer(
        tmp_path / "mixed",
        project_dir,
        languages=("python",),
        adapter_factories=mixed_factories,
    )

    baseline_built = baseline_indexer.build(force=True)
    mixed_built = mixed_indexer.build(force=True)
    assert baseline_built["state"] == "ready"
    assert mixed_built["state"] == "ready"

    request = validate_search_request("run", configured_languages=("python",))
    with baseline_indexer.store.read_lease() as connection:
        baseline_results = CodeGraphQuery(_DOMAIN).search(connection, request)
    with mixed_indexer.store.read_lease() as connection:
        mixed_results = CodeGraphQuery(_DOMAIN).search(connection, request)

    baseline_ids = [result.entity_id for result in baseline_results]
    mixed_ids = [result.entity_id for result in mixed_results]
    assert baseline_ids  # the fixture's Service.run method must be found
    assert baseline_ids == mixed_ids


def test_single_language_filter_excludes_the_other_language(tmp_path):
    project_dir = FIXTURES / "mixed_python_typescript"
    indexer = _build_indexer(
        tmp_path / "cache", project_dir, languages=("python", "typescript")
    )
    built = indexer.build(force=True)
    assert built["state"] == "ready"

    rows = indexer.build_rows()
    python_symbol_id = next(
        row["symbol_id"]
        for row in rows.tables["symbols"]
        if row["local_name"] == "process"
    )
    typescript_symbol_id = next(
        row["symbol_id"]
        for row in rows.tables["symbols"]
        if row["local_name"] == "Client"
    )
    assert python_symbol_id.startswith("py:")
    assert typescript_symbol_id.startswith("ts:")

    with indexer.store.read_lease() as connection:
        typescript_only = CodeGraphQuery(_DOMAIN).search(
            connection,
            validate_search_request(
                "e", languages=["typescript"],
                configured_languages=("python", "typescript"), limit=100,
            ),
        )
        python_only = CodeGraphQuery(_DOMAIN).search(
            connection,
            validate_search_request(
                "e", languages=["python"],
                configured_languages=("python", "typescript"), limit=100,
            ),
        )

    typescript_only_ids = {result.entity_id for result in typescript_only}
    python_only_ids = {result.entity_id for result in python_only}
    assert python_symbol_id not in typescript_only_ids
    assert not any(entity_id.startswith("py:") for entity_id in typescript_only_ids)
    assert typescript_symbol_id not in python_only_ids
    assert not any(entity_id.startswith("ts:") for entity_id in python_only_ids)
    # Sanity: both filtered searches still found something in their own
    # language, proving the IN (...) filter narrows rather than empties.
    assert typescript_symbol_id in typescript_only_ids
    assert python_symbol_id in python_only_ids


def test_typescript_multi_dot_basename_module_name_strips_from_first_dot(tmp_path):
    # "component.spec.ts" is a pervasive real-world TS/Jest/Angular naming
    # convention with more than one dot in its basename. store.py's shared
    # validator re-derives a module's expected local name via a single
    # rsplit(".", 1), so the local/qualified name must strip everything
    # from the FIRST dot onward ("component"), not just the last suffix
    # ("component.spec") -- the latter still contains a literal "." and
    # would fail validation on a real build.
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "component.spec.ts").write_text(
        "export class Widget {}\n", encoding="utf-8",
    )
    indexer = _build_indexer(
        tmp_path / "cache", project_dir, languages=("typescript",),
    )

    built = indexer.build(force=True)

    assert built["state"] == "ready"
    file_row = next(
        row for row in indexer.build_rows().tables["files"]
        if row["path"] == "component.spec.ts"
    )
    assert file_row["module_local_name"] == "component"
    assert file_row["module_qualified_name"] == "component"


def test_typescript_nested_local_declarations_do_not_collide(tmp_path):
    # Regression for C1: two functions each declaring a local arrow function
    # of the same name must not flatten to the same module-scoped
    # qualified_name -- that collision breaks symbol_id's PRIMARY KEY
    # constraint and fails the whole snapshot build, not just this file.
    # A unit-level assertion on parsed.symbols alone would not catch this:
    # the failure only manifests as a real SQLite PRIMARY KEY violation
    # during persistence, so this exercises the real build_rows()/build()
    # path exactly like the other tests in this module.
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "a.ts").write_text(
        "function f() { const h = () => 1; }\n"
        "function g() { const h = () => 2; }\n",
        encoding="utf-8",
    )
    indexer = _build_indexer(
        tmp_path / "cache", project_dir, languages=("typescript",),
    )

    built = indexer.build(force=True)

    assert built["state"] == "ready"
    rows = indexer.build_rows()
    qualified_names = {
        row["qualified_name"] for row in rows.tables["symbols"]
    }
    assert {"a.f", "a.f.h", "a.g", "a.g.h"} <= qualified_names
    symbol_ids = [row["symbol_id"] for row in rows.tables["symbols"]]
    assert len(symbol_ids) == len(set(symbol_ids))


def test_typescript_anonymous_scope_collisions_deduplicate_with_warning(tmp_path):
    # Regression for the residual C1 gap: the first fix wave threaded scope
    # through NAMED declaration parents (functions, methods, classes,
    # namespaces), but declarations inside an ANONYMOUS or block scope --
    # callback bodies passed to another call, or if/else branches -- get no
    # named scope segment of their own, so same-named siblings there still
    # collide on qualified_name/symbol_id. This is a safety-net dedup, not a
    # scoping fix: it must not crash the build, must keep exactly one
    # symbol per colliding id, and must surface duplicate_symbol_identity.
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "callback.ts").write_text(
        "describe('a', () => { function setup(){} });\n"
        "describe('b', () => { function setup(){} });\n",
        encoding="utf-8",
    )
    (project_dir / "block.ts").write_text(
        "if (x) { function f(){} } else { function f(){} }\n",
        encoding="utf-8",
    )
    indexer = _build_indexer(
        tmp_path / "cache", project_dir, languages=("typescript",),
    )

    built = indexer.build(force=True)

    assert built["state"] == "ready"
    assert "duplicate_symbol_identity" in built["warnings"]
    rows = indexer.build_rows()
    symbol_ids = [row["symbol_id"] for row in rows.tables["symbols"]]
    assert len(symbol_ids) == len(set(symbol_ids))


_ALL_LANGUAGES = ("python", "typescript", "javascript")


def _build_tables(cache_base: Path, *, languages: tuple[str, ...]):
    project_dir = FIXTURES / "mixed_python_typescript_javascript"
    indexer = _build_indexer(cache_base, project_dir, languages=languages)
    return indexer.build_rows().tables


def _rows_for_languages(tables, table, prefixes):
    if table == "files":
        return [row for row in tables[table] if row["language"] in prefixes]
    key = "symbol_id" if table == "symbols" else "relation_id"
    return [
        row for row in tables[table]
        if any(row[key].startswith(prefix + ":") for prefix in prefixes)
    ]


def _search(cache_base: Path, *, query: str, languages: list[str]):
    project_dir = FIXTURES / "mixed_python_typescript_javascript"
    indexer = _build_indexer(cache_base, project_dir, languages=_ALL_LANGUAGES)
    built = indexer.build(force=True)
    assert built["state"] == "ready"
    request = validate_search_request(
        query, languages=languages, configured_languages=_ALL_LANGUAGES, limit=100,
    )
    with indexer.store.read_lease() as connection:
        return CodeGraphQuery(_DOMAIN).search(connection, request)


def test_javascript_rows_carry_their_own_language(tmp_path):
    tables = _build_tables(tmp_path / "all", languages=_ALL_LANGUAGES)
    assert {row["language"] for row in tables["files"]} == {
        "python", "typescript", "javascript",
    }
    extensions = {
        row["path"].rsplit(".", 1)[-1]
        for row in tables["files"] if row["language"] == "javascript"
    }
    assert extensions == {"js", "jsx", "cjs", "mjs"}


def test_identifiers_do_not_collide_across_languages(tmp_path):
    tables = _build_tables(tmp_path / "all", languages=_ALL_LANGUAGES)
    for table, key in (("symbols", "symbol_id"), ("relations", "relation_id")):
        identifiers = [row[key] for row in tables[table]]
        assert len(identifiers) == len(set(identifiers))
    # The fixture's intended collision must actually exist: shared/utils.py
    # and shared/utils.js both derive the dotted qualified name
    # "shared.utils.helper" -- if that collision ever stopped existing, this
    # guard test would be protecting against nothing.
    colliding_symbol_ids = {
        row["symbol_id"] for row in tables["symbols"]
        if row["qualified_name"] == "shared.utils.helper"
    }
    assert len(colliding_symbol_ids) == 2
    prefixes = {symbol_id.split(":", 1)[0] for symbol_id in colliding_symbol_ids}
    assert prefixes == {"py", "js"}


def test_javascript_cross_file_calls_and_inherits_resolve_into_typescript(tmp_path):
    tables = _build_tables(tmp_path / "all", languages=_ALL_LANGUAGES)
    symbol_ids_by_qualified_name = {
        row["qualified_name"]: row["symbol_id"] for row in tables["symbols"]
    }
    build_symbol_id = symbol_ids_by_qualified_name["shapes.build"]
    shape_symbol_id = symbol_ids_by_qualified_name["shapes.Shape"]
    assert any(
        row["relation_id"].startswith("js:")
        and row["relation_type"] == "CALLS"
        and row["resolution_state"] == "resolved"
        and row["target_symbol_id"] == build_symbol_id
        for row in tables["relations"]
    )
    assert any(
        row["relation_id"].startswith("js:")
        and row["relation_type"] == "INHERITS"
        and row["resolution_state"] == "resolved"
        and row["target_symbol_id"] == shape_symbol_id
        for row in tables["relations"]
    )


def test_javascript_import_resolves_into_typescript(tmp_path):
    tables = _build_tables(tmp_path / "all", languages=_ALL_LANGUAGES)
    shapes_module_id = next(
        row["module_id"] for row in tables["files"] if row["path"].endswith("shapes.ts")
    )
    assert any(
        row["relation_id"].startswith("js:")
        and row["relation_type"] == "IMPORTS"
        and row["resolution_state"] == "resolved"
        and row["target_module_id"] == shapes_module_id
        for row in tables["relations"]
    )


def test_mjs_extension_bearing_specifier_resolves(tmp_path):
    tables = _build_tables(tmp_path / "all", languages=_ALL_LANGUAGES)
    esm_file_id = next(
        row["file_id"] for row in tables["files"] if row["path"].endswith("esm.mjs")
    )
    shapes_module_id = next(
        row["module_id"] for row in tables["files"] if row["path"].endswith("shapes.ts")
    )
    assert any(
        row["source_file_id"] == esm_file_id
        and row["relation_type"] == "IMPORTS"
        and row["resolution_state"] == "resolved"
        and row["target_module_id"] == shapes_module_id
        for row in tables["relations"]
    )


def test_python_and_typescript_rows_are_unchanged_by_adding_javascript(tmp_path):
    without = _build_tables(tmp_path / "without", languages=("python", "typescript"))
    with_js = _build_tables(tmp_path / "with", languages=_ALL_LANGUAGES)
    assert _rows_for_languages(without, "files", {"python", "typescript"}) == \
        _rows_for_languages(with_js, "files", {"python", "typescript"})
    for table, prefixes in (("symbols", ("py", "ts")), ("relations", ("py", "ts"))):
        assert _rows_for_languages(without, table, prefixes) == \
            _rows_for_languages(with_js, table, prefixes)


def test_search_filtered_to_javascript_returns_only_javascript_symbols(tmp_path):
    # "helper" exists in BOTH shared/utils.py and shared/utils.js (the R6.1
    # collision fixture), so filtering by language must actually narrow the
    # result set rather than merely coexist with a JavaScript-unique term.
    javascript_results = _search(
        tmp_path / "search-js", query="helper", languages=["javascript"],
    )
    assert javascript_results
    # Search results include module/file hits (symbol_id is None for those),
    # not just symbol hits, so filter on entity_id, mirroring the language
    # scoping check the module's existing single-language filter test uses
    # (test_single_language_filter_excludes_the_other_language).
    assert all(item.entity_id.startswith("js:") for item in javascript_results)

    python_results = _search(
        tmp_path / "search-py", query="helper", languages=["python"],
    )
    assert python_results
    assert all(item.entity_id.startswith("py:") for item in python_results)


def test_search_reports_accurate_ranges_for_a_javascript_symbol(tmp_path):
    results = _search(tmp_path / "ranges", query="Widget", languages=["javascript"])
    widget = next(item for item in results if item.local_name == "Widget")
    source = (FIXTURES / "mixed_python_typescript_javascript" / "widget.jsx").read_bytes()
    assert source[widget.start_byte:widget.end_byte].startswith(b"Widget")
    assert widget.start_line >= 1 and widget.end_line >= widget.start_line


def test_typescript_files_respect_exclude_patterns(tmp_path):
    project_dir = FIXTURES / "mixed_python_typescript"
    indexer = _build_indexer(
        tmp_path / "cache",
        project_dir,
        languages=("python", "typescript"),
        exclude=("vendor/",),
    )

    built = indexer.build_rows()

    paths = {row["path"] for row in built.tables["files"]}
    assert not any(path.startswith("vendor/") for path in paths)
    assert paths == {"service.py", "__init__.py", "base.ts", "client.ts"}
    assert not any(
        "ShouldNeverBeIndexed" in row["qualified_name"]
        for row in built.tables["symbols"]
    )
