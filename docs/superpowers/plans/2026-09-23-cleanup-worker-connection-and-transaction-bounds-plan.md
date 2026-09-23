---
review:
  plan_hash: 74ae992cdc04740f
  last_run: 2026-09-23
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings:
    - id: F-001
      phase: coverage
      severity: WARNING
      section: Self-Review
      section_hash: null
      fragment: "R14 - constraint only, no change, asserted by the unchanged default"
      text: "R14 was covered by no plan step. The self-review noticed the absence and rationalised it instead of closing it, which is the failure mode the gate exists to catch."
      fix: "Task 4 gained a step pinning superseded_cleanup_limit's default with a signature test, so the requirement has a step like every other."
      verdict: fixed
      verdict_at: 2026-09-23
    - id: F-002
      phase: coverage
      severity: WARNING
      section: "Task 1"
      section_hash: null
      fragment: "code graph cleanup finished one domain, %s rows removed"
      text: "R19 requires rows removed and elapsed time per completed work item. The runtime logged only rows, so half the requirement was implemented nowhere in the plan."
      fix: "The worker now times each job and logs the elapsed seconds on both the success and failure paths, with a test asserting both parts appear."
      verdict: fixed
      verdict_at: 2026-09-23
    - id: F-003
      phase: coverage
      severity: INFO
      section: "Task 7"
      section_hash: null
      fragment: "cleanup_sweep_due"
      text: "R19 requires throttle skips to stay at debug, and the rewritten cleanup_sweep_due logged nothing at all."
      fix: "The rewrite logs the skip at debug when the interval has not expired."
      verdict: fixed
      verdict_at: 2026-09-23
    - id: F-004
      phase: dependencies
      severity: CRITICAL
      section: "Task 3"
      section_hash: null
      fragment: "If tests/postgres/test_code_graph_publication.py has no _store helper"
      text: "The plan invented _store and _seed_superseded_snapshot, including raw SQL backdating of ready_at, while the suite already provides the pg_graph fixture with advance_clock, _snapshot_states and _snapshot_rows. Worse, it broke three existing tests without saying so: two call the removed _run_cleanup_cycle and _prune_superseded, and one monkeypatches the deleted _schedule_cleanup."
      fix: "Task 3 adds _store_with_factory and _aged_superseded once, built on the real fixture; Tasks 4-6 use them. Task 4 gained an explicit step migrating all three existing call sites."
      verdict: fixed
      verdict_at: 2026-09-23
    - id: F-005
      phase: coverage
      severity: CRITICAL
      section: "Task 5"
      section_hash: null
      fragment: "guards on NOT EXISTS over all four child tables"
      text: "R15's four-table guard defends a state the foreign keys forbid: code_graph_files roots the child chain, so an empty files implies every other child table is empty. Three of the four tests specified as its DoD cannot be constructed, which is how the error surfaced."
      fix: "Drift returned to the earliest gate. The intent's Objective and the spec's section 1 and R15 were corrected and both re-gated; issue 104 item 4 is recorded as mistaken. Task 5 now keeps the single guard and adds a catalogue-reading test that pins the invariant."
      verdict: fixed
      verdict_at: 2026-09-23
  chain:
    intent:
      path: docs/superpowers/intents/2026-09-23-cleanup-worker-connection-and-transaction-bounds-intent.md
      intent_hash: 9411ca5227c39205
    spec:
      path: docs/superpowers/specs/2026-09-23-cleanup-worker-connection-and-transaction-bounds-design.md
      spec_hash: fb769ff4442844b2
---
# Cleanup worker connection and transaction bounds Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bound the code-graph cleanup worker's threads, connections and transactions, so a growing number of clients queues work instead of exhausting `max_connections`, and a kill loses one batch instead of a whole prune.

**Architecture:** A maintenance runtime — a dedicated connection pool, a bounded deduplicated queue, and a fixed worker set — replaces the two ad-hoc guard sets and the two unbounded `threading.Thread` call sites. A cleanup cycle holds one connection for its duration and commits per batch, which makes a partially drained snapshot durable, which is why each batch re-checks that the snapshot has not been reactivated. The snapshot-row guard is left alone: the schema already makes one clause sufficient, and this plan pins that invariant with a test instead of widening the guard.

**Tech Stack:** Python 3.12, `psycopg` 3, `psycopg_pool` 3.3, `queue.Queue`, `threading`, pytest (`asyncio_mode = "auto"`, `pythonpath = ["src"]`), flake8 at `max-line-length = 100`.

**Spec:** `docs/superpowers/specs/2026-09-23-cleanup-worker-connection-and-transaction-bounds-design.md` (`spec_hash` `fb769ff4442844b2`)

## Global Constraints

- `W == N` — worker count equals maintenance pool `max_size`. Starting value **2**, revised only by the R10a burst observation.
- Total server PostgreSQL connections are `pool_max_size + N`. `pool_max_size` is 10 today, so the ceiling is **12**.
- The authentication reserve is untouchable: cleanup never draws from the hosted pool and never raises `_TOOL_LIMITER.total_tokens`.
- Child rows are deleted explicitly, children before parents. No `ON DELETE CASCADE` substitution, and no parent row removed while any child row of that snapshot survives.
- Cleanup never affects its caller: a failure, a full queue, or a wait never delays or fails a publication or an ordinary tool call.
- One store, one domain: no predicate reaches beyond the domain the store was constructed and principal-validated for.
- `_CLEANUP_BATCH_ROWS = 10000` and `_CLEANUP_CYCLE_ROWS = 200000` keep their current values.
- `superseded_cleanup_limit` keeps its default of `2` and its meaning as the candidate-selection page size.
- Every repository change bumps the version in **four** places: `pyproject.toml`, `src/iwiki_mcp/__init__.py`, `tests/test_package.py`, and `uv.lock`. Patch bump by default. Current version is `0.7.297`.
- `flake8` must stay clean: `uv run flake8 src tests`.
- Comments and documentation are written in English.

**One consequence not stated in the spec, decided here.** Cleanup currently opens `psycopg.connect(dsn)` with no options, so it runs with **no `statement_timeout`** — which is how a single delete once ran for 49 minutes. The maintenance pool passes the same `options` string as the hosted pool, so every cleanup statement inherits `statement_timeout=30000`. This is deliberate: a single 10,000-row batch that cannot finish in 30 seconds is a batch that is too large, and failing it loudly is better than a statement nothing bounds. Task 2 states it in the pool's docstring.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/iwiki_mcp/codegraph/maintenance.py` | **New.** The maintenance runtime: `CleanupJob`, `MaintenanceRuntime`, `open_maintenance_pool`. Framework-free, imports nothing from `application.py`, so the runner is injected rather than imported. |
| `src/iwiki_mcp/postgres/codegraph.py` | The store. `_connection` / `_transaction_on` split, the restructured cleanup cycle, the four-way guard, the reactivation re-check. Loses `_schedule_cleanup` and `_cleanup_active`. |
| `src/iwiki_mcp/postgres/store.py` | `validate_direct_principal` gains an optional `connection_factory`. |
| `src/iwiki_mcp/codegraph/application.py` | Loses `_sweep_wiki_cleanup`, `_SWEEP_ACTIVE`. Gains `run_cleanup_job`, the injected runner. `schedule_wiki_cleanup` enqueues instead of spawning. |
| `src/iwiki_mcp/server.py` | `_install_hosted_runtime` / `_clear_hosted_runtime` own the maintenance runtime's lifecycle. |
| `src/iwiki_mcp/http.py` | Passes the DSN and options into `_install_hosted_runtime`. |
| `tests/test_maintenance_runtime.py` | **New.** Queue, dedup, bounded drop, worker lifecycle, shutdown. No database. |
| `tests/postgres/test_code_graph_publication.py` | Guard tests, resumability test, reactivation test, connection-count test. |
| `docs/code-graph-publishing.md`, `docs/code-graph-publishing.ru.md` | The corrected batch/commit claim and the connection arithmetic. |

---

### Task 1: The maintenance runtime — bounded deduplicated queue and worker set

Implements R1 (partly), R2, R4, R5, R20, R21, and the queue-drop half of R19.

**Files:**
- Create: `src/iwiki_mcp/codegraph/maintenance.py`
- Test: `tests/test_maintenance_runtime.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `MAINTENANCE_WORKERS: int = 2`
  - `MAINTENANCE_QUEUE_SIZE: int = 256`
  - `CleanupJob(iwiki_id: str, domain: str, binding, owner_id: str, settings, lock_timeout_ms: int)`, frozen dataclass, with `key -> tuple[str, str]`
  - `MaintenanceRuntime(runner, *, workers=MAINTENANCE_WORKERS, queue_size=MAINTENANCE_QUEUE_SIZE, pool=None)` with `start() -> None`, `submit(job: CleanupJob) -> bool`, `stop(timeout: float = 10.0) -> None`, `dropped: int`
  - The runner contract: `runner(job: CleanupJob, connection_factory) -> int`, returning rows removed. `connection_factory` is `None` when no pool is attached.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_maintenance_runtime.py`:

```python
"""The bounds that keep maintenance from growing with the client count."""

from __future__ import annotations

import threading
import time

from iwiki_mcp.codegraph import maintenance


def _job(domain: str = "personal-ai-wiki", iwiki_id: str = "personal"):
    return maintenance.CleanupJob(
        iwiki_id=iwiki_id,
        domain=domain,
        binding=object(),
        owner_id="token-1",
        settings=object(),
        lock_timeout_ms=5000,
    )


def test_a_queued_key_is_not_queued_twice():
    runtime = maintenance.MaintenanceRuntime(runner=lambda job, factory: 0)

    assert runtime.submit(_job()) is True
    assert runtime.submit(_job()) is False


def test_a_different_domain_is_its_own_key():
    runtime = maintenance.MaintenanceRuntime(runner=lambda job, factory: 0)

    assert runtime.submit(_job(domain="a")) is True
    assert runtime.submit(_job(domain="b")) is True


def test_a_full_queue_drops_rather_than_grows():
    """Maintenance is droppable; memory is not."""
    runtime = maintenance.MaintenanceRuntime(
        runner=lambda job, factory: 0, queue_size=2
    )

    assert runtime.submit(_job(domain="a")) is True
    assert runtime.submit(_job(domain="b")) is True
    assert runtime.submit(_job(domain="c")) is False
    assert runtime.dropped == 1


def test_a_dropped_key_is_released_for_a_later_request():
    runtime = maintenance.MaintenanceRuntime(
        runner=lambda job, factory: 0, queue_size=1
    )
    runtime.submit(_job(domain="a"))
    runtime.submit(_job(domain="b"))

    assert runtime.submit(_job(domain="b")) is False, "still dropped, not stuck"
    assert runtime._scheduled == {("personal", "a")}


def test_a_worker_runs_the_job_and_releases_its_key():
    done = threading.Event()
    seen = []

    def runner(job, factory):
        seen.append(job.key)
        done.set()
        return 7

    runtime = maintenance.MaintenanceRuntime(runner=runner)
    runtime.start()
    try:
        runtime.submit(_job())
        assert done.wait(timeout=5), "worker never ran the job"
        deadline = time.monotonic() + 5
        while runtime._scheduled and time.monotonic() < deadline:
            time.sleep(0.01)
        assert runtime._scheduled == set()
    finally:
        runtime.stop()

    assert seen == [("personal", "personal-ai-wiki")]


def test_a_failing_job_does_not_kill_its_worker():
    calls = []
    second = threading.Event()

    def runner(job, factory):
        calls.append(job.domain)
        if job.domain == "boom":
            raise RuntimeError("secret-bearing failure")
        second.set()
        return 0

    runtime = maintenance.MaintenanceRuntime(runner=runner, workers=1)
    runtime.start()
    try:
        runtime.submit(_job(domain="boom"))
        runtime.submit(_job(domain="after"))
        assert second.wait(timeout=5), "worker died on the first failure"
    finally:
        runtime.stop()

    assert calls == ["boom", "after"]


def test_stop_joins_every_worker_even_with_a_full_queue():
    """The sentinel must always fit, or a bounded queue strands the workers."""
    release = threading.Event()

    runtime = maintenance.MaintenanceRuntime(
        runner=lambda job, factory: release.wait(timeout=5) or 0,
        workers=2,
        queue_size=2,
    )
    runtime.start()
    workers = list(runtime._threads)
    assert len(workers) == 2
    runtime.submit(_job(domain="a"))
    runtime.submit(_job(domain="b"))

    release.set()
    runtime.stop(timeout=5)

    assert runtime._threads == []
    assert [t for t in workers if t.is_alive()] == [], "a worker outlived stop"


def test_submit_after_stop_is_refused():
    runtime = maintenance.MaintenanceRuntime(runner=lambda job, factory: 0)
    runtime.start()
    runtime.stop()

    assert runtime.submit(_job()) is False


def test_start_is_idempotent():
    runtime = maintenance.MaintenanceRuntime(runner=lambda job, factory: 0)
    runtime.start()
    try:
        runtime.start()
        assert len(runtime._threads) == maintenance.MAINTENANCE_WORKERS
    finally:
        runtime.stop()


def test_a_completed_job_reports_rows_and_elapsed_time(caplog):
    """R19 asks for both; rows alone cannot show a worker running long."""
    runtime = maintenance.MaintenanceRuntime(
        runner=lambda job, factory: 4321, workers=1
    )
    runtime.start()
    try:
        with caplog.at_level("INFO", logger=maintenance.LOGGER.name):
            runtime.submit(_job())
            deadline = time.monotonic() + 5
            while "rows removed" not in caplog.text and time.monotonic() < deadline:
                time.sleep(0.01)
    finally:
        runtime.stop()

    assert "4321 rows removed" in caplog.text
    assert "s" in caplog.text.split("rows removed in ")[1][:6]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_maintenance_runtime.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'iwiki_mcp.codegraph.maintenance'`

- [ ] **Step 3: Write the implementation**

Create `src/iwiki_mcp/codegraph/maintenance.py`:

```python
"""Bounded background maintenance for the code graph.

Cleanup used to spawn one unbounded `threading.Thread` per `(iwiki_id,
domain)` and open its connections outside every pool, so neither its threads
nor its connections were counted anywhere. As the number of clients grows,
that shape converts new tenants directly into new threads and new server
connections. A fixed worker set consuming a bounded queue converts them into
queueing instead, which is the property this module exists to provide.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import queue
import threading
import time
from typing import Callable

LOGGER = logging.getLogger(__name__)

# One worker holds at most one connection at a time, so the worker count and
# the maintenance pool's size are the same number. Two is a starting value,
# revised only from the publication-burst observation the design requires.
MAINTENANCE_WORKERS = 2

# Maintenance work is droppable: it returns on a later request. The bound is
# what keeps a growing client count from growing memory instead of queueing.
MAINTENANCE_QUEUE_SIZE = 256

_SHUTDOWN = object()


@dataclass(frozen=True)
class CleanupJob:
    """One cleanup cycle: exactly one domain of exactly one wiki.

    The binding travels with the job because it is the mandate the cycle
    runs under, not a convenience: a store may only touch the domain it was
    constructed and principal-validated for.
    """

    iwiki_id: str
    domain: str
    binding: object
    owner_id: str
    settings: object
    lock_timeout_ms: int

    @property
    def key(self) -> tuple[str, str]:
        return (self.iwiki_id, self.domain)


class MaintenanceRuntime:
    """A fixed worker set draining a bounded, deduplicated queue."""

    def __init__(
        self,
        runner: Callable[[CleanupJob, object], int],
        *,
        workers: int = MAINTENANCE_WORKERS,
        queue_size: int = MAINTENANCE_QUEUE_SIZE,
        pool=None,
    ) -> None:
        self._runner = runner
        self._workers_wanted = workers
        self._pool = pool
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._lock = threading.Lock()
        self._scheduled: set[tuple[str, str]] = set()
        self._threads: list[threading.Thread] = []
        self._stopping = False
        self.dropped = 0

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        """Start the workers once; a second call is a no-op."""
        with self._lock:
            if self._threads or self._stopping:
                return
            for index in range(self._workers_wanted):
                thread = threading.Thread(
                    target=self._work,
                    name=f"iwiki-code-graph-maintenance-{index}",
                    daemon=True,
                )
                self._threads.append(thread)
            threads = list(self._threads)
        for thread in threads:
            thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Drain pending work, send one sentinel per worker, then join.

        The drain is not tidiness: the queue is bounded, so a full queue
        would leave no room for the sentinels and every worker would outlive
        the runtime that owns it.
        """
        with self._lock:
            self._stopping = True
            threads = list(self._threads)
        while True:
            try:
                pending = self._queue.get_nowait()
            except queue.Empty:
                break
            if pending is not _SHUTDOWN:
                with self._lock:
                    self._scheduled.discard(pending.key)
            self._queue.task_done()
        for _ in threads:
            try:
                self._queue.put_nowait(_SHUTDOWN)
            except queue.Full:  # pragma: no cover - the drain just made room
                break
        for thread in threads:
            thread.join(timeout=timeout)
        with self._lock:
            self._threads = []
            self._scheduled.clear()

    # -- submission -----------------------------------------------------

    def submit(self, job: CleanupJob) -> bool:
        """Queue one job. False means deduplicated, dropped, or stopping."""
        with self._lock:
            if self._stopping:
                return False
            if job.key in self._scheduled:
                LOGGER.debug(
                    "code graph cleanup already scheduled for this domain"
                )
                return False
            self._scheduled.add(job.key)
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            with self._lock:
                self._scheduled.discard(job.key)
                self.dropped += 1
                dropped = self.dropped
            LOGGER.warning(
                "code graph cleanup dropped, maintenance queue full "
                "(%s dropped so far)",
                dropped,
            )
            return False
        return True

    # -- worker ---------------------------------------------------------

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _SHUTDOWN:
                    return
                self._run(item)
            finally:
                self._queue.task_done()

    def _run(self, job: CleanupJob) -> None:
        factory = self._pool.connection if self._pool is not None else None
        started = time.monotonic()
        try:
            removed = self._runner(job, factory)
        except Exception as exc:  # noqa: BLE001 - maintenance must not escape
            LOGGER.warning(
                "code graph cleanup failed for one domain after %.1fs: %s",
                time.monotonic() - started,
                type(exc).__name__,
            )
        else:
            LOGGER.info(
                "code graph cleanup finished one domain, "
                "%s rows removed in %.1fs",
                removed,
                time.monotonic() - started,
            )
        finally:
            with self._lock:
                self._scheduled.discard(job.key)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_maintenance_runtime.py -q`
Expected: PASS, 9 passed

- [ ] **Step 5: Lint**

Run: `uv run flake8 src/iwiki_mcp/codegraph/maintenance.py tests/test_maintenance_runtime.py`
Expected: no output

