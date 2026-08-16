"""Quality and performance gates for the isolated code graph benchmark."""
from __future__ import annotations

import json

import pytest

from eval.code_graph import runner
from eval.code_graph.runner import (
    BenchmarkGateError,
    DEFAULT_THRESHOLDS,
    _evaluate_gates,
    _create_search_corpus,
    _write_production_tree,
    run_benchmark,
)


REQUIRED_STRATA = {
    "ascii_name",
    "unicode_name",
    "unicode_signature",
    "shared_unicode_path",
    "duplicate_module",
    "repeated_alias",
    "ambiguous_alias",
}
REQUIRED_RANKS = {
    "qualified_exact",
    "local_exact",
    "alias_exact",
    "canonical_prefix",
    "alias_prefix",
    "canonical_lexical",
    "alias_lexical",
    "signature",
    "path",
}


def _gate_inputs(warm_max_ms: float) -> tuple[dict[str, float], dict[str, object]]:
    quality = {
        "declarations": 1.0,
        "methods": 1.0,
        "local_imports": 1.0,
        "static_calls": 1.0,
        "false_resolved_calls": 0.0,
        "deterministic_rebuild": 1.0,
    }
    performance = {
        "startup_ms": 0.0,
        "noop_ms": 0.0,
        "build_1000_files_ms": 0.0,
        "search_cases": [{"warm_max_ms": warm_max_ms}],
        "context_ms": 0.0,
        "db_source_ratio": 0.0,
        "peak_memory_10000_files_bytes": 0.0,
    }
    return quality, performance


def test_search_release_gate_is_strictly_below_500_ms():
    assert DEFAULT_THRESHOLDS["search_ms"] == 500.0

    quality, performance = _gate_inputs(499.999)
    assert _evaluate_gates(quality, performance, DEFAULT_THRESHOLDS)["search_ms"][
        "passed"
    ] is True

    quality, performance = _gate_inputs(500.0)
    assert _evaluate_gates(quality, performance, DEFAULT_THRESHOLDS)["search_ms"][
        "passed"
    ] is False


def test_startup_release_gate_is_strictly_below_500_ms():
    assert DEFAULT_THRESHOLDS["startup_ms"] == 500.0

    quality, performance = _gate_inputs(0.0)
    performance["startup_ms"] = 499.999
    assert _evaluate_gates(quality, performance, DEFAULT_THRESHOLDS)["startup_ms"][
        "passed"
    ] is True

    performance["startup_ms"] = 500.0
    assert _evaluate_gates(quality, performance, DEFAULT_THRESHOLDS)["startup_ms"][
        "passed"
    ] is False


def test_post_v1_target_is_per_case_and_non_blocking():
    cases = runner._mark_post_v1_targets([
        {"warm_max_ms": 149.999},
        {"warm_max_ms": 150.0},
    ])

    assert [case["meets_post_v1_target"] for case in cases] == [True, False]
    quality, performance = _gate_inputs(150.0)
    assert _evaluate_gates(quality, performance, DEFAULT_THRESHOLDS)["search_ms"][
        "passed"
    ] is True


@pytest.fixture(scope="module")
def benchmark_report(tmp_path_factory):
    output = tmp_path_factory.mktemp("code-graph-benchmark")
    return run_benchmark(
        output=output,
        fixture_root="tests/fixtures/codegraph",
    )


def test_report_has_quality_versions_environment_and_strata(benchmark_report):
    assert set(benchmark_report) >= {
        "command",
        "environment",
        "warm_cold_policy",
        "corpora",
        "versions",
        "quality",
        "performance",
        "strata",
        "constraints",
        "gates",
        "passed",
    }
    corpora = benchmark_report["corpora"]
    assert corpora["search"]["entity_count"] >= 100_000
    assert corpora["production"]["accepted_source_bytes"] > 0
    assert corpora["search"]["sha256"] != corpora["production"]["sha256"]
    assert REQUIRED_STRATA <= set(benchmark_report["strata"])
    assert corpora["search"]["sha256"]


def test_corpus_builders_keep_search_and_source_bytes_separate(tmp_path):
    search = _create_search_corpus(tmp_path / "search.sqlite3")
    try:
        production = _write_production_tree(tmp_path / "project", 3)
        assert search["entity_count"] >= 100_000
        assert "source_bytes" not in search
        assert production.accepted_source_bytes == sum(
            (production.project / path).stat().st_size
            for path in production.accepted_paths
        )
        assert production.sha256 != search["sha256"]
    finally:
        search["connection"].close()


def test_production_corpus_uses_actual_discovery_for_accepted_denominator(
    tmp_path,
):
    project = tmp_path / "project"
    project.mkdir()
    (project / "included.py").write_text("value = 1\n", encoding="utf-8")
    (project / "ignored.py").write_text("value = 2\n", encoding="utf-8")
    (project / ".iwikiignore").write_text("ignored.py\n", encoding="utf-8")

    corpus = runner._discover_production_corpus(project, max_total_files=10)

    assert corpus.accepted_paths == ("included.py",)
    assert corpus.accepted_source_bytes == (project / "included.py").stat().st_size
    assert corpus.sha256 == runner._hash_corpus([
        b"included.py\0" + (project / "included.py").read_bytes()
    ])


