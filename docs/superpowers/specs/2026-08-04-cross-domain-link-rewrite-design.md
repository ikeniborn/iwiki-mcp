---
review:
  spec_hash: 2837c9cfddbb10ac
  last_run: 2026-08-04
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-08-04-cross-domain-link-rewrite-intent.md
---
# Cross-Domain Link Rewrite — Design Specification

**Date:** 2026-08-04
**Status:** approved
**Topic:** `cross-domain-link-rewrite`

## 1. Context

The server stores explicit cross-domain dependencies as
`iwiki://<domain>/<page-id>#<anchor>` links and indexes them in the base-local
SQLite graph. Current page moves rewrite relative links only inside the moved
page's domain. Cross-domain URIs are deliberately left untouched, so moving a
target page or renaming a referenced heading can leave valid authored
knowledge with a broken target.

This design adds permission-scoped reverse-link rewrites while preserving
Markdown as the source of truth, existing URI syntax, legacy single-write
bindings, and fail-soft remote push behavior.

## 2. Acceptance (from intent)

### Desired Outcomes

- Moving a page automatically preserves valid incoming `iwiki://` links from
  other writable domains in the same wiki base.
- Renaming a referenced `##` heading automatically updates incoming URI
  anchors in writable domains.
- After a successful operation, `wiki_lint` reports no broken affected
  cross-domain links and graph parity reports no affected mismatch.
- If the server cannot safely prepare, reindex, or commit every affected
  writable domain, it leaves no partially rewritten Markdown links.

### Done When

- Done when: real cross-domain page-move and anchor-rename scenarios preserve
  valid links; `wiki_lint` and graph parity report no affected failures; and
  required focused and full regression checks pass.

## 3. Design Decisions

### 3.1 Backward-compatible write scope

`Binding.write` remains the primary scalar write domain for compatibility.
`Binding.write_scope` is a new ordered tuple containing every explicitly
writable domain. The primary `write` domain must be present in `write_scope`.

Project configuration accepts:

```toml
read = ["iwiki-mcp", "okf"]
write = "iwiki-mcp"
write_scope = ["iwiki-mcp", "okf"]
```

When `write_scope` is absent, resolution produces `(write,)`, or an empty
tuple when no primary write domain exists. Duplicate domains collapse in
first-seen order. Every member must exist and belong to resolved read scope.
The existing rule that `write` equals the current project domain remains.

`wiki_bind` adds an optional `write_scope: list[str] | None` argument. Omitting
it preserves the existing scope. Passing it validates the complete candidate
configuration before writing `.iwiki.toml`; a validation error leaves the
file byte-identical. `wiki_status` and `wiki_bind` keep the legacy `write`
field and add `write_scope`.

Every tool that modifies content or portable stores in an existing domain must
reject a target outside `write_scope`. `wiki_create_domain` is the explicit
bootstrap exception: it may create an empty domain outside the current scope,
but it must not author pages or portable stores there. The caller can add the
new domain to binding only after creation. Legacy calls remain valid when they
target the primary write domain.

### 3.2 Structured rewrite primitives

`engine.links` owns pure, deterministic URI rewrites. A rewrite request names
the target domain, old page identity, optional old anchor, new page identity,
and optional new anchor.

- A page move matches every cross-domain URI to the exact old page identity
  and preserves each authored anchor.
- A heading rename matches only the exact normalized old anchor and preserves
  the page identity.
- A combined internal representation can express page and anchor changes, but
  the initial MCP operations expose page move and heading rename separately.
- The optional authored `.md` suffix is preserved.
- Visible labels, surrounding prose, query-like text, external URIs, images,
  inline code, and fenced code remain byte-identical.
- Reapplying the same rewrite is a no-op.

The parser output remains the authority for whether a raw target is safe and
matches. SQLite `raw_target` is never edited as canonical content.

### 3.3 Incoming-reference discovery

The coordinator resolves incoming references across the complete resolved
read scope without inspecting hidden domains.

