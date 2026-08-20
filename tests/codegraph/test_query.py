"""Deterministic, bounded symbol search contracts."""
from __future__ import annotations

from pathlib import Path
import re
import sqlite3

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


@pytest.fixture
def search_connection(tmp_path):
    store = CodeGraphStore(tmp_path / "code.sqlite3")
    store.insert_snapshot(_snapshot())
    connection = store.open_existing()
    yield connection
    connection.close()


EXPECTED_MATCHES = [
    "qualified_exact",
    "local_exact",
    "alias_exact",
    "canonical_prefix",
    "alias_prefix",
    "canonical_lexical",
    "alias_lexical",
    "signature",
    "path",
]


def _typed_file(identity, path, *, module=None):
    local_name = path.rsplit("/", 1)[-1]
    return {
        "file_id": f"file:typed:{identity}",
        "repository_id": "backend",
        "path": path,
        "path_casefold": models_module.compact_casefold(path),
        "file_local_name": local_name,
        "file_name_tokens_casefold": models_module.token_key(local_name),
        "language": "python",
        "content_hash": f"hash:typed:{identity}",
        "parser_version": "fixture",
        "size_bytes": 10,
        "start_line": 1,
        "end_line": 1,
        "start_byte": 0,
        "end_byte": 10,
        "module_key": path,
        "module_id": None if module is None else f"module:typed:{identity}",
        "module_qualified_name": module,
        "module_local_name": None if module is None else module.rsplit(".", 1)[-1],
        "module_name_tokens_casefold": (
            None
            if module is None
            else models_module.token_key(module, module.rsplit(".", 1)[-1])
        ),
    }


def _typed_symbol(identity, file_identity, qualified, local, *, signature="()"):
    return {
        "symbol_id": f"symbol:typed:{identity}",
        "file_id": f"file:typed:{file_identity}",
        "kind": "method",
        "qualified_name": qualified,
        "local_name": local,
        "name_tokens_casefold": models_module.token_key(qualified, local),
        "start_line": 1,
        "end_line": 1,
        "start_byte": 0,
        "end_byte": 10,
        "signature": signature,
        "signature_casefold": models_module.compact_casefold(signature),
        "visibility": "public",
        "content_hash": f"symbol-hash:typed:{identity}",
        "metadata_json": "{}",
    }


def _alias_relation(identity, source_file, alias, *, target_module=None,
                    target_symbol=None, state="resolved", start_byte=0,
                    binding_kind="explicit_alias"):
    return {
        "relation_id": f"relation:typed:{identity}",
        "source_file_id": f"file:typed:{source_file}",
        "source_module_id": None,
        "source_symbol_id": None,
        "target_module_id": target_module,
        "target_symbol_id": target_symbol,
        "target_reference": (
            "external.member" if state == "partially_resolved" else None
        ),
        "relation_type": "IMPORTS",
        "source_start_line": 1,
        "source_end_line": 1,
        "source_start_byte": start_byte,
        "source_end_byte": start_byte + 1,
        "binding_name": alias,
        "binding_kind": binding_kind,
        "binding_name_tokens_casefold": models_module.token_key(alias),
        "confidence": 1.0,
        "resolution_state": state,
        "metadata_json": "{}",
    }


