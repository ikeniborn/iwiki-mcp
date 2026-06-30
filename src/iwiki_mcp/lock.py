"""Inter-process lock for git mutations on the shared base.

Many iwiki-mcp servers (one per client session) can share one base repo.
This serializes all git index / push operations across processes."""
from __future__ import annotations

import os

from filelock import FileLock


def base_lock(base: str, timeout: float = 15.0) -> FileLock:
    """Return a FileLock guarding git mutations on `base`.

    The lock file lives at base/.iwiki/lock. base/.iwiki/ holds server
    metadata at the base level; it is never a domain (`.`-prefixed names are
    excluded by list_domains/domain_exists) and is never staged (commits are
    domain-scoped). Acquire blocks up to `timeout` seconds, then raises
    filelock.Timeout."""
    meta_dir = os.path.join(base, ".iwiki")
    os.makedirs(meta_dir, exist_ok=True)
    return FileLock(os.path.join(meta_dir, "lock"), timeout=timeout)
