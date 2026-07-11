import json
import os

from iwiki_mcp import base, indexer, okf, server
from iwiki_mcp.engine.lint import lint


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

    okf.move_page(str(tmp_path), "d", "a", "guide/a")

    recs = [json.loads(ln) for ln in (dom / "log.jsonl").read_text().splitlines() if ln.strip()]
    assert any(r["page"] == "guide/a.md" for r in recs)
    assert not any(r["page"] == "a.md" for r in recs)


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
