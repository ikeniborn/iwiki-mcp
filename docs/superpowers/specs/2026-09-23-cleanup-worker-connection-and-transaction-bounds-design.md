# Cleanup worker connection and transaction bounds — design

**Date:** 2026-09-23
**Intent:** `docs/superpowers/intents/2026-09-23-cleanup-worker-connection-and-transaction-bounds-intent.md` (`intent_hash` `12614af27a64cbb1`)
**Topic:** `cleanup-worker-connection-and-transaction-bounds`

## 1. Problem

Three undeclared boundaries of the code-graph cleanup worker, items 3, 4 and 5 of issue
104. All three were confirmed by reading `src/iwiki_mcp/postgres/codegraph.py` and
`src/iwiki_mcp/codegraph/application.py`.

1. Cleanup opens connections outside the pool. `create_postgres_publisher` passes no
   `connection_factory`, so `_transaction` falls back to `psycopg.connect(dsn)`, and
   `validate_direct_principal` opens a second raw connection when the store is
   constructed. Neither is counted by the `pool_max_size - 2` reserve that protects
   authentication. `_schedule_cleanup` and `schedule_wiki_cleanup` each spawn a
   `threading.Thread` with no global cap on how many exist.
2. The snapshot-row delete guards on `NOT EXISTS` over `code_graph_files` alone. Drain
   order puts files last, so it holds today, but a snapshot with zero file rows and
   surviving symbol rows would pass it.
3. `_run_cleanup_cycle` wraps one whole `_prune_superseded` call in a single transaction,
   and that call iterates up to `superseded_cleanup_limit` snapshots. The 10,000-row
   statements inside `_delete_snapshot_rows` are statements, not commits, so a kill rolls
   back every snapshot the call touched — while both language siblings of
   `docs/code-graph-publishing.md` and the wiki page `concept/code-graph-storage` publish
   the opposite.

Two facts about foreign keys correct issue 104's wording and shape the index decision.
`code_graph_relations` carries exactly one index, its primary key
`(iwiki_id, domain_id, snapshot_id, relation_id)`; the migrations create no index on any
`code_graph_*` table. The three foreign keys on that table are therefore covered to their
snapshot prefix, not unindexed: a cascade scans one snapshot's relations rather than the
whole table, but does so once per deleted parent row. A snapshot holds roughly 21,000
relations.

## 2. Acceptance (from intent)

Desired Outcomes, carried verbatim:

- A burst of publications across many domains never drives the server's PostgreSQL
  connections past the stated ceiling, and authentication answers throughout the burst.
- A superseded snapshot's own row is never removed while any of its child rows survive,
  under any interleaving or truncation of the drain.
- Killing the server mid-cleanup loses at most one batch: rows already removed stay
  removed, and the drain resumes where it stopped.
- The log alone shows how many connections cleanup workers hold and how often they waited
  on the cap; diagnosing exhaustion requires no query against PostgreSQL.
- Removing a snapshot's symbol rows no longer costs a snapshot-wide relation scan per
  deleted row. The improvement is measured against the current behaviour on a real
  snapshot, and the index that buys it is kept only because that measurement, set against
  its own maintenance cost, justifies it.

Done when, carried verbatim:

> On the hosted server, with recorded commands and their output — a publication burst is
> observed holding connections within the stated ceiling while authentication answers
> throughout; a snapshot row is observed surviving while any of its children remain; a
> restart taken mid-drain is observed losing no more than one batch; the log alone reports
> the workers' connection use; and the index decision is settled by a recorded
> before-and-after of both delete time and publication write cost, whichever way it went.
> Passing tests are not evidence for any of these.

One metric from the intent is restated rather than carried, because this design changes
the string it names. The intent's fourth Health Metric reads "the sweep still reports
`swept N of N writable domains`". This design removes the function that walks domains, so
the equivalent evidence becomes `queued N of M writable domains` at enqueue time plus one
completion record per domain. The metric's substance — no domain is cut out of the walk to
satisfy a bound — is unchanged and is what acceptance checks.

