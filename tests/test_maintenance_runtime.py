"""The bounds that keep maintenance from growing with the client count."""

from __future__ import annotations

import threading
import time

import pytest

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


def test_a_failing_jobs_partial_rows_reach_the_failure_log(caplog):
    """A cycle failing on its first batch and one failing after 199,000 rows
    must not read alike -- the exception's `rows_removed`, when a runner
    sets one, belongs in the warning, not just the exception class.
    """
    second = threading.Event()

    def runner(job, factory):
        if job.domain == "boom":
            exc = RuntimeError("killed mid-drain")
            exc.rows_removed = 4200
            raise exc
        second.set()
        return 0

    runtime = maintenance.MaintenanceRuntime(runner=runner, workers=1)
    runtime.start()
    try:
        with caplog.at_level("WARNING", logger=maintenance.LOGGER.name):
            runtime.submit(_job(domain="boom"))
            runtime.submit(_job(domain="after"))
            assert second.wait(timeout=5), "worker died on the failure"
    finally:
        runtime.stop()

    assert "4200 rows removed before the failure" in caplog.text


def test_a_failing_pool_connection_does_not_kill_its_worker(caplog):
    """Pool misbehavior must not strand workers; factory must be inside try."""

    class BadPool:
        @property
        def connection(self):
            raise RuntimeError("pool connection failed")

        def get_stats(self):
            return {}

    def runner(job, factory):
        return 0

    runtime = maintenance.MaintenanceRuntime(runner=runner, workers=1, pool=BadPool())
    runtime.start()
    try:
        with caplog.at_level("WARNING", logger=maintenance.LOGGER.name):
            runtime.submit(_job(domain="first"))
            runtime.submit(_job(domain="second"))
            deadline = time.monotonic() + 5
            while len(caplog.records) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
    finally:
        runtime.stop()

    assert len(caplog.records) >= 2, "both jobs should be attempted despite pool error"
    assert "RuntimeError" in caplog.text, "pool connection error should be logged"
    assert runtime._scheduled == set(), "keys should be released after failed attempts"


def test_something_escaping_run_itself_does_not_kill_the_worker(monkeypatch, caplog):
    """`_run` catches its own runner's failures; this is about what happens
    when something escapes `_run` itself -- nothing does today, but the
    worker loop must survive it regardless, and release the stuck job's key
    rather than stranding that domain in `_scheduled` for the process
    lifetime.
    """
    calls = []
    second = threading.Event()

    def runner(job, factory):
        calls.append(job.domain)
        second.set()
        return 0

    runtime = maintenance.MaintenanceRuntime(runner=runner, workers=1)
    real_run = runtime._run

    def exploding_run(job):
        if job.domain == "boom":
            raise RuntimeError("escaped _run entirely")
        return real_run(job)

    monkeypatch.setattr(runtime, "_run", exploding_run)
    runtime.start()
    try:
        with caplog.at_level("ERROR", logger=maintenance.LOGGER.name):
            runtime.submit(_job(domain="boom"))
            runtime.submit(_job(domain="after"))
            assert second.wait(timeout=5), "worker died on the escaped exception"
        # Checked before `stop()`, which unconditionally clears `_scheduled`
        # on its own and would make this assertion vacuous afterward.
        assert _job(domain="boom").key not in runtime._scheduled, (
            "the domain whose exception escaped _run must not stay stranded"
        )
    finally:
        runtime.stop()

    assert calls == ["after"]
    assert "unexpected exception" in caplog.text


def test_stop_survives_a_thread_that_never_started(monkeypatch):
    """`start()` appends every thread to `self._threads` before starting
    any, so a `.start()` that raises partway leaves later threads in the
    list unstarted. `stop()` joining one of those must not raise -- that
    would propagate through `http.py`'s except clause, replacing the real
    startup error and skipping `pool.close()`.
    """
    real_start = threading.Thread.start
    calls = {"n": 0}

    def flaky_start(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("could not start thread")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", flaky_start)

    runtime = maintenance.MaintenanceRuntime(
        runner=lambda job, factory: 0, workers=2
    )
    with pytest.raises(RuntimeError):
        runtime.start()

    runtime.stop()  # must not raise

    assert runtime._threads == []


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
