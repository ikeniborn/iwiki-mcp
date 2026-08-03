"""Inactivity tracking for the stdio MCP lifecycle."""
from __future__ import annotations

import time

import anyio


class IdleTracker:
    """Track incoming MCP activity and running tool calls."""

    def __init__(self) -> None:
        self._active_calls = 0
        self._last_activity = time.monotonic()
        self._changed = anyio.Event()

    def touch(self) -> None:
        self._last_activity = time.monotonic()
        self._signal()

    def begin_call(self) -> None:
        self._active_calls += 1
        self.touch()

    def end_call(self) -> None:
        self._active_calls -= 1
        self.touch()

    def _signal(self) -> None:
        changed = self._changed
        self._changed = anyio.Event()
        changed.set()

    async def wait_until_idle(self, timeout_seconds: int) -> None:
        """Return only after a quiet period with no tool call in progress."""
        while True:
            if self._active_calls:
                await self._changed.wait()
                continue
            remaining = self._last_activity + timeout_seconds - time.monotonic()
            if remaining <= 0:
                return
            changed = self._changed
            with anyio.move_on_after(remaining):
                await changed.wait()
