"""OKF reserved-file generators (stdlib-only, deterministic) plus the shared
RESERVED_OKF name constant. Same domain state -> identical bytes. Safe to import
from the config-free engine modules (lint, grep) and the top layer."""
from __future__ import annotations

RESERVED_OKF = ("index.md", "log.md")


def render_index(slugs: list[str]) -> str:
    """OKF index.md: a heading plus a sorted markdown-link list of page slugs."""
    lines = ["# Index", ""]
    lines += [f"- [{s}]({s}.md)" for s in sorted(slugs)]
    return "\n".join(lines) + "\n"


def render_log(records: list[dict]) -> str:
    """OKF log.md: a heading plus one line per ingest-log record, in file order."""
    lines = ["# Log", ""]
    for r in records:
        lines.append(
            f"- {r.get('date', '')} {r.get('op', '')} {r.get('page', '')}".rstrip()
        )
    return "\n".join(lines) + "\n"
