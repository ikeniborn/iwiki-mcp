import json

from eval.search_pipeline.report import render_html_report
from eval.search_pipeline.report import render_markdown_report
from eval.search_pipeline.report import write_reports


def _evidence():
    return {
        "kind": "offline",
        "timestamp": "2026-07-28T10:00:00+00:00",
        "summary": {
            "rollup": {
                "case_count": 1,
                "recall_at_k": 1.0,
                "mrr_at_k": 0.5,
                "ndcg_at_k": 0.75,
                "intent_coverage_at_k": 1.0,
                "latency_ms": 12.346,
            },
            "cases": [
                {
                    "case_id": "case-<one>",
                    "mode": "hybrid",
                    "k": 3,
                    "recall_at_k": 1.0,
                    "mrr_at_k": 0.5,
                    "ndcg_at_k": 0.75,
                    "intent_coverage_at_k": 1.0,
                    "latency_ms": 12.346,
                },
            ],
        },
        "backlog": [
            {
                "class": "missing_<candidate>",
                "count": 1,
                "severity": "high",
            },
        ],
        "findings": [
            {
                "case_id": "case-<one>",
                "class": "missing_<candidate>",
                "severity": "high",
                "identity": "eval/<unsafe>.md#A:0",
                "evidence": {"note": "<script>alert('x')</script>"},
            },
        ],
    }


def _pareto_evidence():
    evidence = _evidence()
    evidence.update({
        "kind": "live-pareto",
        "fusion_selection": {
            "status": "passed",
            "weights": {"graph_page": 0.01, "semantic_chunk": 1.0},
            "candidates": [{
                "weights": {"graph_page": 0.1},
                "passed": False,
                "reasons": ["recall_regression", "<unsafe>"],
            }],
        },
        "rerank_batches": {
            "16": {
                "sample_count": 24,
                "p50_rerank_ms": 4.0,
                "p95_rerank_ms": 8.0,
                "summary": {"rollup": {"ndcg_at_k": 0.9}},
            },
            "32": {
                "sample_count": 24,
                "p50_rerank_ms": 8.0,
                "p95_rerank_ms": 16.0,
                "summary": {"rollup": {"ndcg_at_k": 1.0}},
            },
        },
        "decision": {"status": "passed", "batch": 16},
    })
    return evidence


def _bounded_evidence():
    evidence = _evidence()
    candidate = {
        "family": "rrf_k",
        "rrf_k": 20,
        "direct_multiplier": 1.0,
        "direct_quota": 0,
        "fanout_cap": None,
        "components": [],
    }
    record = {
        "candidate": candidate,
        "candidate_key": "candidate-<unsafe>",
        "families": ["rrf_k"],
        "metrics": {"aggregate": {"ndcg_at_k": 0.8, "mrr_at_k": 0.7}, "modes": {}},
        "passed": False,
        "reasons": ["no_passing_fusion_candidate", "<unsafe>"],
    }
    evidence.update({
        "kind": "replay-bounded-fusion",
        "query": "SENTINEL_QUERY",
        "provider_url": "https://provider.example.invalid/v1",
        "authorization": "SENTINEL_AUTHORIZATION",
        "api_key": "SENTINEL_API_KEY",
        "env_file": "/private/SENTINEL_ENV_FILE",
        "base_path": "/private/SENTINEL_BASE_PATH",
        "extra": {
            "exception": "SENTINEL_EXCEPTION",
            "url": "https://sentinel.example.invalid/path",
            "path": "/private/SENTINEL_PATH",
        },
        "fusion_selection": {
            "status": "needs_work",
            "reason": "no_passing_fusion_candidate",
            "candidate": None,
            "baseline": {"metrics": {"aggregate": {"ndcg_at_k": 0.7}}},
            "stage_a": [record],
            "family_winners": [record],
            "family_rejections": ["direct_quota"],
            "stage_b": [record],
        },
        "live_confirmation": {
            "status": "not_run",
            "reason": "replay_only",
            "query": "SENTINEL_NESTED_QUERY",
        },
        "decision": {"status": "needs_work", "reason": "no_passing_fusion_candidate"},
    })
    return evidence


def _hard_negative_evidence():
    evidence = _evidence()
    evidence["fusion_selection"] = {
        "hard_negatives": [
            {
                "case_id": "case-z",
                "mode": "semantic",
                "identity": "iwiki-mcp/z.md#Z:0",
                "state": "active",
                "baseline_rank": 13,
            },
            {
                "case_id": "case-a",
                "mode": "lexical",
                "identity": (
                    "iwiki-mcp/invalid-<script>alert('x')</script>-"
                    "before\\|after|line\nnext:0"
                ),
                "state": "invalid",
                "baseline_rank": None,
            },
            {
                "case_id": "case-a",
                "mode": "hybrid",
                "identity": "iwiki-mcp/unavailable.md#Unavailable:0",
                "state": "unavailable",
                "baseline_rank": 1,
                "source_path": "/private/SENTINEL_HARD_NEGATIVE_PATH",
            },
        ],
    }
    return evidence


def test_render_markdown_report_includes_summary_and_escapes_table_pipes():
    markdown = render_markdown_report(_evidence())

    assert "# Search Pipeline Benchmark Report" in markdown
    assert "| case-<one> | hybrid | 1.000 | 0.500 | 0.750 | 1.000 | 12.346 |" in markdown
    assert "missing_<candidate>" in markdown


