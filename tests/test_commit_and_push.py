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

    called = {}

    def fake_sync(b, **k):
        called["base"] = b
        return {"pulled": True, "pushed": True}

    monkeypatch.setattr(sync, "sync", fake_sync)
    out = sync.commit_and_push(str(base), "msg", pathspec="backend")

    assert out["committed"] is True
    assert out["pushed"] is True
    assert called["base"] == str(base)


def test_commit_and_push_surfaces_sync_warning(tmp_path, monkeypatch):
    base = tmp_path / "wiki"
    _init_repo(base)
    (base / "backend").mkdir()
    (base / "backend" / "page.md").write_text("# P\n## Overview\nx\n")

    monkeypatch.setattr(
        sync, "sync",
        lambda b, **k: {"pulled": True, "pushed": False, "warning": "no remote"},
    )
    out = sync.commit_and_push(str(base), "msg", pathspec="backend")

    assert out["committed"] is True
    assert out["pushed"] is False
    assert out["warning"] == "no remote"


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
    assert calls["n"] == 0
