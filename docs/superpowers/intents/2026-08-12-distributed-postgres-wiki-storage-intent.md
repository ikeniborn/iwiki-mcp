---
review:
  intent_hash: 79b504991ffa8b46
  last_run: 2026-08-12
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
Enable several machines and agents to work with one shared iwiki knowledge base without a local clone, local wiki base, or Git synchronization. The change is needed now to make iwiki distributed through a central runtime service rather than through per-machine files.

## Desired Outcomes
- An HTTP MCP client can read and write an authoritative shared wiki stored in PostgreSQL.
- A second independent HTTP MCP client observes a committed page update without cloning, pulling, or syncing a local base.
- Search and link-graph queries return results from the shared database without requiring local Markdown, `index.jsonl`, or SQLite files at runtime.
- A concurrent update to the same page returns a documented, deterministic optimistic-lock conflict rather than losing either client's data silently.

## Health Metrics
- Existing MCP tool contracts retain their documented request and response semantics unless an approved compatibility decision changes them.
- A page, its chunks/embeddings, and its outgoing links are committed atomically.
- Every migrated acceptance fixture returns its expected page and heading from search and its expected link-graph neighbours.
- Concurrent writes never produce a partially indexed or partially linked committed page.

## Strategic Context
- Interacts with: Codex and Claude MCP clients; the iwiki HTTP server; managed PostgreSQL with pgvector; the current Git base during migration and backup.
- Priority trade-off: trust first, speed second, cost third.

## Constraints
### Steering (behavioral guidance)
- Deliver the smallest centralised solution that removes local wiki files from the runtime path.
- Keep one authoritative source of truth; do not introduce CRDT or peer-to-peer replication.
- Keep Git only as an export or backup mechanism after cutover.

### Hard (architectural enforcement)
- PostgreSQL stores Markdown page bodies, chunks/embeddings, and graph links.
- Each page mutation updates the page, chunks/embeddings, and links in one database transaction.
- Concurrent page writes use optimistic locking and report conflicts deterministically.
- Secrets are supplied only through the environment and are never committed.
- `IWIKI_BASE_DIR`, local SQLite, and Git must not be required by the production runtime path.

## Autonomy Zones
- Full autonomy (reversible, low risk): internal table schema, migration utility, tests, and documentation.
- Guarded (log + confidence threshold): select a managed PostgreSQL provider and database index parameters; record the choice in documentation.
- Proposal-first (needs approval): change a public MCP contract, define compatibility or cutover strategy, or set Git-backup retention.
- No autonomy (human only): create a production database, grant access, or delete the legacy Git base.

> These zones OVERRIDE subagent-driven-development's "continuous execution, don't pause" default. Any task touching proposal-first / no-go decisions is marked HUMAN CHECKPOINT in the plan.

## Stop Rules
- Halt if a safe migration and rollback path cannot be demonstrated, or a page mutation cannot be made atomic.
- Escalate if a public-contract compatibility conflict appears, or security or persisted data is at risk.
- Done when two independent HTTP MCP clients read and write one PostgreSQL-backed wiki; concurrent-write conflicts are deterministic; search and link-graph queries work; and the runtime needs no Git, local Markdown, `index.jsonl`, or SQLite files.
