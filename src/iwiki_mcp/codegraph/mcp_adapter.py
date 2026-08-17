"""Authenticated remote MCP publication and read adapters."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
import json
import os
from typing import Any

import anyio

from .context import ContextRequest
from .models import CodeGraphError
from .publication import (
    PublicationSession,
    SnapshotBatch,
    SnapshotHeader,
    header_payload,
)
from .query import ValidatedSearchRequest


ENDPOINT_ENV = "IWIKI_CODE_GRAPH_MCP_URL"
TOKEN_ENV = "IWIKI_CODE_GRAPH_MCP_TOKEN"

_CONNECT_TIMEOUT = 30
_READ_TIMEOUT = 300

_REMOTE_FAILED: dict[str, object] = {
    "error": "remote_mcp_failed",
    "hint": "retry the remote code graph call",
}
_UNREADABLE: dict[str, object] = {
    "state": "missing",
    "fresh": False,
    **_REMOTE_FAILED,
}


class CodeGraphAdapterError(CodeGraphError):
    """Raised when adapter configuration cannot produce a remote call."""

    code = "invalid_config"


class _RemoteBindError(Exception):
    """Carries a typed bind failure through to the caller, unmasked."""

    def __init__(self, payload: dict[str, object]) -> None:
        super().__init__(payload.get("error", "remote_mcp_failed"))
        self.payload = payload


@asynccontextmanager
async def _official_session(url: str, headers: Mapping[str, str]):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(
        url,
        headers=dict(headers),
        timeout=_CONNECT_TIMEOUT,
        sse_read_timeout=_READ_TIMEOUT,
    ) as (read_stream, write_stream, _session_id):
        async with ClientSession(read_stream, write_stream) as session:
            yield session


def _decoded(result: Any) -> dict[str, object]:
    content = getattr(result, "content", None)
    if not isinstance(content, list) or len(content) != 1:
        raise ValueError("remote result must hold one content item")
    text = getattr(content[0], "text", None)
    if not isinstance(text, str):
        raise ValueError("remote result must hold one text item")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("remote result must be one JSON object")
    return payload


class RemoteMcpTransport:
    """Call remote code-graph tools over one short-lived authenticated session."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        session_factory: Callable[[str, Mapping[str, str]], Any] | None = None,
        primary: str | None = None,
    ) -> None:
        values = os.environ if environ is None else environ
        url = values.get(ENDPOINT_ENV) or ""
        token = values.get(TOKEN_ENV) or ""
        if not url or not token:
            raise CodeGraphAdapterError(
                "remote code graph endpoint and token are required"
            )
        self._url = url
        self._token = token
        self._primary = primary
        self._session_factory = session_factory or _official_session

    def __repr__(self) -> str:
        return "<redacted remote code graph MCP transport>"

    def call(
        self, tool_name: str, arguments: Mapping[str, object]
    ) -> dict[str, object]:
        """Invoke one remote tool, mapping every failure to a safe code."""
        try:
            return anyio.run(self._invoke, tool_name, dict(arguments))
        except _RemoteBindError as exc:
            return dict(exc.payload)
        except Exception:
            return dict(_REMOTE_FAILED)

    async def _invoke(
        self, tool_name: str, arguments: dict[str, object]
    ) -> dict[str, object]:
        headers = {"Authorization": f"Bearer {self._token}"}
        async with self._session_factory(self._url, headers) as session:
            await session.initialize()
            if self._primary:
                bind_result = await session.call_tool(
                    "wiki_bind", arguments={"primary": self._primary}
                )
                bind_payload = _decoded(bind_result)
                if "error" in bind_payload:
                    raise _RemoteBindError(bind_payload)
            result = await session.call_tool(tool_name, arguments=arguments)
        return _decoded(result)


class McpSnapshotPublisher:
    """Publish one snapshot through the remote publication tool surface."""

    def __init__(self, transport: RemoteMcpTransport) -> None:
        self._transport = transport

    def __repr__(self) -> str:
        return "<remote code graph MCP publisher>"

    def begin(self, header: SnapshotHeader) -> PublicationSession | dict:
        """Open one remote session owned by the configured bearer identity."""
        result = self._transport.call(
            "wiki_code_publish_begin", {"header": header_payload(header)}
        )
        if "error" in result:
            return result
        try:
            return PublicationSession(
                session_id=str(result["session_id"]),
                lease_expires_at=str(result["lease_expires_at"]),
                base_snapshot_revision=(
                    None
                    if result.get("base_snapshot_revision") is None
                    else str(result["base_snapshot_revision"])
                ),
                base_markdown_token=result["base_markdown_token"],
            )
        except KeyError:
            return dict(_REMOTE_FAILED)

    def publish_batch(
        self, session: PublicationSession, batch: SnapshotBatch
    ) -> dict[str, object]:
        """Send one canonical row batch without any tenant or domain field."""
        try:
            rows = json.loads(bytes(batch.payload).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {
                "error": "invalid_batch",
                "hint": "send batches that match the declared header",
            }
        return self._transport.call(
            "wiki_code_publish_batch",
            {
                "session_id": session.session_id,
                "kind": batch.kind,
                "ordinal": batch.ordinal,
                "rows": rows,
                "payload_hash": batch.payload_hash,
            },
        )

    def finalize(self, session: PublicationSession) -> dict[str, object]:
        """Ask the target to recompute revisions and activate the snapshot."""
        return self._transport.call(
            "wiki_code_publish_finalize", {"session_id": session.session_id}
        )

    def abort(self, session: PublicationSession) -> dict[str, object]:
        """Discard the remote staging session owned by this identity."""
        return self._transport.call(
            "wiki_code_publish_abort", {"session_id": session.session_id}
        )


class McpCodeGraphReader:
    """Read one remote ready snapshot through the shared reader contract."""

    def __init__(self, transport: RemoteMcpTransport) -> None:
        self._transport = transport

    def __repr__(self) -> str:
        return "<remote code graph MCP reader>"

    def status(self) -> dict[str, object]:
        """Report remote readiness without requesting any graph rows."""
        result = self._transport.call("wiki_code_status", {})
        return {**_UNREADABLE} if "error" in result else result

    def search(self, request: ValidatedSearchRequest) -> dict[str, object]:
        """Search the remote snapshot under the shared validated bounds."""
        result = self._transport.call(
            "wiki_code_search",
            {
                "query": request.query,
                "kinds": list(request.kinds),
                "path": request.path,
                "languages": [request.language],
                "limit": request.limit,
            },
        )
        if "error" in result:
            return {**_UNREADABLE, "results": []}
        return result

    def context(self, request: ContextRequest) -> dict[str, object]:
        """Compose remote context under the shared validated budgets."""
        result = self._transport.call(
            "wiki_code_context",
            {
                "seeds": list(request.seeds),
                "direction": request.direction,
                "depth": request.depth,
                "relations": (
                    None if request.relations is None else list(request.relations)
                ),
                "include_source": request.include_source,
                "include_wiki": request.include_wiki,
                "max_nodes": request.max_nodes,
                "max_files": request.max_files,
                "max_source_bytes": request.max_source_bytes,
            },
        )
        if "error" in result:
            return {
                **_UNREADABLE,
                "seeds": list(request.seeds),
                "nodes": [],
                "relations": [],
                "files": [],
                "wiki_pages": [],
                "warnings": [],
            }
        return result


__all__ = [
    "CodeGraphAdapterError",
    "ENDPOINT_ENV",
    "McpCodeGraphReader",
    "McpSnapshotPublisher",
    "RemoteMcpTransport",
    "TOKEN_ENV",
]
