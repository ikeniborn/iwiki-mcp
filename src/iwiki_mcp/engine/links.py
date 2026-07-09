"""Parse wiki-page links from markdown — CommonMark relative links and legacy
[[target]] / [[target|alias]] — normalized to one 'slug#heading-slug' key,
ignoring code. Also rewrites [[...]] to markdown links (to_markdown_links)."""
from __future__ import annotations
import re

_LINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")
# Fenced code: ``` or ~~~ opener, lazily to a matching closer on its own line.
_FENCE = re.compile(r"^[ \t]*(```|~~~).*?^[ \t]*\1[ \t]*$", re.DOTALL | re.MULTILINE)
# Inline code spans: `...`
_INLINE = re.compile(r"`[^`]*`")
# Inline markdown link [text](target); leading '!' (image) captured to reject it.
_MD_LINK = re.compile(r"(!?)\[[^\]]*\]\(([^)\s]+)\)")


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


def _md_target_key(target: str) -> str | None:
    """A markdown link target -> normalized 'slug' / 'slug#anchor', or None if it
    is not a same-domain wiki-page edge (external / absolute / anchor / non-.md)."""
    if "://" in target or target.startswith(("mailto:", "/", "#")):
        return None
    path, _, anchor = target.partition("#")
    if path.startswith("./"):
        path = path[2:]
    if not path.endswith(".md"):
        return None
    slug = path[:-3]
    if not slug:
        return None
    return f"{slug}#{anchor}" if anchor else slug


def _legacy_target_key(target: str) -> str:
    """A [[...]] target -> normalized 'slug' / 'slug#heading-slug' (heading slugified
    so legacy and markdown links collapse to the same key)."""
    slug, _, heading = target.strip().partition("#")
    slug = slug.strip()
    if slug.startswith("./"):
        slug = slug[2:]
    if slug.endswith(".md"):
        slug = slug[:-3]
    heading = heading.strip()
    return f"{slug}#{slugify_heading(heading)}" if heading else slug


def parse_links(content: str) -> list[str]:
    """Return every wiki-page edge as a normalized 'slug' / 'slug#heading-slug'
    key, de-duplicated and ordered by document position. Reads both CommonMark
    relative links ([text](slug.md#anchor)) and legacy [[slug#Heading]]. Links
    inside Markdown code (fenced or inline) are ignored."""
    stripped = _strip_code(content)
    hits: list[tuple[int, str]] = []
    for m in _MD_LINK.finditer(stripped):
        if m.group(1):  # leading '!' -> image, not an edge
            continue
        key = _md_target_key(m.group(2))
        if key:
            hits.append((m.start(), key))
    for m in _LINK.finditer(stripped):
        hits.append((m.start(), _legacy_target_key(m.group(1))))
    seen: dict[str, None] = {}
    for _, key in sorted(hits, key=lambda t: t[0]):
        seen.setdefault(key, None)
    return list(seen)


def has_legacy_wikilink(content: str) -> bool:
    """True if content still contains a [[...]] link outside code — the
    lazy-migration 'not yet edited' marker surfaced by lint."""
    return bool(_LINK.search(_strip_code(content)))
