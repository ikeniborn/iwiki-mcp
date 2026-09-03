"""Offline unit checks for hosted HTTP boundary helpers."""
from dataclasses import replace

import pytest


def _binding(*, read=("docs",), write=("docs",), primary="docs"):
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


def test_session_binding_registry_expires_abandoned_records(monkeypatch):
    from iwiki_mcp import http, server
    from iwiki_mcp.postgres.auth import AuthContext

    now = 100.0
    monkeypatch.setattr(http.time, "monotonic", lambda: now)
    sessions = http._SessionBindings()
    context = AuthContext("wiki-a", "token-a", ("docs",), (), None)
    selected = server._HostedSelectedState(_binding())
    state = server._HostedBindingState(selected, selected.get())
    sessions.store("session-a", context, state)

    now += http._SESSION_IDLE_SECONDS + 1
    assert sessions.resolve("session-b", context) is None
    assert sessions.resolve("session-a", context) is None


def test_request_scope_intersection_does_not_mutate_selected_session():
    from iwiki_mcp import http, server
    from iwiki_mcp.postgres.auth import AuthContext

    selected = server._HostedSelectedState(
        _binding(
            read=("docs", "private"),
            write=("docs", "private"),
            primary="private",
        )
    )
    sessions = http._SessionBindings()
    original = AuthContext(
        "wiki-a",
        "token-a",
        ("docs", "private"),
        ("docs", "private"),
        "private",
    )
    initial = server._HostedBindingState(selected, selected.get())
    sessions.store("session-a", original, initial)

    revoked = AuthContext(
        "wiki-a", "token-a", ("docs",), ("docs",), "docs"
    )
    persisted = sessions.resolve("session-a", revoked)
    current = http._effective_binding(
        persisted.selected_state().get(), revoked
    )
    persisted.set_effective(current, revoked)

    assert persisted.get().read == ("docs",)
    assert persisted.get().write == ("docs",)
    assert persisted.get().primary == "docs"
    assert persisted.selected_state().get().read == ("docs", "private")
    assert persisted.selected_state().get().write == ("docs", "private")
    assert persisted.selected_state().get().primary == "private"

    restored_and_expanded = AuthContext(
        "wiki-a",
        "token-a",
        ("docs", "private", "new"),
        ("docs", "private", "new"),
        "docs",
    )
    current = http._effective_binding(
        persisted.selected_state().get(), restored_and_expanded
    )
    persisted.set_effective(current, restored_and_expanded)
    assert persisted.get().read == ("docs", "private")
    assert "new" not in persisted.get().read

    narrowed = replace(
        persisted.get(), read=("docs",), write=("docs",), primary="docs"
    )
    persisted.set(narrowed)
    assert persisted.selected_state().get().read == ("docs",)


async def test_middleware_installs_full_context_and_persists_request_session():
    from iwiki_mcp import http, server
    from iwiki_mcp.postgres.auth import AuthContext
    from iwiki_mcp.postgres.config import (
        HostedServerConfig,
        ModelConfig,
        PostgresConfig,
        ServerConfig,
    )

    context = AuthContext(
        "wiki-a",
        "token-a",
        ("docs",),
        ("docs",),
        "docs",
        can_create_domain=True,
        managed_domains=("private",),
    )

    class Auth:
        def authenticate(self, token):
            assert token == "opaque"
            return context

    seen = {}

    async def app(_scope, _receive, send):
        seen["auth"] = server._AUTH_CONTEXT.get()
        seen["state"] = server._SESSION_BINDING.get()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})

    config = ServerConfig(
        storage=PostgresConfig(
            host="127.0.0.1",
            port=5432,
            database="iwiki_test",
            user="iwiki",
            sslmode="prefer",
            password="secret",
        ),
        models=ModelConfig("fixture-model", 3, ""),
        server=HostedServerConfig(
            "127.0.0.1", 8765, (), 1, 2, 30_000, 5_000
        ),
    )
    middleware = http.AuthenticatedMCPMiddleware(
        app,
        config=config,
        auth_store=Auth(),
        project_dir="/not-used",
    )
    body = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}'
    messages = iter(
        [{"type": "http.request", "body": body, "more_body": False}]
    )
    sent = []

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    await middleware(
        {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [
                (b"authorization", b"Bearer opaque"),
                (b"mcp-session-id", b"session-a"),
            ],
        },
        receive,
        send,
    )

    assert seen["auth"] == context
    assert seen["state"].get().read == ("docs",)
    assert middleware.sessions.resolve("session-a", context) is not None
    assert "opaque" not in repr(middleware.sessions._records)
    assert server._AUTH_CONTEXT.get() is None
    assert server._SESSION_BINDING.get() is None
    assert sent[0]["status"] == 200


