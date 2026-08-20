"""Run-level baseline: Python + TypeScript snapshot rows must not move.

Version strings are pinned rather than read from installed distributions,
so a dependency bump changes no baseline row (only `parser_version` would
drift; identifiers do not hash it).

A failure here means the change perturbed Python or TypeScript output.
Fix the code; regenerating the baseline is a stop-rule violation.
"""
import json
from pathlib import Path

from iwiki_mcp.codegraph import indexer as codegraph_indexer
from iwiki_mcp.codegraph.languages import python as codegraph_python
from iwiki_mcp.codegraph.languages import typescript as codegraph_typescript

from .test_mixed_language_indexing import FIXTURES, _DOMAIN, _build_indexer

BASELINE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "mixed_python_typescript_rows.json"
)
BASELINE_LANGUAGES = ("python", "typescript")


def pinned_factories(domain):
    return {
        "python": codegraph_indexer.AdapterFactory(
            create=lambda paths: codegraph_python.PythonAdapter(
                domain, paths, parser_version="pinned-python",
            ),
            extensions=(".py",),
            parser_version="pinned-python",
            grammar_version="pinned-python-grammar",
            adapter_version="python-adapter-v2",
        ),
        "typescript": codegraph_indexer.AdapterFactory(
            create=lambda paths: codegraph_typescript.TypeScriptAdapter(
                domain, paths, parser_version="pinned-typescript",
            ),
            extensions=(".ts", ".tsx"),
            parser_version="pinned-typescript",
            grammar_version="pinned-typescript-grammar",
            adapter_version="typescript-adapter-v1",
        ),
    }


def baseline_rows(tables):
    return {
        table: sorted(
            (dict(row) for row in tables[table]),
            key=lambda row: json.dumps(row, sort_keys=True),
        )
        for table in ("files", "symbols", "relations")
    }


def build_mixed_tables(cache_base, *, languages, factories):
    """Build the mixed fixture once. `cache_base` must be unique per call.

    `_build_indexer` does `(cache_base / _DOMAIN).mkdir(parents=True)` with
    no `exist_ok`, so reusing one directory for two builds raises
    FileExistsError before any assertion runs.
    """
    cache_base.mkdir(parents=True, exist_ok=True)
    indexer = _build_indexer(
        cache_base,
        FIXTURES / "mixed_python_typescript",
        languages=languages,
        adapter_factories=factories,
    )
    return indexer.build_rows().tables


def test_python_typescript_rows_match_baseline(tmp_path):
    tables = build_mixed_tables(
        tmp_path / "baseline",
        languages=BASELINE_LANGUAGES,
        factories=pinned_factories(_DOMAIN),
    )
    assert baseline_rows(tables) == json.loads(BASELINE_PATH.read_text())
