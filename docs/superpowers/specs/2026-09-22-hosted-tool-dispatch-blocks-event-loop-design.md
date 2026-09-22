---
review:
  spec_hash: 9ba0a634c2214831
  last_run: 2026-09-22
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings:
    - id: F-001
      phase: clarity
      severity: WARNING
      section: Testing
      section_hash: 5b78dae010a68153
      fragment: null
      text: "The Testing section listed eight cases with no binding to the requirements they verify, so the plan gate could not check coverage mechanically."
      fix: "Testing is now a table whose every row names the requirements it verifies; all seven of R1-R7 are referenced."
      verdict: fixed
      verdict_at: 2026-09-22
chain:
  intent: 477b97089342a0b1
workflow:
  route: chain
  continuation: full
---

# Design: hosted-tool-dispatch-blocks-event-loop

**Date:** 2026-09-22
**Status:** draft
**Intent:** `docs/superpowers/intents/2026-09-22-hosted-tool-dispatch-blocks-event-loop-intent.md` (approved, hash `477b97089342a0b1`)

## 1. Problem

Every `wiki_*` implementation is a plain `def`, and FastMCP invokes a sync tool inline in
its coroutine. `FuncMetadata.call_fn_with_arg_validation` ends:

```python
if fn_is_async:
    return await fn(**arguments_parsed_dict)
else:
    return fn(**arguments_parsed_dict)
```

There is no thread hop, so every tool runs on the asyncio event loop and any blocking call
inside one freezes the whole server. A `py-spy` stack from the wedged production process,
read bottom to top: `run_forever` → `_run_once` → `_run_coro` → `call_tool` →
`wiki_code_publish_begin` → `_prune_superseded` → `execute` → `wait
(psycopg/connection.py:484)`.

The work that exposed it is ordinary maintenance. `begin` runs `_cleanup_staging` and
`_prune_superseded` inside its own transaction before doing its job, and against a backlog
of 47 superseded snapshots — about 1.24M relation rows — that took eight minutes of active
database time with no lock contention. The watchdog from issue 93 restarts an unhealthy
container after roughly 3.5 minutes, so it killed that transaction every time and the next
publication began the same work again.

## 2. Acceptance (from intent)

Desired Outcomes, carried verbatim:

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

Done when, carried verbatim: during a deliberately long tool call the healthcheck probe
answers `401` or `405` within its 2-second budget and another client's call completes; the
issue 100 reproduction of 60 concurrent `wiki_search` calls no longer times out the probe;
concurrent updates to one section still yield one `200` and the rest `conflict`; both
suites pass unchanged; no pool-exhaustion error occurs under that same load; and a
publication's `begin` returns in under 5 seconds against the current backlog of 47
superseded snapshots while that backlog measurably shrinks across successive publications.

## 3. Verified assumptions

Each was checked against this repository and the running deployment, not inferred:

- `functools.wraps` around an `async def` that awaits `anyio.to_thread.run_sync` preserves
  the signature. `inspect.signature` follows `__wrapped__`, `func_metadata` builds the
  correct argument model, and `inspect.iscoroutinefunction` reports `True` — so FastMCP
  awaits the wrapper instead of calling it inline.
- On PostgreSQL storage the base mutation lock is never taken. `server.py` returns early
  on `if _is_postgres(bind)`, and the `with mutation_lock(bind.base)` branch is reachable
  only for a Git or filesystem binding. `FileLock`, the `_HELD_BASE` reentrancy guard and
  the Git commit path therefore do not apply to the hosted deployment at all.
- Requests within one session are already serialized by `async with state.request_lock()`
  in `http.py`. Concurrency introduced by this change is strictly *across* sessions.
- The deployed connection pool is `pool_min_size = 4`, `pool_max_size = 10`.
- Authentication already hops to the shared anyio thread pool at `http.py:587`, whose
  default limiter is 40 and which nothing in this repository reconfigures.
- Authentication draws from the **same** connection pool as the tools:
  `AuthStore(dsn, connection_factory=pool.connection)`. The thread budget and the
  connection budget are therefore two separate scarcities, and protecting only the first
  would still starve the liveness path.
- `statement_timeout_ms = 30000` bounds each individual statement, but nothing bounds a
  transaction. The eight minutes accumulated across many statements in a loop, each of
  them comfortably inside its own timeout.
