"""Git operations on the shared base: auto-commit on write, and an explicit
sync (pull --rebase + push). Fail-soft: a non-repo or missing remote degrades
to a warning, never an exception."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _run(base: str, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", base, *args], capture_output=True,
                          text=True, timeout=timeout)


def is_git_repo(base: str) -> bool:
    try:
        r = _run(base, "rev-parse", "--is-inside-work-tree")
        return r.returncode == 0 and r.stdout.strip() == "true"
    except Exception:
        return False


def auto_commit(base: str, message: str) -> dict:
    if not is_git_repo(base):
        return {"committed": False, "warning": "base is not a git repo; not committing"}
    try:
        add = _run(base, "add", "-A")
        if add.returncode != 0:
            return {"committed": False, "warning": add.stderr.strip()}
        status = _run(base, "status", "--porcelain")
        if status.returncode != 0:
            return {"committed": False, "warning": status.stderr.strip()}
        if not status.stdout.strip():
            return {"committed": False, "warning": "nothing to commit"}
        r = _run(base, "commit", "-m", message)
        return {"committed": r.returncode == 0,
                **({} if r.returncode == 0 else {"warning": r.stderr.strip()})}
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


def sync(base: str) -> dict:
    if not is_git_repo(base):
        return {"pulled": False, "pushed": False, "error": "base is not a git repo"}
    try:
        if not _has_remote(base):
            return {"pulled": False, "pushed": False,
                    "warning": "no git remote configured; commits stay local"}
        pull = _run(base, "pull", "--rebase")
        if pull.returncode != 0:
            if _has_rebase_state(base):
                _run(base, "rebase", "--abort")
                return {"pulled": False, "pushed": False,
                        "error": "pull --rebase conflict (aborted)",
                        "hint": "resolve in the base repo, or re-run index to regenerate "
                                "a conflicted .iwiki/index.jsonl, then sync again"}
            return {"pulled": False, "pushed": False, "error": _output(pull)}
        push = _run(base, "push")
        return {"pulled": True, "pushed": push.returncode == 0,
                **({} if push.returncode == 0 else {"warning": push.stderr.strip()})}
    except Exception as e:
        return {"pulled": False, "pushed": False, "error": str(e)}
