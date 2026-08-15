---
review:
  plan_hash: e6739c25322aa605
  last_run: 2026-08-15
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-08-14-domain-token-management-intent.md
  spec: docs/superpowers/specs/2026-08-14-domain-token-management-design.md
---

# Domain Token Management Implementation Plan

**Goal:** Let authorized hosted PostgreSQL bearer tokens provision project
domains and manage other tokens' content grants without delegating management
authority.

**Architecture:** Migration v4 stores tenant creation authority on tokens and
per-domain management authority in a table separate from content grants.
Authentication loads both authority shapes into an immutable request context.
Hosted middleware persists explicit session selection but derives a fresh
request-local effective scope. Authenticated store methods recheck authority
inside mutation transactions. Git creation stays unchanged; PostgreSQL stdio
rejects hosted-only grant operations.

**Tech Stack:** Python 3.10+, psycopg 3, PostgreSQL/pgvector migrations,
FastMCP Streamable HTTP, pytest/pytest-asyncio, Starlette TestClient, uv.

## Source Contract

- Intent: `docs/superpowers/intents/2026-08-14-domain-token-management-intent.md`
  at approved body hash `5abfc7209ba1336e`.
- Specification:
  `docs/superpowers/specs/2026-08-14-domain-token-management-design.md` at
  approved body hash `fd2095a0c2926347`.
- Requirements: R1-R9.
- Excluded: domain deletion/archive/transfer, metadata administration, generic
  RBAC, HTTP management-authority delegation, dynamic `tools/list` filtering,
  and session-registry capacity policy.

Implementers must return contract drift to the specification gate. They must
not revise approved intent or specification during execution.

## File Map

- `src/iwiki_mcp/postgres/migrations.py`: additive migration v4 and indexes.
- `src/iwiki_mcp/postgres/auth.py`: strict domain validator, authority context,
  authentication, provisioning, grant transactions, and admin recovery methods.
- `src/iwiki_mcp/admin.py`: creation capability and recovery CLI.
- `src/iwiki_mcp/http.py`: fail-closed pre-dispatch checks, fresh authority,
  selected-session persistence, and request state installation.
- `src/iwiki_mcp/server.py`: request context, selected/effective holder, hosted
  tool adapters, transport guards, and immediate creator expansion.
- `eval/auth_grant_latency.py`: fixed-fixture authentication cost check.
- PostgreSQL, HTTP, server, tool-matrix, admin, and package tests: observable
  contract evidence.
- `README.md`, `docs/README.ru.md`, `docs/architecture.md`: public and operator
  contract, including forward-only migration rollback limits.

## Dependency Order

Tasks 1-4 build the storage and authorization service. Task 5 introduces the
session model used by Task 6. Task 6 exposes the HTTP surface. Task 7 measures
and documents the completed path. Task 8 reconciles the branch as one result.

## Requirement Coverage

| Requirement | Plan tasks |
|---|---|
| R1 Tenant-scoped creation | 1, 2, 3, 6, 8 |
| R2 Atomic bootstrap authority | 1, 3, 6, 8 |
| R3 Idempotent creation | 3, 5, 6, 8 |
| R4 Content-grant administration | 1, 2, 3, 6, 8 |
| R5 Non-delegable management authority | 3, 4, 6, 8 |
| R6 Immediate revocation/session semantics | 2, 3, 5, 6, 8 |
| R7 Database and CLI auditability | 1, 3, 4, 7, 8 |
| R8 Empty-tenant bootstrap | 1, 2, 4, 8 |
| R9 Compatibility and bounded cost | 1, 2, 6, 7, 8 |

### Task 1: Add migration v4, strict identifiers, and context fields

**Closes:** storage and default-authority parts of R7-R9; foundations for R1-R6.

**Files:**
- Modify: `src/iwiki_mcp/postgres/migrations.py`
- Modify: `src/iwiki_mcp/postgres/auth.py`
- Modify: `tests/postgres/test_migrations.py`
- Modify: `tests/postgres/test_auth.py`

- [ ] **Step 1: Make migration tests version-dynamic before adding v4**

Replace hard-coded `(1, 2, 3)`, `[1, 2, 3]`, and synthetic
`Migration(version=4)` expectations with values derived from `MIGRATIONS` and
`MIGRATIONS[-1].version + 1`. Extend expected schema objects and constraints for
`token_domain_management_grants`. This prevents the migration failure test from
creating a duplicate real v4.

- [ ] **Step 2: Write failing v4 and validator tests**

Assert:

- `tokens.can_create_domain` is `NOT NULL DEFAULT false`;
- management rows require tenant-composite token/domain foreign keys and a true
  `can_manage_grants` value;