- Every `code_graph_*` child table has a primary key prefixed
  `(iwiki_id, domain_id, snapshot_id)`, so the existing deletes already ride the index.
  The eight minutes are real row volume, not a bad plan.
- Nothing reads a superseded snapshot: every query joins
  `code_graph_domain_state.active_snapshot_id`.

## 4. Where the boundary falls

The seam is **registration**, not `call_tool`. `IdleFastMCP.call_tool` awaits
`super().call_tool(...)`, which is a coroutine and cannot be handed to
`to_thread.run_sync`. This project already registers implementations separately
(`mcp.tool()(wiki_status)`), which is the point where a wrapper can be inserted without
touching a single handler.

| Component | Responsibility after the change |
|---|---|
| Handler implementations | Unchanged plain `def`, still callable directly from tests |
| Registration wrapper | Moves the call onto a thread under the tool limiter |
| Tool limiter | Bounds how many handlers run at once |
| Shared anyio pool | Keeps serving authentication and liveness, unchanged |
| `_prune_superseded` / `_cleanup_staging` | Bounded by rows and a deadline, in their own transaction |

No new lock is introduced anywhere.

## 5. Requirements

### R1 — Tools execute off the event loop

Each `wiki_*` implementation is registered through a wrapper built with `functools.wraps`
that awaits `anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs),
limiter=_TOOL_LIMITER)`. The implementations themselves are not modified and remain plain
`def`.

The wrapper is applied at the existing registration site so the change has one location.
Both transports use it: the stdio server has a single client and gains nothing from a
separate path, and a transport-conditional wrapper would add a branch that tests would
have to cover twice.

### R2 — The tool ceiling comes from the storage, not from taste

`_TOOL_LIMITER` is one `anyio.CapacityLimiter` created at startup, sized
`pool_max_size - 2` — **8** against the deployed `pool_max_size = 10`.

Choosing a larger number does not buy concurrency, it converts it: handlers beyond the
pool size block waiting for a connection and eventually raise a pool timeout, which is the
failure the intent forbids as a health metric. Queueing on the limiter is visible and
bounded; queueing on the connection pool is neither.

The subtraction is the load-bearing part, not a safety margin. Authentication draws from
the same pool, so a ceiling equal to `pool_max_size` lets tools take every connection and
leaves the liveness path unable to authenticate — the same outage through a different
scarcity. Two connections are reserved because authentication is one short indexed query
and never needs more than a couple in flight.

The relationship is an invariant rather than a tuned constant: the ceiling must stay at
least 2 below `pool_max_size`. A deployment that wants more tool concurrency raises
`pool_max_size` first.

### R3 — Liveness never competes with tools for the same budget

Liveness is protected in both scarcities, because protecting one and not the other leaves
the outage intact.

- **Threads.** The authentication hop at `http.py:587` keeps using the shared anyio
  limiter and is not moved onto `_TOOL_LIMITER`. With the tool ceiling at 8 and the shared
  pool at 40, at least 32 slots remain regardless of tool load.
- **Connections.** The `pool_max_size - 2` ceiling from R2 leaves connections for the same
  path.

This is the requirement that makes the third Desired Outcome achievable. Moving tools off
the loop alone does not: 40 concurrent tools sharing one limiter with authentication would
starve the probe exactly as the loop does today, and a ceiling equal to the pool size
would starve it through the database instead.

### R4 — No write serialization is added

Concurrent mutating tools are allowed to run concurrently. PostgreSQL transactions provide
isolation and `expected_revision` compare-and-swap provides conflict detection; the base
mutation lock is not on this path at all, and per-session ordering is already guaranteed by
the middleware's request lock.

A global write lock was considered and rejected. It would serialize writers to unrelated
domains and make a long publication block every other write, in exchange for a guarantee
the database already provides.

### R5 — Cleanup is bounded by rows and a deadline

`_prune_superseded` and `_cleanup_staging` share one budget per call, whichever limit is
reached first:

- at most **100,000** deleted rows, counted across both cleanups and all four child tables;
- a wall-clock deadline of **2 seconds**, checked between batches;
- a batch size of **10,000** rows per statement, which stays far inside the 30-second
  `statement_timeout` that already bounds each statement.

