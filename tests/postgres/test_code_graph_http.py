"""Hosted authorization classification for code-graph publication and reads."""
from __future__ import annotations

import pytest

from iwiki_mcp import http
from iwiki_mcp.postgres.auth import AccessError, AuthContext


PUBLICATION_TOOLS = (
    "wiki_code_publish_begin",
    "wiki_code_publish_inspect",
    "wiki_code_publish_batch",
    "wiki_code_publish_finalize",
    "wiki_code_publish_abort",
)
READ_TOOLS = ("wiki_code_status", "wiki_code_search", "wiki_code_context")


def _context(*, read=("docs",), write=("docs",), primary="docs"):
    return AuthContext(
        iwiki_id="wiki-a",
        token_id="token-a",
        read_domains=read,
        write_domains=write,
        primary=primary,
    )


def _call(name, arguments=None):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }


@pytest.mark.parametrize("name", PUBLICATION_TOOLS)
def test_publication_requires_write_scope_on_the_bound_primary(name):
    http._authorize_tool(_context(), _call(name))

    with pytest.raises(AccessError) as denied:
        http._authorize_tool(
            _context(read=("docs",), write=(), primary=None), _call(name)
        )
    assert denied.value.status_code == 403


@pytest.mark.parametrize("name", READ_TOOLS)
def test_code_reads_require_read_scope_on_the_bound_primary(name):
    http._authorize_tool(_context(write=(), primary="docs"), _call(name))

    with pytest.raises(AccessError) as denied:
        http._authorize_tool(
            _context(read=(), write=(), primary=None), _call(name)
        )
    assert denied.value.status_code == 403


@pytest.mark.parametrize("name", PUBLICATION_TOOLS + READ_TOOLS)
@pytest.mark.parametrize("override", ["iwiki_id", "domain"])
def test_code_tools_refuse_tenant_and_domain_overrides(name, override):
    with pytest.raises(AccessError) as denied:
        http._authorize_tool(_context(), _call(name, {override: "other"}))
    assert denied.value.status_code == 403


def test_publication_uses_the_primary_even_with_other_writable_domains():
    context = _context(
        read=("docs", "private"), write=("docs", "private"), primary="private"
    )

    http._authorize_tool(context, _call("wiki_code_publish_begin"))

    with pytest.raises(AccessError):
        http._authorize_tool(
            context.narrow(
                read_domains=["docs"], write_domains=[], primary=None
            ),
            _call("wiki_code_publish_begin"),
        )


@pytest.mark.postgres_integration
def test_another_writable_token_cannot_reuse_a_session(pg_graph):
    session = pg_graph.begin()
    replacement = pg_graph.reopen_with_new_ephemeral_owner()

    assert replacement.publish_batch(session, pg_graph.batches[0]) == {
        "error": "unauthorized",
        "hint": "this publisher does not own the session",
    }
    assert replacement.finalize(session)["error"] == "unauthorized"
    assert pg_graph.session(session)["state"] == "staging"


EXPECTED_MATCHES = [
    "qualified_exact",
    "local_exact",
    "alias_exact",
    "canonical_prefix",
    "alias_prefix",
    "canonical_lexical",
    "alias_lexical",
    "signature",
    "path",
]


@pytest.mark.postgres_integration
def test_hosted_reads_answer_from_the_active_snapshot(hosted_ready_code):
    from iwiki_mcp import server

    status = server.wiki_code_status()
    assert status["state"] == "ready"
    assert status["fresh"] is True
    assert status["domain"] == hosted_ready_code.graph.domain

    results = server.wiki_code_search("needle", limit=20)["results"]
    assert [item["match"] for item in results] == EXPECTED_MATCHES


@pytest.mark.postgres_integration
def test_hosted_context_never_returns_source(hosted_ready_code):
    from iwiki_mcp import server

    seed = hosted_ready_code.graph.context_request().seeds[0]
    result = server.wiki_code_context(
        [seed], direction="out", depth=1, include_source=True
    )

    assert result["state"] == "ready"
    assert result["source_unavailable"] is True
    assert all("source" not in item for item in result["files"])


@pytest.mark.postgres_integration
def test_hosted_reads_report_missing_before_publication(hosted_empty_code):
    from iwiki_mcp import server

    polled = server.wiki_code_status(job_id="unknown", job_only=True)
    assert polled["warnings"] == ["job_unknown"]
    assert server.wiki_code_status()["state"] == "missing"
    assert server.wiki_code_search("needle")["results"] == []


