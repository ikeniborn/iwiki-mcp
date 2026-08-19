"""Outbound MCP publication and read adapter contracts."""
from __future__ import annotations

import json

import pytest

from iwiki_mcp.codegraph.context import validate_context_request
from iwiki_mcp.codegraph.mcp_adapter import (
    McpCodeGraphReader,
    McpSnapshotPublisher,
    RemoteMcpTransport,
)
from iwiki_mcp.codegraph.models import CodeGraphError
from iwiki_mcp.codegraph.publication import (
    PublicationSession,
    SnapshotBatch,
    SnapshotHeader,
)
from iwiki_mcp.codegraph.query import validate_search_request


_URL = "https://wiki.example/mcp"
_TOKEN = "fixture-bearer-not-a-real-token"


class _FakeResult:
    def __init__(self, payload):
        self.content = [type("Text", (), {"type": "text", "text": payload})()]
        self.isError = False


class _FakeSession:
    """Record every outbound tool call and answer with canned payloads."""

    def __init__(self, replies=None, failure=None):
        self.calls = []
        self.initialized = 0
        self._replies = replies or {}
        self._failure = failure

    async def initialize(self):
        self.initialized += 1

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        if self._failure is not None:
            raise self._failure
        payload = self._replies.get(name, {"state": "ready"})
        return _FakeResult(json.dumps(payload))


@pytest.fixture
def fake_session():
    return _FakeSession(
        replies={
            "wiki_code_publish_begin": {
                "session_id": "remote-session",
                "lease_expires_at": "2026-08-16T10:00:00+00:00",
                "base_snapshot_revision": "sha256:base",
                "base_markdown_token": 7,
            },
            "wiki_code_publish_batch": {"accepted": True},
            "wiki_code_publish_finalize": {"state": "ready"},
            "wiki_code_publish_abort": {"state": "aborted"},
            "wiki_code_status": {"state": "ready", "fresh": True},
            "wiki_code_search": {"state": "ready", "results": []},
            "wiki_code_context": {"state": "ready", "nodes": []},
        }
    )


_CONFIGURED = {
    "IWIKI_CODE_GRAPH_MCP_URL": _URL,
    "IWIKI_CODE_GRAPH_MCP_TOKEN": _TOKEN,
}


def _transport(session, environ=None, primary=None):
    return RemoteMcpTransport(
        environ=_CONFIGURED if environ is None else environ,
        session_factory=lambda url, headers: _FakeConnection(session),
        primary=primary,
    )


class _FakeConnection:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *_exc):
        return False


@pytest.fixture
def mcp_publisher(fake_session):
    publisher = McpSnapshotPublisher(_transport(fake_session))
    publisher.session = PublicationSession(
        session_id="remote-session",
        lease_expires_at="2026-08-16T10:00:00+00:00",
        base_snapshot_revision="sha256:base",
        base_markdown_token=7,
    )
    return publisher


@pytest.fixture
def mcp_reader(fake_session):
    return McpCodeGraphReader(_transport(fake_session))


@pytest.fixture
def header():
    return SnapshotHeader(
        protocol_version=1,
        schema_version=2,
        repository_id="docs",
        source_fingerprint="source",
        parser_fingerprint="parser",
        normalizer_version="normalizer-1",
        unicode_data_version="15.1",
        languages=("python",),
        expected_counts={
            "repositories": 1, "files": 1, "symbols": 0, "relations": 0
        },
        graph_payload_revision="sha256:payload",
    )


@pytest.fixture
def batch():
    payload = json.dumps([{"file_id": "py:file:0", "path": "a.py"}]).encode()
    return SnapshotBatch(
        kind="files",
        ordinal=0,
        row_count=1,
        byte_count=len(payload),
        payload_hash="sha256:batch",
        payload=payload,
    )


def test_missing_endpoint_or_token_is_an_invalid_config(fake_session):
    for environ in (
        {},
        {"IWIKI_CODE_GRAPH_MCP_URL": _URL},
        {"IWIKI_CODE_GRAPH_MCP_TOKEN": _TOKEN},
    ):
        with pytest.raises(CodeGraphError) as failure:
            _transport(fake_session, environ=environ)
        assert failure.value.code == "invalid_config"


def test_transport_never_reveals_endpoint_or_token(fake_session):
    transport = _transport(fake_session)

    assert _TOKEN not in repr(transport)
    assert _URL not in repr(transport)


def test_mcp_begin_sends_only_the_shared_header(fake_session, header):
    publisher = McpSnapshotPublisher(_transport(fake_session))

    session = publisher.begin(header)

    name, arguments = fake_session.calls[-1]
    assert name == "wiki_code_publish_begin"
    assert set(arguments) == {"header"}
    assert set(arguments["header"]) == {
        "protocol_version",
        "schema_version",
        "repository_id",
        "source_fingerprint",
        "parser_fingerprint",
        "normalizer_version",
        "unicode_data_version",
        "languages",
        "expected_counts",
        "graph_payload_revision",
    }
    assert session.session_id == "remote-session"
    assert session.base_snapshot_revision == "sha256:base"
    assert fake_session.initialized == 1


def test_mcp_begin_reports_absent_batch_limits_as_none(fake_session, header):
    # fake_session's canned reply has no max_batch_rows/max_batch_bytes keys —
    # this proves an older-server response doesn't break parsing.
    publisher = McpSnapshotPublisher(_transport(fake_session))

    session = publisher.begin(header)

    assert session.max_batch_rows is None
    assert session.max_batch_bytes is None


