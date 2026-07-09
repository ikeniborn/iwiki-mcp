from iwiki_mcp.export import convert_wikilinks, export_domain


def test_convert_wikilinks_forms():
    assert convert_wikilinks("see [[base#Purpose]]") == "see [Purpose](base.md)"
    assert convert_wikilinks("see [[base|the base]]") == "see [the base](base.md)"
    assert convert_wikilinks("see [[base]]") == "see [base](base.md)"
    assert convert_wikilinks("sub [[a/b#H]]") == "sub [H](a/b.md)"


def test_convert_wikilinks_skips_code():
    assert convert_wikilinks("`[[ -f x ]]`") == "`[[ -f x ]]`"
    fenced = "```bash\nif [[ $# -gt 0 ]]; then :; fi\n```"
    assert convert_wikilinks(fenced) == fenced


def test_export_writes_bundle(tmp_path):
    dom = tmp_path / "d"
    (dom / ".iwiki").mkdir(parents=True)
    (dom / ".iwiki" / "log.jsonl").write_text(
        '{"op":"ingest","page":"a.md","source":"a.py","date":"2026-07-01"}\n', encoding="utf-8")
    (dom / "a.md").write_text(
        "---\ntype: api\n---\n# A\n\n## Overview\ns\n\n## B\nsee [[a#B]]\n", encoding="utf-8")
    dest = tmp_path / "out"
    res = export_domain(str(dom), str(dest))
    exported = (dest / "a.md").read_text(encoding="utf-8")
    assert "[B](a.md)" in exported          # wikilink converted
    assert "type: api" in exported          # frontmatter preserved
    assert (dest / "index.md").exists()
    assert (dest / "log.md").exists()
    assert res["pages"] == 1
