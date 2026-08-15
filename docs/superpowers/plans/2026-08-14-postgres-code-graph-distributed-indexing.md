---
review:
  plan_hash: e61bff7101f60eb5
  last_run: 2026-08-15
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings:
    - id: F-001
      phase: coverage
      severity: CRITICAL
      section: "Task 1: Shared publication types, canonical batches, and configuration"
      section_hash: f37f897bf94151f1
      fragment: "staging_cleanup_limit == 100"
      text: "Hosted server ceilings and cleanup bounds lacked enforceable config fields."
      fix: "Define local/hosted defaults, maxima, exact existing test helpers, and cleanup limit."
      verdict: fixed
      verdict_at: 2026-08-14
    - id: F-002
      phase: coverage
      severity: CRITICAL
      section: "Task 4: PostgreSQL publication sessions and atomic activation"
      section_hash: 63d3a26227f0e043
      fragment: "Do not implement resume, reattach, transfer, supersession, or fencing fields."
      text: "The prior fencing token had no ownership-transfer operation and therefore no protective effect."
      fix: "Remove fencing and make sessions non-transferable with fixed owner and lease checks."
      verdict: fixed
      verdict_at: 2026-08-14
    - id: F-003
      phase: coverage
      severity: CRITICAL
      section: "Task 5: Target Markdown revision and code-to-wiki link derivation"
      section_hash: 9ceb913212d1ba9a
      fragment: "PostgreSQL `lint_domain` and local `engine.lint` explicitly compute the current canonical hash"
      text: "R-019 lint output had no implementation owner or test."
      fix: "Assign PostgreSQL/local lint implementation and exact revision/stale-link tests to Task 5."
      verdict: fixed
      verdict_at: 2026-08-14
    - id: F-004
      phase: dependencies
      severity: CRITICAL
      section: "Task 3: PostgreSQL migration and shared runtime-principal scope"
      section_hash: 8b775ce58253daa4
      fragment: "reject owner/`BYPASSRLS` roles"
      text: "The documented application/table-owner role could bypass RLS."
      fix: "Separate migrator, hosted service, and restricted direct roles; provision and validate shared grants."
      verdict: fixed
      verdict_at: 2026-08-14
    - id: F-005
      phase: dependencies
      severity: CRITICAL
      section: "Task 3: PostgreSQL migration and shared runtime-principal scope"
      section_hash: 8b775ce58253daa4
      fragment: "schema rollback-v4-compat --confirm"
      text: "Schema-v4 rollback could not start the previous application."
      fix: "Ship compatibility rollback, pinned pre-v4 smoke, and idempotent v4 reapplication."
      verdict: fixed
      verdict_at: 2026-08-14
    - id: F-006
      phase: verifiability
      severity: CRITICAL
      section: "Task 4: PostgreSQL publication sessions and atomic activation"
      section_hash: 63d3a26227f0e043
      fragment: "SET LOCAL lock_timeout = <lock_timeout_ms>"
      text: "pg_try_advisory_xact_lock could not satisfy configured busy timeout semantics."
      fix: "Use blocking advisory lock, transaction-local timeout, SQLSTATE mapping, and real contention test."
      verdict: fixed
      verdict_at: 2026-08-14
    - id: F-007
      phase: coverage
      severity: CRITICAL
      section: "Task 2: Portable snapshot builder and atomic SQLite publication profile"
      section_hash: 7d1bbbc508fd23d7
      fragment: "Generate one ephemeral publisher-instance identity"
      text: "SQLite ownership and lease checks were left to later defect discovery."
      fix: "Make fixed ownership, lease, expiry, and replacement-process rejection Task 2 requirements."
      verdict: fixed
      verdict_at: 2026-08-14
    - id: F-008
      phase: coverage
      severity: CRITICAL
      section: "Task 4: PostgreSQL publication sessions and atomic activation"
      section_hash: 63d3a26227f0e043
      fragment: "call cleanup_staging(now) before session creation"
      text: "Retention cleanup existed without an operational call site."
      fix: "Invoke bounded per-domain cleanup from every begin and test active-data preservation."
      verdict: fixed
      verdict_at: 2026-08-14
    - id: F-009
      phase: coverage
      severity: CRITICAL
      section: "Task 8: Runtime composition and MCP tool surface"
      section_hash: d25928ef2ad33b68
      fragment: "`_code_publication_service` returns `unsupported_storage` unless the current call is authenticated hosted PostgreSQL"
      text: "Publication-tool behavior for Git/SQLite and wiki_code_index matrix entries was undefined."
      fix: "Specify static registration, hosted-only execution, local direct adapter use, and complete matrix tests."
      verdict: fixed
      verdict_at: 2026-08-14
    - id: F-010
      phase: verifiability
      severity: CRITICAL
      section: "Task 9: Cross-adapter concurrency, integrity, and safe-error suite"
      section_hash: 707676f37492b28f
      fragment: "real initialize/tool JSON-RPC requests through /mcp"
      text: "The MCP lifecycle branch could degrade into a fake argument-mapping test."
      fix: "Run the same lifecycle through the real in-process hosted ASGI/MCP stack and disposable PostgreSQL."
      verdict: fixed
      verdict_at: 2026-08-14
    - id: F-011
      phase: verifiability
      severity: CRITICAL
      section: "Task 4: PostgreSQL publication sessions and atomic activation"
      section_hash: 63d3a26227f0e043
      fragment: "test_finalize_recomputes_header_graph_revision"
      text: "Target-side revision_mismatch had no explicit implementation test."
      fix: "Make header the single source and require independent target recomputation with a forged-revision test."
      verdict: fixed
      verdict_at: 2026-08-14
    - id: F-012
      phase: dependencies
      severity: CRITICAL
      section: "Task 3: PostgreSQL migration and shared runtime-principal scope"
      section_hash: 8b775ce58253daa4
      fragment: "hosted service role is rls scoped"
      text: "Hosted service access was undefined after RLS enabled protected tables."
      fix: "Provision explicit shared hosted-domain grants and test a non-owner service role against allowed and unprovisioned domains."
      verdict: fixed
      verdict_at: 2026-08-14
    - id: F-013
      phase: coverage
      severity: CRITICAL
      section: "Task 3: PostgreSQL migration and shared runtime-principal scope"
      section_hash: 8b775ce58253daa4
      fragment: "server._initialize_postgres_storage"
      text: "Stdio direct PostgreSQL would still migrate at startup while hosted startup became read-only."
      fix: "Apply one require_schema_version helper to both server.py and http.py and test that neither calls run_migrations."
      verdict: fixed
      verdict_at: 2026-08-14
    - id: F-014
      phase: consistency
      severity: CRITICAL
      section: "Task 4: PostgreSQL publication sessions and atomic activation"
      section_hash: 63d3a26227f0e043
      fragment: "Do not implement resume, reattach, transfer, supersession, or fencing fields."
      text: "Plan fencing fields contradicted the revised non-transferable-session specification."
      fix: "Remove fencing from types, schema, adapters, MCP signatures, tests, and coverage evidence."
      verdict: fixed
      verdict_at: 2026-08-14
    - id: F-015
      phase: dependencies
      severity: CRITICAL
      section: "Task 3: PostgreSQL migration and shared runtime-principal scope"
      section_hash: 8b775ce58253daa4
      fragment: "move the reusable store_factory"
      text: "Cross-module tests referenced fixtures local to test_store.py and test_http.py."
      fix: "Move store_factory, hosted_runtime, and shared graph builders into tests/postgres/conftest.py before dependent tests."
      verdict: fixed
      verdict_at: 2026-08-14
    - id: F-016
      phase: verifiability
      severity: CRITICAL
      section: "Task 3: PostgreSQL migration and shared runtime-principal scope"
      section_hash: 8b775ce58253daa4
      fragment: "SCHEMA_GUARD_PASSED"
      text: "The pre-v4 rollback subprocess could pass or fail for unrelated interpreter, dependency, config, or startup reasons."
      fix: "Pin interpreter, PYTHONPATH, dependency environment, isolated config/DSN, schema-guard sentinel, and exact CRUD/search assertions."
      verdict: fixed
      verdict_at: 2026-08-14
    - id: F-017
      phase: verifiability
      severity: CRITICAL
      section: "Task 6: PostgreSQL ready/fresh reader"
      section_hash: 7a3114100e2015fc
      fragment: "byte-for-byte equal normalized search results across all nine ranks"
      text: "Reader equivalence checked only entity order and could miss rank, field, or tie-break drift."
      fix: "Use the authoritative rank table and compare complete normalized SQLite/PostgreSQL results for all nine ranks."
      verdict: fixed
      verdict_at: 2026-08-14
    - id: F-018
      phase: dependencies
      severity: CRITICAL
      section: "Task 1: Shared publication types, canonical batches, and configuration"
      section_hash: f37f897bf94151f1
      fragment: "Create canonical_json_bytes"
      text: "Target revision recomputation had no single canonical JSON implementation owner."
      fix: "Create codegraph/canonical.py and migrate fingerprints, SQLite, wire batches, and PostgreSQL recomputation to it."
      verdict: fixed
      verdict_at: 2026-08-14
    - id: F-019
      phase: consistency
      severity: CRITICAL
      section: "Task 3: PostgreSQL migration and shared runtime-principal scope"
      section_hash: 8b775ce58253daa4
      fragment: "Replace runtime migration calls in both"
      text: "Hosted startup migration removal was not shared by stdio and was absent from the prior approved rollout contract."
      fix: "Revise the spec and plan to use operator-only migrations plus identical read-only runtime schema guards."
      verdict: fixed
      verdict_at: 2026-08-14
    - id: F-020
      phase: consistency
      severity: CRITICAL
      section: "Task 1: Shared publication types, canonical batches, and configuration"
      section_hash: f37f897bf94151f1
      fragment: "same six numeric defaults"
      text: "Plan-local publication settings were not enumerated in the prior specification config contract."
      fix: "Enumerate the same local and hosted numeric settings in the revised spec and Task 1."
      verdict: fixed
      verdict_at: 2026-08-14
    - id: F-021
      phase: coverage
      severity: CRITICAL
      section: "Task 9: Cross-adapter concurrency, integrity, and safe-error suite"
      section_hash: 707676f37492b28f
      fragment: "Assert the three closed sets independently"
      text: "A combined adapter/readiness error type erased the route-specific closed sets in R-028."
      fix: "Define publication, adapter, and readiness types/constants separately and test each route independently."
      verdict: fixed
      verdict_at: 2026-08-14
    - id: F-022
      phase: verifiability
      severity: CRITICAL
      section: "Task 10: Operator documentation and 20,000-file evidence"
      section_hash: 34858c04f8bf392e
      fragment: "test_postgres_publication_respects_server_ceilings"
      text: "Scale evidence covered only SQLite and did not exercise distributed server ceilings."
      fix: "Keep the 20,000-file SQLite boundary and add a real 2,000-file PostgreSQL ceiling/query-bound run required before result reconciliation."
      verdict: fixed
      verdict_at: 2026-08-14
    - id: F-023
      phase: dependencies
      severity: CRITICAL
      section: "Task 1: Shared publication types, canonical batches, and configuration"
      section_hash: f37f897bf94151f1
      fragment: "Register slow"
      text: "The scale baseline collected a slow test before its marker was registered."
      fix: "Register slow in Task 1 and exclude both slow and postgres_integration from the Task 10 failing baseline."
      verdict: fixed
      verdict_at: 2026-08-14
    - id: F-024
      phase: structure
      severity: CRITICAL
      section: "Task 2: Portable snapshot builder and atomic SQLite publication profile"
      section_hash: 7d1bbbc508fd23d7
      fragment: "1870-1906"
      text: "The previous codegraph/store.py anchor extended beyond the current 1906-line file."
      fix: "Use the verified 1870-1906 anchor."
      verdict: fixed
      verdict_at: 2026-08-14
    - id: F-025
      phase: consistency
      severity: CRITICAL
      section: "Task 9: Cross-adapter concurrency, integrity, and safe-error suite"
      section_hash: 707676f37492b28f
      fragment: "stage only the contract tests and version files"
      text: "The prior commit command pre-staged implementation files even when no demonstrated defect changed them."
      fix: "Return demonstrated defects to their owning task and stage only Task 9 contract tests in Task 9."
      verdict: fixed
      verdict_at: 2026-08-14
    - id: F-026
      phase: consistency
      severity: CRITICAL
      section: "1. Delivery rules and acceptance source"
      section_hash: 3027725226f66a0f
      fragment: "all four version surfaces"
      text: "Plan commits updated distribution metadata but omitted the runtime version and fixed package-version assertion, leaving the approved baseline failing."
      fix: "Add a checked 0.7.114 baseline repair, synchronize all four version surfaces in every later commit, and shift Task 1 through Task 10 versions."
      verdict: fixed
      verdict_at: 2026-08-14
    - id: F-027
      phase: consistency
      severity: CRITICAL
      section: "1. Delivery rules and acceptance source"
      section_hash: 3027725226f66a0f
      fragment: "strict `<500 ms`"
      text: "The existing strict startup gate of 100 ms rejected repeated healthy environment measurements between 101.742654 and 133.499218 ms."
      fix: "Apply the user-approved strict 500 ms startup gate through a failing boundary test, include it in the 0.7.114 baseline repair, and require the full suite to pass."
      verdict: fixed
      verdict_at: 2026-08-14
