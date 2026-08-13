---
review:
  intent_hash: 496d506bfb0899de
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
Support two explicit iwiki storage modes: preserve the current local Git-backed knowledge base for local workflows, and add a remote PostgreSQL-backed knowledge base for several machines and agents. The remote mode removes the need for a local clone and Git synchronization without removing the existing local mode.

## Desired Outcomes
- A client selects exactly one storage type: `local` or `remote`.
- In `local` mode, the current Git-backed Markdown, portable index, and local graph workflow remains available through a configured Git repository directory.
- An HTTP MCP client can read and write an authoritative shared wiki stored in PostgreSQL.
- A second independent HTTP MCP client observes a committed page update without cloning, pulling, or syncing a local base.
- In `remote` mode, search and link-graph queries return results from the shared database without requiring local Markdown, `index.jsonl`, or SQLite files at runtime.
- A concurrent update to the same page returns a documented, deterministic optimistic-lock conflict rather than losing either client's data silently.
- In `remote` mode, connection configuration identifies the PostgreSQL address, port, database, user, and runtime password secret.

## Health Metrics
- Existing MCP tool contracts retain their documented request and response semantics unless an approved compatibility decision changes them.
- Existing local Git-backed acceptance scenarios remain green when storage type is `local`.
- In `remote` mode, a page, its chunks/embeddings, and its outgoing links are committed atomically.
- Every migrated acceptance fixture returns its expected page and heading from search and its expected link-graph neighbours.
- In `remote` mode, concurrent writes never produce a partially indexed or partially linked committed page.

## Strategic Context
- Interacts with: Codex and Claude MCP clients; local stdio and remote HTTP iwiki servers; managed PostgreSQL with pgvector; the current Git base as the authoritative local store and a migration/export source.
- Priority trade-off: trust first, speed second, cost third.

## Constraints
### Steering (behavioral guidance)
- Deliver the smallest dual-mode solution: preserve the current local implementation and add one remote implementation.
- Within one running server configuration, select one authoritative storage mode; do not synchronize local and remote stores automatically.
- Do not introduce CRDT or peer-to-peer replication.

### Hard (architectural enforcement)
- Storage type is required and accepts only `local` or `remote`.
- `local` requires a configured directory containing the Git-backed wiki base and preserves current Git synchronization behavior.
- `remote` requires PostgreSQL address, port, database name, and user parameters; its password is supplied as a runtime secret and is never committed to repository configuration.
- In `remote` mode, PostgreSQL stores Markdown page bodies, chunks/embeddings, and graph links.
- In `remote` mode, each page mutation updates the page, chunks/embeddings, and links in one database transaction.
- In `remote` mode, concurrent page writes use optimistic locking and report conflicts deterministically.
- Secrets are supplied only through the environment and are never committed.
- `remote` mode must not require `IWIKI_BASE_DIR`, local Markdown, `index.jsonl`, SQLite, or Git in its runtime path.
- `local` mode must not require PostgreSQL or remote HTTP access in its runtime path.

## Autonomy Zones
- Full autonomy (reversible, low risk): internal table schema, migration utility, tests, and documentation.
- Guarded (log + confidence threshold): select a managed PostgreSQL provider and database index parameters; record the choice in documentation.
- Proposal-first (needs approval): change a public MCP contract, define compatibility or cutover strategy, or set Git-backup retention.
- No autonomy (human only): create a production database, grant access, or delete the legacy Git base.

> These zones OVERRIDE subagent-driven-development's "continuous execution, don't pause" default. Any task touching proposal-first / no-go decisions is marked HUMAN CHECKPOINT in the plan.

## Stop Rules
- Halt if a safe migration and rollback path cannot be demonstrated, or a page mutation cannot be made atomic.
- Escalate if a public-contract compatibility conflict appears, or security or persisted data is at risk.
- Done when local mode passes the current Git-backed acceptance scenarios using its configured repository directory; two independent HTTP MCP clients read and write one PostgreSQL-backed remote wiki; remote concurrent-write conflicts are deterministic; both modes provide search and link-graph queries; and neither mode requires the other mode's storage dependencies at runtime.
