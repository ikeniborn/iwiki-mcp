import os
import subprocess
import sys

import pytest

from eval.search_pipeline.fixtures import BenchmarkCase
from eval.search_pipeline.instrumentation import trace_query
from eval.search_pipeline.metrics import identity
from iwiki_mcp.engine.config import Config
from iwiki_mcp.indexer import index_domain


def _build_trace_fixture(tmp_path, monkeypatch, *, rerank_model=""):
    base = tmp_path / "base"
    domain = "eval"
    page_dir = base / domain / "guide"
    page_dir.mkdir(parents=True)
    (page_dir / "auth.md").write_text(
        "# Auth Guide\n\n"
        "## Rotation\n\n"
        "refresh_token credentials rotate safely without exposing secrets.\n\n"
        "## Overview\n\n"
        "general authentication overview.\n",
        encoding="utf-8",
    )
    (page_dir / "backup.md").write_text(
        "# Backup Guide\n\n"
        "## Rotation\n\n"
        "backup refresh_token credentials procedure for operators.\n",
        encoding="utf-8",
    )

    def fake_embed_texts(cfg, texts):
        vectors = []
        for text in texts:
            score = 1.0 if "refresh_token" in text else 0.1
            vectors.append([score, 1.0 - score])
        return vectors

    monkeypatch.setattr("iwiki_mcp.indexer.embed_texts", fake_embed_texts)
    monkeypatch.setattr("iwiki_mcp.retrieval.embed_texts", fake_embed_texts)

    cfg = Config(
        base_url="https://example.invalid/v1",
        api_key="test-key",
        embed_model="test-embed",
        dimensions=2,
        chunk_size=256,
        chunk_overlap=32,
        summary_max=120,
        top_k=3,
        score_threshold=0.0,
        graph_depth=1,
        ignore=None,
        seed_top_k=2,
        bfs_top_k=2,
        seed_threshold=0.0,
        rerank_model=rerank_model,
    )
    index_domain(cfg, str(base), domain)
    case = BenchmarkCase(
        case_id="offline-symbol",
        domain=domain,
        query="refresh_token credentials",
        relevant={"eval/guide/auth.md#Rotation:0": 3},
        intents={"symbol": ["eval/guide/auth.md#Rotation:0"]},
        k=3,
    )
    return cfg, str(base), case


def test_trace_query_records_stage_counts_and_final_results(tmp_path, monkeypatch):
    cfg, base, case = _build_trace_fixture(tmp_path, monkeypatch)

    trace = trace_query(
        cfg,
        base,
        case,
        mode="hybrid",
        rerank_enabled=False,
    )

    assert set(trace) == {
        "case_id",
        "domain",
        "query",
        "mode",
        "k",
        "latency",
        "stages",
        "results",
        "metrics_input",
    }
    assert set(trace["stages"]) == {
        "index",
        "signals",
        "fusion",
        "hydration",
        "rerank",
    }
    final_identities = [identity(result) for result in trace["results"]]
    assert final_identities[0] == "eval/guide/auth.md#Rotation:0"
    assert trace["k"] == case.k
    relevant_identity = "eval/guide/auth.md#Rotation:0"
    assert trace["stages"]["index"]["section_count"] >= 2
    assert trace["stages"]["index"]["relevant"][relevant_identity] == {
        "parseable": True,
        "file_exists": True,
        "chunk_present": True,
        "indexed": True,
        "hash_matches": True,
        "embedding_dim_matches": True,
        "eligible": True,
    }
    assert trace["stages"]["signals"]["counts"]["semantic_chunk"] >= 1
    assert trace["stages"]["signals"]["counts"]["lexical_section"] >= 1
    signal_identities = trace["stages"]["signals"]["identities"]
    assert relevant_identity in signal_identities["semantic_chunk"]
    assert relevant_identity in signal_identities["lexical_section"]
    ranked_signals = trace["stages"]["signals"]["ranked"]
    assert ranked_signals["semantic_chunk"]
    assert set(ranked_signals["semantic_chunk"][0]) == {
        "domain", "file", "heading", "chunk", "ordinal",
    }
    assert "rank_key" not in repr(ranked_signals)
    assert set(
        trace["stages"]["signals"]["relevant_presence"][relevant_identity]
    ) >= {
        "lexical_section",
        "semantic_chunk",
    }
    assert trace["stages"]["fusion"]["candidate_count"] >= 1
    assert (
        trace["stages"]["fusion"]["candidate_identities"][0]
        == "eval/guide/auth.md#Rotation:0"
    )
    assert (
        trace["stages"]["hydration"]["requested"]
        == trace["stages"]["fusion"]["candidate_count"]
    )
    assert trace["stages"]["hydration"]["hydrated"] >= 1
    assert "requested_identities" not in trace["stages"]["hydration"]
    assert trace["stages"]["rerank"] == {
        "applied": False,
        "warning": "rerank disabled",
    }
    assert trace["metrics_input"] == {
        "ranking": final_identities,
        "relevant": case.relevant,
        "intents": case.intents,
    }
    assert trace["latency"]["total_ms"] >= 0
    for stage in (
        "embedding_ms",
        "signals_ms",
        "fusion_ms",
        "hydration_ms",
        "rerank_ms",
    ):
        assert stage in trace["latency"]