chain:
  intent: docs/superpowers/intents/2026-08-14-postgres-code-graph-distributed-indexing-intent.md
  spec: docs/superpowers/specs/2026-08-14-postgres-code-graph-distributed-indexing-design.md
---
# Distributed PostgreSQL Code Graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a local Python indexer publish one atomic code-graph snapshot through SQLite, direct PostgreSQL, or remote MCP and let PostgreSQL-backed servers query it without a checkout.

**Architecture:** Keep extraction local, add one versioned row-native `SnapshotPublisher`/`CodeGraphReader` contract, and place SQLite, PostgreSQL, and MCP adapters behind it. SQLite activates entity rows and an internal ready envelope with one atomic database replacement and reports explicit `commit_uncertain` when directory durability cannot be confirmed. PostgreSQL stages immutable tenant/domain snapshots, derives wiki links from target Markdown, then switches one active pointer under a short advisory transaction lock and optimistic graph/Markdown revision checks.

**Tech Stack:** Python 3.10+, dataclasses and `Protocol`, SQLite, psycopg 3, PostgreSQL transactions/advisory locks, FastMCP and the MCP Streamable HTTP client, pytest/pytest-asyncio.

---

## 1. Delivery rules and acceptance source

- Approved intent: `/home/ikeniborn/Documents/Project/iwiki-mcp/docs/superpowers/intents/2026-08-14-postgres-code-graph-distributed-indexing-intent.md` with body hash `a626f91a91ecfa50`.
- Approved spec: `/home/ikeniborn/Documents/Project/iwiki-mcp/docs/superpowers/specs/2026-08-14-postgres-code-graph-distributed-indexing-design.md` with body hash `3f99f03931a728f7` and commit `c1fa05d`.
- Plan artifact: `/home/ikeniborn/Documents/Project/iwiki-mcp/docs/superpowers/plans/2026-08-14-postgres-code-graph-distributed-indexing.md`.
- Implement requirements `R-001` through `R-030` and prove acceptance criteria `AC-01` through `AC-30` without changing the approved intent or spec.
- Preserve one repository per bound primary domain, Python-only extraction, existing SQLite query behavior, and ordinary PostgreSQL wiki behavior.
- Use TDD for every behavior change: focused failing test, observed failure, minimal implementation, focused pass, broader regression, version bump, commit.
- Every repository commit updates the same version in `pyproject.toml`, `uv.lock`, `src/iwiki_mcp/__init__.py`, and the fixed package-version assertion in `tests/test_package.py`. Existing branch history uses baseline `0.7.114`, Task 1 `0.7.115`, the initial Task 2 slice `0.7.116`, and the approved specification correction `0.7.117`. This checked plan revision prepares `0.7.118`; the Task 2 atomic-recovery commit uses `0.7.119`; Tasks 3–10 use `0.7.120` through `0.7.127`. Any demonstrated defect after Task 10 uses `0.7.128` or the next unused patch version.
- Before Task 1, repair the already reproduced baseline mismatch by setting all four version surfaces to `0.7.114`, and raise the existing code-graph startup release gate from strict `<100 ms` to user-approved strict `<500 ms` in the benchmark implementation and boundary test. Run `uv lock`, require `uv run pytest -q tests/test_package.py`, the focused startup boundary test, and `uv run pytest -q` to pass, then commit this checked plan correction with the synchronized version files, benchmark gate, and test. The observed pre-repair failures were `iwiki_mcp.__version__ == "0.7.108"` versus distribution metadata `0.7.113`, followed by environment startup measurements of `133.499218`, `125.841149`, `101.742654`, and `106.770350` ms against the old `<100 ms` gate.
- Do not push, publish production snapshots, create production credentials, or run destructive database operations outside the disposable `*_test` database.
- Parent agent alone updates iwiki task/wiki pages. Workers return repository paths and check evidence; they never call wiki write tools.

## 2. File responsibility map

| Path | Responsibility after implementation |
|---|---|
| `src/iwiki_mcp/codegraph/canonical.py` | Sole public canonical JSON bytes/hash implementation reused by fingerprints, SQLite, PostgreSQL, and wire batches |
| `src/iwiki_mcp/codegraph/publication.py` | Versioned rows, canonical serialization, batching, session/result types, publisher protocol |
| `src/iwiki_mcp/codegraph/reader.py` | Shared ready/fresh status and reader protocol |
| `src/iwiki_mcp/codegraph/schema.py` | Exact legacy/publication schema-v2 profiles and internal `code_graph_publication` DDL |
| `src/iwiki_mcp/codegraph/sqlite_adapter.py` | Shared-protocol SQLite publisher/reader, atomic embedded authority, reconciliation, and ordered locks |
| `src/iwiki_mcp/codegraph/mcp_adapter.py` | Authenticated Streamable HTTP publisher/reader client |
| `src/iwiki_mcp/codegraph/config.py` | Explicit modes, freshness, batch/session bounds, secret-free config validation |
| `src/iwiki_mcp/codegraph/indexer.py` | Local extraction to portable row set; no target-specific transport logic |
| `src/iwiki_mcp/codegraph/runtime.py` | Compose local indexer with selected publisher/reader and preserve fail-soft tools |
| `src/iwiki_mcp/codegraph/linking.py` | Shared selector parsing plus target-supplied Markdown snapshot resolution |
| `src/iwiki_mcp/postgres/codegraph.py` | PostgreSQL sessions, batches, finalization, active reads, queries, cleanup |
| `src/iwiki_mcp/postgres/config.py` | Hosted freshness, batch, session, and retention ceilings |
| `src/iwiki_mcp/postgres/migrations.py` | Idempotent migration 4, graph schema, shared principal grants, RLS, rollback compatibility SQL |
| `src/iwiki_mcp/postgres/store.py` | Markdown generation mutation, canonical snapshot provider, shared scoped wiki operations |
| `src/iwiki_mcp/admin.py` | Operator-only restricted-principal grants and schema-v4 compatibility rollback |
| `src/iwiki_mcp/postgres/auth.py` | Existing token identity/scope exposed to publication ownership checks |
| `src/iwiki_mcp/storage.py` and `src/iwiki_mcp/base.py` | Preserve immutable local PostgreSQL scope and admit `[code_graph]` config |
| `src/iwiki_mcp/http.py` | Hosted read-only schema guard plus read/write authorization classification for publication tools |
| `src/iwiki_mcp/server.py` | Stdio direct read-only schema guard, tool registration, backend composition, safe diagnostics |
| `src/iwiki_mcp/__init__.py` | Runtime package version synchronized in every repository commit |
| `src/iwiki_mcp/engine/lint.py` | SQLite stored/current canonical Markdown revision and stale-link diagnostics |
| `tests/codegraph/test_publication.py` | Canonical protocol, batching, lease/error contract |
| `tests/codegraph/test_schema.py` | Exact legacy/publication schema-v2 profiles and arbitrary-extra rejection |
| `tests/codegraph/test_sqlite_adapter.py` | SQLite publisher/reader atomicity, recovery, lock, integrity, and compatibility contract |
| `tests/codegraph/test_mcp_adapter.py` | Outbound MCP transport mapping and redaction |
| `tests/codegraph/publication_contract_support.py` | Adapter-neutral lifecycle harness used by SQLite/PostgreSQL/real hosted MCP |
| `tests/postgres/conftest.py` | Shared migrated store, hosted runtime, and graph fixtures used across PostgreSQL test modules |
| `tests/postgres/test_code_graph_migrations.py` | Migration, constraints, database-principal scope |
| `tests/postgres/test_code_graph_publication.py` | PostgreSQL lifecycle, conflicts, target link derivation |
| `tests/postgres/test_code_graph_reader.py` | Ready/fresh bounded search/context and source suppression |
| `tests/postgres/test_code_graph_http.py` | Hosted auth, ownership, publication tools, remote reads |
| `tests/postgres/test_code_graph_contract.py` | Same lifecycle over SQLite, restricted direct PostgreSQL, and real in-process hosted MCP |
| `tests/postgres/test_code_graph_scale.py` | Reduced generated PostgreSQL publication run proving server batch/query ceilings |
| `tests/postgres/test_code_graph_rollback.py` | Schema-v4 compatibility rollback and pre-v4 PostgreSQL smoke |
| `tests/eval/test_code_graph_publication_scale.py` | Generated 20,000-file publication evidence |
| `tests/test_server_import_closure.py` | Live-server eager import closure for the SQLite adapter |
| `eval/code_graph/runner.py` | Code-graph benchmark implementation, including the strict `<500 ms` startup release gate |
| `tests/eval/test_code_graph_runner.py` | Startup release-gate boundary and benchmark report evidence |
| `tests/test_package.py` | Distribution/runtime version equality and fixed release assertion synchronized in every commit |
| `README.md` and `docs/README.ru.md` | Operator modes, secrets, workflow, recovery, errors |
| `pyproject.toml` and `uv.lock` | One synchronized patch version per plan/implementation commit |

## 3. Ordered implementation tasks

### Task 1: Shared publication types, canonical batches, and configuration

**Depends on:** approved spec only.
**Requirements:** R-005–R-009, R-021, R-023, R-028, R-030.
**Problem closed:** all targets must consume one deterministic, bounded protocol and explicit mode/freshness settings.
**Files:**
- Create: `src/iwiki_mcp/codegraph/canonical.py`
- Create: `src/iwiki_mcp/codegraph/publication.py`
- Create: `src/iwiki_mcp/codegraph/reader.py`
- Create: `tests/codegraph/test_publication.py`
- Modify: `src/iwiki_mcp/codegraph/config.py:17-118`
- Modify: `src/iwiki_mcp/codegraph/fingerprint.py:1-40`
- Modify: `src/iwiki_mcp/codegraph/store.py:1-110`
- Modify: `src/iwiki_mcp/postgres/config.py:20-95, 210-280`
- Modify: `tests/codegraph/test_config_location_models.py`
- Modify: `tests/postgres/test_config.py`
- Modify: `pyproject.toml:3`
- Modify: `src/iwiki_mcp/__init__.py:2`
- Modify: `tests/test_package.py:8-15`

- [ ] **Step 1: Write failing protocol/config tests**

Add tests that instantiate the exact public values and verify canonical bytes, row sorting, ordinal/hash metadata, zero-age semantics, unknown-field rejection, and no secret-bearing config fields:

```python
def test_canonical_batches_are_stable_and_row_native():
    rows = {
        "repositories": ({"repository_id": "docs", "state": "rebuilding"},),
        "files": (
            {"file_id": "py:file:b", "path": "b.py"},
            {"file_id": "py:file:a", "path": "a.py"},
        ),
        "symbols": (),
        "relations": (),
    }
    first = tuple(iter_snapshot_batches(rows, max_rows=1, max_bytes=4096))
    second = tuple(iter_snapshot_batches(rows, max_rows=1, max_bytes=4096))
    assert first == second
    assert [batch.kind for batch in first] == [
        "repositories", "files", "files"
    ]
    assert [batch.ordinal for batch in first] == [0, 0, 1]
    assert all(batch.payload_hash.startswith("sha256:") for batch in first)
    assert b"root_path" not in b"".join(batch.payload for batch in first)


def test_modes_and_snapshot_age_are_explicit():
    config = CodeGraphConfig.from_mapping({
        "publish_mode": "mcp",
        "read_mode": "postgres",
        "max_snapshot_age_seconds": 0,
    })
    assert config.publish_mode == "mcp"
    assert config.read_mode == "postgres"
    assert config.max_snapshot_age_seconds == 0


def test_hosted_code_graph_limits_have_safe_defaults(tmp_path):
    config = load_server_config(
        _write_config(tmp_path, _server_toml() + "\n[code_graph]\n"),
        environ=_runtime_env(),
    )
    assert config.code_graph.max_snapshot_age_seconds == 86400
    assert config.code_graph.max_batch_rows == 1000
    assert config.code_graph.max_batch_bytes == 1_000_000
    assert config.code_graph.publication_session_ttl_seconds == 900
    assert config.code_graph.staging_retention_seconds == 86400
    assert config.code_graph.staging_cleanup_limit == 100
```

