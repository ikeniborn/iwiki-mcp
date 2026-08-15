---
review:
  spec_hash: 19b936e63885b62f
  last_run: 2026-08-15
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings:
    - id: F-001
      phase: consistency
      severity: CRITICAL
      section: "5. Domain Provisioning Flow"
      section_hash: 87b101a49d8418b5
      fragment: '"created": true'
      text: "Hosted response changed the existing created field from string to boolean."
      fix: "Keep created as the domain string and add already_existed as a boolean."
      verdict: fixed
    - id: F-002
      phase: consistency
      severity: CRITICAL
      section: "4. Authentication and Authorization"
      section_hash: e682c70152f3bbc8
      fragment: "arguments is not a dictionary"
      text: "Malformed protected tool calls could bypass pre-dispatch capability checks."
      fix: "Authorize a recognized protected tool name before any permissive argument return."
      verdict: fixed
    - id: F-003
      phase: consistency
      severity: CRITICAL
      section: "4. Authentication and Authorization"
      section_hash: e682c70152f3bbc8
      fragment: "persisted session binding"
      text: "In-place fresh-grant intersection would permanently mutate explicit session narrowing."
      fix: "Separate persisted selected scope from the serialized transient effective carrier."
      verdict: fixed
    - id: F-004
      phase: coverage
      severity: CRITICAL
      section: "10. Verification Strategy"
      section_hash: 23c95156a178a59a
      fragment: "Migration v4"
      text: "The migration plan omitted existing dynamic-version and hard-coded v1-v3 tests."
      fix: "Require migration tests to derive the next version and expected applied versions."
      verdict: fixed
    - id: F-005
      phase: clarity
      severity: WARNING
      section: "8. Error Contract"
      section_hash: 26052d90f6cbdac1
      fragment: "403 access denied"
      text: "The source did not distinguish HTTP pre-dispatch denial from in-band transaction-time denial."
      fix: "Specify both boundaries and hosted-only unsupported payloads."
      verdict: fixed
    - id: F-006
      phase: coverage
      severity: WARNING
      section: "9. Files and Boundaries"
      section_hash: 5a0f3deb7433eafd
      fragment: "hosted tool routing"
      text: "The source omitted actual guard split, strict validator, session holder, indexes, and exact-test impacts."
      fix: "Describe the concrete code boundaries and verification targets."
      verdict: fixed
chain:
  intent: docs/superpowers/intents/2026-08-14-domain-token-management-intent.md
  spec: null
---

# Domain Token Management Design

**Date:** 2026-08-14
**Status:** approved
**Intent:** `docs/superpowers/intents/2026-08-14-domain-token-management-intent.md`

## 1. Scope

Hosted Streamable HTTP with PostgreSQL will support controlled domain
provisioning and content-grant administration by authenticated bearer tokens.
The design adds exactly two administrative capabilities:
`tenant:domain:create` and `domain:grants:manage`.

The existing Git implementation of `wiki_create_domain` remains unchanged.
PostgreSQL stdio continues to use the administrative CLI for provisioning.
Domain deletion, archival, ownership transfer, metadata administration, and
HTTP delegation of management authority are outside this design.

"Alongside `.iwiki.toml` and `.iwikiignore`" describes initializer sequencing,
not remote file mutation: the hosted tool changes PostgreSQL only, while the
client-side initializer owns those local files and then binds the returned
domain.

## 2. Requirements

### R1. Tenant-scoped domain creation

An authenticated hosted token with `tenant:domain:create` can call the existing
`wiki_create_domain(name)` tool. The capability applies only to the token's
existing `iwiki_id`; callers cannot provide or override an `iwiki_id`.

Acceptance criterion: a capable token creates a valid domain in its own tenant,
while a token without the capability receives the same sanitized `403 access
denied` response used by other hosted authorization failures. Hosted creation
preserves the existing string-valued `created` field and adds an explicit
`already_existed` boolean, so Git and hosted responses do not overload one key
with different types.

### R2. Atomic bootstrap authority

The domain row, caller content grant with `can_read=true` and `can_write=true`,
and caller management grant with `can_manage_grants=true` are committed in one
PostgreSQL transaction. The target token is always the current bearer token.

Acceptance criterion: injected failure at any write rolls back every row, and a
successful call leaves all three rows visible through PostgreSQL queries.

### R3. Idempotent creation

A retry by the same caller succeeds when the domain already
exists and the caller already owns its read, write, and management grants. In
the concrete response this state is `created=<domain>` and
`already_existed=true`. An
existing domain not managed by the caller returns sanitized denial without
revealing its owner or grants.

