import os
import subprocess

import pytest

from iwiki_mcp import sync


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")


def test_run_disables_git_prompts_and_stdin(monkeypatch):
    calls = []
    monkeypatch.setenv("IWIKI_TEST_SENTINEL", "preserved")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    sync._run("/base", "status")

    argv, kwargs = calls[0]
    assert argv == ["git", "-C", "/base", "status"]
    assert "shell" not in kwargs
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["env"] is not os.environ
    assert kwargs["env"]["IWIKI_TEST_SENTINEL"] == "preserved"
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("! [rejected] main -> main (fetch first)", "non_fast_forward"),
        ("git@host: Permission denied (publickey).", "credential_unavailable"),
        (
            "fatal: could not read Username: terminal prompts disabled",
            "credential_unavailable",
        ),
        (
            "fatal: unable to access: Could not resolve host",
            "transport_unavailable",
        ),
        (
            "fatal: remote origin does not appear to be a git repository",
            "permanent",
        ),
        ("! [rejected] main -> main (hook declined)", "unknown"),
        ("fatal: could not read Username for 'https://host'", "unknown"),
        ("unexpected text", "unknown"),
    ],
)
def test_classify_remote_failure(output, expected):
    assert sync._classify_remote_failure(output) == expected


def test_sanitize_git_output_redacts_credential_bearing_remote_url():
    output = "fatal: unable to access 'https://user:secret@host/path': denied"

    sanitized = sync._sanitize_git_output(output)

    assert "user" not in sanitized
    assert "secret" not in sanitized
    assert "host" not in sanitized
    assert sanitized == "fatal: unable to access '<remote>': denied"


def test_sanitize_git_output_redacts_plain_remote_url():
    output = "fatal: unable to access 'https://host/private/repo.git': unavailable"

    sanitized = sync._sanitize_git_output(output)

    assert "host" not in sanitized
    assert "/private/repo.git" not in sanitized
    assert sanitized == "fatal: unable to access '<remote>': unavailable"


def test_sanitize_git_output_redacts_scp_remote_without_false_positives():
    output = "fatal: 'git@host:private/repo.git' unavailable"

    sanitized = sync._sanitize_git_output(output)

    assert "host" not in sanitized
    assert "private/repo.git" not in sanitized
    assert sanitized == "fatal: '<remote>' unavailable"
    assert (
        sync._sanitize_git_output("fatal: operation failed")
        == "fatal: operation failed"
    )
    assert sync._sanitize_git_output(r"C:\repo\page.md") == r"C:\repo\page.md"


def test_sanitize_git_output_redacts_unquoted_scp_remote_without_user():
    output = "fatal: repository host.example:private/repo.git not found"

    sanitized = sync._sanitize_git_output(output)

    assert "host.example" not in sanitized
    assert "private/repo.git" not in sanitized
    assert sanitized == "fatal: repository <remote> not found"
    assert (
        sync._sanitize_git_output("fatal: operation failed")
        == "fatal: operation failed"
    )
    assert sync._sanitize_git_output(r"C:\repo\page.md") == r"C:\repo\page.md"


def test_sanitize_git_output_redacts_simple_host_scp_remote_safely():
    output = "fatal: repository host:private/repo.git not found"

    sanitized = sync._sanitize_git_output(output)

    assert sanitized == "fatal: repository <remote> not found"
    assert sync._sanitize_git_output(r"C:\repo\page.md") == r"C:\repo\page.md"
    assert sync._sanitize_git_output("C:/repo/page.md") == "C:/repo/page.md"
    assert sync._sanitize_git_output("fatal: operation failed") == "fatal: operation failed"
    assert sync._sanitize_git_output("key:value") == "key:value"


def test_sanitize_git_output_redacts_user_scp_remote_without_path_guard():
    output = "fatal: repository git@host:repo not found"

    sanitized = sync._sanitize_git_output(output)

    assert sanitized == "fatal: repository <remote> not found"


def test_auto_commit_in_repo(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "x.md").write_text("hi")
    res = sync.auto_commit(str(tmp_path), "iwiki: test")
    assert res["committed"] is True
    log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path,
                         capture_output=True, text=True).stdout
    assert "iwiki: test" in log


def test_auto_commit_non_repo_warns(tmp_path):
    (tmp_path / "x.md").write_text("hi")
    res = sync.auto_commit(str(tmp_path), "iwiki: test")
    assert res["committed"] is False
    assert "warning" in res


def test_sync_no_remote_warns(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "x.md").write_text("hi")
    sync.auto_commit(str(tmp_path), "iwiki: c")
    res = sync.sync(str(tmp_path))
    assert res.get("pushed") is False
    assert "warning" in res or "error" in res


def test_sync_pull_failure_preserves_non_conflict_error(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "x.md").write_text("hi")
    sync.auto_commit(str(tmp_path), "iwiki: c")
    _git(tmp_path, "remote", "add", "origin", str(tmp_path / "missing-remote.git"))

    res = sync.sync(str(tmp_path))

    assert res["pushed"] is False
    assert "error" in res
    assert res["error"] != "pull --rebase conflict (aborted)"


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["git"], returncode, stdout, stderr)