Reuse the existing `_write_config`, `_server_toml`, and `_runtime_env` helpers in `tests/postgres/test_config.py`; do not introduce undefined config fixtures.

- [ ] **Step 2: Run focused tests and capture expected failure**

Run:

```bash
uv run pytest -q tests/codegraph/test_publication.py tests/codegraph/test_config_location_models.py tests/postgres/test_config.py
```

Expected: FAIL during import because `codegraph.publication`, `SnapshotBatch`, and the new config fields do not exist.

- [ ] **Step 3: Implement the minimal shared contracts**

Create immutable types and protocols with these exact signatures; canonical JSON is UTF-8, sorted keys, compact separators, and rows sort by the existing table identity keys:

```python
RowKind = Literal["repositories", "files", "symbols", "relations"]
PublishMode = Literal["sqlite", "postgres", "mcp"]
PublicationErrorCode = Literal[
    "unauthorized", "scope_mismatch", "unsupported_storage", "busy",
    "session_expired", "invalid_batch", "batch_conflict",
    "snapshot_incomplete", "revision_mismatch", "snapshot_conflict",
    "markdown_unavailable",
]
AdapterErrorCode = Literal[
    "invalid_config", "remote_mcp_failed", "source_unavailable",
]
ReadinessErrorCode = Literal["missing_snapshot", "stale_snapshot"]

PUBLICATION_ERROR_CODES: tuple[PublicationErrorCode, ...] = (
    "unauthorized", "scope_mismatch", "unsupported_storage", "busy",
    "session_expired", "invalid_batch", "batch_conflict",
    "snapshot_incomplete", "revision_mismatch", "snapshot_conflict",
    "markdown_unavailable",
)
ADAPTER_ERROR_CODES: tuple[AdapterErrorCode, ...] = (
    "invalid_config", "remote_mcp_failed", "source_unavailable",
)
READINESS_ERROR_CODES: tuple[ReadinessErrorCode, ...] = (
    "missing_snapshot", "stale_snapshot",
)

@dataclass(frozen=True)
class SnapshotHeader:
    protocol_version: int
    schema_version: int
    repository_id: str
    source_fingerprint: str
    parser_fingerprint: str
    normalizer_version: str
    unicode_data_version: str
    languages: tuple[str, ...]
    expected_counts: Mapping[str, int]
    graph_payload_revision: str

@dataclass(frozen=True)
class SnapshotBatch:
    kind: RowKind
    ordinal: int
    row_count: int
    byte_count: int
    payload_hash: str
    payload: bytes

@dataclass(frozen=True)
class PublicationSession:
    session_id: str
    lease_expires_at: str
    base_snapshot_revision: str | None
    base_markdown_token: str | int

class SnapshotPublisher(Protocol):
    def begin(self, header: SnapshotHeader) -> PublicationSession: ...
    def publish_batch(self, session: PublicationSession, batch: SnapshotBatch) -> dict[str, object]: ...
    def finalize(self, session: PublicationSession) -> dict[str, object]: ...
    def abort(self, session: PublicationSession) -> dict[str, object]: ...

class CodeGraphReader(Protocol):
    def status(self) -> dict[str, object]: ...
    def search(self, request: ValidatedSearchRequest) -> dict[str, object]: ...
    def context(self, request: ContextRequest) -> dict[str, object]: ...
```

Create `canonical_json_bytes(value) -> bytes` and `canonical_sha256(value, *, prefix: bool) -> str` in `codegraph/canonical.py`. Replace both existing private `_canonical_json` implementations in `fingerprint.py` and `store.py`; publication batches and later PostgreSQL recomputation MUST import this module, and no third JSON serializer may be introduced. Fingerprints call `canonical_sha256(..., prefix=False)` to preserve their existing bare-hex contract; snapshot and wire revisions call it with `prefix=True`. Add byte-for-byte tests covering nested mappings, Unicode, empty rows, key order, existing fingerprint values, and equality between store/wire/target revision paths.

Extend `CodeGraphConfig` with defaults `publish_mode="sqlite"`, `read_mode="sqlite"`, `max_snapshot_age_seconds=86400`, `max_batch_rows=1000`, `max_batch_bytes=1_000_000`, `publication_session_ttl_seconds=900`, `staging_retention_seconds=86400`, and `staging_cleanup_limit=100`. Validate modes exactly, allow freshness `0`, require other bounds positive, and reject DSN/token/password/URL fields in the mapping. Read `IWIKI_CODE_GRAPH_MCP_URL` and `IWIKI_CODE_GRAPH_MCP_TOKEN` only in the MCP adapter, not in this dataclass or repr.

Add immutable `HostedCodeGraphConfig` with the same six numeric defaults and validated hard maxima (`max_batch_rows <= 5000`, `max_batch_bytes <= 5_000_000`, `publication_session_ttl_seconds <= 3600`, `staging_retention_seconds <= 604800`, `staging_cleanup_limit <= 1000`; freshness remains any non-negative integer). Allow optional top-level `[code_graph]` in hosted server TOML; hosted storage is always PostgreSQL, so it has no read/publish mode fields. Reject secret fields and unknown keys.

Register `slow: generated 20,000-file publication evidence` under `[tool.pytest.ini_options].markers` in `pyproject.toml` now, before any scale test is collected.

- [ ] **Step 4: Run focused and existing config/model tests**

Run:

```bash
uv run pytest -q tests/codegraph/test_publication.py tests/codegraph/test_config_location_models.py tests/postgres/test_config.py
```

Expected: PASS with stable payload hashes across two serializations and all prior config/model tests green.

- [ ] **Step 5: Bump version and commit Task 1**

Set all four version surfaces to `0.7.115`, inspect the staged diff, then run:

```bash
uv lock
git add pyproject.toml uv.lock src/iwiki_mcp/__init__.py tests/test_package.py src/iwiki_mcp/codegraph/canonical.py src/iwiki_mcp/codegraph/publication.py src/iwiki_mcp/codegraph/reader.py src/iwiki_mcp/codegraph/config.py src/iwiki_mcp/codegraph/fingerprint.py src/iwiki_mcp/codegraph/store.py src/iwiki_mcp/postgres/config.py tests/codegraph/test_publication.py tests/codegraph/test_config_location_models.py tests/postgres/test_config.py
git diff --cached --check
git commit -m "feat(codegraph): add shared publication protocol"
```

Expected: one commit containing only shared types/config/tests; no PostgreSQL or server behavior yet.

### Task 2: Portable snapshot builder and atomic SQLite publication profile

**Depends on:** Task 1 shared types and config plus approved specification hash `3f99f03931a728f7`.
**Requirements:** R-003, R-005–R-011, R-013–R-020, R-025, R-028–R-029.
**Problem closed:** local SQLite must use the shared row lifecycle while one database replacement activates entity rows and ready evidence together, directory-sync uncertainty is explicit and retryable, and legacy query/source behavior remains available.
**Files:**
- Modify: `src/iwiki_mcp/codegraph/publication.py`
- Modify: `src/iwiki_mcp/codegraph/schema.py`
- Modify: `src/iwiki_mcp/codegraph/indexer.py`
- Modify: `src/iwiki_mcp/codegraph/store.py`
- Modify: `src/iwiki_mcp/codegraph/sqlite_adapter.py`
- Modify: `src/iwiki_mcp/server.py`
- Modify: `tests/codegraph/test_publication.py`
- Create: `tests/codegraph/test_schema.py`
- Modify: `tests/codegraph/test_indexer_runtime.py`
- Modify: `tests/codegraph/test_store.py`
- Modify: `tests/codegraph/test_sqlite_adapter.py`
- Modify: `tests/test_server_import_closure.py`
- Modify: `pyproject.toml:3`
- Modify: `src/iwiki_mcp/__init__.py:2`
- Modify: `tests/test_package.py:8-15`

- [ ] **Step 1: Write failing schema, recovery, and uncertainty tests**

Add exact-profile tests in `tests/codegraph/test_schema.py`. `create_schema()` remains the legacy five-table constructor; the new `create_publication_schema()` constructs the same public tables and indexes plus only `code_graph_publication`:

```python
def test_schema_v2_accepts_only_legacy_or_publication_profile(tmp_path):
    legacy = sqlite3.connect(tmp_path / "legacy.db")
    create_schema(legacy)
    validate_schema(legacy)

    published = sqlite3.connect(tmp_path / "published.db")
    create_publication_schema(published)
    validate_schema(published)
    public_tables = tuple(sorted(
        row[0]
        for row in published.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        if row[0] != "code_graph_publication"
    ))
    assert public_tables == tuple(sorted(TABLES))

    published.execute("CREATE TABLE unexpected(value TEXT)")
    with pytest.raises(CodeGraphSchemaError):
        validate_schema(published)
```

Extend the real `graph_fixture` in `tests/codegraph/test_sqlite_adapter.py`; it may inject clock and failure hooks but MUST use actual files, locks, `CodeGraphStore`, selector resolver, publisher, and reader. Add these observable cases:

```python
def test_directory_sync_failure_reconciles_by_active_session(
    graph_fixture, monkeypatch,
):
    publisher, reader, built = graph_fixture.sqlite_contract()
    session = graph_fixture.publish_batches(publisher, built)
    monkeypatch.setattr(
        publisher, "_sync_canonical_directory", graph_fixture.raise_os_error
    )
    first = publisher.finalize(session)
    assert first["error"] == "commit_uncertain"
    assert reader.status()["snapshot_revision"] == first["snapshot_revision"]

    monkeypatch.setattr(
        publisher, "_sync_canonical_directory", graph_fixture.sync_directory
    )
    second = publisher.finalize(session)
    assert second["state"] == "ready"
    assert second["snapshot_revision"] == first["snapshot_revision"]


def test_post_replace_journal_failure_uses_embedded_terminal_result(
    graph_fixture, monkeypatch,
):
    publisher, reader, built = graph_fixture.sqlite_contract()
    session = graph_fixture.publish_batches(publisher, built)
    monkeypatch.setattr(
        publisher, "_record_external_terminal", graph_fixture.raise_store_error
    )
    result = publisher.finalize(session)
    assert result["state"] == "ready"
    assert reader.status()["snapshot_revision"] == result["snapshot_revision"]


def test_publication_profile_preserves_public_rows_and_backup(
    graph_fixture, tmp_path,
):
    publisher, reader, built = graph_fixture.sqlite_contract(
        git_remote="iwiki-publication:legitimate"
    )
    result = graph_fixture.publish_complete(publisher, built)
    assert reader.store.stable_rows("repositories")[0]["git_remote"] == (
        "iwiki-publication:legitimate"
    )
    backup = tmp_path / "copied.db"
    shutil.copy2(reader.store.path, backup)
    copied = graph_fixture.reader_for(backup)
    assert copied.status()["snapshot_revision"] == result["snapshot_revision"]
```

Also add named tests for reader pause during cache-sidecar publication, restart after persistent sidecar failure, incomplete/corrupt copy rejection, embedded-envelope corruption, active wrong-domain/revision rejection, activation-time lease expiry, Markdown mutation during derivation, writer interleaving after final hash, reader hash-to-link race, sanitized lock timeout, and startup import closure. Each failure injection asserts old-or-new complete visibility and absence of absolute paths or raw OS text.

- [ ] **Step 2: Run the revised SQLite contract and observe RED**

Run:

```bash
uv run pytest -q tests/codegraph/test_publication.py tests/codegraph/test_schema.py tests/codegraph/test_sqlite_adapter.py tests/codegraph/test_indexer_runtime.py tests/codegraph/test_store.py tests/test_server_import_closure.py
```

Expected: FAIL because `commit_uncertain`, the publication schema profile, embedded authority, reconciliation, and exact compatibility behavior are absent or contradicted by the current adapter.

- [ ] **Step 3: Add the exact internal publication profile and safe error**

