# `code_graph_relations` foreign-key index measurement

Task 9 of `docs/superpowers/plans/2026-09-23-cleanup-worker-connection-and-transaction-bounds-plan.md`.
Decides, by measurement, whether the three foreign keys on `code_graph_relations`
(`source_symbol_id`, `source_file_id`, `target_symbol_id`) should be indexed.

**Result: all three measurably help the shipped drain path and are added.** This
falsifies the plan's stated expectation ("no index is justified for the drain path");
Steps 2-4 below show why, with numbers.

## Setup

Disposable container, real corpus, real publication path — never hand-written INSERTs.

```bash
docker run -d --name iwiki-pgbench -e POSTGRES_PASSWORD=pgtest -e POSTGRES_DB=iwiki_test \
    -p 127.0.0.1:55434:5432 pgvector/pgvector:pg16
docker exec iwiki-pgbench psql -U postgres -d iwiki_test -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

Port 55433 (named in the brief) was occupied by an unrelated, already-running container
(`pgcheck2`) not part of this task; 55434 was used instead. `iwiki-pgtest` on 55432 (also
not mine) was never touched.

The corpus was generated with `tests.codegraph.publication_contract_support.
generate_python_project(root, 7000)` (7000 modules, one class + one method each, one
import edge to a neighbor) and indexed with the real `CodeGraphIndexer` /
`PythonAdapter` — the same code path `eval/code_graph/runner.measure_publication` uses,
and the one `iwiki-mcp code publish` uses in production:

```python
indexer = CodeGraphIndexer(..., adapter_factories={"python": ...})  # eval.code_graph.runner._benchmark_indexer
built = indexer.build(force=True)   # 92.5s, state=ready
rows = {kind: list(indexer.store.stable_rows(kind)) for kind in
        ("repositories", "files", "symbols", "relations")}
# counts: repositories=1 files=7001 symbols=14000 relations=21000
```

Rows were published through the real `PostgresCodeGraphStore.begin` / `publish_batch` /
`finalize` — the same store `wiki_code_publish_begin/_batch/_finalize` calls — 30 times
into one domain, each publication superseding the previous snapshot:

```python
store = PostgresCodeGraphStore(dsn, "bench", "code", "bench-owner", lock_timeout_ms=5000,
                                session_ttl_seconds=300, staging_retention_seconds=300,
                                staging_cleanup_limit=2)
header = exported_header(rows)  # SnapshotHeader built the same way measure_publication does
session = store.begin(header)
for batch in iter_snapshot_batches(rows, max_rows=1000, max_bytes=1_000_000):
    store.publish_batch(session, batch)
store.finalize(session)
```

30 publications landed: 2.32-2.69s each (mean ~2.5s), producing 30 snapshots
(29 superseded, 1 active) of ~21,000 relations apiece — the scale the brief asked for,
reached in full. More snapshots were published later purely to have enough intact
superseded snapshots for repeated trials; the domain's history grew past 30 over the
course of the experiment without changing the per-snapshot shape.

## Quantity 1 + 3 + 4: drain time, `CREATE INDEX CONCURRENTLY`, insertion cost, per index state

`run_cleanup_cycle()` drains a *backlog* up to `_CLEANUP_CYCLE_ROWS` (200,000 rows); with
20+ superseded snapshots present and zero retention it happily swept 4-5 of them in one
call during an early trial, contaminating a per-snapshot measurement. All drain numbers
below instead call `_drain_snapshot(connection, domain_id, snapshot_id, budget)` directly
— the real per-snapshot primitive `run_cleanup_cycle` calls in a loop, same shipped
child-table order (`code_graph_wiki_links`, `code_graph_relations`, `code_graph_symbols`,
`code_graph_files`) — isolating exactly one snapshot with no re-implemented logic.

```python
with store._connection() as connection:
    removed = store._drain_snapshot(connection, domain_id, snapshot_id,
                                     store._CLEANUP_CYCLE_ROWS)
