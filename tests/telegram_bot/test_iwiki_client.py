from contextlib import asynccontextmanager
import asyncio
import traceback
from types import SimpleNamespace

import anyio
import httpx
from mcp import McpError
from mcp.types import ErrorData
import pytest

try:
    from builtins import BaseExceptionGroup, ExceptionGroup
except ImportError:
    from exceptiongroup import BaseExceptionGroup, ExceptionGroup

import iwiki_mcp.telegram_bot.iwiki as iwiki_module
from iwiki_mcp.telegram_bot.iwiki import (
    RemoteIwikiClient,
    RemoteIwikiError,
    _direct_httpx_client,
    open_remote_iwiki,
)


def assert_sanitized_error(captured, marker):
    formatted = "".join(
        traceback.format_exception(captured.type, captured.value, captured.tb)
    )
    assert marker not in formatted
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def status_error(status):
    request = httpx.Request("GET", "https://remote-url-marker.example/mcp")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(
        "remote-status-marker", request=request, response=response
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

    with pytest.raises(RemoteIwikiError, match="no_remote_domains") as captured:
        await RemoteIwikiClient(call_tool).list_domains()

    assert captured.value.retryable is False


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


@pytest.mark.asyncio
async def test_unknown_remote_error_code_is_sanitized():
    marker = "remote-error-code-payload-marker"

    async def call_tool(name, arguments):
        return {"error": marker}

    with pytest.raises(RemoteIwikiError) as captured:
        await RemoteIwikiClient(call_tool).list_domains()

    assert str(captured.value) == "remote_call_failed"
    assert_sanitized_error(captured, marker)


@pytest.mark.asyncio
async def test_remote_runtime_failure_has_no_private_exception_chain():
    marker = "runtime-remote-url-token-marker"

    async def call_tool(name, arguments):
        raise RuntimeError(marker)

    with pytest.raises(RemoteIwikiError) as captured:
        await RemoteIwikiClient(call_tool).list_domains()

    assert_sanitized_error(captured, marker)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "message"),
    (
        (-32600, "Session terminated"),
        (-32600, "Session not found"),
        (32600, "Session terminated"),
    ),
    ids=("server-terminated", "server-not-found", "sdk-terminated"),
)
async def test_exact_stale_session_mcp_error_is_sanitized_and_retryable(
    code, message
):
    marker = "session-error-private-payload-marker"

    async def call_tool(name, arguments):
        raise McpError(
            ErrorData(
                code=code,
                message=message,
                data={"detail": marker},
            )
        )

    with pytest.raises(RemoteIwikiError) as captured:
        await RemoteIwikiClient(call_tool).list_domains()

    assert str(captured.value) == "remote_call_failed"
    assert captured.value.retryable is True
    assert_sanitized_error(captured, marker)


@pytest.mark.asyncio
async def test_anyio_like_cancellation_without_owned_scope_is_not_converted():
    marker = "Cancelled via cancel scope private-session-marker"

    async def call_tool(name, arguments):
        raise asyncio.CancelledError(marker)

    with pytest.raises(asyncio.CancelledError) as captured:
        await RemoteIwikiClient(call_tool).list_domains()

    assert str(captured.value) == marker


@pytest.mark.asyncio
async def test_plain_task_cancellation_is_not_converted_to_remote_failure():
    async def call_tool(name, arguments):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await RemoteIwikiClient(call_tool).list_domains()


