from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from pathlib import PureWindowsPath
from time import perf_counter

import numpy as np

from iwiki_mcp.base import index_path
from iwiki_mcp.engine.chunk import chunk_markdown
from iwiki_mcp.engine.config import Config
from iwiki_mcp.engine import fusion
from iwiki_mcp.engine import rerank
from iwiki_mcp.engine.store import VectorStore
from iwiki_mcp import retrieval

from .fixtures import BenchmarkCase
from .metrics import identity
from .metrics import latency_summary
from .metrics import source_mix


_PUBLIC_FIELDS = ("domain", "file", "heading", "chunk", "score", "hit", "source")


def _elapsed_ms(start: float) -> float:
    return (perf_counter() - start) * 1000


def _validate_domain(domain: str) -> str:
    if not domain:
        raise ValueError("invalid domain: empty")
    if domain.startswith("."):
        raise ValueError(f"invalid domain '{domain}'")
    if "/" in domain or "\\" in domain:
        raise ValueError(f"invalid domain '{domain}'")
    if domain in (".", ".."):
        raise ValueError(f"invalid domain '{domain}'")
    if Path(domain).is_absolute() or PureWindowsPath(domain).is_absolute():
        raise ValueError(f"invalid domain '{domain}'")
    if PureWindowsPath(domain).drive:
        raise ValueError(f"invalid domain '{domain}'")
    return domain


def _ensure_read_only_store_layout(base: str, domain: str) -> None:
    legacy_store_dir = Path(base) / domain / ".iwiki"
    if legacy_store_dir.exists():
        raise RuntimeError(
            "benchmark refuses legacy store layout for domain "
            f"'{domain}' because it would require migration writes"
        )


@contextmanager
def _without_store_migration():
    original = retrieval.migrate_store_location
    retrieval.migrate_store_location = lambda *args, **kwargs: None
    try:
        yield
    finally:
        retrieval.migrate_store_location = original


def _public_identity(candidate: dict) -> str:
    return identity({
        key: candidate[key]
        for key in ("domain", "file", "heading", "chunk")
    })


def _public_projection(candidate: dict) -> dict:
    return {key: candidate[key] for key in _PUBLIC_FIELDS}


def _ranked_signal_hit(candidate: dict) -> dict:
    return {
        key: candidate[key]
        for key in ("domain", "file", "heading", "chunk", "ordinal")
    }


def _record_identity(domain: str, record) -> str:
    return identity({
        "domain": domain,
        "file": record.file,
        "heading": record.heading,
        "chunk": record.chunk,
    })


def _parse_identity(value: str, domain: str) -> tuple[str, str, int] | None:
    prefix = f"{domain}/"
    if not isinstance(value, str) or not value.startswith(prefix):
        return None
    rest = value[len(prefix):]
    file_heading, separator, chunk_text = rest.rpartition(":")
    if not separator or "#" not in file_heading:
        return None
    file, heading = file_heading.split("#", 1)
    try:
        chunk_index = int(chunk_text)
    except ValueError:
        return None
    return file, heading, chunk_index


def _chunk_status(
    cfg: Config,
    base: str,
    domain: str,
    file: str,
    heading: str,
    chunk_index: int,
) -> tuple[bool, object | None]:
    path = retrieval._domain_file_path(base, domain, file)
    if path is None or not path.is_file():
        return False, None
    try:
        markdown = path.read_text(encoding="utf-8")
    except OSError:
        return False, None
    chunks = chunk_markdown(
        file,
        markdown,
        cfg.chunk_size,
        cfg.chunk_overlap,
        cfg.summary_max,
    )
    for chunk in chunks:
        if (
            chunk.kind == "section"
            and chunk.heading == heading
            and chunk.chunk == chunk_index
        ):
            return True, chunk
    return True, None


