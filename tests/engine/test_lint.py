import hashlib
import json
import os
from iwiki_mcp.engine.lint import lint


def _wiki(tmp_path, pages: dict) -> str:
    wd = tmp_path / "wiki"
    wd.mkdir()
    for name, body in pages.items():
        (wd / name).write_text(body, encoding="utf-8")
    return str(wd)


def test_absent_wiki_is_noop(tmp_path):
    assert lint(str(tmp_path / "nope")) == {"wiki_present": False}


def test_detects_broken_ref(tmp_path):
    wd = _wiki(tmp_path, {"a.md": "## A\nlink to [[missing]] here\n"})
    out = lint(wd)
    assert any(b["ref"] == "missing" for b in out["broken"])


def test_code_fence_ref_not_broken(tmp_path):
    # page-level regression for P1: bash [[...]] in a fence is not a broken ref
    wd = _wiki(tmp_path, {
        "a.md": "## A\n```bash\nif [[ -d x ]]; then :; fi\n```\n[[b]]\n",
        "b.md": "## B\nbody\n",
    })
    assert lint(wd)["broken"] == []


def test_detects_orphan(tmp_path):
    wd = _wiki(tmp_path, {"a.md": "## A\nno links\n", "b.md": "## B\nno links\n"})
    out = lint(wd)
    assert set(out["orphans"]) == {
        os.path.normpath(os.path.join(wd, "a.md")),
        os.path.normpath(os.path.join(wd, "b.md")),
    }


