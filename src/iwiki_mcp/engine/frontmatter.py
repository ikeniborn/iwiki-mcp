"""OKF frontmatter: split/render a minimal YAML subset (stdlib-only, no pyyaml),
plus the governed type/tag vocabulary. Importable by validate/lint (config-free).
"""
from __future__ import annotations
import json
import os
import re

OKF_TYPES = ("architecture", "api", "guide", "reference", "runbook", "concept")
DEFAULT_TYPE = "concept"
MAX_TAGS = 5
STATUS_VOCAB = ("stub", "developing", "stable", "deprecated")
DEFAULT_STATUS = "stub"
# Reserved ## sections: authored link lists, excluded from chunking/embedding and
# exempt from lead checks. Lower-case; compared case-insensitively. Referenced by
# chunk.py and validate.py so the set lives in one config-free place.
RESERVED_SECTIONS = ("outgoing links", "external links")

OVERVIEW_HEADING = "overview"   # reserved summary section; consumed by chunk.py / okf.py
_H1 = re.compile(r"^#\s+(.*?)\s*$", re.MULTILINE)
_H2 = re.compile(r"^##\s+(.*?)[ \t]*$", re.MULTILINE)
_FM = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


class FrontmatterError(ValueError):
    """Raised when supported nested frontmatter is malformed."""


class _OpaqueCode(tuple):
    """Retain malformed authored code lines for fail-soft ordinary Wiki paths."""


class _OpaqueMeta(dict):
    """Retain a whole ambiguous frontmatter block for fail-soft read paths."""

    def __init__(self, values: dict, raw_frontmatter: str):
        super().__init__(values)
        self.raw_frontmatter = raw_frontmatter


def _needs_quote(s: str) -> bool:
    return (s == "" or s != s.strip() or s[:1] in "-?:,[]{}#&*!|>'\"%@`"
            or "," in s or ": " in s or s.endswith(":"))


def _scalar(value: str):
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        return parsed if isinstance(parsed, str) else value
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if value.startswith("[") and value.endswith("]"):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, list) and all(type(item) is str for item in parsed):
            return parsed
        items = [item.strip() for item in value[1:-1].split(",")]
        return [_scalar(item) for item in items if item]
    return value


def _nested_code(lines: list[str]) -> dict:
    code: dict = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        match = re.fullmatch(r"  ([^:\s][^:]*):\s*(.*)", line)
        if match is None:
            raise FrontmatterError("invalid nested code frontmatter")
        key, inline = match.group(1).strip(), match.group(2).strip()
        if key in code:
            raise FrontmatterError("duplicate nested code frontmatter key")
        if inline:
            code[key] = _scalar(inline)
            index += 1
            continue
        values: list = []
        index += 1
        while index < len(lines):
            item = lines[index]
            if item.startswith("  ") and not item.startswith("    "):
                break
            continuation = re.fullmatch(
                r"      ([^:\s][^:]*):\s*(.*)", item
            )
            if continuation is not None and values and isinstance(values[-1], dict):
                nested_key = continuation.group(1).strip()
                if nested_key in values[-1]:
                    raise FrontmatterError(
                        "duplicate nested code frontmatter key"
                    )
                values[-1][nested_key] = _scalar(
                    continuation.group(2).strip()
                )
                index += 1
                continue
            item_match = re.fullmatch(r"    -(?:\s+(.*))?", item)
            if item_match is None:
                raise FrontmatterError("invalid nested code frontmatter")
            payload = (item_match.group(1) or "").strip()
            mapping_match = re.fullmatch(r"([^:\s][^:]*):\s*(.*)", payload)
            if mapping_match is not None:
                values.append({
                    mapping_match.group(1).strip(): _scalar(
                        mapping_match.group(2).strip()
                    )
                })
            else:
                values.append(_scalar(payload))
            index += 1
        code[key] = values
    return code


