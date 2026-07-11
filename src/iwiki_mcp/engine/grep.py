"""Lexical search over a domain's .md pages: section-level term-frequency
scoring. Complements vector search by catching exact symbol/identifier matches
that embeddings blur. Returns the same section-shaped hits for merging."""
from __future__ import annotations

from pathlib import Path
import re

from .okf_artifacts import RESERVED_OKF

_H2 = re.compile(r"^##\s+(.*?)\s*$", re.MULTILINE)


def _terms(query: str) -> list[str]:
    return [t.lower() for t in re.findall(r"\w+", query) if len(t) > 2]


def grep_sections(domain_dir: str, query: str, top_k: int) -> list[dict]:
    if top_k <= 0:
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
        rel = rel_path.as_posix()
        ms = list(_H2.finditer(content))
        for i, m in enumerate(ms):
            heading = m.group(1).strip()
            end = ms[i + 1].start() if i + 1 < len(ms) else len(content)
            hay = (heading + " " + content[m.end():end]).lower()
            score = sum(hay.count(t) for t in terms)
            if score > 0:
                out.append({"file": rel, "heading": heading, "chunk": 0,
                            "score": score, "hit": "lexical"})
    out.sort(key=lambda h: (-h["score"], h["file"], h["heading"]))
    return out[:top_k]
