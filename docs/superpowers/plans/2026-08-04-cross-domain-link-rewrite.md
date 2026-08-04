---
review:
  plan_hash: 9e65f6c065b74159
  last_run: 2026-08-04
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-08-04-cross-domain-link-rewrite-intent.md
  spec: docs/superpowers/specs/2026-08-04-cross-domain-link-rewrite-design.md
---
# Cross-Domain Link Rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve exact incoming `iwiki://` page and anchor links across writable domains when a target page moves or a `##` heading is renamed.

**Architecture:** Add backward-compatible multi-domain write binding, pure structural rewrite primitives, incoming-edge discovery, and one base-locked transaction coordinator with a durable local journal. Canonical Markdown, portable JSONL stores, and one exact-scope Git commit form the rollback boundary; SQLite remains derived and is refreshed or made unavailable by fingerprint/dirty state after commit.

**Tech Stack:** Python 3.10+, stdlib dataclasses/pathlib/hashlib/json/os/sqlite3/subprocess, FastMCP, existing JSONL vector/index pipeline, Git, pytest/pytest-asyncio.

---

**Date:** 2026-08-04
**Status:** approved
**Topic:** `cross-domain-link-rewrite`

## Source contracts

- Intent: `docs/superpowers/intents/2026-08-04-cross-domain-link-rewrite-intent.md`
- Design: `docs/superpowers/specs/2026-08-04-cross-domain-link-rewrite-design.md`
- Branch/topic: `dev-cross-domain-link-rewrite` / `cross-domain-link-rewrite`

Implementers must not revise accepted intent or design decisions. Return any
contract conflict to the earliest chain gate. Every task begins with a failing
test, changes only its named files, bumps the patch version in
`pyproject.toml`, `src/iwiki_mcp/__init__.py`, and `uv.lock`, and ends with its
own commit. The plan artifact uses `0.7.45`; implementation starts from that
version.

## Plan approval baseline

After `$check-chain plan` returns `OK` and the user approves this plan, commit
the plan artifact, its `docs/TODO.md` state, and version `0.7.45` before T1.
T1 may start only from that clean committed baseline. T10 uses this plan commit
as the implementation-diff base; it must never infer a baseline from an
untracked or first-committed-at-result plan file.

## File ownership

- `engine/links.py`: parse-preserving cross-domain URI rewrites only.
- `engine/section.py`: `##` section body replacement and optional heading rename.
- `base.py`: binding/config representation, write-scope resolution, and the
  existing root-local Git exclusion helper.
- `engine/graph_store.py` and `graph.py`: incoming candidate lookup and graph
  readiness; neither authorizes Markdown mutation.
- `sync.py` and `indexer.py`: exact Git path staging and multi-domain derived
  graph finalization helpers.
- `cross_domain.py`: transaction planning, journaling, recovery, rollback, and
  canonical execution; it owns no Markdown syntax or embedding implementation.
- `okf.py`: compatibility-preserving intra-domain page-move preparation.
- `server.py`: MCP validation/adapters and public result/error shapes.

## Dependency order

```text
T1 -> T2 -> T3 -> T4 -> T5 -> T6 -> T7 -> T8 -> T9 -> T10
```

No task may start before its dependencies pass focused verification. T1–T5
define logically separate contracts, but they execute in task-number order
because every task owns the shared version files and exact patch progression.
T6 is the first task allowed to combine those contracts.

## Requirement coverage

| Requirement | Plan tasks |
|---|---|
| R1 Multi-domain binding | T1, T8, T9, T10 |
| R2 Exact structural rewrite | T2, T7, T10 |
| R3 Scoped discovery | T3, T6, T10 |
| R4 Read-only blocker | T3, T6, T10 |
| R5 Atomic page move | T6, T7, T9, T10 |
| R6 Atomic heading rename | T2, T6, T8, T9, T10 |
| R7 Failure rollback | T5, T6, T10 |
| R8 Crash recovery | T5, T6, T9, T10 |
| R9 Derived graph safety | T3, T4, T6, T9, T10 |
| R10 Git/push behavior | T4, T6, T9, T10 |
| R11 Compatibility | T1, T2, T7, T8, T9, T10 |
| R12 Documentation | T9, T10 |

## Task 1 — Multi-domain binding and existing-domain enforcement

**Dependencies:** none

**Closes:** R1 and the write-scope compatibility portion of R11.

