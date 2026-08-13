---
review:
  intent_hash: 272b8bd4f8746aa7
  last_run: 2026-08-13
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings: []
workflow:
  route: chain
  continuation: full
---

# Intent: distributed-postgres-wiki-storage

**Date:** 2026-08-12
**Status:** approved

## Objective
Preserve the current autonomous Git-backed iwiki workflow and add PostgreSQL-backed shared storage accessible either from a local stdio server or a hosted Streamable HTTP MCP endpoint. Hosted access removes local clones, Git synchronization, and database credentials from developer and agent machines without removing local modes.

## Desired Outcomes
- A client selects one storage backend: `git` or `postgres`; an absent storage type preserves the existing Git-backed behavior.
- A local stdio server supports `git` storage through a configured Git repository directory, with no hosted-server or PostgreSQL dependency.
- A local stdio server supports `postgres` storage using configured address, port, target database name, user, TLS settings, and a runtime password secret.
- The hosted endpoint `https://iwiki.ikeniborn.ru/mcp` is a Streamable HTTP MCP server backed by PostgreSQL; `HTTP + git` is rejected at startup as an invalid configuration.
- Two independent authenticated HTTP MCP clients observe a committed page update without cloning, pulling, syncing a local base, or holding PostgreSQL credentials.
- Each personal Bearer token has `read_domains` and `write_domains`; `wiki_bind` can only narrow those grants.
- Search and link-graph queries return results from PostgreSQL without requiring local Markdown, `index.jsonl`, SQLite, or Git at runtime.
- A concurrent update to the same page returns a documented, deterministic optimistic-lock conflict rather than losing either client's data silently.
- A PostgreSQL-backed server starts against an empty selected database by creating its required schema idempotently.

## Health Metrics
- Existing stdio MCP tool contracts retain their documented request and response semantics unless an approved compatibility decision changes them.
- Existing local Git-backed acceptance scenarios remain green when the storage type is absent or `git`.
- In PostgreSQL mode, a page, its chunks/embeddings, and its outgoing links are committed atomically.
- Every migrated acceptance fixture returns its expected page and heading from search and its expected link-graph neighbours.
- In PostgreSQL mode, concurrent writes never produce a partially indexed or partially linked committed page.
- A revoked or domain-disallowed Bearer token cannot access a hosted MCP tool or resource outside its grants.
- Schema initialization does not alter objects outside the iwiki-owned schema in the selected database.

## Strategic Context
- Interacts with: Codex and Claude MCP clients; local stdio and hosted Streamable HTTP iwiki servers; managed PostgreSQL with pgvector; the current Git base as the authoritative local store and a migration/export source; developers and agents using personal Bearer tokens.
- Priority trade-off: trust first, speed second, cost third.

## Constraints
### Steering (behavioral guidance)
- Deliver the smallest storage and transport matrix: local stdio supports Git or PostgreSQL; hosted Streamable HTTP supports PostgreSQL only.
- Within one running server configuration, select one authoritative storage backend; do not synchronize Git and PostgreSQL stores automatically.
- Do not introduce CRDT or peer-to-peer replication.
- Keep token administration in the server package CLI; do not introduce OAuth or external identity-provider integration in this release.

### Hard (architectural enforcement)
- Storage accepts only `git` or `postgres`; an absent type resolves to `git` for backward compatibility.
- `git` requires a configured directory containing the Git-backed wiki base and preserves current Git synchronization behavior.
- `postgres` requires PostgreSQL address, port, target database name, user, and TLS settings; its password is supplied as a runtime secret and is never committed to repository configuration.
- PostgreSQL stores Markdown page bodies, chunks/embeddings, graph links, token hashes, token domain grants, and token lifecycle metadata.
- In PostgreSQL mode, each page mutation updates the page, chunks/embeddings, and links in one database transaction.
- In PostgreSQL mode, concurrent page writes use optimistic locking and report conflicts deterministically.
- Secrets are supplied only through the environment and are never committed.
- PostgreSQL mode must not require `IWIKI_BASE_DIR`, local Markdown, `index.jsonl`, SQLite, or Git in its runtime path.
- PostgreSQL startup initializes only the iwiki-owned schema when it is absent in the configured target database; it never creates a database or modifies non-iwiki objects.
- Local stdio with Git must not require PostgreSQL or HTTP access in its runtime path.
- Streamable HTTP requires PostgreSQL and fails startup validation if paired with Git.
- Hosted HTTP listens at `https://iwiki.ikeniborn.ru/mcp`, requires TLS, validates Bearer tokens, and does not expose PostgreSQL credentials to MCP clients.
- Token plaintext is generated by an administrative CLI, displayed once, never persisted, and stored only as a cryptographic hash with an owner and `read_domains`/`write_domains` grants.
- `wiki_bind` cannot expand a Bearer token's domain grants.

## Autonomy Zones
- Full autonomy (reversible, low risk): internal table schema, migration utility, token hash format, tests, and documentation.
- Guarded (log + confidence threshold): select a managed PostgreSQL provider and database index parameters; record the choice in documentation.
- Proposal-first (needs approval): change a public MCP contract, define compatibility or cutover strategy, set Git-backup retention, or alter token grants for a real user.
- No autonomy (human only): create a production database, configure `iwiki.ikeniborn.ru` DNS/TLS, grant production access, issue production tokens, or delete the legacy Git base.

> These zones OVERRIDE subagent-driven-development's "continuous execution, don't pause" default. Any task touching proposal-first / no-go decisions is marked HUMAN CHECKPOINT in the plan.

## Stop Rules
- Halt if a safe migration and rollback path cannot be demonstrated, or a page mutation cannot be made atomic.
- Escalate if a public-contract compatibility conflict appears, or security or persisted data is at risk.
- Done when local stdio passes the current Git-backed acceptance scenarios using an absent or explicit `git` type and passes PostgreSQL acceptance scenarios against a configured target database using explicit `postgres`; an empty target database receives only the idempotent iwiki schema initialization; two independent Bearer-authenticated HTTP MCP clients read and write one PostgreSQL-backed hosted wiki at the configured endpoint; HTTP plus Git is rejected deterministically; token domain grants and revocation are enforced; PostgreSQL concurrent-write conflicts are deterministic; every supported mode provides search and link-graph queries; and no supported mode requires another mode's storage dependencies at runtime.
