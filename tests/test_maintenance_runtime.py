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


def test_a_failing_pool_connection_does_not_kill_its_worker(caplog):
    """Pool misbehavior must not strand workers; factory must be inside try."""

    class BadPool:
        @property
        def connection(self):
            raise RuntimeError("pool connection failed")

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
