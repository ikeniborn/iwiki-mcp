---
review:
  intent_hash: 47c50ea5a394309a
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
code graph from a local checkout and publishes it through the existing remote publication
protocol. This CLI is a freshness prerequisite for federated search, not the end product.

## Desired Outcomes

- An operator or scheduled job can run one command in a local checkout and publish a
  complete code-graph snapshot without invoking an MCP tool manually.
- A successful repeated run safely replaces the active snapshot, and
  `wiki_code_status` reports the newly published snapshot as fresh and ready.
- systemd and CI receive a deterministic exit status and a concise publication summary.
- Publication credentials remain absent from command-line arguments, logs, generated
  files, and the repository.

## Health Metrics

- A failed publication leaves the previous active snapshot unchanged and queryable.
- Readers never observe a partially uploaded graph as the active snapshot.
- Existing `wiki_code_status`, `wiki_code_search`, `wiki_code_context`, and local
  `wiki_code_index` contracts remain unchanged.
- Every publication batch stays within the server-advertised limits and project-domain
  grants.
- The existing code-graph test suite remains green.

## Strategic Context

- Interacts with: the local repository checkout and code-graph indexer, the remote HTTP
  publication API, PostgreSQL snapshot storage, systemd and CI schedulers, environment
  secrets, `wiki_code_status` / `wiki_code_search` / `wiki_code_context`, and the future
  federated Wiki/code search.
- Priority trade-off: trust and integrity first, then operational simplicity, speed, and
  cost.

## Constraints

### Steering (behavioral guidance)

- Keep the publisher a narrow one-shot CLI for systemd and CI, not a daemon.
- Reuse the existing code-graph indexer and remote publication protocol.
- Read configuration and credentials from the environment.
- Emit a concise result and stable exit statuses for scheduled operation.
- Keep federated search and `wiki_unified_search` outside this task.

### Hard (architectural enforcement)

- Publish only through the existing publication API; never write directly to PostgreSQL.
- Never place credentials in CLI arguments, logs, generated files, or repository files.
- Activate a snapshot only through a successful publication finalize operation.
- Obey project-domain grants and the batch limits advertised by the server.
- A failed run must preserve the previous active snapshot.

## Autonomy Zones

- Full autonomy (reversible, low risk): internal CLI structure, focused tests,
  documentation, and redacted logging within this approved contract.
- Guarded (log + confidence threshold): aborting an incomplete publication and selecting
  batch sizes from server-advertised limits.
- Proposal-first (needs approval): new public CLI options or exit statuses, or changes to
  the publication API, MCP contracts, or snapshot format.
- No autonomy (human only): direct production-database changes, domain-grant changes,
  credential handling outside the environment, or active-snapshot changes outside the
  finalize operation.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules

- Halt if: the existing publication API cannot satisfy the outcomes without a storage or
  schema change.
- Escalate if: snapshot atomicity cannot be demonstrated or a failed run can affect the
  active snapshot.
- Done when: one command in a local checkout publishes a complete snapshot;
  `wiki_code_status` reports it as fresh and ready; a failed run demonstrably preserves
  the previous active snapshot; systemd or CI receives a stable exit status and concise
  summary; credentials are absent from arguments, logs, generated files, and the
  repository; and the existing code-graph test suite passes.
