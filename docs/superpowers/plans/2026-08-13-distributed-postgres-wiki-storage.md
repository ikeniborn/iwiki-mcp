---
review:
  plan_hash: 81a9b87672ad37e7
  last_run: 2026-08-13
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-08-12-distributed-postgres-wiki-storage-intent.md
  spec: docs/superpowers/specs/2026-08-13-distributed-postgres-wiki-storage-design.md
---
# Distributed PostgreSQL Wiki Storage Implementation Plan

> **For implementer:** Execute tasks in order. Each task is a small TDD change;
> run its listed focused tests before proceeding.

**Goal:** Preserve autonomous local Git-backed stdio operation while adding a
tenant-isolated PostgreSQL backend for stdio and authenticated Streamable HTTP.

**Status:** approved

**Architecture:** Keep the current public MCP tools as the compatibility layer.
Resolve each process to either the existing Git implementation or a new
PostgreSQL backend. PostgreSQL stores pages, derived chunks, link graph, and
authorization in one `iwiki` schema; every tenant query is constrained by an
immutable `iwiki_id` selected by local configuration or a verified HTTP token.

**Tech stack:** Python 3.10+, FastMCP Streamable HTTP, psycopg, PostgreSQL with
pgvector, existing chunking/embedding/frontmatter modules, pytest.

## File Map

| Path | Responsibility |
|---|---|
| `pyproject.toml`, `uv.lock`, `src/iwiki_mcp/__init__.py` | PostgreSQL runtime dependency and synchronized per-change patch version bumps. |
| `src/iwiki_mcp/storage.py` | Backend-neutral binding, page/search/graph operation protocol, and error/result types used by MCP handlers. |
| `src/iwiki_mcp/postgres/config.py` | Strict local/server TOML parsing, password lookup, safe diagnostics, transport/storage compatibility checks. |
| `src/iwiki_mcp/postgres/migrations.py` | Locked, forward-only creation/upgrade of only the `iwiki` schema, immutable embedding metadata, and pgvector validation. |
| `src/iwiki_mcp/postgres/store.py` | Tenant-scoped transactions for wiki, domains, pages, chunks, links, vector search, graph traversal, and Git import/export bookkeeping. |
| `src/iwiki_mcp/postgres/auth.py` | Bearer digest verification, token lifecycle, domain-grant enforcement, and request context. |
| `src/iwiki_mcp/admin.py` | Explicit `base`, `domain`, and `token` CLI handlers, including Git import/export. |
| `src/iwiki_mcp/http.py` | Streamable HTTP application construction, Origin/Bearer middleware, and request-scoped MCP context. |
| `src/iwiki_mcp/base.py` | Recognize `storage.type`, retain default Git binding, and construct immutable local PostgreSQL binding. |
| `src/iwiki_mcp/indexer.py`, `src/iwiki_mcp/retrieval.py`, `src/iwiki_mcp/graph.py`, `src/iwiki_mcp/resources.py` | Route existing indexing, retrieval, graph, and resource operations through the selected backend while retaining Git paths. |
| `src/iwiki_mcp/server.py` | Add backend-aware tool dispatch, additive revision parameters, `serve`, `base`, `domain`, and `token` routing; keep bare command stdio. |
| `tests/postgres/conftest.py` | Explicit-DSN PostgreSQL fixture, offline skip policy, isolated schema cleanup, deterministic embeddings, and test client helpers. |
| `tests/postgres/test_config.py` | Storage/transport configuration and credential-redaction cases. |
| `tests/postgres/test_migrations.py` | Empty-database initialization, migration lock, and unrelated-schema preservation. |
| `tests/postgres/test_store.py` | Tenant FKs, embedding invariants, page mutations, derived data transactionality, cross-backend ranking, graph, and revision conflicts. |
| `tests/postgres/test_auth.py` | Token hashing, one-wiki binding, ACL narrowing, revocation, and disabled-base behavior. |
| `tests/postgres/test_admin.py` | Base/domain/token lifecycle, JSON/dry-run UX, explicit Git import, and rollback export. |
| `tests/postgres/test_http.py` | Authorized Streamable HTTP MCP, Origin denial, no-token denial, and HTTP-plus-Git startup denial. |
| `tests/postgres/test_tool_matrix.py` | Table-driven behavior for every registered tool in Git and PostgreSQL modes, including forbidden Git/SQLite paths. |
| `tests/test_package.py` | Release-version synchronization assertion. |
| `tests/test_base.py`, `tests/test_mcp_smoke.py`, existing focused server tests | Regression coverage proving absent/explicit `git` behavior remains unchanged. |
| `README.md`, `docs/README.ru.md`, `docs/architecture.md` | Local Git/PostgreSQL setup, hosted boundary, model metadata, rollback/DR, CLI, and client examples. |

