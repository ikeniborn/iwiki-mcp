"""Broad multi-signal retrieval followed by deterministic rank fusion."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath

import numpy as np

from .base import domain_dir, index_path, migrate_store_location
from .engine import fusion, hier
from .engine.chunk import chunk_markdown
from .engine.config import Config
from .engine.embed import embed_texts
from .engine.grep import grep_sections
from .engine.store import VectorStore

CANDIDATE_LIMIT = 32
_VALID_MODES = {"hybrid", "semantic", "lexical"}


def _candidate_limit(top_k: int) -> int:
    return max(top_k, CANDIDATE_LIMIT)


def _domain_file_path(base: str, domain: str, file: str) -> Path | None:
    if not isinstance(file, str) or not file or "\\" in file:
        return None
    parts = file.split("/")
    posix_path = PurePosixPath(file)
    windows_path = PureWindowsPath(file)
    if (posix_path.is_absolute() or windows_path.is_absolute()
            or windows_path.drive or any(part in ("", ".", "..") for part in parts)):
        return None
    root = Path(domain_dir(base, domain)).resolve()
    try:
        path = root.joinpath(*parts).resolve()
        path.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return path


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


def _internal_hit(domain, rec, source, rank_key, seed_origins=None) -> dict:
    return {
        "domain": domain,
        "file": rec.file,
        "heading": rec.heading,
        "chunk": rec.chunk,
        "score": 0,
        "hit": "semantic",
        "source": source,
        "ordinal": rec.ordinal,
        "rank_key": rank_key,
        "seed_origins": list(seed_origins or []),
    }


def _domain_signals(cfg: Config, base: str, domain: str, query: str,
                    query_vec: list[float] | None, mode: str, limit: int,
                    threshold: float, type: str | None,
                    tags: list | None) -> dict[str, list[dict]]:
    migrate_store_location(base, domain)
    records = VectorStore(index_path(base, domain)).load()
    records = [
        rec for rec in records
        if _domain_file_path(base, domain, rec.file) is not None
        and _facet_ok(rec.type, rec.tags, type, tags)
        and (query_vec is None or rec.dim == len(query_vec))
    ]
    summaries = [rec for rec in records if rec.kind == "summary"]
    sections = [rec for rec in records if rec.kind == "section"]
    sections_by_file: dict[str, list] = {}
    for rec in sorted(sections, key=lambda item: (
            item.file, item.ordinal, item.chunk, item.heading)):
        sections_by_file.setdefault(rec.file, []).append(rec)

    semantic_seeds: list[tuple[str, float]] = []
    semantic_chunks: list[tuple[object, float]] = []
    if mode in ("semantic", "hybrid"):
        scored_summaries = [
            (rec, round(hier.sim(query_vec, rec), 4)) for rec in summaries
        ]
        scored_summaries = [
            item for item in scored_summaries if item[1] >= cfg.seed_threshold
        ]
        scored_summaries.sort(key=lambda item: (
            -item[1], item[0].file, item[0].ordinal, item[0].chunk))
        semantic_seeds = [
            (rec.file, score) for rec, score in scored_summaries[:cfg.seed_top_k]
        ]

        semantic_chunks = [
            (rec, round(hier.sim(query_vec, rec), 4)) for rec in sections
        ]
        semantic_chunks = [item for item in semantic_chunks if item[1] >= threshold]
        semantic_chunks.sort(key=lambda item: (
            -item[1], item[0].file, item[0].ordinal, item[0].chunk))
        semantic_chunks = semantic_chunks[:limit]

    lexical_hits: list[dict] = []
    lexical_seeds: list[tuple[str, int]] = []
    if mode in ("lexical", "hybrid"):
        eligible_files = set(sections_by_file)
        lexical_map = {
            (rec.file, rec.heading): rec
            for rec in sections
            if rec.chunk == 0
        }
        lexical_hits = [
            hit for hit in grep_sections(domain_dir(base, domain), query, None)
            if hit["file"] in eligible_files
            and (hit["file"], hit["heading"]) in lexical_map
        ]
        page_scores: dict[str, int] = {}
        for hit in lexical_hits:
            page_scores[hit["file"]] = page_scores.get(hit["file"], 0) + hit["score"]
        ranked_pages = sorted(page_scores.items(), key=lambda item: (-item[1], item[0]))
        lexical_seeds = ranked_pages[:cfg.seed_top_k]
    else:
        lexical_map = {}

    graph_seeds = [
        (file, "semantic", rank)
        for rank, (file, _) in enumerate(semantic_seeds)
    ] + [
        (file, "lexical", rank)
        for rank, (file, _) in enumerate(lexical_seeds)
    ]
    graph_pages = hier.rank_graph_pages(
        graph_seeds, domain_dir(base, domain), cfg.graph_depth, cfg.bfs_top_k
    ) if graph_seeds else []

    signals: dict[str, list[dict]] = {
        "semantic_page": [],
        "lexical_page": [],
        "graph_page": [],
        "semantic_chunk": [],
        "lexical_section": [],
    }
    for page_rank, (file, _) in enumerate(semantic_seeds):
        for rec in sections_by_file.get(file, []):
            rank_key = (page_rank, rec.ordinal, rec.chunk, rec.file)
            signals["semantic_page"].append(
                _internal_hit(domain, rec, "seed", rank_key, ["semantic"])
            )
    for page_rank, (file, _) in enumerate(lexical_seeds):
        for rec in sections_by_file.get(file, []):
            rank_key = (page_rank, rec.ordinal, rec.chunk, rec.file)
            signals["lexical_page"].append(
                _internal_hit(domain, rec, "seed", rank_key, ["lexical"])
            )
    for page_rank, page in enumerate(graph_pages):
        for rec in sections_by_file.get(page["file"], []):
            rank_key = (page_rank, rec.ordinal, rec.chunk, rec.file)
            signals["graph_page"].append(
                _internal_hit(
                    domain, rec, page["source"], rank_key, page["seed_origins"]
                )
            )
    for rec, score in semantic_chunks:
        signals["semantic_chunk"].append(
            _internal_hit(
                domain, rec, "global", (-score, rec.file, rec.ordinal, rec.chunk),
                ["semantic"],
            )
        )
    for rank, hit in enumerate(lexical_hits):
        rec = lexical_map[(hit["file"], hit["heading"])]
        signals["lexical_section"].append(
            _internal_hit(
                domain, rec, "lexical", (rank, rec.file, rec.ordinal, rec.chunk),
                ["lexical"],
            )
        )
    return {name: hits for name, hits in signals.items() if hits}


def prepare_read_candidates(cfg: Config, base: str, domains: list[str], query: str,
                            top_k: int, threshold: float, mode: str = "hybrid",
                            type: str | None = None,
                            tags: list | None = None) -> list[dict]:
    if mode not in _VALID_MODES:
        allowed = ", ".join(sorted(_VALID_MODES))
        raise ValueError(f"invalid search mode: {mode}; allowed values: {allowed}")
    if top_k <= 0 or not domains:
        return []

    query_vec = None
    if mode in ("semantic", "hybrid"):
        query_vec = list(np.asarray(embed_texts(cfg, [query])[0], dtype=np.float32))
    limit = _candidate_limit(top_k)
    signals: dict[str, list[dict]] = {}
    for domain in domains:
        domain_signals = _domain_signals(
            cfg, base, domain, query, query_vec, mode, limit, threshold, type, tags
        )
        for name, hits in domain_signals.items():
            signals.setdefault(name, []).extend(hits)
    for hits in signals.values():
        hits.sort(key=lambda hit: (
            hit["rank_key"], hit["domain"], hit["file"], hit["ordinal"], hit["chunk"]
        ))
        for hit in hits:
            hit.pop("rank_key")

    fused = fusion.fuse_ranked(signals, limit)
    public = []
    for candidate in fused:
        signal_names = set(candidate.pop("signals"))
        origins = set(candidate.get("seed_origins", []))
        semantic = bool(signal_names & {"semantic_page", "semantic_chunk"})
        lexical = bool(signal_names & {"lexical_page", "lexical_section"})
        if "graph_page" in signal_names:
            semantic = semantic or "semantic" in origins
            lexical = lexical or "lexical" in origins
        candidate["hit"] = (
            "both" if semantic and lexical else "semantic" if semantic else "lexical"
        )
        candidate.pop("ordinal", None)
        candidate.pop("seed_origins", None)
        public.append({
            key: candidate[key]
            for key in ("domain", "file", "heading", "chunk", "score", "hit", "source")
        })
    return public


def search_read(cfg: Config, base: str, domains: list[str], query: str,
                top_k: int, threshold: float, mode: str = "hybrid",
                type: str | None = None, tags: list | None = None) -> list[dict]:
    return prepare_read_candidates(
        cfg, base, domains, query, top_k, threshold, mode, type, tags
    )[:top_k]


def hydrate_candidates(cfg: Config, base: str, candidates: list[dict]) -> list[dict]:
    indexes: dict[str, dict[tuple[str, str, int], str]] = {}
    pages: dict[tuple[str, str], dict[tuple[str, int], tuple[str, str]] | None] = {}
    hydrated = []
    for candidate in candidates:
        domain = candidate["domain"]
        if domain not in indexes:
            migrate_store_location(base, domain)
            indexes[domain] = {
                (rec.file, rec.heading, rec.chunk): rec.hash
                for rec in VectorStore(index_path(base, domain)).load()
                if rec.kind == "section"
            }
        page_key = candidate["domain"], candidate["file"]
        if page_key not in pages:
            path = _domain_file_path(base, domain, candidate["file"])
            if path is None:
                pages[page_key] = None
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    markdown = fh.read()
            except OSError:
                pages[page_key] = None
            else:
                chunks = chunk_markdown(
                    candidate["file"], markdown, cfg.chunk_size, cfg.chunk_overlap,
                    cfg.summary_max,
                )
                pages[page_key] = {
                    (chunk.heading, chunk.chunk): (chunk.text, chunk.hash)
                    for chunk in chunks if chunk.kind == "section"
                }
        chunks_by_key = pages[page_key]
        chunk_key = candidate["heading"], candidate["chunk"]
        if chunks_by_key is None or chunk_key not in chunks_by_key:
            continue
        text, current_hash = chunks_by_key[chunk_key]
        indexed_hash = indexes[domain].get((candidate["file"], *chunk_key))
        if indexed_hash != current_hash:
            continue
        hydrated.append({**candidate, "text": text})
    return hydrated


def vector_search(cfg: Config, base: str, domains: list[str], query: str,
                  top_k: int, threshold: float,
                  type: str | None = None, tags: list | None = None) -> list[dict]:
    return search_read(
        cfg, base, domains, query, top_k, threshold, "semantic", type, tags
    )


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
    hits = []
    for domain in domains:
        for hit in grep_sections(domain_dir(base, domain), query, None):
            if type is not None or tags:
                record_type, record_tags = _hit_facets(base, domain, hit["file"])
                if not _facet_ok(record_type, record_tags, type, tags):
                    continue
            hits.append({"domain": domain, **hit, "source": "lexical"})
    hits.sort(key=lambda hit: (
        -hit["score"], hit["domain"], hit["file"], hit["heading"]
    ))
    return hits[:top_k]


def hybrid_search(cfg: Config, base: str, domains: list[str], query: str,
                  top_k: int, threshold: float, mode: str = "hybrid",
                  type: str | None = None, tags: list | None = None) -> list[dict]:
    return search_read(cfg, base, domains, query, top_k, threshold, mode, type, tags)
