"""Deterministic, bounded symbol search contracts."""
from __future__ import annotations

import sqlite3
import time

import pytest

from iwiki_mcp.codegraph import runtime as runtime_module
from iwiki_mcp.codegraph import query as query_module
from iwiki_mcp.codegraph import models as models_module
from iwiki_mcp.codegraph.indexer import CodeGraphStaleError
from iwiki_mcp.codegraph.query import (
    CodeGraphQuery,
    CodeGraphQueryError,
    validate_search_request,
)
from iwiki_mcp.codegraph.schema import CodeGraphStoreError
from iwiki_mcp.codegraph.store import CodeGraphStore


def _snapshot():
    files = []
    symbols = []
    fixtures = (
        ("qualified", "src/qualified.py", "run", "qualified_alias", "()"),
        ("local", "src/local.py", "pkg.Exact.run", "run", "()"),
        ("prefix", "src/prefix.py", "pkg.Prefix.runner", "runner", "()"),
        ("lexical-a", "src/lexical_a.py", "pkg.A.batch_run", "batch_run", "()"),
        ("lexical-b", "src/lexical_b.py", "pkg.B.batch_run", "batch_run", "()"),
        ("signature", "src/signature.py", "pkg.Signature.execute", "execute", "(run: str)"),
        ("path", "src/run-assets.py", "pkg.PathOnly.deploy", "deploy", "()"),
        ("crowd-brunch", "crowding/brunch.py", "pkg.A.brunch", "brunch", "()"),
        (
            "crowd-batch-run",
            "crowding/batch.py",
            "pkg.B.batch_run",
            "batch_run",
            "()",
        ),
        ("other-kind", "src/other.py", "pkg.Other.run", "run", None),
        ("other-language", "src/frontend.ts", "frontend.run", "run", "()"),
    )
    for offset, (identity, path, qualified, local, signature) in enumerate(fixtures):
        language = "typescript" if identity == "other-language" else "python"
        file_id = f"file:{identity}"
        file_local_name = path.rsplit("/", 1)[-1]
        files.append({
            "file_id": file_id,
            "repository_id": "backend",
            "path": path,
            "path_casefold": models_module.compact_casefold(path),
            "file_local_name": file_local_name,
            "file_name_tokens_casefold": models_module.token_key(
                file_local_name
            ),
            "language": language,
            "content_hash": f"hash:{identity}",
            "parser_version": "fixture",
            "size_bytes": 10,
            "start_line": 1,
            "end_line": 1,
            "start_byte": 0,
            "end_byte": 10,
            "module_key": path,
            "module_id": None,
            "module_qualified_name": None,
            "module_local_name": None,
            "module_name_tokens_casefold": None,
        })
        symbols.append({
            "symbol_id": f"symbol:{identity}",
            "file_id": file_id,
            "kind": "class" if identity == "other-kind" else "method",
            "qualified_name": qualified,
            "local_name": local,
            "name_tokens_casefold": models_module.token_key(qualified, local),
            "start_line": offset + 1,
            "end_line": offset + 2,
            "start_byte": offset * 10,
            "end_byte": offset * 10 + 9,
            "signature": signature,
            "signature_casefold": models_module.compact_casefold(signature),
            "visibility": "public",
            "content_hash": f"symbol-hash:{identity}",
            "metadata_json": "{}",
        })
    return {
        "repositories": ({
            "repository_id": "backend",
            "root_path": ".",
            "git_remote": None,
            "git_commit": "abc123",
            "source_fingerprint": "source",
            "config_fingerprint": "config",
            "parser_fingerprint": "parser",
            "normalizer_version": models_module.NORMALIZER_VERSION,
            "unicode_data_version": models_module.UNICODE_DATA_VERSION,
            "revision": "sha256:fixture",
            "state": "ready",
            "indexed_at": "2026-08-10T00:00:00Z",
        },),
        "files": tuple(files),
        "symbols": tuple(symbols),
        "relations": (),
        "wiki_code_links": (),
    }