def _script_sync(monkeypatch, results):
    script = iter(results)
    sleeps = []
    monkeypatch.setattr(sync, "is_git_repo", lambda base: True)
    monkeypatch.setattr(sync, "_has_remote", lambda base: True)
    monkeypatch.setattr(sync, "_has_rebase_state", lambda base: False)
    monkeypatch.setattr(sync, "_run", lambda *args, **kwargs: next(script))
    monkeypatch.setattr(sync.time, "sleep", sleeps.append)
    return sleeps


def test_sync_first_attempt_success_reports_attempt_counts(monkeypatch, tmp_path):
    sleeps = _script_sync(monkeypatch, [_completed(), _completed()])

    result = sync.sync(str(tmp_path))

    assert result == {
        "pulled": True,
        "pushed": True,
        "sync_attempts": 1,
        "push_attempts": 1,
    }
    assert sleeps == []


def test_sync_recovers_after_pull_credential_failure(monkeypatch, tmp_path):
    credential_failure = _completed(
        1, stderr="fatal: could not read Username: terminal prompts disabled"
    )
    sleeps = _script_sync(
        monkeypatch, [credential_failure, _completed(), _completed()]
    )

    result = sync.sync(str(tmp_path))

    assert result == {
        "pulled": True,
        "pushed": True,
        "sync_attempts": 2,
        "push_attempts": 1,
    }
    assert sleeps == [0.25]


def test_sync_recovers_after_push_credential_failure(monkeypatch, tmp_path):
    credential_failure = _completed(
        1, stderr="git@host: Permission denied (publickey)."
    )
    sleeps = _script_sync(
        monkeypatch,
        [_completed(), credential_failure, _completed(), _completed()],
    )

    result = sync.sync(str(tmp_path))

    assert result == {
        "pulled": True,
        "pushed": True,
        "sync_attempts": 2,
        "push_attempts": 2,
    }
    assert sleeps == [0.25]


def test_sync_retry_exhaustion_reports_sanitized_warning(monkeypatch, tmp_path):
    credential_failure = _completed(
        1,
        stderr=(
            "fatal: could not read Username for "
            "'https://user:secret@host/wiki': terminal prompts disabled"
        ),
    )
    sleeps = _script_sync(
        monkeypatch,
        [
            _completed(), credential_failure,
            _completed(), credential_failure,
            _completed(), credential_failure,
        ],
    )

    result = sync.sync(str(tmp_path))

    assert result == {
        "pulled": True,
        "pushed": False,
        "warning": (
            "fatal: could not read Username for "
            "'<remote>': terminal prompts disabled"
        ),
        "failure_class": "credential_unavailable",
        "sync_attempts": 3,
        "push_attempts": 3,
    }
    assert sleeps == [0.25, 0.25]


@pytest.mark.parametrize(
    ("failure", "failure_class"),
    [
        (
            "fatal: remote origin does not appear to be a git repository",
            "permanent",
        ),
        ("unexpected pull failure", "unknown"),
    ],
)
def test_sync_permanent_or_unknown_pull_failure_stops_immediately(
    monkeypatch, tmp_path, failure, failure_class
):
    sleeps = _script_sync(monkeypatch, [_completed(1, stderr=failure)])

    result = sync.sync(str(tmp_path))

    assert result == {
        "pulled": False,
        "pushed": False,
        "error": failure,
        "failure_class": failure_class,
        "sync_attempts": 1,
        "push_attempts": 0,
    }
    assert sleeps == []


def test_sync_unknown_push_failure_stops_immediately(monkeypatch, tmp_path):
    failure = "! [rejected] main -> main (hook declined)"
    sleeps = _script_sync(
        monkeypatch, [_completed(), _completed(1, stderr=failure)]
    )

    result = sync.sync(str(tmp_path))

    assert result == {
        "pulled": True,
        "pushed": False,
        "warning": failure,
        "failure_class": "unknown",
        "sync_attempts": 1,
        "push_attempts": 1,
    }
    assert sleeps == []


def test_sync_pull_timeout_preserves_live_attempt_state(monkeypatch, tmp_path):
    sleeps = _script_sync(monkeypatch, [])
    timeout = subprocess.TimeoutExpired(
        ["git", "pull"], 30, stderr="fatal: unable to access 'https://host/repo'"
    )
    def raise_timeout(*args, **kwargs):
        raise timeout

    monkeypatch.setattr(sync, "_run", raise_timeout)

    result = sync.sync(str(tmp_path))

    assert result == {
        "pulled": False,
        "pushed": False,
        "error": "fatal: unable to access '<remote>'",
        "failure_class": "unknown",
        "sync_attempts": 1,
        "push_attempts": 0,
    }
    assert sleeps == []


def test_sync_push_timeout_preserves_live_attempt_state(monkeypatch, tmp_path):
    sleeps = _script_sync(monkeypatch, [])
    timeout = subprocess.TimeoutExpired(
        ["git", "push"], 30, stderr="fatal: unable to access 'https://host/repo'"
    )
    script = iter([_completed(), timeout])

    def scripted_run(*args, **kwargs):
        result = next(script)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(sync, "_run", scripted_run)

    result = sync.sync(str(tmp_path))

    assert result == {
        "pulled": True,
        "pushed": False,
        "warning": "fatal: unable to access '<remote>'",
        "failure_class": "unknown",
        "sync_attempts": 1,
        "push_attempts": 1,
    }
    assert sleeps == []