The ready fast path queries SQLite for edges whose target matches the old page
identity and, for heading rename, the old normalized anchor. It then reparses
every returned Markdown page and keeps only exact canonical matches. SQLite
therefore narrows candidate files but never authorizes a rewrite by itself.

If any read-scope domain is absent, stale, rebuilding, corrupt, or otherwise
unsafe in SQLite, discovery takes one scope-qualified Markdown snapshot using
the existing safe file-open/hash rules. It parses links once, revalidates the
snapshot before mutation, and schedules graph repair. A changed snapshot
aborts before the first canonical write with `source_changed`; callers may
retry the complete MCP operation against a new snapshot. The coordinator does
not retry inside the held mutation because that could hide concurrent authored
changes. Ordinary ready-state mutations perform no full-scope Markdown scan.

A matching referrer in a read-only domain produces `write_scope_blocked`
before any canonical file is changed. The result reports only referrers inside
resolved read scope. Hidden-domain existence is neither inspected nor
disclosed.

### 3.4 Cross-domain mutation coordinator

A new top-level module `cross_domain.py` coordinates the feature. It depends
on binding/path helpers, structured link primitives, indexer operations,
graph maintenance, locking, and Git sync. It does not own Markdown syntax,
SQLite schema, embeddings, or Git command construction.

The coordinator builds an immutable plan containing:

- target operation and expected preimage hashes;
- resolved read and write scopes;
- exact referrer pages and replacements;
- affected domains and tracked files;
- index/log snapshots and Git base revision;
- a transaction identifier.

`wiki_apply_okf` delegates page moves to the coordinator. Existing
intra-domain relative-link rewriting remains in `okf.move_page`; the
coordinator adds cross-domain referrers and the shared transaction boundary.

`wiki_update_page` adds `new_heading: str | None = None`. When absent, behavior
is unchanged. When present, the named section is replaced and its `##` heading
is renamed in the same operation. The new heading must normalize to a
non-empty anchor and must not collide with another heading anchor in the page.
Relative anchor links in the target domain and matching cross-domain URIs are
rewritten together.

### 3.5 Lock ownership and exact Git scope

One base mutation lock covers preflight, planning, canonical writes,
indexing, local commit, and journal transition. The coordinator uses internal
lock-aware freshness and commit helpers so nested acquisition cannot deadlock;
existing public sync functions retain their contracts.

Git staging uses an exact ordered path list rather than whole-domain
pathspecs. It includes only changed Markdown plus affected `index.jsonl` and
`log.jsonl` files. Unrelated dirty files are not staged. One local commit
covers all affected domains and carries an `Iwiki-Transaction` trailer.

The existing `ensure_graph_store_excluded()` helper owns the root-only
`/.iwiki/` entry in the base repository's Git `info/exclude`. Before creating
a journal, the coordinator verifies that exclusion for a Git base. Failure to
establish it aborts before canonical writes. This keeps journals, snapshots,
and `graph.sqlite3` out of both exact staging and unrelated `git add -A`
operations without excluding legacy `<domain>/.iwiki/` paths.

Remote push remains outside the rollback boundary. A push failure returns the
existing sanitized warning while the valid local commit remains.

### 3.6 Derived graph boundary

Before a confirmed local commit, graph preparation belongs to the rollback
boundary. After the local commit, all affected graph pages/domains refresh in
one SQLite transaction where possible. A graph-only failure does not roll back
canonical Markdown, vectors, logs, or the local Git commit. Instead, the
coordinator must invalidate the affected graph state before finalizing: mark
every affected domain `dirty`, then verify that the normal readiness gate
rejects the old Markdown fingerprint. If a dirty-state write itself fails, the
canonical fingerprint mismatch remains the mandatory invalidation signal and
must still make the readiness check fail. The response includes the existing
sanitized graph fallback warning, and later reads rebuild or use safe Markdown
fallback. Thus no stale ready graph survives a successful canonical commit.

No SQLite schema migration is required: existing directed edges and
`raw_target` contain the reverse-lookup information. The implementation adds
query helpers and batch-refresh orchestration only.

## 4. Mutation Flow

### 4.1 Preflight

