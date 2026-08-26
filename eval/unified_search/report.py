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
    lines.extend(["", "## Evaluation", "", "```json", json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False), "```", ""])
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
