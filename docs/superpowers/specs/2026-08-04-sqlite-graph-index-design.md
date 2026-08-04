---
review:
  spec_hash: 3b932e3e2f753d6e
  last_run: 2026-08-04
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-08-04-sqlite-graph-index-intent.md
  spec: null
---

# SQLite Graph Index — Design

**Date:** 2026-08-04
**Status:** approved
**Topic:** `sqlite-graph-index`
**Intent:** `docs/superpowers/intents/2026-08-04-sqlite-graph-index-intent.md`

## 1. Purpose and scope

Replace the per-query Markdown walk used to construct each domain's adjacency
map with one base-wide, local SQLite graph index. The index supports explicit
cross-domain links while preserving current intra-domain links, undirected
search expansion, bounded BFS, deterministic ranking, and project read-scope
isolation.

The change does not move vectors or ingest history into SQLite. Domain
`index.jsonl` files remain the Git-synchronized portable embedding snapshots;
domain `log.jsonl` files remain the Git-synchronized provenance history.
Markdown remains authoritative for pages, anchors, and links. SQLite is a
rebuildable local cache.

Source-code dependency extraction is outside this design. The graph does not
parse ASTs, imports, calls, inheritance, package manifests, or test-to-symbol
relationships. A future code graph requires a separate approved capability and
may link its nodes to wiki pages through existing source provenance.

## 2. Accepted decisions

- Store one graph database at `<IWIKI_BASE_DIR>/.iwiki/graph.sqlite3`.
- Exclude the base-root `.iwiki/`, including SQLite WAL/SHM files, from Git
  synchronization through the repository-local Git exclude file.
- Use relative Markdown `.md` links for intra-domain edges.
- Use `iwiki://<domain>/<page-id>#<anchor>` for cross-domain edges.
- Store authored edges as directed; traverse incoming and outgoing edges for
  `wiki_search` to preserve its current undirected graph semantics.
- Keep `wiki_related(domain, section_id)` domain-local and preserve its public
  response contract.
- Keep `index.jsonl` and `log.jsonl` in each domain. Do not mirror them into
  SQLite in this change.
- Rebuild changed domains proactively after MCP-managed pulls and lazily on a
  freshness mismatch caused by an external pull or working-tree edit.

## 3. Storage ownership

| Artifact | Ownership | Git | Recovery |
|---|---|---|---|
| `<domain>/**/*.md` | canonical authored content | tracked | Git |
| `<domain>/index.jsonl` | portable quantized embeddings | tracked | re-embed or Git |
| `<domain>/log.jsonl` | canonical ingest provenance/history | tracked | Git |
| `.iwiki/graph.sqlite3` | local graph and freshness cache | untracked | parse Markdown |
| `.iwiki/lock` | existing local Git-operation lock | untracked | recreate |
| `.iwiki/graph.sqlite3-wal` / `-shm` | SQLite runtime state | untracked | SQLite |

The server must not stage or commit any `.iwiki/` file. Existing domain-scoped
commit pathspecs continue to include Markdown, `index.jsonl`, and `log.jsonl`.
On graph initialization in a Git base, the server idempotently adds the
root-anchored `/.iwiki/` pattern to the path returned by
`git rev-parse --git-path info/exclude`; it does not edit the tracked root
`.gitignore`. The anchored rule does not match legacy `<domain>/.iwiki/`
directories, and already tracked legacy files retain normal Git behaviour.

## 4. Identity and link model

### R1 — Global page identity

A page is identified by `<domain>/<type>/<slug>` without `.md`. The stored
`file` remains domain-relative `<type>/<slug>.md`. Domain, type, slug, and file
components pass the existing path-safety rules; empty components, `.` and `..`
are rejected.

Examples:

```text
iwiki-mcp/concept/retrieval
backend/reference/auth-api
```

### R2 — Intra-domain links

Existing CommonMark relative links and legacy wikilinks remain intra-domain.
They resolve against the source domain exactly as today. Existing parsing of
code fences, inline code, anchors, and `.md` normalization remains compatible.

### R3 — Cross-domain links

The only cross-domain syntax is:

```markdown
[Retrieval](iwiki://iwiki-mcp/concept/retrieval#hybrid-search)
```

The URI authority is the target domain. The URI path is an absolute page id
within that domain, without a required `.md` suffix. The optional fragment is
normalized with the existing heading slug algorithm. Query, user-info, port,
percent-decoded path separators, empty domain/path, and unsafe path components
are rejected as invalid wiki targets. Other URI schemes remain external links
and never enter the wiki graph.

The parser returns a structured internal value containing source domain,
target domain, target page path, target anchor, raw target, and `intra` or
`cross` kind. Public authoring text stays Markdown.

## 5. SQLite schema