def test_stale_ignores_legacy_and_malformed_log_records(tmp_path):
    wd = _wiki(tmp_path, {"a.md": "## A\nbody\n"})
    iwiki = os.path.join(wd, ".iwiki")
    os.makedirs(iwiki, exist_ok=True)
    with open(os.path.join(iwiki, "log.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"op": "init", "scope": "x", "note": "legacy"}) + "\n")
        fh.write("not json at all\n")
    out = lint(wd)
    assert out["wiki_present"] is True
    assert out["stale"] == []   # records lacking source/page are tolerated, ignored


def test_section_findings_folded_into_report(tmp_path):
    # page with a ### deep heading → deep_heading surfaces; missing_overview is gone
    wd = _wiki(tmp_path, {"a.md": "## A\nlead.\n\n### deep\nx\n"})
    out = lint(wd)
    types = {f["type"] for f in out["sections"]}
    assert "deep_heading" in types
    assert "missing_overview" not in types
    assert all("page" in f for f in out["sections"])


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _wiki_with_log(tmp_path, page_body, src_body, src_hash=None):
    """Wiki dir with one page, one source file, and a single ingest log record
    (absolute paths). Returns (wiki_dir, src_path, page_path)."""
    wd = tmp_path / "wiki"
    wd.mkdir()
    page = wd / "a.md"
    page.write_text(page_body, encoding="utf-8")
    src = tmp_path / "a.py"
    src.write_text(src_body, encoding="utf-8")
    iwiki = wd / ".iwiki"
    iwiki.mkdir()
    rec = {"op": "ingest", "source": str(src), "page": str(page)}
    if src_hash is not None:
        rec["src_hash"] = src_hash
    (iwiki / "log.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
    return str(wd), str(src), str(page)


def test_stale_hash_match_overrides_older_page_mtime(tmp_path):
    # The cure case: page mtime OLDER than source, but src_hash matches the
    # current source → NOT stale (kills git-reset / same-day false positives).
    wd, src, page = _wiki_with_log(
        tmp_path, "## A\nbody\n", "print('x')\n", src_hash=_h("print('x')\n"))
    os.utime(src, (2000, 2000))
    os.utime(page, (1000, 1000))
    assert lint(wd)["stale"] == []


def test_stale_hash_mismatch_is_stale_even_if_page_newer(tmp_path):
    # Hash recorded for OLD content; source now differs → stale regardless of mtime.
    wd, src, page = _wiki_with_log(
        tmp_path, "## A\nbody\n", "new content\n", src_hash=_h("old content\n"))
    os.utime(src, (1000, 1000))
    os.utime(page, (2000, 2000))
    assert any(s["source"] == src for s in lint(wd)["stale"])


def test_stale_resolves_domain_relative_logged_page(tmp_path):
    wd = _wiki(tmp_path, {"a.md": "## A\nbody\n"})
    src = tmp_path / "src.py"
    src.write_text("new content\n", encoding="utf-8")
    iwiki = os.path.join(wd, ".iwiki")
    os.makedirs(iwiki, exist_ok=True)
    rec = {
        "op": "ingest",
        "source": str(src),
        "page": "a.md",
        "src_hash": _h("old content\n"),
    }
    with open(os.path.join(iwiki, "log.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")

    assert lint(wd)["stale"] == [
        {"page": os.path.normpath(os.path.join(wd, "a.md")), "source": str(src)}
    ]


def test_stale_without_hash_uses_mtime(tmp_path):
    # No src_hash in the record → unchanged mtime behaviour.
    wd, src, page = _wiki_with_log(tmp_path, "## A\nbody\n", "x\n")
    os.utime(src, (2000, 2000))
    os.utime(page, (1000, 1000))
    assert any(s["source"] == src for s in lint(wd)["stale"])
    os.utime(page, (3000, 3000))
    assert lint(wd)["stale"] == []


def test_stale_hash_present_but_unreadable_falls_back_to_mtime(tmp_path, monkeypatch):
    # src_hash present but source unreadable (_src_hash → None) → mtime path.
    import iwiki_mcp.engine.lint as lintmod
    wd, src, page = _wiki_with_log(
        tmp_path, "## A\nbody\n", "x\n", src_hash="deadbeefdeadbeef")
    monkeypatch.setattr(lintmod, "_src_hash", lambda p: None)
    os.utime(src, (2000, 2000))
    os.utime(page, (1000, 1000))
    assert any(s["source"] == src for s in lint(wd)["stale"])


def test_stale_last_wins_after_delete_and_reingest(tmp_path):
    # ingest(old hash) -> delete -> ingest(new hash matching current source):
    # last-wins => judged by the NEWEST record => NOT stale.
    wd = _wiki(tmp_path, {"a.md": "## A\nbody\n"})
    src = tmp_path / "s.py"
    src.write_text("new\n", encoding="utf-8")
    iwiki = os.path.join(wd, ".iwiki")
    os.makedirs(iwiki, exist_ok=True)
    recs = [
        {"op": "ingest", "source": str(src), "page": "a.md", "src_hash": _h("old\n")},
        {"op": "delete", "source": "", "page": "a.md"},
        {"op": "ingest", "source": str(src), "page": "a.md", "src_hash": _h("new\n")},
    ]
    with open(os.path.join(iwiki, "log.jsonl"), "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")
    assert lint(wd)["stale"] == []


def test_missing_source_flags_absolute_gone(tmp_path):
    wd = _wiki(tmp_path, {"a.md": "## A\nbody\n"})
    gone = tmp_path / "gone.py"  # never created
    iwiki = os.path.join(wd, ".iwiki")
    os.makedirs(iwiki, exist_ok=True)
    rec = {"op": "ingest", "source": str(gone), "page": "a.md"}
    open(os.path.join(iwiki, "log.jsonl"), "w", encoding="utf-8").write(
        json.dumps(rec) + "\n")
    assert lint(wd)["missing_source"] == [
        {"page": os.path.normpath(os.path.join(wd, "a.md")), "source": str(gone)}
    ]


def test_missing_source_present_not_flagged(tmp_path):
    wd, src, page = _wiki_with_log(tmp_path, "## A\nb\n", "x\n")
    assert lint(wd)["missing_source"] == []


def test_missing_source_empty_source_skipped(tmp_path):
    wd = _wiki(tmp_path, {"a.md": "## A\nb\n"})
    iwiki = os.path.join(wd, ".iwiki")
    os.makedirs(iwiki, exist_ok=True)
    rec = {"op": "ingest", "source": "", "page": "a.md"}
    open(os.path.join(iwiki, "log.jsonl"), "w", encoding="utf-8").write(
        json.dumps(rec) + "\n")
    assert lint(wd)["missing_source"] == []


def test_missing_source_page_absent_skipped(tmp_path):
    wd = _wiki(tmp_path, {"keep.md": "## K\nx\n"})  # a.md never created
    gone = tmp_path / "gone.py"
    iwiki = os.path.join(wd, ".iwiki")
    os.makedirs(iwiki, exist_ok=True)
    rec = {"op": "ingest", "source": str(gone), "page": "a.md"}
    open(os.path.join(iwiki, "log.jsonl"), "w", encoding="utf-8").write(
        json.dumps(rec) + "\n")
    assert lint(wd)["missing_source"] == []


def test_missing_source_relative_found_under_project_dir(tmp_path):
    wd = _wiki(tmp_path, {"a.md": "## A\nb\n"})
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "src.py").write_text("x\n", encoding="utf-8")
    iwiki = os.path.join(wd, ".iwiki")
    os.makedirs(iwiki, exist_ok=True)
    rec = {"op": "ingest", "source": "src.py", "page": "a.md"}
    open(os.path.join(iwiki, "log.jsonl"), "w", encoding="utf-8").write(
        json.dumps(rec) + "\n")
    assert lint(wd, project_dir=str(proj))["missing_source"] == []


def test_missing_source_relative_absent_is_flagged(tmp_path, monkeypatch):
    wd = _wiki(tmp_path, {"a.md": "## A\nb\n"})
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)  # cwd fallback also lacks src.py
    iwiki = os.path.join(wd, ".iwiki")
    os.makedirs(iwiki, exist_ok=True)
    rec = {"op": "ingest", "source": "src.py", "page": "a.md"}
    open(os.path.join(iwiki, "log.jsonl"), "w", encoding="utf-8").write(
        json.dumps(rec) + "\n")
    assert lint(wd, project_dir=str(empty))["missing_source"] == [
        {"page": os.path.normpath(os.path.join(wd, "a.md")), "source": "src.py"}
    ]


def test_missing_source_last_wins_after_delete_and_reingest(tmp_path):
    wd = _wiki(tmp_path, {"a.md": "## A\nb\n"})
    newsrc = tmp_path / "new.py"
    newsrc.write_text("x\n", encoding="utf-8")
    oldsrc = tmp_path / "old.py"  # never created
    iwiki = os.path.join(wd, ".iwiki")
    os.makedirs(iwiki, exist_ok=True)
    recs = [
        {"op": "ingest", "source": str(oldsrc), "page": "a.md"},
        {"op": "delete", "source": "", "page": "a.md"},
        {"op": "ingest", "source": str(newsrc), "page": "a.md"},
    ]
    with open(os.path.join(iwiki, "log.jsonl"), "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")
    assert lint(wd)["missing_source"] == []


def test_broken_markdown_link_flagged(tmp_path):
    wd = _wiki(tmp_path, {"a.md": "## A\nlink [x](missing.md) here\n"})
    out = lint(wd)
    assert any(b["ref"] == "missing" for b in out["broken"])


def test_valid_markdown_anchor_matches_via_slug(tmp_path):
    wd = _wiki(tmp_path, {
        "a.md": "## A\nsee [B](b.md#the-section)\n",
        "b.md": "## The Section\nbody\n",
    })
    assert lint(wd)["broken"] == []


def test_broken_markdown_anchor_flagged(tmp_path):
    wd = _wiki(tmp_path, {
        "a.md": "## A\nsee [B](b.md#no-such)\n",
        "b.md": "## The Section\nbody\n",
    })
    assert any(b["ref"] == "b#no-such" for b in lint(wd)["broken"])


def test_legacy_wikilink_lists_only_unmigrated_pages(tmp_path):
    wd = _wiki(tmp_path, {
        "a.md": "## A\nold [[b]] link\n",
        "b.md": "## B\nnew [x](a.md) link\n",
    })
    assert lint(wd)["legacy_wikilink"] == [
        os.path.normpath(os.path.join(wd, "a.md"))
    ]