@pytest.mark.postgres_integration
def test_hosted_search_follows_the_published_snapshot_languages(
    hosted_mixed_language_code,
):
    # The hosted server's project directory holds no .iwiki.toml, so the
    # only authority for the language filter is the snapshot header the
    # client published (python + javascript here).
    from iwiki_mcp import server

    filtered = server.wiki_code_search("needleWidget", languages=["javascript"])
    assert "error" not in filtered
    assert [item["local_name"] for item in filtered["results"]] == ["needleWidget"]

    unfiltered = server.wiki_code_search("needleWidget")
    assert "error" not in unfiltered
    assert [item["local_name"] for item in unfiltered["results"]] == ["needleWidget"]

    assert server.wiki_code_search("needle", languages=["typescript"]) == {
        "error": "language not available in the active snapshot",
        "code": "unsupported_language",
        "hint": "the active snapshot declares: javascript, python",
    }


@pytest.mark.postgres_integration
def test_hosted_python_only_snapshot_keeps_its_language_scope(hosted_ready_code):
    # Regression: a python-only deployment must observe no behaviour
    # change -- same results, and a javascript filter still refused.
    from iwiki_mcp import server

    results = server.wiki_code_search("needle", languages=["python"])["results"]
    assert [item["match"] for item in results] == EXPECTED_MATCHES

    assert server.wiki_code_search("needle", languages=["javascript"]) == {
        "error": "language not available in the active snapshot",
        "code": "unsupported_language",
        "hint": "the active snapshot declares: python",
    }
    assert server.wiki_code_search("needle", languages=["cobol"]) == {
        "error": "code graph configuration is invalid",
        "code": "invalid_config",
        "hint": "inspect code_graph project configuration",
    }


@pytest.mark.postgres_integration
def test_hosted_search_without_snapshot_stays_missing(hosted_empty_code):
    from iwiki_mcp import server

    result = server.wiki_code_search("needle", languages=["python"])

    assert result["state"] == "missing"
    assert result["error"] == "missing_snapshot"
    assert result["results"] == []


@pytest.mark.postgres_integration
def test_hosted_index_without_checkout_is_safe(hosted_empty_code):
    from iwiki_mcp import server

    assert server.wiki_code_index(force=True) == {
        "error": "source_unavailable",
        "hint": (
            "run wiki_code_index on a local MCP server with the repository "
            "checkout"
        ),
    }


@pytest.mark.postgres_integration
def test_hosted_publication_round_trip_activates_one_snapshot(hosted_empty_code):
    from iwiki_mcp import server
    from iwiki_mcp.codegraph.publication import header_payload

    graph = hosted_empty_code.graph
    session = server.wiki_code_publish_begin(header_payload(graph.header))
    assert set(session) >= {"session_id", "lease_expires_at"}

    for batch in graph.batches:
        import json

        accepted = server.wiki_code_publish_batch(
            session["session_id"],
            batch.kind,
            batch.ordinal,
            json.loads(bytes(batch.payload).decode("utf-8")),
            batch.payload_hash,
        )
        assert accepted == {"accepted": True}

    result = server.wiki_code_publish_finalize(session["session_id"])
    assert result["state"] == "ready"
    assert server.wiki_code_status()["snapshot_revision"] == result[
        "snapshot_revision"
    ]


@pytest.mark.postgres_integration
def test_hosted_publication_rejects_a_tampered_batch(hosted_empty_code):
    import json

    from iwiki_mcp import server
    from iwiki_mcp.codegraph.publication import header_payload

    graph = hosted_empty_code.graph
    session = server.wiki_code_publish_begin(header_payload(graph.header))
    batch = graph.batches[0]

    assert server.wiki_code_publish_batch(
        session["session_id"],
        batch.kind,
        batch.ordinal,
        json.loads(bytes(batch.payload).decode("utf-8")),
        "sha256:" + "0" * 64,
    ) == {
        "error": "invalid_batch",
        "hint": "send batches that match the declared header",
    }
    assert server.wiki_code_publish_abort(session["session_id"]) == {
        "state": "aborted"
    }


def _hosted_binding(*, read=("docs",), write=("docs",), primary="docs"):
    from iwiki_mcp import base

    return base.PostgresBinding(
        host="127.0.0.1",
        port=5432,
        database="iwiki_test",
        user="iwiki",
        sslmode="prefer",
        iwiki_id="wiki-a",
        read=read,
        write=write,
        primary=primary,
        project_dir="/not-used",
        embed_model="fixture-model",
        embed_dimensions=3,
        rerank_model="",
        password="secret",
    )