def _index_stage(
    cfg: Config,
    base: str,
    domain: str,
    case: BenchmarkCase,
    query_vec: list[float] | None,
) -> dict:
    records = VectorStore(index_path(base, domain)).load()
    sections = [record for record in records if record.kind == "section"]
    summaries = [record for record in records if record.kind == "summary"]
    records_by_identity = {
        _record_identity(domain, record): record
        for record in sections
    }
    relevant = {}
    for value in sorted(case.relevant):
        parts = _parse_identity(value, domain)
        if parts is None:
            relevant[value] = {
                "parseable": False,
                "file_exists": False,
                "chunk_present": False,
                "indexed": False,
                "hash_matches": None,
                "embedding_dim_matches": None,
                "eligible": False,
            }
            continue
        file, heading, chunk_index = parts
        file_exists, chunk = _chunk_status(
            cfg,
            base,
            domain,
            file,
            heading,
            chunk_index,
        )
        record = records_by_identity.get(value)
        indexed = record is not None
        hash_matches = (
            record.hash == chunk.hash
            if record is not None and chunk is not None
            else None
        )
        if record is not None and query_vec is not None:
            embedding_dim_matches = record.dim == len(query_vec)
        elif record is not None:
            embedding_dim_matches = True
        else:
            embedding_dim_matches = None
        relevant[value] = {
            "parseable": True,
            "file_exists": file_exists,
            "chunk_present": chunk is not None,
            "indexed": indexed,
            "hash_matches": hash_matches,
            "embedding_dim_matches": embedding_dim_matches,
            "eligible": (
                file_exists
                and indexed
                and embedding_dim_matches is not False
            ),
        }
    return {
        "record_count": len(records),
        "summary_count": len(summaries),
        "section_count": len(sections),
        "relevant": relevant,
    }


def _candidate_key(candidate: dict) -> tuple:
    return (
        candidate["domain"],
        candidate["file"],
        candidate["heading"],
        candidate["chunk"],
    )


def _project_fused_candidate(candidate: dict) -> dict:
    projected = dict(candidate)
    signal_names = set(projected.pop("signals", []))
    origins = set(projected.get("seed_origins", []))
    semantic = bool(signal_names & {"semantic_page", "semantic_chunk"})
    lexical = bool(signal_names & {"lexical_page", "lexical_section"})
    if "graph_page" in signal_names:
        semantic = semantic or "semantic" in origins
        lexical = lexical or "lexical" in origins
    projected["hit"] = (
        "both" if semantic and lexical else "semantic" if semantic else "lexical"
    )
    projected.pop("ordinal", None)
    projected.pop("seed_origins", None)
    return _public_projection(projected)


