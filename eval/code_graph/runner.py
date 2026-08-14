"""Reproducible code graph quality and projection-free performance gates."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import math
import os
from pathlib import Path
import platform
import re
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from typing import Callable, Mapping

from iwiki_mcp.codegraph import models, query as query_module
from iwiki_mcp.codegraph.config import CodeGraphConfig
from iwiki_mcp.codegraph.context import (
    CodeGraphContext,
    validate_context_request,
)
from iwiki_mcp.codegraph.discovery import discover_sources
from iwiki_mcp.codegraph.indexer import AdapterFactory, CodeGraphIndexer
from iwiki_mcp.codegraph.languages.python import PythonAdapter
from iwiki_mcp.codegraph.location import CodeGraphPaths
from iwiki_mcp.codegraph.query import (
    CodeGraphQuery,
    MATCH_RANK,
    validate_search_request,
)
from iwiki_mcp.codegraph.resolver import SymbolIndex
from iwiki_mcp.codegraph.schema import (
    INDEXES,
    SCHEMA_VERSION,
    TABLES,
    create_schema,
)

from .report import write_report


DOMAIN = "benchmark"
SEARCH_ENTITY_COUNT = 100_008
SEARCH_FILE_COUNT = 34_000
SEARCH_SYMBOL_COUNT = SEARCH_ENTITY_COUNT - (SEARCH_FILE_COUNT * 2)
BUILD_FILE_COUNT = 1_000
MEMORY_FILE_COUNT = 10_000
MIB = 1024 ** 2
POST_V1_SEARCH_TARGET_MS = 150.0

DEFAULT_THRESHOLDS = {
    "declarations": 0.98,
    "methods": 0.98,
    "local_imports": 0.95,
    "static_calls": 0.75,
    "false_resolved_calls": 0.05,
    "deterministic_rebuild": 1.0,
    "startup_ms": 500.0,
    "noop_ms": 200.0,
    "build_1000_files_ms": 15_000.0,
    "search_ms": 500.0,
    "context_ms": 300.0,
    "db_source_ratio": 3.0,
    "peak_memory_10000_files_bytes": float(1024 ** 3),
}

_OPERATORS: dict[str, tuple[str, Callable[[float, float], bool]]] = {
    "declarations": (">=", lambda actual, threshold: actual >= threshold),
    "methods": (">=", lambda actual, threshold: actual >= threshold),
    "local_imports": (">=", lambda actual, threshold: actual >= threshold),
    "static_calls": (">=", lambda actual, threshold: actual >= threshold),
    "false_resolved_calls": ("<", lambda actual, threshold: actual < threshold),
    "deterministic_rebuild": (">=", lambda actual, threshold: actual >= threshold),
    "startup_ms": ("<", lambda actual, threshold: actual < threshold),
    "noop_ms": ("<", lambda actual, threshold: actual < threshold),
    "build_1000_files_ms": ("<", lambda actual, threshold: actual < threshold),
    "search_ms": ("<", lambda actual, threshold: actual < threshold),
    "context_ms": ("<", lambda actual, threshold: actual < threshold),
    "db_source_ratio": ("<", lambda actual, threshold: actual < threshold),
    "peak_memory_10000_files_bytes": (
        "<", lambda actual, threshold: actual < threshold
    ),
}


class BenchmarkGateError(RuntimeError):
    """Raised only after failed benchmark evidence has been written."""


class _NoopWikiSelectorResolver:
    def resolve(self, **_kwargs):
        return ()


def _distribution_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unavailable"


def _elapsed_ms(started_ns: int) -> float:
    return round((time.perf_counter_ns() - started_ns) / 1_000_000, 6)


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 6) if values else 0.0


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 1.0


def _hash_rows(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for table in TABLES:
        digest.update(table.encode("ascii"))
        digest.update(b"\0")
        for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1"):
            digest.update(
                json.dumps(
                    list(row), ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            )
            digest.update(b"\n")
    return digest.hexdigest()


def _hash_corpus(parts: list[bytes]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


@dataclass(frozen=True)
class ProductionCorpus:
    project: Path
    accepted_paths: tuple[str, ...]
    accepted_source_bytes: int
    sha256: str


def _write_production_tree(project: Path, count: int) -> ProductionCorpus:
    project.mkdir(parents=True, exist_ok=True)
    quality_sources = (
        (
            "quality/__init__.py",
            ('"""quality package ' + ("x" * 2048) + '"""\n').encode(
                "ascii"
            ),
        ),
        (
            "quality/base.py",
            (
                '"""quality base ' + ("x" * 2048) + '"""\n'
                "def known(value: int) -> int:\n"
                "    return value + 1\n\n"
                "def helper(value: int) -> int:\n"
                "    return value + 2\n\n"
                "class Service:\n"
                "    def run(self, value: int) -> int:\n"
                "        return value\n"
            ).encode("ascii"),
        ),
        (
            "quality/consumer.py",
            (
                '"""quality consumer ' + ("x" * 2048) + '"""\n'
                "from quality.base import known as known_alias\n"
                "from quality.base import helper\n\n"
                "def use(value: int) -> int:\n"
                "    return known_alias(value) + helper(value)\n\n"
                "def dynamic(value: int) -> int:\n"
                "    return external(value)\n"
            ).encode("ascii"),
        ),
        (
            "quality/unicode.py",
            (
                '"""quality unicode ' + ("x" * 2048) + '"""\n'
                "def straße(value: int) -> int:\n"
                "    return value\n"
            ).encode("utf-8"),
        ),
    )
    for index in range(count):
        if index < len(quality_sources):
            relative, source = quality_sources[index]
        else:
            relative = f"pkg_{index:05d}.py"
            source = (
                f'"""benchmark source {index:05d} ' + ("x" * 2048) + '\n"""\n'
                f"def symbol_{index:05d}(value: int) -> int:\n"
                f"    return value + {index}\n"
            ).encode("ascii")
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source)
    return _discover_production_corpus(project, max_total_files=count)


def _discover_production_corpus(
    project: Path,
    *,
    max_total_files: int,
) -> ProductionCorpus:
    snapshot = discover_sources(
        project,
        CodeGraphConfig(
            auto_rebuild="off",
            max_rebuild_seconds=120,
            max_total_files=max_total_files,
        ),
        extensions=(".py",),
    )
    paths = tuple(item.path for item in snapshot.files)
    total = sum(item.size_bytes for item in snapshot.files)
    parts = [
        item.path.encode("utf-8") + b"\0" + item.content
        for item in snapshot.files
    ]
    return ProductionCorpus(
        project=project,
        accepted_paths=paths,
        accepted_source_bytes=total,
        sha256=_hash_corpus(parts),
    )


def _file_row(index: int) -> tuple:
    path = (
        f"src/共有/深い/階層/module_{index:05d}.py"
        if index < 500
        else f"src/ascii/module_{index:05d}.py"
    )
    local = Path(path).name
    qualified = (
        "duplicate.module" if index in {3, 4}
        else f"corpus.module_{index:05d}"
    )
    stable_file_id = models.file_id("python", "py", DOMAIN, path)
    stable_module_id = models.module_id(
        "python", "py", DOMAIN, path, qualified
    )
    size = max(320, len(path.encode("utf-8")) + 256)
    return (
        stable_file_id,
        DOMAIN,
        path,
        models.compact_casefold(path),
        local,
        models.token_key(local),
        "python",
        hashlib.sha256(path.encode("utf-8")).hexdigest(),
        "benchmark",
        size,
        1,
        12,
        0,
        size,
        models.module_key(path),
        stable_module_id,
        qualified,
        qualified.rsplit(".", 1)[-1],
        models.token_key(qualified, qualified.rsplit(".", 1)[-1]),
    )


def _symbol_row(index: int, files: list[tuple]) -> tuple:
    file_row = files[index % len(files)]
    if index == 0:
        qualified, local, signature = "alpha.TargetExact", "TargetExact", "()"
    elif index == 1:
        qualified, local, signature = "beta.LocalExact", "NeedleLocal", "()"
    elif index == 2:
        qualified, local, signature = "prefix.CanonicalPrefixOnly", "Other", "()"
    elif index == 3:
        qualified, local, signature = "lexical.some_token_target", "Other", "()"
    elif index == 4:
        qualified, local, signature = "signature.Subject", "Other", "(δelta: str)"
    elif index == 5:
        qualified, local, signature = "pkg.Straße", "Straße", "()"
    else:
        local = f"Symbol{index:06d}"
        qualified = f"corpus.module_{index % SEARCH_FILE_COUNT:05d}.{local}"
        signature = f"(value_{index % 97}: int)"
    stable_symbol_id = models.symbol_id(
        "python",
        "py",
        DOMAIN,
        file_row[14],
        qualified,
        signature,
    )
    return (
        stable_symbol_id,
        file_row[0],
        "function",
        qualified,
        local,
        (
            models.token_key("lexical.some_token_target", "some_token_target")
            if index == 3
            else models.token_key(qualified, local)
        ),
        2,
        3,
        16,
        64,
        signature,
        models.compact_casefold(signature),
        "public",
        hashlib.sha256(qualified.encode("utf-8")).hexdigest(),
        "{}",
    )


def _relation_row(
    index: int,
    source_file: tuple,
    target_symbol: tuple,
    alias: str,
    *,
    state: str = "resolved",
) -> tuple:
    identity = hashlib.sha256(
        f"{index}\0{source_file[0]}\0{target_symbol[0]}\0{alias}".encode("utf-8")
    ).hexdigest()
    return (
        f"py:relation:{identity}",
        source_file[0],
        source_file[15],
        None,
        None,
        target_symbol[0],
        None,
        "IMPORTS",
        1,
        1,
        0,
        12,
        alias,
        "explicit_alias",
        models.token_key(alias),
        1.0,
        state,
        "{}",
    )


def _function_signatures(connection: sqlite3.Connection) -> set[tuple]:
    return {
        (str(row[0]), str(row[3]), int(row[4]))
        for row in connection.execute("PRAGMA function_list")
    }


def _constraint_measurement(
    connection: sqlite3.Connection,
    baseline_function_signatures: set[tuple],
) -> tuple[dict, dict]:
    schema_rows = tuple(connection.execute(
        "SELECT type, name, COALESCE(sql, '') FROM sqlite_master "
        "WHERE type IN ('table', 'index') ORDER BY type, name"
    ))
    table_sql = {
        str(name): str(sql)
        for object_type, name, sql in schema_rows
        if object_type == "table"
    }
    explicit_indexes = {
        str(name)
        for object_type, name, sql in schema_rows
        if object_type == "index" and sql
    }
    fts_tables = sorted(
        name for name, sql in table_sql.items()
        if "fts" in name.casefold() or "using fts" in sql.casefold()
    )
    search_projection_tables = sorted(
        name for name in table_sql
        if any(
            value in name.casefold()
            for value in ("projection", "search_index", "search_document")
        )
    )
    added_function_signatures = (
        _function_signatures(connection) - baseline_function_signatures
    )
    python_sqlite_udf_names = sorted({
        name for name, _encoding, _argument_count in added_function_signatures
    })
    request = validate_search_request("benchmark", limit=1)
    rank_limit_clause_counts = {}
    final_public_limit_only = True
    for rank in MATCH_RANK:
        sql, _parameters = query_module._rank_query(DOMAIN, request, rank, ())
        count = len(re.findall(r"\bLIMIT\b", sql, flags=re.IGNORECASE))
        rank_limit_clause_counts[rank] = count
        final_public_limit_only = (
            final_public_limit_only
            and count == 1
            and sql.rstrip().endswith("LIMIT ?")
        )
    unapproved_explicit_indexes = sorted(explicit_indexes - set(INDEXES))
    evidence = {
        "python_sqlite_udf_names": python_sqlite_udf_names,
        "baseline_function_signature_count": len(baseline_function_signatures),
        "unapproved_explicit_indexes": unapproved_explicit_indexes,
        "search_projection_tables": search_projection_tables,
        "fts_tables": fts_tables,
        "rank_limit_clause_counts": rank_limit_clause_counts,
    }
    constraints = {
        "fts": bool(fts_tables),
        "python_sqlite_udf": bool(python_sqlite_udf_names),
        "search_projection": bool(search_projection_tables),
        "candidate_cap": not final_public_limit_only,
    }
    return constraints, evidence


def _create_search_corpus(database: Path) -> dict:
    connection = sqlite3.connect(database)
    try:
        baseline_function_signatures = _function_signatures(connection)
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")
        create_schema(connection)
        connection.execute("PRAGMA journal_mode = OFF")
        repository = (
            DOMAIN,
            ".",
            None,
            None,
            "1" * 64,
            "2" * 64,
            "3" * 64,
            models.NORMALIZER_VERSION,
            models.UNICODE_DATA_VERSION,
            "4" * 64,
            "ready",
            "2000-01-01T00:00:00Z",
        )
        connection.execute(
            "INSERT INTO repositories VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            repository,
        )
        files = [_file_row(index) for index in range(SEARCH_FILE_COUNT)]
        connection.executemany(
            "INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            files,
        )
        symbols = [_symbol_row(index, files) for index in range(SEARCH_SYMBOL_COUNT)]
        connection.executemany(
            "INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            symbols,
        )
        aliases = [
            _relation_row(0, files[0], symbols[0], "AliasExactOnly"),
            _relation_row(1, files[1], symbols[1], "AliasPrefixNeedle"),
            _relation_row(2, files[2], symbols[2], "Alias Lexical Needle"),
            _relation_row(3, files[3], symbols[3], "RepeatedAlias"),
            _relation_row(4, files[4], symbols[3], "RepeatedAlias"),
            _relation_row(5, files[5], symbols[4], "AmbiguousAlias", state="ambiguous"),
            _relation_row(6, files[6], symbols[5], "AmbiguousAlias", state="ambiguous"),
        ]
        connection.executemany(
            "INSERT INTO relations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            aliases,
        )
        connection.commit()
        entity_count = connection.execute(
            "SELECT (SELECT COUNT(*) FROM files) + "
            "(SELECT COUNT(module_id) FROM files) + "
            "(SELECT COUNT(*) FROM symbols)"
        ).fetchone()[0]
        constraints, constraint_evidence = _constraint_measurement(
            connection, baseline_function_signatures
        )
        corpus_parts = [
            json.dumps(files, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            json.dumps(symbols, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            json.dumps(aliases, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        ]
        return {
            "connection": connection,
            "files": files,
            "symbols": symbols,
            "aliases": aliases,
            "entity_count": int(entity_count),
            "file_count": len(files),
            "module_count": len(files),
            "symbol_count": len(symbols),
            "sha256": _hash_corpus(corpus_parts),
            "constraints": constraints,
            "constraint_evidence": constraint_evidence,
        }
    except BaseException:
        connection.close()
        raise


def _timed_search(engine, connection, request):
    started = time.perf_counter_ns()
    matched = engine.search(connection, request)
    elapsed = (time.perf_counter_ns() - started) / 1_000_000
    return round(elapsed, 6), matched


def _nearest_rank_p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * 0.95) - 1]


def _measure_search(connection: sqlite3.Connection, corpus: Mapping) -> list[dict]:
    symbols = corpus["symbols"]
    cases = (
        ("qualified_exact", "alpha.TargetExact", "qualified_exact", symbols[0][0]),
        ("local_exact", "NeedleLocal", "local_exact", symbols[1][0]),
        ("alias_exact", "AliasExactOnly", "alias_exact", symbols[0][0]),
        ("canonical_prefix", "prefix.Canon", "canonical_prefix", symbols[2][0]),
        ("alias_prefix", "AliasPrefix", "alias_prefix", symbols[1][0]),
        (
            "canonical_lexical",
            "some token target",
            "canonical_lexical",
            symbols[3][0],
        ),
        (
            "alias_lexical",
            "alias lexical needle",
            "alias_lexical",
            symbols[2][0],
        ),
        ("signature", "δelta", "signature", symbols[4][0]),
        ("path", "共有/深い/階層", "path", symbols[0][0]),
    )
    engine = CodeGraphQuery(DOMAIN)
    results = []
    for name, text, expected_rank, expected_id in cases:
        request = validate_search_request(text, limit=1)
        cold_ms, cold_matches = _timed_search(engine, connection, request)
        warmup_matches = engine.search(connection, request)
        warm_runs = [
            _timed_search(engine, connection, request)
            for _sample in range(10)
        ]
        warm_samples = [elapsed for elapsed, _matched in warm_runs]
        matched = warm_runs[-1][1]
        observed_ids = [item.entity_id for item in matched]
        results.append({
            "name": name,
            "query": text,
            "expected_rank": expected_rank,
            "expected_result_ids": [expected_id],
            "observed_rank": matched[0].match if matched else None,
            "observed_result_ids": observed_ids,
            "cold_ms": cold_ms,
            "cold_result_ids": [item.entity_id for item in cold_matches],
            "warmup_runs": 1,
            "warmup_result_ids": [item.entity_id for item in warmup_matches],
            "warm_samples_ms": warm_samples,
            "warm_median_ms": round(statistics.median(warm_samples), 6),
            "warm_p95_ms": round(_nearest_rank_p95(warm_samples), 6),
            "warm_max_ms": round(max(warm_samples), 6),
        })
    return results


def _mark_post_v1_targets(search_cases: list[dict]) -> list[dict]:
    return [
        {
            **case,
            "meets_post_v1_target": (
                float(case["warm_max_ms"]) < POST_V1_SEARCH_TARGET_MS
            ),
        }
        for case in search_cases
    ]


def _measure_context(connection: sqlite3.Connection, seed: str) -> float:
    request = validate_context_request(
        [seed], depth=1, include_wiki=False, max_nodes=50
    )
    engine = CodeGraphContext(DOMAIN, None, 1_000_000)
    engine.context(connection, request)
    samples = []
    for _repeat in range(3):
        started = time.perf_counter_ns()
        result = engine.context(connection, request)
        samples.append(_elapsed_ms(started))
        if not result["nodes"]:
            raise RuntimeError("context benchmark seed was not found")
    return round(max(samples), 6)


def _measure_memory(corpus: ProductionCorpus) -> int:
    config = CodeGraphConfig(
        auto_rebuild="off",
        max_rebuild_seconds=120,
        max_total_files=len(corpus.accepted_paths),
    )
    tracemalloc.start()
    try:
        snapshot = discover_sources(corpus.project, config, extensions=(".py",))
        adapter = PythonAdapter(
            DOMAIN,
            tuple(item.path for item in snapshot.files),
            parser_version="benchmark",
        )
        parsed = tuple(
            adapter.parse_file(item.content, item.path)
            for item in snapshot.files
        )
        index = SymbolIndex.from_parsed_files(parsed)
        tuple(adapter.resolve_references(item, index) for item in parsed)
        _current, peak = tracemalloc.get_traced_memory()
        return peak
    finally:
        tracemalloc.stop()


def _benchmark_indexer(root: Path, project: Path, count: int) -> CodeGraphIndexer:
    base = root / "base"
    cache = base / ".iwiki"
    cache.mkdir(parents=True)
    paths = CodeGraphPaths(
        database=cache / f"code-{DOMAIN}.sqlite3",
        wal=cache / f"code-{DOMAIN}.sqlite3-wal",
        shm=cache / f"code-{DOMAIN}.sqlite3-shm",
        lock=cache / f"code-{DOMAIN}.lock",
        metadata=cache / f"code-{DOMAIN}.metadata.json",
    )
    parser_version = "tree-sitter-python:" + _distribution_version(
        "tree-sitter-python"
    )

    def create_adapter(source_paths):
        return PythonAdapter(DOMAIN, source_paths, parser_version=parser_version)

    return CodeGraphIndexer(
        cache_base=str(base),
        project_dir=str(project),
        domain=DOMAIN,
        config=CodeGraphConfig(
            auto_rebuild="off",
            max_rebuild_seconds=120,
            max_total_files=count,
        ),
        paths=paths,
        adapter_factories={
            "python": AdapterFactory(
                create=create_adapter,
                extensions=(".py",),
                parser_version=parser_version,
                grammar_version=(
                    "tree-sitter:" + _distribution_version("tree-sitter")
                ),
                adapter_version="python-adapter-v2",
            )
        },
        resolver_version="resolver-v1",
        wiki_selector_resolver=_NoopWikiSelectorResolver(),
    )


_SEMANTIC_SELECTS = {
    "repositories": (
        "repository_id, root_path, git_remote, git_commit, "
        "source_fingerprint, config_fingerprint, parser_fingerprint, "
        "normalizer_version, unicode_data_version, revision"
    ),
    "files": "*",
    "symbols": "*",
    "relations": "*",
    "wiki_code_links": "*",
}


def _semantic_snapshot(connection: sqlite3.Connection) -> dict[str, tuple]:
    rows = {}
    for table, columns in _SEMANTIC_SELECTS.items():
        rows[table] = tuple(
            connection.execute(f"SELECT {columns} FROM {table} ORDER BY 1")
        )
    return rows


def _snapshot_hash(snapshot: Mapping[str, tuple]) -> str:
    return _hash_corpus([
        json.dumps(
            [table, rows], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        for table, rows in sorted(snapshot.items())
    ])


def _snapshot_ids(connection: sqlite3.Connection) -> dict[str, tuple[str, ...]]:
    identifiers = {
        "file_ids": "SELECT file_id FROM files ORDER BY file_id",
        "module_ids": (
            "SELECT module_id FROM files WHERE module_id IS NOT NULL "
            "ORDER BY module_id"
        ),
        "symbol_ids": "SELECT symbol_id FROM symbols ORDER BY symbol_id",
        "relation_ids": "SELECT relation_id FROM relations ORDER BY relation_id",
        "link_ids": "SELECT link_id FROM wiki_code_links ORDER BY link_id",
    }
    return {
        name: tuple(str(row[0]) for row in connection.execute(sql))
        for name, sql in identifiers.items()
    }


def _measure_production_quality(
    connection: sqlite3.Connection,
) -> tuple[dict[str, float], dict[str, int]]:
    expected_symbols = {
        "quality.base.Service",
        "quality.base.Service.run",
        "quality.base.helper",
        "quality.base.known",
        "quality.consumer.dynamic",
        "quality.consumer.use",
        "quality.unicode.straße",
    }
    observed_symbols = {
        str(row[0])
        for row in connection.execute(
            "SELECT qualified_name FROM symbols WHERE qualified_name LIKE 'quality.%'"
        )
    }
    expected_methods = {"quality.base.Service.run"}
    observed_methods = {
        str(row[0])
        for row in connection.execute(
            "SELECT qualified_name FROM symbols WHERE kind = 'method'"
        )
    }
    import_rows = tuple(connection.execute(
        "SELECT r.source_start_line, r.binding_name, r.resolution_state "
        "FROM relations AS r JOIN files AS f ON f.file_id = r.source_file_id "
        "WHERE f.path = 'quality/consumer.py' AND r.relation_type = 'IMPORTS'"
    ))
    expected_imports = {(2, "known_alias"), (3, "helper")}
    resolved_imports = {
        (int(line), str(binding))
        for line, binding, state in import_rows
        if state in {"resolved", "ambiguous"}
    }
    call_rows = tuple(connection.execute(
        "SELECT r.source_start_line, r.resolution_state, s.qualified_name "
        "FROM relations AS r JOIN files AS f ON f.file_id = r.source_file_id "
        "LEFT JOIN symbols AS s ON s.symbol_id = r.target_symbol_id "
        "WHERE f.path = 'quality/consumer.py' AND r.relation_type = 'CALLS'"
    ))
    expected_calls = {
        (6, "quality.base.known"),
        (6, "quality.base.helper"),
    }
    resolved_calls = {
        (int(line), str(qualified_name))
        for line, state, qualified_name in call_rows
        if state in {"resolved", "ambiguous"} and qualified_name is not None
    }
    falsely_resolved = {
        (int(line), str(qualified_name))
        for line, state, qualified_name in call_rows
        if line == 9
        and state in {"resolved", "ambiguous"}
        and qualified_name is not None
    }
    quality = {
        "declarations": _ratio(
            len(observed_symbols & expected_symbols), len(expected_symbols)
        ),
        "methods": _ratio(
            len(observed_methods & expected_methods), len(expected_methods)
        ),
        "local_imports": _ratio(
            len(resolved_imports & expected_imports), len(expected_imports)
        ),
        "static_calls": _ratio(
            len(resolved_calls & expected_calls), len(expected_calls)
        ),
        "false_resolved_calls": _ratio(
            len(falsely_resolved), len(resolved_calls)
        ),
    }
    details = {
        "expected_declarations": len(expected_symbols),
        "observed_declarations": len(observed_symbols & expected_symbols),
        "expected_methods": len(expected_methods),
        "observed_methods": len(observed_methods & expected_methods),
        "expected_local_imports": len(expected_imports),
        "resolved_local_imports": len(resolved_imports & expected_imports),
        "expected_static_calls": len(expected_calls),
        "resolved_static_calls": len(resolved_calls & expected_calls),
        "false_resolved_calls": len(falsely_resolved),
        "resolved_calls": len(resolved_calls),
    }
    return quality, details


def _checkpoint_database(database: Path) -> int:
    connection = sqlite3.connect(database)
    try:
        result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result is None or result[0] != 0:
            raise RuntimeError("production database checkpoint failed")
    finally:
        connection.close()
    wal = Path(f"{database}-wal")
    if wal.exists() and wal.stat().st_size:
        raise RuntimeError("production database WAL was not truncated")
    return database.stat().st_size


def _measure_build(root: Path, corpus: ProductionCorpus) -> dict:
    indexer = _benchmark_indexer(root, corpus.project, len(corpus.accepted_paths))
    started = time.perf_counter_ns()
    first = indexer.build(force=True)
    build_ms = _elapsed_ms(started)
    with indexer.store.read_lease() as connection:
        first_snapshot = _semantic_snapshot(connection)
        first_ids = _snapshot_ids(connection)
    first_revision = str(first["revision"])

    started = time.perf_counter_ns()
    noop = indexer.build(force=False)
    noop_ms = _elapsed_ms(started)

    started = time.perf_counter_ns()
    second = indexer.build(force=True)
    forced_ms = _elapsed_ms(started)
    with indexer.store.read_lease() as connection:
        second_snapshot = _semantic_snapshot(connection)
        second_ids = _snapshot_ids(connection)
        accepted_rows = tuple(connection.execute(
            "SELECT path, size_bytes FROM files ORDER BY path"
        ))
        accepted_paths = tuple(str(row[0]) for row in accepted_rows)
        accepted_source_bytes = sum(int(row[1]) for row in accepted_rows)
        if (
            accepted_paths != corpus.accepted_paths
            or accepted_source_bytes != corpus.accepted_source_bytes
        ):
            raise RuntimeError("published production sources differ from discovery")
        quality, quality_details = _measure_production_quality(connection)
        seed = connection.execute(
            "SELECT symbol_id FROM symbols ORDER BY symbol_id LIMIT 1"
        ).fetchone()[0]
        context_ms = _measure_context(connection, str(seed))
    second_revision = str(second["revision"])
    database_bytes = _checkpoint_database(indexer.paths.database)
    first_hash = _snapshot_hash(first_snapshot)
    second_hash = _snapshot_hash(second_snapshot)
    return {
        "build_ms": build_ms,
        "noop_ms": noop_ms,
        "forced_build_ms": forced_ms,
        "context_ms": context_ms,
        "database_bytes": database_bytes,
        "accepted_paths": accepted_paths,
        "accepted_source_bytes": accepted_source_bytes,
        "quality": quality,
        "quality_counts": quality_details,
        "first_revision": first_revision,
        "second_revision": second_revision,
        "first_semantic_row_hash": first_hash,
        "second_semantic_row_hash": second_hash,
        "semantic_row_hash_equal": first_hash == second_hash,
        "entity_relation_link_ids_equal": first_ids == second_ids,
        "revision_equal": first_revision == second_revision,
        "noop_reported": bool(noop.get("no_op")),
        "quality_provenance": {
            "production_measurement": "canonical_production_database",
            "production_corpus_sha256": corpus.sha256,
            "production_revision": second_revision,
            "production_metrics": [
                "declarations",
                "methods",
                "local_imports",
                "static_calls",
                "false_resolved_calls",
                "deterministic_rebuild",
            ],
        },
    }


def _measure_startup() -> float:
    samples = []
    for _repeat in range(5):
        code = (
            "import time; s=time.perf_counter_ns(); "
            "import iwiki_mcp.codegraph.runtime; "
            "print((time.perf_counter_ns()-s)/1000000)"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
        )
        samples.append(float(completed.stdout.strip()))
    return round(max(samples), 6)


def _search_correctness(connection: sqlite3.Connection, corpus: Mapping) -> dict:
    engine = CodeGraphQuery(DOMAIN)
    files = corpus["files"]
    symbols = corpus["symbols"]

    duplicate = engine.search(
        connection, validate_search_request("duplicate.module", limit=2)
    )
    repeated = engine.search(
        connection, validate_search_request("RepeatedAlias", limit=1)
    )
    ambiguous = engine.search(
        connection, validate_search_request("AmbiguousAlias", limit=2)
    )
    unicode_result = engine.search(
        connection, validate_search_request("PKG STRASSE", limit=1)
    )

    duplicate_expected = {files[3][15], files[4][15]}
    ambiguous_expected = {symbols[4][0], symbols[5][0]}
    return {
        "duplicate_module_correctness": 1.0 if (
            {item.entity_id for item in duplicate} == duplicate_expected
            and all(item.match == "qualified_exact" for item in duplicate)
        ) else 0.0,
        "repeated_alias_correctness": 1.0 if (
            len(repeated) == 1
            and repeated[0].entity_id == symbols[3][0]
            and repeated[0].matched_alias == "RepeatedAlias"
            and repeated[0].alias_target_count == 1
        ) else 0.0,
        "ambiguous_alias_correctness": 1.0 if (
            {item.entity_id for item in ambiguous} == ambiguous_expected
            and all(item.alias_ambiguous for item in ambiguous)
            and all(item.alias_target_count == 2 for item in ambiguous)
        ) else 0.0,
        "unicode_correctness": 1.0 if (
            len(unicode_result) == 1
            and unicode_result[0].entity_id == symbols[5][0]
            and unicode_result[0].match == "canonical_lexical"
            and symbols[5][5] == "\x1fpkg\x1fstrasse\x1f"
            and models.token_key("é") != models.token_key("e\u0301")
        ) else 0.0,
    }


def _evaluate_gates(
    quality: Mapping[str, float],
    performance: Mapping[str, object],
    thresholds: Mapping[str, float],
) -> dict[str, dict[str, object]]:
    actuals = {
        "declarations": quality["declarations"],
        "methods": quality["methods"],
        "local_imports": quality["local_imports"],
        "static_calls": quality["static_calls"],
        "false_resolved_calls": quality["false_resolved_calls"],
        "deterministic_rebuild": quality["deterministic_rebuild"],
        "startup_ms": performance["startup_ms"],
        "noop_ms": performance["noop_ms"],
        "build_1000_files_ms": performance["build_1000_files_ms"],
        "search_ms": max(
            case["warm_max_ms"] for case in performance["search_cases"]
        ),
        "context_ms": performance["context_ms"],
        "db_source_ratio": performance["db_source_ratio"],
        "peak_memory_10000_files_bytes": performance[
            "peak_memory_10000_files_bytes"
        ],
    }
    gates = {}
    for name, actual in actuals.items():
        operator, comparator = _OPERATORS[name]
        threshold = float(thresholds[name])
        gates[name] = {
            "actual": actual,
            "operator": operator,
            "threshold": threshold,
            "passed": comparator(float(actual), threshold),
        }
    return gates


def run_benchmark(
    *,
    output: str | Path,
    fixture_root: str | Path = "tests/fixtures/codegraph",
    thresholds: Mapping[str, float] | None = None,
    command: str | None = None,
) -> dict[str, object]:
    """Measure, persist evidence, then fail if any approved gate misses."""
    effective_thresholds = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        unknown = set(thresholds) - set(effective_thresholds)
        if unknown:
            raise ValueError(f"unknown benchmark threshold: {sorted(unknown)[0]}")
        effective_thresholds.update(thresholds)
    output_path = Path(output)
    with tempfile.TemporaryDirectory(prefix="iwiki-code-graph-") as temporary:
        temporary_path = Path(temporary)
        search_db = temporary_path / "search.sqlite3"
        search_corpus = _create_search_corpus(search_db)
        connection = search_corpus.pop("connection")
        try:
            search_cases = _mark_post_v1_targets(
                _measure_search(connection, search_corpus)
            )
            search_quality = _search_correctness(connection, search_corpus)
            search_semantic_hash = _hash_rows(connection)
        finally:
            connection.close()
        production = _write_production_tree(
            temporary_path / "production" / "project", BUILD_FILE_COUNT
        )
        memory_production = _write_production_tree(
            temporary_path / "memory" / "project", MEMORY_FILE_COUNT
        )
        memory_peak = _measure_memory(memory_production)
        build = _measure_build(temporary_path / "build", production)
        quality = dict(build["quality"])
        quality_details = dict(build["quality_counts"])
        quality.update(search_quality)
        quality_provenance = dict(build["quality_provenance"])
        quality_provenance.update({
            "search_measurement": "schema_v2_search_database",
            "search_corpus_sha256": search_corpus["sha256"],
            "search_metrics": list(search_quality),
        })
        deterministic = (
            build["semantic_row_hash_equal"]
            and build["entity_relation_link_ids_equal"]
            and build["revision_equal"]
            and build["noop_reported"]
        )
        quality["deterministic_rebuild"] = 1.0 if deterministic else 0.0
        performance = {
            "startup_ms": _measure_startup(),
            "noop_ms": build["noop_ms"],
            "build_1000_files_ms": build["build_ms"],
            "forced_build_1000_files_ms": build["forced_build_ms"],
            "search_cases": search_cases,
            "context_ms": build["context_ms"],
            "peak_memory_10000_files_bytes": memory_peak,
            "peak_memory_10000_files_mib": round(memory_peak / MIB, 6),
            "database_bytes": build["database_bytes"],
            "accepted_source_bytes": build["accepted_source_bytes"],
            "db_source_ratio": round(
                build["database_bytes"] / build["accepted_source_bytes"], 6
            ),
        }
        gates = _evaluate_gates(quality, performance, effective_thresholds)
        correctness_gates = {
            name: {
                "actual": value,
                "operator": "==",
                "threshold": 1.0,
                "passed": value == 1.0,
            }
            for name, value in search_quality.items()
        }
        search_truth = all(
            case["observed_rank"] == case["expected_rank"]
            and case["observed_result_ids"] == case["expected_result_ids"]
            and case["cold_result_ids"] == case["expected_result_ids"]
            and case["warmup_result_ids"] == case["expected_result_ids"]
            for case in search_cases
        )
        correctness_gates["search_golden_truth"] = {
            "actual": 1.0 if search_truth else 0.0,
            "operator": "==",
            "threshold": 1.0,
            "passed": search_truth,
        }
        gates.update(correctness_gates)
        passed = all(gate["passed"] for gate in gates.values())
        versions = {
            "iwiki_mcp": _distribution_version("iwiki-mcp"),
            "schema": SCHEMA_VERSION,
            "parser": "tree-sitter-python:" + _distribution_version(
                "tree-sitter-python"
            ),
            "grammar": "tree-sitter:" + _distribution_version("tree-sitter"),
            "language_pack": _distribution_version("tree-sitter-language-pack"),
            "adapter": "python-adapter-v2",
            "resolver": "resolver-v1",
            "normalizer": models.NORMALIZER_VERSION,
            "unicode_data": models.UNICODE_DATA_VERSION,
            "sqlite": sqlite3.sqlite_version,
        }
        report = {
            "command": command or (
                "uv run python -m eval.code_graph --fixture-root "
                f"{fixture_root} --output {output}"
            ),
            "environment": {
                "python": platform.python_version(),
                "python_implementation": platform.python_implementation(),
                "platform": platform.platform(),
                "machine": platform.machine(),
                "processor": platform.processor() or "unreported",
                "cpu_count": os.cpu_count() or 0,
            },
            "warm_cold_policy": {
                "startup": (
                    "fresh subprocess; timer around code-graph import; "
                    "five samples; maximum"
                ),
                "build": "cold temporary cache; one full production build",
                "noop": "warm cache; immediate production fingerprint no-op",
                "forced_rebuild": "warm files; forced production full rebuild",
                "search": (
                    "one cold sample, one untimed warmup, ten warm samples "
                    "on one connection and snapshot; warm maximum gated"
                ),
                "context": "warm SQLite connection; one warmup and three samples; maximum",
                "memory": "cold tracemalloc; production parser over 10000 generated files",
            },
            "search_latency_policy": {
                "release_gate": {
                    "operator": "<",
                    "threshold_ms": effective_thresholds["search_ms"],
                    "blocking": True,
                },
                "post_v1_target": {
                    "operator": "<",
                    "threshold_ms": POST_V1_SEARCH_TARGET_MS,
                    "blocking": False,
                },
            },
            "corpora": {
                "search": {
                    key: value for key, value in search_corpus.items()
                    if key not in {
                        "files",
                        "symbols",
                        "aliases",
                        "constraints",
                        "constraint_evidence",
                    }
                },
                "production": {
                    "sha256": production.sha256,
                    "file_count": len(build["accepted_paths"]),
                    "accepted_source_bytes": build["accepted_source_bytes"],
                    "memory_file_count": len(memory_production.accepted_paths),
                    "memory_sha256": memory_production.sha256,
                },
            },
            "golden_truth": {
                "normalization": "none",
                "unicode_token_key": "\x1fpkg\x1fstrasse\x1f",
                "canonical_lexical_query": "some token target",
                "unicode_composed_and_decomposed_distinct": True,
            },
            "versions": versions,
            "quality": quality,
            "quality_counts": quality_details,
            "quality_provenance": quality_provenance,
            "performance": performance,
            "strata": {
                "ascii_name": {"entities": search_corpus["entity_count"] - 506},
                "unicode_name": {"entities": 6},
                "unicode_signature": {"entities": 1},
                "shared_unicode_path": {"entities": 500 * 2},
                "duplicate_module": {"occurrences": 2, "entities": 2},
                "repeated_alias": {"sites": 2, "targets": 1},
                "ambiguous_alias": {"sites": 2, "targets": 2},
            },
            "constraints": search_corpus["constraints"],
            "constraint_evidence": search_corpus["constraint_evidence"],
            "determinism": {
                "first_revision": build["first_revision"],
                "second_revision": build["second_revision"],
                "revision_equal": build["revision_equal"],
                "first_semantic_row_hash": build["first_semantic_row_hash"],
                "second_semantic_row_hash": build["second_semantic_row_hash"],
                "semantic_row_hash_equal": build["semantic_row_hash_equal"],
                "entity_relation_link_ids_equal": build[
                    "entity_relation_link_ids_equal"
                ],
                "noop_reported": build["noop_reported"],
                "search_corpus_semantic_hash": search_semantic_hash,
                "excluded_fields": [
                    "repositories.indexed_at",
                    "repositories.state",
                    "metadata.phase_timings",
                    "metadata.transient_diagnostics",
                ],
            },
            "schema": {
                "authoritative_tables": list(TABLES),
                "explicit_index_count": len(INDEXES),
                "query_ranks": dict(MATCH_RANK),
            },
            "gates": gates,
            "passed": passed,
        }
        write_report(output_path, report)
    if not passed:
        failed = ", ".join(
            name for name, gate in gates.items() if not gate["passed"]
        )
        raise BenchmarkGateError(failed)
    return report


__all__ = [
    "BenchmarkGateError",
    "DEFAULT_THRESHOLDS",
    "run_benchmark",
]
