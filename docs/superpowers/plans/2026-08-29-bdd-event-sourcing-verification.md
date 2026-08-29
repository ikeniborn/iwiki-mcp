---
review:
  plan_hash: e2ced5febc351929
  last_run: 2026-08-29
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-08-29-bdd-event-sourcing-verification-intent.md
  spec: docs/superpowers/specs/2026-08-29-bdd-event-sourcing-verification-design.md
---

# BDD Event-Sourcing Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add server-owned, graph-optional Given-When-Then specifications with equivalent Git and PostgreSQL projections, semantic tools, strict/optional/disabled modes, and durable agent guidance.

**Architecture:** Canonical Markdown is parsed by a standard-library engine module into immutable scenario and binding records. A backend-neutral application service owns projection assembly, queries, evidence freshness, and fail-soft graph resolution; Git JSONL and PostgreSQL v6/v7 adapters persist equivalent records. PostgreSQL v7 stores last-success projection metadata and detaches stale scenario rows from deleted pages without weakening ordinary Wiki or optional code-graph boundaries.

**Tech Stack:** Python 3.10+, `tomllib`/`tomli`, FastMCP, Git filesystem storage, PostgreSQL/psycopg, pytest/pytest-asyncio, existing code-graph readers.

**Status:** approved
**Approved specification:** `docs/superpowers/specs/2026-08-29-bdd-event-sourcing-verification-design.md` (`4a3cd06030b2a322`)

---

## Scope and delivery invariants

One plan is retained because the public grammar, mode matrix, backend parity, MCP tools,
and authorization contract must ship coherently. Splitting by backend would leave one
accepted contract with incompatible storage behavior.

- Markdown remains canonical; projections and resolution evidence are rebuildable.
- Only an explicitly supplied or preserved normalized `type: specification` activates
  GWT parsing. Automatic classification must never select it.
- `disabled` bypasses parsing/storage/graph; `optional` is advisory and fail-soft;
  `strict` rejects only invalid specification-page mutations.
- Ordinary pages must not open specification storage or inspect the code graph.
- `implements` and `verifies` stay outside structural code-graph tables and public tools.
- PostgreSQL migrations are forward-only v6 and v7 with tenant/domain RLS. Version 7
  adds last-success metadata, stored page slug, nullable page identity, and
  `ON DELETE SET NULL`; Git strict mutation holds one cross-process lock across
  page/projection/index/commit/rollback.
- PostgreSQL-marked tests run only against the disposable pgvector test database guarded
  by `tests/postgres/conftest.py`.
- Each worker owns only files listed by its task, does not revert other changes, returns
  changed paths/checks/blockers, and leaves task-ledger/Wiki writes to the parent.

Every repository commit owns and updates the four version surfaces
`pyproject.toml`, `src/iwiki_mcp/__init__.py`, `tests/test_package.py`, and `uv.lock`.
Workers run `uv sync --extra dev` and `uv run pytest -q tests/test_package.py` before
their focused suite and stage those four files with their task files. Each task command
block contains its one `uv sync --extra dev` invocation; this paragraph defines policy,
not an additional invocation. Exact monotonic targets are fixed here:

| Commit | Version |
|---|---|
| Approved plan | `0.7.205` |
| Task 1 | `0.7.206` |
| Task 2 | `0.7.207` |
| Task 3 | `0.7.208` |
| Task 4 | `0.7.209` |
| Task 5 | `0.7.210` |
| Approved schema-v7 spec | `0.7.211` |
| Revised schema-v7 plan | `0.7.212` |
| Task 6 | `0.7.213` |
| Task 7 | `0.7.214` |
| Task 8 | `0.7.215` |
| Task 9 | `0.7.216` |
| Task 10 | `0.7.217` |

The synchronized execution base is `0.7.204`. Immediately after approval and before any
implementation worker dispatch, the parent changes plan status to approved, updates all
four version surfaces to `0.7.205`, runs
`uv sync --extra dev` and `uv run pytest -q tests/test_package.py`, then commits and
pushes the approved plan plus version surfaces. Task 1 must refuse to start unless HEAD
already contains that `0.7.205` prerequisite commit; its own commit is `0.7.206`.

Tasks 1 through 5 and the approved schema-v7 spec are already delivered through
`0.7.211`. Before resuming implementation, the parent changes this revised plan status
to approved, updates all four version surfaces to `0.7.212`, runs the package tests, and
commits/pushes only the plan plus version surfaces. Task 6 must refuse to start unless
HEAD contains that `0.7.212` prerequisite; its implementation commit is `0.7.213`.

If result reconciliation demonstrates a later defect requiring another repository
commit, that owning task increments all four surfaces to the next unused patch before
the fix commit.

## File responsibility map

| Responsibility | Files |
|---|---|
| GWT grammar, immutable semantic records, findings | `src/iwiki_mcp/engine/specifications.py` |
| Local and hosted policy parsing | `src/iwiki_mcp/base.py`, `src/iwiki_mcp/storage.py`, `src/iwiki_mcp/postgres/config.py` |
| Backend-neutral projection/query/evidence service | `src/iwiki_mcp/specifications.py` |
| Git JSONL records and atomic persistence | `src/iwiki_mcp/specification_store.py` |
| Git lock-held mutation orchestration | `src/iwiki_mcp/sync.py`, `src/iwiki_mcp/server.py` |
| PostgreSQL schema/RLS/grants, startup guards, and adapter | `src/iwiki_mcp/postgres/migrations.py`, `src/iwiki_mcp/postgres/store.py`, `src/iwiki_mcp/http.py`, `src/iwiki_mcp/server.py` |
| Private specification graph-snapshot composition | `src/iwiki_mcp/codegraph/application.py`, `src/iwiki_mcp/server.py` |
| Hosted authorization and policy installation | `src/iwiki_mcp/http.py`, `src/iwiki_mcp/server.py` |
| Public resources/docs/templates | `src/iwiki_mcp/resources.py`, `README.md`, `docs/README.ru.md`, `docs/architecture.md`, `templates/*.snippet` |
| Deterministic measurements | `tests/measurement/test_specification_paths.py` |

## Task 1: Core grammar, identity, and explicit classification

**Files:**
- Create: `src/iwiki_mcp/engine/specifications.py`
- Create: `tests/engine/test_specifications.py`
- Modify: `src/iwiki_mcp/engine/frontmatter.py`
- Modify: `src/iwiki_mcp/engine/classify.py`
- Modify: `tests/test_frontmatter_governance.py`
- Modify: `tests/test_classify.py`

- [ ] **Step 1: Write failing semantic-model and parser tests**

Cover valid aggregate and request/response examples, stable identity, deterministic
binding IDs, exact roles, exception exclusivity, unknown/duplicate keys, scalar bounds,
one fence per H2, incomplete bindings, and ordinary-page bypass. Use the exact public
entry point:

```python
result = parse_specification_page(
    domain="payments",
    slug="specification/open-account",
    markdown=markdown,
)
assert result.scenarios[0].identity == "payments#confirm-account-opening"
assert {item.relation for item in result.scenarios[0].bindings} == {
    "implements", "verifies",
}
assert result.findings == ()
```

