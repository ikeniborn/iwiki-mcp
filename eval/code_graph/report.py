"""Stable JSON and Markdown evidence for the code graph benchmark."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping


def _value(value: object) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


def _mapping_table(values: Mapping[str, object]) -> list[str]:
    rows = ["| Metric | Value |", "|---|---:|"]
    for key in sorted(values):
        value = values[key]
        if isinstance(value, (dict, list)):
            continue
        rows.append(f"| `{key}` | {_value(value)} |")
    return rows


def _evidence_value(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) if value else "none"
    return _value(value)


def render_markdown(report: Mapping[str, object]) -> str:
    """Render one compact human-readable report from canonical evidence."""
    status = "PASS" if report["passed"] else "FAIL"
    corpora = report["corpora"]
    search_corpus = corpora["search"]
    production_corpus = corpora["production"]
    quality = report["quality"]
    performance = report["performance"]
    gates = report["gates"]
    search_cases = performance["search_cases"]
    search_latency_policy = report["search_latency_policy"]
    lines = [
        "# Code graph benchmark evidence",
        "",
        f"Overall gate: **{status}**",
        "",
        "## Invocation",
        "",
        f"- Command: `{report['command']}`",
        f"- Search corpus SHA-256: `{search_corpus['sha256']}`",
        f"- Unified search entities: {search_corpus['entity_count']}",
        f"- Production corpus SHA-256: `{production_corpus['sha256']}`",
        f"- Accepted production source bytes: {production_corpus['accepted_source_bytes']}",
        "",
        "## Search corpus",
        "",
        *_mapping_table(search_corpus),
        "",
        "## Production corpus",
        "",
        *_mapping_table(production_corpus),
        "",
        "## Publication",
        "",
        *_mapping_table(report["publication"]),
        "",
        "## Independent golden truth",
        "",
        *_mapping_table(report["golden_truth"]),
        "",
        "## Environment",
        "",
        *_mapping_table(report["environment"]),
        "",
        "## Versions",
        "",
        *_mapping_table(report["versions"]),
        "",
        "## Warm/cold policy",
        "",
        *_mapping_table(report["warm_cold_policy"]),
        "",
        "## Quality",
        "",
        *_mapping_table(quality),
        "",
        "## Quality provenance",
        "",
        *_mapping_table(report["quality_provenance"]),
        "",
        "## Performance",
        "",
        *_mapping_table(performance),
        "",
        "## Search cases",
        "",
        "Release gate: <500 ms",
        "",
        "Post-v1 target: <150 ms (non-blocking)",
        "",
        (
            "| Case | Expected | Observed | Cold ms | Warm median ms | Warm p95 ms | "
            "Warm max ms | Post-v1 target |"
        ),
        "|---|---|---|---:|---:|---:|---:|:---:|",
    ]
    for case in search_cases:
        lines.append(
            f"| `{case['name']}` | `{case['expected_rank']}` | "
            f"`{case['observed_rank']}` | {_value(case['cold_ms'])} | "
            f"{_value(case['warm_median_ms'])} | "
            f"{_value(case['warm_p95_ms'])} | {_value(case['warm_max_ms'])} | "
            f"{_value(case['meets_post_v1_target'])} |"
        )
    lines.extend((
        "",
        "## Search latency policy",
        "",
        *_mapping_table(search_latency_policy["release_gate"]),
        "",
        *_mapping_table(search_latency_policy["post_v1_target"]),
        "",
        "## Gates",
        "",
        "| Gate | Actual | Operator | Threshold | Result |",
        "|---|---:|:---:|---:|:---:|",
    ))
    for name in sorted(gates):
        gate = gates[name]
        gate_status = "PASS" if gate["passed"] else "FAIL"
        lines.append(
            f"| `{name}` | {_value(gate['actual'])} | "
            f"`{gate['operator']}` | {_value(gate['threshold'])} | "
            f"**{gate_status}** |"
        )
    lines.extend((
        "",
        "## Strata",
        "",
        *_mapping_table({
            name: values.get("entities", values.get("sites", values.get("targets")))
            for name, values in report["strata"].items()
        }),
        "",
        "## Constraints",
        "",
        *_mapping_table(report["constraints"]),
        "",
        "## Constraint evidence",
        "",
        *(
            f"- {name}: {_evidence_value(values)}"
            for name, values in report["constraint_evidence"].items()
            if name != "rank_limit_clause_counts"
        ),
        "",
        "### rank_limit_clause_counts",
        "",
        *_mapping_table(
            report["constraint_evidence"]["rank_limit_clause_counts"]
        ),
        "",
        "## Determinism",
        "",
        *_mapping_table(report["determinism"]),
        "",
        "Excluded operational fields:",
        "",
        *(f"- `{value}`" for value in report["determinism"]["excluded_fields"]),
        "",
    ))
    return "\n".join(lines)


def write_report(
    output: str | Path,
    report: Mapping[str, object],
) -> tuple[Path, Path]:
    """Atomically replace both evidence files after every measurement run."""
    output_path = Path(output)
    output_path.mkdir(parents=True, exist_ok=True)
    json_path = output_path / "code-graph-benchmark.json"
    markdown_path = output_path / "code-graph-benchmark.md"
    json_temp = output_path / ".code-graph-benchmark.json.tmp"
    markdown_temp = output_path / ".code-graph-benchmark.md.tmp"
    json_temp.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_temp.write_text(render_markdown(report), encoding="utf-8")
    json_temp.replace(json_path)
    markdown_temp.replace(markdown_path)
    return json_path, markdown_path


__all__ = ["render_markdown", "write_report"]
