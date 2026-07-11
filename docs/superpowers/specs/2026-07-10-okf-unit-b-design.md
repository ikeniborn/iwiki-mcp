---
review:
  stage: spec
  spec_hash: 8395e0b1ce93a6bf
  last_run: 2026-07-10
  phases:
    structure:
      status: passed
    coverage:
      status: passed
    clarity:
      status: passed
    consistency:
      status: passed
  findings: []
chain:
  intent: n/a
---
# OKF Unit B — hierarchical retrieval — design

**Date:** 2026-07-10
**Branch:** `dev-okf-unit-b` (off `master`, Unit A merged, v0.4.1) → PR into `master`
**Status:** approved design, pre-implementation

## Context

Today every section chunk vector is prefixed with `# title` + the article
`description` + `## heading` + lead (`engine/chunk.py`), so each section vector is
diluted by whole-article context and sections across a page look similar. Unit B
replaces the flat single-level index + `vector_search`/`lexical_search`/hybrid
merge with a **two-level hierarchical retrieval**, ported for parity from the
validated implementation in the sibling project `obsidian-ai-wiki`
(`src/page-similarity.ts`, `src/phases/query.ts`, `src/wiki-seeds.ts`,
`src/wiki-graph.ts`; documented in the `obsidian-ai-wiki` wiki page
`hierarchical-retrieval-eval`).

Builds on Unit A: the full frontmatter `description` (item 3) is the article-level
("summary") embedding input.

## The model

Per page the index holds two record kinds:

- **`summary`** — one per page. Embedding input = the full frontmatter
  `description` only (no title, no body). Used for article-level (seed) scoring.
- **`section`** — one per section sub-chunk. Embedding input = `## {heading}` +
  the windowed section body only — **no title, no description prefix**. Used for
  section-level ranking. Carries `heading` + `ordinal`.

Summary and section embedding inputs are disjoint: article-id / title tokens
never enter a section vector, so they cannot pull an unrelated section.

Retrieval is a single hierarchical flow (embeddings, with a lexical/Jaccard
fallback at both stages):

1. **Seed** — cosine(query, summary vectors) → top `seed_top_k` articles, kept
   above `seed_threshold`. On a weak/failed embedding signal, fall back to lexical
   seed scoring (grep containment over the page).
2. **Graph expand** — undirected BFS over the wiki-link graph
   (`engine/links.parse_links`) from the seed article ids, `graph_depth` hops;
   rank the non-seed BFS pages and cap to `bfs_top_k` → the candidate article
   pool. Each pooled article is tagged `source: "seed" | "graph"`.
3. **Section rank** — score section records whose `file` is in the pool (cosine,
   or grep/Jaccard fallback over `heading + window`) → top `top_k`. Ties break
   `source == "seed"` first, then the article's seed score, then `file`/`ordinal`.
4. **Return** — section hits `{domain, file, heading, chunk, score, source}`.

`mode` maps onto the one flow: `vector` = embeddings, `lexical` = forced
grep/Jaccard at both stages, `hybrid` = embeddings with the grep fallback. The
flat `vector_search` / `lexical_search` / hybrid-merge are replaced by this flow.

## Components

- **`engine/chunk.py`** — drop the `# title` + article-summary prefix from section
  chunks. Emit one `summary` chunk (embed input = full `description`) and clean
  `section` chunks (embed input = `## {heading}` + windowed body). `Chunk` gains
  `kind: "summary" | "section"` and `ordinal`. Keep iwiki's existing word-based
  windows (`chunk_size` / `chunk_overlap`) — the char-based `DEFAULT_CHUNKING`
  from the reference is an implementation detail not ported; the load-bearing
  change is the disjoint inputs + hierarchy, not the window unit.
- **`engine/store.py`** — `Record` gains `kind` (default `"section"` for
  back-compat load of pre-Unit-B indices) and an index `SCHEMA_VERSION` guard so
  an old-schema index is treated as a full miss (forces re-embed) rather than
  silently mixed.