_FILE_INSERT_SQL = """
    INSERT INTO files (
        file_id, repository_id, path, path_casefold, file_local_name,
        file_name_tokens_casefold, language, content_hash, parser_version,
        size_bytes, start_line, end_line, start_byte, end_byte, module_key,
        module_id, module_qualified_name, module_local_name,
        module_name_tokens_casefold
    ) VALUES (
        ?, 'backend', ?, ?, ?, ?, ?, ?, 'fixture', 10, 1, 1, 0, 10, ?,
        NULL, NULL, NULL, NULL
    )
"""


def _file_insert_values(file_id, path, language, content_hash):
    local_name = path.rsplit("/", 1)[-1]
    return (
        file_id,
        path,
        models_module.compact_casefold(path),
        local_name,
        models_module.token_key(local_name),
        language,
        content_hash,
        path,
    )


_SYMBOL_INSERT_SQL = """
    INSERT INTO symbols (
        symbol_id, file_id, kind, qualified_name, local_name,
        name_tokens_casefold, start_line, end_line, start_byte, end_byte,
        signature, signature_casefold, visibility, content_hash,
        metadata_json
    ) VALUES (
        ?, ?, 'method', ?, ?, ?, 1, 1, 0, 1, ?, ?, 'public', ?, '{}'
    )
"""


def _symbol_insert_values(
    symbol_id,
    file_id,
    qualified_name,
    local_name,
    signature,
    content_hash,
):
    return (
        symbol_id,
        file_id,
        qualified_name,
        local_name,
        models_module.token_key(qualified_name, local_name),
        signature,
        models_module.compact_casefold(signature),
        content_hash,
    )