- [ ] **Step 2: Run parser tests and verify RED**

```bash
uv run pytest -q tests/engine/test_specifications.py tests/test_frontmatter_governance.py tests/test_classify.py
```

Expected: collection/import failure for `iwiki_mcp.engine.specifications` or missing
`specification` governance behavior.

- [ ] **Step 3: Implement immutable records and exact grammar**

Define these public engine shapes and keep the module free of server/storage/graph code:

```python
@dataclass(frozen=True)
class PhaseItem:
    phase: Literal["given", "when", "then"]
    role: str
    name: str

@dataclass(frozen=True)
class SpecificationBinding:
    binding_id: str
    relation: Literal["implements", "verifies"]
    phase: Literal["given", "when", "then"] | None
    selector_kind: Literal["symbol", "file", "source_glob"]
    selector: str

@dataclass(frozen=True)
class Scenario:
    domain: str
    scenario_id: str
    title: str
    slug: str
    heading: str
    anchor: str
    source_hash: str
    items: tuple[PhaseItem, ...]
    bindings: tuple[SpecificationBinding, ...]

@dataclass(frozen=True)
class ParseResult:
    scenarios: tuple[Scenario, ...]
    findings: tuple[dict[str, object], ...]

def parse_specification_page(domain: str, slug: str, markdown: str) -> ParseResult:
    """Return scenarios and sanitized findings for one explicit specification page."""
    meta, body = frontmatter.split(markdown, strict_code=True)
    return parse_specification_body(domain, slug, body, meta)
```

Use `tomllib` with the existing Python 3.10 `tomli` fallback. Reuse code-selector
normalization rules without creating structural links. Hash canonical UTF-8 block text;
build binding IDs from the NUL-joined fields defined by the specification.

- [ ] **Step 4: Make classification explicit**

Add `specification` to `OKF_TYPES`, but introduce an ordinary-only classifier vocabulary:

```python
CLASSIFIABLE_TYPES = tuple(item for item in OKF_TYPES if item != "specification")
```

`engine.classify.classify_page()` must format its prompt with `CLASSIFIABLE_TYPES`.
Tests must prove GWT prose/fences never yield `specification` when type is omitted, while
explicit mixed-case input normalizes to it.

- [ ] **Step 5: Bump to `0.7.206`, run focused tests, and commit**

```bash
uv sync --extra dev
uv run pytest -q tests/test_package.py tests/engine/test_specifications.py tests/test_frontmatter_governance.py tests/test_classify.py
git add src/iwiki_mcp/engine/specifications.py src/iwiki_mcp/engine/frontmatter.py src/iwiki_mcp/engine/classify.py tests/engine/test_specifications.py tests/test_frontmatter_governance.py tests/test_classify.py pyproject.toml src/iwiki_mcp/__init__.py tests/test_package.py uv.lock
git commit -m "feat(spec): add deterministic GWT grammar"
```

Expected: all focused tests pass.

## Task 2: Local and hosted specification policy

**Files:**
- Modify: `src/iwiki_mcp/storage.py`
- Modify: `src/iwiki_mcp/base.py`
- Modify: `src/iwiki_mcp/postgres/config.py`
- Create: `tests/test_specification_config.py`
- Modify: `tests/test_base.py`
- Modify: `tests/postgres/test_config.py`

- [ ] **Step 1: Write failing local and hosted configuration tests**

Exercise absent/default/all-three modes; wrong table/key/value types; hosted default;
exact `(iwiki_id, domain)` precedence; duplicate/incomplete override rejection; and
secret-safe errors. Assert immutable policy values:

```python
assert binding.specification_mode == "optional"
assert config.specifications.mode_for("team-wiki", "payments") == "strict"
assert config.specifications.mode_for("team-wiki", "other") == "optional"
```

- [ ] **Step 2: Run configuration tests and verify RED**

```bash
uv run pytest -q tests/test_specification_config.py tests/test_base.py tests/postgres/test_config.py
```

Expected: missing policy fields/models and top-level `specifications` rejection.

- [ ] **Step 3: Implement local policy parsing and template**

Add a defaulted field to both storage bindings so existing fixtures remain compatible:

```python
specification_mode: Literal["disabled", "optional", "strict"] = "optional"
```

Parse only `[specifications].mode`, add `specifications` to both Git and local PostgreSQL
allowlists, and add this commented template:

```toml
# [specifications]
# mode = "optional"  # disabled | optional | strict
```

- [ ] **Step 4: Implement hosted policy and exact precedence**

Add immutable `SpecificationOverride` and `HostedSpecificationsConfig` with
`mode_for(iwiki_id, domain)`. Extend `ServerConfig` and `_SERVER_TOP_LEVEL_FIELDS`.
Validate the complete override list at startup before building its exact-pair map; reject
duplicates even when their modes match.

- [ ] **Step 5: Bump to `0.7.207`, run focused tests, and commit**

```bash
uv sync --extra dev
uv run pytest -q tests/test_package.py tests/test_specification_config.py tests/test_base.py tests/postgres/test_config.py
git add src/iwiki_mcp/storage.py src/iwiki_mcp/base.py src/iwiki_mcp/postgres/config.py tests/test_specification_config.py tests/test_base.py tests/postgres/test_config.py pyproject.toml src/iwiki_mcp/__init__.py tests/test_package.py uv.lock
git commit -m "feat(spec): add specification mode policy"
```

Expected: focused configuration tests pass with sanitized errors.

## Task 3: Backend-neutral projection and Git JSONL adapter

**Files:**
- Create: `src/iwiki_mcp/specification_store.py`
- Create: `src/iwiki_mcp/specifications.py`
- Create: `tests/test_specification_store.py`
- Create: `tests/test_specifications.py`
- Modify: `src/iwiki_mcp/base.py`

- [ ] **Step 1: Write failing canonical-record and projection tests**

Test deterministic domain assembly, duplicate exclusion, valid/incomplete findings,
canonical ordering, metadata row version 1, zero-count lifecycle, stale/failed states,
and preservation of evidence only for unchanged binding ID plus scenario source hash.

```python
projection = assemble_projection("payments", pages, previous_evidence)
assert projection.scenario_count == 2
assert projection.binding_count == 4
assert projection.findings == ()
assert decode_jsonl(encode_jsonl(projection)) == projection
```

- [ ] **Step 2: Run service/store tests and verify RED**

```bash
uv run pytest -q tests/test_specification_store.py tests/test_specifications.py
```

Expected: missing store/service modules.

- [ ] **Step 3: Implement logical records and service interfaces**

Define projection/evidence types plus narrow adapter protocol:

