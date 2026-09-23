"""The floor that lets any request trigger a sweep without sweeping always."""

from __future__ import annotations

import time

import pytest

from iwiki_mcp.codegraph import application


@pytest.fixture(autouse=True)
def _clean_sweep_state():
    with application._SWEEP_LOCK:
        application._SWEEP_LAST.clear()
        application._LOCAL_CLEANUP_ACTIVE.clear()
    yield
    with application._SWEEP_LOCK:
        application._SWEEP_LAST.clear()
        application._LOCAL_CLEANUP_ACTIVE.clear()


def test_a_wiki_never_swept_is_due():
    assert application.cleanup_sweep_due("personal") is True


def test_a_wiki_swept_just_now_is_not_due(monkeypatch):
    """Without the floor, the next request after a sweep starts another."""
    clock = [1000.0]
    monkeypatch.setattr(application.time, "monotonic", lambda: clock[0])
    with application._SWEEP_LOCK:
        application._SWEEP_LAST["personal"] = clock[0]

    assert application.cleanup_sweep_due("personal") is False


def test_a_wiki_becomes_due_again_once_the_interval_passes(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(application.time, "monotonic", lambda: clock[0])
    with application._SWEEP_LOCK:
        application._SWEEP_LAST["personal"] = clock[0]

    clock[0] += application._SWEEP_MIN_INTERVAL_SECONDS - 1
    assert application.cleanup_sweep_due("personal") is False

    clock[0] += 2
    assert application.cleanup_sweep_due("personal") is True


def test_the_floor_is_per_wiki(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(application.time, "monotonic", lambda: clock[0])
    with application._SWEEP_LOCK:
        application._SWEEP_LAST["personal"] = clock[0]

    assert application.cleanup_sweep_due("personal") is False
    assert application.cleanup_sweep_due("other-wiki") is True


def test_the_hook_is_silent_without_an_authenticated_request(monkeypatch):
    """Every stdio call reaches this hook; none of them may pay for it."""
    from iwiki_mcp import server

    called = []
    monkeypatch.setattr(
        server, "_request_auth_context", lambda: None
    )
    monkeypatch.setattr(
        server._codegraph_application,
        "cleanup_sweep_due",
        lambda iwiki_id: called.append(iwiki_id) or True,
    )

    server._maybe_sweep_code_graph_cleanup()

    assert called == [], "the hook resolved state without an authenticated call"


class _Binding:
    def __init__(self, iwiki_id, write):
        self.iwiki_id = iwiki_id
        self.write = write


def _binding(*, iwiki_id, write):
    return _Binding(iwiki_id, write)


def test_scheduling_queues_one_job_per_writable_domain(monkeypatch):
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


def test_scheduling_without_a_runtime_falls_back_to_one_thread(monkeypatch):
    """The local stdio path has no pool; it must still clean."""
    import threading

    release = threading.Event()
    started = []
    monkeypatch.setattr(
        application,
        "run_cleanup_job",
        lambda job, factory: (started.append(job.key), release.wait(timeout=5)),
    )

    binding = _binding(iwiki_id="personal", write=["a"])
    queued = application.schedule_wiki_cleanup(
        binding, "token-1", object(), runtime=None
    )

    deadline = time.monotonic() + 5
    while not started and time.monotonic() < deadline:
        time.sleep(0.01)

    assert queued == 1
    assert started == [("personal", "a")]

    release.set()


def test_scheduling_without_a_runtime_deduplicates_through_the_local_set(
    monkeypatch,
):
    """R7: the stdio fallback must not reinstate the deleted class-scoped
    guard under another name. It has no MaintenanceRuntime to deduplicate
    through, so it must keep its own key set and release it once the job
    finishes."""
    import threading

    release = threading.Event()
    started = []
    finished = []

    def fake_run_cleanup_job(job, connection_factory):
        started.append(job.key)
        release.wait(timeout=5)
        finished.append(job.key)
        return 0

    monkeypatch.setattr(application, "run_cleanup_job", fake_run_cleanup_job)

    binding = _binding(iwiki_id="personal", write=["a"])

    first = application.schedule_wiki_cleanup(
        binding, "token-1", object(), runtime=None
    )
    deadline = time.monotonic() + 5
    while not started and time.monotonic() < deadline:
        time.sleep(0.01)

    second = application.schedule_wiki_cleanup(
        binding, "token-1", object(), runtime=None
    )

    assert first == 1
    assert second == 0, "a second request started a duplicate thread"
    assert started == [("personal", "a")], "only one job should ever run"

    release.set()
    deadline = time.monotonic() + 5
    while not finished and time.monotonic() < deadline:
        time.sleep(0.01)

    third = application.schedule_wiki_cleanup(
        binding, "token-1", object(), runtime=None
    )
    assert third == 1, "the key was not released once the job finished"