In `schema.py`, keep `SCHEMA_VERSION = 2` and public `TABLES` unchanged. Add exact `PUBLICATION_TABLE_DDL`, `EXPECTED_PUBLICATION_TABLE_DDL`, and `create_publication_schema(connection)`. The internal table has one row selected by `singleton = 1` and exact typed columns for `format_version`, `state`, `domain`, `repository_id`, `session_id`, `graph_payload_revision`, `snapshot_revision`, `markdown_revision`, canonical `counts_json`, `indexed_at`, canonical `terminal_result_json`, `content_digest`, and `envelope_digest`. `validate_schema()` accepts only the exact legacy object map or exact publication object map; it still rejects every arbitrary table, index, column, or DDL change.

In `publication.py`, add `commit_uncertain` to `PUBLICATION_ERROR_CODES` only. In `store.py`, add focused primitives that write/read one internal envelope, calculate canonical SHA-256 over every persisted column of the five public tables in stable-key order, calculate the envelope digest excluding itself, and validate counts/revisions/digests. Do not add the internal table to `stable_rows()`, `_snapshot_revision`, wire rows, search/context, or exports. Cache a successful full validation only for the same bounded storage stamp; a changed stamp or new process performs full validation again. Keep strict existing database-plus-sidecar validation for a legacy database without the internal table.

- [ ] **Step 4: Implement atomic activation, reconciliation, and ordered locks**

Keep the fixed-owner session journal in its unique private staging directory; it is not active readiness authority. Build the publication-profile graph database separately, write and verify its ready envelope, checkpoint it, and prepare any prior backup before activation. Perform batch decode, normalized-row validation, selector capture, link derivation, SQLite construction, content/envelope digesting, and backup outside graph exclusive.

For activation, acquire Wiki shared, recompute the canonical Markdown hash, then acquire graph exclusive. Inside that ordered section repeat owner, staging state, lease, active graph revision, and Markdown revision checks immediately before `os.replace`. No graph-to-Wiki acquisition is allowed. Map every lock `Timeout` to sanitized `busy`. An expired session returns `session_expired` and cannot activate.

Treat successful `os.replace` as the logical commit point. Successful directory sync returns the embedded ready result. `CodeGraphPublishedError` or its exact directory-sync equivalent returns `commit_uncertain`; do not hide it as ready and do not roll back. Repeated same-owner `finalize` reads the active envelope: matching session/revision retries directory sync, different active revision returns `snapshot_conflict`, and another sync failure remains `commit_uncertain`. No batch or abort is accepted after uncertainty. If external journal persistence fails after a confirmed replacement, the matching embedded terminal result wins. After process loss, another adapter never resumes the old session and status reports the complete active database.

Reader `status`, `search`, and `context` acquire Wiki shared then graph read from current Markdown-hash comparison through response materialization. A later Markdown change suppresses links without hiding non-Wiki graph rows. The cache sidecar is best-effort for the publication profile and can be regenerated from the embedded envelope; absence, staleness, or failure cannot make a valid new snapshot non-ready. Preserve guarded local `include_source=true`. Add only the eager import required by `tests/test_server_import_closure.py`; Task 8 still owns runtime/tool composition.

- [ ] **Step 5: Run focused GREEN and lock-structure probes**

Run:

```bash
uv run pytest -q tests/codegraph/test_publication.py tests/codegraph/test_schema.py tests/codegraph/test_sqlite_adapter.py tests/codegraph/test_indexer_runtime.py tests/codegraph/test_store.py tests/test_server_import_closure.py
```

Expected: PASS. Structural probes record batch decoding, validation, link derivation, SQLite build, digest calculation, and backup outside graph exclusive; the only activation order observed is Wiki shared then graph exclusive.

- [ ] **Step 6: Run complete SQLite and repository regressions**

Run:

```bash
uv run pytest -q tests/codegraph
uv run pytest -q
uv run flake8 src tests
uv lock --check
uv run iwiki-mcp --help
```

Expected: all tests and lint pass, CLI help exits zero, lockfile is current, legacy/query/source behavior is unchanged, and no aggregate-only timing failure remains unexplained.

- [ ] **Step 7: Bump version and commit Task 2 recovery**

Set all four version surfaces to `0.7.119`, then run:

```bash
uv lock
git add pyproject.toml uv.lock src/iwiki_mcp/__init__.py tests/test_package.py src/iwiki_mcp/codegraph/publication.py src/iwiki_mcp/codegraph/schema.py src/iwiki_mcp/codegraph/indexer.py src/iwiki_mcp/codegraph/store.py src/iwiki_mcp/codegraph/sqlite_adapter.py src/iwiki_mcp/server.py tests/codegraph/test_publication.py tests/codegraph/test_schema.py tests/codegraph/test_indexer_runtime.py tests/codegraph/test_store.py tests/codegraph/test_sqlite_adapter.py tests/test_server_import_closure.py
git diff --cached --check
git commit -m "fix(codegraph): make SQLite publication atomic"
```

Expected: one Task 2 recovery commit with internal ready authority, explicit uncertainty, exact legacy/publication profiles, and full regression evidence; no Task 8 composition.

### Task 3: PostgreSQL migration and shared runtime-principal scope

**Depends on:** Task 1 protocol identity and bounds.
**Requirements:** R-001–R-004, R-010–R-015, R-018, R-030.
**Problem closed:** durable rows, Markdown generation, restricted direct credentials, and rollback compatibility need database-enforced integrity before publication code can write them.
**Files:**
- Create: `tests/postgres/test_code_graph_migrations.py`
- Create: `tests/postgres/test_code_graph_rollback.py`
- Modify: `src/iwiki_mcp/postgres/migrations.py:44-228`
- Modify: `src/iwiki_mcp/postgres/store.py:43-100`
- Modify: `src/iwiki_mcp/admin.py:36-120, 428-532`
- Modify: `src/iwiki_mcp/http.py:410-450`
- Modify: `src/iwiki_mcp/server.py:2709-2735`
- Modify: `src/iwiki_mcp/storage.py:45-80`
- Modify: `src/iwiki_mcp/base.py:180-260`
- Modify: `tests/postgres/test_migrations.py`
- Modify: `tests/postgres/test_auth.py`
- Modify: `tests/postgres/conftest.py`
- Modify: `tests/postgres/test_store.py`
- Modify: `tests/postgres/test_http.py`
- Modify: `pyproject.toml:3`
- Modify: `src/iwiki_mcp/__init__.py:2`
- Modify: `tests/test_package.py:8-15`

- [ ] **Step 1: Write failing migration and scope tests**

First move the reusable `store_factory` from `tests/postgres/test_store.py` and `hosted_runtime` plus its config helpers from `tests/postgres/test_http.py` into `tests/postgres/conftest.py`; the original modules consume the shared fixtures without changing behavior. Assert migration 4 creates the approved objects plus `domains.markdown_generation`, gives domain state a unique database-assigned lock ID, uses composite graph foreign keys, and rejects table-owner, `BYPASSRLS`, unmapped, and out-of-scope runtime roles. Add a hosted service fixture with grants for `docs` but not `private`:

```python
def test_graph_migration_enforces_scope_and_snapshot_foreign_keys(clean_postgres):
    run_migrations(_settings(clean_postgres))
    tables = _iwiki_tables(clean_postgres)
    assert {
        "code_graph_domain_state",
        "code_graph_publication_sessions",
        "code_graph_snapshots",
        "code_graph_batches",
        "code_graph_files",
        "code_graph_symbols",
        "code_graph_relations",
        "code_graph_wiki_links",
        "database_principal_domain_grants",
    } <= tables
    assert _domain_columns(clean_postgres)["markdown_generation"].default == "0"
    assert _domain_state_has_unique_lock_id(clean_postgres)
    assert _call_scope_check(clean_postgres, "missing", "docs", write=True) is False


def test_owner_and_bypass_roles_are_invalid_direct_principals(role_database):
    assert validate_direct_principal(role_database.owner_dsn)["error"] == "invalid_config"
    assert validate_direct_principal(role_database.bypass_dsn)["error"] == "invalid_config"
    assert validate_direct_principal(role_database.restricted_dsn) is None


def test_hosted_service_role_is_rls_scoped(role_database):
    provision_runtime_grant(
        role_database.admin_dsn,
        principal=role_database.hosted_role,
        iwiki_id="wiki-a",
        read_domains=["docs"],
        write_domains=["docs"],
        runtime="hosted",
    )
    assert _visible_page_slugs(role_database.hosted_dsn) == {"docs/page"}
    assert role_database.hosted_role_is_owner is False
    assert role_database.hosted_role_bypasses_rls is False
    assert role_database.protected_tables_force_rls is False
```

Define `role_database` in `tests/postgres/test_code_graph_migrations.py` from `clean_postgres`. It creates uniquely suffixed owner, `BYPASSRLS`, restricted, and unmapped roles with admin credentials, returns redacted DSNs, and drops only those roles in fixture teardown. Never print role passwords or reuse a non-test database.

- [ ] **Step 2: Run migration tests and observe schema-version failure**

Run:

```bash
uv run pytest -q tests/postgres/test_migrations.py tests/postgres/test_code_graph_migrations.py tests/postgres/test_code_graph_rollback.py tests/postgres/test_auth.py tests/postgres/test_http.py
```

Expected: PostgreSQL tests skip when `IWIKI_TEST_POSTGRES_DSN` is absent; when configured, FAIL because schema version remains 3 and graph/scope objects are absent.

- [ ] **Step 3: Add idempotent migration 4, restricted-principal provisioning, and read-only startup schema check**

Add one contiguous `Migration(version=4, statements=GRAPH_MIGRATION_STATEMENTS)` containing the graph tables from spec Section 7.1, `domains.markdown_generation`, database-assigned unique `domain_lock_id`, state/kind checks, composite foreign keys, and active-ready pointer enforcement. Publication tables contain fixed owner and lease columns and no fencing counter/token. Every version-4 statement must tolerate reapplication after the compatibility marker is removed. Use ordinary `ENABLE ROW LEVEL SECURITY`, explicitly omit `FORCE ROW LEVEL SECURITY`, and ensure schema-owner credentials are never accepted as runtime credentials. Policies on domain-scoped Markdown and graph tables call the same fixed qualified principal-scope function:

```sql
CREATE FUNCTION iwiki.database_principal_can_access(
    requested_iwiki text,
    requested_domain bigint,
    requested_write boolean
) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, iwiki
AS $function$
    SELECT EXISTS (
        SELECT 1
        FROM iwiki.database_principal_domain_grants g
        WHERE g.principal = session_user
          AND g.iwiki_id = requested_iwiki
          AND g.domain_id = requested_domain
          AND g.can_read
          AND (NOT requested_write OR g.can_write)
    )
$function$;
```

Add operator commands `iwiki-mcp principal grant` and `iwiki-mcp principal inspect`. They accept an existing PostgreSQL role name plus `--iwiki`, repeated read/write domains, and `--runtime hosted|direct`; reject owner/`BYPASSRLS` roles; write `database_principal_domain_grants`; and grant only required schema/table/sequence/function privileges. They never create a login or accept its password. Hosted domain provisioning inserts or verifies the service role's shared read/write row before enabling tokens for that domain. `PostgresStore` validates both runtime role shapes, while bearer-token checks remain the finer hosted authorization boundary. Policies cover pages by `domain_id`, chunks/ordinary links through their source page, and every graph table by composite tenant/domain key.

Replace runtime migration calls in both `http.prepare_runtime` and `server._initialize_postgres_storage` with the same read-only `require_schema_version(4)` helper. Tests monkeypatch `run_migrations` to fail if either runtime entry point calls it, verify both reject schema 3 and schema 5 before installing/listening, and verify only the operator/admin command uses schema-owner credentials. Allow top-level `code_graph` beside PostgreSQL `storage` in `base._postgres_binding`, keeping `iwiki_id/read/write/primary` immutable.

- [ ] **Step 4: Add schema-v4 compatibility rollback and pre-v4 application test**

Expose reviewed `SCHEMA4_COMPATIBILITY_ROLLBACK_SQL` and operator command `iwiki-mcp schema rollback-v4-compat --confirm`. In one transaction it takes `_MIGRATION_LOCK`, verifies migration 4 is current, verifies every configured runtime principal remains mapped and non-owner/non-`BYPASSRLS`, then deletes only version 4 from `iwiki.schema_migrations`. It retains graph tables, generation values, policies, grants, and data. Without `--confirm`, return a dry-run object and mutate nothing.

