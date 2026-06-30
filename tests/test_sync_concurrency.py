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


def test_auto_commit_pathspec_excludes_sibling_domain(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "alpha" / "a.md").write_text("a")
    (tmp_path / "beta" / "b.md").write_text("b")

    res = sync.auto_commit(str(tmp_path), "iwiki: ingest alpha/a.md", pathspec="alpha")

    assert res["committed"] is True
    committed = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
        cwd=tmp_path, capture_output=True, text=True).stdout
    assert "alpha/a.md" in committed
    assert "beta/b.md" not in committed
    # beta is still untracked, not swept into the commit
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tmp_path, capture_output=True, text=True).stdout
    assert "beta/b.md" in porcelain


def test_sync_push_retry_on_non_fast_forward(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)],
                   check=True)

    base = tmp_path / "base"
    _init_repo(base)
    _git(base, "checkout", "-q", "-b", "main")
    (base / "a.md").write_text("1")
    sync.auto_commit(str(base), "c1")
    _git(base, "remote", "add", "origin", str(remote))
    _git(base, "push", "-q", "-u", "origin", "main")

    # A neighbor clone advances origin/main behind our back.
    nb = tmp_path / "nb"
    subprocess.run(["git", "clone", "-q", str(remote), str(nb)], check=True)
    _git(nb, "config", "user.email", "n@n")
    _git(nb, "config", "user.name", "n")
    (nb / "b.md").write_text("2")
    _git(nb, "add", "-A")
    _git(nb, "commit", "-q", "-m", "neighbor")
    _git(nb, "push", "-q", "origin", "main")

    # We commit locally on top of the now-stale main -> push is non-ff.
    (base / "c.md").write_text("3")
    sync.auto_commit(str(base), "c3")

    res = sync.sync(str(base))

    assert res["pushed"] is True
    log = subprocess.run(["git", "log", "--oneline"], cwd=base,
                         capture_output=True, text=True).stdout
    assert "neighbor" in log  # pull --rebase pulled the neighbor commit in