@pytest.fixture
def schema_v2_search_connection(tmp_path):
    snapshot = _snapshot()
    files = (
        _typed_file("qualified-file", "needle"),
        _typed_file("local-module", "typed/local.py", module="pkg.needle"),
        _typed_file("alias-exact-target", "typed/alias_exact.py"),
        _typed_file("canonical-prefix", "typed/prefix.py"),
        _typed_file("alias-prefix-target", "typed/alias_prefix.py"),
        _typed_file("canonical-lexical", "typed/lexical.py"),
        _typed_file(
            "alias-lexical-target",
            "typed/alias_lexical.py",
            module="pkg.AliasLexical",
        ),
        _typed_file("signature", "typed/signature.py"),
        _typed_file("path", "typed/needle-assets/asset.py"),
        _typed_file("source", "typed/source.py"),
        _typed_file("svc-a", "services/a.py", module="services.alpha"),
        _typed_file("svc-b", "services/b.py", module="services.beta"),
        _typed_file("unicode-alias-target", "typed/unicode_alias.py"),
        _typed_file("canonical-winner", "typed/canonical_winner.py"),
        _typed_file(
            "partial-module", "typed/partial_module.py",
            module="pkg.PartialModule",
        ),
        _typed_file("partial-symbol", "typed/partial_symbol.py"),
    )
    symbols = (
        _typed_symbol(
            "alias-exact-target", "alias-exact-target",
            "pkg.AliasExact", "alias_exact_target",
        ),
        _typed_symbol(
            "canonical-prefix", "canonical-prefix",
            "needle.prefix", "prefix_target",
        ),
        _typed_symbol(
            "alias-prefix-target", "alias-prefix-target",
            "pkg.AliasPrefix", "alias_prefix_target",
        ),
        _typed_symbol(
            "canonical-lexical", "canonical-lexical",
            "pkg.canonical_needle", "canonical_needle",
        ),
        _typed_symbol(
            "signature", "signature", "pkg.Signature", "signature_target",
            signature="(needle: str)",
        ),
        _typed_symbol(
            "unicode-alias-target", "unicode-alias-target",
            "pkg.AliasChoice", "alias_choice",
        ),
        _typed_symbol(
            "canonical-winner", "canonical-winner",
            "canonical_winner", "canonical_target",
        ),
        _typed_symbol(
            "partial-symbol", "partial-symbol",
            "pkg.PartialSymbol", "partial_symbol",
        ),
    )
    relations = (
        _alias_relation(
            "needle-exact", "source", "needle",
            target_symbol="symbol:typed:alias-exact-target",
        ),
        _alias_relation(
            "needle-prefix", "source", "needle_alias",
            target_symbol="symbol:typed:alias-prefix-target",
        ),
        _alias_relation(
            "needle-lexical", "source", "alias_needle",
            target_module="module:typed:alias-lexical-target",
        ),
        _alias_relation(
            "svc-a-1", "source", "svc", target_module="module:typed:svc-a",
            state="ambiguous", start_byte=10,
        ),
        _alias_relation(
            "svc-a-2", "source", "svc", target_module="module:typed:svc-a",
            state="ambiguous", start_byte=20,
        ),
        _alias_relation(
            "svc-b", "source", "svc", target_module="module:typed:svc-b",
            state="ambiguous", start_byte=30,
        ),
        _alias_relation(
            "unicode-high", "source", "Ω_unicode",
            target_symbol="symbol:typed:unicode-alias-target", start_byte=40,
        ),
        _alias_relation(
            "unicode-low", "source", "ä_unicode",
            target_symbol="symbol:typed:unicode-alias-target", start_byte=50,
        ),
        _alias_relation(
            "implicit-hidden", "source", "hidden_alias",
            target_symbol="symbol:typed:unicode-alias-target", start_byte=60,
            binding_kind="implicit_binding",
        ),
        _alias_relation(
            "canonical-winner", "source", "canonical_winner",
            target_symbol="symbol:typed:canonical-winner", start_byte=70,
        ),
        _alias_relation(
            "partial-module-exact", "source", "module-exact",
            target_module="module:typed:partial-module",
            state="partially_resolved", start_byte=80,
        ),
        _alias_relation(
            "partial-module-prefix", "source", "module-prefix-tail",
            target_module="module:typed:partial-module",
            state="partially_resolved", start_byte=90,
        ),
        _alias_relation(
            "partial-module-lexical", "source", "lexical module",
            target_module="module:typed:partial-module",
            state="partially_resolved", start_byte=100,
        ),
        _alias_relation(
            "partial-symbol-exact", "source", "symbol-exact",
            target_symbol="symbol:typed:partial-symbol",
            state="partially_resolved", start_byte=110,
        ),
        _alias_relation(
            "partial-symbol-prefix", "source", "symbol-prefix-tail",
            target_symbol="symbol:typed:partial-symbol",
            state="partially_resolved", start_byte=120,
        ),
        _alias_relation(
            "partial-symbol-lexical", "source", "lexical symbol",
            target_symbol="symbol:typed:partial-symbol",
            state="partially_resolved", start_byte=130,
        ),
    )
    typed_snapshot = {
        **snapshot,
        "files": (*snapshot["files"], *files),
        "symbols": (*snapshot["symbols"], *symbols),
        "relations": relations,
    }
    store = CodeGraphStore(tmp_path / "typed-code.sqlite3")
    store.insert_snapshot(typed_snapshot)
    connection = store.open_existing()
    yield connection
    connection.close()