def trace_query(
    cfg: Config,
    base: str,
    case: BenchmarkCase,
    mode: str,
    rerank_enabled: bool,
) -> dict:
    if mode not in retrieval._VALID_MODES:
        allowed = ", ".join(sorted(retrieval._VALID_MODES))
        raise ValueError(f"invalid search mode: {mode}; allowed values: {allowed}")
    domain = _validate_domain(case.domain)
    _ensure_read_only_store_layout(base, domain)

    stage_ms: dict[str, float] = {}
    page_cache = {}
    limit = retrieval._candidate_limit(case.k)

    query_vec = None
    start = perf_counter()
    if mode in ("semantic", "hybrid"):
        query_vec = list(
            np.asarray(retrieval.embed_texts(cfg, [case.query])[0], dtype=np.float32)
        )
    stage_ms["embedding_ms"] = _elapsed_ms(start)

    start = perf_counter()
    index_stage = _index_stage(cfg, base, domain, case, query_vec)
    stage_ms["index_ms"] = _elapsed_ms(start)

    start = perf_counter()
    signals: dict[str, list[dict]] = {}
    with _without_store_migration():
        domain_signals = retrieval._domain_signals(
            cfg,
            base,
            domain,
            case.query,
            query_vec,
            mode,
            limit,
            cfg.score_threshold,
            None,
            None,
            page_cache,
        )
    for name, hits in domain_signals.items():
        signals.setdefault(name, []).extend(dict(hit) for hit in hits)
    for hits in signals.values():
        hits.sort(key=lambda hit: (
            hit["rank_key"],
            hit["domain"],
            hit["file"],
            hit["ordinal"],
            hit["chunk"],
        ))
        for hit in hits:
            hit.pop("rank_key", None)
    stage_ms["signals_ms"] = _elapsed_ms(start)
    signal_identities = {
        name: [_public_identity(hit) for hit in hits]
        for name, hits in sorted(signals.items())
    }
    ranked_signals = {
        name: [_ranked_signal_hit(hit) for hit in hits]
        for name, hits in signals.items()
    }
    signal_identity_sets = {
        name: set(values)
        for name, values in signal_identities.items()
    }
    relevant_signal_presence = {
        value: [
            name for name in sorted(signal_identity_sets)
            if value in signal_identity_sets[name]
        ]
        for value in sorted(case.relevant)
    }

    start = perf_counter()
    fused_internal = fusion.fuse_ranked(signals, limit)
    fused = [_project_fused_candidate(candidate) for candidate in fused_internal]
    stage_ms["fusion_ms"] = _elapsed_ms(start)

    start = perf_counter()
    with _without_store_migration():
        hydrated = retrieval.hydrate_candidates(
            cfg,
            base,
            [dict(candidate) for candidate in fused],
            page_cache,
        )
    hydrated_public = [_public_projection(candidate) for candidate in hydrated]
    stage_ms["hydration_ms"] = _elapsed_ms(start)

    start = perf_counter()
    if not rerank_enabled:
        final_results = fused[:case.k]
        rerank_metadata = {"applied": False, "warning": "rerank disabled"}
    elif not cfg.rerank_model:
        final_results = fused[:case.k]
        rerank_metadata = {
            "applied": False,
            "warning": "reranker unavailable: no rerank model",
        }
    else:
        ranked, rerank_metadata = rerank.rerank_candidates(
            cfg,
            case.query,
            [dict(candidate) for candidate in hydrated],
            top_n=case.k,
        )
        rerank_metadata = dict(rerank_metadata)
        if rerank_metadata.get("applied"):
            scored_count = rerank_metadata.pop("_scored_count", len(ranked))
            rerank_metadata["scored_count"] = scored_count
            scored = [
                _public_projection(candidate)
                for candidate in ranked[:scored_count]
            ]
            scored_keys = {_candidate_key(candidate) for candidate in scored}
            unscored = [
                candidate for candidate in fused
                if _candidate_key(candidate) not in scored_keys
            ]
            final_results = (scored + unscored)[:case.k]
        else:
            rerank_metadata.pop("_scored_count", None)
            final_results = fused[:case.k]
    stage_ms["rerank_ms"] = _elapsed_ms(start)

    ranking = [_public_identity(result) for result in final_results]
    requested = [_public_identity(candidate) for candidate in fused]
    hydrated_identities = [_public_identity(candidate) for candidate in hydrated_public]
    trace = {
        "case_id": case.case_id,
        "domain": domain,
        "query": case.query,
        "mode": mode,
        "k": case.k,
        "stages": {
            "index": index_stage,
            "signals": {
                "counts": {
                    name: len(hits)
                    for name, hits in sorted(signals.items())
                },
                "identities": signal_identities,
                "ranked": ranked_signals,
                "relevant_presence": relevant_signal_presence,
            },
            "fusion": {
                "candidate_count": len(fused),
                "candidate_identities": requested,
                "source_mix": source_mix(fused),
            },
            "hydration": {
                "requested": len(fused),
                "hydrated": len(hydrated_public),
                "dropped": len(fused) - len(hydrated_public),
                "hydrated_identities": hydrated_identities,
            },
            "rerank": dict(rerank_metadata),
        },
        "results": final_results,
        "metrics_input": {
            "ranking": ranking,
            "relevant": dict(case.relevant),
            "intents": {name: list(values) for name, values in case.intents.items()},
        },
    }
    trace["latency"] = latency_summary(stage_ms)
    return trace
