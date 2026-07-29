from __future__ import annotations

import html
import json
from pathlib import Path
import re


_FORBIDDEN_KEYS = {
    "query", "provider_url", "authorization", "api_key", "env_file", "base_path",
}
_SENSITIVE_VALUE = re.compile(
    r"(?i)(secret|api[_ -]?key|authorization|credential|password|sentinel)",
)
_URL_VALUE = re.compile(r"https?://[^\s'\"<]+")
_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9_])/(?:[^\s/]+/)+[^\s/]+")


def _sanitize_string(value: str) -> str:
    if _SENSITIVE_VALUE.search(value) or value.startswith("/"):
        return "[redacted]"
    return _ABSOLUTE_PATH.sub("[redacted-path]", _URL_VALUE.sub("[redacted-url]", value))


def sanitize_evidence(value):
    if isinstance(value, dict):
        return {
            key: sanitize_evidence(nested)
            for key, nested in value.items()
            if not isinstance(key, str) or key.lower() not in _FORBIDDEN_KEYS
        }
    if isinstance(value, list):
        return [sanitize_evidence(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_evidence(item) for item in value]
    if isinstance(value, str):
        return _sanitize_string(value)
    return value


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _md_cell(value) -> str:
    return _fmt(value).replace("|", "\\|").replace("\n", " ")


def _candidate_rows(records: list[dict]) -> list[dict]:
    return sorted(
        (record for record in records if isinstance(record, dict)),
        key=lambda record: record.get(
            "candidate_key",
            json.dumps(record.get("candidate", {}), sort_keys=True),
        ),
    )


def _append_bounded_markdown(lines: list[str], evidence: dict) -> None:
    selection = evidence.get("fusion_selection", {})
    if not isinstance(selection, dict) or "stage_a" not in selection:
        return
    for heading, key in (("Stage A Candidates", "stage_a"), ("Family Winners", "family_winners"),
                         ("Stage B Pairs", "stage_b")):
        lines.extend(["", f"## {heading}", "", "| Candidate | Passed | Rejection reasons |",
                      "| --- | --- | --- |"])
        for record in _candidate_rows(selection.get(key, [])):
            lines.append("| " + " | ".join([
                _md_cell(json.dumps(record.get("candidate", {}), sort_keys=True)),
                _md_cell(record.get("passed", False)),
                _md_cell(", ".join(record.get("reasons", []))),
            ]) + " |")
    lines.extend(["", "## Family Rejections", ""])
    lines.append(
        _md_cell(", ".join(sorted(selection.get("family_rejections", []))))
        or "None"
    )
    lines.extend(["", "## Live Confirmation", ""])
    lines.append(_md_cell(json.dumps(
        evidence.get("live_confirmation", {"status": "not_run"}),
        sort_keys=True,
    )))


def render_markdown_report(evidence: dict) -> str:
    evidence = sanitize_evidence(evidence)
    rollup = evidence.get("summary", {}).get("rollup", {})
    summaries = evidence.get("summary", {}).get("cases", [])
    backlog = evidence.get("backlog", [])
    lines = [
        "# Search Pipeline Benchmark Report",
        "",
        f"- Kind: {_md_cell(evidence.get('kind', 'unknown'))}",
        f"- Timestamp: {_md_cell(evidence.get('timestamp', 'unknown'))}",
        f"- Cases: {_md_cell(rollup.get('case_count', 0))}",
        f"- Mean recall@k: {_md_cell(rollup.get('recall_at_k', 0.0))}",
        f"- Mean MRR@k: {_md_cell(rollup.get('mrr_at_k', 0.0))}",
        f"- Mean NDCG@k: {_md_cell(rollup.get('ndcg_at_k', 0.0))}",
        f"- Mean intent coverage@k: "
        f"{_md_cell(rollup.get('intent_coverage_at_k', 0.0))}",
        f"- Mean latency ms: {_md_cell(rollup.get('latency_ms', 0.0))}",
        "",
        "## Case Summaries",
        "",
        "| Case | Mode | Recall | MRR | NDCG | Intent Coverage | Latency ms |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for summary in summaries:
        lines.append(
            "| "
            + " | ".join(
                [
                    _md_cell(summary.get("case_id", "")),
                    _md_cell(summary.get("mode", "")),
                    _md_cell(summary.get("recall_at_k", 0.0)),
                    _md_cell(summary.get("mrr_at_k", 0.0)),
                    _md_cell(summary.get("ndcg_at_k", 0.0)),
                    _md_cell(summary.get("intent_coverage_at_k", 0.0)),
                    _md_cell(summary.get("latency_ms", 0.0)),
                ]
            )
            + " |"
        )

    lines.extend([
        "",
        "## Backlog",
        "",
        "| Class | Severity | Count |",
        "| --- | --- | ---: |",
    ])
    for item in backlog:
        lines.append(
            "| "
            + " | ".join(
                [
                    _md_cell(item.get("class", "")),
                    _md_cell(item.get("severity", "")),
                    _md_cell(item.get("count", 0)),
                ]
            )
            + " |"
        )
    fusion_selection = evidence.get("fusion_selection")
    if fusion_selection:
        lines.extend(["", "## Fusion Selection", ""])
        lines.append(
            f"- Status: {_md_cell(fusion_selection.get('status', 'unknown'))}"
        )
        lines.append(
            "- Weights: "
            + _md_cell(json.dumps(fusion_selection.get("weights"), sort_keys=True))
        )
        lines.extend([
            "",
            "| Weights | Passed | Rejection reasons |",
            "| --- | --- | --- |",
        ])
        for candidate in fusion_selection.get("candidates", []):
            lines.append("| " + " | ".join([
                _md_cell(json.dumps(candidate.get("weights", {}), sort_keys=True)),
                _md_cell(candidate.get("passed", False)),
                _md_cell(", ".join(candidate.get("reasons", []))),
            ]) + " |")
    rerank_batches = evidence.get("rerank_batches")
    if rerank_batches:
        lines.extend([
            "",
            "## Rerank Batches",
            "",
            "| Batch | Samples | NDCG | P50 ms | P95 ms |",
            "| --- | ---: | ---: | ---: | ---: |",
        ])
        for batch, run in sorted(
            rerank_batches.items(), key=lambda item: int(item[0])
        ):
            lines.append("| " + " | ".join([
                _md_cell(batch),
                _md_cell(run.get("sample_count", 0)),
                _md_cell(
                    run.get("summary", {}).get("rollup", {}).get(
                        "ndcg_at_k", 0.0
                    )
                ),
                _md_cell(run.get("p50_rerank_ms", 0.0)),
                _md_cell(run.get("p95_rerank_ms", 0.0)),
            ]) + " |")
    _append_bounded_markdown(lines, evidence)
    if evidence.get("decision"):
        lines.extend(["", "## Recommendation", ""])
        lines.append(_md_cell(json.dumps(evidence["decision"], sort_keys=True)))
    return "\n".join(lines) + "\n"


def _html_cell(value, tag: str = "td") -> str:
    return f"<{tag}>{html.escape(_fmt(value))}</{tag}>"


def _bounded_html_section(heading: str, records: list[dict]) -> str:
    rows = "\n".join(
        "<tr>" + "".join(_html_cell(value) for value in (
            json.dumps(record.get("candidate", {}), sort_keys=True),
            record.get("passed", False),
            ", ".join(record.get("reasons", [])),
        )) + "</tr>"
        for record in _candidate_rows(records)
    )
    return (
        f"<h2>{heading}</h2><table><thead><tr><th>Candidate</th><th>Passed</th>"
        f"<th>Rejection reasons</th></tr></thead><tbody>{rows}</tbody></table>"
    )


def render_html_report(evidence: dict) -> str:
    evidence = sanitize_evidence(evidence)
    rollup = evidence.get("summary", {}).get("rollup", {})
    summaries = evidence.get("summary", {}).get("cases", [])
    backlog = evidence.get("backlog", [])
    findings = evidence.get("findings", [])
    summary_rows = "\n".join(
        "<tr>"
        + "".join(
            _html_cell(summary.get(field, 0.0))
            for field in (
                "case_id",
                "mode",
                "recall_at_k",
                "mrr_at_k",
                "ndcg_at_k",
                "intent_coverage_at_k",
                "latency_ms",
            )
        )
        + "</tr>"
        for summary in summaries
    )
    backlog_rows = "\n".join(
        "<tr>"
        + "".join(
            _html_cell(item.get(field, ""))
            for field in ("class", "severity", "count")
        )
        + "</tr>"
        for item in backlog
    )
    findings_json = html.escape(
        json.dumps(findings, indent=2, ensure_ascii=False, sort_keys=True)
    )
    fusion_selection = evidence.get("fusion_selection", {})
    fusion_rows = "\n".join(
        "<tr>"
        + "".join(_html_cell(value) for value in (
            json.dumps(candidate.get("weights", {}), sort_keys=True),
            candidate.get("passed", False),
            ", ".join(candidate.get("reasons", [])),
        ))
        + "</tr>"
        for candidate in fusion_selection.get("candidates", [])
    )
    rerank_rows = "\n".join(
        "<tr>" + "".join(_html_cell(value) for value in (
            batch,
            run.get("sample_count", 0),
            run.get("summary", {}).get("rollup", {}).get(
                "ndcg_at_k", 0.0
            ),
            run.get("p50_rerank_ms", 0.0),
            run.get("p95_rerank_ms", 0.0),
        )) + "</tr>"
        for batch, run in sorted(
            evidence.get("rerank_batches", {}).items(), key=lambda item: int(item[0])
        )
    )
    pareto_sections = ""
    if fusion_selection:
        pareto_sections += f"""
<h2>Fusion Selection</h2>
<p>Status: {html.escape(_fmt(fusion_selection.get('status', 'unknown')))}</p>
<pre>{html.escape(json.dumps(fusion_selection.get('weights'), sort_keys=True))}</pre>
<table><thead><tr><th>Weights</th><th>Passed</th><th>Rejection reasons</th></tr></thead>
<tbody>{fusion_rows}</tbody></table>
"""
    if evidence.get("rerank_batches"):
        pareto_sections += f"""
<h2>Rerank Batches</h2>
<table><thead><tr><th>Batch</th><th>Samples</th><th>NDCG</th><th>P50 ms</th>
<th>P95 ms</th></tr></thead>
<tbody>{rerank_rows}</tbody></table>
"""
    if evidence.get("decision"):
        pareto_sections += (
            "<h2>Recommendation</h2><pre>"
            + html.escape(json.dumps(evidence["decision"], sort_keys=True))
            + "</pre>"
        )
    if isinstance(fusion_selection, dict) and "stage_a" in fusion_selection:
        pareto_sections += _bounded_html_section(
            "Stage A Candidates", fusion_selection.get("stage_a", []),
        )
        pareto_sections += _bounded_html_section(
            "Family Winners", fusion_selection.get("family_winners", []),
        )
        pareto_sections += _bounded_html_section(
            "Stage B Pairs", fusion_selection.get("stage_b", []),
        )
        pareto_sections += (
            "<h2>Family Rejections</h2><pre>"
            + html.escape(json.dumps(
                sorted(fusion_selection.get("family_rejections", [])),
            ))
            + "</pre><h2>Live Confirmation</h2><pre>"
            + html.escape(json.dumps(
                evidence.get("live_confirmation", {"status": "not_run"}),
                sort_keys=True,
            ))
            + "</pre>"
        )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Search Pipeline Benchmark Report</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #1f2933; }}
table {{ border-collapse: collapse; width: 100%; margin: 1rem 0 2rem; }}
th, td {{ border: 1px solid #d8dee4; padding: 0.45rem 0.6rem; text-align: left; }}
th {{ background: #f6f8fa; }}
code, pre {{ background: #f6f8fa; padding: 0.2rem 0.3rem; }}
pre {{ overflow: auto; padding: 1rem; }}
</style>
</head>
<body>
<h1>Search Pipeline Benchmark Report</h1>
<dl>
<dt>Kind</dt><dd>{html.escape(_fmt(evidence.get("kind", "unknown")))}</dd>
<dt>Timestamp</dt><dd>{html.escape(_fmt(evidence.get("timestamp", "unknown")))}</dd>
<dt>Cases</dt><dd>{html.escape(_fmt(rollup.get("case_count", 0)))}</dd>
<dt>Mean recall@k</dt><dd>{html.escape(_fmt(rollup.get("recall_at_k", 0.0)))}</dd>
<dt>Mean MRR@k</dt><dd>{html.escape(_fmt(rollup.get("mrr_at_k", 0.0)))}</dd>
<dt>Mean NDCG@k</dt><dd>{html.escape(_fmt(rollup.get("ndcg_at_k", 0.0)))}</dd>
<dt>Mean intent coverage@k</dt><dd>{html.escape(_fmt(rollup.get("intent_coverage_at_k", 0.0)))}</dd>
<dt>Mean latency ms</dt><dd>{html.escape(_fmt(rollup.get("latency_ms", 0.0)))}</dd>
</dl>
<h2>Case Summaries</h2>
<table>
<thead><tr><th>Case</th><th>Mode</th><th>Recall</th><th>MRR</th><th>NDCG</th>
<th>Intent Coverage</th><th>Latency ms</th></tr></thead>
<tbody>
{summary_rows}
</tbody>
</table>
<h2>Backlog</h2>
<table>
<thead><tr><th>Class</th><th>Severity</th><th>Count</th></tr></thead>
<tbody>
{backlog_rows}
</tbody>
</table>
<h2>Findings</h2>
<pre>{findings_json}</pre>
{pareto_sections}
</body>
</html>
"""


def write_json_evidence(evidence: dict, path: str | Path) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            sanitize_evidence(evidence),
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    return output


def write_reports(
    evidence: dict,
    out_dir: str | Path,
    stem: str = "search-pipeline-benchmark",
) -> dict[str, Path]:
    evidence = sanitize_evidence(evidence)
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": output_dir / f"{stem}.json",
        "markdown": output_dir / f"{stem}.md",
        "html": output_dir / f"{stem}.html",
    }
    write_json_evidence(evidence, paths["json"])
    paths["markdown"].write_text(render_markdown_report(evidence), encoding="utf-8")
    paths["html"].write_text(render_html_report(evidence), encoding="utf-8")
    return paths
