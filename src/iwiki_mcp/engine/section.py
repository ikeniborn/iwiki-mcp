"""Replace the body of a single ``##`` section in a markdown page — stdlib only,
no config/embedding call. Used by ``wiki_update_page`` to edit one section in place.
"""
from __future__ import annotations

import re

# Keep in sync with chunk._H2 / validate._H2 / lint._H2.
_H2 = re.compile(r"^##\s+(.*?)\s*$", re.MULTILINE)


class SectionError(ValueError):
    """Raised when the target ``##`` section cannot be uniquely located."""


def replace_section(content: str, heading: str, new_body: str) -> str:
    """Return ``content`` with the body of the ``## <heading>`` section replaced.

    ``heading`` is matched by its text (leading ``#``/whitespace stripped). The
    replaced span runs from the end of the heading line to the next ``##`` (or EOF);
    the heading line itself is preserved. Raises ``SectionError`` if the heading is
    missing or appears more than once.
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
    body_start = heads[idx].end()
    body_end = heads[idx + 1].start() if idx + 1 < len(heads) else len(content)
    return content[:body_start] + "\n" + new_body.strip("\n") + "\n\n" + content[body_end:]