```python
class SpecificationStore(Protocol):
    def replace_projection(self, projection: DomainProjection) -> dict[str, object]:
        raise NotImplementedError

    def search(self, domains: tuple[str, ...], query: str, limit: int) -> tuple[ScenarioRecord, ...]:
        raise NotImplementedError

    def context(self, domain: str, scenario_id: str) -> ScenarioContext | None:
        raise NotImplementedError

    def record_resolution(self, attempt: ResolutionAttempt) -> None:
        raise NotImplementedError

    def status(self, domain: str) -> ProjectionStatus:
        raise NotImplementedError
```

`assemble_projection()` accepts coherent page snapshots and never calls a graph.
`search()` ranks exact ID, title, phase-item names, then selector text with deterministic
identity tie-breaks.

- [ ] **Step 4: Implement Git JSONL persistence**

Add `base.specifications_path(base, domain)`. `GitSpecificationStore.prepare()` writes a
same-directory temporary file, flushes and `os.fsync()`s it, but returns a prepared
replace operation so the caller controls publication under the Git mutation lock.
Disabled mode must not open the path. Optional preparation failure must leave the old
file byte-identical.

- [ ] **Step 5: Bump to `0.7.208`, run focused tests, and commit**

```bash
uv sync --extra dev
uv run pytest -q tests/test_package.py tests/test_specification_store.py tests/test_specifications.py
git add src/iwiki_mcp/specification_store.py src/iwiki_mcp/specifications.py src/iwiki_mcp/base.py tests/test_specification_store.py tests/test_specifications.py pyproject.toml src/iwiki_mcp/__init__.py tests/test_package.py uv.lock
git commit -m "feat(spec): add canonical specification projection"
```

Expected: canonical and fault-injection tests pass.

## Task 4: Git lock-held mutation, mode matrix, and rebuild

**Files:**
- Modify: `src/iwiki_mcp/sync.py`
- Modify: `src/iwiki_mcp/server.py`
- Create: `tests/test_specification_git.py`
- Modify: `tests/test_server_write.py`
- Modify: `tests/test_server_update.py`
- Modify: `tests/test_server_delete.py`
- Modify: `tests/test_section.py`
- Modify: `tests/test_indexer.py`
- Modify: `tests/test_commit_and_push.py`
- Modify: `tests/test_sync_concurrency.py`

- [ ] **Step 1: Write failing ordinary-bypass and three-mode matrix tests**

Cover write/update/insert/delete/move/delete-page and `wiki_index`. Instrument parser,
projection, and graph factories and assert zero calls for ordinary pages and disabled
mode. Cover optional advisory storage and strict rejection before visible change.

```python
assert calls == {"parser": 0, "projection": 0, "graph": 0}
assert normal_result["page"] == "docs/concept/example.md"
assert spec_result["specifications"]["mode"] == "optional"
```

- [ ] **Step 2: Write failing strict rollback fault tests**

Inject failure at parser, projection preparation, page write, `index_domain`, projection
publish, staging, and local Git commit. Snapshot page, `index.jsonl`, `log.jsonl`, and
`specifications.jsonl`; assert strict failures restore all four, unstage the domain, and
create no commit. Separately inject graph-finalizer and push failures after a successful
local commit; assert the atomic page/projection commit remains and the result reports a
sanitized warning because graph refresh and remote publication are fail-soft.

- [ ] **Step 3: Run Git integration tests and verify RED**

```bash
uv run pytest -q tests/test_specification_git.py tests/test_server_write.py tests/test_server_update.py tests/test_server_delete.py tests/test_section.py tests/test_indexer.py tests/test_commit_and_push.py tests/test_sync_concurrency.py
```

Expected: mode integration and lock-held transaction tests fail.

- [ ] **Step 4: Expose one lock-held commit primitive**

Factor the current `_auto_commit_locked` and post-commit tail without changing
`commit_and_push()` results:

```python
def commit_locked(base: str, message: str, pathspec=None) -> dict:
    """Commit while the caller owns base_lock; never acquire it again."""
    return _auto_commit_locked(base, message, pathspec)

def unstage_locked(base: str, pathspec) -> None:
    """Restore the exact domain index to HEAD after a failed local commit."""
    paths = _normalized_pathspec(pathspec) or ()
    _run(base, "reset", "--mixed", "HEAD", "--", *paths)

def publish_committed(base: str, commit: dict, after_commit=None) -> dict:
    """Run graph finalization and sync after the atomic local commit stands."""
    return _publish_committed(base, commit, after_commit)
```

The server specification transaction acquires `base_lock` once, snapshots bytes, applies
page/log/index/projection, and calls `commit_locked`. A failed local commit restores all
snapshots and calls `unstage_locked` before releasing the lock. After a successful local
commit it releases the lock, runs `publish_committed`, and never rolls back merely because
graph refresh or remote push failed. The ordinary `commit_and_push()` path becomes
`auto_commit()` followed by the same `publish_committed()` helper. Do not nest the lock.

- [ ] **Step 5: Route all Git mutations through one candidate-page hook**

Add server helpers that determine normalized type before parsing and return a prepared
projection only for specification pages. Preserve current CAS/section and graph-finalizer
behavior. Optional projection failure commits ordinary Markdown/index changes with a
sanitized warning and leaves the prior projection untouched. `wiki_index` rebuilds from
one sorted domain snapshot.

- [ ] **Step 6: Bump to `0.7.209`, run Git regression tests, and commit**

```bash
uv sync --extra dev
uv run pytest -q tests/test_package.py tests/test_specification_git.py tests/test_server_write.py tests/test_server_update.py tests/test_server_delete.py tests/test_section.py tests/test_indexer.py tests/test_server_lint_sync.py tests/test_commit_and_push.py tests/test_sync_concurrency.py
git add src/iwiki_mcp/sync.py src/iwiki_mcp/server.py tests/test_specification_git.py tests/test_server_write.py tests/test_server_update.py tests/test_server_delete.py tests/test_section.py tests/test_indexer.py tests/test_commit_and_push.py tests/test_sync_concurrency.py pyproject.toml src/iwiki_mcp/__init__.py tests/test_package.py uv.lock
git commit -m "feat(spec): enforce Git specification modes"
```

Expected: all Git mode/rollback and existing mutation regressions pass.

## Task 5: PostgreSQL v6 schema, RLS, and grants

**Files:**
- Modify: `src/iwiki_mcp/postgres/migrations.py`
- Modify: `src/iwiki_mcp/postgres/store.py`
- Modify: `src/iwiki_mcp/http.py`
- Modify: `src/iwiki_mcp/server.py`
- Modify: `tests/postgres/test_migrations.py`
- Create: `tests/postgres/test_specification_migrations.py`
- Modify: `tests/postgres/test_auth.py`
- Modify: `tests/postgres/test_http.py`
- Modify: `tests/test_server_startup.py`

- [ ] **Step 1: Write failing v6 migration and isolation tests**

Assert migration history `(1, 2, 3, 4, 5, 6)`, three tables, composite tenant/domain/page
FKs, unique domain scenario identity, binding/evidence referential integrity, RLS enabled,
runtime grants, cross-tenant denial, and migration rollback on injected failure.

```python
assert tuple(item.version for item in MIGRATIONS) == (1, 2, 3, 4, 5, 6)
assert {
    "specification_scenarios", "specification_bindings", "specification_evidence",
} <= protected
```