def test_trace_query_keeps_fused_results_when_hydration_drops_candidates(
    tmp_path,
    monkeypatch,
):
    cfg, base, case = _build_trace_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "iwiki_mcp.retrieval.hydrate_candidates",
        lambda *args, **kwargs: [],
    )

    trace = trace_query(
        cfg,
        base,
        case,
        mode="hybrid",
        rerank_enabled=False,
    )

    fused_identities = trace["stages"]["fusion"]["candidate_identities"]
    result_identities = [identity(result) for result in trace["results"]]
    assert (
        trace["stages"]["hydration"]["dropped"]
        == trace["stages"]["fusion"]["candidate_count"]
    )
    assert trace["stages"]["hydration"]["hydrated"] == 0
    assert result_identities == fused_identities[:case.k]


def test_trace_query_keeps_fused_results_when_rerank_falls_back(
    tmp_path,
    monkeypatch,
):
    cfg, base, case = _build_trace_fixture(
        tmp_path,
        monkeypatch,
        rerank_model="test-rerank",
    )
    monkeypatch.setattr(
        "eval.search_pipeline.instrumentation.rerank.rerank_candidates",
        lambda *args, **kwargs: ([], {"applied": False, "warning": "fallback"}),
    )

    trace = trace_query(
        cfg,
        base,
        case,
        mode="hybrid",
        rerank_enabled=True,
    )

    fused_identities = trace["stages"]["fusion"]["candidate_identities"]
    result_identities = [identity(result) for result in trace["results"]]
    assert trace["stages"]["rerank"] == {"applied": False, "warning": "fallback"}
    assert result_identities == fused_identities[:case.k]


def test_trace_query_applies_eval_overrides_without_shrinking_fallback(
    tmp_path,
    monkeypatch,
):
    cfg, base, case = _build_trace_fixture(
        tmp_path,
        monkeypatch,
        rerank_model="test-rerank",
    )
    captured = {}
    original_fuse_ranked = __import__(
        "eval.search_pipeline.instrumentation",
        fromlist=["fusion"],
    ).fusion.fuse_ranked

    def capture_fusion(signals, limit, weights=None):
        captured["weights"] = weights
        return original_fuse_ranked(signals, limit, weights)

    def capture_hydration(cfg, base, candidates, page_cache):
        captured["hydrated_input"] = list(candidates)
        return list(candidates)

    monkeypatch.setattr(
        "eval.search_pipeline.instrumentation.fusion.fuse_ranked",
        capture_fusion,
    )
    monkeypatch.setattr(
        "iwiki_mcp.retrieval.hydrate_candidates",
        capture_hydration,
    )
    monkeypatch.setattr(
        "eval.search_pipeline.instrumentation.rerank.rerank_candidates",
        lambda *args, **kwargs: ([], {"applied": False, "warning": "fallback"}),
    )

    weights = {"semantic_chunk": 1.0, "graph_page": 0.01}
    trace = trace_query(
        cfg,
        base,
        case,
        mode="hybrid",
        rerank_enabled=True,
        fusion_weights=weights,
        rerank_candidate_limit=1,
    )

    fused = trace["stages"]["fusion"]["candidate_identities"]
    assert captured["weights"] == weights
    assert trace["stages"]["hydration"]["requested"] == len(captured["hydrated_input"])
    assert len(captured["hydrated_input"]) == min(len(fused), case.k)
    assert [identity(result) for result in trace["results"]] == fused[:case.k]


def test_trace_query_refuses_legacy_store_before_retrieval_helpers(
    tmp_path,
    monkeypatch,
):
    cfg, base, case = _build_trace_fixture(tmp_path, monkeypatch)
    (tmp_path / "base" / case.domain / ".iwiki").mkdir()

    def fail_domain_signals(*args, **kwargs):
        raise AssertionError("_domain_signals must not be called")

    monkeypatch.setattr("iwiki_mcp.retrieval._domain_signals", fail_domain_signals)

    with pytest.raises(RuntimeError, match="legacy store layout") as exc_info:
        trace_query(
            cfg,
            base,
            case,
            mode="hybrid",
            rerank_enabled=False,
        )
    assert str(tmp_path) not in str(exc_info.value)


def test_trace_query_rejects_invalid_domain_before_retrieval_helpers(
    tmp_path,
    monkeypatch,
):
    cfg, base, case = _build_trace_fixture(tmp_path, monkeypatch)
    invalid_case = BenchmarkCase(
        case_id=case.case_id,
        domain="../outside",
        query=case.query,
        relevant=case.relevant,
        intents=case.intents,
        k=case.k,
    )

    def fail_domain_signals(*args, **kwargs):
        raise AssertionError("_domain_signals must not be called")

    monkeypatch.setattr("iwiki_mcp.retrieval._domain_signals", fail_domain_signals)

    with pytest.raises(ValueError, match="invalid domain"):
        trace_query(
            cfg,
            base,
            invalid_case,
            mode="hybrid",
            rerank_enabled=False,
        )


def test_trace_query_merges_applied_rerank_with_unscored_fused_candidates(
    tmp_path,
    monkeypatch,
):
    cfg, base, case = _build_trace_fixture(
        tmp_path,
        monkeypatch,
        rerank_model="test-rerank",
    )
    captured = {}

    def fake_rerank_candidates(cfg, query, candidates, top_n=None):
        assert len(candidates) >= 2
        reranked = dict(candidates[1])
        reranked["score"] = 42.0
        captured["scored_identity"] = identity(reranked)
        return [reranked], {"applied": True, "_scored_count": 1}

    monkeypatch.setattr(
        "eval.search_pipeline.instrumentation.rerank.rerank_candidates",
        fake_rerank_candidates,
    )

    trace = trace_query(
        cfg,
        base,
        case,
        mode="hybrid",
        rerank_enabled=True,
    )

    rerank_metadata = trace["stages"]["rerank"]
    fused_identities = trace["stages"]["fusion"]["candidate_identities"]
    expected = [captured["scored_identity"]]
    expected.extend(
        item for item in fused_identities
        if item != captured["scored_identity"]
    )
    result_identities = [identity(result) for result in trace["results"]]
    assert rerank_metadata["applied"] is True
    assert "_scored_count" not in rerank_metadata
    assert rerank_metadata["scored_count"] == 1
    assert result_identities == expected[:case.k]