def test_search_returns_typed_union_in_exact_rank_order(
    schema_v2_search_connection,
):
    results = CodeGraphQuery("backend").search(
        schema_v2_search_connection,
        validate_search_request("needle", limit=20),
    )

    typed_results = [item for item in results if ":typed:" in item.entity_id]
    assert [row.match for row in typed_results] == EXPECTED_MATCHES
    assert [row.entity_id for row in typed_results] == [
        "file:typed:qualified-file",
        "module:typed:local-module",
        "symbol:typed:alias-exact-target",
        "symbol:typed:canonical-prefix",
        "symbol:typed:alias-prefix-target",
        "symbol:typed:canonical-lexical",
        "module:typed:alias-lexical-target",
        "symbol:typed:signature",
        "file:typed:path",
    ]
    assert {row.entity_type for row in typed_results} == {
        "file", "module", "symbol",
    }
    assert all(
        row.entity_id in {row.file_id, row.module_id, row.symbol_id}
        for row in typed_results
    )

    limited = CodeGraphQuery("backend").search(
        schema_v2_search_connection,
        validate_search_request("needle", limit=7),
    )
    assert limited == results[:7]


def test_alias_aggregation_binds_remaining_public_limit_after_deduplication(
    schema_v2_search_connection,
):
    statements = []
    schema_v2_search_connection.set_trace_callback(statements.append)

    results = CodeGraphQuery("backend").search(
        schema_v2_search_connection,
        validate_search_request("svc", limit=1),
    )

    assert len(results) == 1
    assert results[0].matched_alias == "svc"
    assert results[0].alias_target_count == 2
    assert results[0].alias_ambiguous is True
    rank_selects = [
        statement
        for statement in statements
        if "/* iwiki-rank:" in statement
    ]
    assert len(rank_selects) == 3
    assert all("LIMIT 1" in statement.upper() for statement in rank_selects)


def test_search_stops_after_first_filled_rank(schema_v2_search_connection):
    statements = []
    schema_v2_search_connection.set_trace_callback(statements.append)

    results = CodeGraphQuery("backend").search(
        schema_v2_search_connection,
        validate_search_request("needle", limit=1),
    )

    assert [item.match for item in results] == ["qualified_exact"]
    assert [
        statement.split("*/", 1)[0].split(":", 1)[1].strip()
        for statement in statements
        if "/* iwiki-rank:" in statement
    ] == ["qualified_exact"]


def test_search_runs_all_ranks_once_when_no_rank_hits(search_connection):
    statements = []
    search_connection.set_trace_callback(statements.append)

    assert CodeGraphQuery("backend").search(
        search_connection,
        validate_search_request("no-such-result", limit=1),
    ) == ()

    assert [
        statement.split("*/", 1)[0].split(":", 1)[1].strip()
        for statement in statements
        if "/* iwiki-rank:" in statement
    ] == EXPECTED_MATCHES


def test_rank_queries_are_branch_specific_and_begin_with_rank_tag():
    request = validate_search_request("needle", kinds=["method"])

    queries = {
        name: query_module._rank_query("backend", request, name, ())
        for name in EXPECTED_MATCHES
    }

    for name, (sql, _parameters) in queries.items():
        assert sql.startswith(f"/* iwiki-rank:{name} */")
        assert "CASE" not in sql
    assert "relations AS r" not in queries["qualified_exact"][0]
    assert "relations AS r" in queries["alias_exact"][0]
    assert "qualified_name = ?" in queries["qualified_exact"][0]
    assert "local_name = ?" in queries["local_exact"][0]
    assert "name_tokens_casefold" in queries["canonical_lexical"][0]
    assert "signature_casefold" in queries["signature"][0]
    assert "path_casefold" in queries["path"][0]


