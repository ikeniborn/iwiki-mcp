"""Index a single domain into its JSONL store, with machine-portable
(domain-relative) `file` paths, and append ingest-log records."""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from pathlib import Path

from .base import index_path, log_path
from .engine.chunk import chunk_markdown
from .engine.config import Config
from .engine.embed import embed_texts
from .engine.store import VectorStore, index_bytes, make_record

CAP_BYTES = 8 * 1024 * 1024


def src_hash(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:16]
    except OSError:
        return None


def index_domain(cfg: Config, base: str, domain: str) -> dict:
    dom_path = Path(base) / domain
    idx = index_path(base, domain)
    store = VectorStore(idx)
    existing = {f"{r.id}#{r.chunk}": r for r in store.load()}
    files = sorted(
        path for path in dom_path.rglob("*.md")
        if ".iwiki" not in path.relative_to(dom_path).parts
    )
    chunks = []
    for md in files:
        rel = md.relative_to(dom_path).as_posix()
        with open(md, encoding="utf-8") as fh:
            content = fh.read()
        chunks.extend(chunk_markdown(rel, content, cfg.chunk_size,
                                     cfg.chunk_overlap, cfg.summary_max))
    fresh, reused, to_embed = [], 0, []
    for c in chunks:
        key = f"{c.id}#{c.chunk}"
        prev = existing.get(key)
        if prev and prev.hash == c.hash and prev.dim == cfg.dimensions:
            fresh.append(prev)
            reused += 1
        else:
            to_embed.append(c)
    if to_embed:
        vecs = embed_texts(cfg, [c.text for c in to_embed])
        fresh.extend(make_record(c, v) for c, v in zip(to_embed, vecs))
    fresh.sort(key=lambda r: (r.file, r.heading, r.chunk))
    store.save(fresh)
    size = index_bytes(idx)
    return {"indexed_chunks": len(fresh), "reused": reused,
            "embedded": len(to_embed), "bytes": size, "over_cap": size > CAP_BYTES}


def append_log(base: str, domain: str, op: str, source: str, page: str,
               src_hash: str | None) -> None:
    path = log_path(base, domain)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rec = {"op": op, "source": source, "page": page,
           "date": _dt.date.today().isoformat(), "src_hash": src_hash}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