class _FakeReader:
    """Answer like the hosted PostgreSQL reader without touching a database."""

    def __init__(self, binding):
        self.domain = binding.primary
        self.calls = 0

    def _answer(self):
        self.calls += 1
        return {
            "domain": self.domain,
            "state": "ready",
            "fresh": True,
            "counts": {"files": 3, "symbols": 7},
        }

    def status(self):
        return self._answer()

    def search(self, _validate):
        return {**self._answer(), "results": [], "warnings": []}

    def context(self, _request):
        return {**self._answer(), "nodes": [], "warnings": ["unknown_seed"]}


@pytest.fixture
def hosted_session(monkeypatch):
    """Install one hosted session state around a fake snapshot reader."""
    from iwiki_mcp import server

    readers = []
    tokens = []

    def factory(source, *, binding=None, session_id="session-a"):
        selected = server._HostedSelectedState(
            binding or _hosted_binding(), source=source
        )
        state = server._HostedBindingState(selected, selected.get())
        state.bind_session(session_id)
        tokens.append(server._SESSION_BINDING.set(state))

        def reader(bind):
            readers.append(_FakeReader(bind))
            return readers[-1]

        monkeypatch.setattr(server, "_postgres_code_reader", reader)
        return state

    factory.readers = readers
    try:
        yield factory
    finally:
        for token in reversed(tokens):
            server._SESSION_BINDING.reset(token)


def test_domain_free_code_read_reports_a_defaulted_binding(hosted_session):
    from iwiki_mcp import server

    hosted_session("token_default")

    answer = server.wiki_code_search("parse")

    assert answer["binding_source"] == "token_default"
    assert "binding_defaulted" in answer["warnings"]
    assert answer["domain"] == "docs"


def test_domain_free_code_read_reports_a_selected_binding(hosted_session):
    from iwiki_mcp import server

    hosted_session("session")

    answer = server.wiki_code_search("parse")

    assert answer["binding_source"] == "session"
    assert "binding_defaulted" not in answer["warnings"]


def test_status_and_context_carry_the_same_binding_source(hosted_session):
    from iwiki_mcp import server

    hosted_session("token_default")

    status = server.wiki_code_status()
    context = server.wiki_code_context(["py:module:" + "a" * 64])

    assert status["binding_source"] == "token_default"
    assert status["warnings"] == ["binding_defaulted"]
    assert context["binding_source"] == "token_default"
    assert context["warnings"] == ["unknown_seed", "binding_defaulted"]


def test_a_lost_session_answers_as_a_token_default(monkeypatch):
    from iwiki_mcp import http, server
    from iwiki_mcp.postgres.auth import AuthContext

    context = AuthContext("wiki-a", "token-a", ("docs",), ("docs",), "docs")
    sessions = http._SessionBindings()
    selected = server._HostedSelectedState(
        _hosted_binding(primary="framework"), source="session"
    )
    bound = server._HostedBindingState(selected, selected.get())
    sessions.store("session-a", context, bound)

    restarted = http._SessionBindings()
    assert restarted.resolve("session-a", context) is None

    fallback = server._HostedBindingState(
        server._HostedSelectedState(_hosted_binding(), source="token_default")
    )
    monkeypatch.setattr(
        server, "_postgres_code_reader", lambda bind: _FakeReader(bind)
    )
    token = server._SESSION_BINDING.set(fallback)
    try:
        answer = server.wiki_code_search("parse")
    finally:
        server._SESSION_BINDING.reset(token)

    assert answer["binding_source"] == "token_default"
    assert answer["domain"] == "docs"


def test_fail_closed_option_refuses_a_defaulted_code_read(
    hosted_session, monkeypatch
):
    from iwiki_mcp import server
    from iwiki_mcp.postgres.config import HostedCodeGraphConfig

    state = hosted_session("token_default")
    monkeypatch.setattr(
        server,
        "_HOSTED_CODE_GRAPH",
        HostedCodeGraphConfig(require_session_binding=True),
    )
    refusal = {
        "error": "binding_not_selected",
        "hint": "call wiki_bind to select a primary domain for this session",
    }

    assert server.wiki_code_status() == refusal
    assert server.wiki_code_status(job_id="unknown", job_only=True) == refusal
    assert server.wiki_code_search("parse") == refusal
    assert server.wiki_code_context(["py:module:" + "a" * 64]) == refusal
    assert hosted_session.readers == []

    state.selected_state().set(_hosted_binding())
    assert server.wiki_code_search("parse")["binding_source"] == "session"