def test_summarize_trace_computes_quality_and_latency_metrics():
    from eval.search_pipeline.runner import summarize_trace

    case = BenchmarkCase(
        case_id="offline-symbol",
        domain="eval",
        query="refresh_token credentials",
        relevant={
            "eval/guide/auth.md#Rotation:0": 3,
            "eval/guide/backup.md#Rotation:0": 2,
        },
        intents={
            "auth": ["eval/guide/auth.md#Rotation:0"],
            "backup": ["eval/guide/backup.md#Rotation:0"],
        },
        k=2,
    )
    trace = {
        "case_id": case.case_id,
        "mode": "hybrid",
        "k": 2,
        "latency": {"total_ms": 12.3456},
        "metrics_input": {
            "ranking": [
                "eval/guide/noise.md#Other:0",
                "eval/guide/auth.md#Rotation:0",
            ],
        },
    }

    summary = summarize_trace(case, trace)

    assert summary == {
        "case_id": "offline-symbol",
        "mode": "hybrid",
        "k": 2,
        "recall_at_k": 0.5,
        "mrr_at_k": 0.5,
        "ndcg_at_k": pytest.approx(0.496639, rel=1e-6),
        "intent_coverage_at_k": 0.5,
        "latency_ms": 12.346,
    }


def test_rollup_means_quality_metrics_and_empty_defaults_to_zero():
    from eval.search_pipeline.runner import _rollup

    assert _rollup([]) == {
        "case_count": 0,
        "recall_at_k": 0.0,
        "mrr_at_k": 0.0,
        "ndcg_at_k": 0.0,
        "intent_coverage_at_k": 0.0,
        "latency_ms": 0.0,
    }

    summaries = [
        {
            "recall_at_k": 1.0,
            "mrr_at_k": 0.5,
            "ndcg_at_k": 0.25,
            "intent_coverage_at_k": 1.0,
            "latency_ms": 10.0,
        },
        {
            "recall_at_k": 0.0,
            "mrr_at_k": 1.0,
            "ndcg_at_k": 0.75,
            "intent_coverage_at_k": 0.0,
            "latency_ms": 20.0,
        },
    ]

    assert _rollup(summaries) == {
        "case_count": 2,
        "recall_at_k": 0.5,
        "mrr_at_k": 0.75,
        "ndcg_at_k": 0.5,
        "intent_coverage_at_k": 0.5,
        "latency_ms": 15.0,
    }


def test_run_offline_traces_aggregates_evidence_without_rerank(monkeypatch):
    from eval.search_pipeline import runner

    calls = []
    cases = [
        BenchmarkCase(
            case_id="case-a",
            domain="eval",
            query="alpha",
            relevant={"eval/a.md#A:0": 3},
            intents={"a": ["eval/a.md#A:0"]},
            k=2,
        ),
        BenchmarkCase(
            case_id="case-b",
            domain="eval",
            query="beta",
            relevant={"eval/b.md#B:0": 3},
            intents={"b": ["eval/b.md#B:0"]},
            k=2,
        ),
    ]

    def fake_trace_query(cfg, base, case, mode, rerank_enabled):
        calls.append((cfg, base, case.case_id, mode, rerank_enabled))
        ranking = [next(iter(case.relevant))]
        return {
            "case_id": case.case_id,
            "domain": case.domain,
            "query": case.query,
            "mode": mode,
            "k": case.k,
            "latency": {"total_ms": 1.0 if case.case_id == "case-a" else 3.0},
            "stages": {"fusion": {"candidate_identities": ranking}},
            "results": [],
            "metrics_input": {
                "ranking": ranking,
                "relevant": case.relevant,
                "intents": case.intents,
            },
        }

    monkeypatch.setattr(runner, "trace_query", fake_trace_query)

    evidence = runner.run_offline_traces(
        cfg={"top_k": 2},
        base="/tmp/wiki",
        cases=cases,
        modes=["lexical", "hybrid"],
    )

    assert evidence["kind"] == "offline"
    assert isinstance(evidence["timestamp"], str)
    assert evidence["config"] == {"top_k": 2}
    assert len(calls) == 4
    assert {call[4] for call in calls} == {False}
    assert [summary["case_id"] for summary in evidence["summary"]["cases"]] == [
        "case-a",
        "case-a",
        "case-b",
        "case-b",
    ]
    assert evidence["summary"]["rollup"] == {
        "case_count": 4,
        "recall_at_k": 1.0,
        "mrr_at_k": 1.0,
        "ndcg_at_k": 1.0,
        "intent_coverage_at_k": 1.0,
        "latency_ms": 2.0,
    }
    assert len(evidence["traces"]) == 4
    assert evidence["backlog"] == []


