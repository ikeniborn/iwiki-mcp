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


def _needs_quote(s: str) -> bool:
    return (s == "" or s != s.strip() or s[:1] in ("[", "{", '"')
            or "," in s or ": " in s or s.endswith(":"))


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
        if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
            meta[key] = val[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        elif val.startswith("[") and val.endswith("]"):
            items = [x.strip() for x in val[1:-1].split(",")]
            meta[key] = [x for x in items if x]
        else:
            meta[key] = val
    return meta, content[m.end():]


def render(meta: dict) -> str:
    """Emit a frontmatter block in a stable key order. Lists render inline;
    scalar strings are double-quoted (with escaping) when bare emission would
    be ambiguous or invalid YAML (see ``_needs_quote``)."""
    order = ["type", "title", "description", "resource", "tags", "timestamp"]
    keys = [k for k in order if k in meta] + [k for k in meta if k not in order]
    lines = ["---"]
    for k in keys:
        v = meta[k]
        if isinstance(v, list):
            lines.append(f"{k}: [{', '.join(v)}]")
        else:
            v = str(v)
            if _needs_quote(v):
                esc = v.replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'{k}: "{esc}"')
            else:
                lines.append(f"{k}: {v}")
    lines.append("---\n")
    return "\n".join(lines)


def normalize_tag(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[\s_,\[\]]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s


def normalize_tags(tags: list[str]) -> list[str]:
    if isinstance(tags, str):
        tags = [tags]
    out: list[str] = []
    for t in tags:
        n = normalize_tag(t)
        if n and n not in out:
            out.append(n)
    return out[:MAX_TAGS]


def coerce_type(s: str | None) -> str:
    s = (s or "").strip().lower()
    return s if s in OKF_TYPES else DEFAULT_TYPE


def derive_title(body: str, slug: str) -> str:
    h2 = _H2.search(body)
    head = body[:h2.start()] if h2 else body
    m = _H1.search(head)
    if m and m.group(1).strip():
        return m.group(1).strip()
    stem = os.path.basename(slug)
    return stem.replace("-", " ").replace("_", " ").strip()


def derive_description(body: str, max_chars: int = 400) -> str:
    """Only the FIRST ``##`` section may serve as the description source,
    mirroring chunk.py/validate.py: an Overview elsewhere in the body doesn't count."""
    ms = list(_H2.finditer(body))
    if not ms or ms[0].group(1).strip().lower() != OVERVIEW_HEADING:
        return ""
    m = ms[0]
    end = ms[1].start() if len(ms) > 1 else len(body)
    return " ".join(body[m.end():end].split())[:max_chars]