`tests/postgres/test_code_graph_rollback.py` exports pinned pre-v4 commit `d4f4e19a50454cb7381268c3fefbcb3135e36929` into `tmp_path` with `git archive`, applies migration 4, provisions the hosted service role, and runs the compatibility rollback. Launch the exported source with `sys.executable`, the current installed dependency environment, `PYTHONPATH=<exported>/src`, an isolated project/server config, and the disposable service-role DSN. A small exported-source driver calls its runtime initialization first, writes a distinct `SCHEMA_GUARD_PASSED` sentinel only after initialization returns, then performs page create/read/update/delete and lexical search. The parent requires exit code 0, the sentinel, and exact CRUD/search assertions; any import, config, dependency, connection, or startup failure fails the test rather than masquerading as rollback success. Reapply current migration 4 afterward and assert one schema row plus preserved Markdown and graph staging rows.

- [ ] **Step 5: Run migration/config/auth/rollback tests**

Run:

```bash
uv run pytest -q tests/postgres/test_migrations.py tests/postgres/test_code_graph_migrations.py tests/postgres/test_code_graph_rollback.py tests/postgres/test_auth.py tests/postgres/test_http.py tests/postgres/test_admin.py tests/postgres/test_config.py tests/test_base.py
```

Expected: migration history `(1, 2, 3, 4)`, concurrent admin migration applies version 4 once, hosted and stdio startup are read-only, non-owner hosted/direct roles enforce shared Markdown/graph scope, owner/`BYPASSRLS` runtime config fails, rollback lets pinned pre-v4 code pass its schema guard and CRUD/search smoke, and reapplication preserves data.

- [ ] **Step 6: HUMAN CHECKPOINT — review migration, roles, and rollback diff**

Parent reviews staged SQL, role grants, RLS policies, startup change, and compatibility rollback against R-002/R-004/R-013/R-018. Stop if documented direct credentials own protected tables, hold `BYPASSRLS`, can cross scope, if rollback removes data/policies, if a graph cascade can delete Markdown, or if active pointer can reference non-ready/cross-domain data.

- [ ] **Step 7: Bump version and commit Task 3**

Set all four version surfaces to `0.7.120`, then run:

```bash
uv lock
git add pyproject.toml uv.lock src/iwiki_mcp/__init__.py tests/test_package.py src/iwiki_mcp/postgres/migrations.py src/iwiki_mcp/postgres/store.py src/iwiki_mcp/admin.py src/iwiki_mcp/http.py src/iwiki_mcp/server.py src/iwiki_mcp/storage.py src/iwiki_mcp/base.py tests/postgres/conftest.py tests/postgres/test_store.py tests/postgres/test_migrations.py tests/postgres/test_code_graph_migrations.py tests/postgres/test_code_graph_rollback.py tests/postgres/test_auth.py tests/postgres/test_http.py tests/postgres/test_admin.py
git diff --cached --check
git commit -m "feat(postgres): add code graph snapshot schema"
```

Expected: one forward-migration commit; no graph tool is enabled yet.

### Task 4: PostgreSQL publication sessions and atomic activation

**Depends on:** Tasks 1 and 3.
**Requirements:** R-005–R-015, R-028–R-030.
**Problem closed:** direct and hosted targets need idempotent chunks, fixed ownership, lease validation, optimistic conflict, and atomic active visibility.
**Files:**
- Create: `src/iwiki_mcp/postgres/codegraph.py`
- Create: `tests/postgres/test_code_graph_publication.py`
- Modify: `src/iwiki_mcp/postgres/__init__.py`
- Modify: `tests/postgres/conftest.py`
- Modify: `pyproject.toml:3`
- Modify: `src/iwiki_mcp/__init__.py:2`
- Modify: `tests/test_package.py:8-15`

- [ ] **Step 1: Write failing PostgreSQL lifecycle tests**

Cover begin, same-hash replay, conflicting hash, incomplete finalization, forged header revision, expiry, non-transferable ownership, bounded begin cleanup, abort, terminal replay, timed lock contention, separate-domain parallel finalize, and same-domain optimistic conflict:

```python
def test_same_domain_publishers_use_optimistic_conflict(pg_graph):
    first = pg_graph.begin(header=pg_graph.header)
    second = pg_graph.begin(header=pg_graph.header)
    pg_graph.upload_all(first)
    pg_graph.upload_all(second)
    assert pg_graph.finalize(first)["state"] == "ready"
    assert pg_graph.finalize(second) == {
        "error": "snapshot_conflict",
        "hint": "begin a new publication session and retry",
    }
    assert pg_graph.reader.status()["snapshot_revision"] == pg_graph.finalize(first)["snapshot_revision"]


def test_finalize_recomputes_header_graph_revision(pg_graph):
    session = pg_graph.begin(header=pg_graph.header_with_revision("sha256:" + "0" * 64))
    pg_graph.upload_all(session)
    assert pg_graph.finalize(session)["error"] == "revision_mismatch"


def test_finalize_waits_for_configured_lock_timeout(pg_graph):
    session = pg_graph.complete_session()
    with pg_graph.hold_domain_advisory_lock():
        result = pg_graph.finalize(session)
    assert result["error"] == "busy"
    assert result["retryable"] is True
    assert pg_graph.session(session)["lease_expires_at"] == session.lease_expires_at


def test_replacement_publisher_cannot_take_over_session(pg_graph):
    session = pg_graph.begin(header=pg_graph.header)
    replacement = pg_graph.reopen_with_new_ephemeral_owner()
    assert replacement.publish_batch(session, pg_graph.first_batch) == {
        "error": "unauthorized"
    }
    assert replacement.abort(session) == {"error": "unauthorized"}
```

Define shared `pg_graph` and later `pg_ready_graph` builders in `tests/postgres/conftest.py` from the Task 3 restricted-role fixture, real `PostgresCodeGraphStore`, deterministic header/rows, and injectable clock. Test modules may extend the returned helper with request values but must not redeclare fixtures. `hold_domain_advisory_lock` uses a second database connection and the stored lock ID; it is not a mocked lock.

- [ ] **Step 2: Run publication tests and observe missing store**

Run:

```bash
uv run pytest -q tests/postgres/test_code_graph_publication.py
```

Expected: configured PostgreSQL run FAILS importing `PostgresCodeGraphStore`; unconfigured run SKIPS by fixture policy.

- [ ] **Step 3: Implement `PostgresCodeGraphStore` lifecycle**

Implement `PostgresCodeGraphStore` with the exact constructor and method signatures below; every mutating method performs owner/state/lease checks in the same transaction as its write:

```text
PostgresCodeGraphStore(
    dsn: str,
    iwiki_id: str,
    domain: str,
    owner_id: str,
    *,
    lock_timeout_ms: int,
    session_ttl_seconds: int,
    staging_retention_seconds: int,
    staging_cleanup_limit: int,
    connection_factory: Callable | None,
    require_database_principal: bool,
)
begin(header: SnapshotHeader) -> PublicationSession
publish_batch(session: PublicationSession, batch: SnapshotBatch) -> dict[str, object]
finalize(session: PublicationSession) -> dict[str, object]
abort(session: PublicationSession) -> dict[str, object]
cleanup_staging(now: datetime) -> int
```

At `begin`, call `cleanup_staging(now)` before session creation; delete at most `staging_cleanup_limit` eligible sessions for the resolved domain. Store the adapter's fixed owner, state, and lease and require all three checks on every mutation. Direct adapter construction generates a fresh ephemeral owner identity for each indexing run; the hosted service supplies the authenticated token identity. Do not implement resume, reattach, transfer, supersession, or fencing fields. Accepted batches renew the lease; rejected calls, `busy`, and failed finalize do not.

Before locking, validate batch ordinals/counts/FKs and independently recompute the header-owned `graph_payload_revision` through `codegraph.canonical`; forged values return `revision_mismatch`. Resolve the database-assigned unique `domain_lock_id`, execute `SET LOCAL lock_timeout = <lock_timeout_ms>`, then call blocking `pg_advisory_xact_lock(0x4957494B, domain_lock_id)`. Catch only SQLSTATE `55P03` from this statement as `busy`. Never hold connection/lock between calls. Under the lock compare `base_snapshot_revision` and captured Markdown change token, mark staged snapshot ready, update active state, and commit once. Persist terminal result JSON so finalize replay cannot switch twice; map remaining states to exact R-028 route-specific codes.

- [ ] **Step 4: Run publication and migration suites**

Run:

```bash
uv run pytest -q tests/postgres/test_code_graph_publication.py tests/postgres/test_code_graph_migrations.py tests/postgres/test_migrations.py
```

Expected: all configured tests PASS; two domains finalize concurrently, same-domain competitors yield one ready and one conflict, and readers never see staging rows.

- [ ] **Step 5: Bump version and commit Task 4**

Set all four version surfaces to `0.7.121`, then run:

```bash
uv lock
git add pyproject.toml uv.lock src/iwiki_mcp/__init__.py tests/test_package.py src/iwiki_mcp/postgres/__init__.py src/iwiki_mcp/postgres/codegraph.py tests/postgres/conftest.py tests/postgres/test_code_graph_publication.py
git diff --cached --check
git commit -m "feat(postgres): add atomic graph publication"
```

Expected: publication store commit with focused PostgreSQL lifecycle evidence.

### Task 5: Target Markdown revision and code-to-wiki link derivation

**Depends on:** Tasks 2 and 4.
**Requirements:** R-016–R-019.
**Problem closed:** publisher Markdown may be absent or stale; target must own links and reject concurrent Markdown changes.
**Files:**
- Modify: `src/iwiki_mcp/codegraph/linking.py`
- Modify: `src/iwiki_mcp/postgres/store.py:319-383, 460-570`
- Modify: `src/iwiki_mcp/postgres/codegraph.py`
- Modify: `src/iwiki_mcp/engine/lint.py`
- Modify: `tests/postgres/test_code_graph_publication.py`
- Modify: `tests/postgres/conftest.py`
- Modify: `tests/postgres/test_store.py`
- Modify: `tests/postgres/test_tool_matrix.py`
- Modify: `tests/engine/test_lint.py`
- Modify: `tests/codegraph/test_lint.py`
- Modify: `tests/codegraph/test_linking.py`
- Modify: `pyproject.toml:3`
- Modify: `src/iwiki_mcp/__init__.py:2`
- Modify: `tests/test_package.py:8-15`

- [ ] **Step 1: Add failing target-link and Markdown-conflict tests**

Test canonical Markdown hash ordering, transactional generation increments for create/update/delete/import, publisher rejection of `wiki_code_links`, target selector provenance, body-only mutation conflict, O(1) status generation comparison, and lint's exact stored/current hashes:

```python
def test_markdown_change_between_begin_and_finalize_conflicts(pg_graph, pg_wiki):
    session = pg_graph.begin(header=pg_graph.header)
    pg_graph.upload_all(session)
    page = pg_wiki.read_page("docs", "architecture")
    pg_wiki.update_page(
        "docs", "architecture", page["markdown"] + "\nchanged\n",
        expected_revision=page["revision"],
    )
    assert pg_graph.finalize(session)["error"] == "snapshot_conflict"
    assert pg_graph.reader.status()["state"] == "missing"


def test_lint_reports_exact_stored_and_current_markdown_revisions(pg_ready_graph):
    pg_ready_graph.mutate_markdown()
    report = pg_ready_graph.wiki_lint()
    graph = report["code_graph"]
    assert graph["wiki_links_stale"] is True
    assert graph["stored_markdown_revision"].startswith("sha256:")
    assert graph["current_markdown_revision"].startswith("sha256:")
    assert graph["stored_markdown_revision"] != graph["current_markdown_revision"]
```

Extend the shared `pg_wiki`, `pg_graph`, and `pg_ready_graph` helpers in `tests/postgres/conftest.py` from `clean_postgres`/`store_factory` plus Task 4 store helpers. They must execute actual PostgreSQL page and graph transactions; no fake store may supply revisions or lint output.

- [ ] **Step 2: Run focused tests and observe failure**

Run:

```bash
uv run pytest -q tests/postgres/test_code_graph_publication.py tests/postgres/test_store.py tests/postgres/test_tool_matrix.py tests/codegraph/test_linking.py tests/codegraph/test_lint.py tests/engine/test_lint.py
```

Expected: FAIL because PostgreSQL Markdown snapshots/revisions and target row resolver do not exist.

- [ ] **Step 3: Implement target Markdown provider and portable resolver input**

Add these exact provider methods to `PostgresStore` and a linking helper that accepts immutable page bytes instead of filesystem paths:

```python
from .canonical import canonical_sha256


@dataclass(frozen=True)
class MarkdownPageSnapshot:
    slug: str
    markdown: str

@dataclass(frozen=True)
class MarkdownDomainSnapshot:
    change_token: str | int
    revision: str
    pages: tuple[MarkdownPageSnapshot, ...]

class MarkdownSnapshotProvider(Protocol):
    def markdown_snapshot(self, domain: str) -> MarkdownDomainSnapshot: ...

def markdown_revision(pages: Sequence[MarkdownPageSnapshot]) -> str:
    payload = [
        [page.slug, hashlib.sha256(page.markdown.encode("utf-8")).hexdigest()]
        for page in sorted(pages, key=lambda item: item.slug)
    ]
    return canonical_sha256(payload)
```

Increment `domains.markdown_generation` in the same PostgreSQL transaction as every authoritative page create/update/delete/import that changes Markdown; chunk-only reindex does not increment it. Reuse `validate_code_mapping` and existing selector specificity/provenance to derive `code_graph_wiki_links` from the staged graph and target snapshot. Finalize derives links, exact canonical Markdown hash, and canonical `snapshot_revision` before the advisory lock; under the lock it compares the current generation with the captured generation. Reader status/context compare stored/current generation without scanning pages, omit links, and add `wiki_links_stale` on mismatch. PostgreSQL `lint_domain` and local `engine.lint` explicitly compute the current canonical hash, returning stored/current hashes, stored/current change tokens, and `wiki_links_stale`.

- [ ] **Step 4: Run link, page-mutation, and publication tests**

Run:

```bash
uv run pytest -q tests/codegraph/test_linking.py tests/codegraph/test_lint.py tests/engine/test_lint.py tests/postgres/test_store.py tests/postgres/test_tool_matrix.py tests/postgres/test_code_graph_publication.py
```

Expected: target links match selector provenance, any intervening Markdown content change conflicts, graph publication never mutates pages/chunks/vectors/ordinary links, and later stale links are suppressed.

- [ ] **Step 5: Bump version and commit Task 5**

Set all four version surfaces to `0.7.122`, then run:

```bash
uv lock
git add pyproject.toml uv.lock src/iwiki_mcp/__init__.py tests/test_package.py src/iwiki_mcp/codegraph/linking.py src/iwiki_mcp/postgres/store.py src/iwiki_mcp/postgres/codegraph.py src/iwiki_mcp/engine/lint.py tests/codegraph/test_linking.py tests/codegraph/test_lint.py tests/engine/test_lint.py tests/postgres/conftest.py tests/postgres/test_store.py tests/postgres/test_tool_matrix.py tests/postgres/test_code_graph_publication.py
git diff --cached --check
git commit -m "feat(codegraph): derive wiki links at publication target"
```

Expected: target-link ownership and Markdown revision binding are isolated in one commit.

### Task 6: PostgreSQL ready/fresh reader

**Depends on:** Tasks 1, 4, and 5.
**Requirements:** R-020–R-025.
**Problem closed:** remote server must answer bounded search/context from active PostgreSQL snapshot without local SQLite or checkout/source reads.
**Files:**
- Create: `tests/postgres/test_code_graph_reader.py`
- Modify: `src/iwiki_mcp/postgres/codegraph.py`
- Modify: `src/iwiki_mcp/codegraph/reader.py`
- Modify: `pyproject.toml:3`
- Modify: `src/iwiki_mcp/__init__.py:2`
- Modify: `tests/test_package.py:8-15`

- [ ] **Step 1: Write failing reader contract tests**

Parameterize missing/staging/ready/stale/age-disabled states, all nine existing search ranks, context budgets, stale wiki links, and source suppression. Seed one shared fixture containing a distinct hit for every `MATCH_RANK` value and compare complete normalized results, not only entity order:

```python
def test_postgres_reader_rejects_age_but_zero_disables_rejection(pg_ready_graph):
    stale = pg_ready_graph.reader(max_snapshot_age_seconds=1, now=pg_ready_graph.indexed_at_plus(2))
    assert stale.search(pg_ready_graph.search_request) == {
        "state": "ready", "fresh": False, "error": "stale_snapshot", "results": []
    }
    allowed = pg_ready_graph.reader(max_snapshot_age_seconds=0, now=pg_ready_graph.indexed_at_plus(2))
    assert allowed.search(pg_ready_graph.search_request)["fresh"] is True


def test_postgres_context_never_returns_source(pg_ready_graph):
    result = pg_ready_graph.reader().context(pg_ready_graph.context_request(include_source=True))
    assert result["source_unavailable"] is True
    assert all("source" not in item for item in result["files"])


def test_sqlite_and_postgres_return_identical_ranked_results(ranked_graph_pair):
    sqlite_results = ranked_graph_pair.sqlite.search(ranked_graph_pair.request)
    postgres_results = ranked_graph_pair.postgres.search(ranked_graph_pair.request)
    assert postgres_results == sqlite_results
    assert [item["match"] for item in postgres_results["results"]] == [
        "qualified_exact", "local_exact", "alias_exact",
        "canonical_prefix", "alias_prefix", "canonical_lexical",
        "alias_lexical", "signature", "path",
    ]
```

The `pg_ready_graph` fixture is the real ready snapshot helper introduced in Task 5, extended only with validated search/context request factories and an injectable clock.

- [ ] **Step 2: Run reader tests and observe failure**

Run:

```bash
uv run pytest -q tests/postgres/test_code_graph_reader.py tests/codegraph/test_query.py tests/codegraph/test_context.py
```

Expected: PostgreSQL tests FAIL because ready search/context implementation is absent; existing SQLite query/context tests remain green.

- [ ] **Step 3: Implement PostgreSQL query/context SQL behind `CodeGraphReader`**

Implement `PostgresCodeGraphReader` with constructor inputs `dsn`, `iwiki_id`, `domain`, `max_snapshot_age_seconds`, optional `connection_factory`, and injectable UTC `clock`. Its `status`, `search`, and `context` use validated request dataclasses and active snapshot ID in every query. PostgreSQL SQL imports the authoritative `MATCH_RANK` names/order and `result_key` from `codegraph.query`; it must not define a second rank table. Port the nine predicate semantics to parameterized PostgreSQL SQL, preserve every normalized result field and stable tie-breaker, and do not add a hidden candidate cap. The full nine-rank parity fixture is the compatibility guard for future changes. Traverse context breadth-first with validated depth/node/file/relation bounds. Return normalized response fields matching current SQLite tools, plus stored canonical Markdown revision, stored/current generation, `wiki_links_stale`, age, freshness limit, and safe warnings. Normal status/search/context must not aggregate page Markdown. Never select an absolute root or source field.

- [ ] **Step 4: Run cross-reader contract and regression tests**

Run:

```bash
uv run pytest -q tests/postgres/test_code_graph_reader.py tests/codegraph/test_query.py tests/codegraph/test_context.py
```

Expected: SQLite and PostgreSQL return byte-for-byte equal normalized search results across all nine ranks and equal bounded context; missing/non-ready/stale states return no graph rows.

- [ ] **Step 5: Bump version and commit Task 6**

Set all four version surfaces to `0.7.123`, then run:

```bash
uv lock
git add pyproject.toml uv.lock src/iwiki_mcp/__init__.py tests/test_package.py src/iwiki_mcp/codegraph/reader.py src/iwiki_mcp/postgres/codegraph.py tests/postgres/test_code_graph_reader.py
git diff --cached --check
git commit -m "feat(postgres): query active code graph snapshots"
```

Expected: one ready-reader commit with no MCP/server registration changes.

### Task 7: Authenticated remote MCP publisher and reader adapters

**Depends on:** Tasks 1, 4, and 6.
**Requirements:** R-002, R-005–R-007, R-020–R-021, R-026, R-028–R-029.
**Problem closed:** local indexer/consumer needs the same contract over remote MCP with existing bearer-token ownership and no tenant/domain overrides.
**Files:**
- Create: `src/iwiki_mcp/codegraph/mcp_adapter.py`
- Create: `tests/codegraph/test_mcp_adapter.py`
- Create: `tests/postgres/test_code_graph_http.py`
- Modify: `src/iwiki_mcp/http.py:34-45, 190-235`
- Modify: `src/iwiki_mcp/postgres/auth.py:49-103`
- Modify: `pyproject.toml:3`
- Modify: `src/iwiki_mcp/__init__.py:2`
- Modify: `tests/test_package.py:8-15`

- [ ] **Step 1: Write failing outbound and hosted auth tests**

Use a fake `ClientSession.call_tool` to prove exact method/argument mapping and no scope overrides; hosted tests prove publication requires primary write, reads require primary read, and another writable token cannot reuse a session:

```python
def test_mcp_batch_call_has_no_scope_override(fake_session, mcp_publisher, batch):
    mcp_publisher.publish_batch(mcp_publisher.session, batch)
    name, arguments = fake_session.calls[-1]
    assert name == "wiki_code_publish_batch"
    assert set(arguments) == {
        "session_id", "kind", "ordinal", "rows", "payload_hash"
    }
    assert "iwiki_id" not in arguments and "domain" not in arguments


def test_mcp_finalize_uses_header_as_single_revision_source(fake_session, mcp_publisher):
    mcp_publisher.finalize(mcp_publisher.session)
    name, arguments = fake_session.calls[-1]
    assert name == "wiki_code_publish_finalize"
    assert set(arguments) == {"session_id"}
```

- [ ] **Step 2: Run adapter/HTTP tests and observe failure**

Run:

```bash
uv run pytest -q tests/codegraph/test_mcp_adapter.py tests/postgres/test_code_graph_http.py tests/postgres/test_http.py tests/postgres/test_auth.py
```

Expected: FAIL because adapter and publication tool authorization classes are absent.

- [ ] **Step 3: Implement official MCP Streamable HTTP adapter and auth classification**

Read endpoint/token at adapter construction from `IWIKI_CODE_GRAPH_MCP_URL` and `IWIKI_CODE_GRAPH_MCP_TOKEN`; reject missing values with `invalid_config`, redact them from repr/errors, and create the official client as follows:

```python
async with streamablehttp_client(
    url,
    headers={"Authorization": f"Bearer {token}"},
    timeout=30,
    sse_read_timeout=300,
) as (read_stream, write_stream, _session_id):
    async with ClientSession(read_stream, write_stream) as session:
        await session.initialize()
        result = await session.call_tool(tool_name, arguments=arguments)
```

Map publisher calls to the four publication tools and reader calls to existing status/search/context tools. Decode only a single JSON object result; map transport/protocol/malformed responses to sanitized `remote_mcp_failed`. Add publication tools to `_WRITE_DOMAIN_TOOLS` with implicit primary, and code status/search/context to read classification. Use `AuthContext.token_id` as the fixed remote owner ID; never persist or log bearer text/digest. There is no resume or ownership-transfer tool.

Expose a synchronous `_call(tool_name, arguments)` used by `SnapshotPublisher`/`CodeGraphReader`; it invokes the async block through `anyio.run`. Add `anyio>=4.0` as a direct project dependency because both existing server paths and this adapter import it. Do not retain a client transport session between calls: durable publication ownership/session state lives in PostgreSQL, so correctness does not require sticky MCP sessions or process affinity. Unit tests call `_call` from the same synchronous runtime path used by FastMCP sync tools.

- [ ] **Step 4: Run adapter, HTTP auth, and redaction tests**

Run:

```bash
uv run pytest -q tests/codegraph/test_mcp_adapter.py tests/postgres/test_code_graph_http.py tests/postgres/test_http.py tests/postgres/test_auth.py
```

Expected: exact tool mapping passes, cross-token session takeover returns `unauthorized`, and secret strings are absent from exceptions/log capture/reprs.

- [ ] **Step 5: Bump version and commit Task 7**

Set all four version surfaces to `0.7.124`, then run:

```bash
uv lock
git add pyproject.toml uv.lock src/iwiki_mcp/__init__.py tests/test_package.py src/iwiki_mcp/codegraph/mcp_adapter.py src/iwiki_mcp/http.py src/iwiki_mcp/postgres/auth.py tests/codegraph/test_mcp_adapter.py tests/postgres/test_code_graph_http.py tests/postgres/test_http.py tests/postgres/test_auth.py
git diff --cached --check
git commit -m "feat(codegraph): add authenticated MCP transport"
```

Expected: transport/auth commit; server functions are added next.

### Task 8: Runtime composition and MCP tool surface

