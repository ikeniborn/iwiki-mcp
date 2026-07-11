import subprocess

from iwiki_mcp import sync


def _git(base, *args):
    subprocess.run(["git", "-C", str(base), *args], check=True, capture_output=True)


def _init_repo(base):
    base.mkdir(parents=True, exist_ok=True)
    _git(base, "init", "-q")
    _git(base, "config", "user.email", "REDACTED")
    _git(base, "config", "user.name", "t")


def test_commit_and_push_commits_then_calls_sync(tmp_path, monkeypatch):
    base = tmp_path / "wiki"
    _init_repo(base)
    (base / "backend").mkdir()
    (base / "backend" / "page.md").write_text("# P\n## Overview\nx\n")

    called = {"commits": 0}

    real_auto_commit = sync.auto_commit

    def counting_auto_commit(*args, **kwargs):
        called["commits"] += 1
        return real_auto_commit(*args, **kwargs)

    def fake_sync(b, **k):
        called["base"] = b
        return {
            "pulled": True,
            "pushed": True,
            "sync_attempts": 3,
            "push_attempts": 2,
            "internal": "do not expose",
        }

    monkeypatch.setattr(sync, "auto_commit", counting_auto_commit)
    monkeypatch.setattr(sync, "sync", fake_sync)
    out = sync.commit_and_push(str(base), "msg", pathspec="backend")

    assert out["committed"] is True
    assert out["pushed"] is True
    assert out["sync_attempts"] == 3
    assert out["push_attempts"] == 2
    assert "pulled" not in out
    assert "internal" not in out
    assert called["commits"] == 1
    assert called["base"] == str(base)


def test_commit_and_push_surfaces_sync_warning(tmp_path, monkeypatch):
    base = tmp_path / "wiki"
    _init_repo(base)
    (base / "backend").mkdir()
    (base / "backend" / "page.md").write_text("# P\n## Overview\nx\n")

    monkeypatch.setattr(
        sync, "sync",
        lambda b, **k: {
            "pulled": True,
            "pushed": False,
            "warning": "push to https://user:secret@example.com/wiki.git failed",
            "failure_class": "transport_unavailable",
            "sync_attempts": 3,
            "push_attempts": 3,
            "debug": "secret",
        },
    )
    out = sync.commit_and_push(str(base), "msg", pathspec="backend")

    assert out["committed"] is True
    assert out["pushed"] is False
    assert out["warning"] == "push to <remote> failed"
    assert out["failure_class"] == "transport_unavailable"
    assert out["sync_attempts"] == 3
    assert out["push_attempts"] == 3
    assert "debug" not in out


def test_commit_and_push_non_repo_is_fail_soft_and_skips_sync(tmp_path, monkeypatch):
    base = tmp_path / "plain"
    base.mkdir()
    calls = {"n": 0}

    def fake_sync(b, **k):
        calls["n"] += 1
        return {}

    monkeypatch.setattr(sync, "sync", fake_sync)
    out = sync.commit_and_push(str(base), "msg")

    assert out["committed"] is False
    assert out["pushed"] is False
    assert out["sync_attempts"] == 0
    assert out["push_attempts"] == 0
    assert calls["n"] == 0


def test_commit_and_push_surfaces_sync_error_as_warning(tmp_path, monkeypatch):
    base = tmp_path / "wiki"
    _init_repo(base)
    (base / "backend").mkdir()
    (base / "backend" / "page.md").write_text("# P\n## Overview\nx\n")

    monkeypatch.setattr(
        sync, "sync",
        lambda b, **k: {
            "pulled": False,
            "pushed": False,
            "error": "conflict at ssh://user:secret@example.com/wiki.git",
            "failure_class": "rebase_conflict",
            "conflict": True,
            "hint": "resolve the conflicting commits, then sync again",
            "sync_attempts": 1,
            "push_attempts": 0,
            "abort_output": "do not expose",
        },
    )
    out = sync.commit_and_push(str(base), "msg", pathspec="backend")

    assert out["committed"] is True
    assert out["pushed"] is False
    assert out["warning"] == "conflict at <remote>"
    assert out["failure_class"] == "rebase_conflict"
    assert out["conflict"] is True
    assert out["hint"] == "resolve the conflicting commits, then sync again"
    assert out["sync_attempts"] == 1
    assert out["push_attempts"] == 0
    assert "error" not in out
    assert "abort_output" not in out