def _rank_sql(request, name):
    sql, parameters = query_module._rank_query("backend", request, name, ())
    return sql, (*parameters, request.limit)


@pytest.mark.parametrize(
    ("kind", "present", "absent"),
    [
        (
            "file",
            "iwiki-entity:file",
            ("iwiki-entity:module", "iwiki-entity:symbol"),
        ),
        (
            "module",
            "iwiki-entity:module",
            ("iwiki-entity:file", "iwiki-entity:symbol"),
        ),
        (
            "method",
            "iwiki-entity:symbol",
            ("iwiki-entity:file", "iwiki-entity:module"),
        ),
    ],
)
def test_rank_sql_emits_only_requested_entity_branches(kind, present, absent):
    sql, _parameters = _rank_sql(
        validate_search_request("needle", kinds=[kind]),
        "canonical_lexical",
    )

    assert present in sql
    assert all(marker not in sql for marker in absent)


def test_rank_predicates_and_filters_are_inside_each_union_branch():
    request = validate_search_request(
        "needle", kinds=["file", "module", "method"], path="src/pkg"
    )
    sql, _parameters = _rank_sql(request, "canonical_lexical")

    branches, outer = sql.split("/* iwiki-after-branches */", 1)
    assert branches.count("f.repository_id = ?") == 3
    assert branches.count("f.language IN (?)") == 3
    assert branches.count("substr(f.path, 1, length(?)) = ?") == 3
    assert branches.count("name_tokens_casefold") >= 3
    assert "repository_id" not in outer
    assert "language" not in outer
    assert "kind IN" not in outer
    assert "path" not in outer
    assert "name_tokens_casefold" not in outer


def test_default_languages_is_configured_languages_not_all_known():
    request = validate_search_request(
        "foo", configured_languages=("python",),
    )
    assert request.languages == ("python",)


def test_explicit_languages_filter_validated_against_known_languages():
    request = validate_search_request(
        "foo", languages=["typescript"], configured_languages=("python", "typescript"),
    )
    assert request.languages == ("typescript",)


def test_unregistered_language_rejected():
    with pytest.raises(CodeGraphQueryError, match="unsupported language"):
        validate_search_request("foo", languages=["ruby"])


def test_snapshot_scoped_filter_reports_the_snapshot_languages():
    from iwiki_mcp.codegraph.query import CodeGraphLanguageUnavailableError

    with pytest.raises(CodeGraphLanguageUnavailableError) as failure:
        validate_search_request(
            "foo",
            languages=["typescript"],
            configured_languages=("javascript", "python"),
            languages_source="snapshot",
        )

    assert failure.value.code == "unsupported_language"
    assert failure.value.available == ("javascript", "python")


def test_snapshot_scope_keeps_invalid_config_for_unknown_languages():
    # A language this build cannot parse stays a contract error: no
    # republished snapshot would make it queryable.
    with pytest.raises(CodeGraphQueryError, match="unsupported language") as failure:
        validate_search_request(
            "foo",
            languages=["cobol"],
            configured_languages=("python",),
            languages_source="snapshot",
        )

    assert failure.value.code == "invalid_config"


def test_config_scoped_filter_keeps_the_generic_contract_error():
    with pytest.raises(CodeGraphQueryError, match="unsupported language") as failure:
        validate_search_request(
            "foo", languages=["typescript"], configured_languages=("python",)
        )

    assert failure.value.code == "invalid_config"


