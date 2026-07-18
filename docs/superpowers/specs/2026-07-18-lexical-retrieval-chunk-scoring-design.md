---
review:
  spec_hash: 4142665a7355e95c
  last_run: 2026-07-18
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-07-18-lexical-retrieval-chunk-scoring-intent.md
---

# Lexical retrieval chunk scoring — design

**Date:** 2026-07-18
**Topic:** `lexical-retrieval-chunk-scoring`
**Branch:** `dev-lexical-retrieval-chunk-scoring`
**Status:** approved design, pending written-spec review

## Context

The indexed representation already splits each non-reserved H2 section into 512-word
windows with 64-word overlap. Semantic retrieval addresses those windows by
`(domain, file, heading, chunk)`, and reranker hydration re-chunks current Markdown and
requires the current and indexed hashes to match.

Lexical retrieval does not follow that identity. `engine/grep.py::grep_sections` scores
an entire H2 section and always returns `chunk=0`. `retrieval.py::_domain_signals` then
maps the hit only to an indexed `chunk=0` record. A term found only in a later window can
therefore select unrelated opening text.

Repeated identical H2 headings expose a second identity defect. `chunk_markdown` resets
the sub-chunk number for every H2 occurrence, so two `## Setup` sections can both emit
`(file, "Setup", chunk=0)`. The sections are not semantic duplicates and must not be
deleted or merged.

## Goals

- Score direct lexical section signals on the exact current chunks produced by
  `chunk_markdown`.
- Admit a direct lexical hit only when its current hash matches the indexed record.
- Preserve all repeated H2 occurrences and give their windows collision-free chunk
  identities.
- Preserve the current lexical page-seed score, graph expansion, RRF lists, candidate
  ceiling, public result shape, and reranker document representation.
- Read and chunk each eligible page no more than once during one search request,
  including optional reranker hydration.

## Non-goals

- Deduplicating sections by heading, body hash, or semantic similarity.
- Changing chunk size 512, overlap 64, section text, or reranker prefixes.
- Changing the index schema version or storing plaintext chunks in the index.
- Changing public `wiki_search` inputs or result fields.
- Optimizing page-seed breadth, graph breadth, or the 32-candidate safety ceiling.
- Claiming corpus-wide Recall@32.

## Requirements

### R1 — collision-free repeated-heading chunks

`chunk_markdown` MUST keep a next-chunk counter per exact heading string across all H2
occurrences in one page. Each body window receives the next number for that heading.

For example, a two-window `## Setup`, a one-window `## Other`, and another two-window
`## Setup` produce `Setup: 0, 1`, `Other: 0`, and `Setup: 2, 3`.

Pages without repeated exact headings MUST retain their current chunk numbers. Heading
matching for this counter remains case-sensitive, matching the current stored identity.
`ordinal` continues to record source H2 order.

### R2 — preserve every source section

Repeated headings MUST NOT trigger deduplication, deletion, or merging. This applies
when bodies differ and when bodies or hashes happen to be identical. Neither heading
equality nor inferred semantic similarity permits discarding a source occurrence.

### R3 — refresh reused positional metadata

When `indexer.index_domain` reuses an existing record by identity, hash, dimension, and
schema version, it MUST refresh `ordinal` from the current chunk in addition to facets.
This prevents an identical reused body from retaining the source position of a
different repeated-heading occurrence.

### R4 — request-local page materialization

Retrieval MUST use a request-local cache keyed by `(domain, file)`. A materialized page
contains the current Markdown and the canonical section chunks from `chunk_markdown`.

The cache MUST:

- use `_domain_file_path` before reading;
- represent unreadable or unsafe pages as unavailable;
- live for one `wiki_search` call only;
- be shared by candidate preparation and optional hydration;
- avoid global or cross-request state.

Lexical/hybrid preparation populates the cache for eligible pages. Semantic-only search
does not read pages until hydration requires them.

### R5 — pure whole-section and chunk scorers

`engine/grep.py` MUST expose pure scoring paths that share the existing query-term
extraction and term-frequency rule:

- whole-H2 scoring over supplied Markdown for lexical page-seed aggregation;
- chunk scoring over supplied canonical section chunks for direct lexical signals.

The existing filesystem compatibility wrapper MUST remain as a thin adapter for focused
engine tests, but retrieval MUST pass already materialized content rather than make
`grep.py` read a page again.

