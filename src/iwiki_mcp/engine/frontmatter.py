"""Parse and render OKF frontmatter and define its governed vocabulary."""
from __future__ import annotations
import json
import math
import os
import re

import yaml

OKF_TYPES = (
    "architecture", "api", "guide", "reference", "runbook", "concept",
    "specification",
)
CLASSIFIABLE_TYPES = tuple(item for item in OKF_TYPES if item != "specification")
DEFAULT_TYPE = "concept"
MAX_TAGS = 5
STATUS_VOCAB = ("draft", "stable", "deprecated", "stub", "developing")
DEFAULT_STATUS = "stable"
# Reserved ## sections: authored link lists, excluded from chunking/embedding and
# exempt from lead checks. Lower-case; compared case-insensitively. Referenced by
# chunk.py and validate.py so the set lives in one config-free place.
RESERVED_SECTIONS = ("outgoing links", "external links")

OVERVIEW_HEADING = "overview"   # reserved summary section; consumed by chunk.py / okf.py
_H1 = re.compile(r"^#\s+(.*?)\s*$", re.MULTILINE)
_H2 = re.compile(r"^##\s+(.*?)[ \t]*$", re.MULTILINE)
_FM = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)


class FrontmatterError(ValueError):
    """Raised when supported nested frontmatter is malformed."""


class _SelectorError(FrontmatterError):
    """Malformed authored code selectors remain errors on section reads."""


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
            raise _SelectorError("invalid nested code frontmatter")
        key, inline = match.group(1).strip(), match.group(2).strip()
        if key in code:
            raise _SelectorError("duplicate nested code frontmatter key")
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
                    raise _SelectorError(
                        "duplicate nested code frontmatter key"
                    )
                values[-1][nested_key] = _scalar(
                    continuation.group(2).strip()
                )
                index += 1
                continue
            item_match = re.fullmatch(r"    -(?:\s+(.*))?", item)
            if item_match is None:
                raise _SelectorError("invalid nested code frontmatter")
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


_SCALAR_TAGS = {f"tag:yaml.org,2002:{tag}"
                for tag in ("str", "null", "int", "float", "bool", "timestamp")}
_SUPPORTED_TAGS = _SCALAR_TAGS | {"tag:yaml.org,2002:seq", "tag:yaml.org,2002:map"}


class _Loader(getattr(yaml, "CSafeLoader", yaml.SafeLoader)):
    """Reject ambiguous mappings before an edit can discard their earlier keys."""

    def construct_object(self, node, deep=False):
        if node.tag not in _SUPPORTED_TAGS:
            raise FrontmatterError("unsupported YAML frontmatter tag")
        value = super().construct_object(node, deep)
        if isinstance(value, float) and not math.isfinite(value):
            raise FrontmatterError("non-finite number in frontmatter")
        return value

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = self.construct_object(key_node, deep=True)
            if key in seen:
                raise FrontmatterError(f"duplicate frontmatter key: {key}")
            seen.add(key)
        mapping = super().construct_mapping(node, deep=True)
        # Check the finished mapping so YAML merge keys cannot bypass this rule.
        if any(type(key) is not str for key in mapping):
            raise FrontmatterError("frontmatter keys must be strings")
        return mapping


class _Timestamp(str):
    """Retain a native YAML timestamp's type, spelling, offset and precision."""


_Loader.add_constructor("tag:yaml.org,2002:timestamp",
                        lambda loader, node: _Timestamp(loader.construct_scalar(node)))