- [ ] **Step 2: Run migration tests and verify RED**

```bash
uv run pytest -q tests/postgres/test_migrations.py tests/postgres/test_specification_migrations.py
```

Expected: schema version and table/RLS assertions fail. If the guarded disposable
database is unavailable, record the explicit pytest skip reason and continue with unit
planning; it must run before result close.

- [ ] **Step 3: Add forward-only migration v6**

Create scenario, binding, and evidence tables keyed by `iwiki_id` and `domain_id`.
Reference existing page and domain composite identities, cascade derived rows on scenario
deletion, enable RLS with the existing `database_principal_can_access` policy pattern,
and add indexes for domain ID/title/item/selector search. Do not add `FORCE ROW LEVEL
SECURITY` because the approved contract reuses the current protected-table pattern;
existing owner/BYPASSRLS runtime-principal guards remain authoritative. Do not alter
structural code-graph tables.

- [ ] **Step 4: Extend schema guards and runtime grants**

Set `require_schema_version(..., expected_version=6)` for hosted and stdio startup; add
all three tables to `_PROTECTED_TABLES`; add v6 policy statements and scoped SELECT/DML
grants to `provision_runtime_grant()`. Add `SCHEMA6_COMPATIBILITY_ROLLBACK_SQL` and
`rollback_v6_compatibility(confirm=True)` that drop only v6 objects and marker 6 so the
previous v5 application can start; test rollback, pinned v5 schema guard, and idempotent
v6 reapplication with NOBYPASSRLS roles. Update the concrete hosted guard in
`http._install_hosted_runtime()` and stdio guard in
`server._initialize_postgres_storage()` as invoked by `server.main()`; tests must prove
each rejects schema v5 and accepts schema v6 before installing its runtime.
Task 5 changes `postgres/store.py` only where the current protected-table and runtime-
grant/schema helpers live. Task 6 owns schema-v7 metadata/detachment, startup guards,
new projection APIs, savepoints, and page/projection transaction integration in that
shared file. Schema and transaction work remain one task because they change the same
files and together establish the optional-deletion durability invariant.

- [ ] **Step 5: Bump to `0.7.210`, run migration tests, and commit**

```bash
uv sync --extra dev
uv run pytest -q tests/test_package.py tests/postgres/test_migrations.py tests/postgres/test_specification_migrations.py tests/postgres/test_auth.py tests/postgres/test_http.py tests/test_server_startup.py
git add src/iwiki_mcp/postgres/migrations.py src/iwiki_mcp/postgres/store.py src/iwiki_mcp/http.py src/iwiki_mcp/server.py tests/postgres/test_migrations.py tests/postgres/test_specification_migrations.py tests/postgres/test_auth.py tests/postgres/test_http.py tests/test_server_startup.py pyproject.toml src/iwiki_mcp/__init__.py tests/test_package.py uv.lock
git commit -m "feat(spec): add PostgreSQL specification schema"
```

Expected: migration, rollback, RLS, and grant tests pass.

## Task 6: PostgreSQL v7 metadata, projection transactions, and parity

**Closes:** R-010 through R-013, R-018, R-022; AC-012 through AC-015, AC-020, and
AC-024.

**Files:**
- Modify: `src/iwiki_mcp/postgres/migrations.py`
- Modify: `src/iwiki_mcp/postgres/store.py`
- Modify: `src/iwiki_mcp/specification_store.py`
- Modify: `src/iwiki_mcp/http.py`
- Modify: `src/iwiki_mcp/server.py`
- Modify: `tests/postgres/test_migrations.py`
- Modify: `tests/postgres/test_specification_migrations.py`
- Modify: `tests/postgres/test_auth.py`
- Modify: `tests/postgres/test_http.py`
- Modify: `tests/postgres/test_code_graph_migrations.py`
- Modify: `tests/test_server_startup.py`
- Create: `tests/postgres/test_specifications.py`
- Modify: `tests/postgres/test_store.py`
- Modify: `tests/postgres/test_section_ops.py`
- Modify: `tests/test_specification_store.py`

- [ ] **Step 1: Write failing v7 migration, detachment, and rollback tests**

Assert migration history `(1, 2, 3, 4, 5, 6, 7)`, one
`specification_projection_state` table, non-null stored scenario `page_slug`, nullable
`page_id`, and a composite page foreign key with `ON DELETE SET NULL`. Insert a v6
scenario/binding/evidence fixture, apply v7, delete its page, and prove all logical rows
remain while scenario `page_id` becomes null and `page_slug` remains exact. Assert an
upgraded domain receives no invented last-success metadata row.

```python
assert tuple(item.version for item in MIGRATIONS) == (1, 2, 3, 4, 5, 6, 7)
assert scenario_after_delete["page_id"] is None
assert scenario_after_delete["page_slug"] == "payments/open-account"
assert projection_state_rows == []
```

Add catalog assertions for metadata primary/domain keys, nonnegative counts, RLS without
FORCE, command-specific read/write policies, runtime grants, NOBYPASSRLS isolation, and
no structural code-graph table change. Add rollback probes proving every non-literal
confirmation rejects before connection, clean v7 restores the v6 FK/columns/marker, and
one detached row rejects before any DDL or marker change.

- [ ] **Step 2: Run v7 migration tests and verify RED**

```bash
uv run pytest -q tests/postgres/test_migrations.py tests/postgres/test_specification_migrations.py tests/postgres/test_auth.py tests/test_server_startup.py
```

Expected: missing migration 7, metadata table, stored page slug, SET NULL foreign key,
schema-7 guards, and compatibility rollback. If `IWIKI_TEST_POSTGRES_DSN` is absent,
record its exact skip reason; the guarded real-database cases remain mandatory before
result close.

- [ ] **Step 3: Add forward-only migration v7**

Add `page_slug`, backfill it from the existing composite page reference, then make it
non-null. Replace the v6 page foreign key by making `page_id` nullable and adding the
same composite reference with `ON DELETE SET NULL`. Create
`specification_projection_state(iwiki_id, domain_id, markdown_revision,
projection_revision, scenario_count, binding_count, updated_at)` with a composite
domain foreign key and nonnegative count checks. Do not backfill a state row: the store
must report an upgraded projection stale until its first successful rebuild. Domain
deletion continues to cascade state and all derived rows.