- [ ] **Step 6: Commit**

```bash
git add src/iwiki_mcp/codegraph/maintenance.py tests/test_maintenance_runtime.py
git commit -m "feat(codegraph): add the bounded maintenance runtime"
```

---

### Task 2: The maintenance connection pool and its statistics

Implements R2's pool half, R8's source half, and R19's pool-statistics half.

**Files:**
- Modify: `src/iwiki_mcp/codegraph/maintenance.py`
- Test: `tests/test_maintenance_runtime.py`

**Interfaces:**
- Consumes: `MAINTENANCE_WORKERS`, `MaintenanceRuntime` from Task 1.
- Produces:
  - `open_maintenance_pool(dsn: str, *, options: str, workers: int = MAINTENANCE_WORKERS) -> ConnectionPool`
  - `MaintenanceRuntime.log_pool_stats() -> None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_maintenance_runtime.py`:

```python
class _FakePool:
    def __init__(self, stats):
        self._stats = stats
        self.max_size = 2

    def connection(self):  # pragma: no cover - identity is all that matters
        raise AssertionError("not called in this test")

    def get_stats(self):
        return self._stats


def test_the_runner_receives_the_pools_connection_factory():
    pool = _FakePool({})
    seen = []
    runtime = maintenance.MaintenanceRuntime(
        runner=lambda job, factory: seen.append(factory) or 0,
        workers=1,
        pool=pool,
    )
    runtime.start()
    try:
        runtime.submit(_job())
        deadline = time.monotonic() + 5
        while not seen and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        runtime.stop()

    assert seen == [pool.connection]


def test_the_runner_receives_no_factory_without_a_pool():
    seen = []
    runtime = maintenance.MaintenanceRuntime(
        runner=lambda job, factory: seen.append(factory) or 0, workers=1
    )
    runtime.start()
    try:
        runtime.submit(_job())
        deadline = time.monotonic() + 5
        while not seen and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        runtime.stop()

    assert seen == [None]


def test_pool_statistics_reach_the_log(caplog):
    """Exhaustion must be diagnosable from the log alone."""
    pool = _FakePool(
        {
            "pool_size": 2,
            "pool_available": 0,
            "requests_waiting": 3,
            "requests_wait_ms": 1250,
        }
    )
    runtime = maintenance.MaintenanceRuntime(
        runner=lambda job, factory: 0, pool=pool
    )

    with caplog.at_level("INFO", logger=maintenance.LOGGER.name):
        runtime.log_pool_stats()

    message = caplog.text
    assert "available=0" in message
    assert "waiting=3" in message
    assert "wait_ms=1250" in message


def test_the_pool_is_sized_to_the_worker_count():
    pool = maintenance.open_maintenance_pool(
        "postgresql://localhost/iwiki_not_opened",
        options="-c statement_timeout=30000",
        workers=3,
    )
    try:
        assert pool.max_size == 3
        assert pool.min_size == 0
    finally:
        pool.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_maintenance_runtime.py -q -k "pool or factory"`
Expected: FAIL — `AttributeError: module 'iwiki_mcp.codegraph.maintenance' has no attribute 'open_maintenance_pool'`

- [ ] **Step 3: Write the implementation**

Add the import at the top of `src/iwiki_mcp/codegraph/maintenance.py`, after `import queue`:

```python
from psycopg_pool import ConnectionPool
```

Add the factory below `MAINTENANCE_QUEUE_SIZE`:

```python
def open_maintenance_pool(
    dsn: str, *, options: str, workers: int = MAINTENANCE_WORKERS
) -> ConnectionPool:
    """Open the pool cleanup draws from, and nothing else does.

    `min_size=0` keeps an idle server holding no maintenance backend at all,
    and `open(wait=False)` keeps startup from blocking on one. `max_size` is
    the worker count: a worker holds at most one connection, so the pool is a
    hard ceiling that holds even if one ever takes a second.

    The options string is the hosted server's own, so a cleanup statement
    inherits `statement_timeout`. That is deliberate. Cleanup previously
    connected with no options and therefore no statement timeout, which is
    how one delete once ran for 49 minutes; a 10,000-row batch that cannot
    finish inside the timeout is a batch that is too large, and failing it
    loudly beats a statement nothing bounds.
    """
    pool = ConnectionPool(
        dsn,
        min_size=0,
        max_size=workers,
        kwargs={"options": options},
        name="iwiki-maintenance",
        open=False,
    )
    pool.open(wait=False)
    return pool
```

Add the statistics method to `MaintenanceRuntime`, directly below `submit`:

```python
    def log_pool_stats(self) -> None:
        """Report connection use so exhaustion needs no database query."""
        if self._pool is None:
            return
        stats = self._pool.get_stats()
        LOGGER.info(
            "code graph maintenance pool: size=%s available=%s "
            "waiting=%s wait_ms=%s dropped=%s",
            stats.get("pool_size", 0),
            stats.get("pool_available", 0),
            stats.get("requests_waiting", 0),
            int(stats.get("requests_wait_ms", 0)),
            self.dropped,
        )
```

Call it from `_run`, immediately before the `try:`:

```python
    def _run(self, job: CleanupJob) -> None:
        self.log_pool_stats()
        factory = self._pool.connection if self._pool is not None else None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_maintenance_runtime.py -q`
Expected: PASS, 13 passed

- [ ] **Step 5: Lint**

Run: `uv run flake8 src/iwiki_mcp/codegraph/maintenance.py tests/test_maintenance_runtime.py`
Expected: no output

- [ ] **Step 6: Commit**

```bash
git add src/iwiki_mcp/codegraph/maintenance.py tests/test_maintenance_runtime.py
git commit -m "feat(codegraph): give maintenance its own pool and connection statistics"
```

---

### Task 3: Split the store's connection lifetime from its transaction lifetime

Implements R11.

**Files:**
- Modify: `src/iwiki_mcp/postgres/codegraph.py:164-172`
- Modify: `tests/postgres/test_code_graph_publication.py:218-234` (the `_CountingConnection` double)
- Test: `tests/postgres/test_code_graph_publication.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces on `PostgresCodeGraphStore`:
  - `_connection() -> ContextManager[psycopg.Connection]`
  - `_transaction_on(connection) -> ContextManager[psycopg.Cursor]`
  - `_transaction() -> ContextManager[psycopg.Cursor]`, unchanged for every existing caller

**The detail that will bite you if you skip it.** `AuthStore` and `postgres/store.py` already declare `connection_factory: Callable[[], ContextManager[Any]]` and consume it as `with self._connect() as connection:`. The code-graph store is the odd one out: it declares `Callable[[], psycopg.Connection]` and calls `.close()` explicitly. This task aligns it with the established pattern, because a pooled `pool.connection` is a context manager and cannot be `.close()`d. `_CountingConnection` in the test suite proxies through `__getattr__`, and Python resolves `__enter__`/`__exit__` on the type rather than the instance, so the double needs both methods added explicitly or the whole publication test fails with `TypeError: 'ise not a context manager`.

- [ ] **Step 1: Add the two shared helpers the later tasks also use**

The file has no store-construction helper — `test_activation_cost_is_bounded_by_row_kinds_not_row_count` builds one inline — and Tasks 4, 5 and 6 all need one with a custom factory. Add both helpers once, here, next to `_snapshot_rows`:

```python
def _store_with_factory(graph, factory):
    """A store on the fixture's wiki and clock, with a factory of our own.

    The clock matters: `advance_clock` is how the suite ages a snapshot past
    the retention window, and a store with the real clock would never see a
    candidate.
    """
    from iwiki_mcp.postgres.codegraph import PostgresCodeGraphStore

    return PostgresCodeGraphStore(
        graph.dsn,
        graph.iwiki_id,
        graph.domain,
        graph.owner_id,
        lock_timeout_ms=graph.lock_timeout_ms,
        session_ttl_seconds=graph.session_ttl_seconds,
        staging_retention_seconds=graph.staging_retention_seconds,
        staging_cleanup_limit=graph.staging_cleanup_limit,
        superseded_retention_seconds=graph.superseded_retention_seconds,
        superseded_cleanup_limit=graph.superseded_cleanup_limit,
        connection_factory=factory,
        clock=graph._now,
    )


def _aged_superseded(graph, publications: int = 3) -> str:
    """Publish repeatedly, age past the window, return the oldest snapshot."""
    for _ in range(publications):
        graph.finalize(graph.complete_session())
    graph.advance_clock(graph.superseded_retention_seconds + 1)
    return _snapshot_states(graph)[0][0]
```

- [ ] **Step 2: Write the failing test**

Add to `tests/postgres/test_code_graph_publication.py`:

```python
def test_one_connection_serves_many_transactions(pg_graph):
    """Committing per batch must not mean connecting per batch."""
    import psycopg

    opened = []

    def factory():
        opened.append(1)
        return psycopg.connect(pg_graph.dsn)

    store = _store_with_factory(pg_graph, factory)

    with store._connection() as connection:
        with store._transaction_on(connection) as cursor:
            cursor.execute("SELECT 1")
            assert cursor.fetchone()[0] == 1
        with store._transaction_on(connection) as cursor:
            cursor.execute("SELECT 2")
            assert cursor.fetchone()[0] == 2

    assert len(opened) == 1, "each transaction opened its own connection"
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55432/iwiki_test" uv run pytest tests/postgres/test_code_graph_publication.py::test_one_connection_serves_many_transactions -q`
Expected: FAIL — `AttributeError: 'PostgresCodeGraphStore' object has no attribute '_connection'`

