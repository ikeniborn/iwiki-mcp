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


def _tool_result(response):
    assert response.status_code == 200
    return json.loads(response.json()["result"]["content"][0]["text"])


def _assert_tool_denied(response, *, request_id=2):
    """Assert the JSON-RPC shape of a `tools/call` refused at the gate.

    A single `tools/call` denied by `http._authorize_tool` answers as one
    JSON-RPC error over HTTP 200 -- matching the `access_denied` code the
    tool handlers themselves return for the same condition -- instead of
    an HTTP status the MCP client cannot correlate with its request.
    """
    assert response.status_code == 200
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": -32001,
            "message": "access_denied",
            "data": {
                "hint": "the authenticated context does not allow this operation"
            },
        },
    }


def _authorization_request(name, arguments):
    return {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def test_specification_http_authorization_uses_authenticated_scope(monkeypatch):
    from iwiki_mcp import http
    from iwiki_mcp.postgres.auth import AuthContext

    context = AuthContext(
        iwiki_id="wiki-a",
        token_id="token-a",
        read_domains=("docs", "shared"),
        write_domains=("docs",),
        primary="docs",
    )
    calls = []
    monkeypatch.setattr(
        http,
        "authorize_domains",
        lambda _context, **kwargs: calls.append(kwargs),
    )

    http._authorize_tool(
        context, _authorization_request("wiki_spec_search", {"query": "open"})
    )
    http._authorize_tool(
        context,
        _authorization_request(
            "wiki_spec_search",
            {"query": "open", "domains": ["shared", "shared"]},
        ),
    )
    http._authorize_tool(
        context,
        _authorization_request(
            "wiki_spec_context", {"domain": "shared", "scenario_id": "open"}
        ),
    )
    http._authorize_tool(
        context,
        _authorization_request(
            "wiki_spec_resolve", {"domain": "docs", "scenario_id": "open"}
        ),
    )

    assert calls == [
        {"read_domains": ("docs", "shared"), "write_domains": ()},
        {"read_domains": ("shared", "shared"), "write_domains": ()},
        {"read_domains": ("shared",), "write_domains": ()},
        {"read_domains": (), "write_domains": ("docs",)},
    ]


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("wiki_spec_search", {"query": "open", "domains": "docs"}),
        ("wiki_spec_search", {"query": "open", "domains": ["docs", 7]}),
        ("wiki_spec_search", {"query": "open", "domains": ["private"]}),
        ("wiki_spec_context", {"domain": "private", "scenario_id": "open"}),
        ("wiki_spec_context", {"domain": 7, "scenario_id": "open"}),
        ("wiki_spec_resolve", {"domain": "shared", "scenario_id": "open"}),
        ("wiki_spec_resolve", {"domain": "docs", "scenario_id": "open", "iwiki_id": "wiki-b"}),
    ],
)
def test_specification_http_authorization_denies_malformed_or_cross_scope(
    name, arguments,
):
    from iwiki_mcp import http
    from iwiki_mcp.postgres.auth import AccessError, AuthContext

    context = AuthContext(
        iwiki_id="wiki-a",
        token_id="token-a",
        read_domains=("docs", "shared"),
        write_domains=("docs", "shared"),
        primary="docs",
    )

    with pytest.raises(AccessError):
        http._authorize_tool(context, _authorization_request(name, arguments))


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
            # No wiki_bind ran in this session, so the scope comes from the
            # token's own grants and the answer says so.
            "binding_source": "token_default",
            "specifications": {"domains": [{
                "domain": "docs",
                "mode": "optional",
                "source": "hosted_default",
                "projection_state": "absent",
                "scenarios": 0,
                "bindings": 0,
            }]},
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
        _assert_tool_denied(denied)
        assert "private" not in denied.text

        client_iwiki = _tool_call(
            client,
            token,
            "wiki_list_domains",
            {"iwiki_id": "wiki-b"},
            session_id=session_id,
        )
        _assert_tool_denied(client_iwiki)
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
        _assert_tool_denied(write_denied)

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
        _assert_tool_denied(write_after_narrow)

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


