"""Git operations on the shared base: auto-commit on write, and an explicit
sync (pull --rebase + push). Fail-soft: a non-repo or missing remote degrades
to a warning, never an exception."""
from __future__ import annotations

import os
import re
import subprocess
from time import sleep as _sleep
from pathlib import Path
from typing import Callable

from filelock import Timeout

from .lock import base_lock


_GRAPH_REFRESH_WARNING = "graph refresh failed; Markdown fallback will be used"


def _run(base: str, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(["git", "-C", base, *args], capture_output=True,
                          text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                          env=env)


def _head_revision(base: str) -> str | None:
    result = _run(base, "rev-parse", "--verify", "HEAD")
    if result.returncode != 0:
        return None
    revision = result.stdout.strip()
    return revision or None


def _refresh_pulled_graph(
    base: str,
    old_revision: str | None,
    new_revision: str | None,
    change_collector: Callable[[object], None] | None,
) -> str | None:
    if not old_revision or not new_revision or old_revision == new_revision:
        return None
    try:
        from .graph import refresh_revision_change

        change = refresh_revision_change(
            base, old_revision, new_revision, lock_held=True
        )
    except Exception:
        return _GRAPH_REFRESH_WARNING
    if change_collector is not None:
        try:
            change_collector(change)
        except Exception:
            pass
    return None


def _add_graph_warning(result: dict, warning: str | None) -> dict:
    if warning is None:
        return result
    existing = result.get("warning")
    if existing and warning in existing:
        return result
    result["warning"] = f"{existing}; {warning}" if existing else warning
    return result


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
    output = r.stderr.strip() or r.stdout.strip() or "git command failed"
    return _sanitize_git_output(output)


def _sanitize_git_output(output: str) -> str:
    output = re.sub(r"[a-z][a-z0-9+.-]*://[^\s'\"]+", "<remote>", output,
                    flags=re.IGNORECASE)
    output = re.sub(
        r"(?P<quote>['\"])(?:[a-z0-9._-]+@)?[a-z0-9.-]{2,}:[^\s'\"]+"
        r"(?P=quote)",
        r"\g<quote><remote>\g<quote>",
        output,
        flags=re.IGNORECASE,
    )
    output = re.sub(
        r"(?<![a-z0-9._-])[a-z0-9._-]+@[a-z0-9.-]+:[^\s'\"]+",
        "<remote>",
        output,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"(?<![a-z0-9._-])[a-z0-9.-]{2,}:"
        r"(?=[^\s'\"]*(?:/|\.git(?:[\s'\"]|$)))[^\s'\"]+",
        "<remote>",
        output,
        flags=re.IGNORECASE,
    )


def _exception_output(error: Exception) -> str:
    for value in (getattr(error, "stderr", None),
                  getattr(error, "stdout", None)):
        if value:
            if isinstance(value, bytes):
                return value.decode(errors="replace")
            return value
    return str(error)


def _classify_remote_failure(output: str) -> str:
    text = output.lower()
    if any(signature in text for signature in
           ("non-fast-forward", "fetch first")):
        return "non_fast_forward"
    if ("permission denied (publickey)" in text or
            ("could not read username" in text and
             "terminal prompts disabled" in text)):
        return "credential_unavailable"
    if "could not resolve host" in text:
        return "transport_unavailable"
    if "does not appear to be a git repository" in text:
        return "permanent"
    return "unknown"


def _is_non_ff(r: subprocess.CompletedProcess) -> bool:
    return _classify_remote_failure(r.stderr + r.stdout) == "non_fast_forward"


def sync(
    base: str,
    timeout: float = 15.0,
    push_retries: int = 3,
    *,
    _change_collector: Callable[[object], None] | None = None,
) -> dict:
    if not is_git_repo(base):
        return {"pulled": False, "pushed": False, "error": "base is not a git repo",
                "sync_attempts": 0, "push_attempts": 0}
    sync_attempts = 0
    push_attempts = 0
    pulled = False
    graph_warning: str | None = None
    try:
        with base_lock(base, timeout):
            if not _has_remote(base):
                return {"pulled": False, "pushed": False,
                        "warning": "no git remote configured; commits stay local",
                        "sync_attempts": 0, "push_attempts": 0}
            max_attempts = min(max(push_retries, 0), 3)
            recoverable = {"non_fast_forward", "credential_unavailable",
                           "transport_unavailable"}
            for attempt in range(max_attempts):
                sync_attempts = attempt + 1
                old_revision = _head_revision(base)
                pull = _run(base, "pull", "--rebase")
                if pull.returncode != 0:
                    if _has_rebase_state(base):
                        abort = _run(base, "rebase", "--abort")
                        result = {
                            "pulled": False,
                            "pushed": False,
                            "error": "pull --rebase conflict (aborted)",
                            "failure_class": "rebase_conflict",
                            "conflict": True,
                            "hint": "resolve the conflicting commits in the base "
                                    "repo, then sync again",
                            "sync_attempts": sync_attempts,
                            "push_attempts": push_attempts,
                        }
                        if abort.returncode != 0:
                            result["error"] = "pull --rebase conflict; abort failed"
                            result["hint"] = (
                                "run git rebase --abort in the base repo, then "
                                f"resolve the conflict and sync again: {_output(abort)}"
                            )
                        return result
                    failure_class = _classify_remote_failure(
                        pull.stderr + pull.stdout)
                    if failure_class in recoverable and attempt < max_attempts - 1:
                        _sleep(0.25)
                        continue
                    return {"pulled": False, "pushed": False,
                            "error": _output(pull),
                            "failure_class": failure_class,
                            "sync_attempts": sync_attempts,
                            "push_attempts": push_attempts}
                pulled = True
                new_revision = _head_revision(base)
                refresh_warning = _refresh_pulled_graph(
                    base, old_revision, new_revision, _change_collector
                )
                if refresh_warning is not None:
                    graph_warning = refresh_warning
                push_attempts += 1
                push = _run(base, "push")
                if push.returncode == 0:
                    return _add_graph_warning(
                        {"pulled": True, "pushed": True,
                         "sync_attempts": sync_attempts,
                         "push_attempts": push_attempts},
                        graph_warning,
                    )
                failure_class = _classify_remote_failure(push.stderr + push.stdout)
                if failure_class in recoverable and attempt < max_attempts - 1:
                    _sleep(0.25)
                    continue
                return _add_graph_warning(
                    {"pulled": True, "pushed": False,
                     "warning": _output(push),
                     "failure_class": failure_class,
                     "sync_attempts": sync_attempts,
                     "push_attempts": push_attempts},
                    graph_warning,
                )
            return _add_graph_warning(
                {"pulled": True, "pushed": False,
                 "warning": "push retries exhausted",
                 "sync_attempts": 0, "push_attempts": 0},
                graph_warning,
            )
    except Timeout:
        return {"pulled": False, "pushed": False,
                "warning": "base busy: lock timeout",
                "sync_attempts": 0, "push_attempts": 0}
    except Exception as e:
        output = _exception_output(e)
        result = {"pulled": pulled, "pushed": False,
                  "failure_class": _classify_remote_failure(output),
                  "sync_attempts": sync_attempts,
                  "push_attempts": push_attempts}
        result["warning" if pulled else "error"] = _sanitize_git_output(output)
        return result


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


def ensure_fresh(
    base: str,
    timeout: float = 15.0,
    *,
    _change_collector: Callable[[object], None] | None = None,
) -> dict:
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
            old_revision = _head_revision(base)
            ff = _run(base, "merge", "--ff-only", "@{upstream}")
            if ff.returncode != 0:
                return {"state": "offline", "warning": _output(ff)}
            graph_warning = _refresh_pulled_graph(
                base, old_revision, _head_revision(base), _change_collector
            )
            return _add_graph_warning({"state": "updated"}, graph_warning)
    except Timeout:
        return {"state": "offline", "warning": "base busy: lock timeout"}
    except Exception as e:
        return {"state": "offline", "warning": str(e)}


def commit_and_push(
    base: str,
    message: str,
    pathspec: str | None = None,
    *,
    _after_commit: Callable[[], str | None] | None = None,
) -> dict:
    """Auto-commit, then push via ``sync`` when the commit landed.

    Fail-soft: when nothing is committed, ``sync`` is not attempted. When the commit
    landed, any ``sync`` failure — whether ``sync`` reported it as ``warning`` (push
    rejected) or ``error`` (non-repo, pull conflict) — is surfaced under a single
    ``warning`` key; the local commit stands.
    """
    commit = auto_commit(base, message, pathspec)
    if not commit.get("committed"):
        out = {"committed": False, "pushed": False,
               "sync_attempts": 0, "push_attempts": 0}
        if commit.get("warning"):
            out["warning"] = commit["warning"]
        if (
            _after_commit is not None
            and commit.get("warning") == "nothing to commit"
        ):
            try:
                if _after_commit() is not None:
                    _add_graph_warning(out, _GRAPH_REFRESH_WARNING)
            except Exception:
                _add_graph_warning(out, _GRAPH_REFRESH_WARNING)
        return out
    graph_warning: str | None = None
    if _after_commit is not None:
        try:
            if _after_commit() is not None:
                graph_warning = _GRAPH_REFRESH_WARNING
        except Exception:
            graph_warning = _GRAPH_REFRESH_WARNING
    result = sync(base)
    out = {
        "committed": True,
        "pushed": bool(result.get("pushed")),
        "sync_attempts": result.get("sync_attempts", 0),
        "push_attempts": result.get("push_attempts", 0),
    }
    for key in ("failure_class", "conflict", "hint"):
        if key in result:
            out[key] = result[key]
    if "hint" in out:
        out["hint"] = _sanitize_git_output(str(out["hint"]))
    warn = result.get("warning") or result.get("error")
    if warn:
        out["warning"] = _sanitize_git_output(str(warn))
    _add_graph_warning(out, graph_warning)
    return out