def test_multi_language_query_filters_both_languages_in_sql():
    request = validate_search_request(
        "foo", languages=["python", "typescript"],
        configured_languages=("python", "typescript"),
    )
    sql, params = query_module._canonical_rank_query(
        "domain", request, "qualified_exact", ()
    )
    assert "f.language IN (?, ?)" in sql
    assert "python" in params and "typescript" in params


def _query_plan(connection, request, name):
    sql, parameters = _rank_sql(request, name)
    return "\n".join(
        str(row[3])
        for row in connection.execute("EXPLAIN QUERY PLAN " + sql, parameters)
    )


@pytest.mark.parametrize(
    ("kind", "rank", "expected_index"),
    [
        ("file", "qualified_exact", "idx_files_repository_path"),
        ("file", "local_exact", "idx_files_repository_local"),
        (
            "module",
            "qualified_exact",
            "idx_files_repository_module_qualified",
        ),
        ("module", "local_exact", "idx_files_repository_module_local"),
        ("method", "qualified_exact", "idx_symbols_qualified"),
        ("method", "local_exact", "idx_symbols_local"),
    ],
)
def test_exact_rank_query_plan_uses_existing_endpoint_index(
    schema_v2_search_connection, kind, rank, expected_index
):
    plan = _query_plan(
        schema_v2_search_connection,
        validate_search_request("needle", kinds=[kind]),
        rank,
    )

    assert expected_index in plan


@pytest.mark.parametrize("name", EXPECTED_MATCHES)
def test_only_public_remaining_limit_is_bound(name):
    sql, _parameters = _rank_sql(
        validate_search_request("needle"),
        name,
    )

    assert re.findall(r"\bLIMIT\s+(?:\?|\d+)", sql, re.IGNORECASE) == [
        "LIMIT ?"
    ]


def test_alias_path_filter_counts_and_returns_target_entities(
    schema_v2_search_connection,
):
    results = CodeGraphQuery("backend").search(
        schema_v2_search_connection,
        validate_search_request(
            "svc", kinds=["module"], path="services/a", limit=20
        ),
    )

    assert [item.entity_id for item in results] == ["module:typed:svc-a"]
    assert results[0].path == "services/a.py"
    assert results[0].alias_target_count == 1
    assert results[0].alias_ambiguous is False


def test_alias_fanout_and_public_alias_are_deterministic(
    schema_v2_search_connection,
):
    query = CodeGraphQuery("backend")

    ambiguous = query.search(
        schema_v2_search_connection,
        validate_search_request("svc", kinds=["module"], limit=20),
    )
    assert [item.entity_id for item in ambiguous] == [
        "module:typed:svc-a",
        "module:typed:svc-b",
    ]
    assert all(item.alias_target_count == 2 for item in ambiguous)
    assert all(item.alias_ambiguous for item in ambiguous)

    alias_choice = query.search(
        schema_v2_search_connection,
        validate_search_request("unicode", kinds=["method"], limit=20),
    )
    assert len(alias_choice) == 1
    assert alias_choice[0].match == "alias_lexical"
    assert alias_choice[0].matched_alias == "ä_unicode"


def test_canonical_winner_deduplicates_alias_and_implicit_binding_is_hidden(
    schema_v2_search_connection,
):
    query = CodeGraphQuery("backend")

    canonical = query.search(
        schema_v2_search_connection,
        validate_search_request(
            "canonical_winner", kinds=["method"], limit=20
        ),
    )

    assert len(canonical) == 1
    assert canonical[0].entity_id == "symbol:typed:canonical-winner"
    assert canonical[0].match == "qualified_exact"
    assert canonical[0].matched_alias is None
    assert canonical[0].alias_target_count == 0
    assert query.search(
        schema_v2_search_connection,
        validate_search_request("hidden_alias", kinds=["method"], limit=20),
    ) == ()


def test_lower_alias_tiers_exclude_stronger_canonical_matches(
    schema_v2_search_connection,
):
    results = CodeGraphQuery("backend").search(
        schema_v2_search_connection,
        validate_search_request(
            "canonical_winner", kinds=["method"], limit=20
        ),
    )

    assert [(item.entity_id, item.match, item.matched_alias) for item in results] == [
        ("symbol:typed:canonical-winner", "qualified_exact", None),
    ]


