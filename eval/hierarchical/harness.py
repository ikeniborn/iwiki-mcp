"""Deterministic, network-free eval for the hierarchical retrieval flow."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from iwiki_mcp.engine import chunk as _chunk
from iwiki_mcp.engine import hier, store


def _records(vault, embed_fn):
    summaries, sections = [], []
    for file, markdown in vault.items():
        for chunk in _chunk.chunk_markdown(file, markdown, 512, 64):
            record = store.make_record(chunk, embed_fn(chunk.text))
            (summaries if chunk.kind == "summary" else sections).append(record)
    return summaries, sections


def _metrics(rankings, queries):
    hits = 0
    reciprocal_rank = 0.0
    for ranking, query in zip(rankings, queries):
        relevant = set(query["relevant"])
        if any(identity in relevant for identity in ranking):
            hits += 1
        for rank, identity in enumerate(ranking, 1):
            if identity in relevant:
                reciprocal_rank += 1.0 / rank
                break
    count = len(queries) or 1
    return {"recall_at_k": hits / count, "mrr_at_k": reciprocal_rank / count}


@contextmanager
def _vault_dir(vault):
    with TemporaryDirectory() as directory:
        root = Path(directory)
        for file, markdown in vault.items():
            path = root / file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(markdown, encoding="utf-8")
        yield root


def run_baseline_eval(vault, queries, embed_fn, top_k=8) -> dict:
    summaries, sections = _records(vault, embed_fn)
    rankings = []
    with _vault_dir(vault) as directory:
        for query in queries:
            query_vector = embed_fn(query["query"])
            seeds = hier.seed_articles(query_vector, summaries, 5, 0.0)
            pool = hier.expand_graph([file for file, _ in seeds], str(directory), 1, 10)
            ranked = hier.rank_sections(query_vector, sections, pool, top_k)
            rankings.append([(hit["file"], hit["heading"]) for hit in ranked])
    return _metrics(rankings, queries)
