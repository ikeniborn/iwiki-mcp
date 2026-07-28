from __future__ import annotations

import html
import json
from pathlib import Path


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _md_cell(value) -> str:
    return _fmt(value).replace("|", "\\|").replace("\n", " ")


def render_markdown_report(evidence: dict) -> str:
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
    return "\n".join(lines) + "\n"


def _html_cell(value, tag: str = "td") -> str:
    return f"<{tag}>{html.escape(_fmt(value))}</{tag}>"


def render_html_report(evidence: dict) -> str:
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
<thead><tr><th>Case</th><th>Mode</th><th>Recall</th><th>MRR</th><th>NDCG</th><th>Intent Coverage</th><th>Latency ms</th></tr></thead>
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
</body>
</html>
"""


def write_json_evidence(evidence: dict, path: str | Path) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output


def write_reports(
    evidence: dict,
    out_dir: str | Path,
    stem: str = "search-pipeline-benchmark",
) -> dict[str, Path]:
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