## Task 1: Define backend selection and safe configuration

**Closes:** R1, R3, R4, and the configuration part of R5.

**Files:**
- Create: `src/iwiki_mcp/storage.py`, `src/iwiki_mcp/postgres/__init__.py`, `src/iwiki_mcp/postgres/config.py`
- Modify: `src/iwiki_mcp/base.py`, `tests/test_base.py`, `tests/postgres/test_config.py`

1. Write failing tests for an absent `[storage]` table and explicit `type = "git"` yielding the present Git binding; test a valid `postgres` local block with required host, port, database, user, sslmode, immutable `iwiki_id`, and maximum `read`/`write`/`primary` scope.
2. Add failing cases for unsupported type, missing PostgreSQL field, missing local `iwiki_id` or scope, inconsistent scopes, and errors containing neither `IWIKI_DB_PASSWORD` nor its value.
3. Introduce small immutable binding/config dataclasses: Git carries the present base path and PostgreSQL carries connection settings plus fixed `iwiki_id`. Do not read PostgreSQL configuration for Git mode.
4. Parse project TOML and dedicated server TOML separately. Reject HTTP plus absent/`git` storage before a listener is created; reject `iwiki_id` in hosted configuration; parse allowed Origins, bounded pool sizes, and database timeouts.
5. Parse model settings from existing runtime variables instead of PostgreSQL backend constants. Test the current deployment values `lemonade-embeddings-bge-m3-q8`, dimension `1024`, and `lemonade-reranker-bge-reranker-v2-m3`, plus a second valid model/dimension combination. Keep credentials server-side and preserve existing Git behavior.
6. Run:

```bash
uv run pytest -q tests/test_base.py tests/postgres/test_config.py
```

Expected: Git default compatibility, local PostgreSQL scope, flexible runtime model configuration, and all safe configuration rejection cases pass.

## Task 2: Establish PostgreSQL schema and migration contract

**Closes:** the startup/migration part of R5 and tenant/vector invariants in R6.

**Files:**
- Create: `src/iwiki_mcp/postgres/migrations.py`, `tests/postgres/conftest.py`, `tests/postgres/test_migrations.py`
- Modify: `pyproject.toml`, `uv.lock`

1. Add the `postgres_integration` marker and fixture contract: without `IWIKI_TEST_POSTGRES_DSN`, integration tests skip before any network call; with an explicit pgvector DSN whose database name ends in `_test`, they run; every other database name is rejected before mutation.
2. Add failing integration tests against that disposable database: a blank target produces only `iwiki`-owned objects; repeated and concurrent startup leaves one ordered migration history; an unrelated schema/table remains usable; failed migration rolls back; newer schema version refuses startup.
3. Add the PostgreSQL driver, pool, and pgvector support dependencies, regenerate `uv.lock`, and ensure the package build includes the migration implementation.
4. Implement an advisory migration lock and ordered, transactional, forward-only migrations. Create schema-qualified tables: `schema_migrations`, `storage_metadata`, `iwikis`, `domains`, `pages`, `chunks`, `links`, `tokens`, `token_domain_grants`, and `git_imports`.
5. On an empty database, record the runtime embedding model and dimension in `storage_metadata` and create dimension-specific vector DDL from the validated value, without a hard-coded model or dimension. Test both the current 1024-dimensional Lemonade configuration and another supported dimension. Store the current quantize/dequantize output in pgvector and reject later runtime metadata mismatch before traffic; do not persist or constrain the reranker in database metadata.
6. Use composite tenant keys/FKs and unique constraints that include `iwiki_id`; declare page revision, timestamps, active state, and safe timeouts. Set no untrusted `search_path`; qualify every DDL reference as `iwiki.*`.
7. Fail startup safely when connection, pgvector availability, metadata validation, or migration fails; never create the target database, downgrade a newer schema, or change objects outside `iwiki`.
8. Run:

```bash
env -u IWIKI_TEST_POSTGRES_DSN uv run pytest -q tests/postgres/test_migrations.py
test -n "${IWIKI_TEST_POSTGRES_DSN:?set IWIKI_TEST_POSTGRES_DSN to a disposable pgvector _test database}"
uv run pytest -q tests/postgres/test_migrations.py
```

Expected: offline skip behavior is explicit; the enabled disposable-service run proves migration, embedding metadata, rollback, and schema boundaries.

## Task 3: Build tenant-scoped PostgreSQL page and derived-data storage

**Closes:** R6, R7, and R8 storage, mutation, ranking, and graph behavior.

**Files:**
- Create: `src/iwiki_mcp/postgres/store.py`, `tests/postgres/test_store.py`
- Modify: `src/iwiki_mcp/storage.py`, `src/iwiki_mcp/indexer.py`, `src/iwiki_mcp/retrieval.py`, `src/iwiki_mcp/graph.py`

1. Write failing store tests for create/list/read page, domain-scoped search, related links, cross-wiki foreign-key rejection, wrong-dimension rejection, and query isolation even with overlapping domain/page slugs.
2. Implement one transaction boundary for page creation/update/delete: validate Markdown/frontmatter with current helpers, chunk and embed before committing, replace chunks and outgoing links atomically, and preserve the old page on any failure.
3. Make PostgreSQL reads return numeric `revision`. Reject a missing PostgreSQL update/delete revision with `{ "error": "expected_revision_required", "hint": "read the page and retry with its revision" }`; after a conditional mutation affects no page return `{ "error": "conflict", "current_revision": N, "hint": "read the page and retry against the current revision" }`.
4. Store dequantized int8 embeddings, retrieve tenant/domain-scoped cosine candidates with pgvector, and reuse current Python lexical scoring, RRF fusion, deduplication, reranking, rounded scores, and deterministic tie breaks.
5. Add one shared deterministic Git/PostgreSQL fixture with non-tied scores. Assert identical normalized result order and graph neighbours, plus compatible response fields; do not assert arbitrary floating-point identity outside the rounded public score.
6. Keep existing JSONL/SQLite/Git functions unchanged for Git bindings; dispatch only at the storage seam so PostgreSQL never reads local Markdown, index JSONL, SQLite, or Git state.
7. Run:

```bash
test -n "${IWIKI_TEST_POSTGRES_DSN:?set IWIKI_TEST_POSTGRES_DSN to a disposable pgvector _test database}"
uv run pytest -q tests/postgres/test_store.py tests/engine/test_graph_store.py tests/test_retrieval.py
```

Expected: PostgreSQL atomicity/isolation/ranking tests and local graph/retrieval regressions pass.

## Task 4: Adapt MCP tools without breaking Git contracts

**Closes:** R1, R3, R7, and the complete tool contract in R8a.

**Files:**
- Create: `tests/postgres/test_tool_matrix.py`
- Modify: `src/iwiki_mcp/server.py`, `src/iwiki_mcp/resources.py`, `tests/test_mcp_smoke.py`, `tests/test_server_read.py`, `tests/test_server_write.py`, `tests/test_server_update.py`, `tests/test_server_delete.py`, `tests/postgres/test_store.py`

