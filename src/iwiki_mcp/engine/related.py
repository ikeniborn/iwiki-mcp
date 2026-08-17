"""Related sections: vector neighbours, with a link-graph fallback (the
parser reads both markdown links and legacy [[refs]])."""
from __future__ import annotations
from .store import Record, dequantize, cosine
from .links import parse_links


def _vector_neighbours(target: Record, recs: list[Record], top_k: int) -> list[dict]:
    tv = dequantize(target.scale, target.q)
    # Multiple sub-chunks of one section share the same `id` (`file#heading`);
    # keep only the best-scoring sub-chunk per section, not every window.
    best: dict[str, dict] = {}
    for r in recs:
        if r.id == target.id:
            continue
        score = round(cosine(tv, dequantize(r.scale, r.q)), 4)
        existing = best.get(r.id)
        if existing is None or score > existing["score"]:
            best[r.id] = {"id": r.id, "file": r.file, "heading": r.heading, "score": score}
    out = sorted(best.values(), key=lambda d: d["score"], reverse=True)
    return out[:top_k]


def _graph_neighbours(target_file: str, depth: int) -> list[str]:
    """BFS over the page link graph (markdown links and legacy [[refs]])
    starting from target_file's links, up to `depth` hops."""
    seen: set[str] = set()
    frontier = [target_file]
    for _ in range(max(0, depth)):
        nxt: list[str] = []
        for f in frontier:
            try:
                content = open(f, encoding="utf-8").read()
            except OSError:
                continue
            for link in parse_links(content):
                base = link.split("#", 1)[0]
                if base and base not in seen:
                    seen.add(base)
                    nxt.append(base if base.endswith(".md") else f"{base}.md")
        frontier = nxt
    return list(seen)


def related(target_id: str, recs: list[Record], top_k: int, graph_depth: int) -> dict:
    target = next((r for r in recs if r.id == target_id), None)
    if target is None:
        return {"vector": [], "graph": []}
    # `wiki_related` finds related *sections*: summary records (one per page, from
    # the frontmatter description) must never be vector-neighbour candidates.
    section_recs = [r for r in recs if r.kind == "section"]
    vec = _vector_neighbours(target, section_recs, top_k)
    graph = _graph_neighbours(target.file, graph_depth) if not vec else []
    return {"vector": vec, "graph": graph}