- both grant tables have an index beginning `(iwiki_id, domain_id)`;
- existing tokens receive no new authority;
- one shared validator accepts `new-project` and rejects empty, trim-invalid,
  leading-dot, slash, and backslash identifiers;
- `AuthContext` defaults new fields to false/empty, preserves them through
  `narrow()`, and never derives primary from management-only domains.

- [ ] **Step 3: Verify RED without breaking the synthetic migration test**

```bash
uv run pytest -q tests/postgres/test_migrations.py tests/postgres/test_auth.py -k "migration or schema or identifier or context"
```

Expected: synthetic migration rollback test remains valid; new v4/context tests
fail because schema and fields do not exist.

- [ ] **Step 4: Implement migration v4 and immutable context helpers**

Append one forward-only migration containing the token column, management
table, constraints, and both indexes from specification section 3. Add a
strict PostgreSQL domain helper in `postgres/auth.py`, accessible to auth and
admin without circular imports. Extend `AuthContext` with
`can_create_domain`, `managed_domains`, `can_manage_grants()`, and
`require_manage_grants()`. Preserve new fields in `narrow()`.

- [ ] **Step 5: Verify migration/context GREEN**

```bash
uv run pytest -q tests/postgres/test_migrations.py tests/postgres/test_auth.py
```

Expected: PASS; applied versions include v4 dynamically and legacy rows have
no authority.

### Task 2: Load capabilities and create bootstrap tokens

**Closes:** R8-R9; supplies request authority for R1, R4, and R6.

**Files:**
- Modify: `src/iwiki_mcp/postgres/auth.py`
- Modify: `tests/postgres/test_auth.py`

- [ ] **Step 1: Write failing authentication and creation tests**

Cover:

- create-only token with no read/write domains and `can_create_domain=True`;
- authority-free token with no reads rejects `read grant is required`;
- write remains a subset of read on both old and new creation paths;
- authentication loads creation, content, and management authority;
- management-only domain appears only in `managed_domains`, never in `primary`;
- explicit AuthContext reconstruction preserves both new fields;
- a cursor/connection spy records the same three authentication statements:
  token lookup, one combined domain-authority query, and throttled last-used
  update. No fourth management query is allowed.

- [ ] **Step 2: Verify capability-loading RED**

```bash
uv run pytest -q tests/postgres/test_auth.py -k "create_only or capability or management or query_count"
```

Expected: FAIL on absent creation argument, columns, and combined authority
loading.

- [ ] **Step 3: Implement combined loading**

Add keyword-only `can_create_domain=False` to `create_token`; permit empty
reads only when true. Insert/select the new token field. Replace the
content-only domain query with one domain-rooted query that left joins both
caller grant tables and returns domains having either authority. Build sorted
read, write, and managed tuples. Keep `primary=write[0] if write else None`.

- [ ] **Step 4: Verify capability-loading GREEN**

```bash
uv run pytest -q tests/postgres/test_auth.py
```

Expected: PASS; authentication adds no query round-trip.

### Task 3: Add atomic provisioning and content-grant transactions

**Closes:** R1-R5 and transaction parts of R7.

**Files:**
- Modify: `src/iwiki_mcp/postgres/auth.py`
- Modify: `tests/postgres/test_auth.py`

- [ ] **Step 1: Write failing provisioning tests**

Exercise new `AuthStore.provision_domain(context, domain)`; do not call or
weaken `PostgresStore.create_domain` or `_require_admin`. Store-layer result is
limited to domain and idempotency state; server response shape belongs to Task
6. Assert new creation atomically leaves domain, caller read/write grant, and
caller management grant. Exact same-caller retry returns idempotent success
without changing row snapshots.

Add cases for missing create authority, revoked caller, injected failure after
each write, occupied foreign domain, and two competing callers. Every failure
must leave no partial or stolen grant.

- [ ] **Step 2: Write failing content-grant tests**

Cover `list_domain_grants`, `set_domain_grant`, and `revoke_domain_grant` with:

- active same-tenant target success;
- write-without-read and false/false validation;
- self-target, revoked/missing/cross-tenant target, unmanaged domain, and
  concurrent management revocation denial;
- stable delete no-op only after caller/target authorization;
- management table unchanged by every content mutation;
- listing includes real `can_manage_grants` values without bearer material.

- [ ] **Step 3: Verify transaction RED**

```bash
uv run pytest -q tests/postgres/test_auth.py -k "provision or domain_grant"
```

Expected: FAIL because authenticated transactions do not exist.

- [ ] **Step 4: Implement transaction-local rechecks**