def test_run_offline_traces_sanitizes_dict_config_deterministically(monkeypatch):
    from eval.search_pipeline import runner

    case = BenchmarkCase(
        case_id="case-a",
        domain="eval",
        query="alpha",
        relevant={"eval/a.md#A:0": 3},
        intents={"a": ["eval/a.md#A:0"]},
        k=2,
    )

    def fake_trace_query(cfg, base, case, mode, rerank_enabled):
        return {
            "case_id": case.case_id,
            "domain": case.domain,
            "query": case.query,
            "mode": mode,
            "k": case.k,
            "latency": {"total_ms": 1.0},
            "stages": {},
            "results": [],
            "metrics_input": {
                "ranking": ["eval/a.md#A:0"],
                "relevant": case.relevant,
                "intents": case.intents,
            },
        }

    monkeypatch.setattr(runner, "trace_query", fake_trace_query)

    cfg = {
        "api_key": "raw-token",
        "base_url": "https://provider.example/v1",
        "IWIKI_LLM_KEY": "env-token",
        "endpoint": "http://localhost:11434/v1",
        "embed_model": "embed-small",
        "dimensions": 384,
        "top_k": 8,
        "rerank_enabled": False,
        "nested": {"key": "nested-token"},
    }

    evidence = runner.run_offline_traces(cfg, "/tmp/wiki", [case], ["hybrid"])

    assert evidence["config"] == {
        "dimensions": 384,
        "embed_model": "embed-small",
        "rerank_enabled": False,
        "top_k": 8,
    }
    assert "raw-token" not in repr(evidence)
    assert "env-token" not in repr(evidence)
    assert "provider.example" not in repr(evidence)
    assert "localhost:11434" not in repr(evidence)
    assert "nested-token" not in repr(evidence)


def test_run_offline_traces_uses_safe_type_for_unknown_config(monkeypatch):
    from eval.search_pipeline import runner

    class SecretConfig:
        def __repr__(self):
            return "SecretConfig(api_key='raw-token', base_url='https://provider.example/v1')"

    case = BenchmarkCase(
        case_id="case-a",
        domain="eval",
        query="alpha",
        relevant={"eval/a.md#A:0": 3},
        intents={"a": ["eval/a.md#A:0"]},
        k=2,
    )

    def fake_trace_query(cfg, base, case, mode, rerank_enabled):
        return {
            "case_id": case.case_id,
            "domain": case.domain,
            "query": case.query,
            "mode": mode,
            "k": case.k,
            "latency": {"total_ms": 1.0},
            "stages": {},
            "results": [],
            "metrics_input": {
                "ranking": ["eval/a.md#A:0"],
                "relevant": case.relevant,
                "intents": case.intents,
            },
        }

    monkeypatch.setattr(runner, "trace_query", fake_trace_query)

    evidence = runner.run_offline_traces(
        SecretConfig(),
        "/tmp/wiki",
        [case],
        ["hybrid"],
    )

    assert evidence["config"] == {"type": "SecretConfig"}
    assert "raw-token" not in repr(evidence)
    assert "provider.example" not in repr(evidence)


def test_run_offline_traces_adds_mode_to_findings_without_mutating_analyzer(
    monkeypatch,
):
    from eval.search_pipeline import runner

    case = BenchmarkCase(
        case_id="case-a",
        domain="eval",
        query="alpha",
        relevant={"eval/a.md#A:0": 3},
        intents={"a": ["eval/a.md#A:0"]},
        k=2,
    )
    analyzer_finding = {
        "case_id": "case-a",
        "class": "missing_from_candidate_pool",
        "severity": "high",
        "identity": "eval/a.md#A:0",
        "evidence": {},
    }

    def fake_trace_query(cfg, base, case, mode, rerank_enabled):
        return {
            "case_id": case.case_id,
            "domain": case.domain,
            "query": case.query,
            "mode": mode,
            "k": case.k,
            "latency": {"total_ms": 1.0},
            "stages": {},
            "results": [],
            "metrics_input": {
                "ranking": [],
                "relevant": case.relevant,
                "intents": case.intents,
            },
        }

    monkeypatch.setattr(runner, "trace_query", fake_trace_query)
    monkeypatch.setattr(runner, "analyze_trace", lambda case, trace: [analyzer_finding])

    evidence = runner.run_offline_traces({}, "/tmp/wiki", [case], ["lexical", "hybrid"])

    assert [finding["mode"] for finding in evidence["findings"]] == [
        "lexical",
        "hybrid",
    ]
    assert "mode" not in analyzer_finding


def test_run_offline_traces_records_rerank_disabled_run_setting(monkeypatch):
    from eval.search_pipeline import runner

    case = BenchmarkCase(
        case_id="case-a",
        domain="eval",
        query="alpha",
        relevant={"eval/a.md#A:0": 3},
        intents={"a": ["eval/a.md#A:0"]},
        k=2,
    )

    def fake_trace_query(cfg, base, case, mode, rerank_enabled):
        assert rerank_enabled is False
        return {
            "case_id": case.case_id,
            "domain": case.domain,
            "query": case.query,
            "mode": mode,
            "k": case.k,
            "latency": {"total_ms": 1.0},
            "stages": {},
            "results": [],
            "metrics_input": {
                "ranking": ["eval/a.md#A:0"],
                "relevant": case.relevant,
                "intents": case.intents,
            },
        }

    monkeypatch.setattr(runner, "trace_query", fake_trace_query)

    evidence = runner.run_offline_traces(
        {
            "rerank_enabled": True,
            "rerank_model": "rerank-model",
            "top_k": 2,
        },
        "/tmp/wiki",
        [case],
        ["hybrid"],
    )

    assert evidence["run_settings"] == {"rerank_enabled": False}
    assert evidence["config"]["rerank_enabled"] is True
    assert evidence["config"]["rerank_model"] == "rerank-model"


