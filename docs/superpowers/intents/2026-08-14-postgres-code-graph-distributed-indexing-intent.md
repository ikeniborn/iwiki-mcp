---
review:
  intent_hash: a626f91a91ecfa50
  last_run: 2026-08-14
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
# Intent: postgres-code-graph-distributed-indexing

**Date:** 2026-08-14
**Status:** approved

## Objective

Allow a local tool with a repository checkout to build a Python code-graph snapshot and persist it either locally in SQLite or in PostgreSQL. A remote MCP server without a checkout must query a published PostgreSQL snapshot.

## Desired Outcomes

- A local indexer builds a graph from a local checkout and publishes it to configured SQLite, PostgreSQL, or an authenticated remote MCP publication endpoint.
- A PostgreSQL-backed remote MCP serves `wiki_code_search` and `wiki_code_context` from a ready published snapshot without access to the source checkout.
- Local consumers can use local SQLite, PostgreSQL, or a remote MCP endpoint according to explicit configuration.
- Missing, stale, or non-ready snapshots return a clear safe response and never return partial graph data.

## Health Metrics

- The first release supports typical Python projects with up to 20,000 indexed files.
- Snapshot freshness is configurable; the initial default is 24 hours.
- Existing Git and SQLite code-graph behavior and ordinary PostgreSQL wiki reads, writes, and search do not regress.
- Query limits remain bounded; a remote query cannot load an entire graph unintentionally.

## Strategic Context

- Interacts with: local indexer/CLI, `wiki_code_*` MCP tools, PostgreSQL storage and migrations, hosted MCP authentication, and local/remote consumers.
- Priority trade-off: trust over speed and cost.

## Constraints

### Steering (behavioral guidance)

- Reuse the current Python extractor and query contracts where possible.
- Keep SQLite as a self-contained local mode.
- Make direct PostgreSQL publication and authenticated remote MCP publication explicit configured modes.

### Hard (architectural enforcement)

- Do not upload or persist source text automatically; PostgreSQL snapshots contain graph entities and relations only.
- Publish each snapshot atomically: readers observe a previous ready snapshot or a new ready snapshot, never a partial snapshot.
- Scope snapshots by tenant and domain. Direct PostgreSQL credentials must be constrained to the same scope as a domain write-token.
- Preserve Git/SQLite compatibility and Python-only code-graph support for the first release.
- Do not silently fall back between direct PostgreSQL and remote MCP publication modes.

## Autonomy Zones

- Full autonomy (reversible, low risk): tests, documentation, temporary fixtures, and internal refactoring.
- Guarded (log + confidence threshold): implementation of approved internal behavior with focused checks.
- Proposal-first (needs approval): final MCP tool names, PostgreSQL migration shape, direct-database credential/configuration format, and security boundaries.
- No autonomy (human only): real production credentials, production publication, and destructive migrations.

## Stop Rules

- Halt if: atomic snapshot visibility or tenant/domain isolation cannot be guaranteed.
- Escalate if: source text could leak, a stale snapshot lacks correct diagnostics, or direct credentials cannot be safely scoped.
- Done when: local publication and remote PostgreSQL querying work against the same ready snapshot; SQLite compatibility tests pass; configured freshness is visible in observed responses; and no query exposes source text or partial graph data.