```

```sql
CREATE INDEX CONCURRENTLY code_graph_relations_source_symbol_idx
    ON iwiki.code_graph_relations (iwiki_id, domain_id, snapshot_id, source_symbol_id);
CREATE INDEX CONCURRENTLY code_graph_relations_source_file_idx
    ON iwiki.code_graph_relations (iwiki_id, domain_id, snapshot_id, source_file_id);
CREATE INDEX CONCURRENTLY code_graph_relations_target_symbol_idx
    ON iwiki.code_graph_relations (iwiki_id, domain_id, snapshot_id, target_symbol_id);
```

**The `CREATE INDEX CONCURRENTLY` column below measures a statement that does not ship.**
It was used here only so building the index wouldn't itself block the benchmark harness's
own connections. `run_migrations` applies every pending migration inside one
`with connection.transaction()`, and `CONCURRENTLY` cannot run inside a transaction block,
so the shipped migration (schema version 9) uses a plain `CREATE INDEX` for each of the
three. Plain `CREATE INDEX` takes a `SHARE` lock on `code_graph_relations`, blocking every
INSERT, UPDATE, and DELETE against it — every publication batch and every cleanup drain —
for as long as the whole migration transaction stays open, not just until the index
finishes building. Two mitigating facts: a plain `CREATE INDEX` on an already-populated
table is a single heap-scan-and-sort with no second pass and no wait for concurrent
transactions, so the measured 1.0-1.4s below at ~630,000 rows is a conservative **upper**
bound for the shipped statement at the roughly one million rows a production table
carries, not a lower one; and the production `lock_timeout_ms = 5000` means the migration
aborts cleanly if it cannot acquire the lock promptly, rather than queuing indefinitely
behind an in-flight publication.

| State | Shipped drain, 1 snapshot (s) | `CREATE INDEX CONCURRENTLY` (s), measurement-only, does not ship | Index `pg_relation_size` | Insert 1 publication (s) |
|---|---|---|---|---|
| no index | see below — 6 observations, mean 6.17s, range 3.068-14.385s | - | - | 2.499 |
| + `source_symbol_idx` | 1.500 | 1.017 | 32,194,560 B (30.7 MiB) | 2.511 |
| + `source_file_idx` (2 total) | 1.389 | 1.418 | 34,758,656 B (33.1 MiB) | 2.609 |
| + `target_symbol_idx` (all 3) | 1.184 | 1.380 | 60,203,008 B (57.4 MiB) | 2.723 |
| control: indexes dropped again, cache still warm | 5.278 | - | - | 2.493 |

All six no-index shipped-drain observations, pooled honestly rather than reported as a
partial subset: `3.068` (the first trial), `4.575, 4.469, 14.385, 5.230` (four repeats),
`5.278` (the cache-warmth control above). Sum 37.005s / 6 = **mean 6.17s**, min 3.068s,
max 14.385s. An earlier draft of this report computed the mean from the four repeats only
(excluding 3.068), reporting 7.165 — about 16% high. Corrected everywhere in this report;
the decision is unchanged because the separation argument rests on the minimum
(no-index best case, 3.068s) and maximum (14.385s) against the indexed mean (1.097s), not
on the mean of the no-index set.

The control trial (drop all 3 indexes, measure again with a warm buffer cache) came back
*slower* than the very first cold-cache baseline, ruling out "cache warmth" as the
explanation for the speedup seen after adding indexes — the indexes are doing the work.

### Per-candidate isolation (repeated trials, one index at a time)

| Index alone | Trials (s) | Mean (s) |
|---|---|---|
| none | all 6 no-index observations: [3.068, 4.575, 4.469, 14.385, 5.230, 5.278] | 6.17 |
| `source_symbol_idx` | [1.881, 1.627, 1.561] | 1.690 |
| `target_symbol_idx` | [1.962, 2.240, 2.077] | 2.093 |
| `source_file_idx` | [1.668, 1.773, 10.985] | 4.808 (2 of 3 trials ~1.7s; the 10.985s outlier matches the no-index trials' own outlier pattern — background autovacuum/checkpoint I/O, not the FK check) |
| `source_symbol_idx` + `source_file_idx` | [1.344, 1.303, 1.647] | 1.431 |
| all three | [1.054, 1.145, 1.119, 1.070] | 1.097 |

Mechanism, confirmed rather than inferred: `pg_stat_user_indexes.idx_scan` deltas across
one drain with all three indexes present —

```sql
SELECT indexrelname, idx_scan FROM pg_stat_user_indexes
WHERE schemaname='iwiki' AND relname='code_graph_relations';
```

`source_file_idx` +7001 scans (= files deleted that drain), `source_symbol_idx` +14007
and `target_symbol_idx` +14001 (~= symbols deleted), **`code_graph_relations_pkey`
unchanged (+0)**. Without the candidate indexes, that same FK-check load falls on the
primary key's `(iwiki_id, domain_id, snapshot_id, …)` prefix — a range scan bounded to
one snapshot, but one that still has to walk however many dead tuples that snapshot's
just-deleted relations left behind, once per deleted symbol or file row (14,000 + 7,001
times per drain). That is exactly the variance measured above (3.1-14.4s across all six
no-index observations, mean 6.17s, no fixed ceiling) versus the tight, predictable ~1.1s
with dedicated indexes.

## Quantity 2: reverse order — cannot complete, by design

```python
# Same ctid-batch shape _delete_batch uses, but against code_graph_symbols
# BEFORE code_graph_relations is touched for that snapshot.
cur.execute("DELETE FROM iwiki.code_graph_symbols WHERE ctid IN ("
            "SELECT ctid FROM iwiki.code_graph_symbols "
            "WHERE iwiki_id=%s AND domain_id=%s AND snapshot_id=%s)", (...))
