"""Real CLI coverage through the hosted streamable-HTTP MCP route."""
from __future__ import annotations

from io import StringIO
import json
import math
from pathlib import Path
import re

import httpx
import pytest
from starlette.testclient import TestClient

from iwiki_mcp import admin, server
from iwiki_mcp.codegraph import application
from iwiki_mcp.postgres.config import HostedCodeGraphConfig
from iwiki_mcp.postgres.store import PostgresStore
from tests.codegraph.synthetic_wiki import (
    _embed,
    _postgres_config,
    create_sqlite_project,
)
from tests.postgres.test_code_graph_contract import _open_session, _request


pytestmark = pytest.mark.postgres_integration

_SERVER_MAX_BATCH_ROWS = 1


def _hosted_route_failure(reason, *, status=None):
    hints = {
        "http_status": "the hosted MCP HTTP call failed",
        "protocol": "the hosted MCP tool call failed",
        "malformed_response": "the hosted MCP response was malformed",
    }
    result = {
        "error": "remote_mcp_failed",
        "reason": reason,
        "hint": hints[reason],
    }
    if status is not None:
        result["status"] = status
    return result


def _same_json_value(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(
            _same_json_value(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _same_json_value(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    return left == right


def _reject_json_constant(_value):
    raise ValueError("non-standard JSON constant")


def _raw_httpx_response(payload):
    return httpx.Response(200, text=json.dumps(payload))


def _contains_non_finite_float(value):
    if type(value) is float:
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(
            _contains_non_finite_float(key)
            or _contains_non_finite_float(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_non_finite_float(item) for item in value)
    return False


def _decode_tool_response(response):
    status = getattr(response, "status_code", None)
    if type(status) is not int or status != 200:
        safe_status = (
            status
            if type(status) is int and 100 <= status < 600
            else None
        )
        return _hosted_route_failure("http_status", status=safe_status)
    try:
        envelope = response.json()
    except Exception:
        return _hosted_route_failure("malformed_response")
    if not isinstance(envelope, dict):
        return _hosted_route_failure("malformed_response")
    if _contains_non_finite_float(envelope):
        return _hosted_route_failure("malformed_response")
    if (
        envelope.get("jsonrpc") != "2.0"
        or type(envelope.get("id")) is not int
        or envelope["id"] != 2
    ):
        return _hosted_route_failure("malformed_response")
    if "error" in envelope:
        if set(envelope) != {"jsonrpc", "id", "error"}:
            return _hosted_route_failure("malformed_response")
        if not isinstance(envelope["error"], dict):
            return _hosted_route_failure("malformed_response")
        return _hosted_route_failure("protocol")
    if set(envelope) != {"jsonrpc", "id", "result"}:
        return _hosted_route_failure("malformed_response")
    result = envelope.get("result")
    if not isinstance(result, dict):
        return _hosted_route_failure("malformed_response")
    result_keys = set(result)
    if result_keys not in (
        {"content"},
        {"content", "isError"},
        {"content", "structuredContent"},
        {"content", "structuredContent", "isError"},
    ):
        return _hosted_route_failure("malformed_response")
    if "isError" in result:
        if not isinstance(result["isError"], bool):
            return _hosted_route_failure("malformed_response")
    content = result.get("content")
    if not isinstance(content, list) or len(content) != 1:
        return _hosted_route_failure("malformed_response")
    item = content[0]
    if (
        not isinstance(item, dict)
        or set(item) != {"type", "text"}
        or item.get("type") != "text"
        or not isinstance(item.get("text"), str)
    ):
        return _hosted_route_failure("malformed_response")
    if result.get("isError") is True:
        if "structuredContent" in result:
            return _hosted_route_failure("malformed_response")
        return _hosted_route_failure("protocol")
    try:
        payload = json.loads(
            item["text"], parse_constant=_reject_json_constant
        )
    except (TypeError, ValueError):
        return _hosted_route_failure("malformed_response")
    if not isinstance(payload, dict):
        return _hosted_route_failure("malformed_response")
    if _contains_non_finite_float(payload):
        return _hosted_route_failure("malformed_response")
    if "structuredContent" in result:
        structured = result["structuredContent"]
        if (
            not isinstance(structured, dict)
            or not _same_json_value(structured, payload)
        ):
            return _hosted_route_failure("malformed_response")
    return payload


class _HostedJsonRpcRoute:
    """Call hosted tools and sanitize every non-tool JSON-RPC failure."""

    def __init__(self, client, token, session_id):
        self._client = client
        self._token = token
        self._session_id = session_id

    def __repr__(self):
        return "<redacted hosted MCP CLI route>"

    def call(self, name, arguments):
        response = _request(
            self._client,
            self._token,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            session_id=self._session_id,
        )
        return _decode_tool_response(response)


class InProcessMcpTransport:
    """Record calls while forwarding them to the real hosted JSON-RPC route."""

    def __init__(self, route, primary, *, reject_batch_at=None):
        self.route = route
        self.primary = primary
        self.reject_batch_at = reject_batch_at
        self.calls = []
        self.attempts = []
        self._batch_count = 0

    def __repr__(self):
        return "<redacted in-process MCP transport>"

    def call(self, name, arguments):
        recorded = dict(arguments)
        self.attempts.append((name, recorded))
        if name != "wiki_bind" and not any(
            call[0] == "wiki_bind" for call in self.calls
        ):
            bind_arguments = {"primary": self.primary}
            self.calls.append(("wiki_bind", bind_arguments))
            bound = self.route.call("wiki_bind", bind_arguments)
            if "error" in bound:
                return bound
        if name == "wiki_code_publish_batch":
            self._batch_count += 1
            if self._batch_count == self.reject_batch_at:
                return {
                    "error": "remote_mcp_failed",
                    "reason": "http_status",
                    "status": 503,
                    "hint": (
                        "the remote wiki refused the code graph call; see status"
                    ),
                }
        self.calls.append((name, recorded))
        return self.route.call(name, recorded)


class _FakeRoute:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def call(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        return dict(self.replies[name])


class _FakeHttpResponse:
    def __init__(self, payload, *, status_code=200, text="raw response"):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._payload


class _DecodedFakeRoute:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def call(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        return _decode_tool_response(self.responses[name])


def _tool_result_envelope(payload, *, is_error=False):
    result = {
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "isError": is_error,
    }
    if not is_error:
        result["structuredContent"] = payload
    return {
        "jsonrpc": "2.0",
        "id": 2,
        "result": result,
    }


def _raw_result_envelope(result, **extra):
    return {
        "jsonrpc": "2.0",
        "id": 2,
        "result": result,
        **extra,
    }


def test_hosted_route_decoder_returns_normal_tool_result():
    expected = {"state": "ready", "snapshot_revision": "sha256:fixture"}

    result = _decode_tool_response(
        _FakeHttpResponse(_tool_result_envelope(expected))
    )

    assert result == expected


def test_hosted_route_decoder_accepts_absent_optional_is_error():
    expected = {"state": "ready"}
    response = _FakeHttpResponse(_raw_result_envelope({
        "content": [{"type": "text", "text": json.dumps(expected)}],
    }))

    assert _decode_tool_response(response) == expected


@pytest.mark.parametrize("status", [200.0, True, "200"])
def test_hosted_route_decoder_requires_exact_integer_http_status(status):
    response = _FakeHttpResponse(
        _tool_result_envelope({"state": "ready"}),
        status_code=status,
    )

    assert _decode_tool_response(response) == _hosted_route_failure(
        "http_status"
    )


@pytest.mark.parametrize(
    "text,structured",
    [
        ('{"value":true}', {"value": 1}),
        ('{"value":false}', {"value": 0}),
        ('{"value":1}', {"value": 1.0}),
        ('{"value":1.0}', {"value": 1}),
        ('{"items":[{"value":true}]}', {"items": [{"value": 1}]}),
    ],
    ids=[
        "true-vs-one",
        "false-vs-zero",
        "integer-vs-float",
        "float-vs-integer",
        "nested-list-bool-vs-integer",
    ],
)
def test_hosted_route_decoder_compares_structured_types_exactly(
    text, structured
):
    response = _FakeHttpResponse(_raw_result_envelope({
        "content": [{"type": "text", "text": text}],
        "structuredContent": structured,
        "isError": False,
    }))

    assert _decode_tool_response(response) == _hosted_route_failure(
        "malformed_response"
    )


def test_hosted_route_decoder_ignores_json_whitespace_and_object_key_order():
    expected = {"alpha": [1, {"ready": True}], "beta": 2}
    response = _FakeHttpResponse(_raw_result_envelope({
        "content": [{
            "type": "text",
            "text": '{\n  "beta": 2, "alpha": [1, {"ready": true}]\n}',
        }],
        "structuredContent": expected,
        "isError": False,
    }))

    assert _decode_tool_response(response) == expected


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_hosted_route_decoder_rejects_nonstandard_json_constants(constant):
    response = _FakeHttpResponse(_raw_result_envelope({
        "content": [{
            "type": "text",
            "text": f'{{"value":{constant}}}',
        }],
        "isError": False,
    }))

    assert _decode_tool_response(response) == _hosted_route_failure(
        "malformed_response"
    )


@pytest.mark.parametrize(
    "text,structured",
    [
        ('{"value":1e999}', None),
        ('{"value":-1e999}', None),
        ('{"value":1e999}', {"value": float("inf")}),
        ('{"value":-1e999}', {"value": float("-inf")}),
        (
            '{"items":[{"value":1e999}]}',
            {"items": [{"value": float("inf")}]},
        ),
        ('{"value":1e999}', {"value": 0.5}),
        ('{"value":0.5}', {"value": float("nan")}),
        ('{"value":0.5}', {"value": float("inf")}),
    ],
    ids=[
        "positive-text-overflow",
        "negative-text-overflow",
        "matching-positive-infinity",
        "matching-negative-infinity",
        "nested-matching-infinity",
        "text-infinity-vs-structured-finite",
        "structured-nan-vs-finite",
        "structured-infinity-vs-finite",
    ],
)
def test_hosted_route_decoder_rejects_non_finite_tool_payloads(
    text, structured
):
    result = {
        "content": [{"type": "text", "text": text}],
        "isError": False,
    }
    if structured is not None:
        result["structuredContent"] = structured
    response = _raw_httpx_response(_raw_result_envelope(result))

    decoded = _decode_tool_response(response)

    assert decoded == _hosted_route_failure("malformed_response")
    assert "Infinity" not in json.dumps(decoded)
    assert "NaN" not in json.dumps(decoded)


@pytest.mark.parametrize("number", [float("inf"), float("-inf"), float("nan")])
def test_hosted_route_decoder_rejects_non_finite_jsonrpc_error_data(number):
    response = _raw_httpx_response(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "error": {
                "code": -32001,
                "message": "access_denied",
                "data": {
                    "number": number,
                    "sentinel": "sentinel-token private.invalid",
                },
            },
        },
    )

    decoded = _decode_tool_response(response)

    assert decoded == _hosted_route_failure("malformed_response")
    assert "sentinel" not in json.dumps(decoded) + repr(decoded)
    assert "private.invalid" not in json.dumps(decoded) + repr(decoded)


def test_hosted_route_decoder_accepts_nested_finite_fastmcp_payload():
    expected = {
        "items": [1, 1.0, {"enabled": True, "ratio": 0.5}],
    }

    decoded = _decode_tool_response(
        _FakeHttpResponse(_tool_result_envelope(expected))
    )

    assert decoded == expected
    assert type(decoded["items"][0]) is int
    assert type(decoded["items"][1]) is float


@pytest.mark.parametrize(
    "envelope",
    [
        {
            "jsonrpc": "2.0",
            "id": 2,
            "error": {
                "code": -32001,
                "message": "access_denied sentinel-token private.invalid",
                "data": {"hint": "sentinel-private-error-data"},
            },
        },
        _tool_result_envelope(
            {"error": "sentinel-private-tool-error"},
            is_error=True,
        ),
    ],
    ids=["jsonrpc-error", "tool-is-error"],
)
def test_hosted_route_decoder_sanitizes_denied_error_shapes(envelope):
    result = _decode_tool_response(
        _FakeHttpResponse(envelope, text="sentinel-private-response-body")
    )

    assert result == {
        "error": "remote_mcp_failed",
        "reason": "protocol",
        "hint": "the hosted MCP tool call failed",
    }
    encoded = json.dumps(result) + repr(result)
    for forbidden in (
        "sentinel-token",
        "private.invalid",
        "sentinel-private-error-data",
        "sentinel-private-tool-error",
        "sentinel-private-response-body",
    ):
        assert forbidden not in encoded


@pytest.mark.parametrize(
    "response,reason,status",
    [
        (
            _FakeHttpResponse(_raw_result_envelope({
                "content": [{"type": "text", "text": "{}"}],
                "isError": 1,
            })),
            "malformed_response",
            None,
        ),
        (
            _FakeHttpResponse(_raw_result_envelope({
                "content": [{"type": "text", "text": "{}"}],
                "isError": "false",
            })),
            "malformed_response",
            None,
        ),
        (
            _FakeHttpResponse(_raw_result_envelope({
                "content": [{"type": "image", "text": "{}"}],
                "isError": False,
            })),
            "malformed_response",
            None,
        ),
        (
            _FakeHttpResponse(_raw_result_envelope({"isError": False})),
            "malformed_response",
            None,
        ),
        (
            _FakeHttpResponse(_raw_result_envelope({
                "content": [
                    {"type": "text", "text": "{}"},
                    {"type": "text", "text": "{}"},
                ],
                "isError": False,
            })),
            "malformed_response",
            None,
        ),
        (
            _FakeHttpResponse(_raw_result_envelope({
                "content": "not-a-list",
                "isError": False,
            })),
            "malformed_response",
            None,
        ),
        (
            _FakeHttpResponse({
                **_tool_result_envelope({"state": "ready"}),
                "jsonrpc": "1.0",
            }),
            "malformed_response",
            None,
        ),
        (
            _FakeHttpResponse({
                key: value
                for key, value in _tool_result_envelope({}).items()
                if key != "jsonrpc"
            }),
            "malformed_response",
            None,
        ),
        (
            _FakeHttpResponse({
                **_tool_result_envelope({"state": "ready"}),
                "id": 3,
            }),
            "malformed_response",
            None,
        ),
        (
            _FakeHttpResponse({
                **_tool_result_envelope({"state": "ready"}),
                "id": 2.0,
            }),
            "malformed_response",
            None,
        ),
        (
            _FakeHttpResponse({
                key: value
                for key, value in _tool_result_envelope({}).items()
                if key != "id"
            }),
            "malformed_response",
            None,
        ),
        (
            _FakeHttpResponse({"jsonrpc": "2.0", "id": 2}),
            "malformed_response",
            None,
        ),
        (
            _FakeHttpResponse(_raw_result_envelope([])),
            "malformed_response",
            None,
        ),
        (
            _FakeHttpResponse(_raw_result_envelope({
                "content": [{"type": "text", "text": 7}],
                "isError": False,
            })),
            "malformed_response",
            None,
        ),
        (
            _FakeHttpResponse(_raw_result_envelope({
                "content": [{"type": "text", "text": "not-json"}],
                "isError": False,
            })),
            "malformed_response",
            None,
        ),
        (
            _FakeHttpResponse(_raw_result_envelope({
                "content": [{"type": "text", "text": "[]"}],
                "isError": False,
            })),
            "malformed_response",
            None,
        ),
        (
            _FakeHttpResponse(_raw_result_envelope({
                "content": [{"type": "text", "text": "{}"}],
                "isError": False,
                "sentinel-extra-result": "private.invalid",
            })),
            "malformed_response",
            None,
        ),
        (
            _FakeHttpResponse(_raw_result_envelope({
                "content": [{"type": "text", "text": "{}"}],
                "structuredContent": [],
                "isError": False,
            })),
            "malformed_response",
            None,
        ),
        (
            _FakeHttpResponse(_raw_result_envelope({
                "content": [{"type": "text", "text": "{}"}],
                "structuredContent": {"sentinel": "private.invalid"},
                "isError": False,
            })),
            "malformed_response",
            None,
        ),
        (
            _FakeHttpResponse(_raw_result_envelope({
                "content": [{
                    "type": "text",
                    "text": "{}",
                    "sentinel-extra-content": "private.invalid",
                }],
                "isError": False,
            })),
            "malformed_response",
            None,
        ),
        (
            _FakeHttpResponse(
                _raw_result_envelope(
                    {
                        "content": [{"type": "text", "text": "{}"}],
                        "isError": False,
                    },
                    sentinel_extra_envelope="private.invalid",
                )
            ),
            "malformed_response",
            None,
        ),
        (
            _FakeHttpResponse(
                {"sentinel": "private.invalid"},
                status_code=503,
                text="sentinel-token private.invalid",
            ),
            "http_status",
            503,
        ),
    ],
    ids=[
        "integer-is-error",
        "string-is-error",
        "non-text-content",
        "missing-content",
        "extra-content",
        "invalid-content",
        "invalid-jsonrpc",
        "missing-jsonrpc",
        "invalid-id",
        "float-id",
        "missing-id",
        "missing-result",
        "invalid-result",
        "non-string-text",
        "non-json-text",
        "non-object-text",
        "extra-result-field",
        "invalid-structured-content",
        "mismatched-structured-content",
        "extra-content-field",
        "extra-envelope-field",
        "non-200-http",
    ],
)
def test_hosted_route_decoder_fails_closed_on_invalid_envelopes(
    response, reason, status
):
    result = _decode_tool_response(response)

    expected = _hosted_route_failure(reason, status=status)
    assert result == expected
    encoded = json.dumps(result) + repr(result)
    for forbidden in (
        "sentinel",
        "private.invalid",
        "raw response",
        "not-json",
    ):
        assert forbidden not in encoded


def test_in_process_transport_records_only_denied_auto_bind():
    route = _DecodedFakeRoute({
        "wiki_bind": _FakeHttpResponse({
            "jsonrpc": "2.0",
            "id": 2,
            "error": {
                "code": -32001,
                "message": "access_denied sentinel-token private.invalid",
                "data": {"hint": "sentinel-private-error-data"},
            },
        }),
    })
    transport = InProcessMcpTransport(route, "docs")

    result = transport.call("wiki_code_publish_begin", {"header": {}})

    expected = [("wiki_bind", {"primary": "docs"})]
    assert result == {
        "error": "remote_mcp_failed",
        "reason": "protocol",
        "hint": "the hosted MCP tool call failed",
    }
    assert transport.calls == expected
    assert route.calls == expected


def test_in_process_transport_records_bind_before_forwarded_begin():
    route = _FakeRoute({
        "wiki_bind": {
            "read": ["docs"],
            "write": ["docs"],
            "primary": "docs",
        },
        "wiki_code_publish_begin": {"session_id": "session-a"},
    })
    transport = InProcessMcpTransport(route, "docs")

    result = transport.call("wiki_code_publish_begin", {"header": {}})

    expected = [
        ("wiki_bind", {"primary": "docs"}),
        ("wiki_code_publish_begin", {"header": {}}),
    ]
    assert result == {"session_id": "session-a"}
    assert transport.calls == expected
    assert route.calls == expected


class HostedMcpCli:
    """One synthetic checkout and its writable hosted MCP session."""

    def __init__(
        self,
        *,
        project,
        client,
        route,
        transport,
        hosted,
        environment,
    ):
        self.project = project
        self.client = client
        self.route = route
        self.transport = transport
        self.hosted = hosted
        self.environment = environment

    def __repr__(self):
        return "<redacted hosted MCP CLI fixture>"

    def run(self):
        stdout = StringIO()
        stderr = StringIO()
        exit_code = admin.run(
            [
                "code",
                "publish",
                "--project",
                str(self.project),
                "--json",
            ],
            stdout=stdout,
            stderr=stderr,
            environ=self.environment,
        )
        return exit_code, stdout.getvalue(), stderr.getvalue()


def _configure_mcp_project(project: Path) -> None:
    config_path = project / ".iwiki.toml"
    config = config_path.read_text(encoding="utf-8")
    config = config.replace(
        'publish_mode = "sqlite"', 'publish_mode = "mcp"'
    ).replace('read_mode = "sqlite"', 'read_mode = "mcp"')
    config = config.replace(
        "max_full_rebuild_seconds = 30",
        "max_batch_rows = 5000\nmax_full_rebuild_seconds = 30",
    )
    config_path.write_text(config, encoding="utf-8")


def _seed_hosted_markdown(clean_postgres) -> None:
    store = PostgresStore(
        clean_postgres,
        "wiki-a",
        _postgres_config(),
        embedder=_embed,
    )
    created = store.write_page(
        "docs",
        "architecture",
        "---\n"
        "type: concept\n"
        "title: Architecture\n"
        "description: Synthetic hosted MCP publication fixture.\n"
        "tags: [fixture]\n"
        "status: stable\n"
        "---\n"
        "# Architecture\n\n"
        "## Service\n\n"
        "Service.run coordinates helper work.\n",
    )
    assert created["page"] == "docs/architecture.md"


@pytest.fixture
def hosted_mcp_cli(
    tmp_path,
    clean_postgres,
    hosted_runtime,
    monkeypatch,
):
    _seed_hosted_markdown(clean_postgres)
    project = create_sqlite_project(tmp_path)
    _configure_mcp_project(project)
    monkeypatch.setattr(
        server,
        "_HOSTED_CODE_GRAPH",
        HostedCodeGraphConfig(
            max_batch_rows=_SERVER_MAX_BATCH_ROWS,
            max_batch_bytes=1_000_000,
        ),
    )
    environment = {
        "IWIKI_CODE_GRAPH_MCP_URL": "http://127.0.0.1:8765/mcp",
        "IWIKI_CODE_GRAPH_MCP_TOKEN": "in-process-only-token",
    }

    with TestClient(
        hosted_runtime.runtime.app,
        base_url="http://127.0.0.1:8765",
    ) as client:
        session_id = _open_session(client, hosted_runtime.token)
        route = _HostedJsonRpcRoute(
            client, hosted_runtime.token, session_id
        )
        transport = InProcessMcpTransport(route, "docs")
        monkeypatch.setattr(
            application,
            "RemoteMcpTransport",
            lambda *, environ, primary: transport,
        )
        yield HostedMcpCli(
            project=project,
            client=client,
            route=route,
            transport=transport,
            hosted=hosted_runtime,
            environment=environment,
        )


def _failure_payload(stdout, stderr):
    payload = json.loads(stdout)
    assert stdout.count("\n") == 1
    assert stderr == ""
    assert payload["state"] == "failed"
    assert payload["publish_mode"] == "mcp"
    assert payload["error"] == "publication_failed"
    return payload


def test_code_publish_cli_activates_snapshot_through_hosted_mcp(
    hosted_mcp_cli,
):
    exit_code, stdout, stderr = hosted_mcp_cli.run()
    payload = json.loads(stdout)
    status = hosted_mcp_cli.transport.call("wiki_code_status", {})
    search = hosted_mcp_cli.transport.call(
        "wiki_code_search", {"query": "Service"}
    )

    assert exit_code == 0
    assert stdout.count("\n") == 1
    assert stdout == json.dumps(payload, separators=(",", ":")) + "\n"
    assert stderr == ""
    assert payload["state"] == "ready"
    assert payload["publish_mode"] == "mcp"
    assert re.fullmatch(
        r"sha256:[0-9a-f]{64}", payload["snapshot_revision"]
    )
    assert status["state"] == "ready"
    assert status["snapshot_revision"] == payload["snapshot_revision"]
    assert search["state"] == "ready"
    assert any(
        result["local_name"] == "Service" for result in search["results"]
    )
    assert hosted_mcp_cli.transport.calls[0][0] == "wiki_bind"
    batch_calls = [
        arguments
        for name, arguments in hosted_mcp_cli.transport.calls
        if name == "wiki_code_publish_batch"
    ]
    assert batch_calls
    assert all(
        len(arguments["rows"]) <= _SERVER_MAX_BATCH_ROWS
        for arguments in batch_calls
    )


def test_read_only_hosted_grant_denies_publication_without_begin_or_abort(
    hosted_mcp_cli,
    monkeypatch,
):
    first_code, first_stdout, first_stderr = hosted_mcp_cli.run()
    old_revision = hosted_mcp_cli.transport.call(
        "wiki_code_status", {}
    )["snapshot_revision"]
    assert first_code == 0
    assert first_stderr == ""
    assert json.loads(first_stdout)["snapshot_revision"] == old_revision

    read_only = hosted_mcp_cli.hosted.auth.create_token(
        "wiki-a",
        "read-only-publisher",
        read_domains=["docs"],
        write_domains=[],
    )["token"]
    session_id = _open_session(hosted_mcp_cli.client, read_only)
    route = _HostedJsonRpcRoute(
        hosted_mcp_cli.client, read_only, session_id
    )
    transport = InProcessMcpTransport(route, "docs")
    monkeypatch.setattr(
        application,
        "RemoteMcpTransport",
        lambda *, environ, primary: transport,
    )

    exit_code, stdout, stderr = hosted_mcp_cli.run()
    payload = _failure_payload(stdout, stderr)
    current = hosted_mcp_cli.transport.call("wiki_code_status", {})

    assert exit_code == 1
    assert current["snapshot_revision"] == old_revision
    assert [name for name, _args in transport.calls] == ["wiki_bind"]
    assert read_only not in stdout + stderr + repr(payload) + repr(transport)


def test_later_batch_failure_preserves_revision_and_aborts_once(
    hosted_mcp_cli,
    monkeypatch,
):
    first_code, first_stdout, first_stderr = hosted_mcp_cli.run()
    old_revision = hosted_mcp_cli.transport.call(
        "wiki_code_status", {}
    )["snapshot_revision"]
    assert first_code == 0
    assert first_stderr == ""
    assert json.loads(first_stdout)["snapshot_revision"] == old_revision

    hosted_mcp_cli.project.joinpath("service.py").write_text(
        "class Service:\n"
        "    def run(self):\n"
        "        return replacement()\n\n\n"
        "def replacement():\n"
        "    return 2\n",
        encoding="utf-8",
    )
    transport = InProcessMcpTransport(
        hosted_mcp_cli.route,
        "docs",
        reject_batch_at=2,
    )
    monkeypatch.setattr(
        application,
        "RemoteMcpTransport",
        lambda *, environ, primary: transport,
    )

    exit_code, stdout, stderr = hosted_mcp_cli.run()
    _failure_payload(stdout, stderr)
    current = hosted_mcp_cli.transport.call("wiki_code_status", {})
    call_names = [name for name, _args in transport.calls]
    attempt_names = [name for name, _args in transport.attempts]

    assert exit_code == 1
    assert current["snapshot_revision"] == old_revision
    assert call_names[0] == "wiki_bind"
    assert call_names.count("wiki_code_publish_begin") == 1
    assert call_names.count("wiki_code_publish_batch") == 1
    assert attempt_names.count("wiki_code_publish_batch") == 2
    assert call_names.count("wiki_code_publish_abort") == 1
    assert call_names.index("wiki_code_publish_begin") < call_names.index(
        "wiki_code_publish_abort"
    )
    assert "wiki_code_publish_finalize" not in call_names