1. Resolve binding and validate target domain membership in `write_scope`.
2. Acquire the base mutation lock and complete freshness checks.
3. Establish and verify the root `/.iwiki/` Git exclusion.
4. Recover or finalize any earlier unfinished cross-domain transaction.
5. Validate target existence, old heading, new identity/heading, and
   collisions.
6. Discover and canonically verify incoming referrers across read scope.
7. Abort with `write_scope_blocked` if any exact referrer is read-only.
8. Capture expected hashes and construct replacement bytes entirely in
   memory.

### 4.2 Prepare and apply

1. Create `<base>/.iwiki/transactions/<id>/` with an fsynced manifest and
   byte snapshots for every path that can change.
2. Mark journal state `prepared`.
3. Revalidate every expected source hash.
4. Apply target move/heading rename and referrer replacements using temporary
   sibling files followed by `os.replace`.
5. Mark journal state `applied`.
6. Reindex only affected domains. Existing chunk hashes reuse unchanged
   vectors; only changed chunks call the embedding provider.
7. Stage the exact affected path list and create one local commit.
8. Mark journal state `committed`, recording the commit revision.
9. Refresh derived graph state. On failure, persist dirty state when possible
   and verify that canonical fingerprint mismatch makes stale rows unavailable.
10. Mark the journal `finalized`, then remove the transaction directory after
   either graph refresh or safe invalidation succeeds.
11. Run existing sync/push behavior.

### 4.3 Failure rollback

Any exception before a confirmed local commit restores snapshots, removes
new move paths, validates restored hashes, repairs affected graph state, and
removes the journal only after recovery is confirmed. The response reports
`mutation_failed` and `rolled_back: true`.

If restored hashes cannot be confirmed, the journal is retained and all later
mutations stop with `manual_recovery_required`. The server never guesses over
an unexpected file or Git revision.

## 5. Crash Recovery

The local journal uses these states:

```text
prepared -> applied -> committed -> finalized
```

Recovery runs before every mutating tool that can overlap affected wiki data:

- `prepared` or `applied`, with HEAD equal to `base_head`: restore snapshots
  and remove created paths.
- `committed`, or HEAD containing the matching `Iwiki-Transaction` trailer:
  keep canonical files, attempt graph repair, then finalize after either
  repair or a verified fingerprint/dirty invalidation succeeds. A repeated
  graph repair failure therefore does not block every later mutation.
- HEAD or file hashes inconsistent with both recorded preimage and committed
  transaction: retain the journal and return `manual_recovery_required`.

Recovery is idempotent. Transaction directories live under the root
`/.iwiki/` path excluded by `ensure_graph_store_excluded()`; they never become
portable state or a second authoring source.

## 6. MCP Contracts

### 6.1 Binding

```python
wiki_bind(
    read: list[str] | None = None,
    write: str | None = None,
    write_scope: list[str] | None = None,
) -> dict
```

Successful binding/status responses include:

```json
{
  "read": ["iwiki-mcp", "okf"],
  "write": "iwiki-mcp",
  "write_scope": ["iwiki-mcp", "okf"]
}
```

### 6.2 Heading rename

```python
wiki_update_page(
    domain: str,
    slug: str,
    heading: str,
    new_body: str,
    source: str | None = None,
    description: str | None = None,
    status: str | None = None,
    new_heading: str | None = None,
) -> dict
```

The response keeps existing fields and adds cross-domain transaction evidence
when a rename or rewrite occurs.

### 6.3 Successful mutation evidence

```json
{
  "rewritten_pages": ["iwiki-mcp/okf-governance.md"],
  "affected_domains": ["iwiki-mcp", "okf"],
  "rewritten_links": 1,
  "transaction_id": "<opaque-id>"
}
```

`rewritten_pages` contains only referrer pages whose link text changed.
`affected_domains` contains every transaction domain, including the moved or
renamed target domain. Lists are deterministic and contain only read-scope
identities.

### 6.4 Error classes

- `write_scope_blocked`: exact incoming referrers exist in read-only domains;
  no files changed.
- `target_collision`: the page destination already exists; no files changed.
- `heading_collision`: another heading normalizes to the requested anchor; no
  files changed.