```

Every trial, at every index state, this raised
`psycopg.errors.ForeignKeyViolation` on `code_graph_relations_target_symbol_fk`
(`ON DELETE NO ACTION`) and rolled back — it is not merely slower, PostgreSQL refuses it
outright. Wall time to the violation: 0.074s (no index), 0.056s (+`source_symbol_idx`),
0.075s (+2 indexes), 0.045s (+all 3). Indexes do not move this number because the
violation trigger is a semi-join with `LIMIT 1`: matches are abundant (most symbols have
a live relation), so it finds one and aborts almost immediately regardless of whether an
index exists. This confirms the shipped order (relations deleted before symbols) is a
hard constraint the schema enforces, not just a performance choice.

## Quantity 5: total on-disk size, before and after

```sql
SELECT pg_total_relation_size('iwiki.code_graph_relations'); -- etc. per table
```

| | files | symbols | relations | wiki_links | snapshots |
|---|---|---|---|---|---|
| before (fresh empty domain) | 16,384 | 16,384 | 16,384 | 16,384 | 24,576 |
| after seeding 30 snapshots, no index | 274,694,144 | 549,289,984 | 916,168,704 | 16,384 | 131,072 |
| final (all 3 indexes, after all drains/inserts) | 206,995,456 | 430,497,792 | 674,332,672 | 16,384 | 172,032 |

Before: ~90 KiB total. After seeding: ~1.66 GiB. Final: ~1.22 GiB (fewer live snapshots
remain at the end because many were consumed by drain trials; `code_graph_relations`'s
674 MiB here includes the three new indexes). Isolated index cost: 32,194,560 +
34,758,656 + 60,203,008 = 127,156,224 bytes (~121 MiB) for all three together, against a
relations table on the order of several hundred MiB to ~1 GiB at 30-snapshot scale.

## Quantity 6: dead tuples and autovacuum collection time

Production-named tuning applied at table level (`autovacuum_naptime` is not named by the
brief and was left at the default 60s):

```sql
ALTER TABLE iwiki.code_graph_relations SET (
    autovacuum_vacuum_scale_factor = 0.02,
    autovacuum_vacuum_threshold = 1000,
    autovacuum_vacuum_cost_limit = 2000
);
```

```sql
SELECT n_dead_tup, n_live_tup, last_autovacuum FROM pg_stat_user_tables
WHERE schemaname='iwiki' AND relname='code_graph_relations';
```

Immediately after one full shipped-order drain: `n_dead_tup` 85,940 -> 106,940 (+21,000,
this drain's relations), `n_live_tup` 293,563. Polled every 5s: at t+5s, `n_dead_tup=0`
and `last_autovacuum` had advanced to a fresh timestamp — the whole backlog was collected
inside one 5-second poll interval. The prior autovacuum on this table had completed 87s
earlier, consistent with the default 60s naptime plus scheduling jitter; `cost_limit=2000`
is far above what one pass over ~21-27k dead tuples needs, so the visible latency here is
polling granularity, not vacuum work.

## Decision per candidate

Rule: add an index only when its measured delete benefit on the shipped drain path
exceeds its measured write and space cost.

- **`code_graph_relations_source_symbol_idx` — ADD.** Delete benefit ~4.5s/drain
  (6.17s no-index mean -> 1.690s alone, confirmed by +14,007 `idx_scan`). Write cost
  ~0.012s per ~21k-row publication (2.499s -> 2.511s as the first index added, n=1 — see
  the write-cost precision note below). Space 30.7 MiB. Benefit far exceeds cost.
- **`code_graph_relations_source_file_idx` — ADD.** Delete benefit converges to the same
  ~1.7s territory in 2 of 3 clean trials (the 4.808s mean is dragged by one outlier that
  matches the no-index trials' own background-noise pattern), confirmed by +7,001
  `idx_scan` (= exactly the files deleted per drain). Write cost ~0.1s per publication
  (2.511s -> 2.609s as the second index added, n=1 — see the write-cost precision note
  below). Space 33.1 MiB. Benefit exceeds cost.
- **`code_graph_relations_target_symbol_idx` — ADD, narrowest margin.** Alone it drops
  the drain from the 6.17s no-index mean to 2.093s; its marginal contribution on top of
  the other two is
  smaller (1.431s -> 1.097s, ~0.33s), and it is the largest of the three (57.4 MiB,
  roughly double the others) because `target_symbol_id` is set on more relation rows than
  `source_symbol_id`. It is also the constraint actually named in every reverse-order
  violation (`code_graph_relations_target_symbol_fk`) and absorbs +14,001 real `idx_scan`
  per drain that would otherwise fall on the primary key. Benefit still exceeds cost, by
  the narrowest margin of the three.

**Why the plan's own expectation was wrong, with the mechanism:** relations for a
snapshot are indeed gone (as live rows) by the time its symbols and files are deleted in
the shipped order. But "gone" means dead tuples, not absent index entries, and the
primary key's `(iwiki_id, domain_id, snapshot_id, …)` prefix only bounds the resulting
scan to *one snapshot's* dead-tuple range — it does not turn the search for "does any
live row reference this symbol/file" into a direct seek. That residual scan, repeated
once per deleted symbol (14,000x) and file (7,001x) per drain, is exactly what the
candidate indexes remove, and `idx_scan` deltas plus the unchanged primary-key scan count
prove the mechanism rather than merely correlating with it.

### Recorded, not acted on (Minor)

- **Write-cost precision.** The per-index write-cost deltas above (2.499s -> 2.511s
  adding `source_symbol_idx`; 2.511s -> 2.609s adding `source_file_idx`) are each a
  single publication (n=1), and both sit inside the ~2.32-2.69s spread the Setup section
  already recorded across the 30 baseline publications with no index at all. At that
  precision the write cost is not resolvable from zero — the numbers are consistent with
  a real small overhead and also consistent with ordinary run-to-run variance. This cuts
  in favour of ADD (a write cost too small to resolve is, at worst, negligible), so no
  decision changes; stated here so the specific deltas aren't read as more precise than
  they are.
- **Partial index not evaluated.** A partial index on `target_symbol_idx`
  (`WHERE target_symbol_id IS NOT NULL`) was never measured, even though that candidate
  is both the narrowest-margin and the largest-footprint of the three. The plan named
  exactly three candidates (the three plain composite indexes above); a partial-index
  variant is outside that enumeration, not a deviation from it. Worth a follow-up
  measurement if `target_symbol_idx`'s ~57.4 MiB (scaling with the domain) becomes a real
  concern later.

## Migration

Added as schema version 9 in `src/iwiki_mcp/postgres/migrations.py`
(`CODE_GRAPH_RELATIONS_FK_INDEX_MIGRATION`), a plain (non-`CONCURRENTLY`) `CREATE INDEX`
for each of the three columns — `CREATE INDEX CONCURRENTLY` cannot run inside a
transaction block, and `run_migrations` applies every pending migration in one. That
plain `CREATE INDEX` holds a `SHARE` lock blocking every write to `code_graph_relations`
for the duration of the whole migration transaction, not just the index build — see the
write-blocking window paragraph above the results table, and `docs/deployment.md`, for
the operational consequence and its two mitigations (a conservative upper-bound build
time, and `lock_timeout_ms` aborting cleanly rather than queuing). Deploying version 9
against production is Task 10 (human checkpoint) and is out of scope here.

`rollback_v9_compatibility` was added alongside it (restores version 8 by dropping the
three indexes), matching the existing per-version rollback convention (`rollback_v5`
through `rollback_v8` in the same file) so the existing chained-rollback test flows
(`run_migrations()` then step back one version at a time) keep working. No
`SCHEMA9_COMPATIBILITY_ROLLBACK_SQL` raw-SQL constant was added: that pattern only backs
version 5's separate "pre-v5-runtime compatibility image" tooling, which nothing about an
index-only migration needs.

## Verification

```bash
uv run pytest -q -m "not slow"    # 3618 passed, 0 failed
uv run flake8 src tests           # clean
```

```bash
docker run -d --name iwiki-pgtest-mine -e POSTGRES_PASSWORD=pgtest -e POSTGRES_DB=iwiki_test \
    -p 127.0.0.1:55436:5432 pgvector/pgvector:pg16
