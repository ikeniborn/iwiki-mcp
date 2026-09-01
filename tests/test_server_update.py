import json
import os
import hashlib
import inspect
import subprocess

import pytest

from iwiki_mcp import base, indexer, server
from iwiki_mcp.engine.graph_store import GraphStore


def _seed(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
    b.mkdir()
    (b / "backend").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text(
        'read = ["backend"]\nwrite = ["backend"]\nprimary = "backend"\n'
    )
    monkeypatch.setenv("IWIKI_BASE_DIR", str(b))
    monkeypatch.setenv("IWIKI_PROJECT_DIR", str(proj))
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", "2")
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, t: [[1.0, 0.0] for _ in t])
    return str(b), str(proj)


def _write(md, source=None):
    return server.wiki_write_page("backend", "auth", md, source=source)


BASE_MD = "# Auth\n## Overview\nsummary\n## Flow\nlogin then token\n"


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


def test_update_edits_section_and_returns_pushed_key(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch)
    _write(BASE_MD)
    # no type/chat model -> default "concept"; addressed by full identity.
    out = server.wiki_update_page("backend", "concept/auth", "Flow", "refreshed flow text")
    assert out["page"] == "backend/concept/auth.md"
    assert out["heading"] == "Flow"
    assert "pushed" in out and "committed" in out
    content = open(os.path.join(b, "backend", "concept", "auth.md"), encoding="utf-8").read()
    assert "refreshed flow text" in content
    assert "login then token" not in content
    assert "transaction_id" not in out
    assert "rewritten_pages" not in out


def test_update_public_signature_keeps_section_positionals_and_adds_trailing_code():
    signature = inspect.signature(server.wiki_update_page)
    assert list(signature.parameters) == [
        "domain",
        "slug",
        "heading",
        "new_body",
        "source",
        "description",
        "status",
        "new_heading",
        "expected_revision",
        "expected_section_hash",
        "code",
    ]
    assert signature.parameters["heading"].default is None
    assert signature.parameters["new_body"].default is None
    assert signature.parameters["new_heading"].default is None
    assert signature.parameters["expected_section_hash"].default is None
    assert signature.parameters["code"].default is None


@pytest.mark.parametrize(
    ("kwargs", "expected_error"),
    [
        ({"heading": "Flow"}, "heading and new_body must be provided together"),
        ({"new_body": "Changed."}, "heading and new_body must be provided together"),
        ({}, "no update operation requested"),
    ],
)
def test_update_rejects_invalid_operation_shapes_before_freshness(
    tmp_path, monkeypatch, kwargs, expected_error
):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        server.cross_domain,
        "recover_pending_transactions",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("recovery called")
        ),
    )
    monkeypatch.setattr(
        server.sync,
        "ensure_fresh",
        lambda *_: (_ for _ in ()).throw(AssertionError("freshness called")),
    )

    result = server.wiki_update_page("backend", "concept/auth", **kwargs)

    assert result["error"] == expected_error
    assert all(name in result["hint"] for name in ("heading", "new_body", "code"))


@pytest.mark.parametrize(
    "reserved",
    [
        {"source": "source.md"},
        {"description": "description"},
        {"status": "stable"},
        {"new_heading": "Renamed"},
        {"expected_section_hash": "0123456789abcdef"},
    ],
)
def test_code_only_update_rejects_section_metadata_before_freshness(
    tmp_path, monkeypatch, reserved
):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        server.cross_domain,
        "recover_pending_transactions",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("recovery called")
        ),
    )
    monkeypatch.setattr(
        server.sync,
        "ensure_fresh",
        lambda *_: (_ for _ in ()).throw(AssertionError("freshness called")),
    )

    result = server.wiki_update_page(
        "backend", "concept/auth", code={"files": ["src/auth.py"]}, **reserved
    )

    assert result["error"] == "code-only update cannot change section metadata"
    assert all(name in result["hint"] for name in ("heading", "new_body", "code"))


