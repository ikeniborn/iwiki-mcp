import json
import subprocess

import iwiki_mcp.server as server
import iwiki_mcp.indexer as indexer
from iwiki_mcp.engine import frontmatter as fm


def _bind(tmp_path):
    return server.base.Binding(base=str(tmp_path), read=("d",), write="d",
                               project_dir=str(tmp_path))


def _patch(monkeypatch, tmp_path):
    # Eager: tests write fixture pages into tmp_path/d/ before calling the
    # tool, so the domain dir must exist before resolve_binding() is (lazily)
    # invoked inside the handler.
    (tmp_path / "d").mkdir(parents=True)
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setattr(server.base, "resolve_binding", lambda: _bind(tmp_path))
    monkeypatch.setattr(server.sync, "ensure_fresh", lambda b: {"state": "clean"})
    monkeypatch.setattr(
        server.sync, "commit_and_push",
        lambda *a, **k: {"committed": True, "pushed": False}
    )
    monkeypatch.setattr(
        indexer, "embed_texts",
        lambda cfg, texts: [[0.1, 0.2] for _ in texts]
    )


def test_migrate_plan_mode_lists_candidates(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)   # no IWIKI_CHAT_MODEL
    (tmp_path / "d" / "a.md").write_text("# A\n\n## Overview\ns\n\n## B\nwords\n", encoding="utf-8")
    res = server.wiki_migrate_okf("d")
    assert res["mode"] == "plan"
    slugs = [c["slug"] for c in res["candidates"]]
    assert "a" in slugs
    assert (tmp_path / "d" / "a.md").read_text(encoding="utf-8").startswith("# A")  # no write


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _seed_git_base(tmp_path):
    """``tmp_path`` becomes a real (remoteless) git repo with domain `d`,
    holding one already-typed flat page so migrate_layout has a move to make."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "d").mkdir(parents=True)
    meta = {"type": "guide", "title": "A"}
    body = "# A\n\n## Overview\ns\n\n## B\nwords\n"
    (tmp_path / "d" / "a.md").write_text(fm.render(meta) + body, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed")


def test_migrate_plan_mode_commits_layout_move(tmp_path, monkeypatch):
    # Real git base (no remote), so the commit path is actually exercised
    # here rather than short-circuited by a mocked sync.commit_and_push.
    _seed_git_base(tmp_path)
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setattr(server.base, "resolve_binding", lambda: _bind(tmp_path))
    monkeypatch.setattr(
        indexer, "embed_texts",
        lambda cfg, texts: [[0.1, 0.2] for _ in texts]
    )
    res = server.wiki_migrate_okf("d")   # no IWIKI_CHAT_MODEL -> plan mode
    assert res["mode"] == "plan"
    assert res["moved"] == ["a -> guide/a"]
    assert res["committed"] is True
    assert res["pushed"] is False
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path,
        capture_output=True, text=True, check=True,
    )
    assert status.stdout.strip() == ""


def test_apply_okf_writes_frontmatter(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    (tmp_path / "d" / "a.md").write_text("# A\n\n## Overview\ns\n\n## B\nwords\n", encoding="utf-8")
    res = server.wiki_apply_okf("d", "a", "guide", tags=["Flow"])
    assert "error" not in res
    # a bare (untyped) slug is moved under its resolved type dir on apply
    assert res["page"] == "d/guide/a.md"
    meta, _ = fm.split((tmp_path / "d" / "guide" / "a.md").read_text(encoding="utf-8"))
    assert meta["type"] == "guide"
    assert meta["tags"] == ["flow"]


def test_migrate_autonomous_mode(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    monkeypatch.setenv("IWIKI_CHAT_MODEL", "chat-x")
    from iwiki_mcp import okf
    monkeypatch.setattr(
        okf.classify, "classify_page",
        lambda cfg, body, existing_tags: {
            "type": "guide", "tags": ["x"], "warning": None
        }
    )
    (tmp_path / "d" / "a.md").write_text("# A\n\n## Overview\ns\n\n## B\nwords\n", encoding="utf-8")
    res = server.wiki_migrate_okf("d")
    assert res["mode"] == "autonomous"
    assert "a" in res["migrated"]
    # the adoption loop adds type=guide, then migrate_layout (running AFTER the
    # loop) moves the now-typed page under its type dir in the same pass.
    assert not (tmp_path / "d" / "a.md").exists()
    assert res["moved"] == ["a -> guide/a"]
    meta, _ = fm.split((tmp_path / "d" / "guide" / "a.md").read_text(encoding="utf-8"))
    assert meta["type"] == "guide"
    # idempotent
    res2 = server.wiki_migrate_okf("d")
    assert res2["migrated"] == [] and "guide/a" in res2["skipped"]
    assert res2["moved"] == []


def test_migrate_autonomous_sets_resource_from_log(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    monkeypatch.setenv("IWIKI_CHAT_MODEL", "chat-x")
    from iwiki_mcp import okf
    monkeypatch.setattr(
        okf.classify, "classify_page",
        lambda cfg, body, existing_tags: {
            "type": "guide", "tags": ["x"], "warning": None
        }
    )
    (tmp_path / "d" / "a.md").write_text("# A\n\n## Overview\ns\n\n## B\nwords\n", encoding="utf-8")
    log_path = tmp_path / "d" / "log.jsonl"
    log_path.write_text(json.dumps({
        "op": "ingest", "source": "/src/a.py", "page": "a.md",
        "date": "2020-01-01", "src_hash": "abc",
    }) + "\n", encoding="utf-8")
    res = server.wiki_migrate_okf("d")
    assert res["mode"] == "autonomous"
    assert "a" in res["migrated"]
    # migrate_layout runs after the adoption loop, moving the newly-typed page.
    meta, _ = fm.split((tmp_path / "d" / "guide" / "a.md").read_text(encoding="utf-8"))
    assert meta["resource"] == "/src/a.py"


def test_migrate_no_domain_no_write_target_friendly_error(monkeypatch):
    monkeypatch.setattr(
        server.base, "resolve_binding",
        lambda: server.base.Binding(base="/b", read=(), write=None, project_dir="/p"),
    )
    res = server.wiki_migrate_okf()
    assert res == {
        "error": "no domain given and no write-target bound",
        "hint": "pass domain= or set write in .iwiki.toml via wiki_bind",
    }


def test_apply_okf_preserves_existing_tags_when_none(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    meta = {"type": "guide", "title": "A", "tags": ["existing"]}
    body = "# A\n\n## Overview\ns\n\n## B\nwords\n"
    (tmp_path / "d" / "a.md").write_text(fm.render(meta) + body, encoding="utf-8")
    res = server.wiki_apply_okf("d", "a", "reference", tags=None)
    assert "error" not in res
    # bare slug -> moved under the new type dir
    new_meta, _ = fm.split((tmp_path / "d" / "reference" / "a.md").read_text(encoding="utf-8"))
    assert new_meta["tags"] == ["existing"]


def test_apply_okf_preserves_existing_description_and_status(tmp_path, monkeypatch):
    # v2-shaped page: frontmatter carries the summary as `description`, body has
    # NO ## Overview (that's what triggers derive_description's empty re-derive).
    _patch(monkeypatch, tmp_path)
    meta = {"type": "concept", "title": "A", "description": "Existing summary text.",
            "status": "stable"}
    body = "# A\n\n## B\nwords\n"
    (tmp_path / "d" / "a.md").write_text(fm.render(meta) + body, encoding="utf-8")
    res = server.wiki_apply_okf("d", "a", "reference", tags=["x"])
    assert "error" not in res
    # bare slug -> moved under the new type dir
    new_meta, _ = fm.split((tmp_path / "d" / "reference" / "a.md").read_text(encoding="utf-8"))
    assert new_meta.get("description") == "Existing summary text."
    assert new_meta.get("status") == "stable"


def test_apply_okf_sets_resource_from_log(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    body = "# A\n\n## Overview\ns\n\n## B\nwords\n"
    (tmp_path / "d" / "a.md").write_text(body, encoding="utf-8")
    log_path = tmp_path / "d" / "log.jsonl"
    log_path.write_text(json.dumps({
        "op": "ingest", "source": "/src/a.py", "page": "a.md",
        "date": "2020-01-01", "src_hash": "abc",
    }) + "\n", encoding="utf-8")
    res = server.wiki_apply_okf("d", "a", "guide")
    assert "error" not in res
    # bare slug -> moved under the new type dir; the log lookup still resolves
    # by the PRE-move page name ("a.md"), since that is what ingest recorded.
    meta, _ = fm.split((tmp_path / "d" / "guide" / "a.md").read_text(encoding="utf-8"))
    assert meta["resource"] == "/src/a.py"


def test_apply_okf_rollback_on_index_failure(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    body = "# A\n\n## Overview\ns\n\n## B\nwords\n"
    page_path = tmp_path / "d" / "a.md"
    page_path.write_text(body, encoding="utf-8")
    before = page_path.read_bytes()

    def boom(cfg, base, domain):
        raise RuntimeError("index failed")

    monkeypatch.setattr(indexer, "index_domain", boom)
    res = server.wiki_apply_okf("d", "a", "guide")
    assert "error" in res
    # bare slug -> moved under the new type dir; the move is authoritative and
    # is NOT undone by the rollback -- original bytes are restored at the NEW
    # path, the old flat path is gone.
    assert not page_path.exists()
    assert (tmp_path / "d" / "guide" / "a.md").read_bytes() == before
