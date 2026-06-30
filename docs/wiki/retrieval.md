# Retrieval and search

## Overview
`retrieval.py` answers queries across the in-scope domains, combining a numpy vector path with a lexical (grep) path into hybrid results. Supporting `engine/` modules: `search` (cosine top-k), `grep` (term-frequency sections), and `related` (neighbours by vector or link graph). [[mcp-server#Tool surface]] exposes these via `wiki_search` and `wiki_related`; scope comes from [[base-binding#Search scope]].

## Hybrid search
`hybrid_search(cfg, base, domains, query, top_k, threshold, mode)` validates `mode` against `hybrid`/`vector`/`lexical` and runs the requested paths. It merges hits by `(domain, file, heading)`: vector and lexical hits on the same section collapse into one marked `both`. Because the two scores live on different scales, results are ordered vector/both first (by cosine), then lexical (by term frequency), then truncated to `top_k`.

## Vector search
`vector_search` embeds the query once, then for each domain loads records whose `dim` matches and computes cosine similarity with numpy: a matrix-vector product normalized by row norms. Hits at or above `threshold` are kept with a rounded `score` and `hit: "vector"`, sorted by descending score then domain/file/heading, and capped at `top_k`. Records are dequantized from int8 before the math (see [[indexing#Vector store]]).

## Lexical search
`lexical_search` complements vectors by catching exact symbol or identifier matches that embeddings blur. It calls `grep_sections` per domain, which scores each `##` section by counting query-term occurrences (terms longer than two chars, lowercased) in the heading plus body. Sections with a non-zero count are returned as `hit: "lexical"`, sorted by score then file/heading.

## Related sections
`related(section_id, recs, top_k, graph_depth)` finds sections near a given one. First it ranks vector neighbours by cosine over the domain's records. If there are none, it falls back to a `[[refs]]` graph: a breadth-first walk over wiki-links up to `graph_depth` hops. `wiki_related` changes into the domain directory first so relative link targets resolve correctly. Link parsing is shared with [[authoring-and-linting#Wiki-link parsing]].

## Result shape
Every retrieval hit is a uniform dict: `domain`, `file`, `heading`, `chunk`, `score`, and `hit` (`vector`, `lexical`, or `both`). The consistent shape lets `hybrid_search` merge and dedupe paths without special-casing, and lets MCP clients render results identically regardless of which path produced them. `wiki_search` wraps the list as `{"results": [...]}`.
