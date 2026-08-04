import os
import subprocess

from iwiki_mcp import base, indexer, server
from iwiki_mcp.engine.graph_store import GraphStore


def _seed(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
    b.mkdir()
    (b / "backend").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text('read = ["backend"]\nwrite = "backend"\n')
    monkeypatch.setenv("IWIKI_BASE_DIR", str(b))
    monkeypatch.setenv("IWIKI_PROJECT_DIR", str(proj))
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", "2")
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, t: [[1.0, 0.0] for _ in t])
    return str(b)


def _write():
    return server.wiki_write_page(
        "backend", "auth", "# Auth\n## Overview\nsummary\n## Flow\nlogin then token\n"
    )


def _git(cwd, *args):
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    )


def _init_git_base(base_dir):
    _git(base_dir, "init", "-q")
    _git(base_dir, "config", "user.email", "t@t")
    _git(base_dir, "config", "user.name", "t")
    (base_dir / "backend" / "seed.md").write_text(
        "# Seed\n\n## Notes\nseed\n", encoding="utf-8"
    )
    _git(base_dir, "add", "-A")
    _git(base_dir, "commit", "-q", "-m", "seed")


def test_delete_removes_file_log_and_index_records(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    _write()
    # no type/chat model -> default "concept"; addressed by full identity.
    out = server.wiki_delete_page("backend", "concept/auth")
    assert out["deleted"] == "backend/concept/auth.md"
    assert not os.path.exists(os.path.join(b, "backend", "concept", "auth.md"))
    log_text = open(base.log_path(b, "backend"), encoding="utf-8").read()
    assert '"op": "delete"' in log_text
    ip = base.index_path(b, "backend")
    index_text = open(ip, encoding="utf-8").read() if os.path.exists(ip) else ""
    assert "auth.md" not in index_text


def test_delete_page_removes_graph_page_and_keeps_domain_ready(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    _init_git_base(tmp_path / "wiki")
    _write()

    out = server.wiki_delete_page("backend", "concept/auth")

    assert out["deleted"] == "backend/concept/auth.md"
    snapshot = GraphStore(b).load_ready_domain("backend")
    assert {page.file for page in snapshot.pages} == {"seed.md"}


def test_delete_last_page_leaves_empty_index(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    _write()
    out = server.wiki_delete_page("backend", "concept/auth")
    assert out["indexed_chunks"] == 0


def test_delete_missing_page_errors(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_delete_page("backend", "ghost")
    assert "error" in out and "not found" in out["error"]


def test_delete_unknown_domain_errors(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_delete_page("nope", "auth")
    assert "error" in out


def test_delete_rejects_existing_domain_outside_scope_before_freshness(
    tmp_path, monkeypatch
):
    _seed(tmp_path, monkeypatch)
    other = tmp_path / "wiki" / "other"
    other.mkdir()
    page = other / "page.md"
    page.write_text("# Page\n\n## Body\nold\n", encoding="utf-8")
    (tmp_path / "proj" / ".iwiki.toml").write_text(
        'read = ["backend", "other"]\nwrite = "backend"\n', encoding="utf-8"
    )
    monkeypatch.setattr(
        server.sync,
        "ensure_fresh",
        lambda *_: (_ for _ in ()).throw(AssertionError("freshness called")),
    )

    out = server.wiki_delete_page("other", "page")

    assert "outside bound write scope" in out["error"]
    assert page.is_file()


def test_delete_rolls_back_on_index_failure(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    _write()
    monkeypatch.setattr(
        indexer,
        "index_domain",
        lambda cfg, base, domain: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    out = server.wiki_delete_page("backend", "concept/auth")
    assert "error" in out
    assert os.path.exists(os.path.join(b, "backend", "concept", "auth.md"))
    log_text = open(base.log_path(b, "backend"), encoding="utf-8").read()
    assert '"op": "delete"' not in log_text


def test_delete_invalid_slug_errors(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_delete_page("backend", "../escape")
    assert "error" in out