## 3. Architecture: the maintenance runtime

A module-level maintenance runtime installed beside the hosted runtime, holding three
things: a `psycopg_pool.ConnectionPool` reserved for maintenance (`min_size=0`,
`max_size=N`), a bounded work queue whose items are `(iwiki_id, domain)` with set-based
deduplication, and a fixed set of W worker threads.

**R1.** The maintenance runtime is created in `_install_hosted_runtime` and stopped in
`_clear_hosted_runtime`, mirroring how `_HOSTED_POOL` is already installed and cleared.
*DoD:* installing the hosted runtime twice leaves one runtime; clearing it stops every
worker and closes the maintenance pool.

**R2.** `W == N`, expressed as one module constant with a starting value of 2. A worker
holds at most one connection at a time, so more workers than connections, or more
connections than workers, buys nothing. The thread count is the operative bound; the
pool's `max_size` is a hard ceiling on connections that holds even if a worker acquires a
second one.
*DoD:* the constant appears once; a test asserts the maintenance pool's `max_size` equals
the worker count.

**R3.** Work is enqueued, not executed, by the request that triggers it. Enqueue is
non-blocking, and its failure is swallowed and logged at debug.
*DoD:* a test asserts a tool call returns without waiting for any cleanup work, and that
an enqueue raising internally does not propagate to the caller.

**R4.** The queue deduplicates by `(iwiki_id, domain)`. A key already queued or in flight
is not enqueued again. This single set replaces both existing guards — `_SWEEP_ACTIVE`
keyed by `iwiki_id` in `codegraph/application.py` and the class-scoped `_cleanup_active`
keyed by `(iwiki_id, domain)` in `postgres/codegraph.py` — and both `threading.Thread`
call sites are removed with them.
*DoD:* enqueueing the same key twice yields one work item; neither removed guard nor
either thread call site remains in the source.

**R5.** The queue is bounded. An enqueue against a full queue is dropped, counted, and
logged at warning. The work returns on a later request; nothing waits on it.
*DoD:* a test fills the queue and asserts the next enqueue is dropped and counted rather
than blocking or growing the queue.

**R6.** The per-wiki throttle stays in `cleanup_sweep_due` with its 900-second floor keyed
by `iwiki_id`, and `_SWEEP_LAST` is recorded at enqueue time rather than at completion, so
the floor bounds enqueue frequency directly.
*DoD:* a test asserts two enqueues for one wiki inside the interval produce one round of
work items, and that the timestamp is written before any worker runs.

**R7.** Without a maintenance runtime — the local stdio path, where no pool exists —
`begin`-triggered cleanup falls back to a single daemon thread that takes its
`(iwiki_id, domain)` key from the same deduplication set R4 introduces, so the removed
guards are not reinstated under another name. The fallback opens one raw connection, as
today. This path is bounded by construction: one stdio process serves one client.
*DoD:* a test with no maintenance runtime installed asserts the fallback runs, that it
deduplicates through the R4 set, and that the hosted path is not used.

## 4. Connection accounting

**R8.** Cleanup draws connections only from the maintenance pool. The tools-and-auth pool
is never used by cleanup, and cleanup never raises the tool ceiling.
*DoD:* a test asserts that a cleanup cycle acquires no connection from the hosted pool.

**R9.** `validate_direct_principal` gains an optional `connection_factory` parameter
defaulting to the current raw `psycopg.connect`, so the publication path is unchanged, and
cleanup passes the maintenance pool's factory. Without this a worker would hold two
connections at once and the ceiling would understate reality.
*DoD:* a test asserts a cleanup worker holds at most one maintenance connection at any
moment, including while its store is being constructed.