docker exec iwiki-pgtest-mine psql -U postgres -d iwiki_test -c "CREATE EXTENSION IF NOT EXISTS vector;"
IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55436/iwiki_test" uv run pytest -q tests/postgres
docker rm -f iwiki-pgtest-mine
```

`tests/postgres`: 574 passed, 2 failed — both
`test_direct_postgres_failure_preserves_active_revision_and_redacts[batch]` and
`[finalize]`, pre-existing and independently reproduced against `944a4db` (pre-Task-3)
in Task 6's own verification; unrelated to this change. A dedicated third disposable
container was used for this run rather than reusing the benchmark database (which still
held the measurement evidence above at the time) or the shared `iwiki-pgtest` (not this
task's to use).

Both benchmark containers (`iwiki-pgbench`, `iwiki-pgtest-mine`) were removed after use.
`iwiki-pgtest` (port 55432, not this task's) was never started, stopped, or written to.

## Fix round 1

Review verdict: measurement Approved — index column sets confirmed to match the composite
FK definitions exactly, trial distributions confirmed non-overlapping at the individual-
observation level, and the `idx_scan`-deltas-matching-cardinality-with-PK-flat evidence
confirmed genuinely causal. The falsified expectation stands. Two Important findings, both
about the deployment story, closed below; one Minor number correction; two further Minors
recorded, not acted on.

**Important 1 — `docs/deployment.md` denied a rollback that exists.** The paragraph said
version 9 "ships no compatibility rollback," when `rollback_v9_compatibility` ships in the
same commit and four test flows call it. Corrected to name it, following the paragraph's
own convention of naming the function for every other version ("Stepping back is
`rollback_v8_compatibility`..."). The qualifier this report already stated correctly — no
separate `SCHEMA9_COMPATIBILITY_ROLLBACK_SQL` raw-SQL artifact — is preserved in the
corrected text so the accurate distinction (no raw-SQL artifact, not no rollback) survives
in both places.

**Important 2 — the write-blocking window was stated nowhere.** Added to both
`docs/deployment.md` and this report (the paragraph above the results table, and a pointer
in the Migration section): plain `CREATE INDEX` takes a `SHARE` lock on
`code_graph_relations` blocking every INSERT/UPDATE/DELETE — every publication batch and
every cleanup drain — for the duration of the whole migration transaction, not just the
index build; measured 1.0-1.4s at ~630k rows is a conservative *upper* bound for the
shipped statement at ~1M rows (single heap-scan-and-sort, no second pass, no wait for
concurrent transactions); `lock_timeout_ms = 5000` aborts the migration cleanly rather than
queuing behind an in-flight publication. The results table's `CREATE INDEX CONCURRENTLY`
column is now labeled "measurement-only, does not ship."

**Minor, fixed — wrong mean in shipped source.** `mean 7.165` was the four repeats only
`(4.575+4.469+14.385+5.230)/4`, excluding the first trial (`3.068`) and the cache-warmth
control (`5.278`), both of which are equally valid no-index observations of the same
operation. All six: `3.068, 4.575, 4.469, 14.385, 5.230, 5.278`, sum 37.005, **mean
6.17s** — about 16% lower than the figure this report and the `migrations.py` migration
comment previously stated. Corrected in both places (`migrations.py:713` comment range
`4.5-14.4s` -> `3.1-14.4s (mean ~6.2s)`; every `7.165` in this report -> the pooled mean
or, where a specific delta was being computed, the corrected delta). No decision changes:
the separation argument was, and remains, the no-index minimum (3.068s, still nearly 3x
the indexed mean) and maximum (14.385s, still ~13x) against the indexed mean (1.097s), not
the no-index mean.

**Minor, recorded, not acted on.** Both noted in a new "Recorded, not acted on (Minor)"
subsection under Decision per candidate: the per-index write-cost deltas are n=1 and sit
inside the 30-publication baseline's own `2.32-2.69s` spread, so they are not resolvable
from zero at that precision (cuts in favour of ADD, no decision change); and a partial
index (`WHERE target_symbol_id IS NOT NULL`) on the narrowest-margin, largest-footprint
candidate was never evaluated — outside the plan's three enumerated candidates, so a
scoped-out gap rather than a deviation, flagged as a follow-up.

### Fix round 1 verification

```bash
uv run pytest -q -m "not slow"    # 3618 passed, 0 failed, 265 skipped
uv run flake8 src tests           # clean
```

No production logic changed in this round — only comments and documentation — so a
targeted (not full) `tests/postgres` re-run against a fresh throwaway container was used
to confirm the `migrations.py` comment edit didn't disturb anything mechanical:

```bash
docker run -d --name iwiki-pgtest-mine2 -e POSTGRES_PASSWORD=pgtest -e POSTGRES_DB=iwiki_test \
    -p 127.0.0.1:55437:5432 pgvector/pgvector:pg16
docker exec iwiki-pgtest-mine2 psql -U postgres -d iwiki_test -c "CREATE EXTENSION IF NOT EXISTS vector;"
IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55437/iwiki_test" uv run pytest -q \
    tests/postgres/test_migrations.py tests/postgres/test_code_graph_migrations.py \
    tests/postgres/test_code_graph_rollback.py tests/postgres/test_specification_migrations.py \
    tests/postgres/test_admin.py tests/postgres/test_http.py
docker rm -f iwiki-pgtest-mine2
```

`102 passed`. Container removed after use.
