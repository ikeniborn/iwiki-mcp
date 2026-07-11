import subprocess

from iwiki_mcp import sync


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _clone(remote, path):
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")


def _commit(repo, path, content, message):
    (repo / path).write_text(content)
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _parallel_repos(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(remote)],
        check=True,
    )
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "Test User")
    _commit(seed, "shared.md", "base\n", "initial")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-q", "-u", "origin", "main")

    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    _clone(remote, repo_a)
    _clone(remote, repo_b)
    return remote, repo_a, repo_b


def test_sync_rebases_non_overlapping_parallel_commit_and_pushes(tmp_path):
    remote, repo_a, repo_b = _parallel_repos(tmp_path)
    remote_commit = _commit(repo_a, "a.md", "from a\n", "commit a")
    _git(repo_a, "push", "-q")
    _commit(repo_b, "b.md", "from b\n", "commit b")

    result = sync.sync(str(repo_b))

    assert result == {
        "pulled": True,
        "pushed": True,
        "sync_attempts": 1,
        "push_attempts": 1,
    }
    remote_head = _git(remote, "rev-parse", "refs/heads/main")
    assert remote_head == _git(repo_b, "rev-parse", "HEAD")
    assert remote_commit in _git(repo_b, "rev-list", "HEAD").splitlines()


def test_sync_aborts_true_rebase_conflict_without_retry_or_commit_loss(
    tmp_path, monkeypatch
):
    remote, repo_a, repo_b = _parallel_repos(tmp_path)
    remote_commit = _commit(repo_a, "shared.md", "from a\n", "commit a")
    _git(repo_a, "push", "-q")
    local_commit = _commit(repo_b, "shared.md", "from b\n", "commit b")
    sleeps = []
    monkeypatch.setattr(sync.time, "sleep", sleeps.append)

    result = sync.sync(str(repo_b))

    assert result["pushed"] is False
    assert result["failure_class"] == "rebase_conflict"
    assert result["conflict"] is True
    assert "resolve" in result["hint"].lower()
    assert result["sync_attempts"] == 1
    assert result["push_attempts"] == 0
    assert sleeps == []
    assert _git(repo_b, "rev-parse", "HEAD") == local_commit
    assert _git(remote, "rev-parse", "refs/heads/main") == remote_commit
    git_dir = repo_b / ".git"
    assert not (git_dir / "rebase-merge").exists()
    assert not (git_dir / "rebase-apply").exists()
