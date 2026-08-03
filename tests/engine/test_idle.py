import time

import anyio
import pytest

from iwiki_mcp.engine.config import Config, ConfigError
from iwiki_mcp.engine.idle import IdleTracker


def test_idle_timeout_defaults_to_thirty_minutes(monkeypatch):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "key")

    assert Config.load().idle_timeout_seconds == 1800


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