- `source_changed`: the validated Markdown snapshot changed before mutation;
  no files changed and the caller may retry the complete operation.
- `mutation_failed` with `rolled_back: true`: pre-commit failure recovered.
- `manual_recovery_required`: journal/file/Git state cannot be reconciled
  automatically; no new mutation starts.

Graph and push failures after a confirmed local commit remain warnings, not
these pre-commit error classes.

## 7. Requirements and Definitions of Done

### R1 — Multi-domain write binding

The server shall persist, resolve, report, and enforce an explicit
`write_scope` while accepting legacy scalar-write configuration.

DoD: focused config/server tests prove legacy behavior, deterministic scope
ordering, read-subset validation, byte-identical config on error, and target
rejection outside write scope for existing-domain mutations. They also prove
that `wiki_create_domain` can bootstrap an empty unbound domain without
creating portable stores. Compatibility explicitly excludes the former
partial enforcement that allowed some existing-domain mutation handlers to
target an unbound domain.

### R2 — Exact cross-domain rewrite

The link engine shall rewrite only structurally parsed URIs matching the old
page and optional old anchor while preserving all unrelated bytes.

DoD: table-driven parser/rewrite tests cover page, anchor, combined internal
mapping, optional `.md`, code, images, external URIs, mismatches, and
idempotence.

### R3 — Complete scoped referrer discovery

The coordinator shall discover exact incoming referrers across resolved read
scope using ready SQLite edges or one validated Markdown snapshot fallback.

DoD: tests prove zero full-scope scan on ready graph, complete fallback on
stale/missing/corrupt graph, canonical candidate revalidation, and zero
hidden-domain access/disclosure.

### R4 — Write-scope blocker

The operation shall reject before its first canonical write when an affected
referrer is readable but not writable.

DoD: a controlled multi-domain test returns `write_scope_blocked`, reports
only visible referrers, and leaves all files/hashes/HEAD unchanged.

### R5 — Atomic page move

`wiki_apply_okf` page moves shall preserve matching incoming cross-domain URIs
and existing intra-domain relative links in one transaction.

DoD: a real temporary Git-base scenario moves a target, rewrites multiple
domains, creates one exact-scope commit, and produces clean link lint and graph
parity.

### R6 — Atomic heading rename

`wiki_update_page(new_heading=...)` shall rename the selected heading and
rewrite exact relative and cross-domain anchor referrers.

DoD: tests cover successful rename, same-anchor no-op, missing old heading,
empty normalized anchor, collision, unrelated anchors, and clean post-mutation
lint/parity.

### R7 — Failure rollback

Every failure before local commit shall restore byte-identical Markdown,
vectors, logs, and old/new page paths, and shall leave Git HEAD unchanged.

DoD: fault injection at each file-write, domain-index, staging, and commit
boundary proves restored hashes, unchanged Git HEAD, and no residual staged
files.

### R8 — Crash recovery

Durable local journals shall recover uncommitted mutations, finalize committed
ones, and block ambiguous state without data loss.

DoD: prepared/applied/committed/unexpected-HEAD fixtures prove idempotent
recovery and retained evidence for manual recovery. Git-base tests also prove
root `/.iwiki/` exclusion, including an unrelated `git add -A`, without
excluding legacy domain-local `.iwiki/` paths.

### R9 — Derived graph safety

Graph batch refresh failure after commit shall make all affected domains
unavailable through dirty state or canonical fingerprint mismatch and preserve
safe Markdown fallback.

DoD: fault injection returns success plus sanitized warning, no stale graph is
trusted, finalizes the journal after dirty-state persistence or graph
fingerprint invalidation, and lets a later repair restore exact parity without
embedding calls.

### R10 — Git and push behavior

All affected tracked files shall land in one local transaction commit; remote
push remains fail-soft.

DoD: tests prove exact path staging, unrelated dirty files excluded, one commit
for all domains, transaction trailer presence, and no local rollback on push
failure.

### R11 — Compatibility