def test_wiki_status_and_bind_report_the_session_binding(
    hosted_session, monkeypatch
):
    from iwiki_mcp import server

    hosted_session("token_default")

    class _Store:
        def list_domains(self):
            return ["docs"]

    monkeypatch.setattr(
        server, "_postgres_store_for_binding", lambda bind: _Store()
    )
    monkeypatch.setattr(
        server,
        "_specification_status_domain",
        lambda bind, domain: {"domain": domain, "mode": "optional"},
    )

    status = server.wiki_status()
    assert status["binding_source"] == "token_default"

    bound = server.wiki_bind(read=["docs"], write=["docs"], primary="docs")
    assert bound["binding_source"] == "session"
    assert bound["session_id"] == "session-a"
    assert server.wiki_status()["binding_source"] == "session"


@pytest.mark.parametrize(("source", "warnings"), [
    ("session", ["job_unknown"]),
    ("token_default", ["job_unknown", "binding_defaulted"]),
])
def test_hosted_job_only_reports_provenance_without_a_reader(hosted_session, source, warnings):
    from iwiki_mcp import server

    hosted_session(source)
    assert server.wiki_code_status(job_id="unknown", job_only=True) == {
        "binding_source": source, "warnings": warnings,
    }
    assert hosted_session.readers == []


@pytest.mark.postgres_integration
def test_hosted_job_only_uses_the_selected_http_session(hosted_runtime):
    from starlette.testclient import TestClient
    from tests.postgres.test_code_graph_contract import _McpRoute, _open_session

    with TestClient(hosted_runtime.runtime.app, base_url="http://127.0.0.1:8765") as client:
        session = _open_session(client, hosted_runtime.token)
        route = _McpRoute(client, hosted_runtime.token, session)
        assert route.call("wiki_bind", {"primary": "docs"})["binding_source"] == "session"
        assert route.call("wiki_code_status", {"job_only": True, "job_id": "unknown"}) == {
            "binding_source": "session", "warnings": ["job_unknown"],
        }


@pytest.mark.postgres_integration
def test_resume_http_requires_selected_session_and_returns_stable_receipts(
    hosted_ranked_runtime, hosted_runtime, monkeypatch,
):
    import time
    from uuid import uuid4

    from starlette.testclient import TestClient
    from iwiki_mcp.codegraph.publication import header_payload, iter_snapshot_batches
    from iwiki_mcp.postgres.codegraph import PostgresCodeGraphStore
    from tests.postgres.test_code_graph_contract import _McpRoute, _open_session

    monkeypatch.setattr(PostgresCodeGraphStore, "_RECEIPT_PAGE_SIZE", 2)
    graph = hosted_ranked_runtime.graph
    begin = {
        "header": header_payload(graph.header), "attempt_key": uuid4().hex,
        "recovery_deadline": int(time.time()) + 1800,
    }
    batches = list(iter_snapshot_batches(graph.rows, max_rows=1, max_bytes=100_000))
    partial = [b for b in batches if (b.kind, b.ordinal) in {
        ("repositories", 0), ("files", 0), ("files", 2),
    }]
    assert len(partial) == 3
    other_token = hosted_runtime.auth.create_token(
        "wiki-a", "other", read_domains=["docs"], write_domains=["docs"],
    )["token"]
    with TestClient(hosted_runtime.runtime.app, base_url="http://127.0.0.1:8765") as client:
        route = _McpRoute(client, hosted_runtime.token, _open_session(client, hosted_runtime.token))
        other = _McpRoute(client, other_token, _open_session(client, other_token))
        assert route.call("wiki_code_publish_begin", begin)["error"] == "binding_not_selected"
        route.call("wiki_bind", {"primary": "docs"})
        started = route.call("wiki_code_publish_begin", begin)
        assert "session_id" in started, started
        assert route.call("wiki_code_publish_begin", begin) == started
        fallback = _McpRoute(
            client, hosted_runtime.token, _open_session(client, hosted_runtime.token),
        )
        assert fallback.status()["binding_source"] == "token_default"
        refused = [
            fallback.publish_batch(started, batches[0]), fallback.finalize(started),
            fallback.abort(started),
        ]
        assert [reply.get("error") for reply in refused] == ["binding_not_selected"] * 3
        legacy = fallback.begin(graph.header)
        assert "error" not in fallback.publish_batch(legacy, batches[0])
        assert fallback.abort(legacy)["state"] == "aborted"
        args = {"session_id": started["session_id"]}
        empty = route.call("wiki_code_publish_inspect", args)
        assert empty["state"] == "staging"
        assert empty["binding_source"] == "session"
        assert empty["receipts"] == [] and empty["next_cursor"] is None
        assert empty["lease_expires_at"] == started["lease_expires_at"]

        assert other.call("wiki_code_publish_inspect", args)["error"] == "binding_not_selected"
        guarded = {**args, "require_session_binding": True}
        assert other.call("wiki_code_publish_finalize", guarded)["error"] == "binding_not_selected"
        other.call("wiki_bind", {"primary": "docs"})
        assert other.call("wiki_code_publish_inspect", args)["error"] == "unauthorized"

        for batch in partial[:2]:
            assert "error" not in route.publish_batch(started, batch)
        exact = route.call("wiki_code_publish_inspect", args)
        assert len(exact["receipts"]) == 2 and exact["next_cursor"] is None
        assert "error" not in route.publish_batch(started, partial[2])
        page = route.call("wiki_code_publish_inspect", args)
        last = route.call("wiki_code_publish_inspect", {**args, "cursor": page["next_cursor"]})
        assert len(page["receipts"]) == 2 and len(last["receipts"]) == 1
        assert last["next_cursor"] is None and last["lease_expires_at"] == page["lease_expires_at"]
        assert page["receipts"] + last["receipts"] == [
            {"kind": b.kind, "ordinal": b.ordinal, "payload_hash": b.payload_hash}
            for b in sorted(partial, key=lambda b: (b.kind, b.ordinal))
        ]
        assert route.call("wiki_code_publish_inspect", {**args, "cursor": "wrong"})[
            "error"
        ] == "invalid_cursor"
        for batch in batches:
            if batch not in partial:
                assert "error" not in route.publish_batch(started, batch)
        complete = route.call("wiki_code_publish_finalize", guarded)
        assert complete["state"] == "ready"
        terminal = route.call("wiki_code_publish_inspect", {**args, "cursor": page["next_cursor"]})
        assert terminal["state"] == "ready"
        assert terminal["snapshot_revision"] == complete["snapshot_revision"]
        assert "receipts" not in terminal


