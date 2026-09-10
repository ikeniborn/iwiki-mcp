---
topic: codegraph-index-job-handle
stage: spec
chain:
  intent: docs/superpowers/intents/2026-09-10-codegraph-index-job-handle-intent.md
---
# Design: code graph index job handle

## 1. Problem

`wiki_code_index` runs its build on the process's single background worker and waits for it inside the call. When that wait expires the call cancels the worker and answers `busy`. Cancellation is cooperative and checked only at phase boundaries and before publication, so a build cancelled during parsing keeps running for another minute, then ends `failed`. The rebuild is paid for and discarded; a retry pays it again.

Measured on this repository (113 files, 2150 symbols, 20513 relations, ~86 s full rebuild) with an identical 5 s wait:

| Behavior | Answer | Worker after the answer | Final state |
| --- | --- | --- | --- |
| Shipped (cancel on wait expiry) | `busy` in 5.01 s | ~75 s of further parsing | `failed` |
| Prototype (no cancel) | `rebuilding` in 5.01 s | the same ~75 s | `ready`, `fresh: true` |

The loss is bounded to one case: a build cancelled **before** it enters publication. A cancellation that arrives after publication entry already completes safely under the writer lock — `tests/codegraph/test_indexer_runtime.py:1438` pins exactly that, with the caller receiving `busy` while the worker reaches `ready`.

## 2. Acceptance (from intent)

Carried verbatim from `docs/superpowers/intents/2026-09-10-codegraph-index-job-handle-intent.md`.

Desired Outcomes:

- `wiki_code_index` returns a job descriptor in under one second on a repository of any size, without waiting for the build.
- A build that a call started reaches `ready` and publishes regardless of whether the caller waited for the answer; no rebuild is discarded because a client deadline expired.
- `wiki_code_status` reports the running job's phase and progress while it runs, and the same job's terminal outcome (`ready` or `failed`) after it finishes.
- While a job is live, a second `wiki_code_index` joins it instead of answering `busy` or starting a second build.

Done when: on this repository, `wiki_code_index` returns in under one second; `wiki_code_status` shows the job progressing and then reports `ready` with a fresh revision for the same run; a second `wiki_code_index` issued while the job runs joins it rather than answering `busy`; and the CLI publish path still exits 0 with an unchanged answer shape.

The first Desired Outcome is met through `wait_seconds=0`, which returns within the one-second grace of R4. The fourth is met for a job of the same domain and the same parameters (R6); joining a build the caller did not ask for would return a report of work it never requested.

## 3. Requirements

### R1 — the job is a first-class value in the worker registry

`_BuildJob` carries `job_id` (16 hex characters from `secrets.token_hex(8)`), `started_at`, `finished_at`, `state` (`running` → `ready` | `failed` | `cancelled`), the requested `force` and `languages`, and the phase marker of R3. `_BuildWorkerRegistry` keeps exactly one slot: the live job, or the last terminal job once its thread ends. `release()` no longer clears the slot; `start()` replaces a terminal job with a new one.

**DoD:** after a build finishes, the registry still answers with that job's id and terminal state; `active_count` and `is_active` remain driven by `thread.is_alive()` and report zero.

### R2 — the caller's wait no longer cancels the build

`wiki_code_index` waits up to `wait_seconds`, defaulting to `max_full_rebuild_seconds`. On expiry it returns the job descriptor and leaves the worker running. Cancellation survives in exactly three places: the build's own deadline (`max_full_rebuild_seconds`), the query-time auto-rebuild budget (`max_rebuild_seconds`), and server shutdown.

**DoD:** a build that outlives the caller's wait reaches `ready` and publishes; `tests/codegraph/test_indexer_runtime.py` gains a case proving the same run ends `ready` where it ends `failed` today.

### R3 — progress comes from an explicit phase marker

`BuildControl` records the phase the build is currently in, set where the build already stamps `phase = time.monotonic()`. Progress is read from that marker and from the phases it has already left, never inferred from `phase_timings_ms`: `_elapsed_ms` rounds to whole milliseconds and the timing map is pre-seeded with zeros, so a fast phase (`normalization: 0` on this repository) is indistinguishable from an unfinished one.

**DoD:** a build observed mid-flight reports a phase name from `_PHASE_NAMES`; a phase whose measured duration rounds to 0 ms is still reported as left behind.

### R4 — a no-op still answers with its report

Even at `wait_seconds=0` the call waits a fixed 1 s grace before returning a descriptor. The no-op decision is made inside `build` (`indexer.py:1642`), so without the grace the common "graph is already current" case would answer with a descriptor and force a status poll. The benchmark gate gives the budget: `eval/code_graph/runner.py:72` requires `noop_ms < 200`.

**DoD:** with a current graph and `wait_seconds=0`, the answer is the full report carrying `no_op: true`, with no running job in it.

### R5 — the answer shapes

`wiki_code_index(force, languages, wait_seconds=None)`:

- the build finished inside the wait → today's full report, field for field, plus `job: {id, state: "ready"}`;
- the build is still running → `{"state": "rebuilding", "job": {"id", "state": "running", "started_at", "phase", "phases_done"}, "hint": …}`, and no publication block, because `tool_result()` attaches one only for `state: "ready"`;
- `wait_seconds` negative, or greater than the build deadline → a parameter error in the shape `query.py:220` already uses for `limit`, naming the field and its range.

`wiki_code_status()` gains a `job` block whenever the registry holds one: a live job reports `id`, `state: "running"`, `started_at`, `phase`, `phases_done`; a terminal job reports `id`, `state`, `started_at`, `finished_at`. A process that never ran a job reports no `job` key at all. Every other field of the status answer is unchanged.

**DoD:** the tool schema test admits exactly `{force, languages, wait_seconds}`; a status answer without a job in the registry is byte-identical to today's.

### R6 — joining is narrow

A second `wiki_code_index` joins the live job only when the domain key, `force`, and `languages` all match. A different domain, or different parameters, answers `busy` as it does today, and so does a writer held by another process (`Timeout` on the file lock).

**DoD:** two identical calls observe one `iwiki-code-graph-build` thread and one shared `job.id`; a call with a different `languages` receives `busy`; `tests/codegraph/test_indexer_runtime.py:2128` keeps passing unchanged.

### R7 — an explicit job keeps the server awake

`IdleTracker` treats a live job started by `wiki_code_index` as activity, so the stdio server does not shut down while it runs. A query-time auto-rebuild is not activity: search-driven rebuilds must not hold a server open indefinitely.

**DoD:** `wait_until_idle` does not return while an explicit job runs, and returns normally while only an auto-rebuild is in flight.

### R8 — the query path is untouched

`query_guard`'s bounded auto-rebuild keeps its budget, its cancellation, and `restore_prior_on_abort=True`. The flag exists for the abort it names, and search answers must not start leaving live builds behind.

**DoD:** the existing query-time rebuild tests pass without modification.

## 4. Architecture

Ownership is unchanged: `_BuildWorkerRegistry` owns the single worker, `CodeGraphRuntime` owns the deadline arithmetic, `server.py` owns the tool surface, and `application.index_and_publish` stays the one path both the tool and the CLI use.

The change adds one value (the job) to a component that already exists, and moves one decision (whether to cancel) from the caller's wait to the build's own deadline. No new module, no new store, no new tool.

```
wiki_code_index ─► application.index_and_publish ─► runtime.index
                                                      │
                                    wait ≤ wait_seconds│   (no cancel on expiry)
                                                      ▼
                                          _BUILD_WORKERS: one slot
                                          live job │ last terminal job
                                                      ▲
wiki_code_status ─────────────────────────────────────┘  (reads, never starts)
```

## 5. Known limits

Stated rather than hidden, because the intent's priority is trust.

- **The CLI gains nothing.** The worker thread is `daemon=True` and `iwiki-mcp code publish` is a short-lived process, so an uncancelled job dies with it. The CLI keeps its synchronous behavior and its exit codes; the job handle is a benefit of the long-lived stdio server only.
- **`cancelled` is observable only while the process lives.** A job cancelled by the build deadline reports that state; a job killed by shutdown cannot report anything, and the next process reads the graph's own metadata instead.
- **A job does not survive the session.** The intent permits this. What must survive is truthfulness: a new process reports no `job` block rather than inventing one, and the existing stale-metadata recovery keeps a dead `rebuilding` from being reported as live work.

## 6. Non-goals

- No new MCP tool, and no change to the registered surface beyond one optional parameter.
- No persistent job store, queue, or table.
- No progress estimate: only phases actually entered and left are reported.
- No change to publication atomicity, the writer lock, or the two canonical verification passes.
- No incremental indexing.

## 7. Testing

The suite must not depend on an 86-second rebuild. The repository already has the technique: `tests/codegraph/test_indexer_runtime.py:1425` slows publication through a monkeypatched `slow_publish`.

- **R2** — a slowed build with `wait_seconds=0`: the answer carries a running job; after `join_workers`, status is `ready`. Today the same shape ends `failed`.
- **R3** — a build paused mid-phase reports a phase name; a zero-millisecond phase is reported as left behind.
- **R4** — a current graph with `wait_seconds=0` answers `no_op: true` inside the grace.
- **R5** — schema properties are exactly `{force, languages, wait_seconds}`; an out-of-range `wait_seconds` is refused by the parameter error; a status answer with an empty registry is unchanged.
- **R6** — two identical calls share one job id and one thread; a differing `languages` gets `busy`; the existing three-caller busy test is untouched.
- **R7** — `wait_until_idle` blocks on an explicit job and does not block on an auto-rebuild.
- **R8** — the query-time rebuild tests are unmodified and green.
- Full `uv run pytest -q` plus `tests/postgres` against a disposable database, and `flake8` on every touched file.