Existing URI syntax, single-write bindings, ordinary `wiki_update_page`,
intra-domain move, retrieval scope, and related-search contracts shall remain
compatible. The intentional exception is consistent write-scope enforcement:
mutation handlers that previously accepted an explicitly unbound domain now
return the documented scope error. `wiki_create_domain` retains its documented
empty-domain bootstrap behavior outside that enforcement boundary.

DoD: existing focused suites and the full pytest suite pass without an
unexplained regression.

### R12 — Documentation

Repository docs, English/Russian user docs, `CLAUDE.md`, templates, authoring
resource, and the bound `iwiki-mcp` wiki shall describe multi-write binding,
automatic cross-domain rewrite, blockers, recovery, and fail-soft boundaries.

DoD: documentation/resource tests pass; bound wiki pages are updated through
MCP; `wiki_lint` has no stale changed-source page, broken affected link, or
graph parity mismatch.

## 8. Security and Data Integrity

- Candidate discovery and responses are limited to resolved read scope.
- Canonical mutation is limited to explicit write scope.
- All paths use existing domain/page containment validation and safe file-open
  rules; journals store only files already inside the wiki base.
- Journal identifiers are opaque and contain no paths or credentials.
- Git output and graph warnings use existing sanitization.
- SQLite cannot authorize content mutation without matching Markdown bytes.
- Hidden domains cannot become blockers because inspecting them would disclose
  their existence; their links are outside the caller's observable guarantee.

## 9. Verification Matrix

### Link and section engine

- Exact page/anchor rewrites and negative syntax cases.
- Heading replacement/rename, normalized-anchor collision, and idempotence.

### Binding and server contracts

- Legacy/new config round trips and MCP schema compatibility.
- Write-scope enforcement on every existing-domain mutation plus the explicit
  empty-domain `wiki_create_domain` bootstrap exception.
- Deterministic success and blocker result shapes.

### Coordinator and graph

- Ready incoming-index fast path with scan guard.
- Stale/corrupt/missing fallback snapshot and post-snapshot race rejection.
- Multi-domain move/rename, read-only blocker, and hidden-domain isolation.
- Graph batch success, dirty fallback, and later repair.

### Transaction and Git

- Fault injection before/after every durable transition.
- Byte/hash/HEAD/staging equivalence after rollback.
- Journal crash recovery and ambiguous-state stop.
- Root `/.iwiki/` exclusion under unrelated `git add -A`.
- Exact multi-domain commit and fail-soft push.

### Final commands and observable scenarios

- Focused pytest files for links, binding, move/update, graph, sync, and lint.
- `uv run pytest -q`.
- `uv run flake8 src tests`.
- `uv run python -m compileall -q src`.
- `uv run iwiki-mcp --help` and `uv build`.
- Temporary multi-domain Git-base page-move and heading-rename scenarios with
  `wiki_lint` and exact graph parity.
- Bound wiki update followed by `wiki_lint`.

## 10. Documentation Impact

- `docs/architecture.md`: coordinator, write scope, journal, and transaction
  boundary.
- `README.md` and `docs/README.ru.md`: `wiki_bind(write_scope=...)`, heading
  rename, blockers, and recovery behavior.
- `CLAUDE.md`: current docs layout, domain-root portable stores, and the new
  binding/rewrite boundary.
- `templates/AGENTS.md.snippet` and `templates/CLAUDE.md.snippet`: authoring and
  move/rename guidance.
- `resources.AUTHORING_RULES`: automatic rewrite and writable-scope limits.
- Bound wiki: architecture, base binding, authoring/linting, MCP server, sync,
  and OKF governance sections as applicable.

## 11. Non-Goals

- Rewriting links in hidden or read-only domains.
- Moving pages between domains.
- Changing `iwiki://` syntax or SQLite schema.
- Making remote push transactional.
- A generic transaction framework for unrelated write/delete/export tools.
- Automatic semantic link creation.

## 12. Success Criteria

The design is satisfied when R1–R12 are evidenced, every Desired Outcome and
the Done When scenario pass against real temporary multi-domain data, no
Health Metric regresses, documentation is current, and result reconciliation
returns `OK` against the approved implementation plan.