@pytest.mark.postgres_integration
def test_global_quota_refusal_discloses_only_the_resource_and_allows_retry(
    hosted_ranked_runtime, hosted_runtime, clean_postgres, monkeypatch,
):
    from dataclasses import replace
    import os

    from starlette.testclient import TestClient
    from iwiki_mcp import server
    from iwiki_mcp.codegraph.publication import iter_snapshot_batches
    from iwiki_mcp.postgres.publication_roles import provision_publication_pair
    from tests.postgres.test_code_graph_contract import _McpRoute, _open_session

    settings = replace(server._HOSTED_CODE_GRAPH, max_server_sessions=1)
    provision_publication_pair(
        clean_postgres, hosted_runtime.runtime.config.storage.user,
        os.environ["IWIKI_PUBLICATION_DB_USER"], settings.publication_limits,
    )
    monkeypatch.setattr(server, "_HOSTED_CODE_GRAPH", settings)
    hidden = hosted_runtime.auth.create_token(
        "wiki-a", "unrelated-owner", read_domains=["private"], write_domains=["private"],
    )
    header = hosted_ranked_runtime.graph.header
    with TestClient(hosted_runtime.runtime.app, base_url="http://127.0.0.1:8765") as client:
        other = _McpRoute(client, hidden["token"], _open_session(client, hidden["token"]))
        caller = _McpRoute(
            client, hosted_runtime.token, _open_session(client, hosted_runtime.token),
        )
        other.call("wiki_bind", {"primary": "private"})
        caller.call("wiki_bind", {"primary": "docs"})
        holding = other.begin(replace(header, repository_id="private"))
        assert "session_id" in holding

        refusal = caller.begin(header)
        assert refusal["error"] == "publication_quota_exceeded"
        assert refusal["resource"] == "sessions"
        assert set(refusal) <= {"error", "code", "resource", "hint", "binding_source"}
        for private in ("private", "unrelated-owner", hidden["token_id"], holding["session_id"]):
            assert private not in repr(refusal)
        batches = iter_snapshot_batches(hosted_ranked_runtime.graph.rows, max_rows=1,
                                        max_bytes=100_000)
        batch = next(batches)
        assert caller.publish_batch(holding, batch)["error"] == "unauthorized"
        assert other.abort(holding)["state"] == "aborted"
        assert "session_id" in caller.begin(header)