**Expected output:** `Binding.write_scope` resolves deterministically, public
binding/status responses expose it, existing-domain mutation adapters reject
unbound targets, and `wiki_create_domain` remains an empty-domain bootstrap.

**Files**

- Modify: `src/iwiki_mcp/base.py`
- Modify: `src/iwiki_mcp/server.py`
- Modify: `tests/test_base.py`
- Modify: `tests/test_server_write.py`
- Modify: `tests/test_server_update.py`
- Modify: `tests/test_server_delete.py`
- Modify: `tests/test_server_lint_sync.py`
- Modify: `tests/test_server_migrate.py`
- Modify: `tests/test_okf_server.py`
- Modify: `tests/test_export_okf.py`
- Modify: `tests/test_export_only_artifacts.py`
- Modify: `tests/test_create_domain_layout.py`
- Modify: `tests/test_mcp_smoke.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

**Contract**

```python
@dataclass(frozen=True)
class Binding:
    base: str
    read: tuple[str, ...]
    write: str | None
    project_dir: str
    write_scope: tuple[str, ...] = ()

def writable_domains(binding: Binding) -> tuple[str, ...]: ...
def write_scope_error(binding: Binding, domain: str) -> dict | None: ...
```

- [ ] Add RED config tests: absent `write_scope` resolves to `(write,)`, an
  explicit list preserves first-seen order, duplicates collapse, every member
  exists and belongs to read scope, and existing manual `Binding(...)`
  fixtures remain compatible.
- [ ] Add RED `wiki_bind` tests for optional `write_scope`, byte-identical
  `.iwiki.toml` on validation error, primary `write` membership, and unchanged
  current-project restriction for scalar `write`.
- [ ] Add RED server tests proving write/update/delete/index/migrate/apply/export
  reject existing domains outside scope before freshness, config, embedding,
  filesystem, or Git side effects.
- [ ] Add RED bootstrap tests proving `wiki_create_domain` can create an empty
  unbound directory, creates no page/index/log, and still follows existing
  freshness and exact-domain commit behavior.
- [ ] Implement binding normalization, config round-trip, MCP schema additions,
  and one shared existing-domain guard. Do not guard read-only tools,
  `wiki_bind`, `wiki_sync`, or the explicit `wiki_create_domain` bootstrap.
- [ ] Bump `0.7.45 -> 0.7.46`, refresh `uv.lock`, then run focused tests.

**Verify**

```bash
uv run pytest -q tests/test_base.py tests/test_server_write.py tests/test_server_update.py tests/test_server_delete.py tests/test_server_lint_sync.py tests/test_server_migrate.py tests/test_okf_server.py tests/test_export_okf.py tests/test_export_only_artifacts.py tests/test_create_domain_layout.py tests/test_mcp_smoke.py
```

Expected: all selected tests pass; legacy scalar binding responses retain
`write` and add deterministic `write_scope`.

**Commit**

```text
feat: add multi-domain write scope
```

## Task 2 — Structural URI and heading rewrite primitives

**Dependencies:** T1 (serial delivery dependency only)

**Closes:** R2 and the pure Markdown portion of R6/R11.

**Expected output:** deterministic pure functions rewrite only exact parsed
cross-domain targets and optionally rename one `##` heading without changing
unrelated bytes.

**Files**

- Modify: `src/iwiki_mcp/engine/links.py`
- Modify: `src/iwiki_mcp/engine/section.py`
- Modify: `tests/engine/test_links.py`
- Modify: `tests/test_links_rewrite.py`
- Modify: `tests/test_section.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

**Contract**

```python
@dataclass(frozen=True)
class CrossDomainRewrite:
    target_domain: str
    old_page: str
    new_page: str
    old_anchor: str | None = None
    new_anchor: str | None = None

def rewrite_cross_domain_links(
    content: str, source_domain: str, rewrite: CrossDomainRewrite
) -> tuple[str, int]: ...

def rewrite_relative_anchors(
    content: str, old_anchor: str, new_anchor: str
) -> tuple[str, int]: ...

