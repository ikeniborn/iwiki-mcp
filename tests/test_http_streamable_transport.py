"""Hosted Streamable HTTP transport integration tests."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.transport_security import TransportSecuritySettings
import pytest

from iwiki_mcp import server
from iwiki_mcp.http import AuthenticatedMCPMiddleware
from iwiki_mcp.postgres.auth import AuthContext


_TOKEN = "fixture-token"
_BASE_URL = "http://127.0.0.1:8765"


class _FixtureAuthStore:
    def authenticate(self, token):
        if token != _TOKEN:
            return None
        return AuthContext(
            iwiki_id="fixture-wiki",
            token_id="fixture-token-id",
            read_domains=("docs",),
            write_domains=("docs",),
            primary="docs",
        )


@pytest.fixture
def stateful_hosted_app(tmp_path):
    original_json_response = server.mcp.settings.json_response
    original_stateless_http = server.mcp.settings.stateless_http
    original_transport_security = server.mcp.settings.transport_security
    server.mcp._session_manager = None
    server.mcp.settings.json_response = True
    server.mcp.settings.stateless_http = False
    server.mcp.settings.transport_security = TransportSecuritySettings(
        allowed_hosts=["127.0.0.1:*"],
        allowed_origins=[],
    )
    inner_app = server.mcp.streamable_http_app()
    config = SimpleNamespace(
        server=SimpleNamespace(allowed_origins=[]),
        storage=SimpleNamespace(
            host="localhost",
            port=5432,
            database="fixture",
            user="fixture",
            sslmode="disable",
            password="fixture",
        ),
        models=SimpleNamespace(
            embed_model="fixture",
            embed_dimensions=3,
            rerank_model="",
        ),
    )
    app = AuthenticatedMCPMiddleware(
        inner_app,
        config=config,
        auth_store=_FixtureAuthStore(),
        project_dir=str(tmp_path),
    )
    yield app, inner_app
    server.mcp._session_manager = None
    server.mcp.settings.json_response = original_json_response
    server.mcp.settings.stateless_http = original_stateless_http
    server.mcp.settings.transport_security = original_transport_security


async def test_official_client_lists_tools_within_five_seconds(
    stateful_hosted_app,
):
    app, inner_app = stateful_hosted_app
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {_TOKEN}"}

    async with inner_app.router.lifespan_context(inner_app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url=_BASE_URL,
            headers=headers,
        ) as http_client:
            started = anyio.current_time()
            with anyio.fail_after(5):
                async with streamable_http_client(
                    f"{_BASE_URL}/mcp",
                    http_client=http_client,
                    terminate_on_close=False,
                ) as (read_stream, write_stream, _):
                    async with ClientSession(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=timedelta(seconds=5),
                    ) as session:
                        await session.initialize()
                        tools = await session.list_tools()

            assert any(tool.name == "wiki_status" for tool in tools.tools)
            assert anyio.current_time() - started <= 5


async def test_authenticated_get_with_valid_session_returns_405_quickly(
    stateful_hosted_app,
):
    app, inner_app = stateful_hosted_app
    transport = httpx.ASGITransport(app=app)
    auth_headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {_TOKEN}",
        "Content-Type": "application/json",
    }

    async with inner_app.router.lifespan_context(inner_app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url=_BASE_URL,
        ) as client:
            unauthenticated = await client.get("/mcp")
            assert unauthenticated.status_code == 401

            initialized = await client.post(
                "/mcp",
                headers=auth_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {
                            "name": "integration-test",
                            "version": "1",
                        },
                    },
                },
            )
            assert initialized.status_code == 200
            session_id = initialized.headers["mcp-session-id"]
            notified = await client.post(
                "/mcp",
                headers={**auth_headers, "Mcp-Session-Id": session_id},
                json={
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                },
            )
            assert notified.status_code == 202

            started = anyio.current_time()
            with anyio.fail_after(5):
                response = await client.get(
                    "/mcp",
                    headers={
                        "Accept": "text/event-stream",
                        "Authorization": f"Bearer {_TOKEN}",
                        "Mcp-Session-Id": session_id,
                    },
                )

            assert response.status_code == 405
            assert response.headers["allow"] == "POST, DELETE"
            assert anyio.current_time() - started <= 5
