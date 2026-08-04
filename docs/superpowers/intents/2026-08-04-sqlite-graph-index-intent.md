---
review:
  intent_hash: 32d98cf3ae07803c
  last_run: 2026-08-04
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings:
    - id: F-001
      phase: structure
      severity: CRITICAL
      section: "document"
      section_hash: 258e288653dacfeb
      fragment: null
      text: "Required Health Metrics, Strategic Context, and Autonomy Zones sections are missing."
      fix: "Add the three required English template sections."
      verdict: fixed
      verdict_at: 2026-08-04
    - id: F-002
      phase: completeness
      severity: CRITICAL
      section: "Constraints"
      section_hash: becfb3313bf7b08d
      fragment: null
      text: "Constraints are not bound to steering XOR hard."
      fix: "Split every constraint into exactly one Steering or Hard subsection."
      verdict: fixed
      verdict_at: 2026-08-04
    - id: F-003
      phase: completeness
      severity: CRITICAL
      section: "document"
      section_hash: 258e288653dacfeb
      fragment: null
      text: "Autonomy Zones do not cover all four required zones."
      fix: "Add Full, Guarded, Proposal-first, and No autonomy zones."
      verdict: fixed
      verdict_at: 2026-08-04
    - id: F-004
      phase: completeness
      severity: CRITICAL
      section: "Stop Rules"
      section_hash: df2ba97189d2248b
      fragment: null
      text: "Stop Rules contain no measurable Done when criterion."
      fix: "Add an observable Done when criterion."
      verdict: fixed
      verdict_at: 2026-08-04
    - id: F-005
      phase: completeness
      severity: CRITICAL
      section: "document"
      section_hash: 258e288653dacfeb
      fragment: null
      text: "Health Metrics are absent."
      fix: "Add non-empty named metrics."
      verdict: fixed
      verdict_at: 2026-08-04
    - id: F-006
      phase: completeness
      severity: CRITICAL
      section: "document"
      section_hash: 258e288653dacfeb
      fragment: null
      text: "Strategic Context lacks Interacts with and Priority trade-off."
      fix: "Add both required context anchors."
      verdict: fixed
      verdict_at: 2026-08-04
    - id: F-007
      phase: consistency
      severity: CRITICAL
      section: "document"
      section_hash: 258e288653dacfeb
      fragment: "**Status:** approved"
      text: "Document is approved while open CRITICAL findings remain."
      fix: "Resolve all CRITICAL source findings and rerun the intent stage."
      verdict: fixed
      verdict_at: 2026-08-04
    - id: F-008
      phase: alignment
      severity: WARNING
      section: "document"
      section_hash: 258e288653dacfeb
      fragment: null
      text: "Missing Health Metrics do not track components documented for retrieval and indexing."
      fix: "Add metrics for graph scans, rebuild embeddings, parity, scope isolation, and regressions."
      verdict: fixed
      verdict_at: 2026-08-04
workflow:
  route: chain
  continuation: full
---

# Intent: sqlite-graph-index

**Date:** 2026-08-04
**Status:** approved

## Objective

Design and implement a persistent SQLite-backed wiki link graph for small and
medium iwiki domains. The graph must replace per-query full-domain link scans
as the source used for graph expansion, support scope-safe traversal across
explicitly linked domains, and remain reproducible from the Markdown pages.

## Desired Outcomes

- `wiki_search` continues to combine semantic, lexical, and graph-derived
  candidates, but obtains graph neighbours from an incrementally maintained
  index rather than reparsing every Markdown file on each request.
- A page identity is unambiguous across domains, and an authored cross-domain
  link can participate in graph expansion only when its target domain is in
  the caller's resolved read scope.
- Writes, updates, deletes, and page moves maintain graph entries
  consistently; a full `wiki_index` rebuild can recreate the graph from the
  Markdown source of truth.
- `wiki_lint` can diagnose broken intra- and cross-domain links using the same
  normalized link model.
- Existing intra-domain Markdown and legacy wiki links remain supported.

## Health Metrics

- Ready-state graph expansion performs zero full-domain Markdown adjacency
  scans, proven by a focused regression test.
- A graph-only rebuild performs zero embedding-provider requests.
- Incremental refresh and a clean full rebuild produce zero page, anchor, or
  edge parity differences on the same Markdown snapshot.
- Scope-isolation tests produce zero hidden-domain hits and zero paths that
  become reachable only through a hidden domain.
