"""Deterministic, network-free eval for the hierarchical retrieval flow.
Builds in-memory summary/section records from a fixture vault, runs
seed->graph->section, and reports article/section recall + MRR."""
from __future__ import annotations

import os
import tempfile

from iwiki_mcp.engine import chunk as _chunk
from iwiki_mcp.engine import hier, store


def _records(vault, embed_fn):
    summ, secs = [], []
    for file, md in vault.items():
        for c in _chunk.chunk_markdown(file, md, 512, 64):
            rec = store.make_record(c, embed_fn(c.text))
            (summ if c.kind == "summary" else secs).append(rec)
    return summ, secs


def run_eval(vault, queries, embed_fn) -> dict:
    summ, secs = _records(vault, embed_fn)
    with tempfile.TemporaryDirectory() as d:
        for file, md in vault.items():
            with open(os.path.join(d, file), "w", encoding="utf-8") as fh:
                fh.write(md)
        art_hit = sec_hit = 0
        rr = 0.0
        for q in queries:
            qv = q["vec"]
            seeds = hier.seed_articles(qv, summ, 5, 0.0)
            pool = hier.expand_graph([f for f, _ in seeds], d, 1, 10)
            ranked = hier.rank_sections(qv, secs, pool, 8)
            if any(f in pool for f in q["articles"]):
                art_hit += 1
            headings = [h["heading"] for h in ranked]
            if any(s in headings for s in q["sections"]):
                sec_hit += 1
            for i, h in enumerate(ranked, 1):
                if h["heading"] in q["sections"]:
                    rr += 1.0 / i
                    break
        n = len(queries) or 1
        return {"article_recall": art_hit / n, "section_recall": sec_hit / n,
                "mrr": rr / n}