def replace_section(
    content: str, heading: str, new_body: str, *, new_heading: str | None = None
) -> str: ...
```

- [ ] Add RED table tests for exact page moves, anchor renames, the combined
  internal mapping, optional `.md` preservation, authored anchor preservation,
  normalized anchors, mismatched domains/pages/anchors, and idempotence.
- [ ] Add RED negative tests for images, external URIs, query/userinfo/port,
  inline/fenced code, visible label text, surrounding prose, and unsafe decoded
  path segments.
- [ ] Add RED section tests for rename plus body replacement, same-anchor
  no-op, empty normalized anchor, missing/duplicate old heading, collision with
  another H1–H6 normalized anchor, and unchanged legacy calls without
  `new_heading`.
- [ ] Implement URI replacement from parser-approved spans and preserve the
  authored `.md` choice. Implement heading-line replacement in
  `engine.section`; do not make either module aware of files, SQLite, Git, MCP,
  config, or embeddings.
- [ ] Bump `0.7.46 -> 0.7.47`, refresh `uv.lock`, then run focused tests.

**Verify**

```bash
uv run pytest -q tests/engine/test_links.py tests/test_links_rewrite.py tests/test_section.py tests/engine/test_lint.py tests/test_apply_move.py
```

Expected: exact rewrite counts pass and every compatibility test remains green.

**Commit**

```text
feat: add exact cross-domain link rewrites
```

## Task 3 — Scope-safe incoming-reference discovery

**Dependencies:** T2 (serial delivery dependency only)

**Closes:** R3 and discovery portions of R4/R9.

**Expected output:** ready SQLite narrows incoming candidates without a domain
walk; unavailable graph state produces one validated read-scope Markdown
snapshot; canonical parsing decides the final referrer set.

**Files**

- Modify: `src/iwiki_mcp/engine/graph_store.py`
- Modify: `src/iwiki_mcp/graph.py`
- Create: `tests/test_cross_domain_discovery.py`
- Modify: `tests/engine/test_graph_store.py`
- Modify: `tests/test_graph_runtime.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

**Contract**

```python
@dataclass(frozen=True)
class IncomingCandidate:
    domain: str
    file: str

@dataclass(frozen=True)
class MarkdownCandidateSnapshot:
    candidates: tuple[IncomingCandidate, ...]
    expected_hashes: tuple[tuple[str, str, str], ...]

class MarkdownSnapshotChanged(RuntimeError):
    pass

def incoming_candidates(
    base: str,
    domains: tuple[str, ...],
    target_page_id: str,
    target_anchor: str | None = None,
) -> tuple[IncomingCandidate, ...] | None: ...

def markdown_incoming_snapshot(
    base: str,
    domains: tuple[str, ...],
    target_page_id: str,
    target_anchor: str | None = None,
) -> MarkdownCandidateSnapshot: ...
```

`None` means the coordinator must use Markdown snapshot fallback; an empty
tuple means a ready graph proved there are no candidate pages. Each
`expected_hashes` item is `(domain, relative_file, sha256)`, sorted by domain
and file. `markdown_incoming_snapshot` raises `MarkdownSnapshotChanged` when
its post-scan hash revalidation fails; T6 maps that internal exception to
`CrossDomainError(code="source_changed")` without retrying under lock.

- [ ] Add RED SQL tests for `edges_target_idx` lookup by target page, optional
  normalized anchor, source-domain filtering, deterministic `(domain, file)`
  order, duplicate edge collapse, and no full-domain snapshot load.
- [ ] Add RED runtime tests proving every requested domain must have matching
  ready fingerprint metadata; missing, dirty, rebuilding, busy, corrupt, or
  race-changed state returns `None` without leaking hidden domains.
- [ ] Add RED discovery tests that monkeypatch `Path.rglob/read_text` to fail on
  the ready fast path and prove fallback scans each visible domain once.
- [ ] Add RED canonical reparse tests: stale `raw_target`, duplicate links,
  code, and changed Markdown candidates cannot authorize a rewrite.
- [ ] Implement the query helper and one snapshot builder using existing safe
  Markdown/fingerprint rules. Revalidate every snapshot hash immediately
  before returning it; raise `MarkdownSnapshotChanged` on mismatch rather than
  retrying under lock.
- [ ] Bump `0.7.47 -> 0.7.48`, refresh `uv.lock`, then run focused tests.

**Verify**

```bash
uv run pytest -q tests/test_cross_domain_discovery.py tests/engine/test_graph_store.py tests/test_graph_runtime.py tests/engine/test_links.py
```

Expected: ready lookup performs zero Markdown walk; fallback is complete,
scope-limited, deterministic, and race-rejecting.

**Commit**

```text
feat: discover scoped incoming wiki references
```

