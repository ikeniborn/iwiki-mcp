"""Git operations on the shared base: auto-commit on write, and an explicit
sync (pull --rebase + push). Fail-soft: a non-repo or missing remote degrades
to a warning, never an exception."""
from __future__ import annotations

import subprocess
from pathlib import Path

from filelock import Timeout

from .lock import base_lock


def _run(base: str, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", base, *args], capture_output=True,
                          text=True, timeout=timeout)


def is_git_repo(base: str) -> bool:
    try:
        r = _run(base, "rev-parse", "--is-inside-work-tree")
        return r.returncode == 0 and r.stdout.strip() == "true"
    except Exception:
        return False


def auto_commit(base: str, message: str, pathspec: str | None = None,
                timeout: float = 15.0) -> dict:
    if not is_git_repo(base):
        return {"committed": False, "warning": "base is not a git repo; not committing"}
    scope = ("--", pathspec) if pathspec else ()
    try:
        with base_lock(base, timeout):
            add = _run(base, "add", *(("--", pathspec) if pathspec else ("-A",)))
            if add.returncode != 0:
                return {"committed": False, "warning": add.stderr.strip()}
            status = _run(base, "status", "--porcelain", *scope)
            if status.returncode != 0:
                return {"committed": False, "warning": status.stderr.strip()}
            if not status.stdout.strip():
                return {"committed": False, "warning": "nothing to commit"}
            r = _run(base, "commit", "-m", message)
            return {"committed": r.returncode == 0,
                    **({} if r.returncode == 0 else {"warning": r.stderr.strip()})}
    except Timeout:
        return {"committed": False, "warning": "base busy: lock timeout"}
    except Exception as e:
        return {"committed": False, "warning": str(e)}


def _has_remote(base: str) -> bool:
    r = _run(base, "remote")
    return bool(r.stdout.strip())


def _has_rebase_state(base: str) -> bool:
    for name in ("rebase-merge", "rebase-apply"):
        r = _run(base, "rev-parse", "--git-path", name)
        path = Path(r.stdout.strip())
        if not path.is_absolute():
            path = Path(base) / path
        if r.returncode == 0 and path.exists():
            return True
    return False


def _output(r: subprocess.CompletedProcess) -> str:
    return r.stderr.strip() or r.stdout.strip() or "git command failed"


def _is_non_ff(r: subprocess.CompletedProcess) -> bool:
    text = (r.stderr + r.stdout).lower()
    return any(s in text for s in ("non-fast-forward", "fetch first", "rejected"))


def sync(base: str, timeout: float = 15.0, push_retries: int = 3) -> dict:
    if not is_git_repo(base):
        return {"pulled": False, "pushed": False, "error": "base is not a git repo"}
    try:
        with base_lock(base, timeout):
            if not _has_remote(base):
                return {"pulled": False, "pushed": False,
                        "warning": "no git remote configured; commits stay local"}
            for attempt in range(push_retries):
                pull = _run(base, "pull", "--rebase")
                if pull.returncode != 0:
                    if _has_rebase_state(base):
                        _run(base, "rebase", "--abort")
                        return {"pulled": False, "pushed": False,
                                "error": "pull --rebase conflict (aborted)",
                                "hint": "resolve in the base repo, or re-run index to "
                                        "regenerate a conflicted .iwiki/index.jsonl, "
                                        "then sync again"}
                    return {"pulled": False, "pushed": False, "error": _output(pull)}
                push = _run(base, "push")
                if push.returncode == 0:
                    return {"pulled": True, "pushed": True}
                if _is_non_ff(push) and attempt < push_retries - 1:
                    continue
                return {"pulled": True, "pushed": False, "warning": push.stderr.strip()}
            # only reachable if push_retries <= 0; loop otherwise always returns inside
            return {"pulled": True, "pushed": False, "warning": "push retries exhausted"}
    except Timeout:
        return {"pulled": False, "pushed": False, "warning": "base busy: lock timeout"}
    except Exception as e:
        return {"pulled": False, "pushed": False, "error": str(e)}


def _ahead_behind(base: str) -> tuple[int, int] | None:
    """(behind, ahead) relative to @{upstream}, or None if no upstream is set."""
    r = _run(base, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    if r.returncode != 0:
        return None
    parts = r.stdout.split()
    if len(parts) != 2:
        return None
    behind, ahead = parts
    return int(behind), int(ahead)


def _tree_clean(base: str) -> bool:
    r = _run(base, "status", "--porcelain")
    if r.returncode != 0:
        return False
    # Untracked files (?? lines) do not block `git merge --ff-only`, so they do
    # not count as "dirty"; only modifications to tracked files skip the ff.
    for line in r.stdout.strip().split("\n"):
        if line and not line.startswith("??"):
            return False
    return True


def ensure_fresh(base: str, timeout: float = 15.0) -> dict:
    """Bring the base up to date with its remote BEFORE a local mutation.

    Fetches, then fast-forwards when the base is cleanly behind its upstream.
    Fail-soft: returns a {"state": ...} dict, never raises. A "diverged" state
    (local commits AND remote ahead) signals the caller to refuse the write.
    """
    if not is_git_repo(base):
        return {"state": "no_repo"}
    try:
        with base_lock(base, timeout):
            if not _has_remote(base):
                return {"state": "no_remote"}
            fetch = _run(base, "fetch")
            if fetch.returncode != 0:
                return {"state": "offline", "warning": _output(fetch)}
            counts = _ahead_behind(base)
            if counts is None:
                return {"state": "no_upstream",
                        "warning": "branch has no upstream; skipped freshness check"}
            behind, ahead = counts
            if behind == 0:
                return {"state": "ahead" if ahead else "up_to_date"}
            if ahead:
                return {"state": "diverged"}
            if not _tree_clean(base):
                return {"state": "dirty",
                        "warning": "local changes present; skipped fast-forward"}
            ff = _run(base, "merge", "--ff-only", "@{upstream}")
            if ff.returncode != 0:
                return {"state": "offline", "warning": _output(ff)}
            return {"state": "updated"}
    except Timeout:
        return {"state": "offline", "warning": "base busy: lock timeout"}
    except Exception as e:
        return {"state": "offline", "warning": str(e)}


def commit_and_push(base: str, message: str, pathspec: str | None = None) -> dict:
    """Auto-commit, then push via ``sync`` when the commit landed.

    Fail-soft: when nothing is committed, ``sync`` is not attempted. When the commit
    landed, any ``sync`` failure — whether ``sync`` reported it as ``warning`` (push
    rejected) or ``error`` (non-repo, pull conflict) — is surfaced under a single
    ``warning`` key; the local commit stands.
    """
    commit = auto_commit(base, message, pathspec)
    if not commit.get("committed"):
        out = {"committed": False, "pushed": False}
        if commit.get("warning"):
            out["warning"] = commit["warning"]
        return out
    result = sync(base)
    out = {"committed": True, "pushed": bool(result.get("pushed"))}
    warn = result.get("warning") or result.get("error")
    if warn:
        out["warning"] = warn
    return out
