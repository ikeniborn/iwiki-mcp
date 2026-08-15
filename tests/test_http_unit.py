"""Offline unit checks for hosted HTTP boundary helpers."""
from dataclasses import replace


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