def test_update_page_section_hash_mismatch_returns_conflict(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nold\n",
    )
    out = server.wiki_update_page(
        "backend", "concept/auth", "Flow", "new",
        expected_section_hash="0000000000000000",
    )
    assert out["error"] == "section_conflict"
    assert "current_section_hash" in out


def test_update_page_section_hash_match_succeeds(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nold\n",
    )
    current = server.wiki_read_page("backend", "concept/auth", heading="Flow")
    out = server.wiki_update_page(
        "backend", "concept/auth", "Flow", "new",
        expected_section_hash=current["section_hash"],
    )
    assert "error" not in out
    read = server.wiki_read_page("backend", "concept/auth", heading="Flow")
    assert read["body"] == "new"


def test_update_page_section_hash_omitted_behaves_as_before(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nold\n",
    )
    out = server.wiki_update_page("backend", "concept/auth", "Flow", "new")
    assert "error" not in out
    read = server.wiki_read_page("backend", "concept/auth", heading="Flow")
    assert read["body"] == "new"


def test_update_page_stale_section_hash_after_concurrent_write(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nold\n",
    )
    stale = server.wiki_read_page("backend", "concept/auth", heading="Flow")["section_hash"]
    # someone else updates the section first
    server.wiki_update_page("backend", "concept/auth", "Flow", "someone else's edit")
    out = server.wiki_update_page(
        "backend", "concept/auth", "Flow", "my edit",
        expected_section_hash=stale,
    )
    assert out["error"] == "section_conflict"


def test_update_page_refreshes_only_changed_graph_page(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch)
    _init_git_base(tmp_path / "wiki")
    _write(BASE_MD)
    store = GraphStore(b)
    before = {page.file: page.content_hash for page in store.load_ready_domain("backend").pages}

    out = server.wiki_update_page(
        "backend", "concept/auth", "Flow", "updated graph body"
    )

    assert "error" not in out
    after = {page.file: page.content_hash for page in store.load_ready_domain("backend").pages}
    content = (tmp_path / "wiki" / "backend" / "concept" / "auth.md").read_bytes()
    assert after["concept/auth.md"] == hashlib.sha256(content).hexdigest()
    assert after["seed.md"] == before["seed.md"]


