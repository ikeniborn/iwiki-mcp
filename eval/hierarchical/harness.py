"""Deterministic, network-free eval for the hierarchical retrieval flow."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from iwiki_mcp import retrieval
from iwiki_mcp.engine import chunk as _chunk
from iwiki_mcp.engine import hier, store
from iwiki_mcp.engine.config import Config


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


def fake_rerank(ranked: list[tuple[str, str]],
                relevant: set[tuple[str, str]]):
    ordered = sorted(
        enumerate(ranked),
        key=lambda pair: (0 if pair[1] in relevant else 1, pair[0]),
    )
    return [item for _, item in ordered]


def _run_current_pipeline(vault, queries, embed_fn, top_k: int,
                          rerank_fn=None) -> dict:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        domain = root / "eval"
        domain.mkdir()
        summaries, sections = _records(vault, embed_fn)
        for file, markdown in vault.items():
            path = domain / file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(markdown, encoding="utf-8")
        store.VectorStore(str(domain / "index.jsonl")).save(summaries + sections)
        dimensions = len(embed_fn("dimension probe"))
        cfg = Config(
            base_url="http://offline.test/v1",
            api_key="offline",
            embed_model="offline",
            dimensions=dimensions,
            chunk_size=512,
            chunk_overlap=64,
            summary_max=400,
            top_k=top_k,
            score_threshold=0.0,
            graph_depth=1,
            ignore=None,
            seed_top_k=5,
            bfs_top_k=10,
            seed_threshold=0.0,
        )
        rankings = []
        with patch(
            "iwiki_mcp.retrieval.embed_texts",
            side_effect=lambda cfg, texts: [embed_fn(text) for text in texts],
        ):
            for query in queries:
                candidates = retrieval.prepare_read_candidates(
                    cfg, str(root), ["eval"], query["query"], top_k, 0.0, "hybrid"
                )
                ranked = [(hit["file"], hit["heading"]) for hit in candidates]
                if rerank_fn is not None:
                    ranked = rerank_fn(ranked, set(query["relevant"]))
                rankings.append(ranked[:top_k])
        return _metrics(rankings, queries)


def run_preliminary_eval(vault, queries, embed_fn, top_k: int = 3) -> dict:
    return _run_current_pipeline(vault, queries, embed_fn, top_k)


def run_fake_reranker_eval(vault, queries, embed_fn, top_k: int = 3) -> dict:
    return _run_current_pipeline(
        vault, queries, embed_fn, top_k, rerank_fn=fake_rerank
    )
