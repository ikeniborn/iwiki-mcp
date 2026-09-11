import logging
import time

import anyio
import pytest

from iwiki_mcp.engine import idle
from iwiki_mcp.engine.config import Config, ConfigError
from iwiki_mcp.engine.idle import IdleTracker


def test_idle_timeout_defaults_to_one_day(monkeypatch):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "key")

    assert Config.load().idle_timeout_seconds == 86400


def test_idle_timeout_zero_disables_limit(monkeypatch):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "key")
    monkeypatch.setenv("IWIKI_IDLE_TIMEOUT_SECONDS", "0")

    assert Config.load().idle_timeout_seconds == 0


@pytest.mark.parametrize("value", ["-1", "one", "1.5", ""])
def test_idle_timeout_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "key")
    monkeypatch.setenv("IWIKI_IDLE_TIMEOUT_SECONDS", value)

    with pytest.raises(ConfigError, match="IWIKI_IDLE_TIMEOUT_SECONDS"):
        Config.load()


@pytest.mark.anyio
async def test_idle_tracker_resets_on_activity():
    tracker = IdleTracker()
    tracker.touch()
    await anyio.sleep(0.01)
    tracker.touch()

    started = time.monotonic()
    await tracker.wait_until_idle(0.02)

    assert time.monotonic() - started >= 0.015


@pytest.mark.anyio
async def test_idle_waits_while_background_work_is_declared():
    """A declared background job is activity even with no call in flight.

    The stdio server hands `wiki_code_index` a job handle and lets the build
    run on after the caller's wait expires; shutting the process down on the
    idle timer would cancel the very build that handle points at.
    """
    working = {"value": True}
    tracker = IdleTracker(has_background_work=lambda: working["value"])

    with anyio.move_on_after(0.3) as scope:
        await tracker.wait_until_idle(0)
    assert scope.cancel_called is True

    working["value"] = False
    with anyio.move_on_after(1.0) as scope:
        await tracker.wait_until_idle(0)
    assert scope.cancel_called is False


@pytest.mark.anyio
async def test_idle_countdown_starts_when_background_work_ends(monkeypatch):
    """R3/F5: the caller gets the full idle budget to read its job handle.

    Polling the predicate used to leave `_last_activity` ageing through the
    whole build, so a build longer than the idle timeout let the session shut
    down the instant it ended -- taking the job handle with it before the
    caller's next poll could read the terminal state. The longer the build,
    the more certain the loss, which is the exact case a detached build
    exists for.
    """
    monkeypatch.setattr(idle, "BACKGROUND_POLL_SECONDS", 0.05)
    polls = {"count": 0}
    ended = {}

    def working() -> bool:
        polls["count"] += 1
        if polls["count"] <= 6:
            return True
        ended.setdefault("at", time.monotonic())
        return False

    tracker = IdleTracker(has_background_work=working)

    await tracker.wait_until_idle(0.2)

    # The work ran for ~0.3s, longer than the 0.2s timeout: without the
    # refresh the countdown is already spent when it ends and this returns
    # immediately.
    assert time.monotonic() - ended["at"] >= 0.15


@pytest.mark.anyio
async def test_idle_tracker_survives_a_raising_background_predicate(caplog):
    """A predicate that raises must not take the server loop down with it.

    Unanswerable is treated as "no background work": that degrades to the
    timer-only behaviour the tracker had before, which is bounded, rather
    than pinning the process open on a predicate that can never answer.

    That degradation is invisible from the outside, and a raising predicate
    raises on every poll forever, so the first failure is announced once at
    warning and the repetitions stay at debug.
    """
    def broken() -> bool:
        raise RuntimeError("registry unavailable")

    tracker = IdleTracker(has_background_work=broken)
    caplog.set_level(logging.DEBUG, logger="iwiki_mcp.engine.idle")

    with anyio.move_on_after(1.0) as scope:
        await tracker.wait_until_idle(0)
        await tracker.wait_until_idle(0)

    assert scope.cancel_called is False

    levels = [record.levelno for record in caplog.records]
    assert levels == [logging.WARNING, logging.DEBUG]
    assert "RuntimeError: registry unavailable" in caplog.text


@pytest.mark.anyio
async def test_idle_tracker_waits_for_active_call_to_finish():
    tracker = IdleTracker()
    tracker.begin_call()

    async def finish_call():
        await anyio.sleep(0.03)
        tracker.end_call()

    started = time.monotonic()
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(finish_call)
        await tracker.wait_until_idle(0.01)

    assert time.monotonic() - started >= 0.03
