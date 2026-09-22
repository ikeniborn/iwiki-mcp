"""The tool dispatch shim: off the loop, bounded, signature-preserving."""

from __future__ import annotations

import inspect
import threading

import anyio
import pytest

from iwiki_mcp import server


def test_threaded_wrapper_is_awaitable_and_runs_off_the_calling_thread():
    calling_thread = threading.get_ident()
    seen = {}

    def handler(domain: str, slug: str = "page") -> dict:
        seen["thread"] = threading.get_ident()
        return {"domain": domain, "slug": slug}

    wrapped = server._threaded(handler)
    assert inspect.iscoroutinefunction(wrapped)

    result = anyio.run(lambda: wrapped("iwiki-mcp", slug="other"))

    assert result == {"domain": "iwiki-mcp", "slug": "other"}
    assert seen["thread"] != calling_thread


def test_threaded_wrapper_preserves_the_signature_fastmcp_reads():
    def handler(domain: str, limit: int = 5) -> dict:
        """Original docstring."""
        return {}

    wrapped = server._threaded(handler)

    assert inspect.signature(wrapped) == inspect.signature(handler)
    assert wrapped.__doc__ == "Original docstring."
    assert wrapped.__name__ == "handler"


def test_threaded_wrapper_propagates_the_exception():
    def handler() -> dict:
        raise ValueError("boom")

    wrapped = server._threaded(handler)

    with pytest.raises(ValueError, match="boom"):
        anyio.run(lambda: wrapped())


def test_every_registered_tool_is_dispatched_through_a_thread():
    """A tool left registered raw would still block the loop."""
    tools = anyio.run(server.mcp.list_tools)
    assert tools, "no tools registered"

    manager = server.mcp._tool_manager
    for tool in tools:
        registered = manager.get_tool(tool.name)
        assert registered.is_async, f"{tool.name} is registered as a sync tool"


class _FakePool:
    def __init__(self, max_size: int) -> None:
        self.max_size = max_size


def test_hosted_runtime_sizes_the_ceiling_below_the_connection_pool(monkeypatch):
    """Authentication shares the pool, so tools must never be able to drain it."""
    monkeypatch.setattr(server._TOOL_LIMITER, "total_tokens", 3, raising=False)

    server._install_hosted_runtime(_FakePool(10), None)
    try:
        assert server._TOOL_LIMITER.total_tokens == 8
    finally:
        server._clear_hosted_runtime_for_test()


def test_a_tiny_pool_still_leaves_one_tool_slot():
    server._install_hosted_runtime(_FakePool(1), None)
    try:
        assert server._TOOL_LIMITER.total_tokens == 1
    finally:
        server._clear_hosted_runtime_for_test()