Each mutation opens one transaction, locks the active caller token, and
rechecks the required capability in the caller's `iwiki_id`. Provisioning uses
the shared strict validator and creates all three bootstrap rows. Uniqueness
conflict succeeds only when exact caller bootstrap ownership already exists.
Grant methods resolve domain and active target in the same tenant, reject
self-target, and change only `token_domain_grants`.

Raise `AccessError(403)` for authorization/race states so server `_safe`
produces the existing in-band `access_denied` response. Raise `ValueError` for
authorized syntax/boolean/invariant failures.

- [ ] **Step 5: Verify transaction GREEN**

```bash
uv run pytest -q tests/postgres/test_auth.py
```

Expected: PASS; forced rollbacks leave identical pre-call row snapshots.

### Task 4: Add admin provisioning and recovery controls

**Closes:** R5, R7-R8.

**Files:**
- Modify: `src/iwiki_mcp/postgres/auth.py`
- Modify: `src/iwiki_mcp/admin.py`
- Modify: `tests/postgres/test_admin.py`
- Modify: `tests/postgres/test_auth.py`

- [ ] **Step 1: Write failing CLI tests**

Cover:

- `token create --can-create-domain` without `--read-domain`;
- parser default `--read-domain=[]`, while service rejects empty authority-free
  creation with `read grant is required`;
- `token set-create-domain --enabled|--disabled`;
- `token set-domain-management --domain ... --enabled|--disabled`;
- mutually exclusive/missing switches and strict invalid-domain rejection;
- wrong tenant, inactive token, missing domain, and idempotent enable/disable;
- default token list and `--json` return the same JSON shape with actual
  `can_create_domain`, `managed_domains`, `read_domains`, and `write_domains`;
- update the existing exact dictionary assertion; no secret or digest appears.

- [ ] **Step 2: Verify admin CLI RED**

```bash
uv run pytest -q tests/postgres/test_admin.py tests/postgres/test_auth.py -k "create_domain or management or token"
```

Expected: FAIL because CLI flags, recovery methods, and capability fields are
absent.

- [ ] **Step 3: Implement bounded recovery methods and CLI**

Add `set_create_domain(iwiki_id, token_id, enabled)` and
`set_domain_management(iwiki_id, token_id, domain, enabled)`. Validate active
same-tenant token and existing strict domain. Enable inserts/updates only the
named authority; disable clears the token flag or deletes the management row.
Do not expose these writes through MCP.

Set `--read-domain` to `action="append", default=[]`. Keep token-list output
JSON in both current branches; `--json` remains a compatibility no-op. Populate
all list fields from real rows.

- [ ] **Step 4: Verify admin CLI GREEN**

```bash
uv run pytest -q tests/postgres/test_admin.py tests/postgres/test_auth.py
```

Expected: PASS; recovery changes only the named capability.

### Task 5: Separate persisted selection from request-effective scope

**Closes:** R6 and session portions of R3.

**Files:**
- Modify: `src/iwiki_mcp/http.py`
- Modify: `src/iwiki_mcp/server.py`
- Modify: `tests/test_http_unit.py`
- Modify: `tests/postgres/test_http.py`

- [ ] **Step 1: Write failing state-model tests**

Persist a selected binding for `docs,private`, authenticate fresh authority
for only `docs`, and assert the request effective scope is only `docs` while
the stored selected scope remains `docs,private`. Restore fresh `private`
authority and assert it reappears because explicit selection was preserved.
Conversely, add a freshly granted `new` domain and assert it does not appear in
an existing target session.

Add tests that successful `wiki_bind` persists explicit narrowing, while a
normal fresh-grant intersection never persists. Cover primary fallback only
within effective write scope.

- [ ] **Step 2: Write failing request-context and session-ID tests**

Assert middleware installs the complete authenticated `AuthContext`, including
real token ID and both management fields, beside hosted binding state and
resets both ContextVars in `finally`. PostgreSQL stdio must retain an explicit
authority-free fallback.

For successful responses, assert capture stores selected state under a new
response `mcp-session-id` or the incoming successful request session ID when
the response omits one. No plaintext bearer is retained.

- [ ] **Step 3: Verify state-model RED**

```bash
uv run pytest -q tests/test_http_unit.py tests/postgres/test_http.py -k "session or context or revocation"
```

Expected: FAIL because current resolution returns and reuses one shared mutable
effective holder.

- [ ] **Step 4: Implement selected/effective holders**

Keep registry records as persistent explicit selected state. For every request,
derive a new request-local holder whose effective binding intersects selected
read/write with fresh content grants and whose primary remains effective and
writable. The holder keeps a reference to selected state so only explicit
`wiki_bind` or creator expansion can update persistence under its lock.

