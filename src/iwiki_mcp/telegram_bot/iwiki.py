"""Least-privilege client for the remote iwiki MCP surface."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
import json
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


class RemoteIwikiError(RuntimeError):
    """A sanitized remote iwiki failure."""


ToolCaller = Callable[[str, dict[str, object]], Awaitable[object]]


def _decode_result(result: object) -> dict[str, object]:
    if isinstance(result, dict):
        return result
    if getattr(result, "isError", False):
        raise RemoteIwikiError("remote_call_failed")
    content = getattr(result, "content", None)
    if not isinstance(content, list) or len(content) != 1:
        raise RemoteIwikiError("invalid_remote_response")
    text = getattr(content[0], "text", None)
    if not isinstance(text, str):
        raise RemoteIwikiError("invalid_remote_response")
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise RemoteIwikiError("invalid_remote_response") from exc
    if not isinstance(payload, dict):
        raise RemoteIwikiError("invalid_remote_response")
    return payload


class RemoteIwikiClient:
    def __init__(self, call_tool: ToolCaller):
        self._call_tool = call_tool

    async def _call(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        try:
            payload = _decode_result(await self._call_tool(name, arguments))
        except RemoteIwikiError:
            raise
        except Exception as exc:
            raise RemoteIwikiError("remote_call_failed") from exc
        error = payload.get("error")
        if error:
            code = error if isinstance(error, str) else "remote_call_failed"
            raise RemoteIwikiError(code)
        return payload

    async def list_domains(self) -> list[str]:
        payload = await self._call("wiki_status", {})
        domains = payload.get("domains")
        if not isinstance(domains, list) or not all(
            isinstance(domain, str) for domain in domains
        ):
            raise RemoteIwikiError("invalid_remote_response")
        return domains

    async def search(self, domain: str, query: str) -> list[dict[str, object]]:
        payload = await self._call(
            "wiki_search", {"domains": [domain], "query": query, "k": 5}
        )
        results = payload.get("results")
        if not isinstance(results, list) or not all(
            isinstance(result, dict) for result in results
        ):
            raise RemoteIwikiError("invalid_remote_response")
        normalized: list[dict[str, object]] = []
        for result in results:
            item = dict(result)
            if not isinstance(item.get("slug"), str):
                file_name = item.get("file")
                if not isinstance(file_name, str):
                    raise RemoteIwikiError("invalid_remote_response")
                item["slug"] = (
                    file_name[:-3] if file_name.endswith(".md") else file_name
                )
            normalized.append(item)
        return normalized

    async def read_page(
        self, domain: str, slug: str, heading: str | None = None
    ) -> dict[str, object]:
        arguments: dict[str, object] = {"domain": domain, "slug": slug}
        if heading is not None:
            arguments["heading"] = heading
        return await self._call("wiki_read_page", arguments)

    async def write_page(
        self, domain: str, slug: str, markdown: str
    ) -> dict[str, object]:
        return await self._call(
            "wiki_write_page",
            {
                "domain": domain,
                "slug": slug,
                "markdown": markdown,
                "source": "telegram-bot",
            },
        )

    async def update_section(
        self,
        domain: str,
        slug: str,
        heading: str,
        new_body: str,
        revision: int,
        section_hash: str,
    ) -> dict[str, object]:
        return await self._call(
            "wiki_update_page",
            {
                "domain": domain,
                "slug": slug,
                "heading": heading,
                "new_body": new_body,
                "expected_revision": revision,
                "expected_section_hash": section_hash,
                "source": "telegram-bot",
            },
        )


@asynccontextmanager
async def open_remote_iwiki(
    url: str, token: str
) -> AsyncIterator[RemoteIwikiClient]:
    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(
        url, headers=headers, timeout=30, sse_read_timeout=300
    ) as (read_stream, write_stream, _session_id):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            async def call_tool(name: str, arguments: dict[str, object]) -> Any:
                return await session.call_tool(name, arguments=arguments)

            yield RemoteIwikiClient(call_tool)