## Task 4 — Exact Git paths and multi-domain graph finalization

**Dependencies:** T3 (serial delivery dependency only)

**Closes:** R9, R10, and transaction prerequisites for R5–R8.

**Expected output:** lock-aware internal sync helpers stage an exact ordered
path list in one commit, and graph helpers finalize all affected domains or
prove their prior fingerprints unavailable.

**Files**

- Modify: `src/iwiki_mcp/sync.py`
- Modify: `src/iwiki_mcp/indexer.py`
- Modify: `src/iwiki_mcp/engine/graph_store.py`
- Modify: `tests/test_sync.py`
- Modify: `tests/test_sync_concurrency.py`
- Modify: `tests/test_commit_and_push.py`
- Create: `tests/test_graph_batch.py`
- Modify: `tests/test_indexer.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

**Contract**

```python
Pathspec = str | tuple[str, ...] | list[str] | None

def auto_commit(
    base: str, message: str, pathspec: Pathspec = None, timeout: float = 15.0
) -> dict: ...

def commit_and_push(
    base: str,
    message: str,
    pathspec: Pathspec = None,
    *,
    _after_commit=None,
) -> dict: ...

def finalize_graph_batch(
    mutations: tuple[GraphMutation, ...],
    refresh_files: dict[str, tuple[str, ...]],
    delete_files: dict[str, tuple[str, ...]],
) -> str | None: ...
```

- [ ] Add RED Git tests for deterministic deduplication, `git add -- path1
  path2`, exact scoped status, unrelated dirty/staged files excluded, one
  commit, transaction trailer preservation, scalar-path compatibility, and
  fail-soft push.
- [ ] Add RED lock tests proving public helpers acquire `base_lock` while
  coordinator-only internals run under an already-held lock without nested
  acquisition.
- [ ] Add RED graph-batch tests for all-domain success in one SQLite
  transaction where possible, refresh failure, dirty marking, dirty-write
  failure, canonical fingerprint mismatch rejection, and later rebuild without
  embedding calls.
- [ ] Implement normalized exact path handling and lock-aware internal helpers;
  use a path-limited commit so unrelated paths already staged by the user stay
  outside the transaction commit. Preserve every existing public response
  field and sanitization rule.
- [ ] Implement batch graph staging/finalization on existing schema. If refresh
  fails after commit, return the existing graph warning only after the normal
  readiness gate rejects every affected old fingerprint.
- [ ] Bump `0.7.48 -> 0.7.49`, refresh `uv.lock`, then run focused tests.

**Verify**

```bash
uv run pytest -q tests/test_sync.py tests/test_sync_concurrency.py tests/test_commit_and_push.py tests/test_graph_batch.py tests/test_indexer.py tests/test_graph_runtime.py
```

Expected: exact path staging and graph invalidation tests pass; scalar callers
remain compatible.

**Commit**

```text
feat: commit exact multi-domain mutations
```

## Task 5 — Durable transaction journal and recovery

**Dependencies:** T4 (serial delivery dependency only)

**Closes:** journal and recovery primitives for R7/R8.

**Expected output:** an fsynced root-local journal can restore uncommitted
files, recognize committed transactions, finalize recoverable graph failures,
and stop on ambiguous state.

**Files**

- Create: `src/iwiki_mcp/cross_domain.py`
- Create: `tests/test_cross_domain_journal.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

**Contract**

```python
@dataclass(frozen=True)
class FileSnapshot:
    path: str
    existed: bool
    sha256: str | None

@dataclass(frozen=True)
class TransactionManifest:
    transaction_id: str
    state: str
    base_head: str | None
    commit_head: str | None
    affected_domains: tuple[str, ...]
    files: tuple[FileSnapshot, ...]

class CrossDomainError(RuntimeError):
    code: str

def recover_pending_transactions(
    base: str,
    *,
    finalize_committed: Callable[[TransactionManifest], bool],
) -> None: ...
```

- [ ] Add RED journal tests for opaque IDs, root
  `.iwiki/transactions/<id>/manifest.json`, snapshots, temp-file plus
  `os.replace`, file/directory fsync, and states
  `prepared -> applied -> committed -> finalized`.
- [ ] Add RED recovery fixtures for prepared/applied with unchanged HEAD,
  crash after Git commit but before committed marker, explicit committed state,
  repeated graph repair failure, unexpected HEAD, and unexpected file hashes.