1. Enumerate all 22 currently registered tools in a failing table-driven test. For each storage mode assert supported behavior or exact `{ "error": "unsupported_storage", "storage": "postgres", "hint": ... }`; make registration drift fail the test.
2. Add handler tests proving `wiki_read_page` includes revision only in PostgreSQL, `wiki_status` safely reports storage/transport and authorized scope without DSN data, and existing Git responses remain unchanged.
3. Route status, domain/page listing, read, search, related, write, update, delete, index, bind, lint, and resource reads through the resolved backend. Select backend before every `sync.ensure_fresh`, Git, filesystem index, or SQLite call.
4. Add `expected_revision: int | None = None` to update/delete tool schemas. Return the required-revision error when it is omitted only in PostgreSQL; leave Git mutation and Git auto-commit/sync semantics intact.
5. Make `wiki_bind` narrow the immutable local-config or HTTP-token maximum scope only. Keep current persisted project binding behavior for Git mode.
6. Return the stable unsupported response in PostgreSQL for all `wiki_code_*`, `wiki_remediation_plan`, `wiki_migrate_okf`, `wiki_apply_okf`, `wiki_export_okf`, `wiki_sync`, and `wiki_create_domain`. Assert spies observe no SQLite, Git, `sync.ensure_fresh`, local Markdown, or JSONL calls.
7. Run:

```bash
test -n "${IWIKI_TEST_POSTGRES_DSN:?set IWIKI_TEST_POSTGRES_DSN to a disposable pgvector _test database}"
uv run pytest -q tests/test_mcp_smoke.py tests/test_server_read.py tests/test_server_write.py tests/test_server_update.py tests/test_server_delete.py tests/postgres/test_store.py tests/postgres/test_tool_matrix.py
```

Expected: every registered tool has explicit mode behavior; revision, status, Git-path isolation, and unsupported-mode rules are deterministic.

## Task 5: Implement token authentication and domain ACL

**Closes:** R9.

**Files:**
- Create: `src/iwiki_mcp/postgres/auth.py`, `tests/postgres/test_auth.py`
- Modify: `src/iwiki_mcp/postgres/store.py`, `src/iwiki_mcp/storage.py`, `src/iwiki_mcp/server.py`

1. Write failing tests for 256-bit one-time token creation, SHA-256-only persistence, one-token/one-wiki association, explicit existing-domain read/write grants, write-implies-read, revocation, disabled wiki, and `wiki_bind` non-escalation.
2. Implement token generation, constant-time digest comparison, a last-use update throttled to once per five minutes, and an immutable request context carrying `iwiki_id`, allowed read domains, and allowed write domains.
3. Verify a Bearer token before every hosted MCP request. Return `401` for missing/malformed/revoked/disabled/unknown tokens and `403` for authenticated requests outside grants, without disclosing token ownership, wiki identifiers, or domains.
4. Apply the same domain scope checks at the backend boundary, not only HTTP middleware, so a mistakenly routed handler cannot cross a tenant or ACL boundary.
5. Run:

```bash
test -n "${IWIKI_TEST_POSTGRES_DSN:?set IWIKI_TEST_POSTGRES_DSN to a disposable pgvector _test database}"
uv run pytest -q tests/postgres/test_auth.py
```

Expected: token database contents contain no plaintext secret and all tenant/ACL denials are enforced.

## Task 6: Add administration CLI, Git import, and rollback export

**Closes:** R10 and R11.

**Files:**
- Create: `src/iwiki_mcp/admin.py`, `tests/postgres/test_admin.py`
- Modify: `src/iwiki_mcp/server.py`, `src/iwiki_mcp/postgres/store.py`, `src/iwiki_mcp/postgres/migrations.py`

