"""Git operations on the shared base: auto-commit on write, and an explicit
sync (pull --rebase + push). Fail-soft: a non-repo or missing remote degrades
to a warning, never an exception."""
from __future__ import annotations

import subprocess


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


def sync(base: str) -> dict:
    if not is_git_repo(base):
        return {"pulled": False, "pushed": False, "error": "base is not a git repo"}
    if not _has_remote(base):
        return {"pulled": False, "pushed": False,
                "warning": "no git remote configured; commits stay local"}
    try:
        pull = _run(base, "pull", "--rebase")
        if pull.returncode != 0:
            _run(base, "rebase", "--abort")
            return {"pulled": False, "pushed": False,
                    "error": "pull --rebase conflict (aborted)",
                    "hint": "resolve in the base repo, or re-run index to regenerate "
                            "a conflicted .iwiki/index.jsonl, then sync again"}
        push = _run(base, "push")
        return {"pulled": True, "pushed": push.returncode == 0,
                **({} if push.returncode == 0 else {"warning": push.stderr.strip()})}
    except Exception as e:
        return {"pulled": False, "pushed": False, "error": str(e)}
