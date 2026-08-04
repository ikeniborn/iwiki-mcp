---
review:
  plan_hash: c1d8812080f241be
  last_run: 2026-08-04
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-08-04-sqlite-graph-index-intent.md
  spec: docs/superpowers/specs/2026-08-04-sqlite-graph-index-design.md
result_check:
  verdict: needs_work
  source: plan
  plan_hash: c1d8812080f241be
  last_run: 2026-08-04
  reviewed: true
  docs_checked: true
---

# SQLite Graph Index — Implementation Plan

**Date:** 2026-08-04
**Status:** proposed
**Topic:** `sqlite-graph-index`

## Goal

Replace ready-state per-query Markdown adjacency scans with a base-wide local
SQLite graph, add scope-safe cross-domain `iwiki://` edges, retain current
search ranking and domain JSONL portability, and make graph freshness,
recovery, and lint parity observable and testable.

## Source contracts

- Intent: `docs/superpowers/intents/2026-08-04-sqlite-graph-index-intent.md`
- Design: `docs/superpowers/specs/2026-08-04-sqlite-graph-index-design.md`
- Branch/topic: `dev-sqlite-graph-index` / `sqlite-graph-index`

Implementers must not revise accepted intent or design decisions. Return any
contract conflict to the spec gate. Every repository-changing task bumps the
patch version before its commit, matching repository policy.

## Architecture and dependency order

```text
T1 -> T2 -> T3 -> T4 -> T5
T3 + T4 + T5 -> T6
T1 + T5 -> T7
T1 + T2 + T3 + T4 -> T8
T5 + T6 + T7 + T8 -> T9
T1..T9 -> T10
```

No task may start before its dependencies pass focused tests. Write each
behavioural regression test first, observe the intended failure, implement the
smallest change, then rerun focused tests.

## Task 1 — Structured link and anchor model

**Closes:** R1, R2, R3 and the shared parser portion of R13.

**Expected output:** one compatibility-preserving structured parser that emits
safe intra/cross-domain targets, H1-H6 anchors, and reserved-target metadata.

**Files**

- Modify: `src/iwiki_mcp/engine/links.py`
- Modify: `tests/engine/test_links.py`
- Modify: `tests/test_links_rewrite.py`
- Modify: `pyproject.toml`

**Steps**

1. Add failing tests for a structured link target carrying source domain,
   target domain/page/anchor, raw target, and `intra|cross` kind.
2. Cover relative `.md`, legacy wikilinks, valid `iwiki://`, fragments,
   code-fence/inline-code exclusion, image/external-link exclusion, and unsafe
   URI components: query, user-info, port, decoded separator, empty authority,
   empty path, `.`, and `..`.
3. Add failing tests that root-level `RESERVED_OKF` targets are classified as
   reserved and omitted from graph edges; `concept/index.md` remains valid.
4. Add H1-H6 anchor extraction tests, including deep legacy headings,
   duplicates, code blocks, and earliest-heading diagnostic selection.
5. Implement a small immutable structured target type and parsing helpers.
   Preserve `parse_links`, `to_markdown_links`, `rewrite_link_targets`, and
   existing intra-domain return values for compatibility callers.
6. Make duplicate normalized targets deterministic: select the
   lexicographically smallest `raw_target`.
7. Run focused tests and the existing link/OKF/lint suites.

**Verify**

```bash
uv run pytest -q tests/engine/test_links.py tests/test_links_rewrite.py tests/engine/test_lint.py tests/test_apply_move.py tests/test_okf_artifacts.py
```

**Commit**

```text
feat: add structured cross-domain wiki links
```

## Task 2 — SQLite schema and local-cache lifecycle

**Dependencies:** T1

**Closes:** storage ownership and schema portions of R1, R4, R14, and R15.

**Expected output:** a versioned local SQLite store with exact schema,
transaction helpers, safe initialization, and root-only local Git exclusion.

**Files**

- Create: `src/iwiki_mcp/engine/graph_store.py`
- Create: `tests/engine/test_graph_store.py`
- Modify: `src/iwiki_mcp/base.py`
- Modify: `tests/test_base.py`
- Modify: `pyproject.toml`

**Steps**

1. Add failing tests for graph path
   `<base>/.iwiki/graph.sqlite3`, directory creation, standard-library SQLite,
   WAL, `synchronous=NORMAL`, foreign keys, busy timeout, and schema version.
2. Add failing schema tests for `domains`, `pages`, `anchors`, `edges`, unique
   constraints, source-page cascade, target preservation, and
   `edges_target_idx`. Do not add the redundant `pages(domain)` index.
