---
review:
  spec_hash: c3e9da15f67386e5
  last_run: 2026-08-14
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings: []
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

## 2. Requirements

### R1. Tenant-scoped domain creation

An authenticated hosted token with `tenant:domain:create` can call the existing
`wiki_create_domain(name)` tool. The capability applies only to the token's
existing `iwiki_id`; callers cannot provide or override an `iwiki_id`.

Acceptance criterion: a capable token creates a valid domain in its own tenant,
while a token without the capability receives the same sanitized `403 access
denied` response used by other hosted authorization failures.

### R2. Atomic bootstrap authority

The domain row, caller content grant with `can_read=true` and `can_write=true`,
and caller management grant with `can_manage_grants=true` are committed in one
PostgreSQL transaction. The target token is always the current bearer token.

Acceptance criterion: injected failure at any write rolls back every row, and a
successful call leaves all three rows visible through PostgreSQL queries.

### R3. Idempotent creation

A retry by the same caller succeeds with `created=false` when the domain already
exists and the caller already owns its read, write, and management grants. An
existing domain not managed by the caller returns sanitized denial without
revealing its owner or grants.

Acceptance criterion: a lost-response retry returns the same effective scope
without adding or changing rows; a competing caller receives no grant.

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
and attempts to smuggle management or tenant identifiers are rejected before a
database mutation.

### R6. Immediate revocation

Every hosted request authenticates against current database grants. A stored
session binding is intersected with the freshly authenticated content scope
before authorization and dispatch. Revoked access therefore disappears on the
next request. Newly granted access does not expand an existing target session;
the target starts a new MCP session to use it.

The successful creator call is the only bounded expansion of its current
session: after commit, the new domain is added to read/write and becomes
primary.

Acceptance criterion: a target's established session loses revoked access on
its next request, does not gain newly granted access, and the creator can use
the new domain immediately in the creation session.

### R7. Database and CLI auditability

Token listing exposes `can_create_domain`, `managed_domains`, `read_domains`,
and `write_domains` without returning bearer secrets. The admin CLI can grant
or revoke tenant creation authority and per-domain management authority for
provisioning and recovery.

Acceptance criterion: SQL and `token list --json` identify each token's content
and management authority, and CLI recovery changes only the named tenant,
token, domain, and capability.

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
without adding a database round-trip to the normal hosted request path.

