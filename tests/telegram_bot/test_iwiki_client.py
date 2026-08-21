import pytest

from iwiki_mcp.telegram_bot.iwiki import RemoteIwikiClient, RemoteIwikiError


@pytest.mark.asyncio
async def test_list_domains_returns_server_visible_domains():
    async def call_tool(name, arguments):
        assert (name, arguments) == ("wiki_status", {})
        return {"domains": ["team", "public"]}

    assert await RemoteIwikiClient(call_tool).list_domains() == ["team", "public"]


@pytest.mark.asyncio
async def test_search_forces_selected_domain_only():
    calls = []

    async def call_tool(name, arguments):
        calls.append((name, arguments))
        return {"results": [{"slug": "guide/a", "heading": "Answer"}]}

    results = await RemoteIwikiClient(call_tool).search("team", "how to deploy")

    assert results == [{"slug": "guide/a", "heading": "Answer"}]
    assert calls == [
        ("wiki_search", {"domains": ["team"], "query": "how to deploy", "k": 5})
    ]


@pytest.mark.asyncio
async def test_read_page_never_changes_domain_scope():
    calls = []

    async def call_tool(name, arguments):
        calls.append((name, arguments))
        return {"domain": "team", "slug": "guide/a", "markdown": "body"}

    page = await RemoteIwikiClient(call_tool).read_page("team", "guide/a", "Steps")

    assert page["markdown"] == "body"
    assert calls == [
        ("wiki_read_page", {"domain": "team", "slug": "guide/a", "heading": "Steps"})
    ]


@pytest.mark.asyncio
async def test_write_page_forwards_only_explicit_target():
    calls = []

    async def call_tool(name, arguments):
        calls.append((name, arguments))
        return {"page": "team/runbook.md"}

    await RemoteIwikiClient(call_tool).write_page("team", "runbook", "# Runbook")

    assert calls == [
        (
            "wiki_write_page",
            {
                "domain": "team",
                "slug": "runbook",
                "markdown": "# Runbook",
                "source": "telegram-bot",
            },
        )
    ]


@pytest.mark.asyncio
async def test_update_requires_fresh_revision_and_section_hash():
    calls = []

    async def call_tool(name, arguments):
        calls.append((name, arguments))
        return {"page": "team/guide/a.md", "revision": 8}

    await RemoteIwikiClient(call_tool).update_section(
        "team", "guide/a", "Steps", "new body", 7, "abc"
    )

    assert calls == [
        (
            "wiki_update_page",
            {
                "domain": "team",
                "slug": "guide/a",
                "heading": "Steps",
                "new_body": "new body",
                "expected_revision": 7,
                "expected_section_hash": "abc",
                "source": "telegram-bot",
            },
        )
    ]


@pytest.mark.asyncio
async def test_remote_error_is_sanitized():
    async def call_tool(name, arguments):
        return {"error": "section_conflict", "detail": "secret payload"}

    with pytest.raises(RemoteIwikiError) as captured:
        await RemoteIwikiClient(call_tool).update_section(
            "team", "guide/a", "Steps", "new body", 7, "abc"
        )

    assert str(captured.value) == "section_conflict"
    assert "secret" not in str(captured.value)