@pytest.mark.parametrize(
    "payload",
    [
        {"method": "tools/call", "params": []},
        {
            "method": "tools/call",
            "params": {"name": "wiki_create_domain"},
        },
        {
            "method": "tools/call",
            "params": {
                "name": "wiki_create_domain",
                "arguments": [],
            },
        },
        {
            "method": "tools/call",
            "params": {
                "name": "wiki_list_domain_grants",
                "arguments": {},
            },
        },
    ],
)
def test_protected_tool_envelopes_fail_closed(payload):
    from iwiki_mcp import http
    from iwiki_mcp.postgres.auth import AccessError, AuthContext

    context = AuthContext("wiki-a", "token-a", ("docs",), (), None)

    with pytest.raises(AccessError) as error:
        http._authorize_tool(context, payload)

    assert error.value.status_code == 403


def test_protected_tools_require_their_distinct_capabilities():
    from iwiki_mcp import http
    from iwiki_mcp.postgres.auth import AccessError, AuthContext

    context = AuthContext("wiki-a", "token-a", ("docs",), (), None)
    create = {
        "method": "tools/call",
        "params": {
            "name": "wiki_create_domain",
            "arguments": {"name": "new-project"},
        },
    }
    manage = {
        "method": "tools/call",
        "params": {
            "name": "wiki_set_domain_grant",
            "arguments": {
                "domain": "docs",
                "token_id": "target",
                "can_read": True,
                "can_write": False,
            },
        },
    }

    with pytest.raises(AccessError):
        http._authorize_tool(context, create)
    with pytest.raises(AccessError):
        http._authorize_tool(context, manage)

    creator = replace(context, can_create_domain=True)
    manager = replace(context, managed_domains=("docs",))
    http._authorize_tool(creator, create)
    http._authorize_tool(manager, manage)


def test_protected_authorization_rejects_tenant_override_but_not_validation():
    from iwiki_mcp import http
    from iwiki_mcp.postgres.auth import AccessError, AuthContext

    context = AuthContext(
        "wiki-a",
        "token-a",
        (),
        (),
        None,
        can_create_domain=True,
        managed_domains=("docs",),
    )
    create = {
        "method": "tools/call",
        "params": {
            "name": "wiki_create_domain",
            "arguments": {"name": "bad/name"},
        },
    }
    invalid_grant = {
        "method": "tools/call",
        "params": {
            "name": "wiki_set_domain_grant",
            "arguments": {
                "domain": "bad/name",
                "token_id": "target",
                "can_read": False,
                "can_write": True,
            },
        },
    }
    override = {
        "method": "tools/call",
        "params": {
            "name": "wiki_set_domain_grant",
            "arguments": {
                **invalid_grant["params"]["arguments"],
                "iwiki_id": "wiki-b",
            },
        },
    }

    http._authorize_tool(context, create)
    http._authorize_tool(context, invalid_grant)
    with pytest.raises(AccessError):
        http._authorize_tool(context, override)

    http._authorize_tool(
        context,
        {"method": "tools/call", "params": {"name": "unknown"}},
    )


def _server_config():
    from iwiki_mcp.postgres.config import (
        HostedServerConfig,
        ModelConfig,
        PostgresConfig,
        ServerConfig,
    )

    return ServerConfig(
        storage=PostgresConfig(
            host="127.0.0.1",
            port=5432,
            database="iwiki_test",
            user="iwiki",
            sslmode="prefer",
            password="secret",
        ),
        models=ModelConfig("fixture-model", 3, ""),
        server=HostedServerConfig("127.0.0.1", 8765, (), 1, 2, 30_000, 5_000),
    )


