"""Index a single domain into its JSONL store, with machine-portable
(domain-relative) `file` paths, and append ingest-log records."""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .base import index_path, log_path, migrate_store_location
from .engine.okf_artifacts import RESERVED_OKF
from .engine.chunk import chunk_markdown
from .engine.config import Config
from .engine.embed import embed_texts
from .engine.store import SCHEMA_VERSION, VectorStore, index_bytes, make_record

CAP_BYTES = 8 * 1024 * 1024
GRAPH_FALLBACK_WARNING = "graph refresh failed; Markdown fallback will be used"


@dataclass
class GraphMutation:
    """State carried across canonical mutation, Git commit, and graph finalize."""

    base: str
    domain: str
    store: object | None
    expected_fingerprint: str | None
    available: bool
    staged_fingerprint: str | None = None


def src_hash(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:16]
    except OSError:
        return None


def index_domain(cfg: Config, base: str, domain: str) -> dict:
    migrate_store_location(base, domain)
    dom_path = Path(base) / domain
    idx = index_path(base, domain)
    store = VectorStore(idx)
    existing = {f"{r.id}#{r.chunk}": r for r in store.load()}
    files = sorted(
        path for path in dom_path.rglob("*.md")
        if path.relative_to(dom_path).as_posix() not in RESERVED_OKF
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
        if (prev and prev.hash == c.hash and prev.dim == cfg.dimensions
                and prev.v == SCHEMA_VERSION):
            prev.type = c.type          # refresh facets without re-embedding
            prev.tags = list(c.tags)
            prev.ordinal = c.ordinal
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


def prepare_graph_mutation(
    base: str, domain: str, *, whole_domain: bool = False
) -> GraphMutation | None:
    """Establish completeness before an incremental derived-graph mutation."""
    from . import graph, sync
    from .engine.graph_store import GraphStore

    if not sync.is_git_repo(base):
        return None
    try:
        if whole_domain:
            return GraphMutation(base, domain, GraphStore(base), None, True)
        provider = graph.scoped_graph(base, (domain,))
        if provider is None:
            return GraphMutation(base, domain, None, None, False)
        expected = dict(provider.expected_fingerprints)[domain]
        return GraphMutation(base, domain, provider.store, expected, True)
    except Exception:
        return GraphMutation(base, domain, None, None, False)


def _write_staged_graph_fingerprint(
    mutation: GraphMutation, fingerprint, connection
) -> None:
    mutation.staged_fingerprint = fingerprint.value
    connection.execute(
        "UPDATE domains SET indexed_commit = ?, markdown_fingerprint = ?, "
        "state = 'dirty', indexed_at = ? WHERE domain = ?",
        (
            fingerprint.indexed_commit,
            fingerprint.value,
            _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            mutation.domain,
        ),
    )


def _set_staged_graph_fingerprint(mutation: GraphMutation, fingerprint) -> None:
    with mutation.store.transaction() as connection:
        _write_staged_graph_fingerprint(mutation, fingerprint, connection)


def _graph_failed(mutation: GraphMutation) -> str:
    mutation.available = False
    mutation.staged_fingerprint = None
    if mutation.store is not None:
        try:
            mutation.store.mark_domain_dirty(mutation.domain)
        except Exception:
            pass
    return GRAPH_FALLBACK_WARNING


def _graph_content_parity(mutation: GraphMutation, connection) -> bool:
    root = Path(mutation.base) / mutation.domain
    markdown = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*.md"))
        if path.is_file()
        and path.relative_to(root).as_posix() not in RESERVED_OKF
    }
    indexed = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT file, content_hash FROM pages WHERE domain = ?",
            (mutation.domain,),
        )
    }
    return indexed == markdown


def _finalize_incremental_batch(mutation: GraphMutation, expected_fingerprint):
    from . import graph

    def finalize(connection) -> None:
        rechecked = graph.markdown_fingerprint(mutation.base, mutation.domain)
        if rechecked != expected_fingerprint or not _graph_content_parity(
            mutation, connection
        ):
            raise RuntimeError("graph batch changed during refresh")
        _write_staged_graph_fingerprint(mutation, rechecked, connection)

    return finalize


