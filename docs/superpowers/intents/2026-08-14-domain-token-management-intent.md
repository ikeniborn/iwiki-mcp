---
workflow:
  route: chain
  continuation: full
review:
  intent_hash: 5abfc7209ba1336e
  last_run: 2026-08-14
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings: []
---

# Intent: domain-token-management

**Date:** 2026-08-14
**Status:** approved

## Objective

Enable HTTP MCP to provision a domain while initializing a new project, so
project initialization can create the domain alongside `.iwiki.toml` and
`.iwikiignore`. Authorization must allow controlled domain provisioning and
management of content access grants without allowing privilege escalation.

## Desired Outcomes

- A new project can initialize through HTTP MCP and create its domain as part
  of the server-side initialization flow.
- Domain creation atomically provisions the initial `read` and `write` grants
  for the project token; a failed grant operation leaves no domain behind.
- A token that lacks `tenant:domain:create` is denied domain provisioning.
- A token with `domain:grants:manage` can grant, alter, and revoke content
  `read` and `write` access only for its authorized domain.
- PostgreSQL makes content grants and domain-management grants inspectable by
  token, domain, and granted management capability.

## Health Metrics

- Existing HTTP tokens and `token_domain_grants` preserve their behavior.
- A token without a new administrative grant receives no new capability.
- Authorization failures do not reveal another tenant's domain or grants.
- Domain provisioning and initial grant issuance are atomic.
- Normal MCP read and write operation latency does not materially degrade.

## Strategic Context

- Interacts with: HTTP MCP server, PostgreSQL token and grant storage,
  `wiki_create_domain`, project initialization, `.iwiki.toml`, `.iwikiignore`,
  and database auditors.
- Priority trade-off: tenant isolation and authorization trust over provisioning
  speed and convenience.

## Constraints

### Steering

- Keep authorization data inspectable in PostgreSQL for database auditing.
- Match current HTTP MCP authorization and error-response conventions.
- Keep the first release to provisioning and content-grant management.

### Hard

- PostgreSQL is the sole authorization source; do not add an in-memory
  authority list.
- The only new administrative capabilities are `tenant:domain:create` and
  `domain:grants:manage`; content `read` and `write` remain domain grants.
- `domain:grants:manage` includes inspection of grants; do not introduce a
  separate `domain:grants:read` capability in this release.
- `tenant:domain:create` is limited to an authorized tenant or namespace.
- No token may grant itself, or another token, a capability beyond its own
  authorized domain-management scope.
- Each HTTP admin operation checks authorization before changing state.
- Domain creation and bootstrap grant issuance are one transaction; no partial
  domain or partial grant state is allowed.
- Do not add domain deletion, archival, ownership transfer, or metadata
  management in this release.
- Every repository change includes the required patch-version bump.

## Autonomy Zones

- Full autonomy: implementation, migration, tests, and documentation within
  the approved capabilities and constraints.
- Guarded: exact PostgreSQL schema and MCP API shapes, recorded in the design
  specification with their authorization rationale.
- Proposal-first: any administrative capability beyond the two approved ones,
  tenant-model changes, or public HTTP compatibility changes.
- No autonomy: domain deletion, ownership transfer, or grants outside an
  authorized tenant.

## Stop Rules

- Halt if atomic provisioning or tenant isolation cannot be guaranteed.
- Escalate if `token_domain_grants` conflicts with the required schema or a
  privilege-escalation path is found.
- Done when: HTTP initialization provisions a new project's domain and bootstrap
  content grants; unauthorized provisioning and grant management are denied;
  management grants are inspectable in PostgreSQL; and existing authorization
  behavior has regression coverage.