```python
SPECIFICATION_METADATA_MIGRATION_STATEMENTS = (
    "ALTER TABLE iwiki.specification_scenarios ADD COLUMN page_slug text",
    """UPDATE iwiki.specification_scenarios s SET page_slug = p.slug
       FROM iwiki.pages p WHERE p.iwiki_id = s.iwiki_id
       AND p.domain_id = s.domain_id AND p.page_id = s.page_id""",
    "ALTER TABLE iwiki.specification_scenarios ALTER COLUMN page_slug SET NOT NULL",
    "ALTER TABLE iwiki.specification_scenarios DROP CONSTRAINT specification_scenarios_page_fk",
    "ALTER TABLE iwiki.specification_scenarios ALTER COLUMN page_id DROP NOT NULL",
    """ALTER TABLE iwiki.specification_scenarios
       ADD CONSTRAINT specification_scenarios_page_fk
       FOREIGN KEY (iwiki_id, domain_id, page_id)
       REFERENCES iwiki.pages (iwiki_id, domain_id, page_id)
       ON DELETE SET NULL""",
    """CREATE TABLE iwiki.specification_projection_state (
       iwiki_id text NOT NULL, domain_id bigint NOT NULL,
       markdown_revision text NOT NULL, projection_revision text NOT NULL,
       scenario_count integer NOT NULL CHECK (scenario_count >= 0),
       binding_count integer NOT NULL CHECK (binding_count >= 0),
       updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
       PRIMARY KEY (iwiki_id, domain_id),
       FOREIGN KEY (iwiki_id, domain_id)
       REFERENCES iwiki.domains (iwiki_id, domain_id) ON DELETE CASCADE)""",
)
SPECIFICATION_METADATA_MIGRATION = Migration(
    version=7,
    statements=SPECIFICATION_METADATA_MIGRATION_STATEMENTS,
)
```

Append `SPECIFICATION_METADATA_MIGRATION` to the existing `MIGRATIONS` tuple after
version 6; do not rewrite or reorder earlier migration objects.

- [ ] **Step 4: Extend RLS, grants, startup guards, and safe rollback**

Add metadata to `_PROTECTED_TABLES`, command-specific policies, and scoped runtime
SELECT/DML grants. Require exact schema 7 in hosted and stdio startup before runtime
installation. Add `SCHEMA7_COMPATIBILITY_ROLLBACK_SQL` and
`rollback_v7_compatibility(confirm=True)`. Under the migration advisory lock, rollback
first rejects any `specification_scenarios.page_id IS NULL`; only then may it drop state,
restore non-null page identity with `ON DELETE CASCADE`, drop stored `page_slug`, and
remove marker 7. No rejection path executes partial DDL or deletes projection data.

```python
SCHEMA7_COMPATIBILITY_ROLLBACK_SQL = f"""
SELECT pg_advisory_xact_lock({_MIGRATION_LOCK});
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM iwiki.specification_scenarios WHERE page_id IS NULL) THEN
    RAISE EXCEPTION 'schema v7 contains detached specification rows';
  END IF;
END $$;
DROP TABLE iwiki.specification_projection_state;
ALTER TABLE iwiki.specification_scenarios DROP CONSTRAINT specification_scenarios_page_fk;
ALTER TABLE iwiki.specification_scenarios ALTER COLUMN page_id SET NOT NULL;
ALTER TABLE iwiki.specification_scenarios ADD CONSTRAINT specification_scenarios_page_fk
  FOREIGN KEY (iwiki_id, domain_id, page_id)
  REFERENCES iwiki.pages (iwiki_id, domain_id, page_id) ON DELETE CASCADE;
ALTER TABLE iwiki.specification_scenarios DROP COLUMN page_slug;
DELETE FROM iwiki.schema_migrations WHERE version = 7;
"""

def rollback_v7_compatibility(dsn: str, *, confirm: bool = False) -> None:
    if confirm is not True:
        raise ValueError("schema v7 compatibility rollback requires confirm=True")
    # Execute the constant above in one advisory-locked transaction.
```

- [ ] **Step 5: Verify v6 upgrade, v7 reapplication, auth, and code-graph isolation**

```bash
uv run pytest -q tests/postgres/test_migrations.py tests/postgres/test_specification_migrations.py tests/postgres/test_auth.py tests/postgres/test_http.py tests/postgres/test_code_graph_migrations.py tests/test_server_startup.py
```

Expected: v6 upgrade, page detachment, metadata RLS/grants, exact schema guards,
fail-closed rollback, clean rollback/reapplication, and unchanged code-graph contracts
pass.

- [ ] **Step 6: Write failing projection, CAS, and persistence tests**

Cover coherent domain duplicate detection, strict page/projection rollback, optional
savepoint rollback with page commit and previous projection/metadata preserved, evidence
restart persistence, delete/move preservation rules, and ordinary/disabled zero
projection SQL. An upgraded v6 projection without metadata is stale; a successful empty
rebuild writes ready zero-count metadata.

```python
before = store.specification_context("docs", "stable-id")
result = store.update_page("docs", slug, candidate, revision)
after = store.specification_context("docs", "stable-id")
assert result["revision"] == revision + 1
assert after == before  # optional projection refresh fault preserved old rows
```

For both last-page and non-last-page deletion, inject failure after the page delete and
before projection replacement. Prove the page commits, old rows survive with null
`page_id` and stored slug, metadata remains byte/field unchanged, a new connection
reports stale and returns stale search/context, and a later rebuild removes obsolete rows
and becomes ready. Add fault seams for projection page/domain reads, not only INSERT/
DELETE statements. Add deterministic interleavings proving `record_specification_resolution`
cannot be lost by a concurrent projection refresh and cannot persist an old source hash.

- [ ] **Step 7: Run PostgreSQL store tests and verify RED**

```bash
uv run pytest -q tests/postgres/test_specifications.py tests/postgres/test_store.py tests/postgres/test_section_ops.py
```

Expected: missing projection operations/transaction hooks.

- [ ] **Step 8: Implement store projection and evidence operations**

Add backend-equivalent `replace_specification_projection`, `search_specifications`,
`specification_context`, `record_specification_resolution`, and
`specification_status`. All methods call `_require_read` or `_require_write` before SQL.
Use canonical logical records from `specification_store.py`. Successful replacement
updates scenario/binding/evidence rows and `specification_projection_state` atomically;
status compares the stored successful Markdown revision with the current coherent
specification-source revision. Missing metadata with sources or rows is stale; no
sources/rows/metadata is absent; zero-count metadata is ready.

- [ ] **Step 9: Integrate strict transaction and optional savepoint**

Prepare embeddings and specification parsing outside the transaction; re-read the
coherent domain and make all projection reads, evidence-preservation decisions, and
DELETE/INSERT/metadata decisions inside it. Strict uses the existing outer page
transaction. Optional wraps the complete derived refresh, including fallible page/domain
reads, in `connection.transaction()` nested savepoint, catches only a sanitized refresh
failure, and commits the page outer transaction. Resolution evidence writes acquire the
same domain lock/order as refresh so neither interleaving loses newer evidence or stores
an obsolete source hash.

- [ ] **Step 10: Add Git/PostgreSQL golden parity assertions**

Run identical Markdown through both adapters and compare normalized scenario/binding/
evidence dictionaries, excluding backend revision/storage metadata only. Canonicalize
accepted `checked_at` timestamps at the logical record boundary so `Z`, non-`Z` offsets,
and equivalent valid spellings round-trip identically through Git and PostgreSQL.

- [ ] **Step 11: Run combined schema and projection verification**

