from contextlib import asynccontextmanager
import traceback

import httpx
import pytest

import iwiki_mcp.telegram_bot.iwiki as iwiki_module
from iwiki_mcp.telegram_bot.iwiki import (
    RemoteIwikiClient,
    RemoteIwikiError,
    _direct_httpx_client,
    open_remote_iwiki,
)


@pytest.mark.asyncio
async def test_list_domains_returns_server_visible_domains():
    async def call_tool(name, arguments):
        assert (name, arguments) == ("wiki_status", {})
        return {"domains": ["team", "public"]}

    assert await RemoteIwikiClient(call_tool).list_domains() == ["team", "public"]


@pytest.mark.asyncio
async def test_list_domains_rejects_empty_remote_scope():
    async def call_tool(name, arguments):
        return {"domains": []}

    with pytest.raises(RemoteIwikiError, match="no_remote_domains"):
        await RemoteIwikiClient(call_tool).list_domains()


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
async def test_search_normalizes_iwiki_file_to_page_slug():
    async def call_tool(name, arguments):
        return {"results": [{"file": "guide/a.md", "heading": "Answer"}]}

    results = await RemoteIwikiClient(call_tool).search("team", "question")

    assert results == [
        {"file": "guide/a.md", "slug": "guide/a", "heading": "Answer"}
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


def test_direct_http_factory_ignores_environment_proxies(monkeypatch):
    seen = {}

    class RecordingAsyncClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setenv("HTTP_PROXY", "http://environment-proxy-marker:8000")
    monkeypatch.setenv("HTTPS_PROXY", "http://environment-proxy-marker:8001")
    monkeypatch.setenv("ALL_PROXY", "socks5://environment-proxy-marker:1080")
    monkeypatch.setenv("NO_PROXY", "wiki.example")
    monkeypatch.setattr(iwiki_module.httpx, "AsyncClient", RecordingAsyncClient)

    _direct_httpx_client(
        headers={"Authorization": "Bearer token"}, timeout=30, auth=None
    )

    assert seen == {
        "headers": {"Authorization": "Bearer token"},
        "timeout": 30,
        "auth": None,
        "follow_redirects": True,
        "trust_env": False,
    }


@pytest.mark.asyncio
async def test_remote_stream_uses_direct_http_factory(monkeypatch):
    seen = {}

    @asynccontextmanager
    async def fake_stream(url, **kwargs):
        seen.update(url=url, kwargs=kwargs)
        yield "read", "write", None

    class FakeSession:
        def __init__(self, read_stream, write_stream):
            seen["streams"] = (read_stream, write_stream)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def initialize(self):
            seen["initialized"] = True

        async def call_tool(self, name, arguments):
            return {"domains": ["team"]}

    monkeypatch.setattr(iwiki_module, "streamablehttp_client", fake_stream)
    monkeypatch.setattr(iwiki_module, "ClientSession", FakeSession)

    async with open_remote_iwiki(
        "https://wiki.example/mcp", "iwiki-token"
    ) as remote:
        assert await remote.list_domains() == ["team"]

    assert seen["url"] == "https://wiki.example/mcp"
    assert seen["kwargs"]["headers"] == {
        "Authorization": "Bearer iwiki-token"
    }
    assert seen["kwargs"]["httpx_client_factory"] is _direct_httpx_client
    assert seen["streams"] == ("read", "write")
    assert seen["initialized"] is True


@pytest.mark.asyncio
async def test_remote_connection_failure_is_sanitized_before_startup_retry(
    monkeypatch,
):
    marker = "remote-url-token-response-marker"

    @asynccontextmanager
    async def failing_stream(url, **kwargs):
        raise httpx.ConnectError(marker)
        yield

    monkeypatch.setattr(iwiki_module, "streamablehttp_client", failing_stream)

    with pytest.raises(RemoteIwikiError) as captured:
        async with open_remote_iwiki(
            "https://wiki.example/mcp", "iwiki-token"
        ):
            pass

    formatted = "".join(
        traceback.format_exception(captured.type, captured.value, captured.tb)
    )
    assert str(captured.value) == "remote_call_failed"
    assert marker not in formatted