The database uses standard-library `sqlite3`, WAL mode, `synchronous=NORMAL`,
foreign keys, a busy timeout, and `PRAGMA user_version` for schema migration.

```sql
CREATE TABLE domains (
    domain TEXT PRIMARY KEY,
    indexed_commit TEXT,
    markdown_fingerprint TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('ready', 'dirty', 'rebuilding')),
    indexed_at TEXT NOT NULL
);

CREATE TABLE pages (
    page_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    file TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    link_hash TEXT NOT NULL,
    UNIQUE(domain, file)
);

CREATE TABLE anchors (
    page_id TEXT NOT NULL,
    anchor TEXT NOT NULL,
    heading TEXT NOT NULL,
    PRIMARY KEY(page_id, anchor),
    FOREIGN KEY(page_id) REFERENCES pages(page_id) ON DELETE CASCADE
);

CREATE TABLE edges (
    source_page_id TEXT NOT NULL,
    target_page_id TEXT NOT NULL,
    target_anchor TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL CHECK (kind IN ('intra', 'cross')),
    raw_target TEXT NOT NULL,
    PRIMARY KEY(source_page_id, target_page_id, target_anchor),
    FOREIGN KEY(source_page_id) REFERENCES pages(page_id) ON DELETE CASCADE
);

CREATE INDEX edges_target_idx ON edges(target_page_id);
```

`target_page_id` intentionally has no foreign key. Missing targets remain as
edges so lint can report them. Duplicate authored links that resolve to the
same `(source, target, anchor)` collapse to one graph edge; `raw_target` is the
lexicographically smallest authored target among the duplicates, making full
and incremental builds deterministic regardless of enumeration order.

## 6. Graph maintenance

### R4 — Page refresh

For a created or changed page, parse the complete current Markdown rather than
diffing the edited section. In one `BEGIN IMMEDIATE` transaction:

1. Upsert the page and its content/link hashes.
2. Delete its old anchors and outgoing edges.
3. Insert current anchors and normalized edges.
4. Commit only after all rows validate.

Anchors include ATX headings from level 1 through 6 outside fenced/inline code.
This preserves link validation for legacy or externally edited pages with deep
headings even though write-time validation reports those headings separately.
When repeated headings normalize to the same anchor, the earliest document
occurrence supplies the stored diagnostic `heading`; existence remains one
anchor row, matching current lint semantics.

If the normalized `link_hash` is unchanged, the implementation may retain
existing anchors/edges while updating the page content hash. This is an
optimization, not a required observable behaviour.

### R5 — Delete and move

Deleting a page removes its page row, anchors, and outgoing edges through
cascade. Incoming edges remain and become broken-link findings.

Moving a page refreshes the moved page under its new identity and refreshes
every Markdown page rewritten by the existing link-rewrite operation. Graph
identity changes never rewrite SQLite alone; Markdown rewrite remains
authoritative.

### R6 — Domain rebuild

A freshness mismatch first marks a domain `dirty` in a short committed
transaction. After the process acquires the base lock, another short
transaction commits `state="rebuilding"` before Markdown is read. This makes
an active or crashed rebuild observable to other processes. The process builds
the replacement snapshot in memory, then a final SQLite transaction replaces
all pages/anchors/outgoing edges sourced by that domain, rechecks the Markdown
fingerprint, writes the new fingerprint/commit, marks the domain `ready`, and
commits.

Readers seeing `dirty` or `rebuilding` never trust the previous graph snapshot;
they use the safe Markdown fallback. A handled failure commits `dirty`. A
process crash may leave `rebuilding`; after the OS releases the base lock, the
next process treats that state as an interrupted rebuild and starts it again.
The state transition is therefore observable and durable:

```text
ready -> dirty -> rebuilding -> ready
                         |-> dirty (handled failure)
```

Rebuild enumeration excludes every root-relative name in `RESERVED_OKF`
(`index.md` and `log.md`) exactly as the current adjacency walk does. These
generated files never become pages or edge sources: `index.md` links every
page and would otherwise create an all-domain hub that destroys bounded graph
candidate semantics.

Authored links whose normalized target is a root-level `RESERVED_OKF` name are
also excluded from graph edges, so a reserved artifact cannot become a transit
node through incoming links. This is an intentional tightening of current
behaviour. Lint reports such links as `reserved_target`, not `broken`, because
the generated file may exist but is not a valid knowledge-graph page.

Full database creation or incompatible-schema recovery rebuilds all requested
domains. It never calls the embedding provider.

## 7. Freshness and Git synchronization

### R7 — Freshness watermark

Freshness covers Markdown only; `index.jsonl` and `log.jsonl` changes do not
invalidate graph rows. Each domain records the Git commit used for comparison
and a deterministic fingerprint of its sorted Markdown paths and Git blob
identities. For an uncommitted working tree, Git status first identifies only
dirty/untracked Markdown paths; the server reads and hashes those paths, not
every clean Markdown body.