**Depends on:** Tasks 2, 4, 6, and 7.
**Requirements:** R-001–R-003, R-020–R-029.
**Problem closed:** selected read/publish modes must drive existing code tools, publication tools must reach PostgreSQL, and a hosted server without checkout must fail indexing safely.
**Files:**
- Modify: `src/iwiki_mcp/codegraph/runtime.py:275-1415`
- Modify: `src/iwiki_mcp/server.py:277-325, 691-790, 2620-2660`
- Modify: `tests/codegraph/test_server_tools.py`
- Modify: `tests/postgres/test_tool_matrix.py`
- Modify: `tests/postgres/test_code_graph_http.py`
- Modify: `tests/test_mcp_smoke.py`
- Modify: `pyproject.toml:3`
- Modify: `src/iwiki_mcp/__init__.py:2`
- Modify: `tests/test_package.py:8-15`

- [ ] **Step 1: Write failing tool-composition tests**

Prove each mode selects exactly one adapter, no failure crosses to another mode, PostgreSQL status/search/context are supported, hosted index returns `source_unavailable`, and publication arguments contain no domain:

```python
def test_postgres_code_index_without_checkout_is_safe(hosted_binding):
    result = server.wiki_code_index(force=True)
    assert result == {
        "error": "source_unavailable",
        "hint": "run wiki_code_index on a local MCP server with the repository checkout",
    }


def test_postgres_tool_matrix_enables_reads_and_publication(tool_matrix):
    assert tool_matrix["wiki_code_status"] == "supported"
    assert tool_matrix["wiki_code_search"] == "supported"
    assert tool_matrix["wiki_code_context"] == "supported"
    assert tool_matrix["wiki_code_index"] == "source_unavailable_without_checkout"
    assert tool_matrix["wiki_code_publish_begin"] == "supported"


def test_publication_tools_reject_git_sqlite_runtime(git_sqlite_runtime):
    assert server.wiki_code_publish_begin({})["error"] == "unsupported_storage"
```

Define `git_sqlite_runtime` by extending existing server-tool builders in `tests/codegraph/conftest.py` with Git storage plus `publish_mode="sqlite"`; use the shared `hosted_runtime` moved to `tests/postgres/conftest.py` for hosted PostgreSQL. `tool_matrix` is the explicit `TOOLS` mapping already owned by `tests/postgres/test_tool_matrix.py`; update its full registered-tool count and every affected entry.

- [ ] **Step 2: Run server/tool-matrix tests and observe current unsupported responses**

Run:

```bash
uv run pytest -q tests/codegraph/test_server_tools.py tests/postgres/test_tool_matrix.py tests/postgres/test_code_graph_http.py tests/test_mcp_smoke.py
```

Expected: FAIL because PostgreSQL code tools still return `unsupported_storage` and publication tools are not registered.

- [ ] **Step 3: Compose adapters and register exact tools**

Refactor `CodeGraphRuntime` to receive `SnapshotPublisher` and `CodeGraphReader`; construct adapters from `CodeGraphConfig.publish_mode/read_mode` plus binding type. Keep local extraction available whenever `binding.project_dir` is a validated checkout, including local PostgreSQL mode. Add these safe wrappers and register them with FastMCP. Each body resolves the current binding and delegates to `_code_publication_service(bind)`; the batch wrapper validates and canonicalizes rows before adapter dispatch:

```python
def wiki_code_publish_begin(header: dict) -> dict:
    return _code_publication_service(_resolved_binding()).begin_from_mapping(header)

def wiki_code_publish_batch(
    session_id: str,
    kind: str,
    ordinal: int,
    rows: list[dict],
    payload_hash: str,
) -> dict:
    return _code_publication_service(_resolved_binding()).publish_from_mapping(
        session_id, kind, ordinal, rows, payload_hash
    )

def wiki_code_publish_finalize(session_id: str) -> dict:
    return _code_publication_service(_resolved_binding()).finalize_from_mapping(
        session_id
    )

def wiki_code_publish_abort(session_id: str) -> dict:
    return _code_publication_service(_resolved_binding()).abort_from_mapping(
        session_id
    )
```

FastMCP registers all four publication wrappers statically, but `_code_publication_service` returns `unsupported_storage` unless the current call is authenticated hosted PostgreSQL with a writable primary. Local SQLite and direct PostgreSQL indexers invoke adapters directly; stdio/Git/SQLite callers cannot use remote publication tools. Each hosted wrapper resolves authenticated `iwiki_id`, bound primary, and token owner internally and rejects missing write scope before parsing rows. Replace PostgreSQL `unsupported_storage` for status/search/context. `wiki_code_index` remains supported for Git/SQLite, publishes through selected adapter when any local checkout exists (including direct PostgreSQL mode), and returns exact `source_unavailable` without creating a session/snapshot on a checkout-less hosted server.

- [ ] **Step 4: Run server, matrix, HTTP, and smoke tests**

Run:

```bash
uv run pytest -q tests/codegraph/test_server_tools.py tests/postgres/test_tool_matrix.py tests/postgres/test_code_graph_http.py tests/test_mcp_smoke.py
```

Expected: complete matrix includes all four publication tools plus `wiki_code_index`; Git/SQLite publication calls return `unsupported_storage`, direct/local indexing publishes, hosted checkout-less indexing returns `source_unavailable`, remote source requests suppress source, and no fallback/session creation occurs.

- [ ] **Step 5: Bump version and commit Task 8**

Set all four version surfaces to `0.7.125`, then run:

```bash
uv lock
git add pyproject.toml uv.lock src/iwiki_mcp/__init__.py tests/test_package.py src/iwiki_mcp/codegraph/runtime.py src/iwiki_mcp/server.py tests/codegraph/test_server_tools.py tests/postgres/test_tool_matrix.py tests/postgres/test_code_graph_http.py tests/test_mcp_smoke.py
git diff --cached --check
git commit -m "feat(server): enable distributed code graph tools"
```

Expected: public tool/composition commit with focused MCP smoke evidence.

### Task 9: Cross-adapter concurrency, integrity, and safe-error suite

**Depends on:** Tasks 1–8.
**Requirements:** R-004–R-015, R-020–R-030.
**Problem closed:** coupled concurrency/security invariants require integration evidence beyond adapter unit tests.
**Files:**
- Create: `tests/codegraph/publication_contract_support.py`
- Create: `tests/postgres/test_code_graph_contract.py`
- Modify: `tests/postgres/test_code_graph_publication.py`
- Modify: `tests/postgres/test_code_graph_reader.py`
- Modify: `tests/postgres/test_code_graph_http.py`
- Modify: `tests/codegraph/test_recovery_concurrency.py`
- Modify implementation files only when a new test demonstrates a defect
- Modify: `pyproject.toml:3`
- Modify: `src/iwiki_mcp/__init__.py:2`
- Modify: `tests/test_package.py:8-15`

- [ ] **Step 1: Add the parameterized contract and adversarial cases**

Define `PublicationContractHarness` in `tests/codegraph/publication_contract_support.py` with `begin`, `publish_batch`, `finalize`, `abort`, `reader`, `hold_finalize_lock`, and raw-persisted-row inspection. Add pure `generate_python_project(root: Path, count: int) -> Path` there so scale modules reuse one deterministic generated corpus without cross-directory pytest fixtures. In `tests/postgres/test_code_graph_contract.py`, the `publication_adapter` fixture has three explicit branches: SQLite uses a temporary local base; PostgreSQL uses the restricted-role disposable database; MCP uses `http.prepare_runtime` plus Starlette `TestClient` and sends real initialize/tool JSON-RPC requests through `/mcp` with a real test token. The MCP branch must not use `fake_session` or call `PostgresCodeGraphStore` directly.

Run identical lifecycle assertions for all three branches. Add fixed/non-transferable ownership, malformed/oversized/out-of-order batches, forged revision, cross-wiki/domain/snapshot keys, timed lock contention, cleanup-on-begin, active-pointer failure, SQL-looking IDs, secret/path/source redaction, every route-specific R-028 publication/adapter/readiness error code, and concurrent reader old-or-new observations. Assert the three closed sets independently so a code cannot migrate to the wrong path:

```python
@pytest.mark.parametrize("publication_adapter", ["sqlite", "postgres", "mcp"], indirect=True)
def test_reader_never_observes_partial_snapshot(publication_adapter):
    old_revision = publication_adapter.publish_complete("old")
    session = publication_adapter.begin("new")
    publication_adapter.publish_half(session)
    assert publication_adapter.reader.status()["snapshot_revision"] == old_revision
    new_revision = publication_adapter.finish(session)
    assert publication_adapter.observed_revisions() <= {old_revision, new_revision}


@pytest.mark.parametrize("publication_adapter", ["sqlite", "postgres", "mcp"], indirect=True)
def test_wrong_or_replacement_owner_never_mutates(publication_adapter):
    session = publication_adapter.begin("owner-a")
    assert publication_adapter.as_owner("owner-b").abort(session)["error"] == "unauthorized"
    assert publication_adapter.replacement_process().publish_one(session)["error"] == "unauthorized"


def test_error_codes_stay_in_their_routes(publication_adapter):
    assert publication_adapter.publication_error_codes == PUBLICATION_ERROR_CODES
    assert publication_adapter.adapter_error_codes == ADAPTER_ERROR_CODES
    assert publication_adapter.readiness_error_codes == READINESS_ERROR_CODES


def test_commit_uncertain_allows_only_sqlite_finalize_reconciliation(
    sqlite_publication_adapter,
):
    session = sqlite_publication_adapter.begin("uncertain")
    sqlite_publication_adapter.publish_complete_batches(session)
    sqlite_publication_adapter.fail_next_directory_sync()
    first = sqlite_publication_adapter.finalize(session)
    assert first["error"] == "commit_uncertain"
    assert sqlite_publication_adapter.reader.status()["snapshot_revision"] == (
        first["snapshot_revision"]
    )

    for rejected in (
        sqlite_publication_adapter.publish_one(session),
        sqlite_publication_adapter.abort(session),
    ):
        assert rejected.get("accepted") is not True
        assert rejected["error"] in PUBLICATION_ERROR_CODES
        assert rejected["error"] != "commit_uncertain"

    sqlite_publication_adapter.restore_directory_sync()
    assert sqlite_publication_adapter.finalize(session)["state"] == "ready"


@pytest.mark.parametrize("publication_adapter", ["postgres", "mcp"], indirect=True)
def test_commit_uncertain_is_not_emitted_by_distributed_routes(
    publication_adapter,
):
    assert publication_adapter.supports_commit_uncertain is False
    assert "commit_uncertain" not in publication_adapter.observable_failure_codes()
```

The harness exposes `supports_commit_uncertain` and
`observable_failure_codes()` from real injected route failures; neither may be a
hard-coded copy of the shared constant tuple.

- [ ] **Step 2: Run the adversarial suite and retain every observed failure**

Run:

```bash
uv run pytest -q tests/postgres/test_code_graph_contract.py tests/codegraph/test_recovery_concurrency.py tests/postgres/test_code_graph_publication.py tests/postgres/test_code_graph_reader.py tests/postgres/test_code_graph_http.py
```

Expected: any failure identifies a concrete implementation defect; do not weaken the test or approved invariant.

- [ ] **Step 3: Fix only demonstrated defects and rerun focused cases**

For each failure, stop Task 9 and return to the owning implementation task. Make and commit the smallest focused fix with the next unused patch version, rerun the exact failing node ID until PASS, then restart Task 9 from Step 1. No new fallback, broker, background daemon, or protocol field is allowed.

- [ ] **Step 4: Run ordinary wiki and full code-graph regressions**

Run:

```bash
uv run pytest -q tests/codegraph tests/postgres/test_store.py tests/postgres/test_auth.py tests/postgres/test_http.py tests/postgres/test_tool_matrix.py tests/engine
```

Expected: all selected tests PASS; PostgreSQL integration tests SKIP only when the disposable DSN is absent, never because of an implementation error.

- [ ] **Step 5: Bump version and commit Task 9**

Set all four version surfaces to `0.7.126`; stage only the contract tests and version files, then run:

```bash
uv lock
git add pyproject.toml uv.lock src/iwiki_mcp/__init__.py tests/test_package.py tests/codegraph/publication_contract_support.py tests/postgres/test_code_graph_contract.py tests/codegraph/test_recovery_concurrency.py tests/postgres/test_code_graph_publication.py tests/postgres/test_code_graph_reader.py tests/postgres/test_code_graph_http.py
git diff --cached --check
git commit -m "test(codegraph): verify distributed snapshot invariants"
```

