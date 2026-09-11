"""Inactivity tracking for the stdio MCP lifecycle."""
from __future__ import annotations

import logging
import time
from typing import Callable

import anyio

LOGGER = logging.getLogger(__name__)

#: How often declared background work is re-checked while it runs. The
#: predicate belongs to another thread's state, so there is nothing to await
#: on: polling is the whole mechanism.
BACKGROUND_POLL_SECONDS = 1.0


class IdleTracker:
    """Track incoming MCP activity, running tool calls, and declared work.

    `has_background_work` lets an owner declare work that no tool call is
    waiting on any more -- a code-graph build whose caller already took its
    job handle and returned. It is polled, never awaited, and must answer
    without blocking: `wait_until_idle` runs on the event loop that serves
    the whole session.
    """

    def __init__(
        self,
        has_background_work: Callable[[], bool] | None = None,
    ) -> None:
        self._active_calls = 0
        self._last_activity = time.monotonic()
        self._changed = anyio.Event()
        self._has_background_work = has_background_work

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

    def _background_work_declared(self) -> bool:
        """Ask the predicate, treating an unanswerable one as "no work".

        The predicate runs on the event loop that owns the stdio session, so
        an exception escaping here would cancel the wait and take the server
        down -- a worse outcome than the one the predicate exists to prevent.
        Falling back to False degrades to the timer-only behaviour this
        tracker had before it existed: a bounded shutdown, which still hands
        the build its cooperative cancellation, rather than a process pinned
        open forever by a predicate that can never answer.
        """
        if self._has_background_work is None:
            return False
        try:
            return bool(self._has_background_work())
        except Exception:
            LOGGER.debug("background work predicate failed", exc_info=True)
            return False

    async def wait_until_idle(self, timeout_seconds: int) -> None:
        """Return after a quiet period with no call and no declared work."""
        while True:
            if self._active_calls:
                await self._changed.wait()
                continue
            if self._background_work_declared():
                await anyio.sleep(BACKGROUND_POLL_SECONDS)
                continue
            remaining = self._last_activity + timeout_seconds - time.monotonic()
            if remaining <= 0:
                return
            changed = self._changed
            with anyio.move_on_after(remaining):
                await changed.wait()
