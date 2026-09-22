"""The floor that lets any request trigger a sweep without sweeping always."""

from __future__ import annotations

import pytest

from iwiki_mcp.codegraph import application


@pytest.fixture(autouse=True)
def _clean_sweep_state():
    with application._SWEEP_LOCK:
        application._SWEEP_ACTIVE.clear()
        application._SWEEP_LAST.clear()
    yield
    with application._SWEEP_LOCK:
        application._SWEEP_ACTIVE.clear()
        application._SWEEP_LAST.clear()


def test_a_wiki_never_swept_is_due():
    assert application.cleanup_sweep_due("personal") is True


def test_a_wiki_with_a_running_sweep_is_not_due():
    with application._SWEEP_LOCK:
        application._SWEEP_ACTIVE.add("personal")

    assert application.cleanup_sweep_due("personal") is False


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


def test_the_floor_is_per_wiki():
    with application._SWEEP_LOCK:
        application._SWEEP_ACTIVE.add("personal")

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