Before graph use, the server performs a cheap Git/index freshness check rather
than parsing Markdown. An equal fingerprint and clean relevant working tree
means ready. A mismatch marks only affected domains dirty.

### R8 — Managed mutations

`wiki_write_page`, `wiki_update_page`, `wiki_delete_page`, OKF page moves, and
whole-domain conformance sweeps refresh graph state as part of their existing
indexing workflow. Tracked files are prepared before SQLite commit. If a crash
or file error leaves a cross-store mismatch, the next freshness check rebuilds
the derived graph; stale graph rows are never silently trusted.

Failure of Markdown, `log.jsonl`, or `index.jsonl` mutation retains the current
all-or-error rollback behaviour and does not commit graph rows. Failure of the
derived graph update alone is fail-soft: the valid tracked mutation continues
through Git commit/push, the tool returns its normal success payload plus a
sanitized `warning` that graph fallback will be used, and the SQLite
transaction rolls back. The unchanged/missing watermark makes the domain
dirty on its next graph use. Graph failure must not discard successfully
prepared canonical content or portable vectors/provenance.

### R9 — Pull and clone

`wiki_sync` and `ensure_fresh` capture the old and new Git revisions around a
successful pull/fast-forward. They identify changed Markdown paths and refresh
only their source domains before returning success. A new machine with no
database builds graph rows for requested read domains on first use. An
external `git pull` is detected lazily before the next graph-backed query.

A pull-triggered graph refresh runs before the existing base Git lock is
released, without reacquiring it inside graph code. A lazy rebuild acquires
that base lock once around fingerprint/read/recheck plus the SQLite write;
ordinary ready-state SQLite reads do not take the Git lock. Because external
Git processes do not honor the server lock, the rebuild rechecks its Markdown
fingerprint before marking a domain ready. A failed proactive refresh does not
undo a successful pull: sync returns a sanitized warning and later reads use
the safe fallback.

Remote changes are not polled by read search. Search reflects the local Git
checkout; fetching remote state remains the responsibility of existing sync
operations.

## 8. Retrieval

### R10 — Scope-safe traversal

Graph traversal receives the fully resolved domain set from `wiki_search`.
Every seed and every hop is restricted to this set. A link to a domain outside
the effective scope cannot enter the frontier, influence distance/rank, or
disclose target existence.

### R11 — Directed storage, undirected search

Edges are stored in authored direction. `wiki_search` expands over the union
of `source_page_id -> target_page_id` and scoped incoming edges. Ordering stays
deterministic: seeds, distance, seed rank, page identity, then existing section
ordinal/chunk ordering. `IWIKI_GRAPH_DEPTH` and `IWIKI_BFS_TOP_K` keep their
current meanings.

The public result shape and `source="graph"` meaning remain unchanged. RRF,
semantic chunks, lexical sections, facets, hydration, and reranking retain
their current contracts.

### R12 — `wiki_related`

`wiki_related(domain, section_id)` remains domain-local and returns the current
`{"vector": [...], "graph": [...]}` shape. It may use SQLite for its existing
link fallback, but must neither traverse into other domains nor change graph
result serialization.

## 9. Lint and diagnostics

### R13 — Broken links and anchors

Lint remains config-free, read-only, and Markdown-authoritative. It uses the
same normalized structured link parser as graph indexing and always builds the
expected page, anchor, and outgoing-edge sets from current Markdown. It then
performs an independent graph-health check against SQLite; the database never
validates itself and lint never creates, rebuilds, or repairs it.

Broken page targets have no matching visible Markdown page. Broken anchors
have a target page but no matching normalized heading. Cross-domain checks run
only where the target domain is visible to the lint invocation; an unavailable
domain is reported distinctly from a confirmed missing page.

Each domain report adds a `graph` object:

```json
{
  "graph": {
    "available": true,
    "schema_version": 1,
    "state": "ready",
    "fingerprint_match": true,
    "missing_pages": [],
    "extra_pages": [],
    "missing_edges": [],
    "extra_edges": [],
    "anchor_mismatches": []
  }
}
```

Parity compares the invoked domain's expected Markdown pages, all H1-H6
anchors, and authored outgoing edges with its SQLite rows. Missing, corrupt,
busy, or incompatible SQLite returns `available=false` plus a sanitized reason;
it does not suppress ordinary lint findings. Any non-ready state, fingerprint
mismatch, or parity difference includes the remediation hint to run
`wiki_index(domain)`.

Existing orphan semantics remain domain-local unless a later public contract
explicitly makes cross-domain incoming links count toward orphan status.

## 10. Failure and recovery

### R14 — Safe fallback

