import os
import subprocess

from iwiki_mcp import indexer, server


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def _seed_git(tmp_path, monkeypatch):
    """Base is a git repo tracking origin/main, with a `backend` domain."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)],
                   check=True)
    b = tmp_path / "wiki"
    b.mkdir()
    _git(b, "init", "-q")
    _git(b, "config", "user.email", "t@t")
    _git(b, "config", "user.name", "t")
    _git(b, "checkout", "-q", "-b", "main")
    (b / "backend" / ".iwiki").mkdir(parents=True)
    (b / ".gitkeep").write_text("")
    _git(b, "add", "-A")
    _git(b, "commit", "-q", "-m", "seed")
    _git(b, "remote", "add", "origin", str(remote))
    _git(b, "push", "-q", "-u", "origin", "main")

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text('read = ["backend"]\nwrite = "backend"\n')
    monkeypatch.setenv("IWIKI_BASE_DIR", str(b))
    monkeypatch.setenv("IWIKI_PROJECT_DIR", str(proj))
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", "2")
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, t: [[1.0, 0.0] for _ in t])
    return b, remote


def _neighbor_push(remote, tmp_path):
    nb = tmp_path / "nb"
    subprocess.run(["git", "clone", "-q", str(remote), str(nb)], check=True)
    _git(nb, "config", "user.email", "n@n")
    _git(nb, "config", "user.name", "n")
    (nb / "neighbor.md").write_text("hello")
    _git(nb, "add", "-A")
    _git(nb, "commit", "-q", "-m", "neighbor")
    _git(nb, "push", "-q", "origin", "main")


def test_write_refuses_on_diverged_with_zero_side_effects(tmp_path, monkeypatch):
    b, remote = _seed_git(tmp_path, monkeypatch)
    _neighbor_push(remote, tmp_path)
    # local commit → base is now ahead AND behind (diverged)
    (b / "local.md").write_text("x")
    _git(b, "add", "-A")
    _git(b, "commit", "-q", "-m", "local")

    md = "# Auth\n## Overview\nsummary\n## Flow\nlogin then token\n"
    out = server.wiki_write_page("backend", "auth", md)

    assert "error" in out and "diverged" in out["error"]
    assert "hint" in out
    assert not os.path.isfile(b / "backend" / "auth.md")
    assert not os.path.isfile(b / "backend" / ".iwiki" / "log.jsonl")


def test_write_fast_forwards_when_behind_then_writes(tmp_path, monkeypatch):
    b, remote = _seed_git(tmp_path, monkeypatch)
    _neighbor_push(remote, tmp_path)  # base is cleanly behind

    md = "# Auth\n## Overview\nsummary\n## Flow\nlogin then token\n"
    out = server.wiki_write_page("backend", "auth", md)

    assert out["page"] == "backend/auth.md"
    assert os.path.isfile(b / "backend" / "auth.md")
    assert os.path.isfile(b / "neighbor.md")  # ff pulled the neighbor commit in
    assert out["pushed"] is True