Install full auth context and request-local holder together. Store state before
forwarding `http.response.start`, using response session ID first and incoming
session ID as fallback. Reset both ContextVars in reverse order. Do not add
token fields to `PostgresBinding` and do not use `_SESSION_BINDING` as the
capability source.

- [ ] **Step 5: Verify state-model GREEN**

```bash
uv run pytest -q tests/test_http_unit.py tests/postgres/test_http.py
```

Expected: PASS; revoke is immediate without converting transient removal into
explicit narrowing.

### Task 6: Expose hosted creation and grant tools safely

**Closes:** R1-R6 and hosted surface of R9.

**Files:**
- Modify: `src/iwiki_mcp/http.py`
- Modify: `src/iwiki_mcp/server.py`
- Modify: `tests/test_http_unit.py`
- Modify: `tests/postgres/test_http.py`
- Modify: `tests/postgres/test_tool_matrix.py`
- Modify: `tests/test_server_write.py`
- Modify: `tests/test_create_domain_layout.py`

- [ ] **Step 1: Write fail-closed authorization tests first**

For recognized protected tools, non-dictionary `params`, non-dictionary or
missing `arguments`, explicit `iwiki_id`, and absent capability must return the
existing HTTP `403 {"error": "access denied"}` before dispatch. Unknown and
ordinary calls retain existing parsing behavior. Add `_DOMAIN_GRANT_TOOLS` as
the fourth authorization category; creation keeps its own capability check.

Authorized but invalid domain/boolean/content combinations must reach MCP/tool
validation, not be mislabeled as missing capability. Transaction-time
revocation/cross-tenant/self-target remains HTTP 200 with in-band
`access_denied`.

- [ ] **Step 2: Write transport and schema matrix tests**

Update the exact tool count from 22 to 25 and registered set. Assert:

- Git `wiki_create_domain` response remains string-valued and existing layout
  tests stay unchanged;
- Git and PostgreSQL stdio return `unsupported_transport` with correct
  `storage`, `transport`, and hint for all three grant tools;
- PostgreSQL stdio still rejects creation;
- removing creation from the shared guard does not unlock
  `wiki_remediation_plan`, `wiki_migrate_okf`, `wiki_apply_okf`,
  `wiki_export_okf`, or `wiki_sync`;
- tool schema introspection proves no grant tool accepts `iwiki_id`, management
  authority, or any management-write field;
- static `tools/list` remains unfiltered by capability.

- [ ] **Step 3: Write hosted end-to-end tests**

Assert successful hosted creation returns:

```python
{
    "created": "new-project",
    "already_existed": False,
    "domain": "new-project",
    "read": [*complete_effective_read],
    "write": [*complete_effective_write],
    "primary": "new-project",
}
```

Exact retry returns the same effective scope with `already_existed=True` and
unchanged database rows. Both paths expand selected and effective creator state
under the request holder lock. Build the response directly from the expanded
holder; do not call `_resolved_binding`, because `_MUTATION_BINDING` pins the
pre-call snapshot.

Cover list/set/revoke success, pre-dispatch denial, in-transaction denial,
invalid values, no automatic target-session expansion, immediate target revoke,
and no management-authority mutation.

- [ ] **Step 4: Verify hosted-tool RED**

```bash
uv run pytest -q tests/test_http_unit.py tests/postgres/test_http.py tests/postgres/test_tool_matrix.py tests/test_server_write.py tests/test_create_domain_layout.py
```

Expected: new auth, tool, and transport tests fail; existing Git tests remain
green.

- [ ] **Step 5: Implement dedicated guards and hosted adapters**

Authorize protected names before permissive argument exits. Remove only
`wiki_create_domain` from `_postgres_unsupported_guard`. Give it a dedicated
transport branch: Git executes the existing body unchanged, hosted PostgreSQL
uses request AuthContext and `AuthStore.provision_domain`, and PostgreSQL stdio
returns unsupported. Leave the other five wrappers untouched.

Add exact-signature `_safe` functions for list/set/revoke and one hosted-only
guard that reports actual storage and transport. Use request-local AuthContext;
never synthesize hosted `token_id=""`. Register each tool once. After new or
idempotent provisioning, lock and expand request selected/effective state,
then return the complete effective scope.

- [ ] **Step 6: Verify hosted-tool GREEN**

```bash
uv run pytest -q tests/test_http_unit.py tests/postgres/test_http.py tests/postgres/test_tool_matrix.py tests/test_server_write.py tests/test_create_domain_layout.py
```

