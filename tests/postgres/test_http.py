"""Authenticated Streamable HTTP contract for hosted PostgreSQL mode."""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient


pytestmark = pytest.mark.postgres_integration


class RedactedEnv(dict):
    """Environment mapping whose pytest representation never exposes secrets."""

    def __repr__(self):
        return "<redacted HTTP environment>"


class HostedFixture:
    """HTTP fixture whose pytest representation never reveals Bearer tokens."""

    def __init__(self, runtime, auth, token, revoked, disabled):
        self.runtime = runtime
        self.auth = auth
        self.token = token
        self.revoked = revoked
        self.disabled = disabled

    def __repr__(self):
        return "<redacted hosted HTTP fixture>"


def _write_server_config(path, dsn_values, *, storage_type="postgres"):
    if storage_type == "postgres":
        storage = (
            "[storage]\n"
            "type = \"postgres\"\n"
            f"host = {json.dumps(dsn_values['host'])}\n"
            f"port = {int(dsn_values.get('port', 5432))}\n"
            f"database = {json.dumps(dsn_values['dbname'])}\n"
            f"user = {json.dumps(dsn_values['user'])}\n"
            f"sslmode = {json.dumps(dsn_values.get('sslmode', 'prefer'))}\n"
        )
    else:
        storage = "[storage]\ntype = \"git\"\n"
    path.write_text(
        storage
        + "\n[server]\n"
        "host = \"127.0.0.1\"\n"
        "port = 8765\n"
        "allowed_origins = [\"https://iwiki.example\"]\n"
        "pool_min_size = 1\n"
        "pool_max_size = 2\n"
        "statement_timeout_ms = 30000\n"
        "lock_timeout_ms = 5000\n",
        encoding="utf-8",
    )


@pytest.fixture
def hosted_runtime(clean_postgres, tmp_path, monkeypatch):
    from psycopg.conninfo import conninfo_to_dict

    from iwiki_mcp import http, server
    from iwiki_mcp.postgres.auth import AuthStore

    values = conninfo_to_dict(clean_postgres)
    config_path = tmp_path / "server.toml"
    _write_server_config(config_path, values)
    environ = RedactedEnv(
        {
            "IWIKI_DB_PASSWORD": values["password"],
            "IWIKI_LLM_BASE_URL": "http://provider.internal/v1",
            "IWIKI_LLM_KEY": "server-only-model-key",
            "IWIKI_EMBED_MODEL": "fixture-model",
            "IWIKI_EMBED_DIMENSIONS": "3",
            "IWIKI_RERANK_MODEL": "",
        }
    )
    for name, value in environ.items():
        monkeypatch.setenv(name, value)

    server.mcp._session_manager = None
    runtime = http.prepare_runtime(
        str(config_path), environ=environ, probe=lambda _cfg: None
    )
    auth = AuthStore(runtime.dsn)
    auth.create_wiki("wiki-a", "wiki-a")
    auth.create_domain("wiki-a", "docs")
    auth.create_domain("wiki-a", "private")
    token = auth.create_token(
        "wiki-a",
        "alice",
        read_domains=["docs"],
        write_domains=["docs"],
    )["token"]
    revoked = auth.create_token(
        "wiki-a",
        "revoked",
        read_domains=["docs"],
        write_domains=[],
    )
    auth.revoke_token(revoked["token_id"])
    disabled = auth.create_token(
        "wiki-a",
        "disabled",
        read_domains=["docs"],
        write_domains=[],
    )["token"]

    yield HostedFixture(runtime, auth, token, revoked["token"], disabled)

    auth.set_wiki_active("wiki-a", True)
    runtime.close()
    server.mcp._session_manager = None


def _request(client, token, payload, *, origin=None, session_id=None):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if origin is not None:
        headers["Origin"] = origin
    if session_id is not None:
        headers["Mcp-Session-Id"] = session_id
    return client.post("/mcp", headers=headers, json=payload)