Whole-H2 hits MUST NOT create direct section candidates and MUST NOT map to `chunk=0`.

### R6 — exact verified direct lexical hits

For every eligible domain, retrieval MUST build indexed section identities from records
that pass path, facet, and dimension-independent lexical eligibility checks.

A current chunk becomes a direct lexical candidate only when:

1. an indexed section record has the same `(file, heading, chunk)`;
2. that identity is unique in the loaded index;
3. the current chunk hash equals the indexed hash; and
4. the chunk term-frequency score is positive.

The hit uses the matched indexed record, not a synthetic `chunk=0` record. Missing,
unsafe, stale, unreadable, or colliding identities fail soft by producing no direct
lexical hit.

### R7 — unchanged page seeds and fusion

Lexical page-seed scores MUST remain the sum of the legacy whole-H2 term-frequency
scores for eligible indexed headings in each file. Scoring direct overlapping chunks
MUST NOT double-count overlap in the page-seed score.

The `lexical_page`, `graph_page`, and `lexical_section` signal meanings, page and graph
expansion, Reciprocal Rank Fusion constant and identity, `max(top_k, 32)` ceiling, and
public `hit`/`source` semantics remain unchanged.

Direct lexical chunks sort by:

1. descending term-frequency score;
2. file;
3. heading;
4. chunk number.

This preserves current ordering for short unique-heading sections and deterministically
orders repeated-heading windows.

### R8 — shared hydration cache

`server.wiki_search` MUST create the request-local materialization cache and pass it to
candidate preparation and hydration. `hydrate_candidates` MUST reuse cached current
chunks when available and retain its indexed-hash equality check.

The cache is an optimization, not a trust boundary: hydration still omits candidates
whose current chunk is absent or whose current hash differs from the indexed hash.

### R9 — lexical compatibility path

The internal `lexical_search` compatibility path MUST use the same chunk-aware
retrieval behavior rather than expose the old whole-H2-to-`chunk=0` defect. Its internal
signature MUST accept `Config` as its first argument and delegate to lexical
`search_read`; the public MCP API does not change.

Lexical mode MUST make zero embedding requests.

### R10 — migration

No index schema bump is performed. Rollout MUST run the existing `wiki_index(domain)`
once for every bound domain after upgrade.

During that reindex:

- unchanged identities remain eligible for hash reuse;
- later repeated-heading occurrences receive new chunk numbers;
- moved identities are embedded as needed;
- obsolete colliding identities disappear because the fresh record set is rebuilt.

Before reindex, duplicate loaded index identities are excluded from direct lexical
chunk hits. The system favors missing a stale ambiguous hit over attributing it to the
wrong source occurrence.

### R11 — documentation

After implementation, the iwiki `indexing` page MUST describe repeated-heading chunk
numbering and the one-time reindex. The iwiki `retrieval` page MUST describe verified
chunk-level direct lexical scoring and whole-H2 scoring limited to page seeds.
`wiki_lint` MUST report no broken or stale page for the changed sources.

## Components

### `engine/chunk.py`

Replace per-section `enumerate(...)->ci` numbering with a per-heading next-number map.
Do not change `_sections`, `_split_section`, chunk text, hash construction, exclusions,
or summary chunks.

### `indexer.py`

Refresh `prev.ordinal` when reusing a record. Existing fresh-set rebuild and key-based
reuse perform the migration without a schema change.

### `engine/grep.py`

Keep `_terms` and term-frequency semantics. Separate supplied-Markdown whole-section
scoring from supplied-chunk scoring. Retain a thin domain filesystem wrapper only where
compatibility tests require it.

### `retrieval.py`

Own safe page materialization, index collision detection, hash verification, lexical
page aggregation, and conversion of verified matches into existing internal hits. It
continues to own signal assembly and public candidate projection.

### `server.py`

Create and thread the request-local materialization cache. Do not expose the cache or
change MCP schemas.

## Data flow

1. `wiki_search` resolves mode, domains, facets, top-k, and threshold, then creates an
   empty request-local materialization cache.
2. `_domain_signals` loads safe eligible records and builds section lists and indexed
   identity state, marking duplicate identities ambiguous.
3. Semantic signals run as today.
4. In lexical/hybrid mode, each eligible page is safely materialized once.
5. Whole-H2 scoring over the cached Markdown produces only per-file page-seed totals.
6. Chunk scoring runs only over canonical current chunks with a unique matching indexed
   identity and equal hash.
