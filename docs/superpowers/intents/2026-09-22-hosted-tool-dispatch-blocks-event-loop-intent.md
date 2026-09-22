---
review:
  intent_hash: 477b97089342a0b1
  last_run: 2026-09-22
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings:
    - id: F-001
      phase: structure
      severity: WARNING
      section: Desired Outcomes
      section_hash: cf6eba8d31a26113
      fragment: null
      text: "A blank line split the Desired Outcomes list in two, leaving the fifth outcome as a separate list."
      fix: "Removed the blank line so all five outcomes form one list."
      verdict: fixed
      verdict_at: 2026-09-22
    - id: F-002
      phase: clarity
      severity: CRITICAL
      section: Desired Outcomes
      section_hash: cf6eba8d31a26113
      fragment: "begin returns within a bounded time regardless of how many superseded snapshots are waiting"
      text: "A bound with no number is not checkable - eight minutes is also a bounded time. The same defect this gate caught on the two previous intents, in the same author's wording."
      fix: "Bounded it: begin returns in under 5 seconds against the current backlog of 47 superseded snapshots, stated against the eight minutes it takes today. Done when carries the same number."
      verdict: fixed
      verdict_at: 2026-09-22
    - id: F-003
      phase: consistency
      severity: CRITICAL
      section: null
      section_hash: null
      fragment: "**Status:** approved"
      text: "Status-guard: the body was marked approved while F-002 was open."
      fix: "Cleared by fixing F-002; no CRITICAL remains open."
      verdict: fixed
      verdict_at: 2026-09-22
workflow:
  route: chain
  continuation: pending
---

# Intent: hosted-tool-dispatch-blocks-event-loop

**Date:** 2026-09-22
**Status:** approved

## Objective

Every `wiki_*` implementation is a plain `def`, and FastMCP invokes a sync tool inline in
its coroutine — `call_fn_with_arg_validation` ends `if fn_is_async: await fn(...) else:
return fn(...)`, with no thread hop. So every tool executes on the asyncio event loop, and
any blocking call inside one freezes the entire server: not one request, not one session,
but the loop. Nothing is accepted and nothing is answered while it runs.

Confirmed with a `py-spy` stack taken from the wedged production process, reading bottom
to top: `run_forever` → `_run_once` → `_run_coro` → `call_tool` → `wiki_code_publish_begin`
→ `_prune_superseded` → `execute` → `wait (psycopg/connection.py:484)`.

Reproduced deliberately: 60 concurrent `wiki_search` calls, each making one blocking
embedding request of about 1.5 s, made the healthcheck's own 2-second probe fail
continuously for 22 seconds against a server that answered `401` immediately before. The
calls serialize on the loop instead of running concurrently.

Why now: this took the hosted server down repeatedly on 2026-09-22. The triggering work
was ordinary maintenance — `_prune_superseded` deleting rows for 47 superseded code-graph
snapshots, about 1.24M relation rows, eight minutes of genuine database work with no lock
contention. Worse, the watchdog added for issue 93 restarts an unhealthy container after
about 3.5 minutes, so it killed that transaction every time and the next publication
started the same work from scratch. Neither component is wrong alone. What is wrong is
that an eight-minute maintenance query is indistinguishable from a dead server.

The objective is both, in that order. First the class of defect: a long operation must
slow the caller that asked for it, not silence the server. Fixing only the query would
leave the next slow tool to reproduce the same outage, so dispatch is the primary change
and the one the outcomes are written against.

Second, the query that exposed it. `_prune_superseded` runs unbounded cleanup for
unrelated snapshots at the start of every publication, so a publication is gated on work
it did not ask for. Once dispatch no longer silences the server this stops being an
outage, but an eight-minute `begin` is still wrong on its own terms. Both land in one
iteration by decision, accepting that the diff then spans a concurrency-model change and
code-graph internals.

## Desired Outcomes

- While a long tool call is running, the server still answers others: the healthcheck
  probe gets `401` or `405` within its 2-second budget, and another client's tool call
  completes.
- Docker does not mark the container `unhealthy` because the server is busy with long but
  ordinary work.
- The reproduction from issue 100 stops reproducing: 60 concurrent `wiki_search` calls no
  longer make the health probe time out.
- Under real concurrency, token isolation and compare-and-swap write semantics are
  unchanged: another session's scope stays invisible, and concurrent updates to one
  section still yield one `200` and the rest `conflict`.
- A code-graph publication is not gated on cleanup it did not ask for: `begin` returns in
  under 5 seconds against the current backlog of 47 superseded snapshots, where it takes
  about eight minutes today, and the backlog still drains to nothing over repeated
  publications rather than growing.

The fourth outcome is deliberate. The change must not trade a loud outage for quiet data
corruption. The fifth carries its own trap: bounding the work per run is easy to write in
a way that means cleanup never catches up, so "drains rather than grows" is part of the
outcome and not an aside.

## Health Metrics

- Hosted `initialize` measures a 23.5 ms median today and must stay under the established
  150 ms bound.
- Single-call latency on an unloaded server measures `wiki_status` ~0.3 s,
  `wiki_update_page` ~0.6 s, `wiki_search` ~1.5 s. Each median stays within twice its
  current value.
- Both suites keep passing — 3195 in the fast set, plus `tests/postgres` against a
  disposable database — without edits to the tests themselves.
- Token isolation holds absolutely: an `mcp-session-id` owned by another token is still
  refused `404`, and no scope widens.
- Resources stay bounded. At rest the process holds 37 threads and 4 PostgreSQL
  connections; neither grows without an upper bound under load.