- [ ] Add RED tests proving recovery is idempotent, created move targets are
  removed during rollback, journals survive ambiguous state, and
  `manual_recovery_required` blocks new mutation.
- [ ] Implement journal serialization with relative base-contained paths only;
  reject symlink/path escapes and never store credentials or remote output.
  Finalize committed recovery after graph repair or verified dirty/fingerprint
  invalidation, so repeated repair failure does not block later mutations.
- [ ] Bump `0.7.49 -> 0.7.50`, refresh `uv.lock`, then run focused tests.

**Verify**

```bash
uv run pytest -q tests/test_cross_domain_journal.py tests/test_lock.py
```

Expected: every crash fixture reaches the specified restored/finalized/blocked
state and ambiguous state remains available for manual recovery.

**Commit**

```text
feat: add durable cross-domain mutation journal
```

## Task 6 — Atomic cross-domain mutation coordinator

**Dependencies:** T5 (requires the completed T1–T5 contract sequence)

**Closes:** R3, R4, R7, R8, R9, R10 and shared transaction portions of R5/R6.

**Expected output:** one immutable plan and execution path performs scoped
discovery, preflight, canonical writes, per-domain reindexing, exact Git commit,
graph finalization, rollback, recovery, and deterministic result evidence; one
shared guard recovers pending transactions before every overlapping mutating
MCP tool.

**Files**

- Modify: `src/iwiki_mcp/cross_domain.py`
- Create: `tests/test_cross_domain_transaction.py`
- Modify: `src/iwiki_mcp/server.py`
- Modify: `src/iwiki_mcp/indexer.py`
- Modify: `src/iwiki_mcp/sync.py`
- Modify: `tests/test_base.py`
- Modify: `tests/test_server_write.py`
- Modify: `tests/test_server_update.py`
- Modify: `tests/test_server_delete.py`
- Modify: `tests/test_server_lint_sync.py`
- Modify: `tests/test_server_migrate.py`
- Modify: `tests/test_okf_server.py`
- Modify: `tests/test_export_okf.py`
- Modify: `tests/test_export_only_artifacts.py`
- Modify: `tests/test_create_domain_layout.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

**Contract**

```python
@dataclass(frozen=True)
class PlannedEdit:
    domain: str
    file: str
    before_hash: str | None
    after: bytes | None

@dataclass(frozen=True)
class MutationPlan:
    operation: str
    transaction_id: str
    base_head: str | None
    edits: tuple[PlannedEdit, ...]
    affected_domains: tuple[str, ...]
    rewritten_pages: tuple[str, ...]
    rewritten_links: int

def execute_plan(base: str, binding: Binding, plan: MutationPlan) -> dict: ...
```

- [ ] Add RED preflight tests for visible writable referrers, visible read-only
  blocker, hidden-domain non-access, target collision, `source_changed`, root
  exclusion failure, pending journal recovery, immutable expected hashes, and
  rejection of an already-staged affected path without disturbing unrelated
  staged entries.
- [ ] Add RED exclusion tests proving the coordinator preflight calls
  `ensure_graph_store_excluded()`, `git check-ignore .iwiki/transactions/...`
  succeeds, unrelated `git add -A` excludes journal/graph, and legacy
  `<domain>/.iwiki/` stays visible.
- [ ] Add RED handler tests proving one common pre-mutation guard runs pending
  transaction recovery before `wiki_write_page`, `wiki_update_page`,
  `wiki_delete_page`, `wiki_index`, `wiki_create_domain`, `wiki_migrate_okf`,
  `wiki_apply_okf`, `wiki_export_okf`, and `wiki_sync`. Assert
  `manual_recovery_required` stops each handler before freshness, filesystem,
  embedding, Git, or remote side effects.
- [ ] Add RED success test proving sorted writes, changed-domain-only indexing,
  vector reuse, exact Markdown/index/log path staging, one commit with
  `Iwiki-Transaction`, deterministic `rewritten_pages`/`affected_domains`, and
  fail-soft push outside rollback.
- [ ] Add RED fault injection at each canonical write, per-domain index,
  staging, commit, journal transition, graph refresh, and dirty write. Assert
  byte-identical Markdown/index/log restoration, old/new path restoration,
  unchanged pre-commit HEAD, empty staging, and retained journal only for
  ambiguous recovery.
- [ ] Implement plan preimage revalidation under one base lock, snapshot all
  mutable paths before the first canonical write, apply sibling temp files via
  `os.replace`, index domains in deterministic order, and use T4 lock-aware
  exact commit helpers.
- [ ] Implement the shared server pre-mutation guard. It acquires `base_lock`,
  invokes `recover_pending_transactions`, then runs a lock-aware operation body
  without nested acquisition. The cross-domain coordinator reuses the held
  guard. `wiki_bind` and read-only tools remain outside it.
- [ ] After confirmed commit, keep canonical data on graph/push failure. Verify
  graph unavailability before journal finalization and return sanitized
  warnings without leaking hidden referrers or filesystem paths.
- [ ] Bump `0.7.50 -> 0.7.51`, refresh `uv.lock`, then run focused tests.

**Verify**

```bash
uv run pytest -q tests/test_cross_domain_transaction.py tests/test_cross_domain_journal.py tests/test_cross_domain_discovery.py tests/test_graph_batch.py tests/test_commit_and_push.py tests/test_indexer.py tests/test_base.py tests/test_server_write.py tests/test_server_update.py tests/test_server_delete.py tests/test_server_lint_sync.py tests/test_server_migrate.py tests/test_okf_server.py tests/test_export_okf.py tests/test_export_only_artifacts.py tests/test_create_domain_layout.py
```

Expected: transaction success/failure matrices pass with no partial canonical
state and no stale trusted graph.

**Commit**

```text
feat: coordinate atomic cross-domain mutations
```

## Task 7 — Page-move integration

**Dependencies:** T6

**Closes:** R5 and page-move portions of R2/R11.

**Expected output:** `wiki_apply_okf` page moves preserve intra-domain relative
links and writable cross-domain `iwiki://` links in the shared transaction,
while no-op type applications keep existing behavior.