1. Write failing CLI tests for exact commands: `base create/list/show/disable/enable/import-git/export-git`, `domain create`, and `token create/list/revoke`. Require `--iwiki` consistently, `--token-id` for revocation, and `--config` or `IWIKI_SERVER_CONFIG` for every admin command.
2. Test parser boundaries: bare `iwiki-mcp --project` remains stdio; admin/serve commands reject `--project`; `serve --transport` defaults to `streamable-http`; list/show/dry-run support `--json`; no physical delete or force-overwrite command exists.
3. Implement base creation as an empty active wiki with no implicit domain or token; implement safe `base show`, explicit `domain create`, and reversible disable/enable without physical deletion. HTTP `wiki_create_domain` stays unsupported.
4. Implement token creation with explicit existing read domains and write subset validation. Print plaintext token exactly once on successful create; list only safe metadata and revoke only by `--token-id`.
5. Implement `base import-git --iwiki <slug> --path <directory>` as an explicit, single-transaction importer. Validate with current Markdown/chunk/link logic; make `--dry-run` mutation-free; check an already-completed identical fingerprint before the non-empty-target guard; never auto-import at startup.
6. Implement `base export-git --iwiki <slug> --path <directory>` from one consistent read snapshot. Require an absent/empty destination, write domain/page Markdown and a hash/count manifest, initialize Git, and create one export commit. Exclude auth and derived data; make `--dry-run` mutation-free and provide no force overwrite.
7. Add a PostgreSQL→Git export/reindex smoke test and import/export round-trip assertions for Markdown hashes, link targets, secret absence, and deterministic counts.
8. Run:

```bash
test -n "${IWIKI_TEST_POSTGRES_DSN:?set IWIKI_TEST_POSTGRES_DSN to a disposable pgvector _test database}"
uv run pytest -q tests/postgres/test_admin.py
```

Expected: operator lifecycle, consistent CLI UX, safe token output, import parity, rollback export, dry runs, and non-destructive retry behavior pass.

## Task 7: Serve authenticated Streamable HTTP safely

**Closes:** hosted rows of R1, the R2 boundary, hosted startup in R5, and HTTP enforcement from R9.

**Files:**
- Create: `src/iwiki_mcp/http.py`, `tests/postgres/test_http.py`
- Modify: `src/iwiki_mcp/server.py`, `src/iwiki_mcp/postgres/config.py`, `src/iwiki_mcp/postgres/auth.py`

1. Write failing HTTP integration tests for an authorized MCP initialize/tool request, missing and invalid Bearer tokens, supplied invalid Origin, allowed Origin, absent Origin from a non-browser client, domain-grant denial, and startup rejection for HTTP plus Git.
2. Keep bare `iwiki-mcp` as stdio. Add `iwiki-mcp serve --config <server.toml>` with optional/defaulted `--transport streamable-http`; initialize configuration, embedding probe, pool, and schema before constructing a listener; mount FastMCP at `/mcp`.
3. Add request middleware that checks supplied Origins against `server.allowed_origins`, extracts and authenticates Bearer credentials, and installs the immutable request context used by tool dispatch. Do not expose model keys, database credentials, SQL, or migration details through HTTP errors.
4. Apply bounded pool sizes, `statement_timeout`, and `lock_timeout` from validated server config. Verify `IWIKI_LLM_BASE_URL`/`IWIKI_LLM_KEY` remain server-only and configured embedding metadata matches the database before traffic.
5. Bind only configured loopback host/port. Document that the public service satisfies the TLS constraint at the reverse proxy; support same-host callers through loopback or public HTTPS using a Bearer token.
6. Run:

```bash
test -n "${IWIKI_TEST_POSTGRES_DSN:?set IWIKI_TEST_POSTGRES_DSN to a disposable pgvector _test database}"
uv run pytest -q tests/postgres/test_http.py
```

Expected: authorized Streamable HTTP works; denied requests receive no MCP data; invalid storage/transport never serves traffic.

## Task 8: Document supported operation and deployment

**Closes:** documentation acceptance in R2-R11 and the operator-owned production checkpoints.

**Files:**
- Modify: `README.md`, `docs/README.ru.md`, `docs/architecture.md`

