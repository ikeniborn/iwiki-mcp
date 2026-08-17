import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

from iwiki_mcp import base, indexer, server
from iwiki_mcp.engine.graph_store import GraphStore
from iwiki_mcp.graph import markdown_fingerprint


@pytest.mark.parametrize(
    ("handler", "args"),
    [
        ("wiki_write_page", ("alpha", "page", "# Page\n")),
        ("wiki_update_page", ("alpha", "page", "Overview", "body")),
        ("wiki_delete_page", ("alpha", "page")),
        ("wiki_index", ("alpha",)),
        ("wiki_create_domain", ("new-domain",)),
        ("wiki_migrate_okf", ("alpha",)),
        ("wiki_apply_okf", ("alpha", "page", "concept")),
        ("wiki_export_okf", ("alpha",)),
        ("wiki_sync", ()),
    ],
)
def test_mutation_guard_blocks_every_handler_before_side_effects(
    tmp_path, monkeypatch, handler, args
):
    from iwiki_mcp import cross_domain
    from iwiki_mcp.base import Binding

    binding = Binding(
        str(tmp_path), ("alpha",), "alpha", str(tmp_path), ("alpha",)
    )
    monkeypatch.setattr(server.base, "resolve_binding", lambda: binding)

    def stop(*_args, **_kwargs):
        raise cross_domain.CrossDomainError("manual_recovery_required")

    monkeypatch.setattr(cross_domain, "recover_pending_transactions", stop)
    monkeypatch.setattr(
        server.sync,
        "ensure_fresh",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("freshness ran before recovery")
        ),
    )

    result = getattr(server, handler)(*args)

    assert result["code"] == "manual_recovery_required"


def _seed(tmp_path, monkeypatch, with_domain=True):
    b = tmp_path / "wiki"
    b.mkdir()
    if with_domain:
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


def _git(cwd, *args):
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    )


def _init_git_base(base_dir):
    _git(base_dir, "init", "-q")
    _git(base_dir, "config", "user.email", "t@t")
    _git(base_dir, "config", "user.name", "t")
    seed = base_dir / "backend" / "seed.md"
    seed.write_text("# Seed\n\n## Notes\nseed\n", encoding="utf-8")
    _git(base_dir, "add", "-A")
    _git(base_dir, "commit", "-q", "-m", "seed")


def test_write_page_indexes_and_logs(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch)
    md = "# Auth\n## Overview\nsummary\n## Flow\nlogin then token\n"
    out = server.wiki_write_page("backend", "auth", md)
    assert out["page"] == "backend/concept/auth.md"    # no type/chat model -> default "concept"
    assert out["indexed_chunks"] >= 1
    assert os.path.isfile(os.path.join(b, "backend", "concept", "auth.md"))
    assert os.path.isfile(os.path.join(b, "backend", "log.jsonl"))


def test_write_page_refreshes_complete_graph_and_finalizes_commit_fingerprint(
    tmp_path, monkeypatch
):
    b, _ = _seed(tmp_path, monkeypatch)
    _init_git_base(tmp_path / "wiki")

    out = server.wiki_write_page(
        "backend", "auth", "# Auth\n\n## Flow\nSee [Seed](../../seed.md).\n"
    )

    assert out["page"] == "backend/concept/auth.md"
    snapshot = GraphStore(b).load_ready_domain("backend")
    assert {page.file for page in snapshot.pages} == {
        "seed.md", "concept/auth.md",
    }
    with GraphStore(b).read_snapshot() as connection:
        row = connection.execute(
            "SELECT indexed_commit, markdown_fingerprint, state "
            "FROM domains WHERE domain = 'backend'"
        ).fetchone()
    expected = markdown_fingerprint(b, "backend")
    assert tuple(row) == (expected.indexed_commit, expected.value, "ready")


