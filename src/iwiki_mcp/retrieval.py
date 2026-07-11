"""Multi-domain hierarchical retrieval: per domain, seed article summaries by
cosine similarity, expand the wiki-link graph from those seeds into a
candidate pool, then rank clean section vectors inside that pool. Plus a
lexical (grep) path, combined into hybrid results.

Vector and lexical scores live on different scales, so hybrid ranks vector/both
hits first (by cosine), then lexical hits (by term-frequency), deduped by
(domain, file, heading).
"""
from __future__ import annotations

import os

import numpy as np

from .base import domain_dir, index_path, migrate_store_location
from .engine.config import Config
from .engine.embed import embed_texts
from .engine.grep import grep_sections
from .engine.store import VectorStore
from .engine import hier

_VALID_MODES = {"hybrid", "vector", "lexical"}


def _facet_ok(rtype, rtags, want_type, want_tags) -> bool:
    if want_type is not None and rtype != want_type:
        return False
    if want_tags and not (set(want_tags) & set(rtags or [])):
        return False
    return True


def _hit_facets(base, domain, file):
    from .engine import frontmatter as fm
    path = os.path.join(domain_dir(base, domain), file)
    try:
        meta, _ = fm.split(open(path, encoding="utf-8").read())
    except OSError:
        return None, []
    return meta.get("type"), fm.normalize_tags(meta.get("tags", []) or [])


def _hier_vector(cfg: Config, base: str, domain: str, qv: list, top_k: int,
                 threshold: float, type: str | None, tags: list | None) -> list[dict]:
    recs = [r for r in VectorStore(index_path(base, domain)).load()
            if r.dim == len(qv) and _facet_ok(r.type, r.tags, type, tags)]
    summ = [r for r in recs if r.kind == "summary"]
    secs = [r for r in recs if r.kind == "section"]
    if not summ or not secs:
        return []
    seeds = hier.seed_articles(qv, summ, cfg.seed_top_k, cfg.seed_threshold)
    if not seeds:
        return []
    pool = hier.expand_graph([f for f, _ in seeds], domain_dir(base, domain),
                             cfg.graph_depth, cfg.bfs_top_k)
    ranked = hier.rank_sections(qv, secs, pool, top_k)
    return [{"domain": domain, "file": h["file"], "heading": h["heading"],
             "chunk": h["chunk"], "score": h["score"], "hit": "vector",
             "source": h["source"]} for h in ranked
            if h["score"] >= threshold]


def vector_search(cfg: Config, base: str, domains: list[str], query: str,
                  top_k: int, threshold: float,
                  type: str | None = None, tags: list | None = None) -> list[dict]:
    if top_k <= 0 or not domains:
        return []
    qv = list(np.asarray(embed_texts(cfg, [query])[0], dtype=np.float32))
    hits: list[dict] = []
    for d in domains:
        migrate_store_location(base, d)
        hits.extend(_hier_vector(cfg, base, d, qv, top_k, threshold, type, tags))
    hits.sort(key=lambda h: (-h["score"], h["domain"], h["file"], h["heading"]))
    return hits[:top_k]


def locate_target(cfg: Config, base: str, domain: str, query: str,
                  heading: str | None = None) -> dict:
    """Precise write-target locate: seed with the higher write_seed_threshold and,
    when a heading hint is given, keep only the exact (case-insensitive) heading
    match. Returns the single best hit with exists=True, else {exists: False}."""
    migrate_store_location(base, domain)
    qv = list(np.asarray(embed_texts(cfg, [query])[0], dtype=np.float32))
    recs = [r for r in VectorStore(index_path(base, domain)).load() if r.dim == len(qv)]
    summ = [r for r in recs if r.kind == "summary"]
    secs = [r for r in recs if r.kind == "section"]
    if not summ or not secs:
        return {"domain": domain, "exists": False}
    seeds = hier.seed_articles(qv, summ, cfg.seed_top_k, cfg.write_seed_threshold)
    if not seeds:
        return {"domain": domain, "exists": False}
    pool = hier.expand_graph([f for f, _ in seeds], domain_dir(base, domain),
                             cfg.graph_depth, cfg.bfs_top_k)
    if heading is not None:
        want = heading.strip().lower()
        secs = [r for r in secs if r.heading.lower() == want]
    ranked = hier.rank_sections(qv, secs, pool, cfg.top_k)
    if not ranked:
        return {"domain": domain, "exists": False}
    best = ranked[0]
    return {"domain": domain, "file": best["file"], "heading": best["heading"],
            "score": best["score"], "exists": True}


def lexical_search(base: str, domains: list[str], query: str, top_k: int,
                   type: str | None = None, tags: list | None = None) -> list[dict]:
    if top_k <= 0:
        return []
    hits: list[dict] = []
    for d in domains:
        for h in grep_sections(domain_dir(base, d), query, top_k * 3):
            if type is not None or tags:
                rt, rtags = _hit_facets(base, d, h["file"])
                if not _facet_ok(rt, rtags, type, tags):
                    continue
            hits.append({"domain": d, **h, "source": "lexical"})
    hits.sort(key=lambda h: (-h["score"], h["domain"], h["file"], h["heading"]))
    return hits[:top_k]


def hybrid_search(cfg: Config, base: str, domains: list[str], query: str,
                  top_k: int, threshold: float, mode: str = "hybrid",
                  type: str | None = None, tags: list | None = None) -> list[dict]:
    if mode not in _VALID_MODES:
        raise ValueError(f"invalid search mode: {mode}")
    if top_k <= 0:
        return []
    vec = (vector_search(cfg, base, domains, query, top_k, threshold, type, tags)
           if mode in ("hybrid", "vector") else [])
    lex = (lexical_search(base, domains, query, top_k, type, tags)
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