def test_quality_is_measured_from_published_production_corpus(tmp_path):
    corpus = _write_production_tree(tmp_path / "project", 10)

    measured = runner._measure_build(tmp_path / "build", corpus)

    assert measured["accepted_paths"] == corpus.accepted_paths
    assert measured["accepted_source_bytes"] == corpus.accepted_source_bytes
    assert measured["quality_provenance"] == {
        "production_measurement": "canonical_production_database",
        "production_corpus_sha256": corpus.sha256,
        "production_revision": measured["second_revision"],
        "production_metrics": [
            "declarations",
            "methods",
            "local_imports",
            "static_calls",
            "false_resolved_calls",
            "deterministic_rebuild",
        ],
    }
    assert measured["quality"]["declarations"] == 1.0
    assert measured["quality"]["methods"] == 1.0
    assert measured["quality"]["local_imports"] == 1.0
    assert measured["quality"]["static_calls"] == 1.0
    assert measured["quality"]["false_resolved_calls"] == 0.0


def test_benchmark_exercises_all_production_query_ranks(benchmark_report):
    search_cases = benchmark_report["performance"]["search_cases"]
    assert {case["expected_rank"] for case in search_cases} == REQUIRED_RANKS
    assert all(case["observed_rank"] == case["expected_rank"]
               for case in search_cases)
    assert all(case["warm_max_ms"] < 500 for case in search_cases)
    assert benchmark_report["constraints"] == {
        "fts": False,
        "python_sqlite_udf": False,
        "search_projection": False,
        "candidate_cap": False,
    }
    evidence = benchmark_report["constraint_evidence"]
    assert evidence["python_sqlite_udf_names"] == []
    assert evidence["unapproved_explicit_indexes"] == []
    assert evidence["search_projection_tables"] == []
    assert evidence["fts_tables"] == []
    assert evidence["rank_limit_clause_counts"] == {
        rank: 1 for rank in REQUIRED_RANKS
    }


def test_report_quality_provenance_matches_production_corpus(benchmark_report):
    provenance = benchmark_report["quality_provenance"]
    production = benchmark_report["corpora"]["production"]
    search = benchmark_report["corpora"]["search"]

    assert provenance["production_measurement"] == (
        "canonical_production_database"
    )
    assert provenance["production_corpus_sha256"] == production["sha256"]
    assert provenance["production_revision"] == benchmark_report["determinism"][
        "second_revision"
    ]
    assert provenance["production_metrics"] == [
        "declarations",
        "methods",
        "local_imports",
        "static_calls",
        "false_resolved_calls",
        "deterministic_rebuild",
    ]
    assert provenance["search_measurement"] == "schema_v2_search_database"
    assert provenance["search_corpus_sha256"] == search["sha256"]
    assert provenance["search_metrics"] == [
        "duplicate_module_correctness",
        "repeated_alias_correctness",
        "ambiguous_alias_correctness",
        "unicode_correctness",
    ]
    assert production["accepted_source_bytes"] == benchmark_report[
        "performance"
    ]["accepted_source_bytes"]


def test_search_records_cold_warmup_and_ten_warm_samples(benchmark_report):
    for case in benchmark_report["performance"]["search_cases"]:
        assert case["cold_ms"] >= 0
        assert case["warmup_runs"] == 1
        assert len(case["warm_samples_ms"]) == 10
        assert case["warm_max_ms"] == max(case["warm_samples_ms"])
        assert isinstance(case["meets_post_v1_target"], bool)


def test_golden_truth_is_independent_and_casefold_only(benchmark_report):
    truth = benchmark_report["golden_truth"]
    assert truth["unicode_token_key"] == "\x1fpkg\x1fstrasse\x1f"
    assert truth["normalization"] == "none"
    assert truth["canonical_lexical_query"] == "some token target"
    assert benchmark_report["quality"]["unicode_correctness"] == 1.0


def test_quality_and_determinism_gates_are_recorded(benchmark_report):
    quality = benchmark_report["quality"]
    assert quality["declarations"] >= 0.98
    assert quality["methods"] >= 0.98
    assert quality["local_imports"] >= 0.95
    assert quality["static_calls"] >= 0.75
    assert quality["false_resolved_calls"] < 0.05
    assert quality["duplicate_module_correctness"] == 1.0
    assert quality["repeated_alias_correctness"] == 1.0
    assert quality["ambiguous_alias_correctness"] == 1.0
    assert quality["unicode_correctness"] == 1.0
    assert quality["deterministic_rebuild"] == 1.0
    assert benchmark_report["determinism"]["semantic_row_hash_equal"] is True
    assert benchmark_report["determinism"][
        "entity_relation_link_ids_equal"
    ] is True
    assert benchmark_report["determinism"]["revision_equal"] is True
    assert benchmark_report["determinism"]["excluded_fields"] == [
        "repositories.indexed_at",
        "repositories.state",
        "metadata.phase_timings",
        "metadata.transient_diagnostics",
    ]
    assert benchmark_report["passed"] is True
    assert all(gate["passed"] for gate in benchmark_report["gates"].values())


def test_performance_gates_are_recorded(benchmark_report):
    performance = benchmark_report["performance"]
    assert performance["startup_ms"] < 500
    assert performance["noop_ms"] < 200
    assert performance["build_1000_files_ms"] < 15_000
    assert performance["context_ms"] < 300
    assert performance["db_source_ratio"] < 3
    assert performance["peak_memory_10000_files_bytes"] < 1024 ** 3


def test_threshold_miss_writes_evidence_and_exits_nonzero(tmp_path):
    with pytest.raises(BenchmarkGateError):
        run_benchmark(
            output=tmp_path,
            fixture_root="tests/fixtures/codegraph",
            thresholds={"declarations": 1.01},
        )
    evidence = tmp_path / "code-graph-benchmark.json"
    assert evidence.is_file()
    assert (tmp_path / "code-graph-benchmark.md").is_file()
    report = json.loads(evidence.read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert report["gates"]["declarations"]["passed"] is False
