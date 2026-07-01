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


def _setup(tmp_path):
    """A base repo tracking origin/main with one seed commit pushed."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)],
                   check=True)
    base = tmp_path / "base"
    _init_repo(base)
    _git(base, "checkout", "-q", "-b", "main")
    (base / "seed.md").write_text("0")
    _git(base, "add", "-A")
    _git(base, "commit", "-q", "-m", "seed")
    _git(base, "remote", "add", "origin", str(remote))
    _git(base, "push", "-q", "-u", "origin", "main")
    return remote, base


def _neighbor_push(remote, tmp_path):
    """A second clone advances origin/main behind our back."""
    nb = tmp_path / "nb"
    subprocess.run(["git", "clone", "-q", str(remote), str(nb)], check=True)
    _git(nb, "config", "user.email", "n@n")
    _git(nb, "config", "user.name", "n")
    (nb / "nb.md").write_text("1")
    _git(nb, "add", "-A")
    _git(nb, "commit", "-q", "-m", "neighbor")
    _git(nb, "push", "-q", "origin", "main")


def _log(base):
    return subprocess.run(["git", "log", "--oneline"], cwd=base,
                          capture_output=True, text=True).stdout


def test_ensure_fresh_up_to_date(tmp_path):
    _remote, base = _setup(tmp_path)
    assert sync.ensure_fresh(str(base))["state"] == "up_to_date"


def test_ensure_fresh_updated_fast_forwards(tmp_path):
    remote, base = _setup(tmp_path)
    _neighbor_push(remote, tmp_path)
    res = sync.ensure_fresh(str(base))
    assert res["state"] == "updated"
    assert "neighbor" in _log(base)  # ff pulled the neighbor commit in


def test_ensure_fresh_updated_ignores_untracked(tmp_path):
    remote, base = _setup(tmp_path)
    _neighbor_push(remote, tmp_path)
    (base / "scratch.tmp").write_text("untracked")  # untracked, must not block ff
    res = sync.ensure_fresh(str(base))
    assert res["state"] == "updated"
    assert "neighbor" in _log(base)
    assert (base / "scratch.tmp").exists()  # ff did not disturb the untracked file


def test_ensure_fresh_ahead_only(tmp_path):
    _remote, base = _setup(tmp_path)
    (base / "local.md").write_text("x")
    _git(base, "add", "-A")
    _git(base, "commit", "-q", "-m", "local")
    assert sync.ensure_fresh(str(base))["state"] == "ahead"


def test_ensure_fresh_diverged_leaves_base_untouched(tmp_path):
    remote, base = _setup(tmp_path)
    _neighbor_push(remote, tmp_path)
    (base / "local.md").write_text("x")
    _git(base, "add", "-A")
    _git(base, "commit", "-q", "-m", "local")
    res = sync.ensure_fresh(str(base))
    assert res["state"] == "diverged"
    log = _log(base)
    assert "local" in log
    assert "neighbor" not in log  # no ff / rebase happened


def test_ensure_fresh_no_remote(tmp_path):
    base = tmp_path / "plain"
    _init_repo(base)
    (base / "x.md").write_text("hi")
    _git(base, "add", "-A")
    _git(base, "commit", "-q", "-m", "c")
    assert sync.ensure_fresh(str(base))["state"] == "no_remote"


def test_ensure_fresh_non_repo(tmp_path):
    assert sync.ensure_fresh(str(tmp_path))["state"] == "no_repo"