def test_mcp_begin_parses_batch_limits_when_present(header):
    session_with_limits = _FakeSession(
        replies={
            "wiki_code_publish_begin": {
                "session_id": "remote-session",
                "lease_expires_at": "2026-08-16T10:00:00+00:00",
                "base_snapshot_revision": "sha256:base",
                "base_markdown_token": 7,
                "max_batch_rows": 1000,
                "max_batch_bytes": 1_000_000,
            },
        }
    )
    publisher = McpSnapshotPublisher(_transport(session_with_limits))

    session = publisher.begin(header)

    assert session.max_batch_rows == 1000
    assert session.max_batch_bytes == 1_000_000


def test_mcp_batch_call_has_no_scope_override(fake_session, mcp_publisher, batch):
    mcp_publisher.publish_batch(mcp_publisher.session, batch)

    name, arguments = fake_session.calls[-1]
    assert name == "wiki_code_publish_batch"
    assert set(arguments) == {
        "session_id", "kind", "ordinal", "rows", "payload_hash"
    }
    assert "iwiki_id" not in arguments and "domain" not in arguments
    assert arguments["rows"] == [{"file_id": "py:file:0", "path": "a.py"}]


def test_mcp_finalize_uses_header_as_single_revision_source(
    fake_session, mcp_publisher
):
    mcp_publisher.finalize(mcp_publisher.session)

    name, arguments = fake_session.calls[-1]
    assert name == "wiki_code_publish_finalize"
    assert set(arguments) == {"session_id"}


def test_mcp_abort_sends_only_the_session(fake_session, mcp_publisher):
    assert mcp_publisher.abort(mcp_publisher.session) == {"state": "aborted"}

    name, arguments = fake_session.calls[-1]
    assert name == "wiki_code_publish_abort"
    assert set(arguments) == {"session_id"}


def test_reader_maps_public_requests_without_scope_fields(
    fake_session, mcp_reader
):
    mcp_reader.status()
    assert fake_session.calls[-1] == ("wiki_code_status", {})

    mcp_reader.search(validate_search_request("needle", limit=5))
    name, arguments = fake_session.calls[-1]
    assert name == "wiki_code_search"
    assert arguments == {
        "query": "needle",
        "kinds": [
            "async_function", "class", "enum", "file", "function",
            "interface", "method", "module", "type_alias",
        ],
        "path": None,
        "languages": ["python"],
        "limit": 5,
    }

    mcp_reader.context(validate_context_request(["py:file:" + "0" * 64]))
    name, arguments = fake_session.calls[-1]
    assert name == "wiki_code_context"
    assert set(arguments) == {
        "seeds",
        "direction",
        "depth",
        "relations",
        "include_source",
        "include_wiki",
        "max_nodes",
        "max_files",
        "max_source_bytes",
    }
    assert "domain" not in arguments and "iwiki_id" not in arguments


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("connection refused"), ValueError("bad frame")],
)
def test_transport_failures_are_sanitized(header, batch, failure):
    session = _FakeSession(failure=failure)
    transport = _transport(session)
    publisher = McpSnapshotPublisher(transport)
    reader = McpCodeGraphReader(transport)
    remote = PublicationSession(
        session_id="remote-session",
        lease_expires_at="2026-08-16T10:00:00+00:00",
        base_snapshot_revision=None,
        base_markdown_token=0,
    )

    assert publisher.begin(header) == {
        "error": "remote_mcp_failed",
        "hint": "retry the remote code graph call",
    }
    assert publisher.publish_batch(remote, batch)["error"] == "remote_mcp_failed"
    assert publisher.finalize(remote)["error"] == "remote_mcp_failed"
    search = reader.search(validate_search_request("needle"))
    assert search["error"] == "remote_mcp_failed"
    assert search["state"] == "missing"
    assert search["results"] == []
    assert _TOKEN not in json.dumps(search)


def test_malformed_remote_payloads_are_rejected(header):
    class _Malformed(_FakeSession):
        async def call_tool(self, name, arguments=None):
            self.calls.append((name, arguments))
            return _FakeResult("not json")

    publisher = McpSnapshotPublisher(_transport(_Malformed()))

    assert publisher.begin(header)["error"] == "remote_mcp_failed"


def test_configured_primary_binds_before_the_first_tool_call(fake_session, header):
    publisher = McpSnapshotPublisher(_transport(fake_session, primary="iwiki-mcp"))

    publisher.begin(header)

    name, arguments = fake_session.calls[0]
    assert name == "wiki_bind"
    assert arguments == {"primary": "iwiki-mcp"}
    assert fake_session.calls[1][0] == "wiki_code_publish_begin"


def test_no_primary_configured_skips_bind(fake_session, header):
    publisher = McpSnapshotPublisher(_transport(fake_session, primary=None))

    publisher.begin(header)

    assert [name for name, _ in fake_session.calls] == ["wiki_code_publish_begin"]


async def test_call_succeeds_from_inside_a_running_event_loop(fake_session, header):
    # wiki_code_index is a sync tool function that FastMCP runs inline on its
    # own running event loop thread (mcp/server/fastmcp calls it directly,
    # not via a thread pool). transport.call() must not assume it is being
    # invoked from a thread with no event loop of its own.
    publisher = McpSnapshotPublisher(_transport(fake_session))

    session = publisher.begin(header)

    assert session.session_id == "remote-session"


def test_bind_scope_mismatch_is_reported_instead_of_generic_failure(
    fake_session, header
):
    fake_session._replies["wiki_bind"] = {
        "error": "scope_mismatch",
        "hint": "token is not granted this domain",
    }
    publisher = McpSnapshotPublisher(
        _transport(fake_session, primary="other-domain")
    )

    result = publisher.begin(header)

    assert result["error"] == "scope_mismatch"
    assert fake_session.calls == [("wiki_bind", {"primary": "other-domain"})]