def split(content: str, *, strict_code: bool = False) -> tuple[dict, str]:
    """Strip a leading ``---\\n…\\n---\\n`` block. Fail-soft: no/broken block -> ({}, content)."""
    m = _FM.match(content)
    if not m:
        return {}, content
    meta: dict = {}
    lines = m.group(1).splitlines()
    index = 0
    seen_code = False
    while index < len(lines):
        line = lines[index]
        line = line.rstrip()
        if not line or line.startswith((" ", "\t")) or ":" not in line:
            index += 1
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if key == "code":
            if seen_code:
                if strict_code:
                    raise FrontmatterError("duplicate code frontmatter key")
                return _OpaqueMeta(meta, content[:m.end()]), content[m.end():]
            seen_code = True
        if key == "code" and not val:
            nested: list[str] = []
            index += 1
            while index < len(lines) and (
                not lines[index].strip()
                or lines[index].startswith((" ", "\t"))
            ):
                nested.append(lines[index])
                index += 1
            try:
                meta[key] = _nested_code(nested)
            except FrontmatterError:
                if strict_code:
                    raise
                meta[key] = _OpaqueCode(nested)
            continue
        else:
            meta[key] = _scalar(val)
        index += 1
    return meta, content[m.end():]


def _render_scalar(value: object) -> str:
    if type(value) is not str:
        raise TypeError("frontmatter values must be strings, lists, or mappings")
    if _needs_quote(value):
        return json.dumps(value, ensure_ascii=False)
    return value


def _render_code(value: object) -> list[str]:
    if isinstance(value, _OpaqueCode):
        return ["code:", *value]
    if not isinstance(value, dict):
        raise TypeError("code frontmatter must be a mapping")
    lines = ["code:"]
    for key, items in value.items():
        if type(key) is not str or type(items) is not list:
            raise TypeError("code frontmatter must contain list values")
        lines.append(f"  {key}:")
        for item in items:
            if isinstance(item, dict):
                if not item:
                    raise TypeError("nested selector mappings must not be empty")
                for index, (nested_key, nested_value) in enumerate(item.items()):
                    if type(nested_key) is not str:
                        raise TypeError("selector keys must be strings")
                    prefix = "    - " if index == 0 else "      "
                    lines.append(
                        f"{prefix}{nested_key}: {_render_scalar(nested_value)}"
                    )
            else:
                lines.append(f"    - {_render_scalar(item)}")
    return lines


def render(meta: dict) -> str:
    """Emit a frontmatter block in a stable key order. Lists render inline;
    scalar strings are double-quoted (with escaping) when bare emission would
    be ambiguous or invalid YAML (see ``_needs_quote``)."""
    if isinstance(meta, _OpaqueMeta):
        return meta.raw_frontmatter
    order = [
        "type", "title", "description", "resource", "tags", "status",
        "code", "timestamp",
    ]
    keys = [k for k in order if k in meta] + [k for k in meta if k not in order]
    lines = ["---"]
    for k in keys:
        v = meta[k]
        if k == "code":
            lines.extend(_render_code(v))
        elif isinstance(v, list):
            lines.append(f"{k}: [{', '.join(_render_scalar(item) for item in v)}]")
        else:
            lines.append(f"{k}: {_render_scalar(v)}")
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


def normalize_type(s: str | None) -> str:
    """Trim/lower-case a type for matching. Open vocabulary — NOT clamped to
    OKF_TYPES (that stays advisory, flagged by validate/lint). Empty -> DEFAULT_TYPE."""
    return (s or "").strip().lower() or DEFAULT_TYPE


def normalize_status(s: str | None) -> str:
    """Trim/lower-case a status. Open like type: a value outside STATUS_VOCAB is
    kept as-is (flagged advisory). Empty -> DEFAULT_STATUS."""
    return (s or "").strip().lower() or DEFAULT_STATUS


def derive_title(body: str, slug: str) -> str:
    h2 = _H2.search(body)
    head = body[:h2.start()] if h2 else body
    m = _H1.search(head)
    if m and m.group(1).strip():
        return m.group(1).strip()
    stem = os.path.basename(slug)
    return stem.replace("-", " ").replace("_", " ").strip()


def derive_description(body: str, max_chars: int | None = None) -> str:
    """Only the FIRST ``##`` section may serve as the description source,
    mirroring chunk.py/validate.py: an Overview elsewhere doesn't count.
    ``max_chars=None`` returns the full text (the stored description must not
    lose context); an explicit int caps it (e.g. an embedding-prefix caller)."""
    ms = list(_H2.finditer(body))
    if not ms or ms[0].group(1).strip().lower() != OVERVIEW_HEADING:
        return ""
    m = ms[0]
    end = ms[1].start() if len(ms) > 1 else len(body)
    text = " ".join(body[m.end():end].split())
    return text if max_chars is None else text[:max_chars]