Start the database first if it is not running:

```bash
docker run -d --name iwiki-pgtest -e POSTGRES_PASSWORD=pgtest -e POSTGRES_DB=iwiki_test -p 127.0.0.1:55432:5432 pgvector/pgvector:pg16
docker exec iwiki-pgtest psql -U postgres -d iwiki_test -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

- [ ] **Step 4: Replace `_transaction` in `src/iwiki_mcp/postgres/codegraph.py`**

Replace lines 164-172 with:

```python
    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection]:
        """One connection, many transactions.

        Cleanup commits per batch, so binding a connection to a single
        transaction would mean one connect per 10,000 rows — roughly ninety
        of them to drain a million. The factory yields a context manager, as
        `AuthStore` and `postgres/store.py` already require, so a pooled
        connection returns to its pool instead of being closed.
        """
        with self._connection_factory() as connection:
            yield connection

    @contextmanager
    def _transaction_on(
        self, connection: psycopg.Connection
    ) -> Iterator[psycopg.Cursor]:
        with connection.transaction():
            with connection.cursor() as cursor:
                yield cursor

    @contextmanager
    def _transaction(self) -> Iterator[psycopg.Cursor]:
        with self._connection() as connection:
            with self._transaction_on(connection) as cursor:
                yield cursor
```

Update the type annotation at line 134 from:

```python
        connection_factory: Callable[[], psycopg.Connection] | None = None,
```

to:

```python
        connection_factory: Callable[[], ContextManager[psycopg.Connection]] | None = None,
```

and add `ContextManager` to the `typing` import at the top of the file.

- [ ] **Step 5: Give the test double the context-manager protocol**

In `tests/postgres/test_code_graph_publication.py`, add these two methods to `_CountingConnection`, below `__repr__`:

```python
    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, *exc):
        return self._connection.__exit__(*exc)
```

- [ ] **Step 6: Run the affected tests**

Run: `IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55432/iwiki_test" uv run pytest tests/postgres/test_code_graph_publication.py -q`
Expected: PASS, every test in the file including the new one

- [ ] **Step 7: Run the fast suite to catch other callers**

Run: `uv run pytest -q`
Expected: PASS with the suite's known result; no new failures

- [ ] **Step 8: Commit**

```bash
git add src/iwiki_mcp/postgres/codegraph.py tests/postgres/test_code_graph_publication.py
git commit -m "refactor(codegraph): separate the store's connection lifetime from its transactions"
```

---

### Task 4: One connection per cycle, one transaction per batch

Implements R12 and R13.

**Files:**
- Modify: `src/iwiki_mcp/postgres/codegraph.py:659-826`
- Test: `tests/postgres/test_code_graph_publication.py`

**Interfaces:**
- Consumes: `_connection`, `_transaction_on` from Task 3.
- Produces on `PostgresCodeGraphStore`:
  - `run_cleanup_cycle(connection=None) -> int` — the public entry point, replacing `_run_cleanup_cycle`
  - `_drain(connection) -> int`
  - `_superseded_candidates(cursor, domain_id, now) -> list[str]`
  - `_drain_snapshot(connection, domain_id, snapshot_id, budget) -> int`
  - `_delete_batch(cursor, table, domain_id, snapshot_id, limit) -> int`
  - `_delete_snapshot_row(cursor, domain_id, snapshot_id) -> int`
  - `_prune_superseded` and `_delete_snapshot_rows` are removed.

- [ ] **Step 1: Write the failing test**

Add to `tests/postgres/test_code_graph_publication.py`:

The fixture publishes a few hundred rows, far below `_CLEANUP_BATCH_ROWS = 10000`, so a test that wants several batches must shrink the batch rather than grow the fixture.

```python
def test_a_kill_mid_drain_keeps_the_batches_already_committed(
    pg_graph, monkeypatch
):
    """The published claim is 'at most one batch'; make it true."""
    monkeypatch.setattr(type(pg_graph.store), "_CLEANUP_BATCH_ROWS", 2)
    oldest = _aged_superseded(pg_graph)
    before = _snapshot_rows(pg_graph, oldest)
    assert sum(before.values()) > 6, "fixture must supply several batches"

    store = pg_graph.store
    real_delete_batch = store._delete_batch
    calls = {"n": 0}

    def exploding(cursor, table, domain_id, sid, limit):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("killed mid-drain")
        return real_delete_batch(cursor, table, domain_id, sid, limit)

    monkeypatch.setattr(store, "_delete_batch", exploding)

    with pytest.raises(RuntimeError):
        store.run_cleanup_cycle()

    after = _snapshot_rows(pg_graph, oldest)
    removed = sum(before.values()) - sum(after.values())
    assert removed > 0, "the committed batches were rolled back with the kill"
    assert removed <= 4, "more than the two committed batches went missing"


def test_a_cycle_opens_exactly_one_connection(pg_graph):
    """One connection per cycle is what makes per-batch commits affordable."""
    import psycopg

    opened = []

    def factory():
        opened.append(1)
        return psycopg.connect(pg_graph.dsn)

    _aged_superseded(pg_graph)
    store = _store_with_factory(pg_graph, factory)

    store.run_cleanup_cycle()

    assert len(opened) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55432/iwiki_test" uv run pytest tests/postgres/test_code_graph_publication.py -q -k "kill_mid_drain or exactly_one_connection"`
Expected: FAIL — `AttributeError: 'PostgresCodeGraphStore' object has no attribute 'run_cleanup_cycle'`

- [ ] **Step 3: Replace the cleanup methods**

In `src/iwiki_mcp/postgres/codegraph.py`, delete `_prune_superseded` (lines 659-729), `_run_cleanup_cycle` (lines 763-799) and `_delete_snapshot_rows` (lines 801-826), and put this in their place. Leave `_schedule_cleanup` alone for now; Task 7 removes it.

```python
    _CLEANUP_CHILD_TABLES = (
        "code_graph_wiki_links",
        "code_graph_relations",
        "code_graph_symbols",
        "code_graph_files",
    )

    def run_cleanup_cycle(self, connection=None) -> int:
        """Drain this domain's superseded backlog and return rows removed.

        The caller may hand in a connection — a maintenance worker does, so
        the whole cycle rides one pooled connection — or let the cycle open
        its own for the local path.
        """
        if connection is not None:
            return self._drain(connection)
        with self._connection() as own:
            return self._drain(own)

    def _drain(self, connection) -> int:
        removed = 0
        with self._transaction_on(connection) as cursor:
            domain_id = self._domain_id(cursor)
        while removed < self._CLEANUP_CYCLE_ROWS:
            with self._transaction_on(connection) as cursor:
                candidates = self._superseded_candidates(
                    cursor, domain_id, self._clock()
                )
            if not candidates:
                break
            progressed = False
            for snapshot_id in candidates:
                if removed >= self._CLEANUP_CYCLE_ROWS:
                    break
                taken = self._drain_snapshot(
                    connection,
                    domain_id,
                    snapshot_id,
                    self._CLEANUP_CYCLE_ROWS - removed,
                )
                removed += taken
                progressed = progressed or taken > 0
            if not progressed:
                break
        LOGGER.info(
            "code graph cleanup removed %s rows from one domain", removed
        )
        return removed

    def _superseded_candidates(self, cursor, domain_id: int, now) -> list:
        """Ready snapshots that are no longer active and past the retention.

        The active snapshot is excluded by the query rather than by an
        ordering assumption, and the state predicate leaves staging and
        failed snapshots untouched.
        """
        threshold = now - datetime.timedelta(
            seconds=self._superseded_retention_seconds
        )
        cursor.execute(
            "SELECT s.snapshot_id FROM iwiki.code_graph_snapshots s "
            "JOIN iwiki.code_graph_domain_state d "
            "ON d.iwiki_id = s.iwiki_id AND d.domain_id = s.domain_id "
            "WHERE s.iwiki_id = %s AND s.domain_id = %s "
            "AND s.state = 'ready' AND s.ready_at <= %s "
            "AND s.snapshot_id IS DISTINCT FROM d.active_snapshot_id "
            "ORDER BY s.ready_at LIMIT %s",
            (
                self.iwiki_id,
                domain_id,
                threshold,
                self._superseded_cleanup_limit,
            ),
        )
        return [row[0] for row in cursor.fetchall()]

    def _drain_snapshot(
        self, connection, domain_id: int, snapshot_id: str, budget: int
    ) -> int:
        """Delete one snapshot's rows, children first, committing per batch.

        Each statement is keyed by `snapshot_id` so it rides the primary-key
        prefix. Letting the foreign keys cascade instead searches every child
        table once per deleted parent row, and those keys carry no index of
        their own beyond that prefix: one such prune ran for 49 minutes on a
        live domain and took the server with it.
        """
        removed = 0
        for table in self._CLEANUP_CHILD_TABLES:
            while removed < budget:
                with self._transaction_on(connection) as cursor:
                    taken = self._delete_batch(
                        cursor,
                        table,
                        domain_id,
                        snapshot_id,
                        min(self._CLEANUP_BATCH_ROWS, budget - removed),
                    )
                if not taken:
                    break
                removed += taken
        with self._transaction_on(connection) as cursor:
            removed += self._delete_snapshot_row(
                cursor, domain_id, snapshot_id
            )
        return removed

    def _delete_batch(
        self,
        cursor,
        table: str,
        domain_id: int,
        snapshot_id: str,
        limit: int,
    ) -> int:
        cursor.execute(
            f"DELETE FROM iwiki.{table} WHERE ctid IN ("
            f"SELECT ctid FROM iwiki.{table} "
            "WHERE iwiki_id = %s AND domain_id = %s "
            "AND snapshot_id = %s LIMIT %s)",
            (self.iwiki_id, domain_id, snapshot_id, limit),
        )
        return cursor.rowcount

    def _delete_snapshot_row(
        self, cursor, domain_id: int, snapshot_id: str
    ) -> int:
        cursor.execute(
            "DELETE FROM iwiki.code_graph_snapshots "
            "WHERE iwiki_id = %s AND domain_id = %s AND snapshot_id = %s "
            "AND state = 'ready' "
            "AND snapshot_id NOT IN ("
            "SELECT active_snapshot_id FROM iwiki.code_graph_domain_state "
            "WHERE iwiki_id = %s AND domain_id = %s "
            "AND active_snapshot_id IS NOT NULL) "
            "AND NOT EXISTS ("
            "SELECT 1 FROM iwiki.code_graph_files f "
            "WHERE f.iwiki_id = %s AND f.domain_id = %s "
            "AND f.snapshot_id = %s)",
            (
                self.iwiki_id,
                domain_id,
                snapshot_id,
                self.iwiki_id,
                domain_id,
                self.iwiki_id,
                domain_id,
                snapshot_id,
            ),
        )
        return cursor.rowcount
```

