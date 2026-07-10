"""Hierarchical retrieval core (framework-free): article seed scoring, undirected
wiki-graph expansion into a candidate pool, then clean-section ranking inside it.
Ported for parity from obsidian-ai-wiki's page-similarity/query flow."""
from __future__ import annotations

import os

from .store import Record, dequantize, cosine
from .links import parse_links


def sim(query_vec: list[float], rec: Record) -> float:
    return cosine(query_vec, dequantize(rec.scale, rec.q))


def seed_articles(query_vec: list[float], summary_recs: list[Record],
                  top_k: int, threshold: float) -> list[tuple[str, float]]:
    scored = [(r.file, round(sim(query_vec, r), 4)) for r in summary_recs]
    scored = [(f, s) for f, s in scored if s >= threshold]
    scored.sort(key=lambda t: (-t[1], t[0]))
    return scored[:top_k]


def _adjacency(domain_dir: str) -> dict[str, set[str]]:
    """Undirected page graph keyed by '<slug>.md'. An edge a->b also adds b->a."""
    adj: dict[str, set[str]] = {}
    root = domain_dir
    for name in os.listdir(root) if os.path.isdir(root) else []:
        if not name.endswith(".md"):
            continue
        try:
            content = open(os.path.join(root, name), encoding="utf-8").read()
        except OSError:
            continue
        for link in parse_links(content):
            base = link.split("#", 1)[0]
            if not base:
                continue
            tgt = base if base.endswith(".md") else f"{base}.md"
            adj.setdefault(name, set()).add(tgt)
            adj.setdefault(tgt, set()).add(name)
    return adj


def expand_graph(seed_files: list[str], domain_dir: str, depth: int,
                 cap: int) -> dict[str, str]:
    pool: dict[str, str] = {f: "seed" for f in seed_files}
    adj = _adjacency(domain_dir)
    frontier = list(seed_files)
    graph_order: list[str] = []
    for _ in range(max(0, depth)):
        nxt: list[str] = []
        for f in frontier:
            for nb in sorted(adj.get(f, ())):
                if nb not in pool:
                    pool[nb] = "graph"
                    graph_order.append(nb)
                    nxt.append(nb)
        frontier = nxt
    if cap > 0 and len(graph_order) > cap:
        for f in graph_order[cap:]:
            del pool[f]
    return pool


def rank_sections(query_vec: list[float], section_recs: list[Record],
                  pool_files: dict[str, str], top_k: int) -> list[dict]:
    hits: list[dict] = []
    for r in section_recs:
        src = pool_files.get(r.file)
        if src is None:
            continue
        hits.append({"file": r.file, "heading": r.heading, "chunk": r.chunk,
                     "score": round(sim(query_vec, r), 4), "source": src,
                     "ordinal": r.ordinal})
    hits.sort(key=lambda h: (-h["score"], 0 if h["source"] == "seed" else 1,
                             h["file"], h["ordinal"], h["chunk"]))
    return hits[:top_k]