- Existing retrieval, indexing, mutation, related, lint, MCP schema, and full
  pytest suites pass without an unexplained regression.

## Strategic Context

- Interacts with: Markdown link parsing, hierarchical retrieval, project/domain
  binding, vector JSONL stores, ingest logs, write/update/delete and OKF moves,
  Git synchronization, lint, MCP authoring guidance, and project wiki docs.
- Priority trade-off: scope isolation and recoverable correctness first,
  cross-machine vector/provenance portability second, ready-query latency
  third, then implementation complexity.

## Constraints

### Steering

- Prefer Python's standard-library `sqlite3` over a graph database, ORM, or
  vector extension while the small/medium-domain target remains satisfied.
- Preserve tracked per-domain `index.jsonl` as the portable embedding snapshot
  and `log.jsonl` as portable provenance rather than mirroring them into the
  graph database.
- Keep graph expansion bounded by configured hop depth and candidate budget,
  with deterministic ordering for equal candidates.

### Hard

- Markdown pages and their links remain canonical. SQLite is a derived,
  rebuildable index and must not become an authoring source of truth.
- Preserve current project binding semantics: explicit `domains` overrides
  `scope`; `scope="project"` uses bound read domains; `scope="all"` uses all
  base domains.
- Traversal must never return, rank, or disclose a page in a domain outside
  the resolved scope, including through a visible-domain link.
- Keep current `wiki_related` behaviour compatible unless an approved design
  explicitly changes its public result contract.
- Include the repository-required version bump, focused regression tests,
  public English/Russian documentation, and iwiki documentation updates for
  any implemented behaviour change.

## Autonomy Zones

- Full autonomy: choose internal names, SQL helper boundaries, deterministic
  test fixtures, and documentation wording that preserve approved contracts.
- Guarded: tune SQLite busy timeout, rebuild batching, or cache checks only
  from recorded concurrency and latency evidence, preserving safe fallback.
- Proposal-first: change the `iwiki://` public syntax, MCP result/schema,
  `wiki_related` domain scope, JSONL portability, SQLite placement, or add an
  external database/vector dependency.
- No autonomy: broaden a caller's read scope, expose hidden-domain existence,
  mutate credentials/remotes, or delete canonical Markdown as cache recovery.

## Non-Goals

- Distributed graph storage, graph analytics, PageRank, arbitrary graph query
  language, or a new public graph-query MCP tool.
- Automatic cross-domain link creation or changing a caller's domain access.
- Replacing vector, lexical, fusion, or reranking retrieval signals.
- Migrating existing authoring content until a canonical cross-domain link
  syntax has been approved.

## Acceptance Criteria

- Controlled tests prove graph expansion works without scanning unrelated
  Markdown files after a valid graph index exists.
- Controlled tests prove an indexed cross-domain edge is traversable only when
  its target belongs to the effective search scope.
- Controlled tests prove updates, deletes, and full rebuilds produce the same
  graph state expected from the current Markdown content.
- Corrupt, absent, or stale SQLite data fails safely by rebuilding or taking a
  documented fallback path; it must not yield unauthorized or stale results.
- Existing intra-domain search, `wiki_related`, indexing, and lint regression
  coverage remains green.

## Design Questions for Full Continuation

The approved design resolves these questions; they remain listed here as the
required decision record for the `full` continuation:

- Exact database placement and lifecycle: shared-base graph database versus a
  per-domain database plus a cross-domain edge registry.
- Canonical syntax and parser representation for cross-domain links.
- Atomicity boundary among page mutation, vector index, ingest log, graph
  update, and git commit; especially recovery after a partial process failure.
- Whether `wiki_related` should expose cross-domain graph neighbours or retain
  its current domain-local API contract.

## Stop Rules

- Stop and return to intent/spec review if scope filtering cannot be guaranteed
  at every graph traversal boundary.
- Stop if the chosen SQLite lifecycle cannot be rebuilt deterministically from
  Markdown without hidden state.
- Escalate for approval before adding a public MCP graph-query API, changing
  existing link syntax semantics, or introducing a non-stdlib dependency.
- Done when: ready search uses the scoped SQLite graph without a full-domain
  adjacency scan; cross-domain scope-isolation and incremental/full parity
  checks have zero mismatches; graph rebuild uses zero embedding calls; all
  required focused/full tests and documentation checks pass; and
  `$check-chain result` returns `OK` against the approved plan.
