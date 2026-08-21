"""Hosted authorization classification for code-graph publication and reads."""
from __future__ import annotations

import pytest

from iwiki_mcp import http
from iwiki_mcp.postgres.auth import AccessError, AuthContext


PUBLICATION_TOOLS = (
    "wiki_code_publish_begin",
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