```bash
uv run pytest -q tests/postgres/test_migrations.py tests/postgres/test_specification_migrations.py tests/postgres/test_auth.py tests/postgres/test_http.py tests/postgres/test_code_graph_migrations.py tests/postgres/test_specifications.py tests/postgres/test_store.py tests/postgres/test_section_ops.py tests/test_server_startup.py tests/test_specification_store.py
```

Expected: schema upgrade/detachment/rollback, runtime guards, transaction/CAS/restart,
stale recovery, and adapter parity pass together. Guarded PostgreSQL cases may skip only
for the exact missing disposable-database reason and must run before result close.

- [ ] **Step 12: Bump to `0.7.213` and commit the complete PostgreSQL boundary**

```bash
uv sync --extra dev
uv run pytest -q tests/postgres/test_migrations.py tests/postgres/test_specification_migrations.py tests/postgres/test_auth.py tests/postgres/test_http.py tests/postgres/test_code_graph_migrations.py tests/postgres/test_specifications.py tests/postgres/test_store.py tests/postgres/test_section_ops.py tests/test_package.py tests/test_server_startup.py tests/test_specification_store.py
git add src/iwiki_mcp/postgres/migrations.py src/iwiki_mcp/postgres/store.py src/iwiki_mcp/specification_store.py src/iwiki_mcp/http.py src/iwiki_mcp/server.py tests/postgres/test_migrations.py tests/postgres/test_specification_migrations.py tests/postgres/test_auth.py tests/postgres/test_http.py tests/postgres/test_code_graph_migrations.py tests/test_server_startup.py tests/postgres/test_specifications.py tests/postgres/test_store.py tests/postgres/test_section_ops.py tests/test_specification_store.py pyproject.toml src/iwiki_mcp/__init__.py tests/test_package.py uv.lock
git commit -m "feat(spec): persist PostgreSQL specification projections"
```

Expected: schema v7 is tenant/domain protected and fail-closed on unsafe rollback;
transactions, CAS, restart, durable stale deletion, recovery, and parity pass.

## Task 7: Semantic search, context freshness, and graph-optional resolution

**Files:**
- Modify: `src/iwiki_mcp/specifications.py`
- Modify: `src/iwiki_mcp/server.py`
- Modify: `src/iwiki_mcp/codegraph/application.py`
- Modify: `src/iwiki_mcp/codegraph/linking.py`
- Modify: `src/iwiki_mcp/codegraph/sqlite_adapter.py`
- Modify: `src/iwiki_mcp/postgres/codegraph.py`
- Create: `tests/test_specification_tools.py`
- Create: `tests/test_specification_resolution.py`
- Modify: `tests/codegraph/test_linking.py`
- Modify: `tests/codegraph/test_application.py`
- Modify: `tests/codegraph/test_sqlite_adapter.py`
- Modify: `tests/codegraph/test_server_tools.py`
- Modify: `tests/postgres/test_code_graph_reader.py`
- Modify: `tests/postgres/test_code_graph_contract.py`

- [ ] **Step 1: Write failing semantic query and freshness tests**

Test query validation/scope/limit/ranking; complete context; `not_checked`, `fresh`,
`stale_spec`, `stale_graph`; graph recovery/state/reason/revision changes; and zero graph
resolution or write calls from search/context.

- [ ] **Step 2: Write failing coherent resolution tests**

Cover symbol/file/glob selectors, resolved/ambiguous/unresolved, graph unavailable reason
codes, non-primary, revision change, source change, write authorization, sanitized errors,
and no Markdown/structural-graph mutation.

```python
attempt = service.resolve("payments", "confirm-account-opening", resolver)
assert attempt.specification_hash == context.scenario.source_hash
assert {item.state for item in attempt.evidence} == {"resolved"}
assert structural_graph_rows_after == structural_graph_rows_before
```

- [ ] **Step 3: Run tool/resolution tests and verify RED**

```bash
uv run pytest -q tests/test_specification_tools.py tests/test_specification_resolution.py tests/codegraph/test_server_tools.py tests/postgres/test_code_graph_contract.py
```

Expected: missing query/resolution service APIs.

- [ ] **Step 4: Implement pure search/context and freshness**

Search/context operate only on persisted projections. Freshness compares scenario hash
first; ready evidence then compares graph revision; unavailable evidence compares only
the sanitized `(state, reason, revision-or-null)` fingerprint. Reads never persist.

- [ ] **Step 5: Implement private coherent selector adapter**

Define `SpecificationGraphSnapshot(revision, files, symbols)` and an internal
`SpecificationGraphResolver` protocol in `specifications.py`. Do not add methods to the
public `CodeGraphReader` protocol. Add private concrete
`specification_snapshot()` adapters to `SqliteCodeGraphReader` and
`PostgresCodeGraphReader`: SQLite reads active metadata plus file/symbol rows under its
existing query guard; PostgreSQL reads the active ready revision plus immutable rows in
one read transaction. Extract a pure binding-to-code-mapping helper in `linking.py`, then
reuse `resolve_selectors()` against that captured snapshot. Server rechecks status and
revision before evidence persistence. MCP read mode exposes no coherent snapshot, so all
its specification resolution attempts record one
`graph_unavailable/source_unavailable` result; normal `wiki_code_search/context` remain
usable. Add a private composition helper in `codegraph/application.py` that accepts the
current `CodeGraphRuntime`, returns a `SqliteCodeGraphReader` only when the local runtime
has a ready store path, and otherwise returns the unavailable resolver. In `server.py`,
select that helper for local Git, `_postgres_code_reader()` for hosted PostgreSQL, and the
unavailable resolver for MCP/source-unavailable mode. Tests in
`tests/codegraph/test_application.py` and `tests/test_specification_resolution.py` must
exercise all three selections and prove the public runtime/reader contracts stay
unchanged. Do not change structural schemas, relation enums, publication payloads,
ranking, or `wiki_code_*` tool inputs/responses.

- [ ] **Step 6: Implement coherent explicit refresh**

Capture scenario hash and graph revision, resolve every binding, recheck both, then write
one attempt transactionally. A changed revision records only
`graph_unavailable/revision_changed`, never partial targets.

- [ ] **Step 7: Bump to `0.7.214`, run query/resolution and graph tests, and commit**

```bash
uv sync --extra dev
uv run pytest -q tests/test_package.py tests/test_specification_tools.py tests/test_specification_resolution.py tests/codegraph/test_application.py tests/codegraph/test_linking.py tests/codegraph/test_sqlite_adapter.py tests/codegraph/test_schema.py tests/codegraph/test_query.py tests/codegraph/test_server_tools.py tests/postgres/test_code_graph_reader.py tests/postgres/test_code_graph_contract.py
git add src/iwiki_mcp/specifications.py src/iwiki_mcp/server.py src/iwiki_mcp/codegraph/application.py src/iwiki_mcp/codegraph/linking.py src/iwiki_mcp/codegraph/sqlite_adapter.py src/iwiki_mcp/postgres/codegraph.py tests/test_specification_tools.py tests/test_specification_resolution.py tests/codegraph/test_application.py tests/codegraph/test_linking.py tests/codegraph/test_sqlite_adapter.py tests/codegraph/test_server_tools.py tests/postgres/test_code_graph_reader.py tests/postgres/test_code_graph_contract.py pyproject.toml src/iwiki_mcp/__init__.py tests/test_package.py uv.lock
git commit -m "feat(spec): add semantic specification queries"
```