@pytest.mark.asyncio
async def test_external_anyio_cancellation_is_not_converted_to_remote_failure():
    started = anyio.Event()

    async def call_tool(name, arguments):
        started.set()
        await anyio.sleep_forever()

    async def invoke():
        with pytest.raises(asyncio.CancelledError):
            await RemoteIwikiClient(call_tool).list_domains()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(invoke)
        await started.wait()
        task_group.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_stale_session_http_404_is_retryable():
    request = httpx.Request(
        "POST",
        "https://wiki.example/mcp",
        headers={"Mcp-Session-Id": "opaque-session"},
    )
    response = httpx.Response(404, request=request)

    async def call_tool(name, arguments):
        raise httpx.HTTPStatusError(
            "stale-session-private-marker",
            request=request,
            response=response,
        )

    with pytest.raises(RemoteIwikiError) as captured:
        await RemoteIwikiClient(call_tool).list_domains()

    assert str(captured.value) == "remote_call_failed"
    assert captured.value.retryable is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "message_template"),
    (
        (-32601, "Session terminated"),
        (-32600, "Different invalid request: {marker}"),
        (32600, "Different invalid request: {marker}"),
    ),
    ids=(
        "different-code",
        "server-different-invalid-request",
        "sdk-different-invalid-request",
    ),
)
async def test_other_mcp_errors_remain_sanitized_and_non_retryable(
    code, message_template
):
    marker = "other-mcp-private-payload-marker"
    error_data = ErrorData(
        code=code,
        message=message_template.format(marker=marker),
        data={"detail": marker},
    )

    async def call_tool(name, arguments):
        raise McpError(error_data)

    with pytest.raises(RemoteIwikiError) as captured:
        await RemoteIwikiClient(call_tool).list_domains()

    assert str(captured.value) == "remote_call_failed"
    assert captured.value.retryable is False
    assert_sanitized_error(captured, marker)


def test_remote_decode_failure_has_no_private_exception_chain():
    marker = "decode-remote-response-marker"
    result = SimpleNamespace(
        isError=False,
        content=[SimpleNamespace(text=marker)],
    )

    with pytest.raises(RemoteIwikiError) as captured:
        iwiki_module._decode_result(result)

    assert_sanitized_error(captured, marker)


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
    assert captured.value.retryable is True
    assert marker not in formatted
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "retryable"),
    ((401, False), (403, False), (404, False), (429, True), (500, True)),
)
async def test_remote_startup_http_status_retryability(
    monkeypatch, status, retryable
):
    request = httpx.Request("GET", "https://remote-url-marker.example/mcp")
    response = httpx.Response(status, request=request)
    failure = httpx.HTTPStatusError(
        "remote-status-marker", request=request, response=response
    )

    @asynccontextmanager
    async def failing_stream(url, **kwargs):
        raise failure
        yield

    monkeypatch.setattr(iwiki_module, "streamablehttp_client", failing_stream)

    with pytest.raises(RemoteIwikiError) as captured:
        async with open_remote_iwiki(
            "https://wiki.example/mcp", "iwiki-token"
        ):
            pass

    assert captured.value.retryable is retryable


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "retryable"),
    (
        (
            ExceptionGroup(
                "transient-group-marker",
                (
                    ExceptionGroup(
                        "nested-group-marker",
                        (httpx.ConnectError("connect-marker"),),
                    ),
                    httpx.ReadTimeout("timeout-marker"),
                    status_error(503),
                ),
            ),
            True,
        ),
        (
            ExceptionGroup(
                "mixed-group-marker",
                (
                    httpx.ConnectError("connect-marker"),
                    httpx.UnsupportedProtocol("unsupported-marker"),
                ),
            ),
            False,
        ),
    ),
)
async def test_remote_startup_exception_group_retryability(
    monkeypatch, failure, retryable
):
    @asynccontextmanager
    async def failing_stream(url, **kwargs):
        raise failure
        yield

    monkeypatch.setattr(iwiki_module, "streamablehttp_client", failing_stream)

    with pytest.raises(RemoteIwikiError) as captured:
        async with open_remote_iwiki(
            "https://wiki.example/mcp", "iwiki-token"
        ):
            pass

    assert captured.value.retryable is retryable
    formatted = "".join(
        traceback.format_exception(captured.type, captured.value, captured.tb)
    )
    assert "marker" not in formatted
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.asyncio
async def test_remote_startup_cancellation_group_is_not_converted(monkeypatch):
    cancellation = asyncio.CancelledError("cancellation-marker")
    failure = BaseExceptionGroup("cancellation-group-marker", (cancellation,))

    @asynccontextmanager
    async def failing_stream(url, **kwargs):
        raise failure
        yield

    monkeypatch.setattr(iwiki_module, "streamablehttp_client", failing_stream)

    with pytest.raises(BaseExceptionGroup) as captured:
        async with open_remote_iwiki(
            "https://wiki.example/mcp", "iwiki-token"
        ):
            pass

    assert captured.value is failure