async def _dispatch(middleware, session_id, seen):
    body = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}'
    messages = iter(
        [{"type": "http.request", "body": body, "more_body": False}]
    )

    async def receive():
        return next(messages)

    async def send(_message):
        return None

    await middleware(
        {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [
                (b"authorization", b"Bearer opaque"),
                (b"mcp-session-id", session_id.encode("latin-1")),
            ],
        },
        receive,
        send,
    )
    return seen


async def test_middleware_reports_a_defaulted_binding_and_its_session():
    from iwiki_mcp import http, server
    from iwiki_mcp.postgres.auth import AuthContext

    context = AuthContext(
        "wiki-a", "token-a", ("docs",), ("docs",), "docs"
    )

    class Auth:
        def authenticate(self, token):
            return context

    seen = {}

    async def app(_scope, _receive, send):
        state = server._SESSION_BINDING.get()
        seen["source"] = state.binding_source()
        seen["session_id"] = state.session_id()
        seen["substituted"] = state.primary_substituted()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})

    middleware = http.AuthenticatedMCPMiddleware(
        app,
        config=_server_config(),
        auth_store=Auth(),
        project_dir="/not-used",
    )

    await _dispatch(middleware, "session-a", seen)
    assert seen == {
        "source": "token_default",
        "session_id": "session-a",
        "substituted": False,
    }

    # An explicit selection survives inside the session and is reported as one.
    persisted = middleware.sessions.resolve("session-a", context)
    persisted.set(replace(persisted.get(), primary="docs"))
    await _dispatch(middleware, "session-a", seen)
    assert seen["source"] == "session"

    # A different session id never inherits that selection.
    await _dispatch(middleware, "session-b", seen)
    assert seen["source"] == "token_default"
    assert seen["session_id"] == "session-b"


async def test_middleware_reports_a_primary_substituted_by_the_write_scope():
    from iwiki_mcp import http, server
    from iwiki_mcp.postgres.auth import AuthContext

    context = AuthContext(
        "wiki-a", "token-a", ("docs",), ("docs",), "docs"
    )

    class Auth:
        def authenticate(self, token):
            return context

    seen = {}

    async def app(_scope, _receive, send):
        state = server._SESSION_BINDING.get()
        seen["substituted"] = state.primary_substituted()
        seen["requested"] = state.requested_primary()
        seen["primary"] = state.get().primary
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})

    middleware = http.AuthenticatedMCPMiddleware(
        app,
        config=_server_config(),
        auth_store=Auth(),
        project_dir="/not-used",
    )
    selected = server._HostedSelectedState(
        _binding(
            read=("docs", "private"),
            write=("private", "docs"),
            primary="private",
        )
    )
    middleware.sessions.store(
        "session-a",
        context,
        server._HostedBindingState(selected, selected.get()),
    )

    await _dispatch(middleware, "session-a", seen)

    assert seen == {
        "substituted": True,
        "requested": "private",
        "primary": "docs",
    }


def _refresh_call(domain):
    return {
        "method": "tools/call",
        "params": {
            "name": "wiki_code_refresh_links",
            "arguments": {"domain": domain},
        },
    }


def test_refresh_links_authorizes_the_domain_it_names():
    """The transport checks the named domain, like every other write tool.

    It shipped in none of the authorization sets, so `_authorize_tool` matched
    no branch and authorized nothing; only the handler's own binding check
    stood between a caller and another domain's links.
    """
    from iwiki_mcp import http
    from iwiki_mcp.postgres.auth import AccessError, AuthContext

    context = AuthContext("wiki-a", "token-a", ("docs", "other"), ("docs",), "docs")

    http._authorize_tool(context, _refresh_call("docs"))

    with pytest.raises(AccessError) as error:
        http._authorize_tool(context, _refresh_call("other"))
    assert error.value.status_code == 403


def test_refresh_links_without_a_domain_falls_back_to_the_primary():
    from iwiki_mcp import http
    from iwiki_mcp.postgres.auth import AccessError, AuthContext

    readable = AuthContext("wiki-a", "token-a", ("docs",), (), "docs")
    call = {
        "method": "tools/call",
        "params": {"name": "wiki_code_refresh_links", "arguments": {}},
    }

    with pytest.raises(AccessError) as error:
        http._authorize_tool(readable, call)
    assert error.value.status_code == 403