3. Add failing tests for state values `ready`, `dirty`, and `rebuilding`.
4. Add an idempotent local-exclude helper using
   `git rev-parse --git-path info/exclude` and root-anchored `/.iwiki/`.
   Test non-Git bases, repeated initialization, linked-worktree Git paths,
   absence of tracked `.gitignore` changes, and no match for
   `<domain>/.iwiki/`. Keep `tests/test_store_migration.py` as an intentional
   regression guard proving the new root exclusion does not break existing
   legacy domain JSONL migration.
5. Implement connection setup, transactional schema initialization, typed
   store operations, read snapshots, and sanitized store exceptions. Keep the
   module free of MCP/framework dependencies.
6. Add known-version migration seams and detection for incompatible/newer or
   corrupt databases without touching wiki content.

**Verify**

```bash
uv run pytest -q tests/engine/test_graph_store.py tests/test_base.py tests/test_store_migration.py
```

**Commit**

```text
feat: add local SQLite graph store
```

## Task 3 — Markdown snapshots and observable rebuild state

**Dependencies:** T1, T2

**Closes:** R4, R5, R6 and rebuild-state portions of R14–R15.

**Expected output:** deterministic domain snapshots plus equivalent full and
incremental refresh with durable `dirty/rebuilding/ready` transitions.

**Files**

- Modify: `src/iwiki_mcp/engine/graph_store.py`
- Modify: `tests/engine/test_graph_store.py`
- Modify: `pyproject.toml`

**Steps**

1. Add failing tests for deterministic domain snapshots containing pages,
   H1-H6 anchors, directed outgoing edges, content hashes, link hashes, and
   cross-domain target identities.
2. Prove `index.md` and `log.md` are neither pages nor edge sources, and links
   targeting them are omitted. Prove reserved artifacts cannot connect two
   authored pages at depth two.
3. Add failing tests for full rebuild and incremental page refresh equivalence,
   including duplicate links, link removal, target deletion, and a moved page.
4. Implement the observable state sequence under the caller-held base lock:
   commit `dirty`, commit `rebuilding`, build the replacement in memory, then
   atomically replace domain rows and commit `ready` with its fingerprint.
5. On handled failure, commit `dirty`. Treat a pre-existing `rebuilding` state
   as an interrupted rebuild that the next lock owner restarts.
6. Ensure readers reject both dirty and rebuilding snapshots. Verify WAL
   readers retain a consistent old snapshot while replacement rows are
   uncommitted.
7. Prove graph rebuild never imports or calls embedding code.

**Verify**

```bash
uv run pytest -q tests/engine/test_graph_store.py tests/engine/test_hier.py tests/engine/test_embed.py
```

**Commit**

```text
feat: rebuild domain graph snapshots
```

## Task 4 — Git freshness, locking, and recovery coordinator

**Dependencies:** T2, T3

**Closes:** R7, R9 and freshness/fallback portions of R8 and R14.

**Expected output:** a coordinator that cheaply validates Markdown freshness,
serializes rebuilds, refreshes managed pulls, and supplies safe fallback.

**Files**

- Create: `src/iwiki_mcp/graph.py`
- Create: `tests/test_graph_runtime.py`
- Modify: `src/iwiki_mcp/sync.py`
- Modify: `tests/test_sync.py`
- Modify: `tests/test_ensure_fresh.py`
- Modify: `tests/test_server_fresh.py`
- Modify: `tests/test_lock.py`
- Modify: `tests/test_sync_concurrency.py`
- Modify: `tests/test_sync_parallel.py`
- Modify: `pyproject.toml`

**Steps**

1. Add failing tests for a deterministic Markdown-only fingerprint based on
   sorted Git paths/blob identities. Changes to `index.jsonl` or `log.jsonl`
   must not invalidate graph state.
2. Add dirty/untracked/deleted/renamed Markdown tests. Use Git status to select
   only exceptional paths for working-tree content hashing; do not read clean
   Markdown bodies during a ready-state check.
3. Add tests for missing/non-Git bases, absent DB, incompatible/corrupt DB,
   external pull, unavailable prior commit, and schema replacement.
4. Implement a top-level coordinator that resolves fingerprints, acquires the
   existing base lock exactly once for lazy rebuild, invokes graph-store
   transactions, rechecks the fingerprint before ready, and exposes a scoped
   graph provider or Markdown fallback.
5. Capture old/new revisions around successful `sync` pull and
   `ensure_fresh` fast-forward. Return internal changed-Markdown domain data to
   callers without leaking private paths into public responses.