Expected: PASS; malformed protected envelopes cannot bypass capability checks,
and Git behavior remains unchanged.

### Task 7: Measure cost, document migration, and bump version

**Closes:** R7, R9, health metrics, and public/operator documentation.

**Files:**
- Add: `eval/auth_grant_latency.py`
- Modify: `README.md`
- Modify: `docs/README.ru.md`
- Modify: `docs/architecture.md`
- Modify: `pyproject.toml`
- Modify: `src/iwiki_mcp/__init__.py`
- Modify: `tests/test_package.py`
- Modify: `uv.lock`

- [ ] **Step 1: Add executable latency evidence**

Create a fixed seeded fixture and two SQL paths on the same configured
PostgreSQL instance: legacy content-only lookup and new combined authority
lookup. Warm both, then run three rounds of 500 authentications per path.
Compute each round p95 and compare medians. Exit non-zero when
`new_p95 / legacy_p95 > 1.25`; print counts, p95 values, ratio, and fixture
identity without credentials.

```bash
uv run python eval/auth_grant_latency.py --help
```

Expected: exits `0` and documents required DSN/config input.

- [ ] **Step 2: Write documentation assertions**

Extend package/doc tests to require both capability names, hosted provisioning,
all three grant tools, non-delegation, selected/effective session semantics,
CLI recovery, local ownership of `.iwiki.toml`/`.iwikiignore`, and migration v4
rollback warning.

- [ ] **Step 3: Update English and Russian public docs**

Document exact tool response/error contracts and admin commands. Architecture
must explain separate tables, combined authentication query, transaction-local
rechecks, and forward-only deployment: an older binary rejects v4, so rollback
requires database restore or a compatibility release; no down migration exists.

- [ ] **Step 4: Bump package version consistently**

Bump one patch from the plan-approved version in `pyproject.toml`, package
`__version__`, exact package test, and `uv.lock`.

```bash
uv lock
uv run pytest -q tests/test_package.py
```

Expected: package metadata and runtime version match.

- [ ] **Step 5: Run configured latency gate**

```bash
uv run python eval/auth_grant_latency.py
```

Expected: three 500-call warm rounds complete and median p95 ratio is `<= 1.25`.
If PostgreSQL integration infrastructure is unavailable, record this as pending
delivery; do not claim the health metric passed.

### Task 8: Verify and reconcile the complete result

**Closes:** all R1-R9 acceptance criteria and Done-when conditions.

- [ ] **Step 1: Run focused contract suites**

```bash
uv run pytest -q tests/postgres/test_migrations.py tests/postgres/test_auth.py tests/postgres/test_admin.py tests/postgres/test_http.py tests/postgres/test_tool_matrix.py tests/test_http_unit.py tests/test_server_write.py tests/test_create_domain_layout.py tests/test_package.py
uv run flake8 src tests eval/auth_grant_latency.py
uv run python -m compileall -q -x 'tests/fixtures/codegraph/python_syntax_errors/broken\.py' src tests eval
uv run iwiki-mcp --help
git diff --check
```

Expected: every command exits `0`. PostgreSQL skips through the established
fixture are acceptable only for local smoke work; configured PostgreSQL runs
and latency evidence remain required before result completion.

- [ ] **Step 2: Run full suite**

```bash
uv run pytest -q
```

Expected: PASS with PostgreSQL integration configured for final evidence.

- [ ] **Step 3: Review the full branch diff**

Map every changed line and test to R1-R9. Confirm no HTTP path writes management
authority, no legacy Git behavior changed, no shared guard unlocked adjacent
tools, no request intersection mutates persisted selection, and no secret,
digest, DSN, or credential appears in output or logs.

- [ ] **Step 4: Update iwiki task and architecture pages**

Update functionality/architecture pages through iwiki MCP, append verification
evidence to `reference/tasks/domain-token-management`, replay any spool, and run
`wiki_lint`.

Expected: no task-introduced broken, stale, missing-source, or task-page
finding. Expected task-page orphan advisory alone remains non-blocking.

- [ ] **Step 5: Run chain result reconciliation**

```bash
$check-chain result docs/superpowers/plans/2026-08-14-domain-token-management.md --since=origin/master
```

Expected: `OK`; all approved source commitments map to diff/test/latency/wiki
evidence and no open CRITICAL finding remains.

## Commit Boundaries

Use Conventional Commits. Keep the migration/context, auth transactions, CLI,
session model, hosted tools, and docs/version changes reviewable as separate
commits when practical. Stage only task-owned files. The implementation version
bump belongs to the completed feature result, not intermediate red tests.

Do not commit this plan until its fresh plan gate passes and the user approves
it.