Expected: semantic tools pass and structural code-graph contract tests remain unchanged.

## Task 8: MCP registration, hosted authorization, status, and lint

**Files:**
- Modify: `src/iwiki_mcp/server.py`
- Modify: `src/iwiki_mcp/http.py`
- Modify: `tests/test_mcp_smoke.py`
- Modify: `tests/postgres/test_tool_matrix.py`
- Modify: `tests/postgres/test_http.py`
- Create: `tests/test_specification_status_lint.py`

- [ ] **Step 1: Write failing registration and authorization tests**

Add exact tool schemas/names. Search/context require bound read scope; resolve requires
write scope and bound primary. Hosted calls derive `iwiki_id` from `AuthContext`, reject
caller tenant identity, prevent cross-domain/tenant access, and redact secrets. For
`wiki_spec_search`, cover omitted `domains`, permitted arrays, denied domains, malformed
non-array/non-string values, duplicates, and cross-tenant requests.

- [ ] **Step 2: Write failing status/lint contract tests**

Assert the additive `wiki_status.specifications.domains[]` fields and every lint finding
and severity. Disabled returns no findings; optional findings are advisory; strict blocks
future invalid specification mutation only. Existing ordinary fields must compare equal.

- [ ] **Step 3: Run transport/status tests and verify RED**

```bash
uv run pytest -q tests/test_mcp_smoke.py tests/postgres/test_tool_matrix.py tests/postgres/test_http.py tests/test_specification_status_lint.py
```

Expected: missing tool names, auth-set entries, and specifications response blocks.

- [ ] **Step 4: Register tools and install hosted policy**

Register `wiki_spec_search`, `wiki_spec_context`, and `wiki_spec_resolve` with `_safe`.
Add context to `_READ_DOMAIN_TOOLS` and resolve to `_WRITE_DOMAIN_TOOLS`. Add an explicit
`wiki_spec_search` branch in `_authorize_tool`, parallel to `wiki_search`: parse the
`arguments["domains"]` array, reject malformed elements, pass every requested domain to
`authorize_domains(read_domains=...)`, and use the authenticated bound read scope when
omitted. Authorization must complete before any specification store access.
Extend hosted runtime installation with immutable specification policy; effective mode is
exact override, hosted default, then built-in optional and is never caller-controlled.

- [ ] **Step 5: Add status and lint assembly**

Compose independent specification blocks after ordinary reports. Catch sanitized
projection exceptions locally so ordinary status/lint data still returns. Do not place
specification findings in `engine.lint` structural categories.

- [ ] **Step 6: Bump to `0.7.215`, run transport/status tests, and commit**

```bash
uv sync --extra dev
uv run pytest -q tests/test_package.py tests/test_mcp_smoke.py tests/postgres/test_tool_matrix.py tests/postgres/test_http.py tests/test_specification_status_lint.py tests/test_server_lint_sync.py
git add src/iwiki_mcp/server.py src/iwiki_mcp/http.py tests/test_mcp_smoke.py tests/postgres/test_tool_matrix.py tests/postgres/test_http.py tests/test_specification_status_lint.py pyproject.toml src/iwiki_mcp/__init__.py tests/test_package.py uv.lock
git commit -m "feat(spec): expose authorized specification tools"
```

Expected: exact registration, authorization, status, lint, and redaction tests pass.

## Task 9: Ordinary Wiki regression, backend parity, and measurement evidence