**Files**

- Modify: `src/iwiki_mcp/okf.py`
- Modify: `src/iwiki_mcp/server.py`
- Modify: `tests/test_apply_move.py`
- Modify: `tests/test_okf_server.py`
- Create: `tests/test_cross_domain_move.py`
- Modify: `tests/test_frontmatter_governance.py`
- Modify: `tests/test_migrate_layout.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

**Contract**

```python
@dataclass(frozen=True)
class PreparedMove:
    old_identity: str
    new_identity: str
    edits: tuple[PlannedEdit, ...]
    refresh_files: tuple[str, ...]
    delete_files: tuple[str, ...]

def prepare_page_move(
    base_dir: str, domain: str, old_identity: str, new_identity: str
) -> PreparedMove: ...
```

`PreparedMove.refresh_files` and `delete_files` are target-domain-relative
tuples. The T7 server adapter maps them to the T4 domain-keyed dictionaries and
merges them with coordinator-discovered cross-domain referrer files before
calling `finalize_graph_batch`.

- [ ] Add RED pure preparation tests proving no filesystem write occurs before
  coordinator execution and relative links/log re-keying match current
  `move_page` results.
- [ ] Add RED two/three-domain Git-base scenarios for multiple referrers,
  authored anchors, optional `.md`, duplicate links, no referrers, read-only
  blocker, hidden domains, collision, and unchanged-type no-op.
- [ ] Refactor `okf.move_page` to reuse pure preparation for compatibility, and
  make the server delegate actual type-changing moves to the coordinator.
  Preserve frontmatter timestamp/source/tag/description/status behavior.
- [ ] Assert one local commit changes the moved target, relative referrers,
  cross-domain referrers, and affected JSONL stores only; result evidence uses
  `rewritten_pages`, `affected_domains`, `rewritten_links`, and
  `transaction_id`.
- [ ] Run lint and exact graph-parity assertions in the temporary base.
- [ ] Bump `0.7.51 -> 0.7.52`, refresh `uv.lock`, then run focused tests.

**Verify**

```bash
uv run pytest -q tests/test_cross_domain_move.py tests/test_apply_move.py tests/test_okf_server.py tests/test_frontmatter_governance.py tests/test_migrate_layout.py tests/engine/test_lint.py tests/test_graph_runtime.py
```

Expected: move scenarios preserve all writable links, reject unsafe scope, and
leave lint/graph parity clean.

**Commit**

```text
feat: preserve cross-domain links on page move
```

## Task 8 — Heading-rename integration and MCP contracts

**Dependencies:** T7

T7 is a serial integration dependency because T7 and T8 both modify the MCP
adapter in `server.py`; T8 functionally consumes T6 and T2 contracts but starts
from the verified page-move integration baseline.

**Closes:** R6 and remaining public-contract portions of R1/R11.

**Expected output:** `wiki_update_page(new_heading=...)` atomically changes the
selected heading/body, relative anchors, and exact writable cross-domain URI
anchors while ordinary updates remain byte-compatible.

**Files**

- Modify: `src/iwiki_mcp/server.py`
- Modify: `tests/test_server_update.py`
- Create: `tests/test_cross_domain_update.py`
- Modify: `tests/test_section.py`
- Modify: `tests/test_mcp_smoke.py`
- Modify: `tests/test_resources.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

