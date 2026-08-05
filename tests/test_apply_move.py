import json
import os
import subprocess
from hashlib import sha256

from iwiki_mcp import base, indexer, okf, server
from iwiki_mcp.engine.lint import lint
from iwiki_mcp.engine.graph_store import GraphStore


def _bind(tmp_path, monkeypatch, dom):
    os.makedirs(tmp_path / dom, exist_ok=True)
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", "2")
    monkeypatch.setattr(
        base, "resolve_binding",
        lambda: base.Binding(base=str(tmp_path), read=(dom,), write=dom,
                             project_dir=str(tmp_path)),
    )
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, t: [[1.0, 0.0] for _ in t])


def _git(cwd, *args):
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    )


def _init_git_base(base_dir, domain):
    _git(base_dir, "init", "-q")
    _git(base_dir, "config", "user.email", "t@t")
    _git(base_dir, "config", "user.name", "t")
    (base_dir / domain / "seed.md").write_text(
        "# Seed\n\n## Body\nseed\n", encoding="utf-8"
    )
    _git(base_dir, "add", "-A")
    _git(base_dir, "commit", "-q", "-m", "seed")


def test_apply_moves_page_on_type_change(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch, "d")
    server.wiki_write_page("d", "x", "# X\n\n## Purpose\n\nBody.\n", type="concept")
    # a sibling links to it
    server.wiki_write_page("d", "y", "# Y\n\n## Purpose\n\nSee [X](concept/x.md).\n", type="guide")
    res = server.wiki_apply_okf("d", "concept/x", type="architecture")
    assert res["page"] == "d/architecture/x.md"
    assert (tmp_path / "d" / "architecture" / "x.md").is_file()
    assert not (tmp_path / "d" / "concept" / "x.md").exists()
    y = (tmp_path / "d" / "guide" / "y.md").read_text()
    assert "(architecture/x.md)" in y      # inbound link rewritten


def test_apply_move_refreshes_target_and_rewritten_link_graph_pages(
    tmp_path, monkeypatch
):
    _bind(tmp_path, monkeypatch, "d")
    _init_git_base(tmp_path, "d")
    server.wiki_write_page(
        "d", "x", "# X\n\n## Purpose\nBody.\n", type="concept"
    )
    server.wiki_write_page(
        "d", "y", "# Y\n\n## Purpose\nSee [X](concept/x.md).\n", type="guide"
    )

    result = server.wiki_apply_okf("d", "concept/x", type="architecture")

    assert result["page"] == "d/architecture/x.md"
    snapshot = GraphStore(tmp_path).load_ready_domain("d")
    assert "concept/x.md" not in {page.file for page in snapshot.pages}
    assert "architecture/x.md" in {page.file for page in snapshot.pages}
    assert any(
        edge.source_page_id == "d/guide/y"
        and edge.target_page_id == "d/architecture/x"
        for edge in snapshot.edges
    )


def test_apply_is_noop_move_when_type_unchanged(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch, "d")
    server.wiki_write_page("d", "x", "# X\n\n## Purpose\n\nBody.\n", type="concept")
    res = server.wiki_apply_okf("d", "concept/x", type="concept")
    assert res["page"] == "d/concept/x.md"
    assert (tmp_path / "d" / "concept" / "x.md").is_file()


def test_apply_refuses_to_clobber_colliding_target(tmp_path, monkeypatch):
    # concept/x and architecture/x are distinct pages under the identity model
    # (same tail, different type). Retyping concept/x to "architecture" resolves
    # to a target that already exists -- must refuse, not silently os.replace it.
    _bind(tmp_path, monkeypatch, "d")
    server.wiki_write_page("d", "x", "# X\n\n## Purpose\n\nConcept body.\n", type="concept")
    server.wiki_write_page("d", "x", "# X\n\n## Purpose\n\nArchitecture body.\n",
                           type="architecture")
    res = server.wiki_apply_okf("d", "concept/x", type="architecture")
    assert res == {
        "error": "page 'd/architecture/x' exists",
        "hint": "delete or rename the colliding page first",
    }
    concept_p = tmp_path / "d" / "concept" / "x.md"
    arch_p = tmp_path / "d" / "architecture" / "x.md"
    assert concept_p.is_file() and arch_p.is_file()
    assert "Concept body." in concept_p.read_text()
    assert "Architecture body." in arch_p.read_text()