- No connection-pool exhaustion errors under the same load that reproduced the defect —
  60 concurrent calls. This metric exists because the change creates that risk: up to the
  concurrency ceiling of handlers may want a database connection at once, and replacing
  "the server is silent" with "the server returns pool errors" would not be a fix.
- Cleanup stays correct and keeps up. The active snapshot and its rows are never deleted,
  and the superseded backlog — 47 snapshots and about 1.24M relation rows today — does not
  grow across a series of publications. A bound that turns an eight-minute stall into a
  permanent backlog is a regression, not a fix.

## Strategic Context

- Interacts with: the `call_tool` seam in `server.py` (which also drives `_idle_tracker`
  for the idle timeout), the `_safe` and `wrap` handler decorators, the non-hosted
  `_SESSION_BINDING.set` branch at `server.py:5078`, the `http.py` middleware with
  `_SessionBindings` and its per-session `anyio.Lock`, the locking surface (`RLock` in
  `_HostedSelectedState`, `FileLock` and the `_HELD_BASE` reentrancy guard, the base
  mutation lock), the psycopg connection pool, both transports including the local stdio
  server, the Telegram bot as another client of this same server, and the
  `tests/postgres/test_http.py` and `tests/deployment` suites. On the cleanup side:
  `_prune_superseded` and `begin` in `postgres/codegraph.py`, the four
  `code_graph_*` row tables plus `code_graph_snapshots` and `code_graph_domain_state`, and
  the `staging_retention_seconds` and `staging_cleanup_limit` settings that decide how much
  each run attempts.
- Verified rather than assumed: on the hosted path `wiki_bind` calls `session.set(...)`,
  which mutates the shared `_HostedBindingState` object and therefore survives a thread
  hop; `_MUTATION_BINDING` is set and read within one call stack and stays self-consistent
  in a copied context. The one write that would be lost across a hop is
  `_SESSION_BINDING.set` at `server.py:5078`, on the non-hosted branch.
- Until now all serialization was supplied implicitly by the event loop. Making dispatch
  concurrent makes that serialization real for the first time, which is where latent races
  would surface.
- Priority trade-off: **trust**. This change removes a loud failure. If it exchanges that
  for a quiet race in the transactional write path, the result is worse than the defect.
  Correctness of concurrent access outranks both delivery speed and implementation
  simplicity.

## Constraints

### Steering (behavioral guidance)

- Change one seam rather than the handlers. The tool surface is not rewritten.
- Prefer adding tests over changing passing ones. Change a test only where the contract
  genuinely changed, and say so explicitly.
- Do not refactor neighbouring code because it is nearby; the change is dispatch and its
  direct consequences.
- Where serialization was previously supplied by the event loop for free, make it explicit
  deliberately — name each such place rather than assuming it still holds.

### Hard (architectural enforcement)

- The token's grants remain the absolute ceiling, and a session id is never adopted across
  tokens.
- The transactional write path keeps its guarantees: no orphaned file, log record, or
  index row on failure, and `expected_revision` compare-and-swap semantics unchanged.
- No plaintext token is held beyond need or written to a log.
- The local stdio transport and its startup path keep working. The `_SESSION_BINDING`
  write at `server.py:5078` must not silently become a no-op.
- The `mcp<2` pin stays, and no private SDK internals are relied upon.
- Concurrency carries an explicit upper bound, and the psycopg pool must not become the
  new bottleneck in place of the event loop.
- Handler implementations stay plain `def`, callable directly from tests without the MCP
  runtime.
- Cleanup never deletes the active snapshot, the rows belonging to it, or a snapshot still
  being staged. Bounding the work per run must leave cleanup eventually complete: a
  backlog that can only grow is forbidden.

## Autonomy Zones

- Full autonomy (reversible, low risk): the dispatch seam itself, new tests, and the
  documentation of the resulting behaviour.
- Guarded (log + confidence threshold): every place where implicit serialization by the
  event loop is replaced with explicit serialization. Each is named and justified on its
  own, never bundled. Also the cleanup bound itself — the batch size, where cleanup runs
  relative to `begin`, and the evidence that the backlog still drains.
- Proposal-first (needs approval): changing the guarantees of the transactional write
  path; changing the concurrency ceiling away from the anyio default; changing the
  semantics of `_SESSION_BINDING` at `server.py:5078`.
- No autonomy (human only): destructive operations against the production database,
  including pruning the backlog of 47 superseded snapshots. These are outside this intent
  entirely.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules

- Halt if: moving execution off the loop is only achievable by weakening the transactional
  write guarantees or the token grant ceiling.
- Halt if: the psycopg pool becomes the new bottleneck and cannot be addressed inside this
  intent without changing storage configuration.
- Escalate if: real concurrency exposes a race in the write path that cannot be closed
  within the boundaries of this change.
- Halt if: bounding cleanup can only be made to fit by letting the superseded backlog grow
  without limit, or by risking the active snapshot.
- Deploy only after the full suite is green and a rollback path is confirmed: the hosted
  server is shared, and a failed dispatch change takes every client with it.
- Done when: during a deliberately long tool call the healthcheck probe answers `401` or
  `405` within its 2-second budget and another client's call completes; the issue 100
  reproduction of 60 concurrent `wiki_search` calls no longer times out the probe;
  concurrent updates to one section still yield one `200` and the rest `conflict`; both
  suites pass unchanged; no pool-exhaustion error occurs under that same load; and a
  publication's `begin` returns in under 5 seconds against the current backlog of 47
  superseded snapshots while that backlog measurably shrinks across successive
  publications.