def _large_connection(*, local_name: str | None = None):
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE files (
            file_id TEXT PRIMARY KEY, repository_id TEXT, path TEXT, language TEXT
        );
        CREATE TABLE symbols (
            symbol_id TEXT PRIMARY KEY, file_id TEXT, kind TEXT,
            qualified_name TEXT, local_name TEXT, signature TEXT,
            start_line INTEGER, end_line INTEGER,
            start_byte INTEGER, end_byte INTEGER
        );
        CREATE INDEX idx_files_repository_path ON files(repository_id, path);
        CREATE INDEX idx_symbols_file ON symbols(file_id);
        CREATE INDEX idx_symbols_qualified ON symbols(qualified_name);
        CREATE INDEX idx_symbols_local ON symbols(local_name);
        CREATE INDEX idx_symbols_kind ON symbols(kind);
        """
    )
    connection.execute(
        "INSERT INTO files VALUES (?, ?, ?, ?)",
        ("file:large", "backend", "src/large.py", "python"),
    )
    connection.executemany(
        "INSERT INTO symbols VALUES (?, ?, 'function', ?, ?, '()', 1, 1, 0, 1)",
        (
            (
                f"symbol:{number:06d}",
                "file:large",
                f"pkg.symbol_{number:06d}",
                local_name or f"symbol_{number:06d}",
            )
            for number in range(100_000)
        ),
    )
    return connection


@pytest.fixture
def search_connection(tmp_path):
    store = CodeGraphStore(tmp_path / "code.sqlite3")
    store.insert_snapshot(_snapshot())
    connection = store.open_existing()
    yield connection
    connection.close()


def test_search_orders_each_symbol_by_its_strongest_match(search_connection):
    query = CodeGraphQuery("backend")
    request = validate_search_request(
        "run",
        kinds=["method"],
        path="src/",
        languages=["python"],
        limit=7,
    )
    results = query.search(search_connection, request)

    assert [item.match for item in results] == [
        "qualified_exact",
        "local_exact",
        "canonical_prefix",
        "canonical_lexical",
        "canonical_lexical",
        "signature",
        "path",
    ]
    assert [item.qualified_name for item in results[3:5]] == [
        "pkg.A.batch_run",
        "pkg.B.batch_run",
    ]
    assert len({item.symbol_id for item in results}) == len(results)
    assert results == query.search(search_connection, request)


def test_prefix_tier_does_not_refetch_exact_local_rows(search_connection):
    search_connection.execute(
        _FILE_INSERT_SQL,
        _file_insert_values(
            "file:tier-overlap",
            "tier-overlap/sample.py",
            "python",
            "hash:tier-overlap",
        ),
    )
    search_connection.executemany(
        _SYMBOL_INSERT_SQL,
        (
            _symbol_insert_values(
                "symbol:tier-qualified", "file:tier-overlap",
                "alpha", "root", "()", "hash:tier-qualified",
            ),
            _symbol_insert_values(
                "symbol:tier-local-overlap",
                "file:tier-overlap",
                "alpha.00_overlap",
                "alpha",
                "()",
                "hash:tier-local-overlap",
            ),
            _symbol_insert_values(
                "symbol:tier-prefix-1", "file:tier-overlap",
                "alpha.10_first", "first", "()", "hash:tier-1",
            ),
            _symbol_insert_values(
                "symbol:tier-prefix-2", "file:tier-overlap",
                "alpha.20_second", "second", "()", "hash:tier-2",
            ),
            _symbol_insert_values(
                "symbol:tier-prefix-3", "file:tier-overlap",
                "alpha.30_third", "third", "()", "hash:tier-3",
            ),
        ),
    )
    request = validate_search_request(
        "alpha",
        kinds=["method"],
        path="tier-overlap/",
        limit=5,
    )

    results = CodeGraphQuery("backend").search(search_connection, request)

    assert [(item.symbol_id, item.match) for item in results] == [
        ("symbol:tier-qualified", "qualified_exact"),
        ("symbol:tier-local-overlap", "local_exact"),
        ("symbol:tier-prefix-1", "canonical_prefix"),
        ("symbol:tier-prefix-2", "canonical_prefix"),
        ("symbol:tier-prefix-3", "canonical_prefix"),
    ]


def test_lexical_boundary_is_enforced_before_candidate_limit(search_connection):
    request = validate_search_request(
        "run",
        kinds=["method"],
        path="crowding/",
        languages=["python"],
        limit=1,
    )

    results = CodeGraphQuery("backend").search(search_connection, request)

    assert [item.local_name for item in results] == ["batch_run"]
    assert results[0].match == "canonical_lexical"


def test_tokenless_query_does_not_crowd_signature_or_path_fallback(
    search_connection,
):
    request = validate_search_request("-", kinds=["method"], limit=1)

    results = CodeGraphQuery("backend").search(search_connection, request)

    assert [item.path for item in results] == ["src/run-assets.py"]
    assert results[0].match == "path"


def test_unicode_casefold_is_shared_by_sql_and_strongest_classification(
    search_connection,
):
    search_connection.executemany(
        _FILE_INSERT_SQL,
        (
            _file_insert_values(
                "file:unicode-qualified", "unicode/qualified.py",
                "python", "hash:uq",
            ),
            _file_insert_values(
                "file:unicode-local", "unicode/local.py", "python", "hash:ul",
            ),
            _file_insert_values(
                "file:unicode-signature", "unicode/signature.py",
                "python", "hash:us",
            ),
            _file_insert_values(
                "file:unicode-path", "unicode/Straße/data.py",
                "python", "hash:path",
            ),
        ),
    )
    search_connection.executemany(
        _SYMBOL_INSERT_SQL,
        (
            _symbol_insert_values(
                "symbol:uq", "file:unicode-qualified", "Straße", "alias",
                "()", "hash:uq",
            ),
            _symbol_insert_values(
                "symbol:ul", "file:unicode-local", "pkg.Local", "Straße",
                "()", "hash:ul",
            ),
            _symbol_insert_values(
                "symbol:us", "file:unicode-signature", "pkg.Signature", "execute",
                "(Straße: str)", "hash:us",
            ),
            _symbol_insert_values(
                "symbol:unicode-path", "file:unicode-path", "pkg.UnicodePath",
                "deploy", "()", "hash:path",
            ),
        ),
    )
    request = validate_search_request("strasse", kinds=["method"], limit=4)

    results = CodeGraphQuery("backend").search(search_connection, request)

    assert [(item.symbol_id, item.match) for item in results] == [
        ("symbol:uq", "canonical_lexical"),
        ("symbol:ul", "canonical_lexical"),
        ("symbol:us", "signature"),
        ("symbol:unicode-path", "path"),
    ]


def test_search_applies_filters_limit_and_range_contract(search_connection):
    query = CodeGraphQuery("backend")
    request = validate_search_request(
        "run",
        kinds=["method"],
        path="src/lexical_",
        languages=["python"],
        limit=1,
    )
    results = query.search(search_connection, request)

    assert len(results) == 1
    item = results[0]
    assert item.match == "canonical_lexical"
    assert item.path == "src/lexical_a.py"
    assert item.start_line >= 1
    assert item.end_line >= item.start_line
    assert item.start_byte is not None
    assert item.end_byte is not None


def test_path_filter_is_case_sensitive_literal_prefix(search_connection):
    paths = {
        "upper": "SRC/case.py",
        "lower": "src/case.py",
        "percent": "src/%literal/target.py",
        "percent-lookalike": "src/xliteral/target.py",
        "underscore": "src/_literal/target.py",
    }
    search_connection.executemany(
        _FILE_INSERT_SQL,
        (
            _file_insert_values(
                f"file:path-{identity}", path, "python", f"hash:path-{identity}"
            )
            for identity, path in paths.items()
        ),
    )
    search_connection.executemany(
        _SYMBOL_INSERT_SQL,
        (
            _symbol_insert_values(
                f"symbol:path-{identity}",
                f"file:path-{identity}",
                f"pkg.path_{identity}",
                "path_filter",
                "()",
                f"hash:symbol-path-{identity}",
            )
            for identity in paths
        ),
    )
    query = CodeGraphQuery("backend")

    for prefix, expected_ids in (
        ("src/%literal/", {"symbol:path-percent"}),
        ("src/_literal/", {"symbol:path-underscore"}),
        (
            "src/",
            {
                "symbol:path-lower",
                "symbol:path-percent",
                "symbol:path-percent-lookalike",
                "symbol:path-underscore",
            },
        ),
    ):
        request = validate_search_request(
            "path_filter",
            kinds=["method"],
            path=prefix,
            limit=10,
        )
        results = query.search(search_connection, request)

        assert {item.symbol_id for item in results} == expected_ids


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"query": "  "}, "query"),
        ({"query": "run", "kinds": []}, "kind"),
        ({"query": "run", "kinds": ["unknown"]}, "kind"),
        ({"query": "run", "languages": []}, "language"),
        ({"query": "run", "languages": "python"}, "language"),
        ({"query": "run", "languages": ["typescript"]}, "language"),
        ({"query": "run", "languages": ["python", "typescript"]}, "language"),
        ({"query": "run", "path": "/src"}, "path"),
        ({"query": "run", "path": "C:\\src"}, "path"),
        ({"query": "run", "path": "../src"}, "path"),
        ({"query": "run", "path": "src/\0private"}, "path"),
        ({"query": "run", "limit": 0}, "limit"),
        ({"query": "run", "limit": 101}, "limit"),
    ],
)
def test_search_rejects_invalid_configuration(search_connection, arguments, message):
    with pytest.raises(CodeGraphQueryError, match=message):
        validate_search_request(**arguments)


def test_query_text_validation_is_pure_and_bounded(monkeypatch):
    assert query_module._TOKENS is models_module._TOKENS
    assert not hasattr(query_module, "re")

    def unexpected_io(*_args, **_kwargs):
        pytest.fail("query validation performed I/O")

    monkeypatch.setattr(query_module.sqlite3, "connect", unexpected_io)

    request = validate_search_request(" Straße ")
    assert request.query == " Straße "
    assert request.tokens == ("strasse",)
    assert validate_search_request("x" * 4096).query == "x" * 4096

    for invalid_query in (
        "nul\0query",
        "lone-surrogate-\ud800",
        "é" * 2049,
    ):
        with pytest.raises(CodeGraphQueryError, match="query"):
            validate_search_request(invalid_query)

    with pytest.raises(CodeGraphQueryError, match="path"):
        validate_search_request("run", path="\ud800")


def test_query_rejects_more_than_64_distinct_tokens():
    assert len(validate_search_request(" ".join(f"t{i}" for i in range(64))).tokens) == 64
    assert validate_search_request("token " * 100).tokens == ("token",)

    with pytest.raises(CodeGraphQueryError, match="query"):
        validate_search_request(" ".join(f"t{i}" for i in range(65)))


def test_runtime_search_returns_metadata_relative_ranges_and_no_stale_rows(
    ready_runtime,
):
    ready = ready_runtime.runtime.search("run", kinds=["method"])

    assert {
        "domain", "state", "revision", "fresh", "warnings", "results",
    } <= set(ready)
    assert ready["fresh"] is True
    assert ready["results"]
    assert set(ready["results"][0]) == {
        "entity_id", "entity_type", "file_id", "module_id", "symbol_id",
        "kind", "qualified_name", "local_name", "signature", "path",
        "start_line", "end_line", "start_byte", "end_byte", "match",
        "matched_alias", "alias_ambiguous", "alias_target_count",
    }
    assert ready["results"][0]["entity_id"] == ready["results"][0]["symbol_id"]
    assert ready["results"][0]["entity_type"] == "symbol"
    assert ready["results"][0]["file_id"] is not None
    assert ready["results"][0]["module_id"] is None
    assert ready["results"][0]["match"] == "local_exact"
    assert not ready["results"][0]["path"].startswith("/")

    dirty = ready_runtime.with_state("dirty", auto_rebuild="off")
    stale = dirty.runtime.search("run")
    assert stale["fresh"] is False
    assert stale["results"] == []
    assert stale["hint"] == "run wiki_code_index"


def test_runtime_invalid_search_is_stable_and_sanitized(ready_runtime, caplog):
    caplog.clear()
    first = ready_runtime.runtime.search("run", languages=["typescript"])
    second = ready_runtime.runtime.search("run", languages=["typescript"])

    assert first == second == {
        "error": "code graph configuration is invalid",
        "code": "invalid_config",
        "hint": "inspect code_graph project configuration",
    }
    assert "code_graph_query" not in caplog.text


def test_runtime_validates_before_nonready_guard_or_rebuild(ready_runtime):
    dirty = ready_runtime.with_state("dirty", auto_rebuild="bounded")

    result = dirty.runtime.search("run", languages=["typescript"])

    assert result == {
        "error": "code graph configuration is invalid",
        "code": "invalid_config",
        "hint": "inspect code_graph project configuration",
    }
    assert dirty.build_attempts == 0


def test_runtime_search_maps_store_failure_without_leaking_text(
    ready_runtime, monkeypatch, caplog
):
    class FailingQuery:
        def search(self, *_args, **_kwargs):
            raise CodeGraphStoreError("secret SQL and /absolute/path")

    monkeypatch.setattr(
        runtime_module,
        "CodeGraphQuery",
        lambda _domain: FailingQuery(),
    )

    result = ready_runtime.runtime.search("run")

    assert result == {
        "error": "code graph store failed",
        "code": "store_failed",
        "hint": "inspect wiki_code_status and retry",
        "fresh": False,
        "results": [],
    }
    assert "code_graph_query" not in caplog.text


def test_runtime_unexpected_search_failure_logs_only_stable_metadata(
    ready_runtime, monkeypatch, caplog
):
    class FailingQuery:
        def search(self, *_args, **_kwargs):
            raise RuntimeError(
                "secret-token /absolute/path SELECT source classified-query"
            )

    monkeypatch.setattr(
        runtime_module,
        "CodeGraphQuery",
        lambda _domain: FailingQuery(),
    )
    caplog.clear()

    with caplog.at_level("ERROR", logger=runtime_module.__name__):
        result = ready_runtime.runtime.search("classified-query")

    assert result == {
        "error": "code graph rebuild failed",
        "code": "rebuild_failed",
        "hint": "inspect wiki_code_status and retry",
        "fresh": False,
        "results": [],
    }
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == runtime_module.__name__
        and record.getMessage().startswith("code_graph_query ")
    ]
    assert len(messages) == 1
    assert messages[0].startswith(
        "code_graph_query code=rebuild_failed count=1 duration_ms="
    )
    assert messages[0].removeprefix(
        "code_graph_query code=rebuild_failed count=1 duration_ms="
    ).isdigit()
    assert "secret-token" not in caplog.text
    assert "classified-query" not in caplog.text
    assert "/absolute/path" not in caplog.text
    assert "SELECT source" not in caplog.text


def test_query_guard_logs_unexpected_freshness_failure_once(
    ready_runtime, monkeypatch, caplog
):
    def fail_freshness(**_kwargs):
        raise RuntimeError(
            "secret-freshness /absolute/path SELECT source private-query"
        )

    monkeypatch.setattr(
        ready_runtime.runtime._indexer,
        "mark_dirty_if_stale",
        fail_freshness,
    )
    caplog.clear()

    with caplog.at_level("ERROR", logger=runtime_module.__name__):
        result = ready_runtime.runtime.search("private-query")

    assert result["code"] == "rebuild_failed"
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == runtime_module.__name__
        and record.getMessage().startswith("code_graph_query_guard ")
    ]
    assert len(messages) == 1
    assert messages[0].startswith(
        "code_graph_query_guard code=rebuild_failed count=1 duration_ms="
    )
    assert "secret-freshness" not in caplog.text
    assert "private-query" not in caplog.text
    assert "/absolute/path" not in caplog.text
    assert "SELECT source" not in caplog.text


def test_query_guard_does_not_log_typed_freshness_failure(
    ready_runtime, monkeypatch, caplog
):
    def fail_freshness(**_kwargs):
        raise CodeGraphStaleError()

    monkeypatch.setattr(
        ready_runtime.runtime._indexer,
        "mark_dirty_if_stale",
        fail_freshness,
    )
    caplog.clear()

    result = ready_runtime.runtime.search("run")

    assert result["code"] == "stale"
    assert "code_graph_query_guard" not in caplog.text


def test_query_uses_bounded_sql_candidates(search_connection):
    statements = []
    search_connection.set_trace_callback(statements.append)
    request = validate_search_request("run", kinds=["method"], limit=7)

    CodeGraphQuery("backend").search(search_connection, request)

    selects = [statement for statement in statements if statement.lstrip().startswith("SELECT")]
    assert len(selects) == 6
    assert all(" LIMIT " in statement for statement in selects)
    assert all("SELECT *" not in statement for statement in selects)
    assert sum(statement.count("CASE ") for statement in selects) == 1


def test_query_stops_before_lower_tiers_when_stronger_results_fill_limit(
    search_connection,
):
    statements = []
    search_connection.set_trace_callback(statements.append)
    request = validate_search_request("run", limit=1)

    results = CodeGraphQuery("backend").search(search_connection, request)

    selects = [
        statement
        for statement in statements
        if statement.lstrip().startswith("SELECT")
    ]
    assert [(item.qualified_name, item.match) for item in results] == [
        ("run", "qualified_exact"),
    ]
    assert len(selects) == 1
    assert all(" CASE " not in statement for statement in selects)


def test_exact_and_prefix_candidates_use_name_indexes(search_connection):
    statements = []
    search_connection.set_trace_callback(statements.append)
    request = validate_search_request("run", kinds=["method"], limit=7)

    CodeGraphQuery("backend").search(search_connection, request)

    selects = [statement for statement in statements if statement.lstrip().startswith("SELECT")]
    plans = [
        " ".join(
            str(row[3])
            for row in search_connection.execute(
                "EXPLAIN QUERY PLAN " + statement
            )
        )
        for statement in selects
    ]
    assert "idx_symbols_qualified" in plans[0]
    assert "idx_symbols_local" in plans[1]
    assert "idx_symbols_qualified" in plans[2]
    assert "idx_symbols_local" in plans[3]
    assert "idx_symbols_qualified" in plans[4]
    assert "idx_symbols_qualified" not in plans[5]
    assert "USE TEMP B-TREE FOR ORDER BY" not in plans[2]


def test_fallback_uses_file_outer_index_plan(search_connection):
    statements = []
    search_connection.set_trace_callback(statements.append)
    request = validate_search_request(
        "definitely_absent_needle",
        kinds=["method"],
        limit=7,
    )

    assert CodeGraphQuery("backend").search(search_connection, request) == ()

    fallback = [
        statement
        for statement in statements
        if statement.lstrip().startswith("SELECT")
    ][-1]
    plan = " ".join(
        str(row[3])
        for row in search_connection.execute("EXPLAIN QUERY PLAN " + fallback)
    )
    assert "idx_files_repository_path" in plan
    assert "idx_symbols_file" in plan
    assert "sqlite_autoindex_files_1" not in plan


def test_fallback_uses_sqlite_utf8_guard_instead_of_python_udf(
    search_connection,
):
    statements = []
    search_connection.set_trace_callback(statements.append)
    request = validate_search_request(
        "definitely_absent_needle",
        kinds=["method"],
        limit=7,
    )

    assert CodeGraphQuery("backend").search(search_connection, request) == ()

    fallback = [
        statement
        for statement in statements
        if statement.lstrip().startswith("SELECT")
    ][-1]
    assert "iwiki_code_has_non_ascii" not in fallback
    for expression in (
        "length(CAST(s.qualified_name AS BLOB)) > length(s.qualified_name)",
        "length(CAST(s.local_name AS BLOB)) > length(s.local_name)",
        "length(CAST(s.signature AS BLOB)) > length(s.signature)",
        "length(CAST(f.path AS BLOB)) > length(f.path)",
    ):
        assert expression in fallback


def test_fallback_excludes_only_returned_stronger_symbol_ids(
    search_connection,
):
    statements = []
    search_connection.set_trace_callback(statements.append)
    request = validate_search_request(
        "run",
        kinds=["method"],
        path="src/",
        limit=7,
    )

    results = CodeGraphQuery("backend").search(search_connection, request)

    fallback = [
        statement
        for statement in statements
        if statement.lstrip().startswith("SELECT")
    ][-1]
    stronger_ids = [
        item.symbol_id
        for item in results
        if item.match in {"qualified_exact", "local_exact", "canonical_prefix"}
    ]
    assert stronger_ids == [
        "symbol:qualified",
        "symbol:local",
        "symbol:prefix",
    ]
    assert "s.symbol_id NOT IN" in fallback
    assert all(f"'{symbol_id}'" in fallback for symbol_id in stronger_ids)
    assert fallback.count("'symbol:") == len(stronger_ids)
    assert "s.qualified_name <> 'run'" not in fallback


def test_no_hit_search_over_100k_symbols_stays_within_ci_budget():
    connection = _large_connection()
    request = validate_search_request("definitely_absent_needle", limit=20)
    query = CodeGraphQuery("backend")
    assert query.search(connection, request) == ()  # Warm cache and query plan.

    timings = []
    for _run in range(5):
        started = time.perf_counter()
        assert query.search(connection, request) == ()
        timings.append(time.perf_counter() - started)
    connection.close()

    assert max(timings) < 0.30  # 2x CI margin over provisional 150 ms target.


def test_common_exact_local_over_100k_symbols_uses_ordered_index_and_budget():
    connection = _large_connection(local_name="run")
    request = validate_search_request("run", limit=20)
    query = CodeGraphQuery("backend")
    statements = []
    connection.set_trace_callback(statements.append)
    assert len(query.search(connection, request)) == 20

    selects = [
        statement
        for statement in statements
        if statement.lstrip().startswith("SELECT")
    ]
    assert len(selects) == 3
    plan = " ".join(
        str(row[3])
        for row in connection.execute("EXPLAIN QUERY PLAN " + selects[2])
    )
    assert "idx_symbols_qualified" in plan
    assert "USE TEMP B-TREE FOR ORDER BY" not in plan

    timings = []
    for _run in range(5):
        started = time.perf_counter()
        assert len(query.search(connection, request)) == 20
        timings.append(time.perf_counter() - started)
    connection.close()

    assert max(timings) < 0.30  # 2x CI margin over provisional 150 ms target.