`_delete_snapshot_row` keeps its single `NOT EXISTS` over `code_graph_files`, which the corrected spec establishes is exactly sufficient. Task 5 adds the reactivation check and the test that pins the invariant behind that sufficiency.

- [ ] **Step 4: Point every existing caller at the new names**

Three call sites outside this task's own code use the methods it just removed. Leaving any of them is a red suite, not a later problem.

In `src/iwiki_mcp/codegraph/application.py`, inside `_sweep_wiki_cleanup`, change `store._run_cleanup_cycle()` to `store.run_cleanup_cycle()`. Task 7 deletes that function entirely; this keeps the tree green in between.

In `tests/postgres/test_code_graph_publication.py`, `test_a_superseded_snapshot_is_pruned_once_it_leaves_the_window` calls `store._run_cleanup_cycle()`. Rename it to `store.run_cleanup_cycle()` and update the comment above it, which still says `begin()` schedules a daemon thread — after Task 7 it schedules nothing.

In the same file, `test_cleanup_deletes_a_snapshot_row_only_after_every_child_is_gone` drives the removed `_prune_superseded` directly. Migrate it to the new decomposition, keeping its assertions exactly as they are:

```python
    store = pg_graph.store
    with store._connection() as connection:
        with store._transaction_on(connection) as cursor:
            domain_id = store._domain_id(cursor)
        store._drain_snapshot(connection, domain_id, oldest, budget)
```

Task 5 keeps this test as the guard's behavioural check and adds the schema invariant beside it.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55432/iwiki_test" uv run pytest tests/postgres/test_code_graph_publication.py -q`
Expected: PASS

- [ ] **Step 6: Lint and run the fast suite**

Run: `uv run flake8 src tests && uv run pytest -q`
Expected: flake8 silent; no new failures

- [ ] **Step 7: Assert the candidate page size is untouched (R14)**

This restructure moves the governing bound from a snapshot count to a row budget, which
invites someone to "tidy up" the now-quieter parameter. Pin it with a test in
`tests/postgres/test_code_graph_publication.py`:

```python
def test_the_candidate_page_size_keeps_its_default():
    """The row budget governs volume now; this parameter only pages the
    candidate query, and changing its default is proposal-first."""
    import inspect

    from iwiki_mcp.postgres.codegraph import PostgresCodeGraphStore

    parameter = inspect.signature(
        PostgresCodeGraphStore.__init__
    ).parameters["superseded_cleanup_limit"]

    assert parameter.default == 2
```

Run: `uv run pytest tests/postgres/test_code_graph_publication.py -q -k candidate_page_size`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/iwiki_mcp/postgres/codegraph.py src/iwiki_mcp/codegraph/application.py tests/postgres/test_code_graph_publication.py
git commit -m "fix(codegraph): commit cleanup per batch on one connection per cycle"
```

---

### Task 5: The reactivation re-check and the guard's schema invariant

Implements R15 and R16.

**Files:**
- Modify: `src/iwiki_mcp/postgres/codegraph.py` (`_drain_snapshot`)
- Test: `tests/postgres/test_code_graph_publication.py`

**Interfaces:**
- Consumes: `_drain_snapshot`, `_delete_snapshot_row`, `_CLEANUP_CHILD_TABLES`, `_aged_superseded` from Tasks 3-4.
- Produces: `_still_superseded(cursor, domain_id, snapshot_id) -> bool`

**Read this before writing the guard you expect to write.** An earlier draft of this plan widened the snapshot-row guard to all four child tables, and the spec required it. Both were wrong and the spec has been corrected. `code_graph_symbols.file_id` is `NOT NULL` with a cascading foreign key to `code_graph_files`; `code_graph_relations` depends on files and symbols; `code_graph_wiki_links` depends on relations; and no constraint is `NOT VALID`. Files roots the chain, so an empty `code_graph_files` implies every other child table is empty by integrity, not by drain order. The state issue 104 describes cannot exist, three of the four tests that would have proved the extra clauses are unconstructible, and the clauses would cost an index probe each for nothing. The guard stays as Task 4 wrote it. What this task adds is the test that pins the invariant, so a future migration that adds a child table outside the chain fails loudly instead of silently widening what the guard owes.

- [ ] **Step 1: Write the failing tests**

Add to `tests/postgres/test_code_graph_publication.py`:

```python
def test_a_reactivated_snapshot_stops_its_drain_within_one_batch(
    pg_graph, monkeypatch
):
    """The retention window exists to be a revert target; the drain must not
    strip the snapshot an operator just restored."""
    monkeypatch.setattr(type(pg_graph.store), "_CLEANUP_BATCH_ROWS", 1)
    oldest = _aged_superseded(pg_graph)
    before = _snapshot_rows(pg_graph, oldest)
    assert sum(before.values()) > 4, "fixture must supply several batches"

    store = pg_graph.store
    original = store._delete_batch
    calls = {"n": 0}

    def reactivate_then_delete(cursor, table, domain_id, sid, limit):
        calls["n"] += 1
        if calls["n"] == 2:
            cursor.execute(
                "UPDATE iwiki.code_graph_domain_state "
                "SET active_snapshot_id = %s "
                "WHERE iwiki_id = %s AND domain_id = %s",
                (sid, store.iwiki_id, domain_id),
            )
        return original(cursor, table, domain_id, sid, limit)

    monkeypatch.setattr(store, "_delete_batch", reactivate_then_delete)
    store.run_cleanup_cycle()

    after = _snapshot_rows(pg_graph, oldest)
    assert oldest in {row[0] for row in _snapshot_states(pg_graph)}, (
        "the restored snapshot's row was removed"
    )
    assert sum(after.values()) > 0, "the drain continued past the reactivation"