Expected: integration-hardening commit with no speculative code.

### Task 10: Operator documentation and 20,000-file evidence

**Depends on:** Tasks 1–9.
**Requirements:** R-021, R-023–R-025, R-027–R-030 and acceptance closure for R-001–R-030.
**Problem closed:** operators need exact mode/secret/recovery guidance and the first-release file bound needs reproducible evidence.
**Files:**
- Create: `tests/eval/test_code_graph_publication_scale.py`
- Create: `tests/postgres/test_code_graph_scale.py`
- Modify: `eval/code_graph/runner.py`
- Modify: `README.md:190-300, 420-440`
- Modify: `docs/README.ru.md:190-300, 420-440`
- Modify: `src/iwiki_mcp/base.py:15-62`
- Modify: `pyproject.toml:3`
- Modify: `src/iwiki_mcp/__init__.py:2`
- Modify: `tests/test_package.py:8-15`

- [ ] **Step 1: Add failing generated-scale and documentation contract tests**

Generate files under `tmp_path` rather than committing a corpus. Publish 20,000 minimal Python files through SQLite and a smaller 2,000-file corpus through the real PostgreSQL publisher with deliberately small batch ceilings. Assert count/revision/ready state, every emitted batch stays within row/byte limits, query limits remain bounded, and elapsed/peak-heap evidence is recorded. Verify docs contain all three modes, freshness `0`, secret environment names, local-checkout requirement, restricted-role separation, rollback/cleanup procedure, and no-fallback statement:

```python
@pytest.mark.slow
def test_publication_supports_twenty_thousand_files(tmp_path, publication_target):
    project = generate_python_project(tmp_path / "project", 20_000)
    result = publication_target.index(project, max_total_files=20_000)
    assert result["state"] == "ready"
    assert result["counts"]["files"] == 20_000
    assert result["publication_seconds"] > 0
    assert result["peak_python_heap_bytes"] > 0
    assert result["max_batch_rows_observed"] <= 1000
    assert result["max_batch_bytes_observed"] <= 1_000_000


@pytest.mark.postgres_integration
def test_postgres_publication_respects_server_ceilings(
    tmp_path, postgres_scale_target,
):
    project = generate_python_project(tmp_path / "project", 2_000)
    result = postgres_scale_target.index(
        project, max_batch_rows=100, max_batch_bytes=100_000
    )
    assert result["state"] == "ready"
    assert result["counts"]["files"] == 2_000
    assert result["max_batch_rows_observed"] <= 100
    assert result["max_batch_bytes_observed"] <= 100_000
    assert len(postgres_scale_target.search("value", limit=20)["results"]) <= 20


def test_small_publication_report_is_mode_aware(
    tmp_path, publication_target,
):
    result = publication_target.index(
        generate_python_project(tmp_path / "project", 2)
    )
    assert result["target_mode"] == "sqlite"
    assert result["batch_count"] > 0
    assert result["batch_bytes"] > 0
```

Place the SQLite cases in `tests/eval/test_code_graph_publication_scale.py`. Place the PostgreSQL case and its `postgres_scale_target(clean_postgres)` fixture in `tests/postgres/test_code_graph_scale.py`, where `tests/postgres/conftest.py` is in scope. Both import `generate_python_project` from Task 9 support. MCP is not multiplied at scale because Task 9 already proves the real hosted transport uses the same PostgreSQL publication service.

- [ ] **Step 2: Run documentation/scale tests and capture baseline**

Run:

```bash
uv run pytest -q tests/eval/test_code_graph_publication_scale.py -m "not slow"
```

Expected: focused non-scale contract tests FAIL until runner/report fields and documentation contract exist. The registered `slow` and `postgres_integration` cases are intentionally excluded from this baseline.

- [ ] **Step 3: Update runner, English/Russian docs, and config template**

Document these exact deployment paths:

```toml
[code_graph]
publish_mode = "sqlite" # sqlite | postgres | mcp
read_mode = "sqlite"    # sqlite | postgres | mcp
max_snapshot_age_seconds = 86400 # 0 disables age rejection
```

Document `IWIKI_CODE_GRAPH_MCP_URL` and `IWIKI_CODE_GRAPH_MCP_TOKEN` as runtime-only MCP values; direct PostgreSQL reuses `[storage]` and `IWIKI_DB_PASSWORD`. Replace the old single-role example: schema owner/migrator applies migrations through admin commands, hosted service and direct runtime roles are non-owner/non-`BYPASSRLS`, ordinary RLS is enabled without `FORCE`, and both roles receive explicit shared domain grants through `iwiki-mcp principal grant`. Include hosted-domain provisioning before token enablement, privilege inspection, identical HTTP/stdio startup schema checks, `schema rollback-v4-compat` dry-run/confirm sequence, post-rollback pre-v4 smoke, later migration reapply, cleanup-on-next-begin behavior, and production stop conditions. Explain local checkout, one domain/repository, non-transferable sessions, target-derived links, atomic visibility, conflicts/retry, remote source unavailability, no fallback, and first publication.

Document both exact SQLite schema-v2 profiles. Legacy five-table readiness requires the strict sidecar. The publication profile carries authoritative ready evidence in `code_graph_publication` and treats `.metadata.json` as cache-only. SQLite `commit_uncertain` permits only same-process repeated `finalize`, never batch, abort, automatic rollback, or adapter fallback; after process loss, operators inspect status and start a new session. Before rolling back to a pre-publication binary, retain or restore a legacy snapshot or reindex with that binary because it may reject the internal table. Update benchmark report with target mode, batch count/bytes, publication time, active revision, peak Python heap, and generated file count.

Keep `anyio` in direct dependencies from Task 7; the `slow` marker was already registered by Task 1.

- [ ] **Step 4: Parent updates bound iwiki architecture pages**

Through iwiki MCP, parent updates existing pages `concept/code-graph-configuration`, `concept/code-graph-storage`, `concept/code-graph-runtime`, `concept/code-graph-wiki-linking`, and `mcp-server` with implemented behavior and source anchors, then runs `wiki_lint`. Expected: no broken, stale, missing-source, or graph-integrity finding caused by this task; pre-existing unrelated advisories are recorded, not repaired here.

- [ ] **Step 5: Run docs, benchmark, and broad regression checks**

Run:

```bash
uv run pytest -q tests/eval/test_code_graph_publication_scale.py tests/eval/test_code_graph_runner.py -m "not slow and not postgres_integration"
uv run pytest -q tests/eval/test_code_graph_publication_scale.py -m slow
uv run pytest -q tests/postgres/test_code_graph_scale.py
uv run pytest -q
uv run iwiki-mcp --help
```

Expected: SQLite scale reports exactly 20,000 files, PostgreSQL scale reports exactly 2,000 files within server ceilings, both produce ready snapshots, full pytest has zero failures, and CLI help exits 0. The PostgreSQL command must run against the disposable `*_test` DSN before result reconciliation; a skip is not acceptance evidence.

- [ ] **Step 6: Bump version and commit Task 10**

Set all four version surfaces to `0.7.127`, then run:

```bash
uv lock
git add pyproject.toml uv.lock src/iwiki_mcp/__init__.py tests/test_package.py README.md docs/README.ru.md src/iwiki_mcp/base.py eval/code_graph/runner.py tests/eval/test_code_graph_publication_scale.py tests/postgres/test_code_graph_scale.py
git diff --cached --check
git commit -m "docs(codegraph): document distributed publication modes"
```

Expected: final implementation commit contains docs, scale evidence, config template, and patch bump only.

## 4. Final verification and result reconciliation

### Task 11: Verify the approved chain without changing implementation

**Depends on:** Tasks 1–10 and parent wiki updates.
**Requirements:** R-001–R-030; AC-01–AC-30.
**Problem closed:** produce fresh evidence that implementation matches the selected spec and contains no excess behavior.
**Files:**
- Inspect only. If a check demonstrates a defect, stop Task 11, return to the owning implementation task, make a focused fix at version `0.7.128` or the next unused patch, synchronize all four version surfaces, commit it, then restart Task 11 from Step 1.

- [ ] **Step 1: Verify repository scope and versions**

Run:

```bash
git status --short
git log --oneline --decorate origin/master..HEAD
git diff --check origin/master...HEAD
```

Expected: clean worktree, ordered task commits, and no whitespace errors.

- [ ] **Step 2: Run focused contracts with disposable PostgreSQL configured**

Run:

```bash
uv run pytest -q tests/codegraph/test_publication.py tests/codegraph/test_schema.py tests/codegraph/test_sqlite_adapter.py tests/codegraph/test_mcp_adapter.py tests/test_server_import_closure.py tests/postgres/test_code_graph_contract.py tests/postgres/test_code_graph_migrations.py tests/postgres/test_code_graph_rollback.py tests/postgres/test_code_graph_publication.py tests/postgres/test_code_graph_reader.py tests/postgres/test_code_graph_http.py tests/postgres/test_tool_matrix.py tests/codegraph/test_lint.py tests/engine/test_lint.py
```

Expected: zero failures and no PostgreSQL skips when `IWIKI_TEST_POSTGRES_DSN` points to the approved disposable `*_test` database.

- [ ] **Step 3: Run full regressions and scale evidence**

Run:

```bash
uv run pytest -q
uv run pytest -q tests/eval/test_code_graph_publication_scale.py -m slow
uv run pytest -q tests/postgres/test_code_graph_scale.py
uv run iwiki-mcp --help
```

Expected: full suite zero failures, 20,000-file SQLite and 2,000-file PostgreSQL scale cases PASS without PostgreSQL skips, help exits 0.

- [ ] **Step 4: Verify secret/source/path absence**

Run targeted redaction tests and inspect tracked config/docs:

```bash
uv run pytest -q tests/codegraph/test_mcp_adapter.py tests/postgres/test_code_graph_http.py -k "secret or source or path or redact"
git grep -nE "IWIKI_CODE_GRAPH_MCP_TOKEN[[:space:]]*=|IWIKI_DB_PASSWORD[[:space:]]*=" -- ':!docs/superpowers/plans/*'
```

Expected: redaction tests PASS; grep returns no committed secret assignment.

- [ ] **Step 5: Parent runs wiki consistency and `$check-chain result`**

Parent reads the task page, verifies empty task spool, runs `wiki_lint`, then invokes `$check-chain result docs/superpowers/plans/2026-08-14-postgres-code-graph-distributed-indexing.md` through the skill interface.

Expected: result `OK` only when every spec requirement maps to diff/test evidence, documentation is current, no open critical finding exists, and the task page can move to `done`.

## 5. Requirement coverage matrix

| Requirements | Owning tasks | Verification evidence |
|---|---|---|
| R-001, R-002, R-003, R-004 | Tasks 3, 8, 9 | restricted-role/RLS/FK tests, complete tool matrix, adversarial cross-scope tests |
| R-005, R-006, R-007, R-008, R-009 | Tasks 1, 2, 4, 7 | shared serializer contract, SQLite/PostgreSQL/MCP lifecycle suites |
| R-010, R-011, R-012, R-013, R-014, R-015 | Tasks 2, 3, 4, 9 | all-adapter fixed owner/lease/non-transfer tests, timed lock, cleanup-on-begin, conflict, visibility tests |
| R-016, R-017, R-018, R-019 | Tasks 2, 3, 5, 6 | generation mutation, target hash/link derivation, O(1) status/context, exact lint revisions |
| R-020, R-021, R-022, R-023, R-024, R-025 | Tasks 1, 2, 6, 7, 8 | reader contract, freshness matrix, bounded queries, source suppression |
| R-026, R-027, R-028, R-029 | Tasks 2, 4, 7, 8, 9 | hosted scope, all-adapter ownership, complete errors, source/no-checkout tests |
| R-030 | Tasks 1, 3, 4, 9, 10 | validated ceilings, cleanup, generated 20,000-file SQLite and 2,000-file PostgreSQL evidence |
| AC-01–AC-30 | Task 11 | focused contracts, full pytest, both scale cases, lint, result reconciliation |

## 6. Human checkpoints

1. Task 3: approve concrete migration and database privilege diff before commit.
2. Task 10: parent updates iwiki pages and confirms lint; workers do not write wiki.
3. Task 11: production credentials/publication remain outside agent authority.
4. After `$check-chain result OK`: choose PR publication through the repository's branch-finishing workflow; never merge or push directly to `master`.