def stage_graph_pages(
    mutation: GraphMutation | None,
    *,
    refresh_files: tuple[str, ...] = (),
    delete_files: tuple[str, ...] = (),
) -> str | None:
    """Refresh only affected pages, keeping the domain untrusted until finalize."""
    if mutation is None:
        return None
    if not mutation.available or mutation.store is None:
        return GRAPH_FALLBACK_WARNING
    from . import graph

    try:
        current = graph.markdown_fingerprint(mutation.base, mutation.domain)
        with mutation.store.read_snapshot() as connection:
            row = connection.execute(
                "SELECT markdown_fingerprint, state FROM domains WHERE domain = ?",
                (mutation.domain,),
            ).fetchone()
        metadata = None if row is None else tuple(row)
        if metadata not in {
            (mutation.expected_fingerprint, "ready"),
            (current.value, "ready"),
        }:
            return _graph_failed(mutation)
        mutation.store.mark_domain_dirty(mutation.domain)
        root = Path(mutation.base) / mutation.domain
        pages = tuple(
            (file, (root / file).read_text(encoding="utf-8"))
            for file in refresh_files
        )
        mutation.store.refresh_pages(
            mutation.domain,
            pages,
            delete_files=delete_files,
            finalize=_finalize_incremental_batch(mutation, current),
        )
        return None
    except Exception:
        return _graph_failed(mutation)


def stage_graph_rebuild(mutation: GraphMutation | None) -> str | None:
    """Rebuild a whole domain, then leave it dirty until the Git phase ends."""
    if mutation is None:
        return None
    if not mutation.available or mutation.store is None:
        return GRAPH_FALLBACK_WARNING
    from . import graph

    try:
        mutation.store.mark_domain_dirty(mutation.domain)
        provider = graph.scoped_graph(mutation.base, (mutation.domain,))
        if provider is None:
            return _graph_failed(mutation)
        mutation.store = provider.store
        fingerprint = graph.markdown_fingerprint(mutation.base, mutation.domain)
        mutation.store.mark_domain_dirty(mutation.domain)
        _set_staged_graph_fingerprint(mutation, fingerprint)
        return None
    except Exception:
        return _graph_failed(mutation)


def finalize_graph_mutation(mutation: GraphMutation | None) -> str | None:
    """Publish a staged graph using metadata only after the local Git phase."""
    if mutation is None:
        return None
    if (
        not mutation.available
        or mutation.store is None
        or mutation.staged_fingerprint is None
    ):
        return GRAPH_FALLBACK_WARNING
    from . import graph

    try:
        fingerprint = graph.markdown_fingerprint(mutation.base, mutation.domain)
        with mutation.store.transaction() as connection:
            row = connection.execute(
                "SELECT markdown_fingerprint, state FROM domains WHERE domain = ?",
                (mutation.domain,),
            ).fetchone()
            if row is None:
                raise RuntimeError("domain graph is unavailable")
            if tuple(row) == (fingerprint.value, "ready"):
                return None
            if tuple(row) != (mutation.staged_fingerprint, "dirty"):
                raise RuntimeError("domain graph changed during mutation")
            rechecked = graph.markdown_fingerprint(mutation.base, mutation.domain)
            if rechecked != fingerprint:
                raise RuntimeError("Markdown changed during graph finalize")
            connection.execute(
                "UPDATE domains SET indexed_commit = ?, markdown_fingerprint = ?, "
                "state = 'ready', "
                "indexed_at = ? WHERE domain = ?",
                (
                    rechecked.indexed_commit,
                    rechecked.value,
                    _dt.datetime.now(_dt.timezone.utc).isoformat().replace(
                        "+00:00", "Z"
                    ),
                    mutation.domain,
                ),
            )
        return None
    except Exception:
        return _graph_failed(mutation)


def _invalidate_graph_batch(mutations: tuple[GraphMutation, ...]) -> bool:
    from . import graph

    stores = [mutation.store for mutation in mutations if mutation.store is not None]
    if stores:
        try:
            with stores[0].transaction() as connection:
                now = _dt.datetime.now(_dt.timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                )
                connection.executemany(
                    "INSERT INTO domains VALUES (?, NULL, '', 'dirty', ?) "
                    "ON CONFLICT(domain) DO UPDATE SET state = 'dirty'",
                    ((mutation.domain, now) for mutation in mutations),
                )
        except Exception:
            pass
    unavailable = True
    for mutation in mutations:
        mutation.available = False
        mutation.staged_fingerprint = None
        if mutation.store is None:
            continue
        try:
            current = graph.markdown_fingerprint(
                mutation.base, mutation.domain
            ).value
            with mutation.store.read_snapshot() as connection:
                row = connection.execute(
                    "SELECT markdown_fingerprint, state FROM domains "
                    "WHERE domain = ?",
                    (mutation.domain,),
                ).fetchone()
            if row is not None and tuple(row) == (current, "ready"):
                unavailable = False
        except Exception:
            continue
    return unavailable