@pytest.mark.parametrize(
    ("kind", "query", "match", "entity_id"),
    [
        ("module", "module-exact", "alias_exact", "module:typed:partial-module"),
        (
            "module", "module-prefix", "alias_prefix",
            "module:typed:partial-module",
        ),
        (
            "module", "module lexical", "alias_lexical",
            "module:typed:partial-module",
        ),
        ("method", "symbol-exact", "alias_exact", "symbol:typed:partial-symbol"),
        (
            "method", "symbol-prefix", "alias_prefix",
            "symbol:typed:partial-symbol",
        ),
        (
            "method", "symbol lexical", "alias_lexical",
            "symbol:typed:partial-symbol",
        ),
    ],
)
def test_partially_resolved_typed_aliases_participate_in_all_alias_tiers(
    schema_v2_search_connection, kind, query, match, entity_id
):
    results = CodeGraphQuery("backend").search(
        schema_v2_search_connection,
        validate_search_request(query, kinds=[kind], limit=20),
    )

    assert len(results) == 1
    assert results[0].entity_id == entity_id
    assert results[0].match == match
    assert results[0].alias_target_count == 1
    assert results[0].alias_ambiguous is False


class _LazyDatabaseFailure:
    def __iter__(self):
        return self

    def __next__(self):
        raise sqlite3.DatabaseError("secret lazy SQLite failure")


class _LazyFailureConnection:
    def execute(self, *_args, **_kwargs):
        return _LazyDatabaseFailure()


def test_query_maps_lazy_cursor_database_failure_to_store_error():
    with pytest.raises(CodeGraphStoreError, match="code graph search failed"):
        CodeGraphQuery("backend").search(
            _LazyFailureConnection(),
            validate_search_request("run", kinds=["method"]),
        )


@pytest.mark.parametrize("kind", sorted(query_module.KNOWN_ENTITY_KINDS))
def test_search_accepts_all_six_public_kinds(kind):
    assert validate_search_request("entity", kinds=[kind]).kinds == (kind,)


def test_percent_and_underscore_are_literal_search_characters(search_connection):
    search_connection.executemany(
        _FILE_INSERT_SQL,
        (
            _file_insert_values(
                "file:wildcard-percent", "wildcards/percent.py",
                "python", "hash:wildcard-percent",
            ),
            _file_insert_values(
                "file:wildcard-underscore", "wildcards/underscore.py",
                "python", "hash:wildcard-underscore",
            ),
        ),
    )
    search_connection.executemany(
        _SYMBOL_INSERT_SQL,
        (
            _symbol_insert_values(
                "symbol:wildcard-percent", "file:wildcard-percent",
                "pkg.PercentLiteral", "percentLiteral", "(value: '%')",
                "hash:symbol-wildcard-percent",
            ),
            _symbol_insert_values(
                "symbol:wildcard-underscore", "file:wildcard-underscore",
                "pkg.UnderscoreLiteral", "underscoreLiteral", "(value_name: str)",
                "hash:symbol-wildcard-underscore",
            ),
        ),
    )
    query = CodeGraphQuery("backend")

    percent = query.search(
        search_connection,
        validate_search_request("%", kinds=["method"], path="wildcards/"),
    )
    underscore = query.search(
        search_connection,
        validate_search_request("_", kinds=["method"], path="wildcards/"),
    )

    assert [(item.symbol_id, item.match) for item in percent] == [
        ("symbol:wildcard-percent", "signature"),
    ]
    assert [(item.symbol_id, item.match) for item in underscore] == [
        ("symbol:wildcard-underscore", "signature"),
    ]


def test_query_uses_no_python_sqlite_udf():
    source = Path("src/iwiki_mcp/codegraph/query.py").read_text(encoding="utf-8")
    assert ".create_function(" not in source
    assert "FTS" not in source.upper()
    assert "SEARCH_ENTITIES" not in source


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
    assert ready["results"][0]["module_id"] is not None
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
