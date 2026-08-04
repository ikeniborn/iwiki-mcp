"""Inter-process lock for git mutations on the shared base.

Many iwiki-mcp servers (one per client session) can share one base repo.
This serializes all git index / push operations across processes."""
from __future__ import annotations

import os
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar

from filelock import FileLock


_HELD_BASE: ContextVar[str | None] = ContextVar("iwiki_held_base", default=None)


def base_lock(base: str, timeout: float = 15.0):
    """Return a FileLock guarding git mutations on `base`.

    The lock file lives at base/.iwiki/lock. base/.iwiki/ holds server
    metadata at the base level; it is never a domain (`.`-prefixed names are
    excluded by list_domains/domain_exists) and is never staged (commits are
    domain-scoped). Acquire blocks up to `timeout` seconds, then raises
    filelock.Timeout."""
    normalized = os.path.abspath(base)
    if _HELD_BASE.get() == normalized:
        return nullcontext()
    meta_dir = os.path.join(normalized, ".iwiki")
    os.makedirs(meta_dir, exist_ok=True)
    return FileLock(os.path.join(meta_dir, "lock"), timeout=timeout)


@contextmanager
def mutation_lock(base: str, timeout: float = 15.0):
    """Hold one base lock and make same-context nested acquisitions no-ops."""
    normalized = os.path.abspath(base)
    if _HELD_BASE.get() == normalized:
        yield
        return
    meta_dir = os.path.join(normalized, ".iwiki")
    os.makedirs(meta_dir, exist_ok=True)
    lock = FileLock(os.path.join(meta_dir, "lock"), timeout=timeout)
    with lock:
        token = _HELD_BASE.set(normalized)
        try:
            yield
        finally:
            _HELD_BASE.reset(token)