1. Add verified examples for unchanged local Git stdio, local stdio PostgreSQL with explicit maximum domain scope and `IWIKI_DB_PASSWORD`, and hosted `serve` configuration using a specific database name and Origin allowlist.
2. Explain the one-database/shared-`iwiki_id` model, runtime-selectable embedding/reranking settings, current 1024-dimensional Lemonade deployment example, database model/dimension startup check, server-only model credentials, idempotent `iwiki` schema startup, database-role least privilege, bounded pool/timeouts, `sslmode=verify-full`, and reverse-proxy TLS/Origin boundary.
3. Document every admin command and parser boundary, one-time token handling, token revocation, domain creation/grants, reversible base disable, explicit Git import/export with dry-run, local reindex after rollback, and PostgreSQL-native encrypted backup/restore responsibility.
4. Document the complete MCP tool matrix and PostgreSQL `wiki_status`, required-revision, conflict, and unsupported-storage result shapes. State non-goals: HTTP+Git, automatic synchronization, database creation, physical delete, and automatic embedding-model migration.
5. Update the bound `iwiki-mcp` architecture/operation pages through iwiki MCP after implementation and run `wiki_lint`; do not create a repository `docs/wiki/` directory.
6. Run:

```bash
uv run iwiki-mcp --help
uv run iwiki-mcp serve --help
uv run iwiki-mcp base --help
uv run iwiki-mcp domain --help
uv run iwiki-mcp token --help
```

Expected: documented commands are present and command help contains no secrets.

## Task 9: Run complete regression and release checks

**Closes:** aggregate acceptance for R1-R11 and the intent's Done-when criterion.

**Files:**
- Modify: `pyproject.toml`, `uv.lock`, `src/iwiki_mcp/__init__.py`, `tests/test_package.py`

1. Follow the repository version rule in every implementation commit: apply one patch bump consistently to `pyproject.toml`, `uv.lock`, `src/iwiki_mcp/__init__.py`, and the package-version assertion. Do not hard-code the eventual final version in advance; minor/major remains out of scope unless separately requested.
2. Run the default offline suite with `IWIKI_TEST_POSTGRES_DSN` absent and assert PostgreSQL integration tests are reported as skipped rather than attempting network access:

```bash
env -u IWIKI_TEST_POSTGRES_DSN uv run pytest -q
```

Expected: all existing Git tests and all new unit tests pass; PostgreSQL integration skips are explicit.

3. Provision a disposable pgvector test database, set only `IWIKI_TEST_POSTGRES_DSN` for the test process, and run the complete PostgreSQL integration set:

```bash
test -n "${IWIKI_TEST_POSTGRES_DSN:?set IWIKI_TEST_POSTGRES_DSN to a disposable pgvector database}"
uv run pytest -q tests/postgres
```

Expected: migrations, metadata, storage, ranking, tool matrix, auth, CLI, export/import, and HTTP integration pass; zero integration tests skip.

4. Re-run focused security and deployment checks:

```bash
test -n "${IWIKI_TEST_POSTGRES_DSN:?set IWIKI_TEST_POSTGRES_DSN to a disposable pgvector database}"
uv run pytest -q tests/postgres/test_config.py tests/postgres/test_migrations.py tests/postgres/test_auth.py tests/postgres/test_admin.py tests/postgres/test_http.py tests/postgres/test_tool_matrix.py
uv run iwiki-mcp --help
```

Expected: startup/error redaction, schema/embedding boundary, ACL, CLI, rollback, complete tool/HTTP matrix, and stdio entry point are verified together. A skipped PostgreSQL integration run blocks result reconciliation.

## Execution Notes

- PostgreSQL tests require an explicitly provisioned disposable PostgreSQL service with pgvector; they skip offline and must never target a developer or production database. Result reconciliation requires a non-skipped integration run.
- The implementation must preserve existing Git test fixtures and must not migrate or import Git data implicitly.
- Before committing implementation, run the required result reconciliation against the approved intent, specification, and this plan.
