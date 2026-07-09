"""Deterministic section-formation checks over a wiki page — stdlib only, no API.

Mirrors the structural rules the authoring skills mandate (see the section-formation
spec). Consumed by ``lint`` (folded into its report) and the ``validate`` subcommand.
The blocking subset (deep_heading, pre_h2_text) is mirrored inline by the
iwiki-validate PreToolUse hook; the advisory subset (missing_overview, missing_lead,
long_lead, and — only when the page has frontmatter — missing_type, unknown_type,
missing_description) is report-only.
"""
from __future__ import annotations
import re
from . import frontmatter as _fm

OVERVIEW_HEADING = "overview"   # keep in sync with chunk.OVERVIEW_HEADING
LEAD_MAX = 250                  # keep in sync with chunk.LEAD_MAX

_DEEP = re.compile(r"^#{3,}\s", re.MULTILINE)   # ### or deeper
_H1_LINE = re.compile(r"^#\s+\S")               # a single-# H1 line
_H2 = re.compile(r"^##\s+(.*?)\s*$", re.MULTILINE)   # keep in sync with chunk._H2


def _sections(content: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    ms = list(_H2.finditer(content))
    for i, m in enumerate(ms):
        start = m.end()
        end = ms[i + 1].start() if i + 1 < len(ms) else len(content)
        out.append((m.group(1).strip(), content[start:end].strip()))
    return out


def _lead(body: str) -> str:
    para: list[str] = []
    for ln in body.splitlines():
        if not ln.strip():
            if para:
                break
            continue
        para.append(ln.strip())
    return " ".join(para)


def validate_page(content: str) -> list[dict]:
    """Return a list of {type, severity, text} section-formation findings."""
    meta, body = _fm.split(content)
    findings: list[dict] = []

    if _DEEP.search(body):
        findings.append({"type": "deep_heading", "severity": "block",
                         "text": "heading deeper than ## (###+); flatten to ##"})

    h2 = _H2.search(body)
    pre = body[:h2.start()] if h2 else body
    if any(ln.strip() and not _H1_LINE.match(ln) for ln in pre.splitlines()):
        findings.append({"type": "pre_h2_text", "severity": "block",
                         "text": "indexable text before the first ## (only a single # H1 allowed)"})

    secs = _sections(body)
    if not (secs and secs[0][0].lower() == OVERVIEW_HEADING):
        findings.append({"type": "missing_overview", "severity": "advisory",
                         "text": "first ## section is not 'Overview'"})

    for heading, sbody in secs:
        lead = _lead(sbody)
        if not lead:
            findings.append({"type": "missing_lead", "severity": "advisory",
                             "text": f"section '{heading}' has no lead paragraph"})
        elif len(lead) > LEAD_MAX:
            findings.append({"type": "long_lead", "severity": "advisory",
                             "text": f"section '{heading}' lead exceeds {LEAD_MAX} chars"})

    if meta:                       # only nudge per-field issues once frontmatter exists
        if not meta.get("type"):
            findings.append({"type": "missing_type", "severity": "advisory",
                             "text": "frontmatter has no 'type' (run wiki_migrate_okf)"})
        elif meta["type"] not in _fm.OKF_TYPES:
            findings.append({"type": "unknown_type", "severity": "advisory",
                             "text": f"type '{meta['type']}' not in the OKF vocabulary"})
        if not meta.get("description"):
            findings.append({"type": "missing_description", "severity": "advisory",
                             "text": "frontmatter has no 'description'"})
    return findings
