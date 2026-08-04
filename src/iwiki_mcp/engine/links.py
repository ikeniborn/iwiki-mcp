"""Parse wiki-page links from markdown — CommonMark relative links and legacy
[[target]] / [[target|alias]] — normalized to one 'slug#heading-slug' key,
ignoring code. Also rewrites [[...]] to markdown links (to_markdown_links)."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
import re
from typing import Literal
from urllib.parse import unquote, urlsplit

from .okf_artifacts import RESERVED_OKF

_LINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")
_FENCE_OPEN = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})([^\r\n]*)$")
_FENCE_CLOSE = re.compile(r"^[ \t]{0,3}(`+|~+)[ \t]*$")
# Inline markdown link [text](target); leading '!' (image) captured to reject it.
_MD_LINK = re.compile(r"(!?)\[[^\]]*\]\(([^)\s]+)\)")
# Full [[slug#Heading|Alias]] with optional #Heading and |Alias, for rewriting.
_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#([^\]|]+))?(?:\|([^\]]+))?\]\]")
_ATX_HEADING = re.compile(
    r"^[ \t]{0,3}#{1,6}(?:[ \t]+(.*)|[ \t]*)$", re.MULTILINE
)
_BAD_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


@dataclass(frozen=True)
class LinkTarget:
    """Normalized immutable link used by graph and lint internals."""

    source_domain: str
    target_domain: str
    target_page: str
    target_anchor: str
    raw_target: str
    kind: Literal["intra", "cross"]
    is_reserved: bool


@dataclass(frozen=True)
class HeadingAnchor:
    """One normalized heading anchor and its earliest authored label."""

    anchor: str
    heading: str


@dataclass(frozen=True)
class CrossDomainRewrite:
    target_domain: str
    old_page: str
    new_page: str
    old_anchor: str | None = None
    new_anchor: str | None = None


def slugify_heading(s: str) -> str:
    """Heading text -> GitHub-style anchor slug (github-slugger algorithm).
    Lowercase; drop every character that is not a word char, whitespace, or
    hyphen; replace each whitespace character with a hyphen. Repeated hyphens
    are NOT collapsed and leading/trailing hyphens are NOT stripped, matching
    GitHub exactly so a heading like `A - B` resolves to `#a---b` when the page
    renders there. Deterministic and idempotent on an already-slugified anchor.
    The single shared anchor helper for the parser, the write-time rewriter,
    and lint, so all three agree on the same anchor by construction."""
    s = s.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s", "-", s)
    return s


def _fenced_code_spans(content: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    opened: tuple[int, str, int] | None = None
    offset = 0
    for line in content.splitlines(keepends=True):
        text = line.rstrip("\r\n")
        if opened is None:
            match = _FENCE_OPEN.match(text)
            if match is not None:
                marker = match.group(1)
                if marker[0] != "`" or "`" not in match.group(2):
                    opened = (offset, marker[0], len(marker))
        else:
            match = _FENCE_CLOSE.match(text)
            if match is not None:
                marker = match.group(1)
                if marker[0] == opened[1] and len(marker) >= opened[2]:
                    spans.append((opened[0], offset + len(line)))
                    opened = None
        offset += len(line)
    if opened is not None:
        spans.append((opened[0], len(content)))
    return spans


def _inline_code_spans(content: str, start: int, end: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cursor = start
    while cursor < end:
        opener = content.find("`", cursor, end)
        if opener < 0:
            break
        opener_end = opener
        while opener_end < end and content[opener_end] == "`":
            opener_end += 1
        width = opener_end - opener
        candidate = opener_end
        closer_end = -1
        while candidate < end:
            closer = content.find("`", candidate, end)
            if closer < 0:
                break
            run_end = closer
            while run_end < end and content[run_end] == "`":
                run_end += 1
            if run_end - closer == width:
                closer_end = run_end
                break
            candidate = run_end
        if closer_end < 0:
            cursor = opener_end
            continue
        spans.append((opener, closer_end))
        cursor = closer_end
    return spans


def _code_spans(content: str) -> list[tuple[int, int]]:
    fences = _fenced_code_spans(content)
    spans = list(fences)
    cursor = 0
    for start, end in fences:
        spans.extend(_inline_code_spans(content, cursor, start))
        cursor = end
    spans.extend(_inline_code_spans(content, cursor, len(content)))
    return sorted(spans)


def _blank_code_spans(content: str, spans: list[tuple[int, int]]) -> str:
    pieces: list[str] = []
    cursor = 0
    for start, end in spans:
        pieces.append(content[cursor:start])
        pieces.append(re.sub(r"[^\r\n]", " ", content[start:end]))
        cursor = end
    pieces.append(content[cursor:])
    return "".join(pieces)


def _strip_code(content: str) -> str:
    """Blank fenced and inline code while retaining line and byte positions."""
    return _blank_code_spans(content, _code_spans(content))


def _heading_code_projection(content: str) -> str:
    """Blank fences and multiline code, retaining inline heading code text."""
    fences = set(_fenced_code_spans(content))
    spans: list[tuple[int, int]] = []
    for span in _code_spans(content):
        code = content[span[0]:span[1]]
        if span in fences or "\n" in code or "\r" in code:
            spans.append(span)
    return _blank_code_spans(content, spans)


def _mask_code(content: str) -> tuple[str, list[str]]:
    masks: list[str] = []
    pieces: list[str] = []
    cursor = 0
    for start, end in _code_spans(content):
        pieces.append(content[cursor:start])
        masks.append(content[start:end])
        pieces.append(f"\x00{len(masks) - 1}\x00")
        cursor = end
    pieces.append(content[cursor:])
    return "".join(pieces), masks


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
    return f"{slug}#{slugify_heading(anchor)}" if anchor else slug


def _legacy_target_key(target: str) -> str | None:
    """A [[...]] target -> normalized 'slug' / 'slug#heading-slug' (heading slugified
    so legacy and markdown links collapse to the same key), or None for a bare
    same-page anchor ([[#Heading]], empty slug)."""
    slug, _, heading = target.strip().partition("#")
    slug = slug.strip()
    if slug.startswith("./"):
        slug = slug[2:]
    if slug.endswith(".md"):
        slug = slug[:-3]
    if not slug:
        return None
    heading = heading.strip()
    return f"{slug}#{slugify_heading(heading)}" if heading else slug


def _decode_path_segments(path: str) -> list[str] | None:
    """Decode a domain-relative page path without allowing path reinterpretation."""
    if not path or "\\" in path or _BAD_PERCENT_ESCAPE.search(path):
        return None
    raw_parts = path.split("/")
    if any(not part for part in raw_parts):
        return None
    parts = [unquote(part) for part in raw_parts]
    if any(
        part in ("", ".", "..") or "/" in part or "\\" in part
        for part in parts
    ):
        return None
    if parts[0].startswith(".") or re.match(r"^[A-Za-z]:", parts[0]):
        return None
    return parts


def _page_id(path: str, *, strip_dot_slash: bool = False) -> str | None:
    if strip_dot_slash and path.startswith("./"):
        path = path[2:]
    if path.startswith("/"):
        return None
    parts = _decode_path_segments(path)
    if parts is None:
        return None
    page = "/".join(parts)
    if page.endswith(".md"):
        page = page[:-3]
    return page or None


def _is_reserved_page(page: str) -> bool:
    return f"{page}.md" in RESERVED_OKF


def _valid_domain(domain: str) -> bool:
    if not domain or domain.startswith(".") or "/" in domain or "\\" in domain:
        return False
    win_path = PureWindowsPath(domain)
    return (
        not Path(domain).is_absolute()
        and not win_path.is_absolute()
        and not win_path.drive
    )


def _structured_intra_target(
    raw_target: str, source_domain: str, *, markdown: bool
) -> LinkTarget | None:
    authored_target = raw_target
    working_target = raw_target.strip()
    if not working_target:
        return None
    if markdown:
        try:
            parsed = urlsplit(working_target)
        except ValueError:
            return None
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.query
            or "?" in working_target.partition("#")[0]
        ):
            return None
        if not parsed.path.endswith(".md"):
            return None
        path = parsed.path
        fragment = unquote(parsed.fragment)
    else:
        path, _, fragment = working_target.partition("#")
        try:
            if urlsplit(path).scheme:
                return None
        except ValueError:
            return None
    page = _page_id(path.strip(), strip_dot_slash=True)
    if page is None:
        return None
    anchor = slugify_heading(fragment.strip()) if fragment.strip() else ""
    return LinkTarget(
        source_domain=source_domain,
        target_domain=source_domain,
        target_page=page,
        target_anchor=anchor,
        raw_target=authored_target,
        kind="intra",
        is_reserved=_is_reserved_page(page),
    )


def _structured_cross_target(raw_target: str, source_domain: str) -> LinkTarget | None:
    try:
        parsed = urlsplit(raw_target)
    except ValueError:
        return None
    if parsed.scheme.lower() != "iwiki":
        return None
    query_part = raw_target.partition("#")[0]
    if parsed.query or "?" in query_part or not parsed.netloc:
        return None
    if "@" in parsed.netloc or ":" in parsed.netloc:
        return None
    if _BAD_PERCENT_ESCAPE.search(parsed.netloc):
        return None
    target_domain = unquote(parsed.netloc)
    if (
        not target_domain
        or target_domain.startswith(".")
        or target_domain in (".", "..")
        or any(char in target_domain for char in "/\\@:")
    ):
        return None
    if not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return None
    page = _page_id(parsed.path[1:])
    if page is None:
        return None
    fragment = unquote(parsed.fragment)
    anchor = slugify_heading(fragment) if fragment else ""
    return LinkTarget(
        source_domain=source_domain,
        target_domain=target_domain,
        target_page=page,
        target_anchor=anchor,
        raw_target=raw_target,
        kind="cross",
        is_reserved=_is_reserved_page(page),
    )


def parse_link_targets(content: str, source_domain: str) -> list[LinkTarget]:
    """Return safe normalized intra/cross-domain targets outside Markdown code.

    Duplicate normalized targets retain the lexicographically smallest authored
    target. Result order follows the first occurrence of each normalized edge.
    """
    if not _valid_domain(source_domain):
        return []
    stripped = _strip_code(content)
    hits: list[tuple[int, LinkTarget]] = []
    for match in _MD_LINK.finditer(stripped):
        if match.group(1):
            continue
        raw_target = match.group(2)
        try:
            scheme = urlsplit(raw_target).scheme.lower()
        except ValueError:
            continue
        if scheme == "iwiki":
            target = _structured_cross_target(raw_target, source_domain)
        elif scheme:
            target = None
        else:
            target = _structured_intra_target(
                raw_target, source_domain, markdown=True
            )
        if target is not None:
            hits.append((match.start(), target))
    for match in _LINK.finditer(stripped):
        target = _structured_intra_target(
            match.group(1), source_domain, markdown=False
        )
        if target is not None:
            hits.append((match.start(), target))

    targets: list[LinkTarget] = []
    positions: dict[tuple[str, str, str, str], int] = {}
    for _, target in sorted(hits, key=lambda hit: hit[0]):
        identity = (
            target.source_domain,
            target.target_domain,
            target.target_page,
            target.target_anchor,
        )
        position = positions.get(identity)
        if position is None:
            positions[identity] = len(targets)
            targets.append(target)
        elif target.raw_target < targets[position].raw_target:
            targets[position] = target
    return targets


def parse_heading_anchors(content: str) -> list[HeadingAnchor]:
    """Extract unique H1-H6 ATX anchors, keeping each earliest heading label."""
    without_code = _heading_code_projection(content)
    anchors: dict[str, HeadingAnchor] = {}
    for match in _ATX_HEADING.finditer(without_code):
        heading = (match.group(1) or "").strip()
        heading = re.sub(r"[ \t]+#+[ \t]*$", "", heading).rstrip()
        anchor = slugify_heading(heading)
        if anchor:
            anchors.setdefault(anchor, HeadingAnchor(anchor, heading))
    return list(anchors.values())


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
        key = _legacy_target_key(m.group(1))
        if key:
            hits.append((m.start(), key))
    seen: dict[str, None] = {}
    for _, key in sorted(hits, key=lambda t: t[0]):
        seen.setdefault(key, None)
    return list(seen)


def has_legacy_wikilink(content: str) -> bool:
    """True if content still contains a [[...]] link with a real slug outside
    code — the lazy-migration 'not yet edited' marker surfaced by lint. A bare
    same-page anchor ([[#Heading]], empty slug) is not counted: parse_links
    rejects it and to_markdown_links never rewrites it, so it must never keep a
    page from converging to a clean legacy_wikilink report."""
    stripped = _strip_code(content)
    return any(_legacy_target_key(m.group(1)) for m in _LINK.finditer(stripped))


def to_markdown_links(body: str) -> str:
    """Rewrite the four [[...]] forms to CommonMark relative links, leaving code
    (fenced + inline) and existing markdown links untouched. Idempotent: a body
    with no [[...]] is returned unchanged."""
    masked, masks = _mask_code(body)

    def _rewrite(m: re.Match) -> str:
        slug = m.group(1).strip()
        heading = (m.group(2) or "").strip()
        alias = (m.group(3) or "").strip()
        text = alias or heading or slug
        anchor = f"#{slugify_heading(heading)}" if heading else ""
        return f"[{text}]({slug}.md{anchor})"

    def _restore(m: re.Match) -> str:
        # Only our own sentinels index into `masks`; a stray \x00N\x00 already
        # in the body (never in real markdown) passes through, not IndexError.
        i = int(m.group(1))
        return masks[i] if i < len(masks) else m.group(0)

    rewritten = _WIKILINK.sub(_rewrite, masked)
    return re.sub(r"\x00(\d+)\x00", _restore, rewritten)


def rewrite_link_targets(body: str, mapping: dict[str, str]) -> str:
    """Rewrite markdown ([t](slug.md#a)) and legacy ([[slug#H|a]]) link targets
    whose slug is a key of `mapping` to its mapped slug, leaving code and text
    untouched. Idempotent when nothing matches."""
    if not mapping:
        return body
    masked, masks = _mask_code(body)

    def _md(m: re.Match) -> str:
        if m.group(1):                      # image
            return m.group(0)
        target = m.group(2)
        path, sep, anchor = target.partition("#")
        clean = path[2:] if path.startswith("./") else path
        slug = clean[:-3] if clean.endswith(".md") else clean
        if slug in mapping:
            # Replace exactly the captured target span, not a naive substring
            # replace: when the link text equals the target ([alpha.md](alpha.md))
            # `.replace(target, ...)` (with or without count=1) touches the wrong
            # occurrence or both -- slicing on the group's own span always hits
            # only the href, leaving visible link text untouched.
            new_target = f"{mapping[slug]}.md{sep}{anchor}"
            start, end = m.start(2) - m.start(), m.end(2) - m.start()
            return m.group(0)[:start] + new_target + m.group(0)[end:]
        return m.group(0)

    def _legacy(m: re.Match) -> str:
        inner = m.group(1)
        slug, sep, rest = inner.partition("#")
        s = slug.strip()
        base = s[:-3] if s.endswith(".md") else s
        if base in mapping:
            return m.group(0).replace(inner, f"{mapping[base]}{sep}{rest}", 1)
        return m.group(0)

    out = _MD_LINK.sub(_md, masked)
    out = _LINK.sub(_legacy, out)

    def _restore(m: re.Match) -> str:
        i = int(m.group(1))
        return masks[i] if i < len(masks) else m.group(0)

    return re.sub(r"\x00(\d+)\x00", _restore, out)


def _rewrite_markdown_targets(body: str, rewrite) -> tuple[str, int]:
    masked, masks = _mask_code(body)
    count = 0

    def replace(match: re.Match) -> str:
        nonlocal count
        if match.group(1):
            return match.group(0)
        replacement = rewrite(match.group(2))
        if replacement is None:
            return match.group(0)
        count += 1
        start = match.start(2) - match.start()
        end = match.end(2) - match.start()
        return match.group(0)[:start] + replacement + match.group(0)[end:]

    out = _MD_LINK.sub(replace, masked)

    def restore(match: re.Match) -> str:
        index = int(match.group(1))
        return masks[index] if index < len(masks) else match.group(0)

    return re.sub(r"\x00(\d+)\x00", restore, out), count


def rewrite_cross_domain_links(
    content: str, source_domain: str, rewrite: CrossDomainRewrite
) -> tuple[str, int]:
    """Rewrite exact parsed ``iwiki://`` hrefs while preserving link text/code."""
    if not _valid_domain(source_domain):
        return content, 0
    old_anchor = slugify_heading(rewrite.old_anchor) if rewrite.old_anchor else None

    def replace(raw_target: str) -> str | None:
        target = _structured_cross_target(raw_target, source_domain)
        if target is None or target.target_domain != rewrite.target_domain:
            return None
        if target.target_page != rewrite.old_page:
            return None
        if old_anchor is not None and target.target_anchor != old_anchor:
            return None
        parsed = urlsplit(raw_target)
        suffix = ".md" if parsed.path.endswith(".md") else ""
        anchor = (
            slugify_heading(rewrite.new_anchor)
            if rewrite.new_anchor is not None else parsed.fragment
        )
        return f"iwiki://{rewrite.target_domain}/{rewrite.new_page}{suffix}" + (
            f"#{anchor}" if anchor else ""
        )

    return _rewrite_markdown_targets(content, replace)


def rewrite_relative_anchors(
    content: str, old_anchor: str, new_anchor: str
) -> tuple[str, int]:
    """Rewrite matching relative Markdown fragments, preserving link text/code."""
    old = slugify_heading(old_anchor)
    new = slugify_heading(new_anchor)
    if not old or not new or old == new:
        return content, 0

    def replace(raw_target: str) -> str | None:
        try:
            parsed = urlsplit(raw_target)
        except ValueError:
            return None
        if parsed.scheme or parsed.netloc or parsed.query or not parsed.fragment:
            return None
        if slugify_heading(unquote(parsed.fragment)) != old:
            return None
        return f"{parsed.path}#{new}"

    return _rewrite_markdown_targets(content, replace)
