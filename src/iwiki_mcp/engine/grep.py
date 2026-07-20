"""Lexical search over a domain's .md pages: section-level term-frequency
scoring. Complements vector search by catching exact symbol/identifier matches
that embeddings blur. Returns the same section-shaped hits for merging."""
from __future__ import annotations

from pathlib import Path
import re

from .chunk import Chunk
from .okf_artifacts import RESERVED_OKF

_H2 = re.compile(r"^##\s+(.*?)\s*$", re.MULTILINE)


def _terms(query: str) -> list[str]:
    return [t.lower() for t in re.findall(r"\w+", query) if len(t) > 2]


def _score(terms: list[str], haystack: str) -> int:
    hay = haystack.lower()
    return sum(hay.count(term) for term in terms)


def _ordered(hits: list[dict], top_k: int | None) -> list[dict]:
    hits.sort(
        key=lambda hit: (
            -hit["score"],
            hit["file"],
            hit["heading"],
            hit["chunk"],
        )
    )
    return hits if top_k is None else hits[:top_k]


def score_sections(file: str, content: str, query: str) -> list[dict]:
    terms = _terms(query)
    if not terms:
        return []
    out: list[dict] = []
    matches = list(_H2.finditer(content))
    for index, match in enumerate(matches):
        heading = match.group(1).strip()
        end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(content)
        )
        score = _score(terms, heading + " " + content[match.end():end])
        if score > 0:
            out.append(
                {
                    "file": file,
                    "heading": heading,
                    "chunk": 0,
                    "score": score,
                    "hit": "lexical",
                }
            )
    return _ordered(out, None)


def score_chunks(
    chunks: list[Chunk], query: str, top_k: int | None
) -> list[dict]:
    if top_k is not None and top_k <= 0:
        return []
    terms = _terms(query)
    if not terms:
        return []
    out: list[dict] = []
    for chunk in chunks:
        if chunk.kind != "section":
            continue
        score = _score(terms, chunk.text)
        if score > 0:
            out.append(
                {
                    "file": chunk.file,
                    "heading": chunk.heading,
                    "chunk": chunk.chunk,
                    "score": score,
                    "hit": "lexical",
                }
            )
    return _ordered(out, top_k)


def grep_sections(domain_dir: str, query: str, top_k: int | None) -> list[dict]:
    if top_k is not None and top_k <= 0:
        return []
    terms = _terms(query)
    if not terms:
        return []
    root = Path(domain_dir)
    out: list[dict] = []
    for md in sorted(root.rglob("*.md")):
        rel_path = md.relative_to(root)
        if rel_path.as_posix() in RESERVED_OKF:
            continue
        try:
            content = md.read_text(encoding="utf-8")
        except OSError:
            continue
        out.extend(score_sections(rel_path.as_posix(), content, query))
    return _ordered(out, top_k)
