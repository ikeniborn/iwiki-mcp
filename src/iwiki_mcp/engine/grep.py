"""Lexical search over a domain's .md pages: section-level term-frequency
scoring. Complements vector search by catching exact symbol/identifier matches
that embeddings blur. Returns the same section-shaped hits for merging."""
from __future__ import annotations

import glob
import os
import re

_H2 = re.compile(r"^##\s+(.*?)\s*$", re.MULTILINE)


def _terms(query: str) -> list[str]:
    return [t.lower() for t in re.findall(r"\w+", query) if len(t) > 2]


def grep_sections(domain_dir: str, query: str, top_k: int) -> list[dict]:
    terms = _terms(query)
    if not terms:
        return []
    out: list[dict] = []
    for md in glob.glob(os.path.join(domain_dir, "**", "*.md"), recursive=True):
        if "/.iwiki/" in md:
            continue
        try:
            content = open(md, encoding="utf-8").read()
        except OSError:
            continue
        rel = os.path.relpath(md, domain_dir)
        ms = list(_H2.finditer(content))
        for i, m in enumerate(ms):
            heading = m.group(1).strip()
            end = ms[i + 1].start() if i + 1 < len(ms) else len(content)
            hay = (heading + " " + content[m.end():end]).lower()
            score = sum(hay.count(t) for t in terms)
            if score > 0:
                out.append({"file": rel, "heading": heading, "chunk": 0,
                            "score": score, "hit": "lexical"})
    out.sort(key=lambda d: d["score"], reverse=True)
    return out[:top_k]
