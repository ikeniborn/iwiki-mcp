"""Replace the body of a single ``##`` section in a markdown page — stdlib only,
no config/embedding call. Used by ``wiki_update_page`` to edit one section in place.
"""
from __future__ import annotations

import re

from .links import slugify_heading

# Keep in sync with chunk._H2 / validate._H2 / lint._H2.
_H2 = re.compile(r"^##\s+(.*?)\s*$", re.MULTILINE)
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$", re.MULTILINE)


class SectionError(ValueError):
    """Raised when the target ``##`` section cannot be uniquely located."""


def replace_section(
    content: str, heading: str, new_body: str, *, new_heading: str | None = None
) -> str:
    """Return ``content`` with the body of the ``## <heading>`` section replaced.

    ``heading`` is matched by its text (leading ``#``/whitespace stripped). The
    replaced span runs from the end of the heading line to the next ``##`` (or EOF).
    ``new_heading`` optionally renames the section and must not collide with any
    heading anchor. Raises ``SectionError`` if the heading is missing or ambiguous.
    """
    target = heading.lstrip("#").strip()
    if not target:
        raise SectionError("empty heading")
    if _H2.search(new_body):
        raise SectionError("new_body must not contain a ## heading")
    heads = list(_H2.finditer(content))
    matches = [i for i, m in enumerate(heads) if m.group(1).strip() == target]
    if not matches:
        raise SectionError(f"section '## {target}' not found")
    if len(matches) > 1:
        raise SectionError(
            f"section '## {target}' is ambiguous ({len(matches)} matches)"
        )
    idx = matches[0]
    replacement_heading = target if new_heading is None else new_heading.strip()
    replacement_anchor = slugify_heading(replacement_heading)
    if not replacement_anchor:
        raise SectionError("empty normalized heading")
    for candidate in _HEADING.finditer(content):
        candidate_anchor = slugify_heading(candidate.group(1).strip())
        if (
            candidate.start() != heads[idx].start()
            and candidate_anchor == replacement_anchor
        ):
            raise SectionError(
                f"section heading '{replacement_heading}' collides with another anchor"
            )
    body_start = heads[idx].end()
    body_end = heads[idx + 1].start() if idx + 1 < len(heads) else len(content)
    heading_start = heads[idx].start(1)
    heading_end = heads[idx].end(1)
    renamed = content[:heading_start] + replacement_heading + content[heading_end:]
    shift = len(replacement_heading) - (heading_end - heading_start)
    body_start += shift
    body_end += shift
    return renamed[:body_start] + "\n" + new_body.strip("\n") + "\n\n" + renamed[body_end:]