def _parse_block(block: str) -> dict:
    lines = block.splitlines()
    code = None
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not re.match(r"^code\s*:", line):
            continue
        if code is not None:
            raise _SelectorError("duplicate code frontmatter key")
        inline = line.partition(":")[2].strip()
        if inline:
            code = _scalar(inline)
            continue
        start = index
        while index < len(lines) and (
            not lines[index].strip() or lines[index].startswith((" ", "\t"))
        ):
            index += 1
        code = _nested_code(lines[start:index])
        # Selectors have a narrower legacy grammar with opaque literal values.
        lines[start:index] = [""] * (index - start)
    loader = _Loader("\n".join(lines))
    try:
        node = loader.get_single_node()
        if node is None:
            return {}
        if not isinstance(node, yaml.MappingNode):
            raise FrontmatterError("frontmatter must be a mapping")
        # Preserve the established string fields, including numeric titles and tags.
        strings = {"title", "type", "description", "status", "timestamp",
                   "resource", "superseded_by"}
        for key, value in node.value:
            if key.value in strings and isinstance(value, yaml.ScalarNode):
                if value.tag in _SCALAR_TAGS:
                    value.tag = "tag:yaml.org,2002:str"
                    # Legacy emitters wrote plain text such as "Incident #42".
                    # Quoted scalars and extension fields retain YAML comment semantics.
                    if (not value.style and key.start_mark.column == 0
                            and key.start_mark.line == value.start_mark.line
                            and value.start_mark.line == value.end_mark.line):
                        line = lines[value.start_mark.line]
                        raw = line[value.start_mark.column:].rstrip()
                        if re.search(r"[ \t]#", raw) and not _needs_quote(raw):
                            value.value = raw
            if key.value == "tags" and isinstance(value, yaml.SequenceNode):
                for item in value.value:
                    if isinstance(item, yaml.ScalarNode) and item.tag in _SCALAR_TAGS:
                        item.tag = "tag:yaml.org,2002:str"
        meta = loader.construct_object(node, deep=True)
    finally:
        loader.dispose()
    if code is not None:
        meta["code"] = code
    return meta


def split(content: str, *, strict_code: bool = False,
          strict_selectors: bool = False) -> tuple[dict, str]:
    """Read YAML, optionally requiring valid selectors or a fully writable block."""
    match = _FM.match(content)
    if not match:
        return {}, content
    block = match.group(1)
    try:
        meta = _parse_block(block)
    except (yaml.YAMLError, ValueError, TypeError, RecursionError) as exc:
        if strict_code or (strict_selectors and isinstance(exc, _SelectorError)):
            if isinstance(exc, FrontmatterError):
                raise
            raise FrontmatterError("invalid YAML frontmatter") from exc
        # Keep the original bytes when only the legacy scalar view is readable.
        values = {}
        for line in block.splitlines():
            if line and not line[0].isspace() and ":" in line:
                key, _, value = line.partition(":")
                values[key.strip()] = _scalar(value.strip())
        meta = _OpaqueMeta(values, content[:match.end()])
    return meta, content[match.end():]


def _render_scalar(value: object) -> str:
    if type(value) is not str:
        raise TypeError("frontmatter values must be strings, lists, or mappings")
    if _needs_quote(value):
        return json.dumps(value, ensure_ascii=False)
    return value


def _render_code(value: object) -> list[str]:
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


class _Dumper(getattr(yaml, "CSafeDumper", yaml.SafeDumper)):
    """Keep scalar lists compact and preserve the canonical double-quoted style."""


def _dump_string(dumper, value):
    style = '"' if _needs_quote(value) else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


def _dump_list(dumper, values):
    return dumper.represent_sequence("tag:yaml.org,2002:seq", values,
                                     flow_style=all(isinstance(item, str) for item in values))


_Dumper.add_representer(_Timestamp, lambda dumper, value:
                        dumper.represent_scalar("tag:yaml.org,2002:timestamp", str(value)))
_Dumper.add_representer(str, _dump_string)
_Dumper.add_representer(list, _dump_list)


def render(meta: dict) -> str:
    """Emit a mapping in stable order using the existing code-selector grammar."""
    if isinstance(meta, _OpaqueMeta):
        return meta.raw_frontmatter
    order = [
        "type", "title", "description", "resource", "tags", "status",
        "code", "timestamp",
    ]
    keys = [k for k in order if k in meta] + [k for k in meta if k not in order]
    lines = ["---"]
    for key in keys:
        if key == "code":
            lines.extend(_render_code(meta[key]))
        else:
            value = meta[key]
            if key == "timestamp" and isinstance(value, str) and re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value
            ):
                value = _Timestamp(value)
            lines.append(yaml.dump({key: value}, Dumper=_Dumper, allow_unicode=True,
                                   sort_keys=False, width=2**31-1).rstrip("\n"))
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
