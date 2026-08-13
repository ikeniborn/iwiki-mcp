"""Stable benchmark evidence serialization."""
from __future__ import annotations

import json

from eval.code_graph.report import render_markdown, write_report


def _report():
    return {
        "command": "uv run python -m eval.code_graph --output evidence",
        "environment": {"python": "3.11", "platform": "test"},
        "warm_cold_policy": {"startup": "cold", "search": "warm"},
        "search_latency_policy": {
            "release_gate": {
                "operator": "<",
                "threshold_ms": 500.0,
                "blocking": True,
            },
            "post_v1_target": {
                "operator": "<",
                "threshold_ms": 150.0,
                "blocking": False,
            },
        },
        "corpora": {
            "search": {
                "sha256": "a" * 64,
                "entity_count": 100_001,
                "file_count": 40_000,
                "module_count": 30_000,
                "symbol_count": 30_001,
            },
            "production": {
                "sha256": "b" * 64,
                "accepted_source_bytes": 1_000_000,
                "file_count": 1_000,
            },
        },
        "golden_truth": {
            "normalization": "none",
            "unicode_token_key": "\x1fpkg\x1fstrasse\x1f",
        },
        "quality_provenance": {
            "production_measurement": "canonical_production_database",
            "production_corpus_sha256": "b" * 64,
            "production_revision": "sha256:revision",
            "production_metrics": ["declarations"],
            "search_measurement": "schema_v2_search_database",
            "search_corpus_sha256": "a" * 64,
            "search_metrics": ["unicode_correctness"],
        },
        "versions": {"schema": 2, "normalizer": "casefold-v1"},
        "quality": {"declarations": 1.0},
        "performance": {
            "startup_ms": 1.0,
            "search_cases": [{
                "name": "qualified",
                "expected_rank": "qualified_exact",
                "observed_rank": "qualified_exact",
                "cold_ms": 2.5,
                "warmup_runs": 1,
                "warm_samples_ms": [2.0] * 10,
                "warm_median_ms": 2.0,
                "warm_p95_ms": 2.0,
                "warm_max_ms": 2.0,
                "meets_post_v1_target": True,
            }],
        },
        "strata": {"ascii_name": {"entities": 1}},
        "constraints": {"fts": False},
        "determinism": {
            "semantic_row_hash_equal": True,
            "excluded_fields": [
                "repositories.indexed_at",
                "repositories.state",
                "metadata.phase_timings",
                "metadata.transient_diagnostics",
            ],
        },
        "constraint_evidence": {
            "python_sqlite_udf_names": [],
            "unapproved_explicit_indexes": [],
            "search_projection_tables": [],
            "fts_tables": [],
            "rank_limit_clause_counts": {"qualified_exact": 1},
        },
        "gates": {
            "declarations": {
                "actual": 1.0,
                "operator": ">=",
                "threshold": 0.98,
                "passed": True,
            }
        },
        "passed": True,
    }


def test_write_report_emits_stable_json_and_markdown(tmp_path):
    json_path, markdown_path = write_report(tmp_path, _report())

    assert json_path.name == "code-graph-benchmark.json"
    assert markdown_path.name == "code-graph-benchmark.md"
    assert json.loads(json_path.read_text(encoding="utf-8")) == _report()
    assert markdown_path.read_text(encoding="utf-8") == render_markdown(_report())


def test_markdown_contains_gate_corpus_and_search_evidence():
    markdown = render_markdown(_report())

    assert "# Code graph benchmark evidence" in markdown
    assert "100001" in markdown
    assert "qualified_exact" in markdown
    assert "Warm max ms" in markdown
    assert "Production corpus" in markdown
    assert "declarations" in markdown
    assert "PASS" in markdown
    assert "Release gate: <500 ms" in markdown
    assert "Post-v1 target: <150 ms (non-blocking)" in markdown
    assert "canonical_production_database" in markdown
    assert "repositories.indexed_at" in markdown
    assert "repositories.state" in markdown
    assert "metadata.phase_timings" in markdown
    assert "metadata.transient_diagnostics" in markdown
    assert "python_sqlite_udf_names: none" in markdown
    assert "rank_limit_clause_counts" in markdown