Acceptance criterion: a lost-response retry returns the same effective scope,
restores the creator's request-local/session selection for that domain, and
does not add or change database rows; a competing caller receives no grant.

### R4. Content-grant administration

Three hosted PostgreSQL tools expose bounded administration:

- `wiki_list_domain_grants(domain)` lists token IDs, owner labels, content
  rights, and whether each token has management authority.
- `wiki_set_domain_grant(domain, token_id, can_read, can_write)` creates or
  replaces one content grant.
- `wiki_revoke_domain_grant(domain, token_id)` deletes one content grant.

All three require the caller's `domain:grants:manage` capability for `domain`.
Set and revoke reject a target token equal to the caller, a cross-tenant or
revoked target, `can_write=true` with `can_read=false`, and `false/false`.
Callers use revoke instead of storing an empty content grant.

Acceptance criterion: a manager can list and change another active token's
read/write grant in the same tenant and domain, but cannot change its own grant
or any management grant.

### R5. Management authority is not HTTP-delegable

No MCP argument or tool can create, update, or delete another token's
`domain:grants:manage` capability. Management authority arises from successful
domain bootstrap or explicit server-side admin CLI recovery.

Acceptance criterion: the MCP tool schemas contain no management-write field,
schema introspection confirms that absence, and attempts to smuggle management
or tenant identifiers are rejected before a database mutation.

### R6. Immediate revocation

Every hosted request authenticates against current database grants. A stored
session retains the user's explicit selected scope. Middleware derives a fresh
effective snapshot by intersecting that selected scope with authenticated content
grants, publishes it through a serialized per-session transport carrier for the
duration of FastMCP dispatch, and resets the transient state afterward. It never
writes a transient revocation back into the persisted selection. Revoked access
therefore disappears on the next request, while later restoration can reappear only
when the domain remained in the explicit selection. Newly granted access does not
expand an existing target session; the target starts a new MCP session to use it.

The successful creator call is the only bounded expansion of its current
session: after commit, the new domain is added to read/write and becomes
primary.

Acceptance criterion: a target's established session loses revoked access on
its next request without corrupting its explicit selection, does not gain newly
granted access, and the creator can use the new domain immediately in the
creation session or after an idempotent retry. Existing request session IDs are
persisted on any successful response even when no new response session header
is emitted.

### R7. Database and CLI auditability

Token listing exposes `can_create_domain`, `managed_domains`, `read_domains`,
and `write_domains` without returning bearer secrets. The admin CLI can grant
or revoke tenant creation authority and per-domain management authority for
provisioning and recovery.

Acceptance criterion: SQL and `token list --json` identify each token's content
and management authority. The existing default token-list output remains JSON;
`--json` remains a compatibility no-op. CLI recovery changes only the named
tenant, token, domain, and capability.

### R8. Empty-tenant bootstrap

`token create` accepts no content domains only when
`--can-create-domain` is supplied. Without that capability, the existing
requirement for at least one read domain remains. A create-only token receives
content and management grants only after successful domain creation.

Acceptance criterion: an administrator can create a provisioning bearer for a
tenant with no domains, while an authority-free token with no read domain is
still rejected.

### R9. Compatibility and bounded cost

Migration defaults grant no new authority. Existing token and content-grant
behavior remains unchanged. Authentication loads the two new authority shapes
without adding a database round-trip to the normal hosted request path. Both
grant tables have indexes beginning with `(iwiki_id, domain_id)` for domain
listing and foreign-key cascade paths.

Acceptance criterion: legacy token tests remain green, existing rows
authenticate with both new capabilities disabled, and query-level tests show
the token lookup plus combined domain-authority lookup retain the existing
authentication query count. On the same PostgreSQL instance and fixed seeded
fixture, the median of three runs of 500 warm authentications must keep p95 at
or below 1.25 times the legacy content-only SQL baseline.

## 3. PostgreSQL Model

Migration v4 adds:

```sql
ALTER TABLE iwiki.tokens
    ADD COLUMN can_create_domain boolean NOT NULL DEFAULT false;

CREATE TABLE iwiki.token_domain_management_grants (
    iwiki_id text NOT NULL,
    token_id text NOT NULL,
    domain_id bigint NOT NULL,
    can_manage_grants boolean NOT NULL,
    PRIMARY KEY (iwiki_id, token_id, domain_id),
    CONSTRAINT token_domain_management_grants_enabled
        CHECK (can_manage_grants),
    CONSTRAINT token_domain_management_grants_iwiki_token_fk
        FOREIGN KEY (iwiki_id, token_id)
        REFERENCES iwiki.tokens (iwiki_id, token_id)
        ON DELETE CASCADE,
    CONSTRAINT token_domain_management_grants_iwiki_domain_fk
        FOREIGN KEY (iwiki_id, domain_id)
        REFERENCES iwiki.domains (iwiki_id, domain_id)
        ON DELETE CASCADE
);

CREATE INDEX token_domain_grants_domain_idx
    ON iwiki.token_domain_grants (iwiki_id, domain_id);

CREATE INDEX token_domain_management_grants_domain_idx
    ON iwiki.token_domain_management_grants (iwiki_id, domain_id);
```

The explicit boolean keeps management authority visible in SQL output. Only a
`true` row is valid; revocation deletes the row. Composite foreign keys preserve
tenant isolation and cascade token/domain deletion.

`token_domain_grants` remains content-only and is not rewritten by the
migration. All existing tokens receive `can_create_domain=false`, and the new
management table starts empty.

Migrations remain forward-only. Because older binaries reject a database whose
schema version is newer than they know, binary rollback after v4 requires a
database restore or a compatibility release; no destructive down migration is
added. Deployment documentation must state this stop condition before applying
v4.

## 4. Authentication and Authorization

`AuthContext` gains immutable `can_create_domain: bool` and
`managed_domains: tuple[str, ...]` fields plus `can_manage_grants(domain)` and
require helpers. Existing read/write methods retain their semantics. Every
explicit reconstruction site (`AuthContext.narrow`, hosted middleware, and the
stdio fallback in `server._postgres_store_for_binding`) must preserve or
intentionally default the new fields. `primary` continues to derive only from
write domains; management-only domains never become primary.

Authentication reads `can_create_domain` in the existing token-row query. Its
existing domain query becomes a combined indexed query over the tenant's domain
rows with left joins to the caller's content and management grants. It returns
any domain for which either grant exists, so a recovery manager need not also
hold content access. No extra query is added.

The HTTP middleware installs the full authenticated context in a request-local
context variable beside `_HostedBindingState`. FastMCP retains the initialization
task's ContextVar object, so the session record also retains that transport carrier;
requests are serialized while its effective binding and authenticated context are
replaced, then both are reset after dispatch. No `PostgresBinding` token field is
added: hosted code reads the real token/capabilities through the current carrier,
while PostgreSQL stdio uses the explicit authority-free fallback.

The carrier references a separate persistent explicit selected binding. Every request
derives an effective binding intersected with fresh grants, but normal refresh never
writes that intersection back to selected state. A successful `wiki_bind` persists its
explicit narrowing; successful domain creation or idempotent recovery explicitly
expands both selected and effective bindings. Response capture stores the carrier under
the response session ID or, when absent, the successful request session ID. Outside an
active request its effective/authenticated fields are reset, so only explicit selection
has durable authority semantics. This keeps creator expansion available after the call
without confusing revocation with user narrowing.

Middleware recognizes protected tool names before permissive parsing exits.
For a recognized protected call, malformed/non-dictionary arguments fail
closed and never reach dispatch. A fourth `_DOMAIN_GRANT_TOOLS` category owns
the three grant tools; creation has its own capability rule. Static
`tools/list` remains unchanged and may advertise unavailable tools because tool
discovery is not the authorization boundary.

Each mutating `AuthStore` method also locks and rechecks the active token,
tenant, and capability inside its
write transaction. This second check closes the interval between bearer
authentication and mutation if an administrator revokes authority concurrently.

## 5. Domain Provisioning Flow

The shared PostgreSQL unsupported guard remains unchanged for the other five
Git-only tools. `wiki_create_domain` is removed from that shared guard and gets
a dedicated guard: Git behavior remains unchanged, PostgreSQL stdio remains
unsupported, and hosted PostgreSQL dispatches to a new authenticated
`AuthStore.provision_domain` method. `PostgresStore.create_domain` and its
`_require_admin` rule remain unchanged.

Domain validation moves to one strict PostgreSQL-safe helper shared by admin
and authenticated provisioning. It rejects empty or trim-invalid identifiers,
leading dots, `/`, and `\\`; server and admin must not import each other.

The store transaction performs this sequence:

1. Lock and recheck the active caller token and `can_create_domain` in its
   authenticated `iwiki_id`.
