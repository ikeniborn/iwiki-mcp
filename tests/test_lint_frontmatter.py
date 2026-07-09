from iwiki_mcp.engine.lint import lint


def _wiki(tmp_path, pages):
    d = tmp_path / "d"
    (d / ".iwiki").mkdir(parents=True)
    for slug, text in pages.items():
        (d / f"{slug}.md").write_text(text, encoding="utf-8")
    return str(d)


def test_missing_frontmatter_reported(tmp_path):
    wiki = _wiki(tmp_path, {"a": "# A\n\n## Overview\ns\n\n## B\nx\n"})
    rep = lint(wiki)
    assert any("a.md" in p for p in rep["missing_frontmatter"])


def test_tag_drift_flags_near_duplicates(tmp_path):
    wiki = _wiki(tmp_path, {
        "a": "---\ntype: api\ntags: [config]\n---\n# A\n\n## Overview\ns\n\n## B\nx\n",
        "b": "---\ntype: api\ntags: [configs]\n---\n# B\n\n## Overview\ns\n\n## C\ny\n",
    })
    rep = lint(wiki)
    pairs = {tuple(sorted(d["tags"])) for d in rep["tag_drift"]}
    assert ("config", "configs") in pairs


def test_no_drift_for_distinct_tags(tmp_path):
    wiki = _wiki(tmp_path, {
        "a": "---\ntype: api\ntags: [config]\n---\n# A\n\n## Overview\ns\n\n## B\nx\n",
        "b": "---\ntype: api\ntags: [binding]\n---\n# B\n\n## Overview\ns\n\n## C\ny\n",
    })
    assert lint(wiki)["tag_drift"] == []


def test_lint_sections_surface_frontmatter_advisories(tmp_path):
    wiki = _wiki(tmp_path, {
        "a": "---\ntype: bogus\n---\n# A\n\n## Overview\ns\n\n## B\nx\n",
    })
    rep = lint(wiki)
    kinds = {s["type"] for s in rep["sections"] if "a.md" in s["page"]}
    assert "unknown_type" in kinds