- **`engine/hier.py`** (new) — the hierarchical scoring core, framework-free and
  unit-testable: seed scoring, graph expansion (a BFS helper, shared with /
  extracted from `engine/related.py`'s existing graph walk), section ranking
  within a pool, and the tie-break ordering. Takes records + query vector (or the
  grep scorer) and returns ordered section hits.
- **`retrieval.py`** — rewrite `vector_search` / `lexical_search` / `hybrid_search`
  to drive `engine/hier.py`: embed the query once, run seed → graph → section,
  thread the facet (`type`/`tags`) filters, and preserve the fail-soft JSON shape.
- **`engine/links.py` / `engine/related.py`** — reuse for the BFS graph
  expansion; extract a reusable undirected-BFS helper if that keeps `related.py`
  focused.
- **`indexer.py`** — build summary + section chunks and embed both; hash-reuse
  migrates automatically (clean section text yields new hashes → re-embed; summary
  records are new; stale prefixed records drop because `fresh` is rebuilt from the
  current chunk set). Honor the `SCHEMA_VERSION` guard.
- **`engine/config.py`** — add `seed_top_k` (default 5), `bfs_top_k` (default 10),
  `seed_threshold` (default from the reference gate). `graph_depth` already exists
  (default 2; set the retrieval default to 1 for parity, still configurable).
- **`eval/hierarchical/`** (new) — the mini eval harness (see below).

## Migration / index versioning

The index schema changes (new `kind`, summary records, clean section text). The
`SCHEMA_VERSION` guard makes `index_domain` treat a pre-Unit-B index as a full
miss, so a single `wiki_index` per domain (or `wiki_export_okf`, which re-indexes)
rebuilds it into the two-level format. No data loss — pages are re-chunked and
re-embedded from source markdown. The rebuild is a one-time embedding cost per
domain, documented for operators.

## Mini eval harness

`eval/hierarchical/` — a deterministic, network-free pytest-runnable harness that
measures hierarchical retrieval quality:

- **Fixtures** — a small in-memory vault: pages with a `description`, several `##`
  sections, and `[[wiki-links]]` forming a graph.
- **A fake/deterministic embedder** (or forced Jaccard mode) so the harness runs
  in CI without network — same pattern as the existing `monkeypatch` of
  `indexer.embed_texts`.
- **A query set** — each query labelled with the expected article id(s) and the
  expected section heading(s).
- **Metrics** — article recall@k (does the seed+graph pool contain the expected
  article?) and section recall@k (does the ranked section list contain the
  expected section?), plus a mean-reciprocal-rank style ranking number.
- Runs under `uv run pytest`; asserts the metrics clear a floor so a regression
  fails the build.

## Success criteria

- Section vectors exclude the article title/description (verified by a chunk test
  asserting the embed input of a `section` chunk contains no title/description
  text).
- `summary` + `section` records are produced and round-trip through the store with
  `kind`.
- The hierarchical flow returns sections only from the seed+graph candidate pool,
  with `source` tagging, in the specified tie-break order (hier tests).
- `wiki_search` works end to end in all three modes on the real `iwiki-mcp`
  domain after a re-index (live smoke).
- The mini eval harness passes its recall/ranking floor.
- `uv run pytest -q` green; `uv run flake8 src tests` clean.

## Tests

- `chunk`: summary chunk present; section chunk embed input has no title/desc;
  `kind`/`ordinal` set; reserved/`## Overview` still excluded.
- `store`: `kind` round-trips; `SCHEMA_VERSION` guard forces re-embed of an
  old-schema index.
- `hier`: seed selection (top-k + threshold + lexical fallback); graph expansion
  (undirected BFS, depth, `bfs_top_k` cap, `source` tag); section ranking within
  the pool; tie-break ordering; grep/Jaccard fallback path.
- `retrieval`: the three modes drive the flow; facet filters honored; fail-soft
  shape preserved.
- `indexer`: re-index migrates an old-schema index to two-level (hash-reuse +
  stale drop + summary add).
- `eval`: the harness computes metrics and clears the floor.

## Out of scope

- Char-based chunk windows / `maxCount` fold from the reference (kept word-based).
- Cross-domain fused re-ranking beyond the existing multi-domain iteration
  (`runCrossDomainQuery` parity) — the per-domain hierarchical flow is applied
  across in-scope domains as today; a fused cross-domain pool is a follow-up.
- Any change to the write/frontmatter path (that was Unit A).

## Version / branch

Minor bump `0.4.1` → `0.5.0` (retrieval-behavior redesign). Branch
`dev-okf-unit-b` off `master`, PR into `master`.