2. Insert the validated domain with the existing tenant/domain unique
   constraint.
3. Insert the caller's read/write content grant.
4. Insert the caller's management grant.
5. Commit.

On a uniqueness conflict, the transaction reads the existing domain and caller
grants. Exact bootstrap ownership produces the idempotent result; every other
state produces sanitized denial. Concurrent creators therefore cannot attach
grants to another creator's domain.

After either a new commit or an exact idempotent match, the server locks the
request-local state directly, adds the domain to selected/effective read and
write scope, and selects it as primary. It does not rebuild the response through
`_resolved_binding`, because `_MUTATION_BINDING` contains the pre-call snapshot.
The response is:

```json
{
  "created": "new-project",
  "already_existed": false,
  "domain": "new-project",
  "read": ["new-project"],
  "write": ["new-project"],
  "primary": "new-project"
}
```

The arrays contain the complete effective session scope, not only the new
domain. An idempotent retry returns the same shape with
`already_existed=true`.

## 6. Content-Grant Tool Flow

`wiki_list_domain_grants` performs an authorized domain-scoped join across
tokens, content grants, and management grants. It returns revoked tokens only
when a retained grant row exists; normal token revocation cascades both grant
types, so this state is not expected through supported administration.

`wiki_set_domain_grant` rechecks caller management authority and target token
activity in one transaction, resolves the domain in the same tenant, and
upserts only `can_read` and `can_write`. It never touches the management table.

`wiki_revoke_domain_grant` applies the same checks and deletes only the target's
content row. A missing target content row returns a stable no-op result only
after caller and target authorization succeeds; unauthorized and cross-tenant
states remain indistinguishable.

All tools reject a target equal to the caller before mutation. Local
`wiki_bind` remains the supported way for a token to narrow its own effective
content scope. This self-target rule prevents accidental lockout; it is not a
credential-compromise boundary. A manager controlling another token's bearer
secret is already outside the token-isolation threat model. Management
non-delegation and admin-only recovery remain the authority boundaries.

## 7. Admin CLI

`token create` adds optional `--can-create-domain`. `--read-domain` becomes
parser-optional with `default=[]`; service validation requires at least one read
domain unless the creation capability is enabled.

Explicit recovery commands set the two management capability types:

```text
iwiki-mcp token set-create-domain --iwiki <id> --token-id <id> --enabled|--disabled
iwiki-mcp token set-domain-management --iwiki <id> --token-id <id> --domain <name> --enabled|--disabled
```

Both commands validate the tenant, active token, and existing domain before the
single intended update. `token list` adds the capability fields to the existing
default JSON output. The current `--json` flag remains a no-op compatibility
alias; no new human-readable formatter is introduced. No command prints a token
secret after initial creation.

## 8. Error Contract

- Missing, malformed, disabled, or revoked bearer: HTTP `401 authentication
  required` with the existing `WWW-Authenticate: Bearer` header.
- Missing capability detected before dispatch, a protected call with malformed
  argument envelope, or an explicit tenant override: HTTP `403 access denied`.
- Capability revoked after dispatch, cross-tenant/foreign/missing domain or
  token discovered by a transaction, occupied domain not managed by the
  caller, or self-target mutation: HTTP `200` with the existing tool-level
  `{\"error\": \"access_denied\", ...}` payload. This in-band response is
  intentionally indistinguishable across those states.
- Invalid domain syntax, invalid booleans, `write` without `read`, or empty
  grant in an otherwise authorized call: sanitized MCP validation failure with
  no database identifiers beyond caller-supplied values.
- Calling any of the three grant tools outside hosted PostgreSQL returns HTTP
  `200` with `{\"error\": \"unsupported_transport\", \"storage\":
  <git|postgres>, \"transport\": <stdio|streamable-http>, \"hint\": \"use
  hosted Streamable HTTP with PostgreSQL storage\"}`.
- PostgreSQL availability or transaction failure: existing sanitized `503
  service unavailable` or tool-level `operation failed` boundary; no partial
  rows survive.

Logs may carry stable error codes and token/domain identifiers already safe for
server operators, but never bearer secrets, token digests, DSNs, or credentials.

## 9. Files and Boundaries

- `src/iwiki_mcp/postgres/migrations.py`: additive migration v4.
- `src/iwiki_mcp/postgres/auth.py`: authority model, authentication, atomic
  provisioning, strict shared domain validation, grant operations, and
  CLI-facing recovery methods. `PostgresStore.create_domain` remains untouched.