**R10.** The server's total PostgreSQL connections are `pool_max_size + N`, stated in the
documentation as one arithmetic sentence rather than implied. This expression is the
"stated ceiling" the first Desired Outcome refers to; finding F-001 on the intent recorded
that the number did not yet exist, and this requirement is where it comes into existence.
*DoD:* the number appears in `docs/code-graph-publishing.md` and its Russian sibling, and
matches the constants in the source.

**R10a.** The starting value of 2 for `W` is confirmed or revised by the publication-burst
observation that acceptance requires. If that burst shows workers waiting on the queue for
longer than one cycle's duration, or the backlog failing to shrink, the value is revised
and both the old and new values are recorded with the measurement that moved them.
*DoD:* the burst observation is recorded on the task page together with the value it
confirmed or changed.

The publication path's own `validate_direct_principal` connection is already bounded: a
publication runs as a tool call under `_TOOL_LIMITER`, so no more than the tool ceiling of
them can be in flight. It is left as it is.

## 5. Transaction boundary

**R11.** `_transaction` is split into `_connection()`, which opens and closes one
connection, and `_transaction_on(connection)`, which runs one transaction with a cursor on
an already-open connection. `_transaction()` remains as the composition of the two, so
every existing caller is untouched.
*DoD:* no existing call site of `_transaction` changes; both new helpers have direct
tests.

**R12.** A cleanup cycle acquires one connection for its whole duration and runs many
transactions on it. One transaction is one delete of at most `_CLEANUP_BATCH_ROWS` rows
from one child table of one snapshot.
*DoD:* a test counts connection acquisitions per cycle and asserts one, while asserting
more than one commit occurred.

**R13.** `_prune_superseded` is decomposed into three roles, each in its own transaction:
selecting candidate snapshots, draining one snapshot batch by batch, and deleting that
snapshot's own row. The child-table order stays `code_graph_wiki_links`,
`code_graph_relations`, `code_graph_symbols`, `code_graph_files`, because files last is
what makes an empty `code_graph_files` imply the earlier tables were drained first.
*DoD:* a test injects a failure partway through a drain and asserts the batches committed
before it remain committed, and that a subsequent cycle resumes from that point.

**R14.** `superseded_cleanup_limit` keeps its current meaning as the candidate-selection
page size and its default of 2. The row budget governs how much a cycle removes. Changing
this default is proposal-first and out of scope here.
*DoD:* the parameter's default is unchanged in the source.

## 6. Parent-row guard

**R15.** The snapshot-row delete guards on `NOT EXISTS` over all four child tables —
`code_graph_wiki_links`, `code_graph_relations`, `code_graph_symbols` and
`code_graph_files` — in addition to the existing exclusion of the active snapshot.
*DoD:* four tests, one per child table, each leaving rows only in that table and asserting
the snapshot row survives. Each is shown to discriminate by reverting the guard and
observing the test fail.

The guard is required by this change rather than merely improved by it. Today one
transaction spans the whole prune, so a state with zero file rows and surviving symbol
rows cannot persist. Per-batch commits make exactly that state durable, so the change that
fixes item 5 is what makes item 4 reachable.

## 7. Reactivation race

The retention window exists to leave an operator a manual revert target. Candidate
selection and deletion are not protected against a concurrent reactivation today either —
the transaction runs at READ COMMITTED — but the window is narrow. Per-batch commits widen
it to the whole duration of a snapshot's drain, which is minutes. If an operator reverts to
snapshot X during that window, X's own row survives, because the parent delete already
carries `AND snapshot_id NOT IN (SELECT active_snapshot_id ...)`, but its child rows are
already gone: the operator gets an active snapshot with no rows.

**R16.** Each batch transaction re-checks that the snapshot is still not the active one
before deleting, and abandons the snapshot when it is. This costs one indexed lookup per
10,000 rows and narrows the race to a single batch.
*DoD:* a test reactivates a snapshot partway through its drain and asserts the drain stops
within one batch and the snapshot row survives.

## 8. Index decision procedure