def test_denied_batch_request_keeps_the_http_status_path(hosted_runtime):
    # The JSON-RPC denial shape covers one request, which carries an `id`
    # the client can correlate. A batch has no single id, so it keeps the
    # HTTP 403 path -- the boundary that makes the single-request shape safe.
    runtime = hosted_runtime.runtime
    token = hosted_runtime.token

    with TestClient(runtime.app, base_url="http://127.0.0.1:8765") as client:
        session_id = _initialize(client, token).headers["mcp-session-id"]
        denied_batch = _request(
            client,
            token,
            [
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {
                        "name": "wiki_read_page",
                        "arguments": {"domain": "private", "slug": "hidden"},
                    },
                }
            ],
            session_id=session_id,
        )

    assert denied_batch.status_code == 403
    assert denied_batch.json() == {"error": "access denied"}
    assert "private" not in denied_batch.text


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


def test_hosted_runtime_pins_v7_guard_before_install_without_database(
    tmp_path, monkeypatch
):
    from iwiki_mcp import http, server

    config_path = tmp_path / "server.toml"
    _write_server_config(
        config_path,
        {
            "host": "db.invalid",
            "port": 5432,
            "dbname": "fixture",
            "user": "fixture",
            "sslmode": "require",
        },
    )
    environ = RedactedEnv(
        {
            "IWIKI_DB_PASSWORD": "fixture-password",
            "IWIKI_LLM_BASE_URL": "http://example.invalid/v1",
            "IWIKI_LLM_KEY": "fixture-key",
            "IWIKI_EMBED_MODEL": "fixture-model",
            "IWIKI_EMBED_DIMENSIONS": "3",
            "IWIKI_RERANK_MODEL": "",
        }
    )
    calls = []
    monkeypatch.setattr(
        http,
        "require_schema_version",
        lambda _dsn, *, expected_version: calls.append(
            ("schema", expected_version)
        ),
    )
    monkeypatch.setattr(
        http,
        "require_hosted_runtime_principal",
        lambda _dsn: calls.append("principal"),
    )

    class Pool:
        def __init__(self, *_args, **_kwargs):
            pass

        def open(self, *, wait):
            assert wait is True
            calls.append("pool")

        def close(self):
            calls.append("close")

        def connection(self):
            raise AssertionError("mock runtime opened a database connection")

    monkeypatch.setattr(http, "ConnectionPool", Pool)
    monkeypatch.setattr(
        http, "AuthenticatedMCPMiddleware", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(
        server,
        "_install_hosted_runtime",
        lambda *_args, **_kwargs: calls.append("install"),
    )

    runtime = http.prepare_runtime(
        str(config_path), environ=environ, probe=lambda _cfg: calls.append("probe")
    )

    assert calls[:5] == ["probe", ("schema", 7), "principal", "pool", "install"]
    runtime.close()


@pytest.mark.parametrize("installed_version", [6, 7])
def test_hosted_runtime_requires_exact_v7_before_installing_runtime(
    clean_postgres, tmp_path, monkeypatch, installed_version
):
    from psycopg.conninfo import conninfo_to_dict

    from iwiki_mcp import http, server
    from iwiki_mcp.postgres import migrations

    settings = migrations.MigrationSettings(
        dsn=clean_postgres,
        embed_model="fixture-model",
        embed_dimensions=3,
        statement_timeout_ms=30_000,
        lock_timeout_ms=5_000,
    )
    migrations.run_migrations(
        settings, migrations=migrations.MIGRATIONS[:installed_version]
    )
    values = conninfo_to_dict(clean_postgres)
    config_path = tmp_path / "server.toml"
    _write_server_config(config_path, values)
    environ = RedactedEnv(
        {
            "IWIKI_DB_PASSWORD": values.get("password", ""),
            "IWIKI_LLM_BASE_URL": "http://example.invalid/v1",
            "IWIKI_LLM_KEY": "fixture-key",
            "IWIKI_EMBED_MODEL": "fixture-model",
            "IWIKI_EMBED_DIMENSIONS": "3",
            "IWIKI_RERANK_MODEL": "",
        }
    )
    monkeypatch.setattr(
        migrations,
        "run_migrations",
        lambda *_args, **_kwargs: pytest.fail("runtime must not migrate"),
    )
    installed = []
    monkeypatch.setattr(
        server,
        "_install_hosted_runtime",
        lambda *_args, **_kwargs: installed.append("runtime"),
    )
    monkeypatch.setattr(http, "require_hosted_runtime_principal", lambda _dsn: None)

    class Pool:
        def __init__(self, *_args, **_kwargs):
            self.closed = False

        def open(self, *, wait):
            assert wait is True

        def close(self):
            self.closed = True

        def connection(self):
            raise AssertionError("connection is not opened while installing runtime")

    monkeypatch.setattr(http, "ConnectionPool", Pool)
    monkeypatch.setattr(
        http, "AuthenticatedMCPMiddleware", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(server.mcp, "streamable_http_app", lambda: object())

    if installed_version == 6:
        with pytest.raises(
            migrations.MigrationError,
            match="schema version 7 is required",
        ):
            http.prepare_runtime(
                str(config_path), environ=environ, probe=lambda _cfg: None
            )
        assert installed == []
    else:
        runtime = http.prepare_runtime(
            str(config_path), environ=environ, probe=lambda _cfg: None
        )
        assert installed == ["runtime"]
        runtime.close()


def test_hosted_runtime_rejects_an_unprovisioned_session_user(
    clean_postgres, tmp_path, monkeypatch
):
    from psycopg.conninfo import conninfo_to_dict

    from tests.postgres.conftest import _cfg, create_runtime_role, drop_runtime_role

    from iwiki_mcp import http, server
    from iwiki_mcp.postgres import migrations
    from iwiki_mcp.postgres.store import PostgresStore, provision_runtime_grant

    migrations.run_migrations(
        migrations.MigrationSettings(
            dsn=clean_postgres,
            embed_model="fixture-model",
            embed_dimensions=3,
            statement_timeout_ms=30_000,
            lock_timeout_ms=5_000,
        )
    )
    admin_store = PostgresStore(clean_postgres, "wiki-a", _cfg())
    admin_store.create_wiki("wiki-a")
    admin_store.create_domain("docs")
    role, password = create_runtime_role(clean_postgres, prefix="unprovisioned")
    provision_runtime_grant(
        clean_postgres,
        principal=role,
        iwiki_id="wiki-a",
        read_domains=["docs"],
        write_domains=["docs"],
        runtime="direct",
    )
    values = {**conninfo_to_dict(clean_postgres), "user": role, "password": password}
    config_path = tmp_path / "server.toml"
    _write_server_config(config_path, values)
    environ = RedactedEnv(
        {
            "IWIKI_DB_PASSWORD": password,
            "IWIKI_LLM_BASE_URL": "http://example.invalid/v1",
            "IWIKI_LLM_KEY": "fixture-key",
            "IWIKI_EMBED_MODEL": "fixture-model",
            "IWIKI_EMBED_DIMENSIONS": "3",
            "IWIKI_RERANK_MODEL": "",
        }
    )
    monkeypatch.setattr(
        server,
        "_install_hosted_runtime",
        lambda *_args, **_kwargs: pytest.fail("runtime must fail before install"),
    )

    try:
        with pytest.raises(ValueError, match="hosted principal"):
            http.prepare_runtime(
                str(config_path), environ=environ, probe=lambda _cfg: None
            )
    finally:
        drop_runtime_role(clean_postgres, role)


@pytest.mark.parametrize("installed_version", [6, 7])
def test_stdio_runtime_requires_exact_v7_without_running_migrations(
    clean_postgres, monkeypatch, installed_version
):
    from iwiki_mcp import server
    from iwiki_mcp.engine.config import Config
    from iwiki_mcp.postgres import migrations

    settings = migrations.MigrationSettings(
        dsn=clean_postgres,
        embed_model="fixture-model",
        embed_dimensions=3,
        statement_timeout_ms=30_000,
        lock_timeout_ms=5_000,
    )
    migrations.run_migrations(
        settings, migrations=migrations.MIGRATIONS[:installed_version]
    )
    binding = type(
        "DisposableBinding",
        (),
        {"connection_dsn": lambda self: clean_postgres},
    )()
    monkeypatch.setattr(server.base, "resolve_project_dir", lambda: "/tmp/project")
    monkeypatch.setattr(
        server.base,
        "load_project_config",
        lambda _project: {"storage": {"type": "postgres"}},
    )
    monkeypatch.setattr(
        server.base, "resolve_storage_binding", lambda _project: binding
    )
    monkeypatch.setattr(server, "_is_postgres", lambda _binding: True)
    monkeypatch.setattr(
        migrations,
        "run_migrations",
        lambda *_args, **_kwargs: pytest.fail("runtime must not migrate"),
    )
    cfg = Config(
        base_url="http://example.invalid/v1",
        api_key="fixture",
        embed_model="fixture-model",
        dimensions=3,
        chunk_size=512,
        chunk_overlap=64,
        summary_max=400,
        top_k=8,
        score_threshold=0.0,
        graph_depth=2,
        ignore=None,
        seed_top_k=2,
        bfs_top_k=10,
        seed_threshold=0.0,
    )

    if installed_version == 6:
        with pytest.raises(
            migrations.MigrationError,
            match="schema version 7 is required",
        ):
            server._initialize_postgres_storage(cfg)
    else:
        server._initialize_postgres_storage(cfg)


def test_session_refresh_preserves_selection_without_grant_expansion(
    hosted_runtime,
):
    import psycopg

    runtime = hosted_runtime.runtime
    auth = hosted_runtime.auth
    created = auth.create_token(
        "wiki-a",
        "session-manager",
        read_domains=["docs", "private"],
        write_domains=["docs", "private"],
    )
    token = created["token"]

    def status(client, session_id):
        response = _tool_call(
            client, token, "wiki_status", {}, session_id=session_id
        )
        assert response.status_code == 200
        return json.loads(response.json()["result"]["content"][0]["text"])

    def set_private(enabled):
        with psycopg.connect(auth.dsn) as connection:
            with connection.cursor() as cursor:
                if enabled:
                    cursor.execute(
                        "INSERT INTO iwiki.token_domain_grants "
                        "(iwiki_id, token_id, domain_id, can_read, can_write) "
                        "SELECT %s, %s, domain_id, true, true "
                        "FROM iwiki.domains WHERE iwiki_id = %s "
                        "AND slug = 'private' "
                        "ON CONFLICT (iwiki_id, token_id, domain_id) "
                        "DO UPDATE SET can_read = true, can_write = true",
                        ("wiki-a", created["token_id"], "wiki-a"),
                    )
                else:
                    cursor.execute(
                        "DELETE FROM iwiki.token_domain_grants g "
                        "USING iwiki.domains d WHERE d.iwiki_id = g.iwiki_id "
                        "AND d.domain_id = g.domain_id AND g.iwiki_id = %s "
                        "AND g.token_id = %s AND d.slug = 'private'",
                        ("wiki-a", created["token_id"]),
                    )

    with TestClient(
        runtime.app, base_url="http://127.0.0.1:8765"
    ) as client:
        initialized = _initialize(client, token)
        session_id = initialized.headers["mcp-session-id"]
        _request(
            client,
            token,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session_id=session_id,
        )

        set_private(False)
        assert status(client, session_id)["read"] == ["docs"]

        set_private(True)
        restored = status(client, session_id)
        assert restored["read"] == ["docs", "private"]
        assert restored["write"] == ["docs", "private"]

        auth.create_domain("wiki-a", "new")
        with psycopg.connect(auth.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO iwiki.token_domain_grants "
                    "(iwiki_id, token_id, domain_id, can_read, can_write) "
                    "SELECT %s, %s, domain_id, true, true "
                    "FROM iwiki.domains WHERE iwiki_id = %s AND slug = 'new'",
                    ("wiki-a", created["token_id"], "wiki-a"),
                )
        assert "new" not in status(client, session_id)["read"]

        narrowed = _tool_call(
            client,
            token,
            "wiki_bind",
            {"read": ["docs"], "write": ["docs"], "primary": "docs"},
            session_id=session_id,
        )
        assert narrowed.status_code == 200
        set_private(False)
        set_private(True)
        assert status(client, session_id)["read"] == ["docs"]


def test_hosted_domain_creation_and_content_grant_lifecycle(hosted_runtime):
    import psycopg

    runtime = hosted_runtime.runtime
    auth = hosted_runtime.auth
    creator = auth.create_token(
        "wiki-a",
        "creator",
        read_domains=[],
        write_domains=[],
        can_create_domain=True,
    )
    target = auth.create_token(
        "wiki-a",
        "target",
        read_domains=["docs"],
        write_domains=[],
    )
    outsider = auth.create_token(
        "wiki-a",
        "outsider",
        read_domains=["docs"],
        write_domains=[],
    )

    def initialize(client, token):
        response = _initialize(client, token)
        assert response.status_code == 200
        session_id = response.headers["mcp-session-id"]
        _request(
            client,
            token,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session_id=session_id,
        )
        return session_id

    def row_counts():
        with psycopg.connect(auth.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT "
                    "(SELECT count(*) FROM iwiki.domains "
                    "WHERE iwiki_id = 'wiki-a' AND slug = 'new-project'), "
                    "(SELECT count(*) FROM iwiki.token_domain_grants g "
                    "JOIN iwiki.domains d ON d.iwiki_id = g.iwiki_id "
                    "AND d.domain_id = g.domain_id "
                    "WHERE g.iwiki_id = 'wiki-a' AND d.slug = 'new-project'), "
                    "(SELECT count(*) "
                    "FROM iwiki.token_domain_management_grants m "
                    "JOIN iwiki.domains d ON d.iwiki_id = m.iwiki_id "
                    "AND d.domain_id = m.domain_id "
                    "WHERE m.iwiki_id = 'wiki-a' "
                    "AND d.slug = 'new-project')"
                )
                return cursor.fetchone()

    with TestClient(
        runtime.app, base_url="http://127.0.0.1:8765"
    ) as client:
        creator_session = initialize(client, creator["token"])
        target_session = initialize(client, target["token"])
        outsider_session = initialize(client, outsider["token"])

        listed_tools = _request(
            client,
            outsider["token"],
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            session_id=outsider_session,
        )
        names = {
            tool["name"] for tool in listed_tools.json()["result"]["tools"]
        }
        assert {
            "wiki_create_domain",
            "wiki_list_domain_grants",
            "wiki_set_domain_grant",
            "wiki_revoke_domain_grant",
        } <= names

        denied_create = _tool_call(
            client,
            outsider["token"],
            "wiki_create_domain",
            {"name": "denied"},
            session_id=outsider_session,
        )
        denied_list = _tool_call(
            client,
            outsider["token"],
            "wiki_list_domain_grants",
            {"domain": "docs"},
            session_id=outsider_session,
        )
        _assert_tool_denied(denied_create)
        _assert_tool_denied(denied_list)

        malformed_create = _request(
            client,
            creator["token"],
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "wiki_create_domain",
                    "arguments": [],
                },
            },
            session_id=creator_session,
        )
        tenant_override = _tool_call(
            client,
            creator["token"],
            "wiki_create_domain",
            {"name": "denied", "iwiki_id": "wiki-b"},
            session_id=creator_session,
        )
        # Malformed `arguments` on a protected tool is still a denial of that
        # one request, so it answers in the same JSON-RPC shape.
        _assert_tool_denied(malformed_create, request_id=4)
        _assert_tool_denied(tenant_override)

        invalid_domain = _tool_result(
            _tool_call(
                client,
                creator["token"],
                "wiki_create_domain",
                {"name": "bad/name"},
                session_id=creator_session,
            )
        )
        assert invalid_domain["error"] == "operation failed"

        created = _tool_result(
            _tool_call(
                client,
                creator["token"],
                "wiki_create_domain",
                {"name": "new-project"},
                session_id=creator_session,
            )
        )
        assert created == {
            "created": "new-project",
            "already_existed": False,
            "domain": "new-project",
            "read": ["new-project"],
            "write": ["new-project"],
            "primary": "new-project",
        }
        counts = row_counts()

        retried = _tool_result(
            _tool_call(
                client,
                creator["token"],
                "wiki_create_domain",
                {"name": "new-project"},
                session_id=creator_session,
            )
        )
        assert retried == {**created, "already_existed": True}
        assert row_counts() == counts == (1, 1, 1)

        before_grant = _tool_result(
            _tool_call(
                client,
                target["token"],
                "wiki_status",
                {},
                session_id=target_session,
            )
        )
        assert before_grant["read"] == ["docs"]

        granted = _tool_result(
            _tool_call(
                client,
                creator["token"],
                "wiki_set_domain_grant",
                {
                    "domain": "new-project",
                    "token_id": target["token_id"],
                    "can_read": True,
                    "can_write": False,
                },
                session_id=creator_session,
            )
        )
        assert granted == {
            "domain": "new-project",
            "token_id": target["token_id"],
            "can_read": True,
            "can_write": False,
        }
        assert "new-project" not in _tool_result(
            _tool_call(
                client,
                target["token"],
                "wiki_status",
                {},
                session_id=target_session,
            )
        )["read"]

        fresh_target_session = initialize(client, target["token"])
        assert "new-project" in _tool_result(
            _tool_call(
                client,
                target["token"],
                "wiki_status",
                {},
                session_id=fresh_target_session,
            )
        )["read"]

        grants = _tool_result(
            _tool_call(
                client,
                creator["token"],
                "wiki_list_domain_grants",
                {"domain": "new-project"},
                session_id=creator_session,
            )
        )
        by_token = {row["token_id"]: row for row in grants["grants"]}
        assert by_token[creator["token_id"]]["can_manage_grants"] is True
        assert by_token[target["token_id"]]["can_manage_grants"] is False

        invalid = _tool_result(
            _tool_call(
                client,
                creator["token"],
                "wiki_set_domain_grant",
                {
                    "domain": "new-project",
                    "token_id": target["token_id"],
                    "can_read": False,
                    "can_write": True,
                },
                session_id=creator_session,
            )
        )
        assert invalid["error"] == "operation failed"

        self_target = _tool_result(
            _tool_call(
                client,
                creator["token"],
                "wiki_revoke_domain_grant",
                {
                    "domain": "new-project",
                    "token_id": creator["token_id"],
                },
                session_id=creator_session,
            )
        )
        assert self_target["error"] == "access_denied"

        revoked = _tool_result(
            _tool_call(
                client,
                creator["token"],
                "wiki_revoke_domain_grant",
                {
                    "domain": "new-project",
                    "token_id": target["token_id"],
                },
                session_id=creator_session,
            )
        )
        assert revoked == {
            "domain": "new-project",
            "token_id": target["token_id"],
            "revoked": True,
        }
        assert "new-project" not in _tool_result(
            _tool_call(
                client,
                target["token"],
                "wiki_status",
                {},
                session_id=fresh_target_session,
            )
        )["read"]

        stale = auth.authenticate(creator["token"])
        auth.set_domain_management(
            "wiki-a", creator["token_id"], "new-project", False
        )
        original_authenticate = runtime.app.auth_store.authenticate
        runtime.app.auth_store.authenticate = lambda _token: stale
        try:
            transaction_denied = _tool_result(
                _tool_call(
                    client,
                    creator["token"],
                    "wiki_list_domain_grants",
                    {"domain": "new-project"},
                    session_id=creator_session,
                )
            )
        finally:
            runtime.app.auth_store.authenticate = original_authenticate
        assert transaction_denied["error"] == "access_denied"
