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
    assert res["warnings"] == []


def test_export_derives_frontmatter_for_unmigrated_page(tmp_path):
    dom = tmp_path / "d"
    dom.mkdir(parents=True)
    src = "# My Page\n\n## Overview\nsome summary.\n\n## Body\ntext\n"
    (dom / "a.md").write_text(src, encoding="utf-8")
    dest = tmp_path / "out"
    export_domain(str(dom), str(dest))
    exported = (dest / "a.md").read_text(encoding="utf-8")
    assert exported.startswith("---\n")
    assert "type: concept" in exported
    assert "title: My Page" in exported
    # source is unchanged on disk -- no frontmatter added to the original
    assert (dom / "a.md").read_text(encoding="utf-8") == src


def test_export_warns_on_reserved_slug_collision(tmp_path):
    dom = tmp_path / "d"
    dom.mkdir(parents=True)
    (dom / "index.md").write_text("# Real Index\n\nsome content\n", encoding="utf-8")
    dest = tmp_path / "out"
    res = export_domain(str(dom), str(dest))
    assert res["warnings"]
    assert any("index.md" in w for w in res["warnings"])


def test_export_dedupes_tags(tmp_path):
    dom = tmp_path / "d"
    dom.mkdir(parents=True)
    (dom / "a.md").write_text(
        "---\ntype: concept\ntags: [config, config]\n---\n# A\n\n## Overview\ns\n",
        encoding="utf-8")
    dest = tmp_path / "out"
    export_domain(str(dom), str(dest))
    exported = (dest / "a.md").read_text(encoding="utf-8")
    assert "tags: [config]" in exported
