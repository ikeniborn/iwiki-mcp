"""Hierarchical retrieval core (framework-free): article seed scoring, undirected
wiki-graph expansion into a candidate pool, then clean-section ranking inside it.
Ported for parity from obsidian-ai-wiki's page-similarity/query flow."""
from __future__ import annotations

from pathlib import Path

from .store import Record, dequantize, cosine
from .links import parse_links
from .okf_artifacts import RESERVED_OKF


def sim(query_vec: list[float], rec: Record) -> float:
    return float(cosine(query_vec, dequantize(rec.scale, rec.q)))


def seed_articles(query_vec: list[float], summary_recs: list[Record],
                  top_k: int, threshold: float) -> list[tuple[str, float]]:
    scored = [(r.file, round(sim(query_vec, r), 4)) for r in summary_recs]
    scored = [(f, s) for f, s in scored if s >= threshold]
    scored.sort(key=lambda t: (-t[1], t[0]))
    return scored[:top_k]


def _adjacency(domain_dir: str) -> dict[str, set[str]]:
    """Undirected page graph keyed by domain-relative '<type>/<slug>.md'. An edge
    a->b also adds b->a. Walks the nested type-dir tree; a link target is
    normalized to '<type>/<slug>' by parse_links and matched with a '.md' suffix.
    Skips the generated OKF artifacts (index.md/log.md): index.md links every
    page in the domain, so reading it here would turn it into an all-pages hub
    that pulls the whole domain into every seed's candidate pool."""
    adj: dict[str, set[str]] = {}
    root = Path(domain_dir)
    if not root.is_dir():
        return adj
    for path in root.rglob("*.md"):
        name = path.relative_to(root).as_posix()
        if name in RESERVED_OKF:
            continue
        try:
            content = path.read_text(encoding="utf-8")
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


def rank_graph_pages(seeds: list[tuple[str, str, int]], domain_dir: str,
                     depth: int, cap: int) -> list[dict]:
    rows: dict[str, dict] = {}
    discovery = 0
    for file, origin, seed_rank in sorted(
            seeds, key=lambda seed: (seed[2], seed[0], seed[1])):
        row = rows.get(file)
        if row is None:
            rows[file] = {"file": file, "source": "seed", "origins": [origin],
                          "distance": 0, "seed_rank": seed_rank,
                          "discovery": discovery}
            discovery += 1
        else:
            row["origins"] = sorted(set(row["origins"]) | {origin})
            row["seed_rank"] = min(row["seed_rank"], seed_rank)

    seed_count = len(rows)
    adjacency = _adjacency(domain_dir)
    frontier = list(rows)
    for distance in range(1, max(0, depth) + 1):
        next_frontier: list[str] = []
        for file in frontier:
            parent = rows[file]
            for neighbor in sorted(adjacency.get(file, ())):
                row = rows.get(neighbor)
                if row is None:
                    rows[neighbor] = {
                        "file": neighbor,
                        "source": "graph",
                        "origins": list(parent["origins"]),
                        "distance": distance,
                        "seed_rank": parent["seed_rank"],
                        "discovery": discovery,
                    }
                    discovery += 1
                    next_frontier.append(neighbor)
                elif row["distance"] == distance:
                    row["origins"] = sorted(set(row["origins"]) | set(parent["origins"]))
                    row["seed_rank"] = min(row["seed_rank"], parent["seed_rank"])
        frontier = next_frontier

    ranked = sorted(rows.values(), key=lambda row: (
        0 if row["source"] == "seed" else 1,
        row["distance"], row["seed_rank"], row["file"], row["discovery"],
    ))
    if cap > 0:
        return ranked[:seed_count + cap]
    return ranked


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