**R17.** The decision is settled by measurement on a disposable database seeded to
production scale, never against production — creating an index there is a deployment step
and carries no autonomy. Six quantities are recorded: drain time for one snapshot in the
normal child-table order; drain time in the reverse order, where relations still live when
symbols are deleted; `CREATE INDEX CONCURRENTLY` wall time and resulting size for each
candidate; insertion time for a publication's ~21,000 relations before and after; total
on-disk size before and after; and dead-tuple accumulation after a drain with the time
taken to collect it.
*DoD:* all six recorded with their commands and output on the task page.

**R18.** Candidates are the three foreign keys on `code_graph_relations`:
`(iwiki_id, domain_id, snapshot_id, source_symbol_id)`, the same shape on
`source_file_id`, and the same on `target_symbol_id`. An index is added only where the
measurement shows it cuts the delete time of the path the drain actually takes by more
than its measured write and space cost. A result of no index is a valid outcome and is
recorded as one.
*DoD:* a decision per candidate, each with the measurement that produced it.

In the normal drain order relations are deleted before symbols, so by the time symbols are
deleted the foreign-key check examines an already-emptied set — live rows are gone, dead
tuples remain until vacuum. The expected result is therefore that no index is justified for
the drain path, and that any measured cost is vacuum timing rather than index absence. The
procedure exists to establish which of those is true, not to confirm either.

## 9. Observability

**R19.** The following are logged: `queued N of M writable domains` at enqueue; rows
removed and elapsed time per completed work item; the maintenance pool's `get_stats()`
alongside each work item, carrying `pool_available`, `requests_waiting`, `requests_wait_ms`
and `connections_num`; and a counted warning when a work item is dropped against a full
queue. Throttle skips stay at debug.
*DoD:* a test asserts each line is emitted on its path, and that connection use can be read
from the log alone without querying PostgreSQL.

## 10. Error handling and shutdown

**R20.** A worker lets nothing escape. Exceptions are logged by type only, matching the
existing sanitization, and a failed work item's key is released so it can be enqueued
again later.
*DoD:* a test raising inside a work item asserts the worker survives, the key is released,
and nothing reaches the caller.

**R21.** Shutdown sends one sentinel per worker, joins each with a timeout, then closes the
maintenance pool.
*DoD:* a test asserts every worker thread has exited and the pool is closed after
`_clear_hosted_runtime`.

## 11. Documentation corrections

**R22.** The claim that a cycle drains "in committed batches, each batch in its own
transaction" is corrected in all three places that publish it: `docs/code-graph-publishing.md`,
`docs/code-graph-publishing.ru.md`, and the wiki page `concept/code-graph-storage` under
"Lifecycle and metadata". After this change the claim becomes true, so the correction is to
state precisely what one batch is — at most `_CLEANUP_BATCH_ROWS` rows from one child table
of one snapshot — and to record the new trigger, queue and connection ceiling.
*DoD:* all three describe the same behaviour as the source, and the connection arithmetic
from R10 appears in both repository siblings.

## 12. Testing

Unit coverage follows the DoD of each requirement above. Beyond those:

**R23.** PostgreSQL-backed tests run against a real database, since `tests/postgres` skips
itself without one and a change to a PostgreSQL path is otherwise unverified.
*DoD:* the suite is run against a disposable database and its result recorded, not relied
on from the default skipping run.

**R24.** The guard tests and the resumability test are each shown to discriminate:
reverting the change under test makes them fail.
*DoD:* the demonstration is recorded for each.

## 13. Out of scope

- Changing `pool_max_size`, the tool ceiling, or the `superseded_cleanup_limit` default.
- Altering any foreign key's delete action.
- Item 6 of issue 104, the direct-PostgreSQL CLI publication exiting before its cycle
  drains. It is documented rather than fixed, and the queue does not change it.
- A container memory limit. Issue 102 was closed as not planned, and the bounded queue in
  R5 is what keeps this change from depending on one.