**Public signature**

```python
def wiki_update_page(
    domain: str,
    slug: str,
    heading: str,
    new_body: str,
    source: str | None = None,
    description: str | None = None,
    status: str | None = None,
    new_heading: str | None = None,
) -> dict: ...
```

- [ ] Add RED ordinary-update regression proving omitted `new_heading` retains
  current response fields, source/log upsert, validation order, rollback, graph
  refresh, and push-warning sanitization.
- [ ] Add RED rename cases: success, same normalized anchor no-op, missing old
  heading, empty new anchor, collision, relative same-domain anchors, unrelated
  anchors, multiple writable domains, read-only blocker, hidden domains, and
  `source_changed`.
- [ ] Route only rename operations through the coordinator. Use T2 section
  primitive for target bytes and T2 link primitive for relative/cross-domain
  anchors; SQLite candidates remain advisory.
- [ ] Return existing update fields plus deterministic transaction evidence
  only when a rename/rewrite transaction occurs. Keep errors as
  `write_scope_blocked`, `heading_collision`, `source_changed`,
  `mutation_failed`, or `manual_recovery_required` with specified hints.
- [ ] Run temporary-base lint/parity and assert no embedding call for pure
  graph repair; changed chunks may embed through normal domain indexing.
- [ ] Bump `0.7.52 -> 0.7.53`, refresh `uv.lock`, then run focused tests.

**Verify**

```bash
uv run pytest -q tests/test_cross_domain_update.py tests/test_server_update.py tests/test_section.py tests/test_mcp_smoke.py tests/test_resources.py tests/engine/test_lint.py tests/test_graph_runtime.py
```

Expected: rename contracts pass and ordinary `wiki_update_page` remains
compatible.

**Commit**

```text
feat: rename headings with incoming link updates
```

## Task 9 — Documentation, resources, and bound wiki

**Dependencies:** T1, T7, T8

**Closes:** R12 and user-facing portions of R1/R5/R6/R8–R11.

**Expected output:** repository docs, snippets, resource text, stale project
guidance, and bound wiki explain the implemented binding, rewrite, recovery,
graph, and Git boundaries without claiming hidden-domain coverage.

**Files**

- Modify: `README.md`
- Modify: `docs/README.ru.md`
- Modify: `docs/architecture.md`
- Modify: `CLAUDE.md`
- Modify: `templates/AGENTS.md.snippet`
- Modify: `templates/CLAUDE.md.snippet`
- Modify: `src/iwiki_mcp/resources.py`
- Modify: `tests/test_resources.py`
- Modify: `tests/test_resources_frontmatter.py`
- Modify: `tests/test_server_lint_sync.py`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

- [ ] Add RED documentation/resource assertions for `write_scope`, scalar
  compatibility, `wiki_create_domain` bootstrap, URI rewrite timing,
  `new_heading`, blockers, journal path/recovery, one local commit, graph
  dirty/fingerprint fallback, and fail-soft push.
- [ ] Update English/Russian setup and tool tables with exact signatures and
  result/error examples. State that hidden/read-only domains are not rewritten
  and read-only visible referrers block before mutation.
- [ ] Update architecture with coordinator ownership, lock order, durable
  states, canonical/derived commit boundary, exact path staging, and recovery
  sequence. Correct `CLAUDE.md` paths from obsolete `docs/wiki/` and
  domain-local portable stores to current files/domain-root JSONL.
- [ ] Update both templates and `AUTHORING_RULES` so agents use relative links
  inside a domain, `iwiki://` across domains, and rely on automatic rewrite only
  when all visible referrers are writable.
