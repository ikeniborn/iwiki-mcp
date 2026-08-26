"""Safe, deterministic report writing for unified-search evaluation."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any

_SECRET_KEY = re.compile(r"(?i)(authorization|bearer|auth|token|api[_-]?key|key|password|credential|exception|traceback)")
_URL = re.compile(r"(?i)\b(?:https?|postgres(?:ql)?|sqlite)://[^\s'\"]+")
_PATH = re.compile(r"(?<![\w.-])/(?:[^\s/]+/)+[^\s/]+")
_WINDOWS_PATH = re.compile(r"(?i)\b[a-z]:\\(?:[^\\\s]+\\)+[^\\\s]+")
_SECRET = re.compile(r"(?i)(bearer\s+\S+|authorization\s*[:=]\s*\S+|token\s*[:=]\s*\S+|password\s*[:=]\s*\S+|key\s*[:=]\s*\S+)")
_SENTINEL = re.compile(r"(?i)(?:secret|credential)[_-]?sentinel\w*")
_DSN = re.compile(r"(?i)\b(?:postgres(?:ql)?|sqlite)(?:\+\w+)?:[^\s'\"]+")
_DSN_KEY_VALUE = re.compile(r"(?i)\b(?:host|dbname|user|password|port|sslmode)\s*=")


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key if isinstance(key, str) else "[unsupported-key]": "[redacted]" if isinstance(key, str) and _SECRET_KEY.search(key) else sanitize(nested)
                for key, nested in sorted(value.items(), key=lambda pair: pair[0] if isinstance(pair[0], str) else "[unsupported-key]")}
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        if _DSN_KEY_VALUE.search(value):
            return "[redacted-dsn]"
        return _WINDOWS_PATH.sub("[redacted-path]", _PATH.sub("[redacted-path]", _DSN.sub("[redacted-dsn]", _URL.sub("[redacted-url]", _SENTINEL.sub("[redacted]", _SECRET.sub("[redacted]", value))))))
    if isinstance(value, BaseException):
        return "[redacted-exception]"
    if isinstance(value, float) and not math.isfinite(value):
        return "[non-finite-float]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"[unsupported:{type(value).__name__}]"


def render_markdown(evidence: dict[str, Any]) -> str:
    value = sanitize(evidence)
    lines = ["# Wiki Unified Search Evaluation", "", f"- Decision: {value.get('decision', 'blocked')}",
             f"- Blocker: {value.get('blocker') or 'none'}", f"- Model: {value.get('model') or 'missing'}",
             f"- Runs: {value.get('runs', 0)}", "", "## Raw parity", "",
             "| Case | Backend | Passed |", "| --- | --- | --- |"]
    for row in sorted(value.get("raw_parity", []), key=lambda item: item.get("case_id", "")):
        lines.append(f"| {row.get('case_id', '')} | {row.get('backend', '')} | {row.get('passed', False)} |")
    sampling = value.get("sampling", {})
    attempt_counts = value.get("attempt_counts", {})
    included = attempt_counts.get("included_pairs", 0)
    excluded = attempt_counts.get("excluded_pairs", 0)
    quality = value.get("quality", {})
    calls = value.get("tool_calls", {})
    excluded_calls = calls.get("excluded_attempt_calls", {})
    per_case = value.get("aggregates", {}).get("per_case", {})
    preflight = value.get("preflight", {})
    protocol = value.get("protocol", {})
    gates = value.get("gates", {})
    lines.extend(["", "## Preflight", "", f"- Preflight: {preflight.get('status', 'not_run')} (available={preflight.get('available', False)})", "",
                  "## Protocol", "", f"- Protocol required pairs: {protocol.get('required_pairs', 0)}",
                  f"- Protocol max attempts: {protocol.get('max_attempts', 0)}", f"- Protocol case IDs: {json.dumps(protocol.get('expected_case_ids', []), ensure_ascii=False)}", "",
                  "## Decision gates", "", f"```json\n{json.dumps(gates, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)}\n```", "",
                  "## Sampling", "", f"- Required pairs: {sampling.get('required_pairs', 0)}",
                  f"- Max attempts: {sampling.get('max_attempts', 0)}", f"- Total attempts: {attempt_counts.get('total_attempts', 0)}",
                  f"- Included pairs: {included}", f"- Excluded pairs: {excluded}",
                  f"- Exclusion reasons: {json.dumps(attempt_counts.get('exclusion_reasons', {}), sort_keys=True, ensure_ascii=False)}", f"- Complete: {sampling.get('complete', False)}",
                  f"- Attempt cap exhausted: {sampling.get('attempt_cap_exhausted', False)}", "",
                  "## Quality", "", f"- Aggregate lower bound: {quality.get('aggregate_lower_bound', 0.0)}",
                  f"- Scenario lower bounds: {json.dumps(quality.get('scenario_lower_bounds', {}), sort_keys=True, ensure_ascii=False, allow_nan=False)}",
                  f"- Non-inferiority margin: {quality.get('non_inferiority_margin', 0.0)}", f"- Bootstrap samples: {quality.get('bootstrap_samples', 0)}",
                  f"- Bootstrap seed: {quality.get('bootstrap_seed', 0)}", "",
                  "## Scenario success rates", ""])
    for case_id, rates in sorted(per_case.items()):
        lines.append(f"- {case_id}: candidate {rates.get('candidate_success_rate', 0.0)}; baseline {rates.get('baseline_success_rate', 0.0)}; bound {quality.get('scenario_lower_bounds', {}).get(case_id, 0.0)}")
    lines.extend(["", "## Per-case attempts", ""])
    for case_id, counts in sorted(attempt_counts.get("per_case", {}).items()):
        lines.append(f"- {case_id}: total {counts.get('total_attempts', 0)}; included {counts.get('included_pairs', 0)}; excluded {counts.get('excluded_pairs', 0)}; reasons {json.dumps(counts.get('exclusion_reasons', {}), sort_keys=True, ensure_ascii=False)}")
    lines.extend(["",
                  "## Failure counts", "", f"```json\n{json.dumps(value.get('workflow_failure_counts', {}), sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)}\n```", "",
                  "## Secondary tool calls", "", f"- Included-pair candidate mean: {calls.get('included_pair_candidate_mean', 0.0)}",
                  f"- Included-pair baseline mean: {calls.get('included_pair_baseline_mean', 0.0)}", f"- Included-pair mean difference: {calls.get('included_pair_mean_difference', 0.0)}",
                  f"- Excluded-attempt calls: {json.dumps(excluded_calls, sort_keys=True, ensure_ascii=False, allow_nan=False)}", "",
                  "## Registry state", "", f"- Public registry contains tool: {value.get('public_registry_contains_tool', False)}", "",
                  "## Evaluation", "", "```json", json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False), "```", ""])
    return "\n".join(lines)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".iwiki-unified-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def write_reports(evidence: dict[str, Any], output_dir: str | Path) -> dict[str, Path]:
    safe = sanitize(evidence)
    directory = Path(output_dir)
    paths = {"json": directory / "wiki-unified-search-evaluation.json",
             "markdown": directory / "wiki-unified-search-evaluation.md"}
    _atomic_write(paths["json"], json.dumps(safe, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    _atomic_write(paths["markdown"], render_markdown(safe))
    return paths
