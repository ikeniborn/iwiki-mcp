"""Multi-domain retrieval: numpy-merged vector search across the in-scope
domains' indices, plus a lexical (grep) path, combined into hybrid results.

Vector and lexical scores live on different scales, so hybrid ranks vector/both
hits first (by cosine), then lexical hits (by term-frequency), deduped by
(domain, file, heading).
"""
from __future__ import annotations

import numpy as np

from .base import domain_dir, index_path
from .engine.config import Config
from .engine.embed import embed_texts
from .engine.grep import grep_sections
from .engine.store import VectorStore, dequantize

_VALID_MODES = {"hybrid", "vector", "lexical"}


def vector_search(cfg: Config, base: str, domains: list[str], query: str,
                  top_k: int, threshold: float) -> list[dict]:
    if top_k <= 0 or not domains:
        return []
    qv = np.asarray(embed_texts(cfg, [query])[0], dtype=np.float32)
    qnorm = float(np.linalg.norm(qv)) or 1.0
    hits: list[dict] = []
    for d in domains:
        recs = [
            r for r in VectorStore(index_path(base, d)).load()
            if r.dim == qv.size
        ]
        if not recs:
            continue
        mat = np.asarray([dequantize(r.scale, r.q) for r in recs], dtype=np.float32)
        norms = np.linalg.norm(mat, axis=1)
        norms[norms == 0] = 1.0
        sims = (mat @ qv) / (norms * qnorm)
        for r, s in zip(recs, sims):
            if s >= threshold:
                hits.append({"domain": d, "file": r.file, "heading": r.heading,
                             "chunk": r.chunk, "score": round(float(s), 4),
                             "hit": "vector"})
    hits.sort(key=lambda h: (-h["score"], h["domain"], h["file"],
                             h["heading"], h["chunk"]))
    return hits[:top_k]


def lexical_search(base: str, domains: list[str], query: str,
                   top_k: int) -> list[dict]:
    if top_k <= 0:
        return []
    hits: list[dict] = []
    for d in domains:
        for h in grep_sections(domain_dir(base, d), query, top_k):
            hits.append({"domain": d, **h})
    hits.sort(key=lambda h: (-h["score"], h["domain"], h["file"], h["heading"]))
    return hits[:top_k]


def hybrid_search(cfg: Config, base: str, domains: list[str], query: str,
                  top_k: int, threshold: float, mode: str = "hybrid") -> list[dict]:
    if mode not in _VALID_MODES:
        raise ValueError(f"invalid search mode: {mode}")
    if top_k <= 0:
        return []
    vec = (vector_search(cfg, base, domains, query, top_k, threshold)
           if mode in ("hybrid", "vector") else [])
    lex = (lexical_search(base, domains, query, top_k)
           if mode in ("hybrid", "lexical") else [])
    merged: dict[tuple, dict] = {}
    for h in vec:
        key = (h["domain"], h["file"], h["heading"])
        if key not in merged or h["score"] > merged[key]["score"]:
            merged[key] = h
    for h in lex:
        key = (h["domain"], h["file"], h["heading"])
        if key in merged:
            merged[key]["hit"] = "both"
        else:
            merged[key] = h
    out = list(merged.values())
    out.sort(key=lambda h: (0 if h["hit"] in ("vector", "both") else 1,
                            -h["score"], h["domain"], h["file"], h["heading"]))
    return out[:top_k]