def test_write_graph_failure_keeps_canonical_commit_and_recovers_on_next_use(
    tmp_path, monkeypatch
):
    import iwiki_mcp.graph as graph
    from iwiki_mcp.engine import graph_store as graph_store_module

    b, _ = _seed(tmp_path, monkeypatch)
    _init_git_base(tmp_path / "wiki")
    current_store = graph_store_module.GraphStore
    real_refresh = current_store.refresh_pages

    def fail_refresh(self, domain, pages, *, delete_files=(), **kwargs):
        raise RuntimeError(f"failed at {tmp_path}/private/page.md")

    monkeypatch.setattr(current_store, "refresh_pages", fail_refresh)

    out = server.wiki_write_page(
        "backend", "auth", "# Auth\n\n## Flow\nupdated\n"
    )

    assert out["page"] == "backend/concept/auth.md"
    assert out["committed"] is True
    assert indexer.GRAPH_FALLBACK_WARNING in out["warning"]
    assert str(tmp_path) not in repr(out)
    with current_store(b).read_snapshot() as connection:
        state = connection.execute(
            "SELECT state FROM domains WHERE domain = 'backend'"
        ).fetchone()[0]
    assert state == "dirty"

    monkeypatch.setattr(current_store, "refresh_pages", real_refresh)
    provider = graph.scoped_graph(b, ("backend",))
    assert provider is not None
    assert "concept/auth.md" in {
        page.file for page in current_store(b).load_ready_domain("backend").pages
    }