6. Keep pull-triggered refresh inside the already-held base lock and prohibit
   graph code from reacquiring it. On proactive refresh failure, preserve the
   successful pull and return a sanitized warning.
7. Add multi-process tests proving base-lock serialization, SQLite busy
   handling, crash-left rebuilding recovery, and ordinary ready reads without
   the Git lock.
8. Retain the current in-memory Markdown adjacency builder as the correctness
   fallback; never use a stale SQLite snapshot.

**Verify**

```bash
uv run pytest -q tests/test_graph_runtime.py tests/test_sync.py tests/test_ensure_fresh.py tests/test_server_fresh.py tests/test_lock.py tests/test_sync_concurrency.py tests/test_sync_parallel.py
```

**Commit**

```text
feat: coordinate graph freshness and recovery
```

## Task 5 — Global scoped graph expansion in retrieval

**Dependencies:** T3, T4

**Closes:** R10, R11 and retrieval/fallback portions of R6, R7, and R14.

**Expected output:** one post-aggregation, scope-safe cross-domain BFS feeding
current RRF signals without ready-state Markdown adjacency scans.

**Files**

- Modify: `src/iwiki_mcp/engine/hier.py`
- Modify: `src/iwiki_mcp/retrieval.py`
- Modify: `src/iwiki_mcp/server.py`
- Modify: `tests/engine/test_hier.py`
- Modify: `tests/engine/test_hier_adjacency.py`
- Modify: `tests/test_retrieval.py`
- Modify: `tests/test_server_search.py`
- Modify: `tests/test_server_search_write_intent.py`
- Modify: `tests/test_server_search_facets.py`
- Modify: `tests/test_retrieval_facets.py`
- Modify: `pyproject.toml`

**Steps**

1. Add failing tests proving ready-state graph expansion performs no
   `rglob/read_text` adjacency scan and falls back safely when graph state is
   missing, dirty, rebuilding, busy, or corrupt.
2. Add two-domain fixtures with semantic and lexical seeds, forward/reverse
   cross-domain edges, depth two, deterministic ties, and candidate caps.
3. Add the critical scope fixture `visible A -> hidden B -> visible C`; prove B
   never enters the frontier and cannot make C reachable or affect rank.
4. Refactor per-domain signal preparation to return domain context: eligible
   sections, semantic seeds, lexical seeds, and non-graph signals. Do not run
   BFS inside `_domain_signals`.
5. After all domain contexts are collected, create globally qualified seeds,
   perform one scoped graph expansion across the resolved domain set, then map
   graph pages back to eligible per-domain section records.
6. Preserve facet filtering: an expanded page contributes only eligible
   sections. Preserve `source`, `hit`, RRF, hydration, reranker, threshold,
   top-k, and deterministic ordering contracts.
7. Update `locate_target` to use the same ready domain-local provider or safe
   fallback, eliminating its full-domain scan without changing write-intent
   public behaviour.
8. Keep an injected neighbor-provider seam in framework-free hierarchy code;
   hierarchy logic must not open SQLite or resolve bindings itself.

**Verify**

```bash
uv run pytest -q tests/engine/test_hier.py tests/engine/test_hier_adjacency.py tests/test_retrieval.py tests/test_retrieval_facets.py tests/test_server_search.py tests/test_server_search_write_intent.py tests/test_server_search_facets.py tests/test_locate_target.py tests/engine/test_rerank.py
```

**Commit**

```text
feat: use scoped SQLite graph retrieval
```

## Task 6 — Mutation, indexing, OKF, and sync integration

**Dependencies:** T3, T4, T5

**Closes:** R5, R8, R9 and mutation/recovery portions of R6 and R14.

**Expected output:** canonical mutation paths that maintain graph state,
preserve JSONL portability, and fail soft only for derived-graph errors.

**Files**

- Modify: `src/iwiki_mcp/engine/store.py`
- Modify: `src/iwiki_mcp/indexer.py`
- Modify: `src/iwiki_mcp/server.py`
- Modify: `src/iwiki_mcp/okf.py`
- Modify: `src/iwiki_mcp/sync.py`
- Modify: `tests/test_indexer.py`
- Modify: `tests/test_server_write.py`
- Modify: `tests/test_server_update.py`
- Modify: `tests/test_server_delete.py`
- Modify: `tests/test_okf_server.py`
- Modify: `tests/test_apply_move.py`
- Modify: `tests/test_server_migrate.py`
- Modify: `tests/test_export_okf.py`
- Modify: `tests/test_migrate_layout.py`
- Modify: `tests/test_export_only_artifacts.py`
- Modify: `tests/test_server_fresh.py`
- Modify: `tests/test_server_lint_sync.py`
- Modify: `pyproject.toml`