def _initialize(client, token, *, origin=None, client_name="integration-test"):
    return _request(
        client,
        token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": client_name, "version": "1"},
            },
        },
        origin=origin,
    )


def _tool_call(
    client, token, name, arguments, *, origin=None, session_id=None
):
    return _request(
        client,
        token,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        origin=origin,
        session_id=session_id,
    )


def test_streamable_http_auth_origin_acl_and_pool_contract(hosted_runtime):
    runtime = hosted_runtime.runtime
    auth = hosted_runtime.auth
    token = hosted_runtime.token
    revoked = hosted_runtime.revoked
    disabled = hosted_runtime.disabled
    with TestClient(
        runtime.app, base_url="http://127.0.0.1:8765"
    ) as client:
        authorized = _initialize(
            client,
            token,
            origin="https://iwiki.example",
            client_name=f"integration-test Authorization Bearer {token}",
        )
        assert authorized.status_code == 200
        assert token not in authorized.text
        assert authorized.json()["result"]["serverInfo"]["name"] == "iwiki"
        session_id = authorized.headers["mcp-session-id"]
        initialized = _request(
            client,
            token,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session_id=session_id,
        )
        assert initialized.status_code == 202

        wrong_token_session = _tool_call(
            client,
            disabled,
            "wiki_list_domains",
            {},
            session_id=session_id,
        )
        unknown_session = _tool_call(
            client,
            disabled,
            "wiki_list_domains",
            {},
            session_id="0" * 32,
        )
        assert wrong_token_session.status_code == 404
        assert wrong_token_session.json() == unknown_session.json()
        assert "wiki-a" not in wrong_token_session.text

        absent_origin = _initialize(client, token)
        assert absent_origin.status_code == 200

        domains = _tool_call(
            client,
            token,
            "wiki_list_domains",
            {},
            session_id=session_id,
        )
        assert domains.status_code == 200
        tool_result = json.loads(
            domains.json()["result"]["content"][0]["text"]
        )
        assert tool_result["domains"] == ["docs"]

        status = _tool_call(
            client,
            token,
            "wiki_status",
            {},
            session_id=session_id,
        )
        assert status.status_code == 200
        status_result = json.loads(
            status.json()["result"]["content"][0]["text"]
        )
        assert status_result == {
            "storage": "postgres",
            "transport": "streamable-http",
            "read": ["docs"],
            "write": ["docs"],
            "primary": "docs",
            "domains": ["docs"],
        }
        assert runtime.config.storage.password not in status.text
        assert "server-only-model-key" not in status.text

        denied = _tool_call(
            client,
            token,
            "wiki_read_page",
            {"domain": "private", "slug": "hidden"},
            session_id=session_id,
        )
        assert denied.status_code == 403
        assert denied.json() == {"error": "access denied"}
        assert "private" not in denied.text

        client_iwiki = _tool_call(
            client,
            token,
            "wiki_list_domains",
            {"iwiki_id": "wiki-b"},
            session_id=session_id,
        )
        assert client_iwiki.status_code == 403
        assert "wiki-b" not in client_iwiki.text

        missing = _initialize(client, None)
        assert missing.status_code == 401
        assert missing.headers["www-authenticate"] == "Bearer"
        assert "server-only-model-key" not in missing.text

        invalid = _initialize(client, "invalid")
        assert invalid.status_code == 401
        assert "iwiki_" not in invalid.text

        revoked_response = _initialize(client, revoked)
        assert revoked_response.status_code == 401
        assert "iwiki_" not in revoked_response.text

        invalid_origin = _initialize(
            client, token, origin="https://evil.example"
        )
        assert invalid_origin.status_code == 403
        assert "evil.example" not in invalid_origin.text

        original_authenticate = runtime.app.auth_store.authenticate

        def fail_authentication(_token):
            import psycopg

            raise psycopg.OperationalError(
                "password=database-secret SQL SELECT private"
            )

        runtime.app.auth_store.authenticate = fail_authentication
        try:
            unavailable = _initialize(client, token)
        finally:
            runtime.app.auth_store.authenticate = original_authenticate
        assert unavailable.status_code == 503
        assert unavailable.json() == {"error": "service unavailable"}
        assert "database-secret" not in unavailable.text
        assert "SELECT" not in unavailable.text

        from iwiki_mcp import server
        from iwiki_mcp.engine.embed import EmbedError

        original_store_factory = server._postgres_store_for_binding

        class FailingStore:
            def prepare_read_candidates(self, *_args, **_kwargs):
                raise EmbedError(
                    "provider=http://private.internal model-secret"
                )

        server._postgres_store_for_binding = lambda _binding: FailingStore()
        try:
            model_failure = _tool_call(
                client,
                token,
                "wiki_search",
                {"query": "secret failure"},
                session_id=session_id,
            )
        finally:
            server._postgres_store_for_binding = original_store_factory
        assert model_failure.status_code == 200
        failure_result = json.loads(
            model_failure.json()["result"]["content"][0]["text"]
        )
        assert failure_result["error"] == "model operation failed"
        assert "private.internal" not in model_failure.text
        assert "model-secret" not in model_failure.text

        read_only_init = _initialize(client, disabled)
        assert read_only_init.status_code == 200
        read_only_session = read_only_init.headers["mcp-session-id"]
        _request(
            client,
            disabled,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session_id=read_only_session,
        )
        write_denied = _tool_call(
            client,
            disabled,
            "wiki_write_page",
            {
                "domain": "docs",
                "slug": "concept/blocked",
                "markdown": "# Blocked\n\n## Body\ntext\n",
            },
            session_id=read_only_session,
        )
        assert write_denied.status_code == 403

        narrowed = _tool_call(
            client,
            token,
            "wiki_bind",
            {"read": ["docs"], "write": []},
            session_id=session_id,
        )
        assert narrowed.status_code == 200
        narrowed_result = json.loads(
            narrowed.json()["result"]["content"][0]["text"]
        )
        assert narrowed_result["write"] == []
        write_after_narrow = _tool_call(
            client,
            token,
            "wiki_write_page",
            {
                "domain": "docs",
                "slug": "concept/blocked",
                "markdown": "# Blocked\n\n## Body\ntext\n",
            },
            session_id=session_id,
        )
        assert write_after_narrow.status_code == 403

        auth.set_wiki_active("wiki-a", False)
        disabled_response = _initialize(client, disabled)
        assert disabled_response.status_code == 401

    assert runtime.pool.min_size == 1
    assert runtime.pool.max_size == 2
    assert runtime.app.app.routes[0].app.session_manager.session_idle_timeout == 1800
    with runtime.pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SHOW statement_timeout")
            assert cursor.fetchone()[0] == "30s"
            cursor.execute("SHOW lock_timeout")
            assert cursor.fetchone()[0] == "5s"


def test_hosted_runtime_rejects_git_before_listener(
    clean_postgres, tmp_path, monkeypatch
):
    from psycopg.conninfo import conninfo_to_dict

    from iwiki_mcp import http
    from iwiki_mcp.postgres.config import ConfigError

    values = conninfo_to_dict(clean_postgres)
    config_path = tmp_path / "git-server.toml"
    _write_server_config(config_path, values, storage_type="git")
    environ = RedactedEnv(
        {
            "IWIKI_DB_PASSWORD": values["password"],
            "IWIKI_EMBED_MODEL": "fixture-model",
            "IWIKI_EMBED_DIMENSIONS": "3",
        }
    )
    listener_started = False

    def fail_listener(*_args, **_kwargs):
        nonlocal listener_started
        listener_started = True

    monkeypatch.setattr(http.uvicorn, "run", fail_listener)
    with pytest.raises(ConfigError, match="requires postgres storage"):
        http.run_server(str(config_path), environ=environ)

    assert listener_started is False
