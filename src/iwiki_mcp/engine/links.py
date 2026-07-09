"""Parse [[target]] / [[target|alias]] wiki-links from markdown, ignoring code."""
from __future__ import annotations
import re

_LINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")
# Fenced code: ``` or ~~~ opener, lazily to a matching closer on its own line.
_FENCE = re.compile(r"^[ \t]*(```|~~~).*?^[ \t]*\1[ \t]*$", re.DOTALL | re.MULTILINE)
# Inline code spans: `...`
_INLINE = re.compile(r"`[^`]*`")


def slugify_heading(s: str) -> str:
    """Heading text -> GitHub-style anchor slug. Shared by the parser, the
    write-time rewriter, and lint so all three agree on the same anchor.
    Lowercase; drop non-word/space/hyphen chars; whitespace -> '-'; collapse
    repeated '-'. Deterministic and idempotent."""
    s = s.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def _strip_code(content: str) -> str:
    """Drop fenced code blocks and inline code spans so [[...]] inside code
    (e.g. bash `[[ $# -gt 0 ]]`) is not mistaken for a wiki-link."""
    content = _FENCE.sub("", content)
    content = _INLINE.sub("", content)
    return content


def parse_links(content: str) -> list[str]:
    """Return the target part of every [[...]] link, de-duplicated, order-preserving.
    Links inside Markdown code (fenced or inline) are ignored."""
    seen: dict[str, None] = {}
    for m in _LINK.finditer(_strip_code(content)):
        seen.setdefault(m.group(1).strip(), None)
    return list(seen)
