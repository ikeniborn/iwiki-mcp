---
review:
  intent_hash: 99be72c70a4a20c4
  last_run: 2026-08-24
  phases:
    structure: {status: passed}
    completeness: {status: passed}
    clarity: {status: passed}
    consistency: {status: passed}
    alignment: {status: passed}
  findings: []
workflow:
  route: chain
  continuation: full
---
# Intent: codegraph-publisher-cli

**Date:** 2026-08-24
**Status:** approved

## Objective

The target capability is federated Wiki/code search, but its code-side results are not
trustworthy when the published code graph is stale or depends on a manual MCP call from a
machine that has the repository checkout. Provide a narrow publisher CLI that builds the
code graph from a local checkout and publishes it to exactly one configured target:
SQLite, PostgreSQL through the existing direct publisher, or an MCP HTTP server. This
CLI is a freshness prerequisite for federated search, not the end product.

## Desired Outcomes

- An operator or scheduled job can run one command in a local checkout and publish a
  complete code-graph snapshot without invoking an MCP tool manually: `publish_mode =
  "sqlite"` activates the local snapshot, `publish_mode = "postgres"` publishes through
  the configured PostgreSQL publisher, and `publish_mode = "mcp"` publishes through the
  configured local or remote HTTP server.
- A successful repeated run safely replaces the active snapshot, and
  `wiki_code_status` reports the newly published snapshot as fresh and ready.
- systemd and CI receive a deterministic exit status and a concise publication summary.
- Database and remote publication credentials remain absent from command-line arguments,
  logs, generated files, and the repository.

## Health Metrics

- A failed publication leaves the previous active snapshot unchanged and queryable.
- Readers never observe a partially uploaded graph as the active snapshot.
- Existing `wiki_code_status`, `wiki_code_search`, `wiki_code_context`, and local
  `wiki_code_index` contracts remain unchanged.
- Every publication batch stays within the selected publisher's limits; MCP mode also
  obeys server-advertised limits and project-domain grants.
- The existing code-graph test suite remains green.

## Strategic Context

- Interacts with: the local repository checkout and code-graph indexer, local SQLite
  snapshot storage, configured PostgreSQL storage, the local or remote HTTP publication
  API, systemd and CI schedulers, environment secrets, `wiki_code_status` /
  `wiki_code_search` / `wiki_code_context`, and the future federated Wiki/code search.
- Priority trade-off: trust and integrity first, then operational simplicity, speed, and
  cost.

## Constraints

### Steering (behavioral guidance)

- Keep the publisher a narrow one-shot CLI for systemd and CI, not a daemon.
- Reuse the existing code-graph indexer and the existing SQLite, PostgreSQL, and MCP
  publishers.
- Select exactly one target from `[code_graph].publish_mode`; do not add a CLI target
  override or fallback.
- Read project and mode configuration from `.iwiki.toml`; read remote credentials from
  the environment.
- Emit a concise result and stable exit statuses for scheduled operation.
- Keep federated search and `wiki_unified_search` outside this task.

### Hard (architectural enforcement)

- Support exactly the existing `publish_mode` values: `sqlite`, `postgres`, and `mcp`.
- PostgreSQL mode must use the existing publisher abstraction; the CLI must not issue raw
  SQL or introduce another database publication path.
- MCP mode publishes only through the existing publication API.
- Never place credentials in CLI arguments, logs, generated files, or repository files.
- Activate either target only through its successful publication finalize operation.
- MCP mode must obey project-domain grants and the batch limits advertised by the server.
- A failed run must preserve the previous active snapshot.

## Autonomy Zones

- Full autonomy (reversible, low risk): internal CLI structure, focused tests,
  documentation, and redacted logging within this approved contract.
- Guarded (log + confidence threshold): aborting an incomplete publication and selecting
  batch sizes from server-advertised limits.
- Proposal-first (needs approval): new public CLI options or exit statuses, another
  publication target, or changes to the publication API, MCP contracts, or snapshot
  format.
- No autonomy (human only): production-database changes outside the existing publisher,
  domain-grant changes, credential handling outside configuration/environment, or
  active-snapshot changes outside the finalize operation.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules

- Halt if: either supported mode cannot satisfy the outcomes without a storage, schema,
  or publication-protocol change.
- Escalate if: snapshot atomicity cannot be demonstrated or a failed run can affect the
  active snapshot.
- Done when: the same command in a local checkout publishes and activates a complete
  snapshot with each existing target — `sqlite`, `postgres`, and `mcp` — in focused
  tests; `wiki_code_status` reports the selected target as fresh and ready; a failed run
  demonstrably preserves the previous active snapshot; systemd or CI receives a stable
  exit status and concise summary; database and remote credentials are absent from
  arguments, logs, generated files, and the repository; and the existing code-graph test
  suite passes.