def test_cli_help_exits_zero_and_lists_required_flags():
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    result = subprocess.run(
        [sys.executable, "-m", "eval.search_pipeline", "--help"],
        cwd=os.getcwd(),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 0
    assert "--domain" in result.stdout
    assert "--env-file" in result.stdout
    assert "--out" in result.stdout
    assert "--pareto" in result.stdout


def test_cli_rejects_pareto_mode_subset_before_loading_config(tmp_path, monkeypatch):
    from eval.search_pipeline import __main__ as cli

    monkeypatch.setattr(
        cli.Config,
        "load",
        lambda: pytest.fail("Config.load must not be called"),
    )

    code = cli.main([
        "--out",
        str(tmp_path),
        "--pareto",
        "--modes",
        "hybrid,lexical",
    ])

    assert code == 2


def test_cli_without_live_config_exits_two_without_secret_values(
    tmp_path,
    monkeypatch,
    capsys,
):
    from eval.search_pipeline import __main__ as cli

    monkeypatch.delenv("IWIKI_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("IWIKI_LLM_KEY", raising=False)
    monkeypatch.setattr(
        cli,
        "write_reports",
        lambda *args, **kwargs: pytest.fail("write_reports must not be called"),
    )

    code = cli.main(["--out", str(tmp_path)])

    captured = capsys.readouterr()
    assert code == 2
    assert "IWIKI_LLM_BASE_URL" in captured.err
    assert "secret" not in captured.err.lower()


def test_cli_sanitizes_env_value_parse_errors(tmp_path, monkeypatch, capsys):
    from eval.search_pipeline import __main__ as cli

    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "test-key")
    monkeypatch.setenv("IWIKI_TOP_K", "LEAK_SENTINEL_123")
    monkeypatch.setattr(
        cli,
        "write_reports",
        lambda *args, **kwargs: pytest.fail("write_reports must not be called"),
    )

    code = cli.main(["--out", str(tmp_path)])

    captured = capsys.readouterr()
    assert code == 2
    assert "invalid numeric configuration" in captured.err
    assert "IWIKI_" in captured.err
    assert "LEAK_SENTINEL_123" not in captured.err


def test_cli_sanitizes_unexpected_runtime_failures(tmp_path, monkeypatch, capsys):
    from eval.search_pipeline import __main__ as cli

    cfg = Config(
        base_url="https://provider.example/v1",
        api_key="secret-token",
        embed_model="test-embed",
        dimensions=2,
        chunk_size=256,
        chunk_overlap=32,
        summary_max=120,
        top_k=3,
        score_threshold=0.0,
        graph_depth=1,
        ignore=None,
        seed_top_k=2,
        bfs_top_k=2,
        seed_threshold=0.0,
        rerank_model="",
    )
    monkeypatch.setattr(cli.Config, "load", lambda: cfg)

    def fail_run_live_traces(*args, **kwargs):
        raise RuntimeError(f"LEAK_SENTINEL_123 at {tmp_path}/private/base")

    monkeypatch.setattr(cli, "run_live_traces", fail_run_live_traces)
    monkeypatch.setattr(
        cli,
        "write_reports",
        lambda *args, **kwargs: pytest.fail("write_reports must not be called"),
    )

    code = cli.main(["--out", str(tmp_path / "reports")])

    captured = capsys.readouterr()
    assert code == 2
    assert "benchmark failed unexpectedly" in captured.err
    assert "Traceback" not in captured.err
    assert "LEAK_SENTINEL_123" not in captured.err
    assert str(tmp_path) not in captured.err


def test_cli_invalid_mode_exits_two_before_loading_config(tmp_path, monkeypatch):
    from eval.search_pipeline import __main__ as cli

    monkeypatch.setattr(
        cli.Config,
        "load",
        lambda: pytest.fail("Config.load must not be called"),
    )

    code = cli.main(["--out", str(tmp_path), "--modes", "hybrid,invalid"])

    assert code == 2


def test_cli_rejects_env_file_under_output_dir_before_applying(
    tmp_path,
    monkeypatch,
    capsys,
):
    from eval.search_pipeline import __main__ as cli

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    env_file = out_dir / ".env"
    env_file.write_text("IWIKI_LLM_KEY=secret\n", encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "apply_env_file",
        lambda *args, **kwargs: pytest.fail("apply_env_file must not be called"),
    )
    monkeypatch.setattr(
        cli.Config,
        "load",
        lambda: pytest.fail("Config.load must not be called"),
    )

    code = cli.main(["--out", str(out_dir), "--env-file", str(env_file)])

    captured = capsys.readouterr()
    assert code == 2
    assert "env file is inside output directory" in captured.err
    assert "secret" not in captured.err.lower()


def test_cli_success_writes_reports_and_applies_live_overrides(
    tmp_path,
    monkeypatch,
    capsys,
):
    from eval.search_pipeline import __main__ as cli

    cfg = Config(
        base_url="https://provider.example/v1",
        api_key="secret-token",
        embed_model="test-embed",
        dimensions=2,
        chunk_size=256,
        chunk_overlap=32,
        summary_max=120,
        top_k=3,
        score_threshold=0.0,
        graph_depth=1,
        ignore=None,
        seed_top_k=2,
        bfs_top_k=2,
        seed_threshold=0.0,
        rerank_model="",
    )
    captured_run = {}

    def fake_run_live_traces(loaded_cfg, domain, modes, cases, latency_ceiling_ms=None):
        captured_run["cfg"] = loaded_cfg
        captured_run["domain"] = domain
        captured_run["modes"] = modes
        captured_run["case_ks"] = [case.k for case in cases]
        captured_run["latency_ceiling_ms"] = latency_ceiling_ms
        return {
            "kind": "live",
            "summary": {"rollup": {"latency_ms": 42.0}, "cases": []},
            "run_settings": {},
            "traces": [],
            "findings": [],
            "backlog": [],
        }

    def fake_write_reports(evidence, out_dir):
        captured_run["evidence"] = evidence
        captured_run["out_dir"] = out_dir
        return {
            "json": tmp_path / "report.json",
            "markdown": tmp_path / "report.md",
            "html": tmp_path / "report.html",
        }

    monkeypatch.setattr(cli.Config, "load", lambda: cfg)
    monkeypatch.setattr(cli, "run_live_traces", fake_run_live_traces)
    monkeypatch.setattr(cli, "write_reports", fake_write_reports)

    code = cli.main([
        "--domain",
        "iwiki-mcp",
        "--out",
        str(tmp_path / "reports"),
        "--modes",
        "semantic,lexical",
        "--k",
        "5",
        "--latency-ceiling-ms",
        "10",
    ])

    output = capsys.readouterr()
    assert code == 0
    assert captured_run["cfg"] is cfg
    assert captured_run["domain"] == "iwiki-mcp"
    assert captured_run["modes"] == ["semantic", "lexical"]
    assert set(captured_run["case_ks"]) == {5}
    assert captured_run["latency_ceiling_ms"] == 10.0
    assert captured_run["evidence"]["run_settings"]["latency_ceiling_ms"] == 10.0
    assert captured_run["evidence"]["run_settings"]["latency_ceiling_exceeded"] is True
    assert str(tmp_path / "report.json") in output.out


def test_run_live_traces_filters_domain_and_records_live_evidence(monkeypatch):
    from eval.search_pipeline import runner

    cfg = Config(
        base_url="https://provider.example/v1",
        api_key="secret-token",
        embed_model="test-embed",
        dimensions=2,
        chunk_size=256,
        chunk_overlap=32,
        summary_max=120,
        top_k=3,
        score_threshold=0.0,
        graph_depth=1,
        ignore=None,
        seed_top_k=2,
        bfs_top_k=2,
        seed_threshold=0.0,
        rerank_model="test-rerank",
    )
    selected_case = BenchmarkCase(
        case_id="case-a",
        domain="iwiki-mcp",
        query="alpha",
        relevant={"iwiki-mcp/a.md#A:0": 3},
        intents={"a": ["iwiki-mcp/a.md#A:0"]},
        k=2,
    )
    skipped_case = BenchmarkCase(
        case_id="case-b",
        domain="other",
        query="beta",
        relevant={"other/b.md#B:0": 3},
        intents={"b": ["other/b.md#B:0"]},
        k=2,
    )
    calls = []
    analyzer_finding = {
        "case_id": "case-a",
        "class": "missing_from_candidate_pool",
        "severity": "high",
        "identity": "iwiki-mcp/a.md#A:0",
        "evidence": {},
    }

    class Binding:
        base = "/tmp/live-wiki"

    def fake_trace_query(cfg, base, case, mode, rerank_enabled):
        calls.append((cfg, base, case.case_id, mode, rerank_enabled))
        return {
            "case_id": case.case_id,
            "domain": case.domain,
            "query": case.query,
            "mode": mode,
            "k": case.k,
            "latency": {"total_ms": 2.0},
            "stages": {},
            "results": [],
            "metrics_input": {
                "ranking": ["iwiki-mcp/a.md#A:0"],
                "relevant": case.relevant,
                "intents": case.intents,
            },
        }

    monkeypatch.setattr(runner, "resolve_binding", lambda: Binding())
    monkeypatch.setattr(runner, "trace_query", fake_trace_query)
    monkeypatch.setattr(runner, "analyze_trace", lambda case, trace: [analyzer_finding])

    evidence = runner.run_live_traces(
        cfg,
        "iwiki-mcp",
        ["semantic", "lexical"],
        [skipped_case, selected_case],
    )

    assert calls == [
        (cfg, "/tmp/live-wiki", "case-a", "semantic", True),
        (cfg, "/tmp/live-wiki", "case-a", "lexical", True),
    ]
    assert evidence["kind"] == "live"
    assert evidence["domain"] == "iwiki-mcp"
    assert evidence["run_settings"] == {
        "domain": "iwiki-mcp",
        "rerank_enabled": True,
    }
    assert evidence["config"]["rerank_model"] == "test-rerank"
    assert evidence["config"]["rerank_enabled"] is True
    assert "secret-token" not in repr(evidence)
    assert "provider.example" not in repr(evidence)
    assert evidence["modes"] == ["semantic", "lexical"]
    assert [case["case_id"] for case in evidence["cases"]] == ["case-a"]
    assert evidence["summary"]["rollup"] == {
        "case_count": 2,
        "recall_at_k": 1.0,
        "mrr_at_k": 1.0,
        "ndcg_at_k": 1.0,
        "intent_coverage_at_k": 1.0,
        "latency_ms": 2.0,
    }
    assert [finding["mode"] for finding in evidence["findings"]] == [
        "semantic",
        "lexical",
    ]
    assert "mode" not in analyzer_finding
    assert evidence["backlog"] == [
        {
            "class": "missing_from_candidate_pool",
            "count": 2,
            "severity": "high",
        }
    ]


def test_run_live_traces_returns_empty_live_evidence_for_unmatched_domain(
    monkeypatch,
):
    from eval.search_pipeline import runner

    case = BenchmarkCase(
        case_id="case-a",
        domain="other",
        query="alpha",
        relevant={"other/a.md#A:0": 3},
    )

    monkeypatch.setattr(
        runner,
        "trace_query",
        lambda *args, **kwargs: pytest.fail("trace_query must not be called"),
    )

    evidence = runner.run_live_traces(
        {"rerank_model": "secret-rerank", "top_k": 3},
        "iwiki-mcp",
        ["hybrid"],
        [case],
        base="/tmp/live-wiki",
    )

    assert evidence["kind"] == "live"
    assert evidence["domain"] == "iwiki-mcp"
    assert evidence["cases"] == []
    assert evidence["summary"] == {
        "rollup": {
            "case_count": 0,
            "recall_at_k": 0.0,
            "mrr_at_k": 0.0,
            "ndcg_at_k": 0.0,
            "intent_coverage_at_k": 0.0,
            "latency_ms": 0.0,
        },
        "cases": [],
    }
    assert evidence["traces"] == []
    assert evidence["findings"] == []
    assert evidence["backlog"] == []


def test_run_live_traces_captures_failed_query_and_continues(monkeypatch):
    from eval.search_pipeline import runner

    failing_case = BenchmarkCase(
        case_id="case-a",
        domain="iwiki-mcp",
        query="alpha",
        relevant={"iwiki-mcp/a.md#A:0": 3},
        intents={"a": ["iwiki-mcp/a.md#A:0"]},
        k=2,
    )
    passing_case = BenchmarkCase(
        case_id="case-b",
        domain="iwiki-mcp",
        query="beta",
        relevant={"iwiki-mcp/b.md#B:0": 3},
        intents={"b": ["iwiki-mcp/b.md#B:0"]},
        k=2,
    )
    analyze_calls = []

    def fake_trace_query(cfg, base, case, mode, rerank_enabled):
        if case.case_id == "case-a":
            raise RuntimeError(
                "LEAK_SENTINEL_123 provider failure at /tmp/private/wiki-base"
            )
        return {
            "case_id": case.case_id,
            "domain": case.domain,
            "query": case.query,
            "mode": mode,
            "k": case.k,
            "latency": {"total_ms": 2.0},
            "stages": {},
            "results": [],
            "metrics_input": {
                "ranking": ["iwiki-mcp/b.md#B:0"],
                "relevant": case.relevant,
                "intents": case.intents,
            },
        }

    def fake_analyze_trace(case, trace):
        analyze_calls.append((case.case_id, trace["status"]))
        return []

    monkeypatch.setattr(runner, "trace_query", fake_trace_query)
    monkeypatch.setattr(runner, "analyze_trace", fake_analyze_trace)

    evidence = runner.run_live_traces(
        {},
        "iwiki-mcp",
        ["hybrid"],
        [failing_case, passing_case],
        base="/tmp/wiki",
    )

    assert [(trace["case_id"], trace["status"]) for trace in evidence["traces"]] == [
        ("case-a", "failed"),
        ("case-b", "passed"),
    ]
    failed_trace = evidence["traces"][0]
    assert failed_trace["mode"] == "hybrid"
    assert failed_trace["error"] == {
        "type": "RuntimeError",
        "category": "query_runtime_error",
        "message": "query failed during benchmark execution",
    }
    assert evidence["summary"]["rollup"]["case_count"] == 1
    assert analyze_calls == [("case-b", "passed")]
    assert evidence["findings"] == [
        {
            "case_id": "case-a",
            "class": "query_failed",
            "severity": "high",
            "mode": "hybrid",
            "evidence": {
                "domain": "iwiki-mcp",
                "error_type": "RuntimeError",
                "error_category": "query_runtime_error",
            },
        }
    ]
    assert evidence["backlog"] == [
        {"class": "query_failed", "count": 1, "severity": "high"}
    ]
    assert "LEAK_SENTINEL_123" not in repr(evidence)
    assert "/tmp/private/wiki-base" not in repr(evidence)


def test_run_live_traces_suppresses_store_migration_on_read_path(
    tmp_path,
    monkeypatch,
):
    from eval.search_pipeline import runner
    from iwiki_mcp import retrieval

    cfg, base, case = _build_trace_fixture(tmp_path, monkeypatch)

    def fail_migration(*args, **kwargs):
        raise AssertionError("benchmark read path must not migrate store")

    monkeypatch.setattr(retrieval, "migrate_store_location", fail_migration)

    evidence = runner.run_live_traces(
        cfg,
        case.domain,
        ["hybrid"],
        [case],
        base=base,
    )

    assert evidence["kind"] == "live"
    assert evidence["summary"]["rollup"]["case_count"] == 1
    assert evidence["traces"][0]["case_id"] == case.case_id


def test_pareto_experiment_runs_required_matrix_and_excludes_warmups(monkeypatch):
    from eval.search_pipeline import runner

    cases = [
        BenchmarkCase(
            case_id=f"case-{index}",
            domain="iwiki-mcp",
            query=f"query {index}",
            relevant={f"iwiki-mcp/{index}.md#Section:0": 3},
            intents={"intent": [f"iwiki-mcp/{index}.md#Section:0"]},
            k=8,
        )
        for index in range(12)
    ]
    calls = []

    def fake_trace_query(
        cfg,
        base,
        case,
        mode,
        rerank_enabled,
        *,
        fusion_weights=None,
        rerank_candidate_limit=None,
    ):
        calls.append((mode, rerank_enabled, rerank_candidate_limit, fusion_weights))
        ranking = list(case.relevant)
        return {
            "case_id": case.case_id,
            "domain": case.domain,
            "query": case.query,
            "mode": mode,
            "k": case.k,
            "latency": {"total_ms": 10.0, "rerank_ms": 4.0},
            "stages": {"signals": {"ranked": {}}, "fusion": {}},
            "results": [],
            "metrics_input": {
                "ranking": ranking,
                "relevant": case.relevant,
                "intents": case.intents,
            },
        }

    monkeypatch.setattr(runner, "trace_query", fake_trace_query)
    monkeypatch.setattr(
        runner,
        "select_fusion_weights",
        lambda cases, traces: {
            "status": "passed",
            "weights": {"semantic_chunk": 1.0, "graph_page": 0.01},
            "candidates": [],
        },
    )
    monkeypatch.setattr(
        runner,
        "select_rerank_batch",
        lambda cases, batch_runs: {
            "status": "passed", "batch": 16, "candidates": [],
        },
    )

    evidence = runner.run_pareto_experiment({}, "iwiki-mcp", cases, base="/wiki")

    assert evidence["kind"] == "live-pareto"
    assert evidence["baseline"]["modes"] == ["hybrid", "lexical", "semantic"]
    assert set(evidence["rerank_batches"]) == {"16", "24", "32"}
    assert all(run["sample_count"] == 24 for run in evidence["rerank_batches"].values())
    assert evidence["run_settings"]["warmup_passes"] == 1
    assert evidence["run_settings"]["measured_passes"] == 2
    assert len(calls) == 36 + (3 * 12) + (3 * 2 * 12)
    assert all("query" not in trace for trace in evidence["traces"])
    measured = [
        trace
        for run in evidence["rerank_batches"].values()
        for trace in run["traces"]
    ]
    assert len({trace["sample_id"] for trace in measured}) == 6


def test_pareto_experiment_stops_before_rerank_matrix_when_fusion_needs_work(
    monkeypatch,
):
    from eval.search_pipeline import runner

    case = BenchmarkCase(
        case_id="case-a",
        domain="iwiki-mcp",
        query="secret query text",
        relevant={"iwiki-mcp/a.md#A:0": 3},
        intents={"a": ["iwiki-mcp/a.md#A:0"]},
        k=8,
    )
    calls = []

    def fake_trace_query(cfg, base, case, mode, rerank_enabled, **kwargs):
        calls.append((mode, rerank_enabled))
        return {
            "case_id": case.case_id,
            "domain": case.domain,
            "query": case.query,
            "mode": mode,
            "k": case.k,
            "latency": {"total_ms": 1.0, "rerank_ms": 0.0},
            "stages": {"signals": {"ranked": {}}},
            "results": [],
            "metrics_input": {
                "ranking": [],
                "relevant": case.relevant,
                "intents": case.intents,
            },
        }

    monkeypatch.setattr(runner, "trace_query", fake_trace_query)
    monkeypatch.setattr(
        runner,
        "select_fusion_weights",
        lambda cases, traces: {
            "status": "needs_work",
            "reason": "no_passing_weight_map",
            "candidates": [],
        },
    )

    evidence = runner.run_pareto_experiment({}, "iwiki-mcp", [case], base="/wiki")

    assert evidence["decision"]["status"] == "needs_work"
    assert "rerank_batches" not in evidence
    assert calls == [(mode, False) for mode in ("hybrid", "lexical", "semantic")]
    assert "secret query text" not in repr(evidence)


def test_pareto_experiment_rejects_failed_batch16_with_valid_batch32(
    monkeypatch,
):
    from eval.search_pipeline import runner

    case = BenchmarkCase(
        case_id="case-a",
        domain="iwiki-mcp",
        query="query",
        relevant={"iwiki-mcp/a.md#A:0": 3},
        intents={"a": ["iwiki-mcp/a.md#A:0"]},
        k=8,
    )

    def fake_trace_query(
        cfg,
        base,
        case,
        mode,
        rerank_enabled,
        *,
        rerank_candidate_limit=None,
        **kwargs,
    ):
        trace = {
            "case_id": case.case_id,
            "domain": case.domain,
            "query": case.query,
            "mode": mode,
            "k": case.k,
            "status": "passed",
            "latency": {"total_ms": 10.0, "rerank_ms": 100.0},
            "stages": {"signals": {"ranked": {}}, "fusion": {}},
            "results": [],
            "metrics_input": {
                "ranking": list(case.relevant),
                "relevant": case.relevant,
                "intents": case.intents,
            },
        }
        if rerank_enabled and rerank_candidate_limit == 16:
            trace["status"] = "failed"
            trace["latency"] = {"total_ms": 10.0}
        return trace

    monkeypatch.setattr(runner, "trace_query", fake_trace_query)
    monkeypatch.setattr(
        runner,
        "select_fusion_weights",
        lambda cases, traces: {
            "status": "passed",
            "weights": {"semantic_chunk": 1.0, "graph_page": 0.01},
            "candidates": [],
        },
    )

    evidence = runner.run_pareto_experiment({}, "iwiki-mcp", [case], base="/wiki")

    assert evidence["rerank_batches"]["16"]["p50_rerank_ms"] is None
    assert evidence["rerank_batches"]["16"]["p95_rerank_ms"] is None
    assert evidence["rerank_batches"]["32"]["p95_rerank_ms"] == 100.0
    assert evidence["decision"]["status"] == "needs_work"
    assert evidence["decision"]["reason"] == "invalid_rerank_evidence"