def test_update_page_not_found(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_update_page("backend", "nope", "Flow", "x")
    assert "error" in out and "not found" in out["error"]


def test_update_rejects_existing_domain_outside_scope_before_freshness(
    tmp_path, monkeypatch
):
    b, proj = _seed(tmp_path, monkeypatch)
    other = tmp_path / "wiki" / "other"
    other.mkdir()
    page = other / "page.md"
    page.write_text("# Page\n\n## Body\nold\n", encoding="utf-8")
    (tmp_path / "proj" / ".iwiki.toml").write_text(
        'read = ["backend", "other"]\nwrite = ["backend"]\nprimary = "backend"\n', encoding="utf-8"
    )
    monkeypatch.setattr(
        server.sync,
        "ensure_fresh",
        lambda *_: (_ for _ in ()).throw(AssertionError("freshness called")),
    )

    out = server.wiki_update_page("other", "page", "Body", "new")

    assert "outside bound write scope" in out["error"]
    assert page.read_text(encoding="utf-8").endswith("old\n")


def test_update_missing_heading(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    _write(BASE_MD)
    out = server.wiki_update_page("backend", "concept/auth", "Nonexistent", "y")
    assert "error" in out and "not found" in out["error"]


def test_update_rejects_deep_heading_in_body(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    _write(BASE_MD)
    out = server.wiki_update_page("backend", "concept/auth", "Flow", "### too deep\ny")
    assert "error" in out


def test_update_upserts_log_when_source_given(tmp_path, monkeypatch):
    b, proj = _seed(tmp_path, monkeypatch)
    src = os.path.join(proj, "src.txt")
    open(src, "w").write("v1")
    _write(BASE_MD, source=src)
    open(src, "w").write("v2")

    out = server.wiki_update_page("backend", "concept/auth", "Flow", "new", source=src)
    assert "error" not in out

    text = open(base.log_path(b, "backend"), encoding="utf-8").read()
    recs = [json.loads(line) for line in text.splitlines() if line.strip()]
    ingest = [r for r in recs if r.get("op") == "ingest" and r["page"] == "concept/auth.md"]
    assert len(ingest) == 1
    assert ingest[0]["source"] == "src.txt"


def test_update_rolls_back_file_and_log_on_index_failure(tmp_path, monkeypatch):
    b, proj = _seed(tmp_path, monkeypatch)
    src = os.path.join(proj, "src.txt")
    open(src, "w").write("v1")
    _write(BASE_MD, source=src)
    log_before = open(base.log_path(b, "backend"), encoding="utf-8").read()

    monkeypatch.setattr(
        indexer, "index_domain",
        lambda cfg, base, domain: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    open(src, "w").write("v2")
    out = server.wiki_update_page("backend", "concept/auth", "Flow", "newbody", source=src)

    assert "error" in out
    content = open(os.path.join(b, "backend", "concept", "auth.md"), encoding="utf-8").read()
    assert "login then token" in content and "newbody" not in content
    assert open(base.log_path(b, "backend"), encoding="utf-8").read() == log_before


def test_update_removes_log_it_created_on_rollback(tmp_path, monkeypatch):
    b, proj = _seed(tmp_path, monkeypatch)
    # page exists on disk but NO ingest log yet (log-less page)
    page = os.path.join(b, "backend", "auth.md")
    with open(page, "w", encoding="utf-8") as fh:
        fh.write(BASE_MD)
    log_file = base.log_path(b, "backend")
    assert not os.path.exists(log_file)

    src = os.path.join(proj, "src.txt")
    open(src, "w").write("v1")
    monkeypatch.setattr(
        indexer, "index_domain",
        lambda cfg, base, domain: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    out = server.wiki_update_page("backend", "auth", "Flow", "newbody", source=src)

    assert "error" in out
    assert open(page, encoding="utf-8").read() == BASE_MD          # file restored
    assert not os.path.exists(log_file)                            # log removed on rollback


def test_update_normalizes_wikilinks_in_edited_section(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch)
    _write(BASE_MD)
    server.wiki_update_page("backend", "concept/auth", "Flow", "see [[core|the core]] now")
    content = open(os.path.join(b, "backend", "concept", "auth.md"), encoding="utf-8").read()
    assert "[the core](core.md)" in content
    assert "[[core|the core]]" not in content


def test_update_sets_description_and_status(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch)
    server.wiki_write_page("backend", "alice", "# Alice\n\n## Role\nwork.\n",
                           source=None, type="person", description="old desc")
    res = server.wiki_update_page("backend", "person/alice", "Role", "new role prose.\n",
                                  description="new desc", status="deprecated")
    assert "error" not in res
    path = os.path.join(b, "backend", "person", "alice.md")
    content = open(path, encoding="utf-8").read()
    meta, _ = server._fm.split(content)
    assert meta["description"] == "new desc"
    assert meta["status"] == "deprecated"


def test_update_page_surfaces_safe_push_failure_metadata(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    _write(BASE_MD)
    monkeypatch.setattr(
        server.sync,
        "commit_and_push",
        lambda *args, **kwargs: {
            "committed": True,
            "pushed": False,
            "sync_attempts": 2,
            "push_attempts": 3,
            "failure_class": "push_rejected",
            "conflict": True,
            "hint": "run wiki_sync",
            "warning": "commit saved locally; push failed",
            "remote": "https://user:secret@example.test/wiki.git",
            "credential": "secret",
        },
    )

    out = server.wiki_update_page("backend", "concept/auth", "Flow", "new flow")

    assert out == {
        "page": "backend/concept/auth.md",
        "heading": "Flow",
        "indexed_chunks": out["indexed_chunks"],
        "reused": out["reused"],
        "embedded": out["embedded"],
        "bytes": out["bytes"],
        "over_cap": out["over_cap"],
        "committed": True,
        "pushed": False,
        "sync_attempts": 2,
        "push_attempts": 3,
        "failure_class": "push_rejected",
        "conflict": True,
        "hint": "run wiki_sync",
        "warning": "commit saved locally; push failed",
    }