Deletion proceeds in batches keyed by the primary-key prefix:

```sql
DELETE FROM iwiki.code_graph_relations
WHERE ctid IN (
  SELECT ctid FROM iwiki.code_graph_relations
  WHERE iwiki_id = %s AND domain_id = %s AND snapshot_id = %s
  LIMIT %s
)
```

A snapshot's own row is deleted only once its children are gone. Partial progress is safe
because a superseded snapshot is already invisible to every query, so interrupting cleanup
mid-snapshot leaves nothing inconsistent — this is what makes a row-based bound possible
where the current snapshot-based one is not.

The existing bound counts snapshots and is documented as needing to "stay small", but the
smallest useful value is already two snapshots, which is the eight minutes being fixed. A
snapshot is too coarse a unit to bound with.

### R6 — The row budget exceeds what one publication adds

The per-call row budget must be strictly greater than the rows one publication adds — about
30,000 for this repository's snapshot, against the 100,000 budget of R5. Otherwise each
publication removes less than it creates and the backlog grows, turning a stall into a
permanent leak.

At that ratio the current backlog of roughly 1.24M relation rows drains over about
15 publications rather than accumulating.

This is an invariant with a test, not a tuning note.

### R7 — Cleanup does not gate the publication

Cleanup runs in its own transaction, after the snapshot and session rows have been
committed. `begin` still performs it within the same call — no background task is
introduced — but the publication is no longer *gated* on it in the two senses that matter:
a cleanup failure is logged and cannot roll back the committed publication, and the total
time is bounded by R5's deadline rather than by the size of the backlog.

`begin` therefore costs its own inserts plus at most the 2-second cleanup deadline, which
is what keeps it inside the 5-second acceptance bound.

Today both cleanups run inside `begin`'s transaction before its inserts, which is why a
long cleanup both delays an unrelated publication and can roll it back.

## 6. Error handling

| Situation | Behaviour |
|---|---|
| Exception inside a handler | Raised through the wrapper and caught by `@_safe`, as today |
| Client disconnects mid-call | The thread runs to completion and the result is discarded; it no longer blocks the server |
| Tool ceiling reached | The caller queues on the limiter; no error is returned |
| Connection pool exhausted | Should not occur, because the ceiling is the pool size; if it does, it surfaces as the existing PostgreSQL error path |
| Cleanup fails or hits its deadline | Logged with the row count; the publication proceeds |
| Concurrent updates to one section | One `200`, the rest `conflict`, from the database's own compare-and-swap |

## 7. Testing

Every case names the requirement it verifies, so the plan stage can check coverage
mechanically rather than by reading prose.

| # | Case | Verifies |
|---|---|---|
| 1 | A handler that blocks for several seconds does not prevent the healthcheck's probe from answering within 2 seconds. The probe is unauthenticated, so it is answered `401` by the auth path and only `405` once authenticated — which is precisely why it depends on a free connection as well as a free thread. | R1, R3 |
| 2 | The issue 100 reproduction: 60 concurrent `wiki_search` calls, with the health probe answering throughout. | R1, R2, R3 |
| 3 | Two sessions updating the same section with genuinely overlapping transactions yield one `200` and one `conflict`. New coverage: the existing four-way test ran under event-loop serialization and never overlapped. | R4 |
| 4 | Saturating `_TOOL_LIMITER` queues callers rather than raising a pool timeout. | R2 |
| 5 | The registration wrapper preserves the signature, so `func_metadata` builds the same argument model as the unwrapped function. | R1 |
| 6 | Cleanup honours its row budget and its deadline, and never touches the active or a staging snapshot. | R5 |
| 7 | Repeated `begin` calls against a seeded backlog each stay within the time bound while the row count strictly decreases. | R5, R6, R7 |
| 8 | The row budget is greater than one publication's rows — an invariant test that fails loudly rather than degrading silently. | R6 |
| 9 | A cleanup failure is logged and leaves the publication committed. | R7 |

The existing suites are expected to pass unchanged.

## 8. Out of scope

- `superseded_retention_seconds`, which is a retention policy rather than a performance
  bound.
- Pruning the existing production backlog by hand; the intent places destructive database
  operations outside this work.
- The watchdog's thresholds. Once a long operation no longer makes the server unresponsive,
  the watchdog stops interacting with it.
