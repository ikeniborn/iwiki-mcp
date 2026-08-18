---
review:
  intent_hash: c771fa25fff90bf7
  last_run: 2026-08-19
  phases:
    structure:
      status: passed
    completeness:
      status: passed
    clarity:
      status: passed
    consistency:
      status: passed
    alignment:
      status: passed
  findings: []
---
# Intent: codegraph-publish-client-batch-limits

**Date:** 2026-08-19
**Status:** approved

## Objective

Hosted code-graph publish (`wiki_code_publish_begin`/`_batch`/`_finalize`, `publish_mode
= "mcp"`) currently sizes outgoing batches using the CLIENT's local `.iwiki.toml`
`[code_graph]` values (`max_batch_rows`, `max_batch_bytes`), via
`server.py::_publish_local_snapshot`'s unconditional call to `iter_snapshot_batches(rows,
max_rows=config.max_batch_rows, max_bytes=config.max_batch_bytes)` — used identically for
both the local `sqlite` target and the remote `mcp` target. The hosted server independently
enforces its own admin-only `HostedCodeGraphConfig.max_batch_rows`/`max_batch_bytes`
(default 1000 rows / 1MB, hard ceiling 5000 rows / 5MB) in `_HostedPublication.
publish_from_mapping`, silently rejecting any batch the client already built larger than
that with a generic `{"error": "invalid_batch", "hint": "send batches that match the
declared header"}` — no discovery mechanism, no diagnostic detail. Discovered via a real
deterministic `wiki_code_index` publish failure against project `aioperator` (4001 symbols
packed into one client-side batch under its local `max_batch_rows = 5000`, rejected by the
remote's unconfigured 1000-row default; 3 identical failures traced end-to-end via SSH
access to the remote host's Docker container and PostgreSQL `code_graph_publication_
sessions`/`code_graph_batches` tables, confirming 0 batches persisted and the session
aborted on the very first oversized batch).

Decision made during intent capture: when the code-graph target is remote (`publish_mode
= "mcp"`), the client must not influence transport-mechanical batch sizing at all — the
server is the sole authority for `max_batch_rows`/`max_batch_bytes` in that mode. Local
`.iwiki.toml` values for these two fields keep governing `publish_mode = "sqlite"`
unchanged. (Full audit of all 6 fields shared between `CodeGraphConfig` and
`HostedCodeGraphConfig` — `max_snapshot_age_seconds`, `max_batch_rows`, `max_batch_bytes`,
`publication_session_ttl_seconds`, `staging_retention_seconds`, `staging_cleanup_limit` —
found only `max_batch_rows`/`max_batch_bytes` actually leak the client's local value into
`mcp`-target behavior via `_publish_local_snapshot`; the other four are already read
exclusively inside `sqlite_adapter.py`'s `SqliteSnapshotPublisher`, never consulted on the
`mcp` path, and need no change.)

## Desired Outcomes

- A client whose `.iwiki.toml` sets `max_batch_rows`/`max_batch_bytes` above the remote's
  actual limit (e.g. `aioperator`'s `max_batch_rows = 5000` vs. the remote's unconfigured
  1000) publishes successfully via `wiki_code_index` without any manual `.iwiki.toml` edit
  — the client discovers and honors the server's real limit automatically.
- `publish_mode = "sqlite"` (pure local target) behavior is unchanged: local
  `max_batch_rows`/`max_batch_bytes` continue to size local batches exactly as before.
  This intent does not touch that path.
- If a client somehow still sends an over-limit batch (stale client, bug, race), the
  server's rejection names the actual limit and what was received (e.g. `limit: 1000,
  received: 4001`) instead of the current generic `invalid_batch` with no numbers.

## Health Metrics

- `publish_mode = "sqlite"` behavior and its existing tests stay green unmodified.
- The server's admin hard ceiling (`max_batch_rows` 1–5000, `max_batch_bytes`
  1–5,000,000, enforced in `HostedCodeGraphConfig.__post_init__`) is never bypassable by
  a client in any scenario — no client-declared value may ever cause the server to accept
  a batch larger than its own configured (or default) cap.
- The publication protocol's closed error-code/field sets
  (`concept/code-graph-publication`: "adapter errors cover invalid configuration, remote
  MCP failure, and unavailable source... must preserve these safe sets and must not add
  source, secrets, paths, SQL, or cross-scope identifiers") stay closed — new diagnostic
  detail (limit/received counts) is safe numeric metadata, not a new error-code class.

## Strategic Context

- Interacts with: `src/iwiki_mcp/server.py` (`_publish_local_snapshot`,
  `wiki_code_publish_begin`, `_HostedPublication`), `src/iwiki_mcp/codegraph/
  mcp_adapter.py` (`McpSnapshotPublisher`, `RemoteMcpTransport`),
  `src/iwiki_mcp/codegraph/publication.py` (`SnapshotHeader`, `PublicationSession`,
  publication/adapter/readiness error-code tuples), `src/iwiki_mcp/postgres/config.py`
  (`HostedCodeGraphConfig`). Affects every existing project bound with `publish_mode =
  "mcp"` against a hosted iwiki server (e.g. `aioperator`), and the hosted server
  deployment itself (a shared, multi-tenant resource).
- Priority trade-off: **trust** — the server must remain the sole, unambiguous authority
  for remote-mode batch sizing; no negotiation model that could let any client push
  transport limits toward its own preference.

## Constraints

### Steering (behavioral guidance)
- Prefer the smallest change that makes the client discover-and-obey the server's real
  limit; do not restructure the broader publication protocol beyond what this fixes.
- Keep the fix scoped to `max_batch_rows`/`max_batch_bytes` only — the other 4
  shared-name fields are confirmed already correctly scoped to `sqlite`-only and must not
  be touched without separate justification.

### Hard (architectural enforcement)
- `publish_mode = "sqlite"` batch sizing is out of scope — client-controlled behavior
  there is unchanged.
- The server's admin-configured (or default) `HostedCodeGraphConfig.max_batch_rows`/
  `max_batch_bytes`, and its hard ceiling (1–5000 rows, 1–5,000,000 bytes), remain the
  sole source of truth for `publish_mode = "mcp"` batch sizing — never overridable by a
  client-declared value.
- The publication error-code/field sets stay closed per `concept/code-graph-publication`
  — no source paths, secrets, SQL, or cross-scope identifiers added to any error
  response; only safe numeric limit/received counts.
- A public-contract change to `wiki_code_publish_begin`'s request/response shape (e.g. a
  new field for the server to report its limits) must remain backward compatible: an
  older client that does not read the new field must not break.

## Autonomy Zones
- Full autonomy (reversible, low risk): adding diagnostic numeric fields (limit/received)
  to existing error dicts; adding a client-side discovery call/step for the server's
  real batch limits.
- Guarded (log + confidence threshold): changing `_publish_local_snapshot`'s batch-sizing
  source to be target-aware (server-discovered limits for `mcp`, client config for
  `sqlite`).
- Proposal-first (needs approval): any change to `wiki_code_publish_begin`'s public
  request/response contract (e.g. a new response field carrying server limits).
- No autonomy (human only): changing the admin hard ceiling (1–5000 rows, 1–5,000,000
  bytes) or weakening the server-side enforcement in any way.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules
- Halt if: a proposed fix would let a client-declared value exceed the server's admin
  hard ceiling under any code path.
- Escalate if: the backward-compatibility requirement for `wiki_code_publish_begin`'s
  contract cannot be satisfied without breaking an existing deployed client.
- Done when: `aioperator` (or an equivalent project with a `.iwiki.toml` `max_batch_rows`
  above the remote's real limit) runs `wiki_code_index` and reaches `state: ready` on the
  remote via `publish_mode = "mcp"` with zero manual `.iwiki.toml` edits, AND a
  deliberately oversized client batch produces an error response that states the actual
  limit and the received count, AND `publish_mode = "sqlite"`'s existing test suite
  passes unmodified.
