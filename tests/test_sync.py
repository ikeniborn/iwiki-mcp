import subprocess
from iwiki_mcp import sync


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")


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