def test_every_code_graph_child_table_reaches_code_graph_files(pg_graph):
    """The one-table guard is sufficient only while files roots the chain.

    Read the live catalogue rather than a hand-written list: the point is to
    fail when a migration adds a child table that does not depend on files,
    which is exactly the case a hand-written list would not know about.
    """
    rows = pg_graph._query(
        "SELECT c.relname, f.relname "
        "FROM pg_constraint con "
        "JOIN pg_class c ON c.oid = con.conrelid "
        "JOIN pg_class f ON f.oid = con.confrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE con.contype = 'f' AND n.nspname = 'iwiki' "
        "AND c.relname LIKE 'code\\_graph\\_%'",
        (),
        admin=True,
    )
    parents = {}
    for child, parent in rows:
        parents.setdefault(child, set()).add(parent)

    children = {
        "code_graph_wiki_links",
        "code_graph_relations",
        "code_graph_symbols",
    }
    assert children <= set(parents), "a child table has no foreign keys at all"

    for table in children:
        reached, frontier = set(), [table]
        while frontier:
            for parent in parents.get(frontier.pop(), ()):
                if parent not in reached:
                    reached.add(parent)
                    frontier.append(parent)
        assert "code_graph_files" in reached, (
            f"{table} no longer depends on code_graph_files, so guarding the "
            "snapshot-row delete on files alone is no longer sufficient"
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55432/iwiki_test" uv run pytest tests/postgres/test_code_graph_publication.py -q -k "reactivated or reaches_code_graph_files"`
Expected: the reactivation test FAILS — the drain runs to completion and the snapshot row is gone. The invariant test PASSES immediately, because it asserts a property the schema already has; that is correct and expected. Its value is regression, and Step 6 is where it earns it.

- [ ] **Step 3: Add the reactivation check**

Add this method to `PostgresCodeGraphStore`, directly above `_delete_batch`:

```python
    def _still_superseded(
        self, cursor, domain_id: int, snapshot_id: str
    ) -> bool:
        """A revert during the drain must not strip the snapshot it restored.

        The retention window buys exactly one thing: a manual revert target.
        Per-batch commits stretch a snapshot's drain over minutes, so the
        check runs per batch rather than once per cycle -- one indexed lookup
        per 10,000 rows narrows the race to a single batch.
        """
        cursor.execute(
            "SELECT 1 FROM iwiki.code_graph_domain_state "
            "WHERE iwiki_id = %s AND domain_id = %s "
            "AND active_snapshot_id = %s",
            (self.iwiki_id, domain_id, snapshot_id),
        )
        return cursor.fetchone() is None
```

- [ ] **Step 4: Call it from `_drain_snapshot`**

Replace the body of `_drain_snapshot` from Task 4 with:

```python
        removed = 0
        for table in self._CLEANUP_CHILD_TABLES:
            while removed < budget:
                with self._transaction_on(connection) as cursor:
                    if not self._still_superseded(
                        cursor, domain_id, snapshot_id
                    ):
                        return removed
                    taken = self._delete_batch(
                        cursor,
                        table,
                        domain_id,
                        snapshot_id,
                        min(self._CLEANUP_BATCH_ROWS, budget - removed),
                    )
                if not taken:
                    break
                removed += taken
        with self._transaction_on(connection) as cursor:
            if not self._still_superseded(cursor, domain_id, snapshot_id):
                return removed
            removed += self._delete_snapshot_row(
                cursor, domain_id, snapshot_id
            )
        return removed
```

`_delete_snapshot_row` is unchanged from Task 4 — one `NOT EXISTS` over `code_graph_files`, plus the active-snapshot exclusion.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55432/iwiki_test" uv run pytest tests/postgres/test_code_graph_publication.py -q`
Expected: PASS

- [ ] **Step 6: Prove both tests discriminate**

For the reactivation test: remove the `_still_superseded` call inside the batch loop, re-run it, expect failure, then restore.

For the invariant test, add a throwaway child table that hangs off snapshots without touching files, and confirm the test fails:

```sql
CREATE TABLE iwiki.code_graph_orphan_child (
    iwiki_id text NOT NULL,
    domain_id bigint NOT NULL,
    snapshot_id text NOT NULL,
    PRIMARY KEY (iwiki_id, domain_id, snapshot_id),
    CONSTRAINT code_graph_orphan_child_snapshot_fk
        FOREIGN KEY (iwiki_id, domain_id, snapshot_id)
        REFERENCES iwiki.code_graph_snapshots (iwiki_id, domain_id, snapshot_id)
        ON DELETE CASCADE
);
```

The test as written iterates a fixed set of three child tables, so extend that set from the catalogue for this check, observe the failure, then drop the table. Record both demonstrations in the task's report — a test that cannot fail is not a test.

- [ ] **Step 7: Lint and commit**

```bash
uv run flake8 src tests
git add src/iwiki_mcp/postgres/codegraph.py tests/postgres/test_code_graph_publication.py
git commit -m "fix(codegraph): re-check snapshot activity per batch and pin the guard's schema invariant"
```
---

### Task 6: `validate_direct_principal` accepts a connection factory

Implements R9.

**Files:**
- Modify: `src/iwiki_mcp/postgres/store.py:87-120`
- Modify: `src/iwiki_mcp/postgres/codegraph.py:154-160`
- Test: `tests/postgres/test_code_graph_publication.py`

**Interfaces:**
- Consumes: the context-manager factory contract from Task 3.
- Produces: `validate_direct_principal(dsn, *, iwiki_id=None, read_domains=(), write_domains=(), connection_factory=None)`

- [ ] **Step 1: Write the failing test**

Add to `tests/postgres/test_code_graph_publication.py`:

```python
def test_principal_validation_uses_the_supplied_factory(pg_graph):
    """A worker holding two connections would put the ceiling out by one per
    worker, which is the whole of the bound at two workers."""
    opened = []

    def factory():
        opened.append(1)
        return psycopg.connect(pg_graph.dsn)

    result = store_module.validate_direct_principal(
        pg_graph.dsn,
        iwiki_id=pg_graph.iwiki_id,
        connection_factory=factory,
    )

    assert result is None
    assert len(opened) == 1, "validation ignored the factory and dialled out"
```

Import the module as `from iwiki_mcp.postgres import store as store_module` if the file does not already import it.

- [ ] **Step 2: Run the test to verify it fails**

Run: `IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55432/iwiki_test" uv run pytest tests/postgres/test_code_graph_publication.py -q -k supplied_factory`
Expected: FAIL — `TypeError: validate_direct_principal() got an unexpected keyword argument 'connection_factory'`

- [ ] **Step 3: Add the parameter**

In `src/iwiki_mcp/postgres/store.py`, change the signature and the first line of the body:

```python
def validate_direct_principal(
    dsn: str,
    *,
    iwiki_id: str | None = None,
    read_domains: tuple[str, ...] = (),
    write_domains: tuple[str, ...] = (),
    connection_factory: Callable[[], ContextManager[Any]] | None = None,
) -> dict[str, str] | None:
    """Reject owner/BYPASSRLS roles and, when supplied, unmapped scope.

    The factory exists so a maintenance worker validates through the same
    pooled connection it will then work on. Dialling out separately would
    put a worker at two connections and the server's stated ceiling out by
    one per worker.
    """
    connect = connection_factory or (lambda: psycopg.connect(dsn))
    try:
        with connect() as connection:
```

Leave the rest of the function unchanged.

- [ ] **Step 4: Pass the store's own factory through**

In `src/iwiki_mcp/postgres/codegraph.py`, replace the validation block at lines 154-160 with:

```python
        if require_database_principal and validate_direct_principal(
            dsn,
            iwiki_id=iwiki_id,
            read_domains=(domain,),
            write_domains=(domain,),
            connection_factory=self._connection_factory,
        ) is not None:
            raise ValueError("invalid_config")
```

`self._connection_factory` is assigned above this block, so it is available.

- [ ] **Step 5: Run the tests**

Run: `IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55432/iwiki_test" uv run pytest tests/postgres -q`
Expected: PASS, with the suite's two known pre-existing failures and no new ones

- [ ] **Step 6: Lint and commit**

```bash
uv run flake8 src tests
git add src/iwiki_mcp/postgres/store.py src/iwiki_mcp/postgres/codegraph.py tests/postgres/test_code_graph_publication.py
git commit -m "refactor(postgres): let principal validation reuse a pooled connection"
```

---

### Task 7: Wire the runtime in and delete the unbounded paths

Implements R1's install half, R3, R6, R7, R8's enforcement half, and R4's removal half.

**Files:**
- Modify: `src/iwiki_mcp/codegraph/application.py` (`_SWEEP_ACTIVE`, `_sweep_wiki_cleanup`, `schedule_wiki_cleanup`)
- Modify: `src/iwiki_mcp/postgres/codegraph.py` (`_schedule_cleanup`, `_cleanup_lock`, `_cleanup_active`)
- Modify: `src/iwiki_mcp/server.py` (`_install_hosted_runtime`, `_clear_hosted_runtime`, `_clear_hosted_runtime_for_test`)
- Modify: `src/iwiki_mcp/http.py:799-801`
- Test: `tests/test_cleanup_sweep_interval.py`, `tests/test_maintenance_runtime.py`

**Interfaces:**
- Consumes: `MaintenanceRuntime`, `CleanupJob`, `open_maintenance_pool` from Tasks 1-2; `run_cleanup_cycle` from Task 4.
- Produces:
  - `application.run_cleanup_job(job, connection_factory) -> int` — the injected runner
  - `application.schedule_wiki_cleanup(binding, owner_id, settings, *, lock_timeout_ms=5000, runtime=None) -> int` — returns how many domains were queued
  - `server._MAINTENANCE_RUNTIME` — module global, `None` when absent
  - `_install_hosted_runtime(pool, cfg, code_graph=None, specifications=None, *, maintenance_dsn=None, maintenance_options=None)`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cleanup_sweep_interval.py`:

```python
def test_scheduling_queues_one_job_per_writable_domain(monkeypatch):
    from iwiki_mcp.codegraph import maintenance

    submitted = []

    class _Runtime:
        def submit(self, job):
            submitted.append(job.key)
            return True

    binding = _binding(iwiki_id="personal", write=["a", "b", "c"])

    queued = application.schedule_wiki_cleanup(
        binding, "token-1", object(), runtime=_Runtime()
    )

    assert queued == 3
    assert submitted == [
        ("personal", "a"),
        ("personal", "b"),
        ("personal", "c"),
    ]
    assert isinstance(maintenance.CleanupJob, type)


def test_scheduling_without_a_runtime_falls_back_to_one_thread(monkeypatch):
    """The local stdio path has no pool; it must still clean, and still
    deduplicate through the same set."""
    started = []
    monkeypatch.setattr(
        application, "_run_local_cleanup", lambda job: started.append(job.key)
    )

    binding = _binding(iwiki_id="personal", write=["a"])
    queued = application.schedule_wiki_cleanup(
        binding, "token-1", object(), runtime=None
    )

    assert queued == 1
    assert started == [("personal", "a")]
```

`_binding(...)` is a small helper in that file returning an object with `iwiki_id` and `write` attributes; write it if the file has none.

Add to `tests/test_maintenance_runtime.py`:

```python
def test_the_removed_guards_are_gone_from_the_source():
    """R4: one deduplication set, not three."""
    from pathlib import Path

    import iwiki_mcp

    root = Path(iwiki_mcp.__file__).parent
    application_src = (root / "codegraph" / "application.py").read_text()
    store_src = (root / "postgres" / "codegraph.py").read_text()

    assert "_SWEEP_ACTIVE" not in application_src
    assert "_cleanup_active" not in store_src
    assert "_schedule_cleanup" not in store_src
    assert "threading.Thread" not in store_src
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cleanup_sweep_interval.py tests/test_maintenance_runtime.py -q`
Expected: FAIL — `schedule_wiki_cleanup() got an unexpected keyword argument 'runtime'`, and the source assertions fail.

- [ ] **Step 3: Replace the scheduling in `application.py`**

Delete `_SWEEP_ACTIVE` and `_sweep_wiki_cleanup` entirely. Keep `_SWEEP_LOCK`, `_SWEEP_MIN_INTERVAL_SECONDS` and `_SWEEP_LAST`, and change `cleanup_sweep_due` to consult only the floor:

```python
def cleanup_sweep_due(iwiki_id: str) -> bool:
    """Cheap enough to ask on every request: a lock and a float compare.

    Deliberately not a timer. A background schedule would have no request, no
    binding and no token, so it would have to act with the service role's
    whole reach -- substituting connection privileges for a mandate, which is
    exactly what the per-domain store design rejects. A request already
    carries the mandate the sweep needs.

    In-flight deduplication now lives in the maintenance queue, so this
    function answers one question only: has this wiki been queued recently.
    """
    now = time.monotonic()
    with _SWEEP_LOCK:
        last = _SWEEP_LAST.get(iwiki_id)
        due = last is None or now - last >= _SWEEP_MIN_INTERVAL_SECONDS
    if not due:
        LOGGER.debug("code graph cleanup skipped, inside the throttle interval")
    return due
```

Replace `schedule_wiki_cleanup` with:

```python
def run_cleanup_job(job, connection_factory) -> int:
    """Run one domain's cleanup cycle under that domain's own store.

    A store cleans only the domain it was built for and validated against,
    so one job is one store: widening a store's reach would let it delete
    rows in a domain whose principal it never checked. `binding.write` is the
    mandate -- exactly the domains this caller may already write -- not
    everything the connection happens to see.
    """
    store = create_postgres_publisher(
        job.binding,
        job.owner_id,
        job.settings,
        lock_timeout_ms=job.lock_timeout_ms,
        domain=job.domain,
        connection_factory=connection_factory,
    )
    if connection_factory is None:
        return store.run_cleanup_cycle()
    with connection_factory() as connection:
        return store.run_cleanup_cycle(connection)


def _run_local_cleanup(job) -> None:
    """The stdio path: no pool exists, so one daemon thread per job.

    Bounded by construction rather than by a queue -- one stdio process
    serves one client -- and still deduplicated, because the caller checked
    the same set the hosted path uses.
    """
    threading.Thread(
        target=run_cleanup_job,
        args=(job, None),
        name="iwiki-code-graph-cleanup",
        daemon=True,
    ).start()


def schedule_wiki_cleanup(
    binding: PostgresBinding,
    owner_id: str,
    settings,
    *,
    lock_timeout_ms: int = 5000,
    runtime=None,
) -> int:
    """Queue one cleanup job per writable domain. Never blocks the caller.

    The publication or read that triggered this is already committed, and
    cleanup is maintenance rather than a precondition for work someone else
    succeeded at.
    """
    queued = 0
    for domain in binding.write:
        job = maintenance.CleanupJob(
            iwiki_id=binding.iwiki_id,
            domain=domain,
            binding=binding,
            owner_id=owner_id,
            settings=settings,
            lock_timeout_ms=lock_timeout_ms,
        )
        if runtime is not None:
            if runtime.submit(job):
                queued += 1
            continue
        _run_local_cleanup(job)
        queued += 1
    with _SWEEP_LOCK:
        _SWEEP_LAST[binding.iwiki_id] = time.monotonic()
    LOGGER.info(
        "code graph cleanup queued %s of %s writable domains",
        queued,
        len(binding.write),
    )
    return queued
```

Add `from . import maintenance` to the relative imports at the top of the file.

Add `connection_factory` to `create_postgres_publisher`:

```python
def create_postgres_publisher(
    binding: PostgresBinding,
    owner_id: str,
    settings,
    *,
    lock_timeout_ms: int = 5000,
    domain: str | None = None,
    connection_factory=None,
) -> PostgresCodeGraphStore:
```

and pass `connection_factory=connection_factory` into the `PostgresCodeGraphStore(...)` call.

- [ ] **Step 4: Delete the store's guard and thread**

In `src/iwiki_mcp/postgres/codegraph.py`, delete the `_cleanup_lock` and `_cleanup_active` class attributes with their comment (lines 113-119) and the whole `_schedule_cleanup` method. Replace its call inside `begin` — find the `self._schedule_cleanup()` line in the try/except after the commit — and delete that try/except block: scheduling now happens in `server.py`, where the binding and the runtime are both in scope.

`import threading` at line 11 then has no remaining user in that file — those three sites are all of them — so delete the import too or flake8 fails with `F401 'threading' imported but unused`. Confirm with `grep -n threading src/iwiki_mcp/postgres/codegraph.py` returning nothing.

- [ ] **Step 5: Own the runtime's lifecycle in `server.py`**

Add the module global beside the other hosted globals:

```python
_MAINTENANCE_RUNTIME = None
```

Change `_install_hosted_runtime`:

```python
def _install_hosted_runtime(
    pool,
    cfg: Config,
    code_graph=None,
    specifications=None,
    *,
    maintenance_dsn: str | None = None,
    maintenance_options: str | None = None,
) -> None:
    global _HOSTED_POOL, _HOSTED_CONFIG, _HOSTED_CODE_GRAPH
    global _HOSTED_SPECIFICATIONS, _MAINTENANCE_RUNTIME
    _HOSTED_POOL = pool
    _HOSTED_CONFIG = cfg
    _HOSTED_CODE_GRAPH = code_graph
    _HOSTED_SPECIFICATIONS = specifications
    # Authentication borrows from this same pool, so the tool ceiling has to
    # leave connections behind for it. At parity the liveness probe would be
    # starved through the database exactly as it was through the loop.
    _TOOL_LIMITER.total_tokens = max(1, pool.max_size - _POOL_RESERVE)
    if maintenance_dsn and _MAINTENANCE_RUNTIME is None:
        maintenance_pool = _codegraph_maintenance.open_maintenance_pool(
            maintenance_dsn, options=maintenance_options or ""
        )
        _MAINTENANCE_RUNTIME = _codegraph_maintenance.MaintenanceRuntime(
            _codegraph_application.run_cleanup_job, pool=maintenance_pool
        )
        _MAINTENANCE_RUNTIME.start()
```

Change `_clear_hosted_runtime` and `_clear_hosted_runtime_for_test` to stop it:

```python
def _stop_maintenance_runtime() -> None:
    global _MAINTENANCE_RUNTIME
    runtime = _MAINTENANCE_RUNTIME
    _MAINTENANCE_RUNTIME = None
    if runtime is None:
        return
    runtime.stop()
    if runtime._pool is not None:
        runtime._pool.close()
```

Call `_stop_maintenance_runtime()` as the first statement of both clear functions.

Import the module at the top, beside the other code-graph imports:

```python
from .codegraph import maintenance as _codegraph_maintenance
```

Pass the runtime into both scheduling call sites. In `_schedule_wiki_code_graph_cleanup`, add `runtime=_MAINTENANCE_RUNTIME` to the `schedule_wiki_cleanup(...)` call.

- [ ] **Step 6: Feed the DSN from `http.py`**

At `src/iwiki_mcp/http.py:799`, change the install call to:

```python
        server._install_hosted_runtime(
            pool,
            cfg,
            config.code_graph,
            config.specifications,
            maintenance_dsn=dsn,
            maintenance_options=options,
        )
```

`dsn` and `options` are both already in scope above.

- [ ] **Step 7: Run the tests**

Run: `uv run pytest -q`
Expected: PASS; no new failures

Run: `IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55432/iwiki_test" uv run pytest tests/postgres -q`
Expected: PASS with the two known pre-existing failures only

- [ ] **Step 8: Bump the version in all four places**

```bash
sed -i 's/0\.7\.297/0.7.298/' pyproject.toml tests/test_package.py src/iwiki_mcp/__init__.py
uv sync --extra dev
git diff --stat uv.lock
```

- [ ] **Step 9: Lint and commit**

```bash
uv run flake8 src tests
git add -A
git commit -m "feat(codegraph): queue cleanup on a bounded maintenance runtime"
```

---

### Task 8: Correct the published claim in all three places

Implements R10's documentation half and R22.

**Files:**
- Modify: `docs/code-graph-publishing.md`
- Modify: `docs/code-graph-publishing.ru.md`
- The wiki page `concept/code-graph-storage`, section `Lifecycle and metadata`, is updated by the parent session through the iwiki MCP tools — a subagent is read-only against the wiki and must not attempt it. Report the proposed wording instead.

**Interfaces:**
- Consumes: `MAINTENANCE_WORKERS` and the `pool_max_size` arithmetic from Task 7.
- Produces: nothing code depends on.

- [ ] **Step 1: Rewrite the English paragraph**

In `docs/code-graph-publishing.md`, replace the paragraph beginning "Two things schedule that work." with:

```markdown
Two things schedule that work, and neither of them runs it. `begin` queues a job for the
domain it is publishing; any other authenticated hosted request may queue one job per
domain in `binding.write`, throttled to once per 900 seconds per wiki. A fixed set of two
maintenance workers drains that queue, so a growing number of clients produces queueing
rather than threads and connections. The queue is bounded and an enqueue against a full
one is dropped and counted — the work returns on a later request, because nothing waits
on it.

One batch is one delete of at most 10,000 rows from one child table of one snapshot, and
each batch commits on its own. A kill therefore costs at most that one batch, and the
next cycle resumes where it stopped. A worker holds one connection for a whole cycle and
draws it from a maintenance pool of its own, never from the pool the tools and
authentication share: the server's total PostgreSQL connections are `pool_max_size` plus
the two maintenance workers, twelve at the shipped defaults.
```

- [ ] **Step 2: Rewrite the Russian sibling to match**

In `docs/code-graph-publishing.ru.md`, replace the paragraph beginning "Эту работу планируют две вещи." with:

```markdown
Эту работу планируют две вещи, и ни одна из них её не выполняет. `begin` ставит в очередь
работу для того домена, который публикует; любой другой аутентифицированный hosted-запрос
может поставить по одной работе на каждый домен из `binding.write`, не чаще одного раза в
900 секунд на вики. Очередь разбирает фиксированный набор из двух обслуживающих воркеров,
поэтому рост числа клиентов даёт очередь, а не потоки и соединения. Очередь ограничена, и
постановка в переполненную отбрасывается со счётчиком — работа вернётся со следующим
запросом, потому что её никто не ждёт.

Один батч — это одно удаление не более 10 000 строк из одной дочерней таблицы одного
снапшота, и каждый батч коммитится отдельно. Поэтому убийство процесса стоит не больше
одного такого батча, а следующий цикл продолжает с того места, где остановился предыдущий.
Воркер держит одно соединение на весь цикл и берёт его из собственного пула обслуживания,
никогда — из того, который делят инструменты и аутентификация: всего соединений к
PostgreSQL у сервера `pool_max_size` плюс два обслуживающих воркера, двенадцать при
поставляемых значениях по умолчанию.
```

- [ ] **Step 3: Verify both siblings agree**

Run: `grep -n "10,000\|10 000\|twelve\|двенадцать" docs/code-graph-publishing.md docs/code-graph-publishing.ru.md`
Expected: both files carry the batch size and the connection total

- [ ] **Step 4: Report the wiki wording**

Write the proposed replacement for the last paragraph of `concept/code-graph-storage` → `Lifecycle and metadata` into the task report. The parent session applies it with `wiki_update_page`. The sentence to correct is the one claiming the worker "drains in committed batches of 10,000 rows", which was true of the intent and not of the code until this change.

- [ ] **Step 5: Commit**

```bash
git add docs/code-graph-publishing.md docs/code-graph-publishing.ru.md
git commit -m "docs(codegraph): correct the batch-commit claim and state the connection ceiling"
```

---

### Task 9: Measure the foreign-key indexes and decide

Implements R17 and R18.

**Files:**
- Create: `docs/superpowers/reports/cleanup-index-measurement.md` (the recorded measurement)
- Possibly modify: `src/iwiki_mcp/postgres/migrations.py` — only if the measurement justifies an index

**Interfaces:**
- Consumes: the restructured drain from Tasks 4-5, because the path being measured is the one that ships.
- Produces: a decision per candidate index, and the migration only if a decision says yes.

**This task may correctly produce no code change.** The normal drain order deletes relations before symbols, so by the time symbols are deleted the foreign-key check examines an already-emptied set. The expected result is that no index is justified for the drain path and that any residual cost is vacuum timing. The measurement exists to establish which of those is true.

- [ ] **Step 1: Seed a disposable database to production scale**

```bash
docker run -d --name iwiki-pgbench -e POSTGRES_PASSWORD=pgtest -e POSTGRES_DB=iwiki_test -p 127.0.0.1:55433:5432 pgvector/pgvector:pg16
docker exec iwiki-pgbench psql -U postgres -d iwiki_test -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

Publish thirty snapshots of roughly 21,000 relations each into one domain using the repository's own publication path, so the row shapes are real. Record the resulting `pg_total_relation_size` of every `code_graph_*` table.

- [ ] **Step 2: Measure the drain in the shipped order**

Time one full `run_cleanup_cycle` against one superseded snapshot. Record wall time and rows removed.

- [ ] **Step 3: Measure the drain in the reverse order**

Delete a snapshot's symbol rows while its relation rows are still present, so the foreign-key check has live rows to examine. Record wall time for the same row count. This is the path an index would actually help.

- [ ] **Step 4: Build each candidate and measure it**

```sql
CREATE INDEX CONCURRENTLY code_graph_relations_source_symbol_idx
    ON iwiki.code_graph_relations (iwiki_id, domain_id, snapshot_id, source_symbol_id);
CREATE INDEX CONCURRENTLY code_graph_relations_source_file_idx
    ON iwiki.code_graph_relations (iwiki_id, domain_id, snapshot_id, source_file_id);
CREATE INDEX CONCURRENTLY code_graph_relations_target_symbol_idx
    ON iwiki.code_graph_relations (iwiki_id, domain_id, snapshot_id, target_symbol_id);
```

For each: record `CREATE INDEX CONCURRENTLY` wall time and `pg_relation_size` of the index. Then repeat Steps 2 and 3 with the index present.

- [ ] **Step 5: Measure the write cost**

Time the insertion of one publication's ~21,000 relations before and after each index exists. Record total on-disk size before and after.

- [ ] **Step 6: Measure the vacuum cost**

After one full drain, record `n_dead_tup` from `pg_stat_user_tables` for `code_graph_relations` and the time until autovacuum collects them under the tuned settings already in production (`scale_factor` 0.02, `threshold` 1000, `cost_limit` 2000).

- [ ] **Step 7: Decide and record**

Write all six quantities into `docs/superpowers/reports/cleanup-index-measurement.md` with the commands that produced them. State a decision per candidate. Add a migration only for a candidate whose measured delete benefit on the shipped drain path exceeds its measured write and space cost. A decision of "no index" is a result, not a failure, and is recorded as one.

- [ ] **Step 8: Tear the database down**

```bash
docker rm -f iwiki-pgbench
```

- [ ] **Step 9: Commit**

```bash
git add docs/superpowers/reports/cleanup-index-measurement.md
git commit -m "docs(codegraph): record the foreign-key index measurement and decision"
```

---

### Task 10: Hosted acceptance — HUMAN CHECKPOINT

Implements R10a, R23, R24, and the intent's `Done when`.

**Deployment carries no autonomy.** Do not deploy, restart, or create an index against production. Prepare every command, present them, and wait.

**Files:**
- No source changes. Output is recorded evidence.

- [ ] **Step 1: Run the PostgreSQL suite against a real database**

```bash
docker run -d --name iwiki-pgtest -e POSTGRES_PASSWORD=pgtest -e POSTGRES_DB=iwiki_test -p 127.0.0.1:55432:5432 pgvector/pgvector:pg16
docker exec iwiki-pgtest psql -U postgres -d iwiki_test -c "CREATE EXTENSION IF NOT EXISTS vector;"
IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55432/iwiki_test" uv run pytest -q tests/postgres
docker rm -f iwiki-pgtest
```

Record the result. The default run skips these tests, so a PostgreSQL change verified only by it is unverified.

- [ ] **Step 2: Present the deployment for approval and stop**

Present the version, the image, the rollback tag, and the restart command. Wait for an explicit yes.

- [ ] **Step 3: After approval — observe the publication burst**

Publish across several domains concurrently while sampling `pg_stat_activity` for the server's connection count and polling the unauthenticated health endpoint. Record: peak connections against the stated ceiling of twelve, and whether the probe answered on every attempt.

- [ ] **Step 4: Observe the restart mid-drain**

With a backlog draining, restart the service. Record rows removed before and after, and confirm the loss does not exceed one batch.

- [ ] **Step 5: Observe the guard**

Find or construct a superseded snapshot whose drain was interrupted with children remaining, and record that its snapshot row survives.

- [ ] **Step 6: Read connection use from the log alone**

Record the `code graph maintenance pool:` lines and confirm `available`, `waiting` and `wait_ms` are readable without querying PostgreSQL.

- [ ] **Step 7: Confirm or revise the worker count**

If workers waited longer than one cycle's duration, or the backlog did not shrink, revise `MAINTENANCE_WORKERS` and record both values with the measurement that moved them. Otherwise record that two was confirmed.

- [ ] **Step 8: Hand the evidence to the parent session**

The parent records every observation on the wiki task page and runs `/check-chain result` against this plan.

---

## Self-Review

**Spec coverage.** R1 → T1+T7. R2 → T1+T2. R3 → T7. R4 → T1+T7. R5 → T1. R6 → T7. R7 → T7. R8 → T2+T7. R9 → T6. R10 → T7+T8. R10a → T10. R11 → T3. R12 → T4. R13 → T4. R14 → T4 step 7. R15 → T5 (invariant test; the guard itself is unchanged and correct). R16 → T5. R17 → T9. R18 → T9. R19 → T1 (rows, elapsed, drop counter), T2 (pool statistics), T7 (throttle skip at debug). R20 → T1. R21 → T1. R22 → T8. R23 → T10. R24 → T5 step 6 and T10. No gaps.

**Placeholder scan.** No "TBD", no "add appropriate error handling", no "similar to Task N". Every code step carries the code. Two steps name a helper the implementer must locate in the existing test file rather than invent — `_store` and `_publish_snapshot` — and both say explicitly what to do if it is absent.

**Type consistency.** `run_cleanup_cycle(connection=None) -> int` is defined in T4 and consumed under that exact name in T7. `CleanupJob` fields are defined in T1 and constructed with the same keywords in T7. `run_cleanup_job(job, connection_factory) -> int` matches the runner contract T1 declares and T7 supplies. `connection_factory` means the same thing — a callable returning a context manager — in T3, T6 and T7 alike, which is the contract `AuthStore` already uses.
