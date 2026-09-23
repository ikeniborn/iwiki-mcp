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

from psycopg_pool import ConnectionPool

LOGGER = logging.getLogger(__name__)

# One worker holds at most one connection at a time, so the worker count and
# the maintenance pool's size are the same number. Two is a starting value,
# revised only from the publication-burst observation the design requires.
MAINTENANCE_WORKERS = 2

# Maintenance work is droppable: it returns on a later request. The bound is
# what keeps a growing client count from growing memory instead of queueing.
MAINTENANCE_QUEUE_SIZE = 256


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
        self.log_pool_stats()
        started = time.monotonic()
        try:
            factory = self._pool.connection if self._pool is not None else None
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
