"""Exercise the same public tool sequence against both MCP transports."""


async def check_lifecycle(call, domain, *, postgres):
    for slug, heading, tags in [
        ("oldname", "Legacy", ["task"]),
        ("current-guide", "Operate", ["guide"]),
        ("active-task", "Active", ["task"]),
    ]:
        created = await call("wiki_write_page", {
            "author": "agent:iwiki-test-editor/1",
            "domain": domain, "slug": slug, "type": "guide", "tags": tags,
            "markdown": f"# {slug}\n\n## {heading}\nSharedword {heading} content.\n",
        })
        assert "error" not in created, created
    old = {"domain": domain, "slug": "guide/oldname"}
    before = await call("wiki_read_page", old)
    assert before["status"] == "stable"
    change = dict(old, status="deprecated", superseded_by="current-guide.md")
    if postgres:
        change["expected_revision"] = before["revision"]
    retired = await call("wiki_update_page", change)
    assert "error" not in retired, retired
    if postgres:
        assert (await call("wiki_update_page", change))["error"] == "conflict"
    declared = await call("wiki_read_page", old)
    assert declared["status"] == "deprecated"
    assert declared["superseded_by"] == "current-guide.md"
    assert "Legacy content" in declared["markdown"]
    options = {"mode": "lexical", "query": "oldname", "exclude_tags": ["Task", "task-history"]}
    current = await call("wiki_search", options)
    assert current == await call("wiki_search", dict(options, lifecycle="current"))
    hits = current["results"]
    assert [(r["file"], r["heading"]) for r in hits] == [("guide/current-guide.md", "Operate")]
    hit = hits[0]
    readback = await call("wiki_read_page", {
        "domain": hit["domain"], "slug": hit["file"][:-3], "heading": hit["heading"],
    })
    assert "Operate content" in str(readback)
    history = await call("wiki_search", {
        "mode": "lexical", "query": "Sharedword", "tags": ["task"], "lifecycle": "all",
    })
    assert {r["file"] for r in history["results"]} == {"guide/oldname.md", "guide/active-task.md"}
    unchanged = await call("wiki_read_page", {"domain": domain, "slug": "guide/active-task"})
    assert unchanged["status"] == "stable"
    for slug, replacement in [("cyclepath", "cyclepath.md"), ("missingpath", "missing.md"),
                              ("externalpath", "../../private/secret.md")]:
        created = await call("wiki_write_page", {
            "author": "agent:iwiki-test-editor/1",
            "domain": domain, "slug": slug, "type": "guide", "status": "deprecated",
            "superseded_by": replacement, "markdown": f"# {slug}\n\n## Old\n{slug} text.\n",
        })
        assert "error" not in created, created
        current = await call("wiki_search", {"query": slug, "mode": "lexical"})
        assert current["results"] == []
    report = await call("wiki_lint", {"domain": domain})
    findings = {row["type"] for row in report["reports"][domain]["lifecycle"]}
    assert {"cycle", "missing_replacement", "invalid_replacement"} <= findings