7. Verified records create `lexical_section` hits; page totals create lexical seeds;
   graph expansion and RRF run unchanged.
8. If reranking is configured, hydration reuses the same page cache and rechecks the
   indexed hash before passing exact text to the reranker.
9. Final top-k handling and public result projection remain unchanged.

## Error handling

- Invalid or escaping indexed paths are filtered before materialization.
- Read failures mark a page unavailable for the current request.
- Empty or too-short query terms produce no lexical signals.
- Duplicate old index identities produce no direct lexical hit until reindex.
- Missing current identities and hash mismatches produce no direct lexical hit.
- There is no fallback from a failed later-window match to `chunk=0`.
- Reranker failure retains the existing preliminary-order fail-soft behavior.

All failures above remain local omissions. They do not turn a read search into an MCP
error.

## Migration and compatibility

The release uses project version `0.7.3`. No JSONL record fields or schema version
change. Public search fields remain exactly `domain`, `file`, `heading`, `chunk`,
`score`, `hit`, and `source`.

Operators MUST run `wiki_index` for every bound domain. Because indexing is incremental,
stable identities and hashes reuse their embeddings.

The meaning of `chunk` changes only for later occurrences of an exact repeated heading:
it continues numbering from earlier occurrences. Unique-heading pages remain
compatible.

## Testing

### Chunking

- A repeated heading with multiple windows produces continuous unique numbers.
- An intervening different heading has its own zero-based counter.
- Unique-heading pages preserve existing chunk numbers.
- Repeated headings with different bodies remain present.
- Repeated headings with identical bodies/hashes remain present with distinct identity
  and ordinal.

### Indexing

- Reindex converts an old repeated-heading collision into unique records without a
  schema bump.
- Stable records reuse embeddings.
- Moved repeated-heading identities embed as needed.
- Reused records refresh `ordinal`.

### Lexical scoring and retrieval

- A term only in a later window returns that exact `chunk > 0`.
- The unmatched `chunk=0` receives no direct lexical signal.
- Short-section score and ordering remain unchanged.
- A matching later repeated-H2 occurrence returns its unique chunk.
- Whole-H2 page-seed totals remain unchanged and do not double-count overlap.
- Old-index collisions, missing records, and hash mismatches produce no direct hit.
- Lexical mode performs no embedding request.
- Public fields, `hit`, `source`, RRF behavior, and candidate ceiling remain unchanged.

### Hydration and server integration

- Hydration sends the exact verified matched chunk text to the reranker.
- Candidate preparation and hydration share one request-local materialization.
- An eligible page is read and chunked at most once per request.
- Unsafe paths and stale chunks remain omitted.

### Verification commands

```bash
uv run pytest -q tests/engine/test_chunk.py tests/test_grep.py tests/test_indexer.py tests/test_retrieval.py tests/test_server_search.py
uv run pytest -q
uv run flake8 src tests
uv run iwiki-mcp --help
```

## Risks and mitigations

- **Old ambiguous index remains installed.** Direct lexical hits for colliding identities
  are skipped; rollout documentation requires one normal domain reindex.
- **Overlap inflates page seed scores.** Page seeds retain whole-H2 scoring; only direct
  section signals score chunks.
- **Request cache serves stale cross-request text.** Cache lifetime is one call and has
  no global state.
- **Identical repeated bodies reuse the wrong source position.** Index reuse refreshes
  `ordinal`; unique chunk numbering distinguishes occurrences after reindex.
- **Chunk scorer drifts from indexing.** Retrieval consumes `chunk_markdown` output
  directly rather than reproducing window logic in `grep.py`.

## Acceptance (from intent)

### Desired Outcomes

- A query term present only in `chunk > 0` returns that exact indexed chunk.
- `chunk=0` receives no lexical-section signal when its own text does not match.
- Short sections retain their current lexical behavior and deterministic ordering.
- Repeated identical H2 sections remain supported and receive distinct chunk identities,
  so retrieval can return the exact matching occurrence.
- No repeated H2 section is deleted or merged solely because its heading matches another
  section; different bodies and meanings remain independently searchable.
- The reranker receives the exact current matched chunk after index-hash verification.

### Done when

A query whose term exists only in a later window returns that exact `chunk > 0`, does
not assign the lexical-section match to `chunk=0`, sends the exact verified text to
hydration/reranking; repeated identical H2 sections have distinct searchable chunk
identities after reindex; and all focused and regression tests pass.