def finalize_graph_batch(
    mutations: tuple[GraphMutation, ...],
    refresh_files: dict[str, tuple[str, ...]],
    delete_files: dict[str, tuple[str, ...]],
) -> str | None:
    """Refresh and publish all affected domain graphs in one transaction."""
    if not mutations:
        return None
    usable = all(
        mutation.available and mutation.store is not None
        for mutation in mutations
    )
    bases = {mutation.base for mutation in mutations}
    paths = {
        str(mutation.store.path)
        for mutation in mutations
        if mutation.store is not None
    }
    if not usable or len(bases) != 1 or len(paths) != 1:
        if _invalidate_graph_batch(mutations):
            return GRAPH_FALLBACK_WARNING
        raise RuntimeError("cannot invalidate graph batch")

    from . import graph
    from .engine.graph_store import GraphStore

    fingerprints = {}
    prepared = {}
    try:
        for mutation in mutations:
            fingerprints[mutation.domain] = graph.markdown_fingerprint(
                mutation.base, mutation.domain
            )
            root = Path(mutation.base) / mutation.domain
            prepared[mutation.domain] = tuple(
                (file, (root / file).read_text(encoding="utf-8"))
                for file in refresh_files.get(mutation.domain, ())
            )
        store = mutations[0].store
        with store.transaction() as connection:
            for mutation in mutations:
                row = connection.execute(
                    "SELECT markdown_fingerprint, state FROM domains "
                    "WHERE domain = ?",
                    (mutation.domain,),
                ).fetchone()
                expected = mutation.expected_fingerprint
                if row is None or row[1] != "ready" or (
                    expected is not None
                    and row[0] not in {expected, fingerprints[mutation.domain].value}
                ):
                    raise RuntimeError("domain graph changed before batch")
                GraphStore.refresh_pages_in_transaction(
                    connection,
                    mutation.domain,
                    prepared[mutation.domain],
                    delete_files=delete_files.get(mutation.domain, ()),
                )
            for mutation in mutations:
                rechecked = graph.markdown_fingerprint(
                    mutation.base, mutation.domain
                )
                if (
                    rechecked != fingerprints[mutation.domain]
                    or not _graph_content_parity(mutation, connection)
                ):
                    raise RuntimeError("graph batch changed during finalize")
                connection.execute(
                    "UPDATE domains SET indexed_commit = ?, "
                    "markdown_fingerprint = ?, state = 'ready', indexed_at = ? "
                    "WHERE domain = ?",
                    (
                        rechecked.indexed_commit,
                        rechecked.value,
                        _dt.datetime.now(_dt.timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        mutation.domain,
                    ),
                )
        return None
    except Exception:
        if _invalidate_graph_batch(mutations):
            return GRAPH_FALLBACK_WARNING
        raise RuntimeError("cannot invalidate graph batch")


def append_log(base: str, domain: str, op: str, source: str, page: str,
               src_hash: str | None) -> None:
    path = log_path(base, domain)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rec = {"op": op, "source": source, "page": page,
           "date": _dt.date.today().isoformat(), "src_hash": src_hash}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def upsert_ingest_log(base: str, domain: str, source: str, page: str,
                      src_hash: str | None) -> None:
    """Replace any prior ``ingest`` records for ``page`` with a single fresh one.

    Unlike ``append_log`` this keeps one ingest record per page, so ``lint``'s
    stale detection reads the current ``src_hash`` after an edit. ``lint`` also
    tolerates append-only history via its last-wins ``_latest_ingest_by_page``
    reader, but a single record keeps the log compact.
    """
    path = log_path(base, domain)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    kept: list[str] = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                s = line.strip()
                if not s:
                    continue
                try:
                    rec = json.loads(s)
                except Exception:
                    kept.append(s)
                    continue
                if rec.get("op") == "ingest" and rec.get("page") == page:
                    continue   # drop the stale record for this page
                kept.append(s)
    rec = {"op": "ingest", "source": source, "page": page,
           "date": _dt.date.today().isoformat(), "src_hash": src_hash}
    kept.append(json.dumps(rec, ensure_ascii=False))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(kept) + "\n")