def test_render_html_report_escapes_values_and_uses_no_external_assets():
    html = render_html_report(_evidence())

    assert "<!doctype html>" in html
    assert "case-&lt;one&gt;" in html
    assert "missing_&lt;candidate&gt;" in html
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in html
    assert "http://" not in html
    assert "https://" not in html


def test_pareto_reports_render_selection_batches_and_recommendation_safely():
    markdown = render_markdown_report(_pareto_evidence())
    html = render_html_report(_pareto_evidence())

    assert "## Fusion Selection" in markdown
    assert "recall_regression" in markdown
    assert "## Rerank Batches" in markdown
    assert "Recommendation" in markdown
    assert "Fusion Selection" in html
    assert "Rerank Batches" in html
    assert "&lt;unsafe&gt;" in html
    assert "http://" not in html
    assert "https://" not in html


def test_hard_negative_reports_render_sorted_active_unavailable_and_invalid_records(
    tmp_path,
):
    evidence = _hard_negative_evidence()
    markdown = render_markdown_report(evidence)
    html = render_html_report(evidence)
    paths = write_reports(evidence, tmp_path, stem="hard-negatives")
    persisted = json.loads(paths["json"].read_text(encoding="utf-8"))

    assert "## Hard-Negative Cases" in markdown
    assert "| Case | Mode | Identity | State | Baseline rank |" in markdown
    assert markdown.index("case-a | hybrid") < markdown.index("case-a | lexical")
    assert markdown.index("case-a | lexical") < markdown.index("case-z | semantic")
    assert "<script>alert('x')</script>" not in markdown
    assert (
        "invalid-&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;-"
        "before\\\\\\|after\\|line next:0 "
        "| invalid | None"
    ) in markdown
    assert "unavailable | 1" in markdown
    assert "active | 13" in markdown
    assert "SENTINEL_HARD_NEGATIVE_PATH" not in markdown

    assert "<h2>Hard-Negative Cases</h2>" in html
    assert (
        "<th>Case</th><th>Mode</th><th>Identity</th><th>State</th>"
        "<th>Baseline rank</th>"
    ) in html
    assert (
        "invalid-&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;-before\\|after|line\n"
        "next:0"
    ) in html
    assert "<td>invalid</td><td>None</td>" in html
    assert "SENTINEL_HARD_NEGATIVE_PATH" not in html

    assert persisted["fusion_selection"]["hard_negatives"] == [
        {
            "baseline_rank": 1,
            "case_id": "case-a",
            "identity": "iwiki-mcp/unavailable.md#Unavailable:0",
            "mode": "hybrid",
            "source_path": "[redacted]",
            "state": "unavailable",
        },
        {
            "baseline_rank": None,
            "case_id": "case-a",
            "identity": "iwiki-mcp/invalid-<script>alert('x')</script>-before\\|after|line\nnext:0",
            "mode": "lexical",
            "state": "invalid",
        },
        {
            "baseline_rank": 13,
            "case_id": "case-z",
            "identity": "iwiki-mcp/z.md#Z:0",
            "mode": "semantic",
            "state": "active",
        },
    ]


def test_write_reports_outputs_deterministic_json_markdown_and_html(tmp_path):
    paths = write_reports(_evidence(), tmp_path, stem="evidence")

    assert set(paths) == {"json", "markdown", "html"}
    json_text = paths["json"].read_text(encoding="utf-8")
    assert json_text == json.dumps(
        _evidence(),
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
    ) + "\n"
    assert paths["markdown"].read_text(encoding="utf-8").startswith(
        "# Search Pipeline Benchmark Report\n"
    )
    assert paths["html"].read_text(encoding="utf-8").startswith("<!doctype html>\n")


def test_bounded_reports_render_selection_stages_and_remove_sensitive_values(tmp_path):
    paths = write_reports(_bounded_evidence(), tmp_path, stem="bounded")
    markdown = paths["markdown"].read_text(encoding="utf-8")
    html = paths["html"].read_text(encoding="utf-8")
    persisted = json.loads(paths["json"].read_text(encoding="utf-8"))

    assert "Stage A Candidates" in markdown
    assert "Family Winners" in markdown
    assert "Stage B Pairs" in markdown
    assert "Live Confirmation" in markdown
    assert "Stage A Candidates" in html
    assert "Family Winners" in html
    assert "Stage B Pairs" in html
    assert "Live Confirmation" in html
    assert "no_passing_fusion_candidate" in html
    assert "&lt;unsafe&gt;" in html
    assert len(persisted["fusion_selection"]["stage_b"]) <= 6

    forbidden_keys = {
        "query", "provider_url", "authorization", "api_key", "env_file", "base_path",
    }

    def assert_safe(value):
        if isinstance(value, dict):
            assert not forbidden_keys.intersection(value)
            for nested in value.values():
                assert_safe(nested)
        elif isinstance(value, list):
            for nested in value:
                assert_safe(nested)

    assert_safe(persisted)
    for value in (
        "SENTINEL_QUERY", "SENTINEL_AUTHORIZATION", "SENTINEL_API_KEY",
        "SENTINEL_ENV_FILE", "SENTINEL_BASE_PATH", "SENTINEL_EXCEPTION",
        "SENTINEL_PATH", "http://", "https://",
    ):
        assert value not in paths["json"].read_text(encoding="utf-8")
        assert value not in markdown
        assert value not in html