**Files:**
- Create: `tests/test_specification_compatibility.py`
- Create: `tests/measurement/test_specification_paths.py`
- Modify: `tests/postgres/test_specifications.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Add adversarial no-graph compatibility tests**

Inject missing/disabled/stale/failed/unreachable graph readers and parser/projection
exceptions. Run ordinary write/update/section/delete/index/read/search/lint operations in
all modes and assert unchanged results plus zero specification blockers.

- [ ] **Step 2: Add complete backend golden tests**

Extend `tests/postgres/test_specifications.py` with one aggregate scenario, one
request/response/events scenario, one duplicate, one incomplete scenario, and resolution
evidence in every state. Compare normalized Git and PostgreSQL search/context/status/lint
outputs in a named golden-parity test. The guarded PostgreSQL suite may skip during task
work only for the explicit disposable-database reason; Task 11 requires it to run.

- [ ] **Step 3: Add deterministic measurement command**

Add the exact `measurement: deterministic path measurements without timing thresholds`
entry to `tool.pytest.ini_options.markers`. Build a fixed corpus and print one JSON record
with counts and `time.perf_counter_ns()` elapsed values:

```json
{"pages":100,"scenarios":200,"bindings":400,"projection_rebuild_ms":0.0,"search_ms":0.0,"context_ms":0.0,"resolution_ms":0.0}
```

Assertions validate keys/counts/nonnegative values only; elapsed time has no threshold.

- [ ] **Step 4: Run compatibility, graph-contract, and measurement suites**

```bash
uv run pytest -q tests/test_specification_compatibility.py tests/test_specification_store.py tests/test_specification_tools.py tests/test_specification_resolution.py
uv run pytest -q tests/postgres/test_specifications.py
uv run pytest -q tests/codegraph/test_schema.py tests/codegraph/test_query.py tests/codegraph/test_sqlite_adapter.py tests/codegraph/test_server_tools.py
uv run pytest --collect-only -q -m measurement tests/measurement/test_specification_paths.py
uv run pytest -q -m measurement tests/measurement/test_specification_paths.py -s
```

Expected: compatibility/parity suites pass; measurement emits one deterministic-shape
JSON record without a timing gate.

- [ ] **Step 5: Bump to `0.7.216`, verify, and commit regression coverage**

```bash
uv sync --extra dev
uv run pytest -q tests/test_package.py
git add tests/test_specification_compatibility.py tests/measurement/test_specification_paths.py tests/postgres/test_specifications.py pyproject.toml src/iwiki_mcp/__init__.py tests/test_package.py uv.lock
git commit -m "test(spec): prove compatibility and measure paths"
```

## Task 10: Authoring rules, user documentation, Wiki, and release version

**Files:**
- Modify: `src/iwiki_mcp/resources.py`
- Modify: `tests/test_resources.py`
- Modify: `README.md`
- Modify: `docs/README.ru.md`
- Modify: `docs/architecture.md`
- Modify: `templates/AGENTS.md.snippet`
- Modify: `templates/CLAUDE.md.snippet`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `tests/test_package.py`
- Modify: `uv.lock`

- [ ] **Step 1: Write failing resource/documentation assertions**

Tests must find creation criteria, stable ID, exact phase roles, both bindings, executable
test evidence, stale handling, graph-unavailable fallback, coherent-unit review, mode
matrix, tool names, and ordinary Wiki compatibility in `AUTHORING_RULES`. The same
`tests/test_resources.py` suite must open and assert the relevant contract in
`README.md`, `docs/README.ru.md`, `docs/architecture.md`,
`templates/AGENTS.md.snippet`, and `templates/CLAUDE.md.snippet`; no documentation path
in this task is verified only by existence.

- [ ] **Step 2: Run resource tests and verify RED**

```bash
uv run pytest -q tests/test_resources.py tests/test_resources_frontmatter.py
```

Expected: missing GWT authoring/lifecycle text.

- [ ] **Step 3: Publish English resource and client-neutral snippets**

Update `AUTHORING_RULES` with the eight approved rules and canonical syntax. Snippets tell
future iClaude/iCodex skills to call context before edits, resolve when graph is ready,
continue with repository search/tests when unavailable, and record test command/status/
revision in the task ledger. Do not embed client-specific hidden semantics.

- [ ] **Step 4: Update user and architecture documentation**

Document local/hosted TOML, precedence, mode matrix, fenced grammar, three tools,
status/lint, Git/PostgreSQL persistence, evidence freshness, authorization, no-graph
behavior, and measurement command in English README, Russian README, and architecture.

- [ ] **Step 5: Update durable Wiki through parent MCP calls**

Parent writes or updates an English BDD/event-sourcing guide and relevant architecture/
configuration sections using compare-and-swap revisions, then runs `wiki_lint`. Record the
page slugs/revisions and lint result in task ledger. No source hook writes Wiki.

- [ ] **Step 6: Bump implementation release version**

Task 9 commits `0.7.216`; this documentation repository change therefore bumps all four
version surfaces to `0.7.217`:

```text
pyproject.toml: version = "0.7.217"
src/iwiki_mcp/__init__.py: __version__ = "0.7.217"
tests/test_package.py: expected "0.7.217"
uv.lock: iwiki-mcp package version "0.7.217"
```

- [ ] **Step 7: Run docs/resource/version checks and commit**

```bash
uv sync --extra dev
uv run pytest -q tests/test_resources.py tests/test_resources_frontmatter.py tests/test_package.py
git diff --check
git add src/iwiki_mcp/resources.py tests/test_resources.py README.md docs/README.ru.md docs/architecture.md templates/AGENTS.md.snippet templates/CLAUDE.md.snippet pyproject.toml src/iwiki_mcp/__init__.py tests/test_package.py uv.lock
git commit -m "docs(spec): publish GWT maintenance guidance"
```

Expected: resources/version tests pass and diff has no whitespace errors.

## Task 11: Full verification, result reconciliation, and PR readiness

**Files:**
- Modify only demonstrated defects in their owning task files
- Update chain metadata only after fresh results

- [ ] **Step 1: Run all focused specification suites**

```bash
uv run pytest -q tests/engine/test_specifications.py tests/test_specification_config.py tests/test_specification_store.py tests/test_specifications.py tests/test_specification_git.py tests/test_specification_tools.py tests/test_specification_resolution.py tests/test_specification_status_lint.py tests/test_specification_compatibility.py
```

Expected: all focused tests pass.

- [ ] **Step 2: Run PostgreSQL and HTTP integration suites**

```bash
uv run pytest -q tests/postgres/test_specification_migrations.py tests/postgres/test_specifications.py tests/postgres/test_migrations.py tests/postgres/test_store.py tests/postgres/test_section_ops.py tests/postgres/test_tool_matrix.py tests/postgres/test_http.py
```

Expected: all tests pass against guarded disposable pgvector database; no unexplained
skip is accepted for result close.

- [ ] **Step 3: Run unchanged code-graph contracts and full regression**

```bash
uv run pytest -q tests/codegraph/test_schema.py tests/codegraph/test_query.py tests/codegraph/test_sqlite_adapter.py tests/codegraph/test_server_tools.py tests/postgres/test_code_graph_contract.py tests/postgres/test_code_graph_http.py
uv run pytest -q
uv run iwiki-mcp --help
```

Expected: all tests pass and CLI exits 0.

- [ ] **Step 4: Refresh optional local code graph and Wiki evidence**

Parent calls `wiki_code_index` only if the active local MCP server owns this checkout;
hosted `source_unavailable` is recorded as non-blocking. Parent updates required Wiki
pages, runs `wiki_lint`, and verifies no task-specific broken/stale/frontmatter/tag finding.

- [ ] **Step 5: Run formal result and repository safety gates**

Run `$check-chain result docs/superpowers/plans/2026-08-29-bdd-event-sourcing-verification.md`, then `superpowers:requesting-code-review` and `git-workflow` PR-readiness review. Fix only evidenced failures in their owning task and rerun the failed command plus full affected suite.

- [ ] **Step 6: Push branch and open PR to master**

```bash
git status --short
git log --oneline origin/master..HEAD
git push origin dev-bdd-event-sourcing-verification
gh pr create --base master --head dev-bdd-event-sourcing-verification --title "feat: add BDD event sourcing verification" --body-file /tmp/iwiki-bdd-event-sourcing-verification-pr.md
```

Expected: clean worktree, pushed commits, and PR URL. Parent creates the temporary PR body
outside repository with summary, full verification output, configuration/migration notes,
Wiki/code-graph evidence, and no secrets.

## Requirement coverage

| Specification requirements | Plan tasks |
|---|---|
| R-001, R-002, R-003, R-004, R-005: grammar, identity, roles, bindings | 1, 3 |
| R-006, R-007, R-008, R-009: modes and ordinary/disabled isolation | 2, 4, 6, 9 |
| R-010, R-011, R-012, R-013, R-014, R-015: parity, atomicity, evidence, queries, resolution | 3, 4, 6, 7, 8 |
| R-016: structural graph preservation | 7, 9, 11 |
| R-017: status/lint | 8 |
| R-018: authorization/redaction | 5, 6, 8 |
| R-019: agent rules | 10 |
| R-020: regressions/no-graph compatibility | 4, 6, 9, 11 |
| R-021: measurement evidence | 9, 11 |
| R-022: durable optional deletion and recovery | 6, 11 |
| AC-001, AC-002, AC-003, AC-004, AC-005, AC-006 | 1, 3, 4 |
| AC-007, AC-008, AC-009, AC-010, AC-011 | 2, 4, 6, 9 |
| AC-012, AC-013, AC-014, AC-015, AC-016, AC-017 | 3, 4, 6, 7 |
| AC-018, AC-019, AC-020, AC-021, AC-022, AC-023 | 7, 8, 9, 10, 11 |
| AC-024: schema-v7 stale deletion/rebuild contract | 6, 11 |

## Worker review protocol

For each task, parent dispatches one fresh implementation worker with exclusive ownership
of listed files and exact semantic route. After return, parent records the return event,
inspects the diff, runs the task commands, then dispatches separate spec-compliance and
code-quality reviewers before accepting the task commit. Any accepted intent/spec/plan
drift returns to the corresponding chain gate; implementers never edit approved meaning.
