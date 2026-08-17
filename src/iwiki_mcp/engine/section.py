"""Replace the body of a single ``##`` section in a markdown page — stdlib only,
no config/embedding call. Used by ``wiki_update_page`` to edit one section in place.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import frontmatter as _fm
from .links import slugify_heading

# Keep in sync with chunk._H2 / validate._H2 / lint._H2.
_H2 = re.compile(r"^##\s+(.*?)\s*$", re.MULTILINE)
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$", re.MULTILINE)


class SectionError(ValueError):
    """Raised when the target ``##`` section cannot be uniquely located."""


@dataclass(frozen=True)
class Section:
    """One `##` section: heading text plus its body span in the source."""

    heading: str
    body: str           # body content (starts after the newline following heading)
    start: int          # offset of the "## " line
    body_start: int     # offset of newline after heading (splice point for replace_section)
    body_end: int       # offset of the next "##" heading, or EOF


def list_sections(content: str) -> list[Section]:
    """Split ``content`` into ``##`` sections in document order."""
    heads = list(_H2.finditer(content))
    sections = []
    for i, m in enumerate(heads):
        # body_start points to the newline after the heading (where replace_section splices).
        # body content starts one position later (after the newline).
        body_start = m.end()
        body_content_start = (
            m.end() + 1
            if m.end() < len(content) and content[m.end()] == "\n"
            else m.end()
        )
        body_end = heads[i + 1].start() if i + 1 < len(heads) else len(content)
        sections.append(
            Section(
                heading=m.group(1).strip(),
                body=content[body_content_start:body_end],
                start=m.start(),
                body_start=body_start,
                body_end=body_end,
            )
        )
    return sections


def _locate(sections: list[Section], heading: str) -> int:
    target = heading.lstrip("#").strip()
    if not target:
        raise SectionError("empty heading")
    matches = [i for i, s in enumerate(sections) if s.heading == target]
    if not matches:
        raise SectionError(f"section '## {target}' not found")
    if len(matches) > 1:
        raise SectionError(
            f"section '## {target}' is ambiguous ({len(matches)} matches)"
        )
    return matches[0]


def replace_section(
    content: str, heading: str, new_body: str, *, new_heading: str | None = None
) -> str:
    """Return ``content`` with the body of the ``## <heading>`` section replaced.

    ``heading`` is matched by its text (leading ``#``/whitespace stripped). The
    replaced span runs from the end of the heading line to the next ``##`` (or EOF).
    ``new_heading`` optionally renames the section and must not collide with any
    heading anchor. Raises ``SectionError`` if the heading is missing or ambiguous.
    """
    if _H2.search(new_body):
        raise SectionError("new_body must not contain a ## heading")
    sections = list_sections(content)
    idx = _locate(sections, heading)
    target = sections[idx].heading
    replacement_heading = target if new_heading is None else new_heading.strip()
    replacement_anchor = slugify_heading(replacement_heading)
    if not replacement_anchor:
        raise SectionError("empty normalized heading")
    for candidate in _HEADING.finditer(content):
        candidate_anchor = slugify_heading(candidate.group(1).strip())
        if (
            candidate.start() != sections[idx].start
            and candidate_anchor == replacement_anchor
        ):
            raise SectionError(
                f"section heading '{replacement_heading}' collides with another anchor"
            )
    heads = list(_H2.finditer(content))
    heading_start = heads[idx].start(1)
    heading_end = heads[idx].end(1)
    renamed = content[:heading_start] + replacement_heading + content[heading_end:]
    shift = len(replacement_heading) - (heading_end - heading_start)
    body_start = sections[idx].body_start + shift
    body_end = sections[idx].body_end + shift
    return renamed[:body_start] + "\n" + new_body.strip("\n") + "\n\n" + renamed[body_end:]


def _anchor_collision(content: str, exclude_start: int, anchor: str) -> bool:
    return any(
        candidate.start() != exclude_start
        and slugify_heading(candidate.group(1).strip()) == anchor
        for candidate in _HEADING.finditer(content)
    )


def _anchor_point(content: str, *, after: str | None, before: str | None) -> int:
    """Return the insertion offset for `after`/`before`, or EOF for neither."""
    if after is not None and before is not None:
        raise SectionError("cannot set both after and before")
    sections = list_sections(content)
    if after is not None:
        idx = _locate(sections, after)
        return sections[idx].body_end
    if before is not None:
        idx = _locate(sections, before)
        return sections[idx].start
    return len(content)


def insert_section(
    content: str, heading: str, body: str, *,
    after: str | None = None, before: str | None = None,
) -> str:
    """Insert a new ``## heading`` section at the given anchor point."""
    target = heading.lstrip("#").strip()
    if not target:
        raise SectionError("empty heading")
    if _H2.search(body):
        raise SectionError("body must not contain a ## heading")
    anchor = slugify_heading(target)
    if not anchor:
        raise SectionError("empty normalized heading")
    if _anchor_collision(content, -1, anchor):
        raise SectionError(f"section heading '{target}' collides with another anchor")
    point = _anchor_point(content, after=after, before=before)
    block = f"## {target}\n{body.strip(chr(10))}\n\n"
    prefix = content[:point]
    if prefix and not prefix.endswith("\n\n"):
        prefix = prefix.rstrip("\n") + "\n\n"
    return prefix + block + content[point:]


def delete_section(content: str, heading: str) -> str:
    """Remove the ``## heading`` section entirely."""
    sections = list_sections(content)
    idx = _locate(sections, heading)
    target = sections[idx].heading
    if target.lower() in _fm.RESERVED_SECTIONS or target.lower() == _fm.OVERVIEW_HEADING:
        raise SectionError(f"cannot delete reserved section '## {target}'")
    if len(sections) <= 1:
        raise SectionError("cannot delete the last remaining section")
    return content[:sections[idx].start] + content[sections[idx].body_end:]


def move_section(
    content: str, heading: str, *,
    after: str | None = None, before: str | None = None,
) -> str:
    """Reorder the ``## heading`` section relative to ``after``/``before``."""
    if after is not None and before is not None:
        raise SectionError("cannot set both after and before")
    sections = list_sections(content)
    idx = _locate(sections, heading)
    target = sections[idx].heading
    if target.lower() in _fm.RESERVED_SECTIONS or target.lower() == _fm.OVERVIEW_HEADING:
        raise SectionError(f"cannot move reserved section '## {target}'")
    anchor_name = after if after is not None else before
    if anchor_name is not None and anchor_name.lstrip("#").strip() == target:
        raise SectionError("move target must not be the section itself")
    block = content[sections[idx].start:sections[idx].body_end]
    remainder = content[:sections[idx].start] + content[sections[idx].body_end:]
    point = _anchor_point(remainder, after=after, before=before)
    prefix = remainder[:point]
    if prefix and not prefix.endswith("\n\n"):
        prefix = prefix.rstrip("\n") + "\n\n"
    return prefix + block.rstrip("\n") + "\n\n" + remainder[point:]