def test_write_rejects_deep_heading(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_write_page("backend", "bad", "# T\n### Too Deep\nx\n")
    assert "error" in out


def test_write_refuses_overwrite_without_force(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    md = "# Auth\n## Overview\no\n## Flow\nx\n"
    server.wiki_write_page("backend", "auth", md)
    out = server.wiki_write_page("backend", "auth", md)
    assert "error" in out and "exists" in out["error"]


def test_create_domain(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch)
    out = server.wiki_create_domain("new-domain")
    assert out["created"] == "new-domain"
    assert os.path.isdir(os.path.join(b, "new-domain"))


def test_bind_returns_controlled_error_and_preserves_nonempty_config(
    tmp_path, monkeypatch
):
    b, proj = _seed(tmp_path, monkeypatch)
    os.makedirs(os.path.join(b, "proj"))
    config_path = os.path.join(proj, ".iwiki.toml")
    before = open(config_path, "rb").read()

    out = server.wiki_bind(
        read=["backend", "proj"], write=["proj"], primary="proj"
    )

    assert out["code"] == "project_config_manual_edit_required"
    assert "edit .iwiki.toml manually" in out["hint"]
    assert "existing file was not changed" not in out["hint"]
    assert open(config_path, "rb").read() == before


def test_bind_initializes_empty_config_then_returns_controlled_error(
    tmp_path, monkeypatch
):
    b, proj = _seed(tmp_path, monkeypatch)
    os.makedirs(os.path.join(b, "proj"))
    config_path = os.path.join(proj, ".iwiki.toml")
    open(config_path, "w", encoding="utf-8").write(" \n\t")

    out = server.wiki_bind(
        read=["backend", "proj"], write=["proj"], primary="proj"
    )

    assert out["code"] == "project_config_manual_edit_required"
    text = open(config_path, encoding="utf-8").read()
    assert "Git storage" in text
    assert "PostgreSQL storage" in text
    assert "[code_graph]" in text


def test_bind_initializes_missing_config_then_returns_controlled_error(
    tmp_path, monkeypatch
):
    b, proj = _seed(tmp_path, monkeypatch)
    os.makedirs(os.path.join(b, "proj"))
    config_path = os.path.join(proj, ".iwiki.toml")
    os.unlink(config_path)

    out = server.wiki_bind(
        read=["backend", "proj"], write=["proj"], primary="proj"
    )

    assert out["code"] == "project_config_manual_edit_required"
    assert os.path.isfile(config_path)
    assert "max_rebuild_seconds" in open(config_path, encoding="utf-8").read()


def test_write_rejects_existing_domain_outside_scope_before_freshness(
    tmp_path, monkeypatch
):
    b, proj = _seed(tmp_path, monkeypatch)
    os.makedirs(os.path.join(b, "other"))
    open(os.path.join(proj, ".iwiki.toml"), "w", encoding="utf-8").write(
        'read = ["backend", "other"]\nwrite = ["backend"]\nprimary = "backend"\n'
    )
    monkeypatch.setattr(
        server.sync,
        "ensure_fresh",
        lambda *_: (_ for _ in ()).throw(AssertionError("freshness called")),
    )

    out = server.wiki_write_page("other", "page", "# Page\n\n## Body\ntext\n")

    assert "outside bound write scope" in out["error"]
    assert list((tmp_path / "wiki" / "other").iterdir()) == []


def test_write_page_removes_new_file_when_indexing_fails(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        indexer,
        "index_domain",
        lambda cfg, base, domain: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    out = server.wiki_write_page(
        "backend",
        "auth",
        "# Auth\n## Overview\nsummary\n## Flow\nlogin then token\n",
    )

    log_path = base.log_path(b, "backend")
    log_text = (
        open(log_path, encoding="utf-8").read() if os.path.exists(log_path) else ""
    )
    assert "error" in out
    assert not os.path.exists(os.path.join(b, "backend", "concept", "auth.md"))
    assert "auth.md" not in log_text


def test_write_page_does_not_leave_index_record_when_logging_fails(
    tmp_path, monkeypatch
):
    b, _ = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        indexer,
        "append_log",
        lambda base, domain, op, source, page, src_hash: (_ for _ in ()).throw(
            RuntimeError("log failed")
        ),
    )

    out = server.wiki_write_page(
        "backend",
        "auth",
        "# Auth\n## Overview\nsummary\n## Flow\nlogin then token\n",
    )

    index_path = base.index_path(b, "backend")
    index_text = (
        open(index_path, encoding="utf-8").read()
        if os.path.exists(index_path)
        else ""
    )
    assert "error" in out
    assert not os.path.exists(os.path.join(b, "backend", "concept", "auth.md"))
    assert "auth.md" not in index_text


def test_index_commits_and_reports_push(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch)
    server.wiki_write_page("backend", "auth", "# A\n## Overview\no\n## Flow\nx\n")
    out = server.wiki_index("backend")
    assert out["domain"] == "backend"
    assert "committed" in out and "pushed" in out


def test_index_rebuilds_whole_domain_graph(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch)
    _init_git_base(tmp_path / "wiki")
    (tmp_path / "wiki" / "backend" / "other.md").write_text(
        "# Other\n\n## Links\n[Seed](seed.md)\n", encoding="utf-8"
    )

    out = server.wiki_index("backend")

    assert out["domain"] == "backend"
    snapshot = GraphStore(b).load_ready_domain("backend")
    assert {page.file for page in snapshot.pages} == {"seed.md", "other.md"}


def test_index_rebuilds_missing_graph_when_nothing_needs_git_commit(
    tmp_path, monkeypatch
):
    b, _ = _seed(tmp_path, monkeypatch)
    _init_git_base(tmp_path / "wiki")
    server.wiki_index("backend")
    graph_path = GraphStore(b).path
    graph_path.unlink()

    out = server.wiki_index("backend")

    assert out["committed"] is False
    assert "nothing to commit" in out["warning"]
    assert {page.file for page in GraphStore(b).load_ready_domain("backend").pages} == {
        "seed.md"
    }


def test_push_failure_keeps_graph_aligned_with_local_commit(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch)
    _init_git_base(tmp_path / "wiki")
    monkeypatch.setattr(
        server.sync,
        "sync",
        lambda base: {
            "pulled": True,
            "pushed": False,
            "warning": "push rejected",
            "sync_attempts": 1,
            "push_attempts": 1,
        },
    )

    out = server.wiki_write_page(
        "backend", "auth", "# Auth\n\n## Flow\nbody\n"
    )

    assert out["committed"] is True
    assert out["pushed"] is False
    assert "push rejected" in out["warning"]
    expected = markdown_fingerprint(b, "backend")
    with GraphStore(b).read_snapshot() as connection:
        row = connection.execute(
            "SELECT indexed_commit, markdown_fingerprint, state "
            "FROM domains WHERE domain = 'backend'"
        ).fetchone()
    assert tuple(row) == (expected.indexed_commit, expected.value, "ready")


def test_write_normalizes_wikilinks_to_markdown(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch)
    md = "# Auth\n## Overview\nsummary\n## Flow\nsee [[core#Token Store]] here\n"
    server.wiki_write_page("backend", "auth", md)
    content = open(os.path.join(b, "backend", "concept", "auth.md"), encoding="utf-8").read()
    assert "[Token Store](core.md#token-store)" in content
    assert "[[core#Token Store]]" not in content


def test_normalize_source(tmp_path):
    proj = str(tmp_path)
    assert server._normalize_source(proj, "src/x.py") == "src/x.py"
    inside = str(tmp_path / "src" / "x.py")
    assert server._normalize_source(proj, inside) == "src/x.py"
    import pytest
    with pytest.raises(ValueError):
        server._normalize_source(proj, "/etc/passwd")


def test_normalize_source_rejects_relative_escape(tmp_path):
    # SECURITY (holistic review finding 2): a RELATIVE source containing '..'
    # used to pass through unchanged, escaping the project (and dodging the
    # anchored .iwikiignore patterns, which resolve relative to project_dir).
    proj = str(tmp_path / "proj")
    os.makedirs(proj)
    import pytest
    with pytest.raises(ValueError):
        server._normalize_source(proj, "../../../../etc/hosts")


def test_write_page_rejects_relative_source_escape(tmp_path, monkeypatch):
    b, proj = _seed(tmp_path, monkeypatch)
    md = "# Auth\n## Overview\no\n## Flow\nx\n"
    out = server.wiki_write_page("backend", "auth", md, source="../../../../etc/hosts")
    assert "error" in out
    assert "outside project" in out["error"]
    assert not os.path.isfile(os.path.join(b, "backend", "concept", "auth.md"))


def test_write_page_surfaces_safe_push_failure_metadata(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        server.sync,
        "commit_and_push",
        lambda *args, **kwargs: {
            "committed": True,
            "pushed": False,
            "sync_attempts": 2,
            "push_attempts": 3,
            "failure_class": "push_rejected",
            "conflict": False,
            "hint": "run wiki_sync",
            "warning": "commit saved locally; push failed",
            "remote": "https://user:secret@example.test/wiki.git",
            "credential": "secret",
        },
    )

    out = server.wiki_write_page(
        "backend",
        "auth",
        "# Auth\n## Overview\nsummary\n## Flow\nlogin then token\n",
    )

    assert out == {
        "page": "backend/concept/auth.md",
        "indexed_chunks": out["indexed_chunks"],
        "bytes": out["bytes"],
        "over_cap": out["over_cap"],
        "committed": True,
        "pushed": False,
        "sync_attempts": 2,
        "push_attempts": 3,
        "failure_class": "push_rejected",
        "conflict": False,
        "hint": "run wiki_sync",
        "warning": (
            "commit saved locally; push failed; "
            "type not given and IWIKI_CHAT_MODEL unset; defaulted to concept"
        ),
    }


def test_read_page_with_heading_returns_only_that_section(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nflow body\n## Notes\nkeep\n",
    )
    out = server.wiki_read_page("backend", "concept/auth", heading="Flow")
    assert out["heading"] == "Flow"
    assert out["body"].strip() == "flow body"
    assert "section_hash" in out
    assert "markdown" not in out


def test_read_page_with_missing_heading_returns_error(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n",
    )
    out = server.wiki_read_page("backend", "concept/auth", heading="Nope")
    assert "error" in out
    assert "not found" in out["error"]


def test_read_page_without_heading_is_unchanged(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n",
    )
    out = server.wiki_read_page("backend", "concept/auth")
    assert set(out) == {"domain", "slug", "markdown"}


def test_insert_section_adds_new_section_after_target(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nflow body\n",
    )
    out = server.wiki_insert_section(
        "backend", "concept/auth", "New", "new body", after_heading="Flow"
    )
    assert "error" not in out
    read = server.wiki_read_page("backend", "concept/auth")
    assert "## New\n\nnew body" in read["markdown"]
    assert read["markdown"].index("## Flow") < read["markdown"].index("## New")


def test_insert_section_missing_page_returns_error(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_insert_section("backend", "nope", "New", "body")
    assert "not found" in out["error"]


def test_insert_section_rejects_invalid_body_structure(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n",
    )
    out = server.wiki_insert_section(
        "backend", "concept/auth", "New", "### too deep\nx"
    )
    assert "error" in out


def test_insert_section_missing_anchor_heading_returns_error(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n",
    )
    out = server.wiki_insert_section(
        "backend", "concept/auth", "New", "body", after_heading="NoSuchSection"
    )
    assert "error" in out
    assert "not found" in out["error"]


def test_insert_section_rejects_both_after_and_before(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nflow body\n",
    )
    out = server.wiki_insert_section(
        "backend", "concept/auth", "New", "body",
        after_heading="Flow", before_heading="Overview",
    )
    assert "error" in out


def test_insert_section_rejects_anchor_collision(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nflow body\n",
    )
    out = server.wiki_insert_section("backend", "concept/auth", "Flow", "body")
    assert "error" in out
    assert "collides" in out["error"]


def test_delete_section_removes_target_section(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nflow body\n## Notes\nkeep\n",
    )
    out = server.wiki_delete_section("backend", "concept/auth", "Flow")
    assert "error" not in out
    read = server.wiki_read_page("backend", "concept/auth")
    assert "## Flow" not in read["markdown"]
    assert "## Notes" in read["markdown"]


def test_delete_section_rejects_last_section(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n",
    )
    out = server.wiki_delete_section("backend", "concept/auth", "Overview")
    assert "error" in out


def test_delete_section_missing_page_returns_error(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_delete_section("backend", "concept/missing", "Flow")
    assert "error" in out
    assert "not found" in out["error"]


def test_delete_section_missing_heading_returns_error(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nflow body\n",
    )
    out = server.wiki_delete_section("backend", "concept/auth", "Nope")
    assert "error" in out


def test_move_section_reorders_target(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nflow body\n## Notes\nkeep\n",
    )
    out = server.wiki_move_section("backend", "concept/auth", "Notes", before_heading="Overview")
    assert "error" not in out
    read = server.wiki_read_page("backend", "concept/auth")
    assert read["markdown"].index("## Notes") < read["markdown"].index("## Overview")


def test_move_section_rejects_self_reference(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nflow body\n",
    )
    out = server.wiki_move_section("backend", "concept/auth", "Flow", after_heading="Flow")
    assert "error" in out


def test_move_section_missing_page_returns_error(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_move_section("backend", "concept/missing", "Flow", after_heading="Overview")
    assert "error" in out
    assert "not found" in out["error"]


def test_move_section_missing_heading_returns_error(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nflow body\n",
    )
    out = server.wiki_move_section("backend", "concept/auth", "Nope", after_heading="Overview")
    assert "error" in out


def test_move_section_rejects_both_after_and_before(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nflow body\n## Notes\nkeep\n",
    )
    out = server.wiki_move_section(
        "backend", "concept/auth", "Notes", after_heading="Overview", before_heading="Flow",
    )
    assert "error" in out


def test_delete_section_hash_mismatch_returns_conflict(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nflow body\n## Notes\nkeep\n",
    )
    out = server.wiki_delete_section(
        "backend", "concept/auth", "Flow", expected_section_hash="0000000000000000",
    )
    assert out["error"] == "section_conflict"
    assert "current_section_hash" in out
    read = server.wiki_read_page("backend", "concept/auth")
    assert "## Flow" in read["markdown"]


def test_delete_section_hash_match_succeeds(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nflow body\n## Notes\nkeep\n",
    )
    current = server.wiki_read_page("backend", "concept/auth", heading="Flow")
    out = server.wiki_delete_section(
        "backend", "concept/auth", "Flow", expected_section_hash=current["section_hash"],
    )
    assert "error" not in out
    read = server.wiki_read_page("backend", "concept/auth")
    assert "## Flow" not in read["markdown"]


def test_delete_section_hash_omitted_behaves_as_before(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nflow body\n## Notes\nkeep\n",
    )
    out = server.wiki_delete_section("backend", "concept/auth", "Flow")
    assert "error" not in out
    read = server.wiki_read_page("backend", "concept/auth")
    assert "## Flow" not in read["markdown"]


def test_move_section_hash_mismatch_returns_conflict(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nflow body\n## Notes\nkeep\n",
    )
    out = server.wiki_move_section(
        "backend", "concept/auth", "Notes", before_heading="Overview",
        expected_section_hash="0000000000000000",
    )
    assert out["error"] == "section_conflict"
    assert "current_section_hash" in out
    read = server.wiki_read_page("backend", "concept/auth")
    assert read["markdown"].index("## Overview") < read["markdown"].index("## Notes")


def test_move_section_hash_match_succeeds(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nflow body\n## Notes\nkeep\n",
    )
    current = server.wiki_read_page("backend", "concept/auth", heading="Notes")
    out = server.wiki_move_section(
        "backend", "concept/auth", "Notes", before_heading="Overview",
        expected_section_hash=current["section_hash"],
    )
    assert "error" not in out
    read = server.wiki_read_page("backend", "concept/auth")
    assert read["markdown"].index("## Notes") < read["markdown"].index("## Overview")


def test_move_section_hash_omitted_behaves_as_before(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nflow body\n## Notes\nkeep\n",
    )
    out = server.wiki_move_section("backend", "concept/auth", "Notes", before_heading="Overview")
    assert "error" not in out
    read = server.wiki_read_page("backend", "concept/auth")
    assert read["markdown"].index("## Notes") < read["markdown"].index("## Overview")


def test_concurrent_updates_to_different_sections_both_succeed(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nflow body\n",
    )
    overview = server.wiki_read_page("backend", "concept/auth", heading="Overview")
    flow = server.wiki_read_page("backend", "concept/auth", heading="Flow")

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(
            server.wiki_update_page, "backend", "concept/auth", "Overview", "new sum",
            expected_section_hash=overview["section_hash"],
        )
        f2 = pool.submit(
            server.wiki_update_page, "backend", "concept/auth", "Flow", "new flow",
            expected_section_hash=flow["section_hash"],
        )
        r1, r2 = f1.result(), f2.result()

    assert "error" not in r1
    assert "error" not in r2
    final = server.wiki_read_page("backend", "concept/auth")
    assert "new sum" in final["markdown"]
    assert "new flow" in final["markdown"]


def test_concurrent_updates_to_same_section_second_conflicts(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nflow body\n",
    )
    flow = server.wiki_read_page("backend", "concept/auth", heading="Flow")
    server.wiki_update_page(
        "backend", "concept/auth", "Flow", "first write",
        expected_section_hash=flow["section_hash"],
    )
    out = server.wiki_update_page(
        "backend", "concept/auth", "Flow", "second write",
        expected_section_hash=flow["section_hash"],  # stale, already applied above
    )
    assert out["error"] == "section_conflict"