Acceptance criterion: legacy token tests remain green, existing rows
authenticate with both new capabilities disabled, and query-level tests show
the token lookup plus combined domain-authority lookup retain the existing
authentication query count.

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
```

The explicit boolean keeps management authority visible in SQL output. Only a
`true` row is valid; revocation deletes the row. Composite foreign keys preserve
tenant isolation and cascade token/domain deletion.

`token_domain_grants` remains content-only and is not rewritten by the
migration. All existing tokens receive `can_create_domain=false`, and the new
management table starts empty.

## 4. Authentication and Authorization

`AuthContext` gains immutable `can_create_domain: bool` and
`managed_domains: tuple[str, ...]` fields plus `can_manage_grants(domain)` and
require helpers. Existing read/write methods retain their semantics.

Authentication reads `can_create_domain` in the existing token-row query. Its
existing domain query becomes a combined indexed query over the tenant's domain
rows with left joins to the caller's content and management grants. It returns
any domain for which either grant exists, so a recovery manager need not also
hold content access. No extra query is added.

The HTTP middleware installs the full authenticated context in a request-local
context variable used by hosted tool dispatch. The persisted session binding
continues to store only narrowed content scope. On every request the middleware
intersects that scope with fresh `read_domains` and `write_domains`; it never
restores a domain removed by the session's earlier explicit narrowing.

Middleware checks tool capabilities before dispatch. Each mutating store method
also locks and rechecks the active token, tenant, and capability inside its
write transaction. This second check closes the interval between bearer
authentication and mutation if an administrator revokes authority concurrently.

## 5. Domain Provisioning Flow

The PostgreSQL unsupported guard for `wiki_create_domain` becomes transport
aware: Git behavior remains unchanged, PostgreSQL stdio remains unsupported,
and hosted PostgreSQL dispatches to the authenticated provisioning method.

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

Only after commit does the server update `_HostedBindingState` by adding the
domain to read and write scope and selecting it as primary. The response is:

```json
{
  "created": true,
  "domain": "new-project",
  "read": ["new-project"],
  "write": ["new-project"],
  "primary": "new-project"
}
```

The arrays contain the complete effective session scope, not only the new
domain. An idempotent retry returns the same shape with `created=false`.

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
content scope.

## 7. Admin CLI

`token create` adds optional `--can-create-domain`. `--read-domain` is no longer
parser-required, but service validation requires at least one read domain unless
the creation capability is enabled.

Explicit recovery commands set the two management capability types:

```text
iwiki-mcp token set-create-domain --iwiki <id> --token-id <id> --enabled|--disabled
iwiki-mcp token set-domain-management --iwiki <id> --token-id <id> --domain <name> --enabled|--disabled
```

Both commands validate the tenant, active token, and existing domain before the
single intended update. `token list` adds the capability fields to JSON and
human-readable output. No command prints a token secret after initial creation.

## 8. Error Contract

- Missing, malformed, disabled, or revoked bearer: HTTP `401 authentication
  required` with the existing `WWW-Authenticate: Bearer` header.
- Missing capability, cross-tenant identifier, foreign domain, foreign token,
  or occupied domain not managed by the caller: HTTP `403 access denied`.
- Invalid domain syntax, invalid booleans, `write` without `read`, empty grant,
  or self-target mutation: sanitized MCP validation failure with no database
  identifiers beyond caller-supplied values.
- PostgreSQL availability or transaction failure: existing sanitized `503
  service unavailable` or tool-level `operation failed` boundary; no partial
  rows survive.

Logs may carry stable error codes and token/domain identifiers already safe for
server operators, but never bearer secrets, token digests, DSNs, or credentials.

## 9. Files and Boundaries

- `src/iwiki_mcp/postgres/migrations.py`: additive migration v4.
- `src/iwiki_mcp/postgres/auth.py`: authority model, authentication, atomic
  provisioning, grant operations, CLI-facing recovery methods.
- `src/iwiki_mcp/http.py`: tool authorization, fresh-scope intersection, and
  request-local authenticated context installation.
- `src/iwiki_mcp/server.py`: hosted tool routing, session expansion after
  provisioning, tool registration, and unsupported-storage responses.
- `src/iwiki_mcp/admin.py`: provisioning/recovery flags and capability output.
- PostgreSQL auth, migration, HTTP, tool-matrix, admin, and store tests: focused
  observable coverage.
- `README.md`, `docs/README.ru.md`, and `docs/architecture.md`: public contract,
  commands, authority model, and security behavior.

No generic RBAC framework, tenant abstraction, domain lifecycle API, or hidden
in-memory authority cache is introduced.

## 10. Verification Strategy

Migration tests verify defaults, explicit management visibility, composite
foreign keys, cascade behavior, uniqueness, and cross-tenant rejection.

Authentication tests verify legacy defaults, create-only token bootstrap,
combined authority loading, no additional normal authentication query, and
admin listing. Store integration tests force rollback between each provisioning
write and exercise same-caller retry plus concurrent competing callers.

Hosted HTTP tests cover create allowed/denied, immediate creator use, grant
list/set/revoke, invalid grant combinations, self-target rejection, foreign and
missing object indistinguishability, capability revocation between requests,
content revocation in an established session, and no automatic expansion for a
newly granted target.

Tool-matrix tests prove Git `wiki_create_domain` stays unchanged, hosted
PostgreSQL gains the four intended tools, and PostgreSQL stdio plus Git reject
the three grant tools. Admin tests cover parser, service validation, recovery
commands, JSON output, and secret redaction.

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