def test_apply_okf_not_found_error_precedes_config_halt(tmp_path, monkeypatch):
    # Regression: Config.load() used to run before the not-found guard, so an
    # unset LLM config + a missing slug returned a misleading "HALT:" config
    # error instead of the friendly "page not found".
    os.makedirs(tmp_path / "d", exist_ok=True)
    monkeypatch.delenv("IWIKI_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("IWIKI_LLM_KEY", raising=False)
    monkeypatch.setattr(
        base, "resolve_binding",
        lambda: base.Binding(base=str(tmp_path), read=("d",), write="d",
                             project_dir=str(tmp_path)),
    )
    res = server.wiki_apply_okf("d", "missing", type="concept")
    assert res == {
        "error": "page 'd/missing' not found",
        "hint": "list pages with wiki_list_pages",
    }


def test_move_page_rekeys_ingest_log(tmp_path, monkeypatch):
    # CORRECTNESS (holistic review finding 3): move_page used to rename the
    # file + rewrite links but never re-key log.jsonl, so the ingest record
    # stayed under the pre-move page name -- lint's stale/missing_source
    # checks (keyed off the log) silently stopped finding the page post-move.
    _bind(tmp_path, monkeypatch, "d")
    dom = tmp_path / "d"
    (dom / "a.md").write_text("# A\n\n## Overview\ns\n\n## B\nwords\n", encoding="utf-8")
    (dom / "log.jsonl").write_text(json.dumps({
        "op": "ingest", "source": "/src/a.py", "page": "a.md",
        "date": "2020-01-01", "src_hash": "abc",
    }) + "\n", encoding="utf-8")

    change = okf.move_page(str(tmp_path), "d", "a", "guide/a")

    recs = [json.loads(ln) for ln in (dom / "log.jsonl").read_text().splitlines() if ln.strip()]
    assert any(r["page"] == "guide/a.md" for r in recs)
    assert not any(r["page"] == "a.md" for r in recs)
    assert change.refresh_files == ("guide/a.md",)
    assert change.delete_files == ("a.md",)


def test_move_page_reports_every_rewritten_link_source(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch, "d")
    dom = tmp_path / "d"
    (dom / "a.md").write_text("# A\n\n## Body\ntext\n", encoding="utf-8")
    (dom / "b.md").write_text(
        "# B\n\n## Link\n[A](a.md)\n", encoding="utf-8"
    )

    change = okf.move_page(str(tmp_path), "d", "a", "guide/a")

    assert change.refresh_files == ("b.md", "guide/a.md")
    assert change.delete_files == ("a.md",)


def test_prepare_page_move_is_pure_and_includes_log_rekey(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch, "d")
    dom = tmp_path / "d"
    (dom / "a.md").write_text("# A\n\n## Body\ntext\n", encoding="utf-8")
    (dom / "b.md").write_text(
        "# B\n\n## Link\n[A](a.md#Body)\n", encoding="utf-8"
    )
    (dom / "log.jsonl").write_text(
        json.dumps({"op": "ingest", "page": "a.md", "source": "/src/a.py"})
        + "\n",
        encoding="utf-8",
    )
    before = {
        path.relative_to(dom).as_posix(): path.read_bytes()
        for path in dom.rglob("*")
        if path.is_file()
    }

    prepared = okf.prepare_page_move(str(tmp_path), "d", "a", "guide/a")

    after = {
        path.relative_to(dom).as_posix(): path.read_bytes()
        for path in dom.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert prepared.old_identity == "a"
    assert prepared.new_identity == "guide/a"
    assert prepared.refresh_files == ("b.md", "guide/a.md")
    assert prepared.delete_files == ("a.md",)
    edits = {(edit.domain, edit.file): edit for edit in prepared.edits}
    assert edits[("d", "a.md")].before_hash == sha256(before["a.md"]).hexdigest()
    assert edits[("d", "a.md")].after is None
    assert edits[("d", "guide/a.md")].before_hash is None
    assert edits[("d", "guide/a.md")].after == before["a.md"]
    assert b"guide/a.md#Body" in edits[("d", "b.md")].after
    assert b'"page": "guide/a.md"' in edits[("d", "log.jsonl")].after


def test_apply_okf_move_rekeys_log_and_lint_still_flags_stale(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch, "d")
    dom = tmp_path / "d"
    (dom / "a.md").write_text("# A\n\n## Overview\ns\n\n## B\nwords\n", encoding="utf-8")
    src = tmp_path / "src.py"
    src.write_text("v1", encoding="utf-8")
    (dom / "log.jsonl").write_text(json.dumps({
        "op": "ingest", "source": str(src), "page": "a.md",
        "date": "2020-01-01", "src_hash": None,
    }) + "\n", encoding="utf-8")

    server.wiki_apply_okf("d", "a", type="guide")

    recs = [json.loads(ln) for ln in (dom / "log.jsonl").read_text().splitlines() if ln.strip()]
    assert any(r["page"] == "guide/a.md" for r in recs)
    assert not any(r["page"] == "a.md" for r in recs)

    src.write_text("v2 -- source drifted after ingest", encoding="utf-8")
    report = lint(str(dom))
    stale_pages = [os.path.relpath(s["page"], str(dom)) for s in report.get("stale", [])]
    assert "guide/a.md" in stale_pages
