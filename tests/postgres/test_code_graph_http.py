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
