"""OKF frontmatter: split/render a minimal YAML subset (stdlib-only, no pyyaml),
plus the governed type/tag vocabulary. Importable by validate/lint (config-free).
"""
from __future__ import annotations
import os
import re

OKF_TYPES = ("architecture", "api", "guide", "reference", "runbook", "concept")
DEFAULT_TYPE = "concept"
MAX_TAGS = 5

OVERVIEW_HEADING = "overview"   # keep in sync with chunk.OVERVIEW_HEADING
_H1 = re.compile(r"^#\s+(.*?)\s*$", re.MULTILINE)
_H2 = re.compile(r"^##\s+(.*?)\s*$", re.MULTILINE)
_FM = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def split(content: str) -> tuple[dict, str]:
    """Strip a leading ``---\\n…\\n---\\n`` block. Fail-soft: no/broken block -> ({}, content)."""
    m = _FM.match(content)
    if not m:
        return {}, content
    meta: dict = {}
    for line in m.group(1).splitlines():
        line = line.rstrip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if val.startswith("[") and val.endswith("]"):
            items = [x.strip() for x in val[1:-1].split(",")]
            meta[key] = [x for x in items if x]
        else:
            meta[key] = val
    return meta, content[m.end():]


def render(meta: dict) -> str:
    """Emit a frontmatter block in a stable key order. Lists render inline."""
    order = ["type", "title", "description", "resource", "tags", "timestamp"]
    keys = [k for k in order if k in meta] + [k for k in meta if k not in order]
    lines = ["---"]
    for k in keys:
        v = meta[k]
        if isinstance(v, list):
            lines.append(f"{k}: [{', '.join(v)}]")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---\n")
    return "\n".join(lines)


def normalize_tag(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s


def normalize_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    for t in tags:
        n = normalize_tag(t)
        if n and n not in out:
            out.append(n)
    return out[:MAX_TAGS]


def coerce_type(s: str | None) -> str:
    return s if s in OKF_TYPES else DEFAULT_TYPE


def derive_title(body: str, slug: str) -> str:
    head = body[:_H2.search(body).start()] if _H2.search(body) else body
    m = _H1.search(head)
    if m and m.group(1).strip():
        return m.group(1).strip()
    stem = os.path.basename(slug)
    return stem.replace("-", " ").replace("_", " ").strip()


def derive_description(body: str, max_chars: int = 400) -> str:
    ms = list(_H2.finditer(body))
    for i, m in enumerate(ms):
        if m.group(1).strip().lower() != OVERVIEW_HEADING:
            continue
        end = ms[i + 1].start() if i + 1 < len(ms) else len(body)
        return " ".join(body[m.end():end].split())[:max_chars]
    return ""
