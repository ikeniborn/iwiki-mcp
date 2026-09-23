# Intent: cleanup-worker-connection-and-transaction-bounds

**Date:** 2026-09-23
**Status:** approved

## Objective

Close the last three follow-ups of issue 104 — items 3, 4 and 5 — before the next
publication-heavy period.

All three are undeclared boundaries of the same code-graph cleanup worker, and all three
were confirmed by reading `src/iwiki_mcp/postgres/codegraph.py` rather than inferred:

- `create_postgres_publisher` passes no `connection_factory`, so `_transaction` falls back
  to a raw `psycopg.connect(dsn)`. The `pool_max_size - 2` reserve that protects
  authentication therefore accounts for pooled tool work only, and `_schedule_cleanup`
  spawns one thread per `(iwiki_id, domain)` with no global cap.
- `_prune_superseded`'s parent delete guards on `NOT EXISTS` over `code_graph_files`
  alone. Drain order makes files last, so it holds today; a snapshot with zero file rows
  and surviving symbol rows would pass the guard and cascade onto
  `code_graph_relations_source_symbol_fk`.
- That foreign key is covered only to its snapshot prefix, which is the more precise
  statement than issue 104's "unindexed". The migrations create no index on any
  `code_graph_*` table; `code_graph_relations` has exactly one, its primary key
  `(iwiki_id, domain_id, snapshot_id, relation_id)`. The key's leading three columns are
  shared with the foreign key, so a cascade scans one snapshot's relations rather than the
  whole table — but it does so once per deleted parent row. A snapshot holds roughly
  21,000 relations, so deleting a few thousand symbol rows means a few thousand scans of
  that size. That is the shape of the prune that once ran for 49 minutes.
  `code_graph_relations_source_file_fk` has the same coverage, and
  `code_graph_relations_target_symbol_fk` is `ON DELETE NO ACTION`, so it forces the same
  lookup as a check on every symbol delete.
- `_run_cleanup_cycle` wraps one whole `_prune_superseded` call in a single transaction,
  and that call iterates up to `superseded_cleanup_limit` snapshots. The 10,000-row
  statements inside `_delete_snapshot_rows` are statements, not commits, so a kill rolls
  back every snapshot the call touched.

Indexing those foreign keys is therefore in scope rather than deferred: the four-way guard
closes the hole the drain can actually reach, but leaves the cascade expensive everywhere
else it can occur. It is in scope as a measured decision, not as a foregone one — an index
is maintained on every publication for as long as it exists.

Why now: the worker only just became reachable from any authenticated request, so it runs
far more often than when these boundaries were written. The third item also contradicts
what both language siblings of `docs/code-graph-publishing.md` publish — a claim the
rewrite in PR 107 carried forward unexamined rather than caught.

## Desired Outcomes

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

## Health Metrics

- The superseded backlog keeps shrinking under normal publication load. A cap that made
  the connection outcome green by quietly stopping the drain is a failure, not a fix.
- A publication stays seconds, not minutes, end to end, and no publication call waits on
  cleanup.
- The liveness probe answers inside its two-second budget while the tool ceiling is
  saturated.
- The sweep still reports `swept N of N writable domains`; no domain is cut out of the
  walk to satisfy a bound.
- An index added for delete speed does not quietly move the cost to the write path. A
  publication's insert throughput, the table's on-disk size, and autovacuum's ability to
  keep up with it are each measured before and after, and each stays within a stated,
  accepted margin. An index is a structure that must be maintained on every publication
  forever, not a one-time purchase.

## Strategic Context

- Interacts with: hosted tool dispatch (`_threaded` and `_TOOL_LIMITER`), the psycopg
  `ConnectionPool`, `AuthStore` — which shares that pool — PostgreSQL's own
  `max_connections`, the wiki sweep in `codegraph/application.py`, the direct-PostgreSQL
  CLI publication path, the container healthcheck and the host systemd watchdog, and both
  language siblings of `docs/code-graph-publishing.md`.
- Priority trade-off: trust. Two of the three defects are latent and silent, and cleanup
  deletes data — it has no right to be wrong quietly, even at the cost of a slower drain.

## Constraints

### Steering (behavioral guidance)

- Measure before choosing any numeric bound. A round number picked without measurement is
  not a bound.
- Where the code and a published claim disagree, decide which one is correct on the
  merits; do not assume the code is the party in error.
- Keep new machinery minimal. No configurability without a second caller, and no
  abstraction introduced for a single use.
- An index earns its place or it is dropped. Add the narrowest set that the delete
  measurement actually justifies — not one per foreign key by symmetry — and if the
  measured write, space and vacuum cost is not repaid by the measured delete benefit, ship
  the guard alone and record the negative result.

### Hard (architectural enforcement)

- The authentication reserve is untouchable. No cleanup design may reduce what remains
  available to authentication — neither by borrowing from the connection pool nor by
  raising the tool ceiling to compensate.
- Child rows are deleted explicitly, children before parents. No substitution of
  `ON DELETE CASCADE`, and no parent row removed while any child row of that snapshot
  survives.
- Cleanup never affects its caller. A failure, or a wait on the cap, never delays or fails
  a publication or an ordinary tool call.
- One store, one domain. No predicate may reach beyond the domain the store was
  constructed and principal-validated for.

## Autonomy Zones

- Full autonomy (reversible, low risk): test design, log wording, documentation phrasing,
  internal naming, and refactoring confined to the touched functions without behaviour
  change.
- Guarded (log + confidence threshold): the numeric concurrency cap, the commit
  granularity, and which indexes — if any — the foreign keys on `code_graph_relations`
  receive. Each is chosen from measurement, and the measurement is recorded in the task
  ledger beside the decision, including a negative result that leads to no index.
- Proposal-first (needs approval): any schema change other than those indexes, including
  altering a foreign key's delete action; changing `pool_max_size`, the tool ceiling, or
  the `superseded_cleanup_limit` default. Creating an index against the production
  database is a deployment step and falls under no autonomy, whatever the design
  concluded.
- No autonomy (human only): deployment, merge, and any destructive operation against
  production data outside the normal cleanup path.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules

- Halt if: a proposed bound cannot be shown to keep the drain ahead of the inflow. That is
  the original failure re-entered through its own fix, and it is not a trade to make
  silently.
- Halt if: correcting the transaction boundary turns out to require a schema change. That
  crosses into proposal-first and needs a decision, not an assumption.
- Halt if: an index's measured maintenance cost is not repaid by its measured delete
  benefit. Record the negative result and ship the guard alone; carrying a structure that
  every publication must maintain forever, for a benefit that did not materialise, is the
  worse outcome.
- Escalate if: two different bounding strategies both fail to satisfy the connection
  outcome and the drain-throughput metric at the same time.
- Done when: on the hosted server, with recorded commands and their output — a publication
  burst is observed holding connections within the stated ceiling while authentication
  answers throughout; a snapshot row is observed surviving while any of its children
  remain; a restart taken mid-drain is observed losing no more than one batch; the log
  alone reports the workers' connection use; and the index decision is settled by a
  recorded before-and-after of both delete time and publication write cost, whichever way
  it went. Passing tests are not evidence for any of these.
