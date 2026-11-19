"""Authenticated remote MCP publication and read adapters."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
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

# Closed, secret-free vocabulary describing *why* a remote call failed. The
# error code stays `remote_mcp_failed` (R-028); `reason` and the optional HTTP
# `status` only narrow it, and neither can carry a token, URL, or host detail.
_HINTS: dict[str, str] = {
    "http_status": "the remote wiki refused the code graph call; see status",
    "timeout": "the remote code graph call timed out; retry it",
    "connect": "the remote code graph endpoint was unreachable; check the URL",
    "protocol": "the remote MCP session failed; retry the remote call",
    "malformed_response": "the remote answered with a payload this client "
    "cannot decode",
    "unknown": "retry the remote code graph call",
}
_UNREADABLE: dict[str, object] = {"state": "missing", "fresh": False}


def _status_code(exc: BaseException) -> int | None:
    """Return an HTTP status carried by the exception, when it holds one."""
    for candidate in (getattr(getattr(exc, "response", None), "status_code", None),
                      getattr(exc, "status_code", None)):
        if isinstance(candidate, int) and 100 <= candidate < 600:
            return candidate
    return None


def _related(exc: BaseException):
    """Walk the exception, its group members, and its cause/context chain."""
    seen: set[int] = set()
    queue: list[BaseException | None] = [exc]
    while queue:
        current = queue.pop(0)
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        queue.extend(getattr(current, "exceptions", ()) or ())
        queue.append(current.__cause__)
        queue.append(current.__context__)


def _remote_failed(exc: BaseException | None = None) -> dict[str, object]:
    """Map one failure to `remote_mcp_failed` plus a safe reason and status."""
    reason, status = "malformed_response", None
    if exc is not None:
        reason, status = "unknown", None
        for current in _related(exc):
            code = _status_code(current)
            if code is not None:
                reason, status = "http_status", code
                break
            name = type(current).__name__
            if "Mcp" in name or "Protocol" in name:
                reason = "protocol"
            elif isinstance(current, ValueError):
                reason = "malformed_response"
            elif reason == "unknown":
                if isinstance(current, TimeoutError) or "Timeout" in name:
                    reason = "timeout"
                elif isinstance(current, ConnectionError) or "Connect" in name:
                    reason = "connect"
    payload: dict[str, object] = {
        "error": "remote_mcp_failed",
        "reason": reason,
        "hint": _HINTS[reason],
    }
    if status is not None:
        payload["status"] = status
    return payload


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
        answer, _ = self.call_with_origin(tool_name, arguments)
        return answer

    def call_with_origin(
        self, tool_name: str, arguments: Mapping[str, object]
    ) -> tuple[dict[str, object], str]:
        """Distinguish a tool response, a refused bind and a local transport failure."""
        # FastMCP runs sync tool functions inline on its own running event
        # loop thread, so anyio.run() here would raise "already running" -
        # run the coroutine on a fresh worker thread instead, which has no
        # event loop of its own regardless of the caller's context.
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(
                    anyio.run, self._invoke, tool_name, dict(arguments)
                ).result(), "tool"
        except Exception as exc:
            # A session's cleanup can group its own timeout with the bind refusal.
            for cause in _related(exc):
                if isinstance(cause, _RemoteBindError):
                    return dict(cause.payload), "bind"
            return _remote_failed(exc), "transport"

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
                if tool_name == "wiki_code_status" and not _matching_scope(
                    bind_payload, self._primary
                ):
                    raise _RemoteBindError({
                        "error": "binding_not_selected",
                        "hint": "bind the remote session to the requested primary",
                    })
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
                max_batch_rows=result.get("max_batch_rows"),
                max_batch_bytes=result.get("max_batch_bytes"),
            )
        except KeyError:
            return _remote_failed()

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


def _matching_scope(answer: Mapping[str, object], primary: str) -> bool:
    return (
        answer.get("binding_source", "session") == "session"
        and not answer.get("primary_substituted")
        and "binding_defaulted" not in (answer.get("warnings") or ())
        and all(answer.get(field, primary) == primary for field in ("domain", "primary"))
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
        return {**_UNREADABLE, **result} if "error" in result else result

    def status_for_job(self, primary: str) -> tuple[dict[str, object], bool]:
        """Check the origin and scope before combining remote status with a local job."""
        raw, origin = self._transport.call_with_origin("wiki_code_status", {})
        raw.pop("job", None)  # This caller's process owns its job descriptors.
        if "error" not in raw:
            healthy = raw.get("state") == "ready" and raw.get("fresh") is True
            return raw, healthy and "code" not in raw and origin == "tool" and _matching_scope(
                raw, primary
            )
        status = {**_UNREADABLE, **raw}
        error = raw["error"]
        code = raw.get("code", error)
        # A normalized message can accompany a code. Two conflicting machine
        # tokens, an HTTP status, or a malformed error are never evidence of availability.
        if not isinstance(error, str) or not error.strip() or "status" in raw:
            return status, False
        if error.isidentifier() and error != code:
            return status, False
        outage = (
            code == "remote_mcp_failed"
            and raw.get("reason") in ("connect", "timeout")
        )
        if origin == "transport":
            return status, outage
        verified = (
            origin == "tool"
            and raw.get("binding_source") == "session"
            and _matching_scope(raw, primary)
        )
        return status, verified and (code in ("missing_snapshot", "stale_snapshot") or outage)

    def search(self, request: ValidatedSearchRequest) -> dict[str, object]:
        """Search the remote snapshot under the shared validated bounds.

        `languages` is sent only when the caller named one. The remote
        snapshot -- not this project's `code_graph.languages` -- is the
        authority for the unfiltered scope, and sending the local list
        would make the remote's snapshot-scoped validator refuse every
        search whose project declares a language the snapshot lacks.
        """
        payload: dict[str, object] = {
            "query": request.query,
            "kinds": list(request.kinds),
            "path": request.path,
            "limit": request.limit,
        }
        if request.languages_requested:
            payload["languages"] = list(request.languages)
        result = self._transport.call("wiki_code_search", payload)
        if "error" in result:
            return {**_UNREADABLE, **result, "results": []}
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
                **result,
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
