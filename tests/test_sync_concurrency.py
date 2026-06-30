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
    assert "beta" in porcelain