**Steps**

1. Add RED tests for create/update/delete, whole-domain `wiki_index`, OKF
   apply/move/migrate/export, link rewrite, and sync-pulled domain refresh.
2. Make JSONL store replacement atomic through a same-directory temporary
   file plus `os.replace`, preserving current serialization and reuse logic.
3. Stage canonical Markdown/log/vector mutations before graph commit. Preserve
   existing rollback/error behaviour when those tracked artifacts fail.
4. Refresh only affected graph pages for ordinary mutations; rebuild a domain
   for whole-domain index/sweep/migration operations. Finalize the committed
   Markdown fingerprint after the local Git commit without reparsing links.
5. Add graph-only failure tests: roll back the SQLite transaction, continue
   canonical Git commit/push, return the normal success payload plus sanitized
   fallback warning, and prove the next graph use detects stale state.
6. Compose graph, freshness, frontmatter, and sync warnings deterministically
   without exposing paths or overwriting a more actionable existing warning.
7. Prove a push failure leaves graph aligned with the local committed working
   tree, while a later pull/rebase refreshes changed domains under the base
   lock.

**Verify**

```bash
uv run pytest -q tests/test_indexer.py tests/test_server_write.py tests/test_server_update.py tests/test_server_delete.py tests/test_okf_server.py tests/test_apply_move.py tests/test_server_migrate.py tests/test_export_okf.py tests/test_migrate_layout.py tests/test_export_only_artifacts.py tests/test_server_fresh.py tests/test_server_lint_sync.py tests/test_sync.py
```

**Commit**

```text
feat: maintain graph across wiki mutations
```

## Task 7 — Preserve `wiki_related` compatibility

**Dependencies:** T1, T5

**Closes:** R12 and the domain-local compatibility constraint from the intent.

**Expected output:** unchanged `wiki_related` MCP schema and vector-first,
domain-local graph fallback despite structured cross-domain parsing.

**Files**

- Modify only if required: `src/iwiki_mcp/engine/related.py`
- Modify: `tests/engine/test_related.py`
- Modify: `tests/test_server_search.py`
- Modify: `pyproject.toml`

**Steps**

1. Add regression tests proving `wiki_related` remains domain-local when a
   section links through `iwiki://` to another domain.
2. Preserve vector-first behaviour, graph-only fallback, graph depth, current
   string serialization, legacy/Markdown equivalence, and unreadable-path
   handling.
3. Keep `parse_links` as the compatibility adapter that exposes only
   intra-domain targets to this caller. Do not add scope/domains parameters or
   change the MCP schema.
4. Prefer no production change in `related.py` if T1 compatibility suffices.

**Verify**

```bash
uv run pytest -q tests/engine/test_related.py tests/test_server_search.py tests/test_mcp_smoke.py
```

**Commit**

```text
test: preserve domain-local related lookup
```

## Task 8 — Markdown-authoritative lint and graph parity

**Dependencies:** T1, T2, T3, T4

**Closes:** R13, reserved-target diagnostics, and lint-related R14 recovery
behaviour.

**Expected output:** read-only Markdown findings plus an independent exact
SQLite parity report for every graph availability/state condition.

**Files**

- Modify: `src/iwiki_mcp/engine/lint.py`
- Modify: `src/iwiki_mcp/server.py`
- Modify: `tests/engine/test_lint.py`
- Modify: `tests/test_server_lint_sync.py`
- Modify: `tests/test_mcp_smoke.py`
- Modify: `pyproject.toml`

**Steps**

1. Add failing tests that ordinary lint results are identical for ready,
   missing, dirty, rebuilding, busy, incompatible, and corrupt graph states.
2. Build expected pages, H1-H6 anchors, reserved targets, and outgoing edges
   from current Markdown on every lint call using the shared parser.
3. Add exact parity tests for missing/extra pages and edges, anchor mismatch,
   state, schema version, and Markdown fingerprint.
4. Add `reserved_target` findings for authored links to root `index.md` or
   `log.md`; do not classify them as broken.
5. Add a per-domain `graph` report with `available`, `schema_version`, `state`,
   `fingerprint_match`, parity arrays, sanitized unavailable reason, and
   `wiki_index(domain)` remediation hint.
