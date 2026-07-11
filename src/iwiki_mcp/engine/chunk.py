"""Split a page into one summary chunk plus clean per-section chunks.

The article summary is the frontmatter ``description``, emitted as a single
``kind="summary"`` chunk. Each content section then becomes its own ``kind="section"``
chunk (word-split with overlap when long), with no title/description prefix, so
section vectors carry only that section's own text. The reserved link sections
(``## Outgoing links`` / ``## External links``) and any ``## Overview`` are excluded
from the index; the summary lives only in ``description``.
"""
from __future__ import annotations
import hashlib
import re
from dataclasses import dataclass, field

from . import frontmatter as _fm

_H1 = re.compile(r"^#\s+(.*?)\s*$", re.MULTILINE)
_H2 = re.compile(r"^##\s+(.*?)\s*$", re.MULTILINE)


@dataclass
class Chunk:
    file: str
    heading: str
    chunk: int           # sub-chunk index within the section (0-based)
    text: str            # the text that gets embedded
    hash: str            # sha256(text)[:16]
    type: str | None = None
    tags: list = field(default_factory=list)
    kind: str = "section"
    ordinal: int = 0

    @property
    def id(self) -> str:
        return f"{self.file}#{self.heading}"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _split_section(words: list[str], size: int, overlap: int) -> list[list[str]]:
    if len(words) <= size:
        return [words]
    step = max(1, size - overlap)
    return [words[i:i + size] for i in range(0, len(words), step) if words[i:i + size]]


def _sections(content: str) -> list[tuple[str, str]]:
    """[(heading, body), ...] split on ``##``. Pre-``##`` content is ignored."""
    out: list[tuple[str, str]] = []
    ms = list(_H2.finditer(content))
    for i, m in enumerate(ms):
        start = m.end()
        end = ms[i + 1].start() if i + 1 < len(ms) else len(content)
        out.append((m.group(1).strip(), content[start:end].strip()))
    return out


def chunk_markdown(file: str, content: str, size: int, overlap: int,
                   summary_max: int = 400) -> list[Chunk]:
    """Return chunks for one markdown file.

    Emits at most one ``kind="summary"`` chunk (the full whitespace-collapsed
    frontmatter ``description``), followed by one ``kind="section"`` chunk per
    non-reserved, non-``## Overview`` section (word-split with overlap when long).
    Section chunk text is ``## {heading}\\n{body window}`` only — no title or
    description prefix, so section vectors carry only that section's own text.
    Reserved link sections and ``## Overview`` are dropped, never indexed.
    """
    meta, content = _fm.split(content)
    ptype = _fm.normalize_type(meta.get("type")) if meta.get("type") else None
    ptags = _fm.normalize_tags(meta.get("tags", [])) if meta.get("tags") else []
    out: list[Chunk] = []
    desc = meta.get("description", "")
    summary = " ".join(desc.split()) if isinstance(desc, str) else ""
    if summary:
        out.append(Chunk(file=file, heading="", chunk=0, text=summary,
                         hash=_hash(summary), type=ptype, tags=list(ptags),
                         kind="summary", ordinal=-1))
    # `## Overview` is never indexed: its text belongs in `description` now, and an
    # un-migrated Overview must not leak into the vectors. Excluded like the reserved
    # link sections (migration also strips it from the body).
    excluded = (*_fm.RESERVED_SECTIONS, _fm.OVERVIEW_HEADING)
    secs = [(h, b) for h, b in _sections(content) if h.lower() not in excluded]
    for ordinal, (heading, body) in enumerate(secs):
        prefix = f"## {heading}"
        for ci, piece in enumerate(_split_section(body.split(), size, overlap)):
            text = prefix + "\n" + " ".join(piece)
            out.append(Chunk(file=file, heading=heading, chunk=ci, text=text,
                             hash=_hash(text), type=ptype, tags=list(ptags),
                             kind="section", ordinal=ordinal))
    return out
