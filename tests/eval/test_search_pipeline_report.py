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