- `src/iwiki_mcp/http.py`: fail-closed protected-tool authorization,
  selected/effective session separation, request-session persistence, and
  request-local authenticated context installation.
- `src/iwiki_mcp/server.py`: dedicated hosted-create and hosted-grant guards,
  direct locked session expansion after provisioning, tool registration, and
  transport-aware unsupported responses. The shared Git-only guard remains for
  its other five tools.
- `src/iwiki_mcp/admin.py`: provisioning/recovery flags and capability output.
- `eval/auth_grant_latency.py`: fixed-fixture legacy/new authentication SQL
  p95 comparison used as result evidence, not as a flaky unit-test gate.
- PostgreSQL auth, migration, HTTP, tool-matrix, admin, and server tests:
  focused observable coverage, including exact dict/schema assertions and
  static tool discovery behavior.
- `README.md`, `docs/README.ru.md`, and `docs/architecture.md`: public contract,
  commands, authority model, and security behavior.

No generic RBAC framework, tenant abstraction, domain lifecycle API, or hidden
in-memory authority cache is introduced.

## 10. Verification Strategy

Migration tests verify defaults, explicit management visibility, composite
foreign keys, both domain-leading indexes, cascade behavior, uniqueness, and
cross-tenant rejection. Existing rollback/failure tests must derive the next
synthetic migration version from `MIGRATIONS[-1].version + 1` and derive applied
version lists rather than hard-code `(1, 2, 3)` or fabricate a second v4.

Authentication tests verify legacy defaults, create-only token bootstrap,
combined authority loading, all three reconstruction sites, write-implies-read,
strict shared domain validation, no additional normal authentication query, and
admin listing. `AuthStore` integration tests force rollback between each
provisioning write and exercise same-caller retry plus concurrent competing
callers without weakening `PostgresStore._require_admin`.

Hosted HTTP tests cover create allowed/denied, immediate creator use, grant
list/set/revoke, invalid grant combinations, self-target rejection, foreign and
missing object indistinguishability, capability revocation between requests,
content revocation in an established session without permanent selected-scope
mutation, idempotent session restoration, request-session persistence without a
new response header, and no automatic expansion for a newly granted target.
Unit tests prove malformed protected calls cannot bypass authorization and that
both hosted context variables are installed/reset together.

Tool-matrix tests prove Git `wiki_create_domain` stays unchanged, hosted
PostgreSQL gains the four intended tools, and PostgreSQL stdio plus Git reject
the three grant tools with the transport-aware payload. Tests update the exact
registered-tool count and assert real Git rejection instead of constructing an
all-supported tautology. Tool-schema introspection proves no management-write or
tenant override field exists. Admin tests update exact token-list dictionaries,
cover `--read-domain` defaulting, recovery commands, JSON output, and secret
redaction.

`eval/auth_grant_latency.py` compares the legacy content-only SQL and new
combined authority SQL against the same seeded database. Result verification
records three 500-call warm runs and requires median p95 ratio `<= 1.25`.

Focused suites run before the full `uv run pytest -q` suite. Documentation and
version artifacts receive the required patch bump, and wiki lint must report no
new finding for the task or updated authorization documentation.

## 11. Acceptance (from intent)

The following desired outcomes are carried from the approved intent:

- A new project can initialize through HTTP MCP and create its domain as part
  of the server-side initialization flow.
- Domain creation atomically provisions the initial `read` and `write` grants
  for the project token; a failed grant operation leaves no domain behind.
- A token that lacks `tenant:domain:create` is denied domain provisioning.
- A token with `domain:grants:manage` can grant, alter, and revoke content
  `read` and `write` access only for its authorized domain.
- PostgreSQL makes content grants and domain-management grants inspectable by
  token, domain, and granted management capability.

Done when: HTTP initialization provisions a new project's domain and bootstrap
content grants; unauthorized provisioning and grant management are denied;
management grants are inspectable in PostgreSQL; and existing authorization
behavior has regression coverage.

## 12. Requirement Coverage

| Intent commitment | Design requirements |
|---|---|
| HTTP project domain initialization | R1, R2, R3, R6, R8 |
| Atomic bootstrap content grants | R2, R3 |
| Denial without create capability | R1, R9 |
| Bounded content-grant management | R4, R5, R6 |
| PostgreSQL auditability | R3, R7 |
| Existing authorization compatibility | R6, R9 |

This specification is ready for implementation planning only after
`$check-chain spec` returns `OK` and the user approves the checked source.