- [ ] Through iwiki MCP, update existing `iwiki-mcp` pages for architecture,
  base binding, authoring/linting, MCP server, Git sync, and OKF governance as
  applicable. Do not call `wiki_index` after MCP writes; finish with full-scope
  `wiki_lint` and require no broken/stale/missing affected link or graph parity
  mismatch.
- [ ] Bump `0.7.53 -> 0.7.54`, refresh `uv.lock`, then run docs/resource tests.

**Verify**

```bash
uv run pytest -q tests/test_resources.py tests/test_resources_frontmatter.py tests/test_server_lint_sync.py tests/test_mcp_smoke.py
```

Expected: documentation assertions pass; full-scope `wiki_lint` reports empty
`broken`, `stale`, `missing_source`, `unavailable_domain`, `missing_pages`,
`extra_pages`, `missing_edges`, `extra_edges`, and `anchor_mismatches` for the
changed `iwiki-mcp` scope. Pre-existing advisory/orphan findings must be
reported separately, not silently rewritten.

**Commit**

```text
docs: document cross-domain link transactions
```

## Task 10 — Full integration and result evidence

**Dependencies:** T1–T9

**Closes:** R1–R12, every Desired Outcome, Health Metric, and Done When.

**Expected output:** complete automated and real temporary-base evidence proves
the feature, compatibility, recovery, performance guard, docs, and graph
parity; result metadata and the final version bump are committed.

**Files**

- No implementation-file ownership: a failure reopens its owning T1–T9 task
  before T10 resumes
- Modify: `docs/TODO.md` only through `$check-chain result`
- Modify: `docs/superpowers/plans/2026-08-04-cross-domain-link-rewrite.md`
  only for `result_check` frontmatter
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `uv.lock`

- [ ] Run every focused suite from T1–T9. Any failure must receive a minimal
  RED regression in its owning test file before its fix.
- [ ] Run the full test, lint, compile, CLI, build, lock, and diff checks below;
  capture exact pass counts and command status for result reconciliation.
- [ ] Run a real temporary multi-domain Git-base move scenario with two writable
  referrer domains and one target domain. Assert rewritten URIs, portable-store
  parity, one exact-scope commit/trailer, unrelated dirty file exclusion,
  `wiki_lint`, and graph parity.
- [ ] Run a real heading-rename scenario with relative and cross-domain anchors.
  Then run read-only blocker, injected pre-commit rollback, post-commit graph
  failure, and crash-recovery scenarios; record observable files, HEAD, journal,
  warning, and graph readiness outcomes.
- [ ] Prove ready incoming discovery performs no full-scope Markdown walk and
  ordinary search/write contracts do not regress. Confirm vector reuse and no
  embedding calls during graph-only repair.
- [ ] Re-run full-scope bound `wiki_lint`; document unchanged pre-existing
  advisory/orphan findings separately. Before result metadata changes, resolve
  the committed pre-T1 implementation baseline with
  `git log -1 --format=%H -- docs/superpowers/plans/2026-08-04-cross-domain-link-rewrite.md`,
  then run `$check-chain result` against this plan with that revision as
  `--since`. Close the TODO row only on `OK`.
- [ ] Bump `0.7.54 -> 0.7.55`, refresh `uv.lock`, and commit final verification
  fixes plus `result_check`/TODO metadata. Do not create an HTML report; the
  user declined it.

**Verify**

```bash
uv run pytest -q
uv run flake8 src tests
uv run python -m compileall -q src
uv run iwiki-mcp --help
uv build
uv lock --check
git diff --check
```

Expected: all commands exit 0; real scenarios satisfy every Desired Outcome,
Health Metric, R1–R12 DoD, and Done When criterion. Result reconciliation must
map every changed path to T1–T10 with no unexplained `EXCESS` file.

**Commit**

```text
test: verify cross-domain link transactions
```

## Expected implementation summary

After T1–T10, clients can bind multiple writable domains, move a target page or
rename a `##` heading, and receive deterministic transaction evidence while
all visible writable incoming links stay valid. Unsafe scope, races, and
pre-commit failures leave canonical data unchanged; post-commit graph/push
failures preserve the valid local commit and never expose stale graph rows.

## Human checkpoints

- No implementation task may change URI syntax, SQLite schema, hidden-domain
  visibility, remote transaction semantics, or the accepted MCP result fields.
- Any need to rewrite a read-only/hidden domain, weaken rollback, or replace
  `rewritten_pages`/`affected_domains` returns to the spec gate.
- No further user decision is required before execution if this checked plan is
  approved.