6. Keep lint read-only: monkeypatch every graph create/update/rebuild path to
   fail the test if invoked. Preserve config-free operation and all existing
   top-level lint keys.
7. For multi-domain invocation, resolve cross-domain targets only against the
   visible target set and distinguish unavailable-domain from confirmed
   missing-target findings without disclosing hidden-domain contents.

**Verify**

```bash
uv run pytest -q tests/engine/test_lint.py tests/test_server_lint_sync.py tests/test_mcp_smoke.py
```

**Commit**

```text
feat: report SQLite graph parity in lint
```

## Task 9 — Public documentation and project wiki

**Dependencies:** T5, T6, T7, T8

**Closes:** documentation obligations for R1–R15 and the intent requirement
that repository and iwiki documentation match observed behaviour.

**Expected output:** synchronized English/Russian docs, authoring guidance,
architecture, templates, and bound iwiki pages for the implemented contracts.

**Files**

- Modify: `README.md`
- Modify: `docs/README.ru.md`
- Modify: `docs/architecture.md`
- Modify: `src/iwiki_mcp/resources.py`
- Modify: `templates/AGENTS.md.snippet`
- Modify: `templates/CLAUDE.md.snippet`
- Modify: `tests/test_resources.py`
- Modify: `tests/test_resources_frontmatter.py`
- Modify: `pyproject.toml`
- Update: bound `iwiki-mcp` wiki pages through iwiki MCP tools

**Steps**

1. Document local `.iwiki/graph.sqlite3`, Git exclusion, rebuild/freshness,
   state machine, failure warning/fallback, and retained domain JSONL files.
2. Document relative intra-domain links and canonical cross-domain
   `iwiki://<domain>/<page-id>#<anchor>` syntax with safe examples.
3. Document scope-safe traversal, directed storage/undirected search,
   `RESERVED_OKF`, unchanged `wiki_related`, and graph parity lint output.
4. Update authoring resources/templates so new pages use `iwiki://` only for
   cross-domain targets and never link to generated artifacts as graph pages.
5. Update architecture diagrams/data flow without introducing a code graph or
   vector/log SQLite migration.
6. After code behaviour is verified, call iwiki status/bind, update affected
   existing pages with their changed sources, then run iwiki lint. Do not run
   a routine manual reindex after wiki write/update tools.

**Verify**

```bash
uv run pytest -q tests/test_resources.py tests/test_resources_frontmatter.py tests/test_mcp_smoke.py
```

**Commit**

```text
docs: document SQLite graph retrieval
```

## Task 10 — Whole-system verification and reconciliation

**Dependencies:** T1–T9

**Closes:** all remaining acceptance criteria, verification obligations, and
the plan-backed result reconciliation requirement.

**Expected output:** complete command evidence, live two-domain recovery
evidence, current docs/wiki lint, and a formal chain result verdict.

**Files**

- Modify only for confirmed regressions: affected implementation/tests/docs
- Modify through chain validation: `docs/TODO.md`
- Modify: `pyproject.toml` only if this task changes repository content

**Steps**

1. Run the focused graph, retrieval, mutation, sync, lint, related, and smoke
   suites together to expose state leakage or ordering differences.
2. Run full pytest, flake8, compileall, and CLI help.
3. Create a temporary two-domain Git base. Index it, restart with no SQLite,
   run scoped cross-domain search, mutate/pull on a second checkout, confirm
   proactive refresh, corrupt the cache, and confirm fallback/recovery without
   embedding calls.
4. Confirm ready-state search opens no Markdown file for adjacency and that
   hidden-domain traversal cannot affect visible results.
5. Run project iwiki lint and reconcile repository docs with observed MCP
   responses. Record any unrelated pre-existing findings without fixing them.
6. Run `$check-chain result` against this plan and resolve every confirmed
   critical/important finding before completion. Close the single
   `sqlite-graph-index` TODO row only on result `OK`.
7. If `check-chain` is unavailable at execution time, stop T10, leave Result
   and Closed unset, report the tooling blocker, and resume only when the skill
   can write the required `result_check` and TODO state. Never substitute a
   manual completion verdict.

**Verify**

```bash
uv run pytest -q
uv run flake8 src tests
uv run python -m compileall -q src tests
uv run iwiki-mcp --help
git diff --check
```

**Commit**

```text
test: verify SQLite graph integration
```

## Plan acceptance

The plan is ready for execution only after `$check-chain plan` returns `OK`
against the approved spec. If chain tooling is unavailable during authoring or
execution, stop at that gate, leave the corresponding TODO/result state open,
and resume when the validator is available; never substitute a manual verdict.