Missing, corrupt, incompatible, busy beyond timeout, or stale SQLite must not
produce stale graph results. The server attempts transactional repair. If it
cannot repair before the request, it uses the current in-memory Markdown graph
path for that request and leaves the affected domain dirty. If neither SQLite
nor Markdown fallback is safe, the tool returns an actionable error.

SQLite errors must not expose base paths or SQL internals through public MCP
responses. Recovery never deletes Markdown, `index.jsonl`, or `log.jsonl`.

### R15 — Schema migration

`PRAGMA user_version` identifies compatible schema. Version zero initializes
the database. A known migration runs transactionally. An unknown/newer or
corrupt schema is moved aside or recreated only as a derived cache; Markdown
is then re-read. WAL/SHM lifecycle follows SQLite and is never copied or
committed.

## 11. Performance boundary

The target is small and medium wiki bases. A ready query performs indexed edge
lookups and bounded BFS without reading every Markdown body. Rebuild cost is
linear in affected Markdown pages and links, but contains no embedding calls.

This change does not optimize vector loading. Current domain stores are below
the 8 MiB cap and contain at most a few hundred records. A vector SQLite or
`sqlite-vec` migration requires separate benchmark evidence and a separate
approved change. If introduced later, tracked `index.jsonl` remains the
portable vector interchange unless an alternative cross-machine format is
approved.

## 12. Verification

Focused tests must cover:

- schema creation, user-version handling, WAL settings, and corrupt recovery;
- intra-domain, cross-domain, anchor, malformed URI, code-fence, and unsafe
  path parsing;
- directed storage and undirected deterministic traversal;
- scope exclusion on every hop, including a visible -> hidden -> visible path;
- graph-depth and BFS-cap compatibility;
- exclusion of `RESERVED_OKF`, proving generated `index.md` cannot become a
  graph hub or candidate source;
- authored links to root-level reserved artifacts are omitted from edges and
  reported by lint as `reserved_target` rather than `broken`;
- create, update, delete, move, link removal, and broken incoming edges;
- incremental refresh equivalence with a clean full rebuild;
- deterministic duplicate-edge `raw_target` selection and H1-H6 anchor
  extraction from legacy/out-of-band Markdown;
- absent DB, stale commit, manual working-tree edit, managed pull, external
  pull, and failed rebuild fallback;
- observable `ready -> dirty -> rebuilding -> ready` transitions, handled
  failure back to dirty, and restart of a crash-left rebuilding domain after
  base-lock release;
- mutation success plus sanitized warning on graph-only failure, versus
  rollback/error for canonical Markdown/log/vector failure;
- no embedding call during graph-only rebuild;
- unchanged `wiki_related`, result serialization, facets, RRF, and reranking;
- unchanged portable `index.jsonl` and `log.jsonl` behaviour;
- exact graph parity for pages, H1-H6 anchors, and outgoing edges, plus ordinary
  lint equivalence with ready, missing, dirty, rebuilding, busy, incompatible,
  and corrupt graph state;
- root-only local Git exclusion without matching legacy domain `.iwiki/`;
- base-lock coordination and post-read fingerprint recheck around rebuild;
- concurrent readers and serialized writers under WAL mode;
- full pytest, flake8, CLI help, MCP schema smoke, and live two-domain search.

## 13. Expected code and documentation surfaces

- Add `src/iwiki_mcp/engine/graph_store.py` for SQLite schema, transactions,
  freshness state, edge lookup, and rebuild operations.
- Extend `src/iwiki_mcp/engine/links.py` with structured targets and `iwiki://`
  parsing while preserving current compatibility helpers.
- Replace adjacency construction in `src/iwiki_mcp/engine/hier.py` with an
  injected scoped graph lookup; retain an in-memory fallback.
- Integrate freshness and maintenance in `src/iwiki_mcp/retrieval.py`,
  `src/iwiki_mcp/indexer.py`, mutation paths in `src/iwiki_mcp/server.py`,
  `src/iwiki_mcp/okf.py`, and pull paths in `src/iwiki_mcp/sync.py`.
- Extend base-level local-metadata setup to add the root-anchored graph-cache
  pattern to the repository-local Git exclude file without touching tracked
  `.gitignore` content.
- Extend `src/iwiki_mcp/engine/lint.py` for normalized cross-domain findings
  without changing unrelated lint contracts.
- Update English/Russian README, MCP authoring rules/resources, architecture
  docs, version, focused tests, and the bound iwiki domain.

## 14. Done when

Ready-state search performs no full-domain Markdown adjacency scan, scoped
cross-domain expansion works through explicit `iwiki://` links, local graph
state repairs after clone/pull/edit/failure, no graph path leaks an unavailable
domain, JSONL vectors/logs retain current portability, compatibility tests pass,
and repository plus iwiki documentation describe observed behaviour.
