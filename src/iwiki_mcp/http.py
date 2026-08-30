"""Authenticated Streamable HTTP runtime for hosted PostgreSQL storage."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from threading import Lock
import time
from typing import Any, Mapping
from urllib.parse import urlsplit

import anyio
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.transport_security import TransportSecuritySettings
import psycopg
from psycopg_pool import ConnectionPool
import uvicorn

from . import admin, base
from .engine.config import Config
from .engine.embed import probe_embedding_endpoint
from .postgres.auth import (
    AccessError,
    AuthContext,
    AuthStore,
    authenticate_bearer,
    authorize_domains,
    validate_domain_identifier,
)
from .postgres.config import ConfigError, ServerConfig, load_server_config
from .postgres.migrations import require_schema_version
from .postgres.store import require_hosted_runtime_principal


_READ_DOMAIN_TOOLS = {
    "wiki_list_pages",
    "wiki_read_page",
    "wiki_related",
    "wiki_lint",
    "wiki_spec_context",
}
_WRITE_DOMAIN_TOOLS = {
    "wiki_write_page",
    "wiki_update_page",
    "wiki_delete_page",
    "wiki_insert_section",
    "wiki_delete_section",
    "wiki_move_section",
    "wiki_index",
    "wiki_spec_resolve",
}
_CODE_PUBLISH_TOOLS = {
    "wiki_code_publish_begin",
    "wiki_code_publish_batch",
    "wiki_code_publish_finalize",
    "wiki_code_publish_abort",
}
_CODE_READ_TOOLS = {
    "wiki_code_status",
    "wiki_code_search",
    "wiki_code_context",
}
_DOMAIN_GRANT_TOOLS = {
    "wiki_list_domain_grants",
    "wiki_set_domain_grant",
    "wiki_revoke_domain_grant",
}
_SESSION_IDLE_SECONDS = 1800.0


@dataclass(frozen=True)
class _SessionRecord:
    token_id: str
    iwiki_id: str
    state: Any
    last_seen: float


class _SessionBindings:
    """Persist narrowed binding state without retaining plaintext tokens."""

    def __init__(self) -> None:
        self._records: dict[str, _SessionRecord] = {}
        self._lock = Lock()

    def _prune(self, now: float) -> None:
        expired = [
            session_id
            for session_id, record in self._records.items()
            if now - record.last_seen >= _SESSION_IDLE_SECONDS
        ]
        for session_id in expired:
            self._records.pop(session_id, None)

    def resolve(
        self, session_id: str | None, context: AuthContext
    ) -> Any | None:
        if session_id is None:
            return None
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            record = self._records.get(session_id)
            if record is None:
                return None
            if (
                record.token_id != context.token_id
                or record.iwiki_id != context.iwiki_id
            ):
                return None
            self._records[session_id] = _SessionRecord(
                record.token_id, record.iwiki_id, record.state, now
            )
        return record.state

    def store(
        self,
        session_id: str,
        context: AuthContext,
        state: Any,
    ) -> None:
        now = time.monotonic()
        record = _SessionRecord(
            context.token_id, context.iwiki_id, state, now
        )
        with self._lock:
            self._prune(now)
            current = self._records.get(session_id)
            if current is not None and (
                current.token_id != context.token_id
                or current.iwiki_id != context.iwiki_id
            ):
                raise AccessError(403)
            self._records[session_id] = record

    def remove(self, session_id: str | None) -> None:
        if session_id is not None:
            with self._lock:
                self._records.pop(session_id, None)


def _header_values(scope, name: bytes) -> list[str]:
    return [
        value.decode("latin-1")
        for key, value in scope.get("headers", ())
        if key.lower() == name
    ]


def _one_header(scope, name: bytes) -> str | None:
    values = _header_values(scope, name)
    if len(values) > 1:
        raise AccessError(401 if name == b"authorization" else 403)
    return values[0] if values else None


async def _send_error(send, error: AccessError) -> None:
    body = json.dumps({"error": str(error)}).encode("utf-8")
    headers = [(b"content-type", b"application/json")]
    if error.status_code == 401:
        headers.append((b"www-authenticate", b"Bearer"))
    await send(
        {
            "type": "http.response.start",
            "status": error.status_code,
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": body})


def _request_id(payload: Any) -> Any:
    return payload.get("id") if isinstance(payload, dict) else None


async def _send_tool_access_denied(send, request_id: Any) -> None:
    """JSON-RPC error for a `tools/call` denied at the authorization gate
    (`_authorize_tool`), matching the `access_denied` code/hint the tool
    handlers themselves return via `@_safe` for the same condition."""
    body = json.dumps(
        {
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
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _send_service_unavailable(send) -> None:
    body = b'{"error":"service unavailable"}'
    await send(
        {
            "type": "http.response.start",
            "status": 503,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _send_method_not_allowed(send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 405,
            "headers": [(b"allow", b"POST, DELETE")],
        }
    )
    await send({"type": "http.response.body", "body": b""})


def _binding(
    config: ServerConfig, context: AuthContext, project_dir: str
) -> base.PostgresBinding:
    storage = config.storage
    specifications = getattr(config, "specifications", None)
    specification_mode = "optional"
    if specifications is not None:
        specification_mode = (
            specifications.mode_for(context.iwiki_id, context.primary)
            if context.primary is not None
            else specifications.default_mode
        )
    return base.PostgresBinding(
        host=storage.host,
        port=storage.port,
        database=storage.database,
        user=storage.user,
        sslmode=storage.sslmode,
        password=storage.password,
        iwiki_id=context.iwiki_id,
        read=context.read_domains,
        write=context.write_domains,
        primary=context.primary,
        project_dir=project_dir,
        embed_model=config.models.embed_model,
        embed_dimensions=config.models.embed_dimensions,
        rerank_model=config.models.rerank_model,
        specification_mode=specification_mode,
    )


def _effective_binding(
    selected: base.PostgresBinding, context: AuthContext
) -> base.PostgresBinding:
    read = tuple(
        domain for domain in selected.read if domain in context.read_domains
    )
    write = tuple(
        domain
        for domain in selected.write
        if domain in context.write_domains and domain in read
    )
    primary = selected.primary if selected.primary in write else None
    if primary is None and write:
        primary = write[0]
    return replace(
        selected,
        read=read,
        write=write,
        primary=primary,
    )


def _string_domains(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _authorize_tool(context: AuthContext, request: Any) -> None:
    if isinstance(request, list):
        for item in request:
            _authorize_tool(context, item)
        return
    if not isinstance(request, dict) or request.get("method") != "tools/call":
        return
    params = request.get("params")
    if not isinstance(params, dict):
        raise AccessError(403)
    name = params.get("name")
    protected = name == "wiki_create_domain" or name in _DOMAIN_GRANT_TOOLS
    code_graph = name in _CODE_PUBLISH_TOOLS or name in _CODE_READ_TOOLS
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        if protected or name in {
            "wiki_spec_search", "wiki_spec_context", "wiki_spec_resolve"
        }:
            raise AccessError(403)
        if not code_graph:
            return
        arguments = {}
    if "iwiki_id" in arguments:
        raise AccessError(403)
    if code_graph:
        if "domain" in arguments:
            raise AccessError(403)
        if name in _CODE_PUBLISH_TOOLS:
            context.require_primary_write()
        else:
            context.require_primary_read()
        return
    if name == "wiki_create_domain":
        if not context.can_create_domain:
            raise AccessError(403)
        return
    if name in _DOMAIN_GRANT_TOOLS:
        if "domain" not in arguments:
            raise AccessError(403)
        domain = arguments["domain"]
        try:
            validate_domain_identifier(domain)
        except ValueError:
            if not context.managed_domains:
                raise AccessError(403)
            return
        if not context.can_manage_grants(domain):
            raise AccessError(403)
        return

    read_domains: tuple[str, ...] = ()
    write_domains: tuple[str, ...] = ()
    domain = arguments.get("domain")
    if name == "wiki_spec_search":
        raw_domains = arguments.get("domains")
        if raw_domains is None:
            read_domains = context.read_domains
        elif type(raw_domains) is not list or any(
            type(item) is not str for item in raw_domains
        ):
            raise AccessError(403)
        else:
            read_domains = tuple(raw_domains)
    elif name == "wiki_spec_context":
        if type(domain) is not str:
            raise AccessError(403)
        read_domains = (domain,)
    elif name == "wiki_spec_resolve":
        if type(domain) is not str or domain != context.require_primary_write():
            raise AccessError(403)
        write_domains = (domain,)
    elif name in _READ_DOMAIN_TOOLS and isinstance(domain, str):
        read_domains = (domain,)
    elif name in _WRITE_DOMAIN_TOOLS:
        target = domain if isinstance(domain, str) else context.primary
        if target is not None:
            write_domains = (target,)
    elif name == "wiki_search":
        requested = _string_domains(arguments.get("domains"))
        raw_intent = arguments.get("intent", "read")
        intent = raw_intent.strip().lower() if isinstance(raw_intent, str) else ""
        if intent == "write":
            target = requested[0] if requested else context.primary
            if target is not None:
                write_domains = (target,)
        else:
            read_domains = requested
    elif name == "wiki_bind":
        read_domains = _string_domains(arguments.get("read"))
        write_domains = _string_domains(arguments.get("write"))
        primary = arguments.get("primary")
        if isinstance(primary, str):
            write_domains = (*write_domains, primary)
    authorize_domains(
        context,
        read_domains=read_domains,
        write_domains=write_domains,
    )


async def _request_messages(receive):
    messages = []
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request" or not message.get(
            "more_body", False
        ):
            return messages


def _request_json(messages) -> Any:
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.request"
    )
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


class AuthenticatedMCPMiddleware:
    """Authenticate each MCP request and install tenant-scoped dispatch state."""

    def __init__(
        self,
        app,
        *,
        config: ServerConfig,
        auth_store: AuthStore,
        project_dir: str,
    ) -> None:
        self.app = app
        self.config = config
        self.auth_store = auth_store
        self.project_dir = project_dir
        self.sessions = _SessionBindings()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope.get("path") != "/mcp":
            await self.app(scope, receive, send)
            return
        try:
            origins = _header_values(scope, b"origin")
            if len(origins) > 1:
                raise AccessError(403)
            if origins:
                from .postgres.config import normalize_origin

                try:
                    supplied_origin = normalize_origin(origins[0])
                except ConfigError as exc:
                    raise AccessError(403) from exc
                if (
                    origins[0] != supplied_origin
                    or supplied_origin not in self.config.server.allowed_origins
                ):
                    raise AccessError(403)

            authorization = _one_header(scope, b"authorization")
            context = await anyio.to_thread.run_sync(
                authenticate_bearer, self.auth_store, authorization
            )
            scope["user"] = AuthenticatedUser(
                AccessToken(
                    token="",
                    client_id=context.token_id,
                    subject=context.iwiki_id,
                    scopes=[],
                )
            )
            if scope.get("method") == "GET":
                await _send_method_not_allowed(send)
                return
            session_id = _one_header(scope, b"mcp-session-id")
            initial = _binding(self.config, context, self.project_dir)

            from . import server

            state = self.sessions.resolve(session_id, context)
            if state is None:
                selected = server._HostedSelectedState(initial)
                state = server._HostedBindingState(selected, initial)

            async with state.request_lock():
                selected = state.selected_state()
                current = _effective_binding(selected.get(), context)
                effective_context = replace(
                    context,
                    read_domains=tuple(current.read),
                    write_domains=tuple(current.write),
                    primary=current.primary,
                )
                state.set_effective(current, effective_context)
                try:
                    messages = await _request_messages(receive)
                    request_json = _request_json(messages)
                    try:
                        _authorize_tool(effective_context, request_json)
                    except AccessError:
                        if not isinstance(request_json, dict):
                            raise
                        await _send_tool_access_denied(
                            send, _request_id(request_json)
                        )
                        return
                    iterator = iter(messages)

                    async def replay_receive():
                        try:
                            return next(iterator)
                        except StopIteration:
                            return await receive()

                    auth_token = server._AUTH_CONTEXT.set(effective_context)
                    binding_token = server._SESSION_BINDING.set(state)

                    async def capture_send(message):
                        if message["type"] == "http.response.start":
                            response_session = next(
                                (
                                    value.decode("latin-1")
                                    for key, value in message.get("headers", ())
                                    if key.lower() == b"mcp-session-id"
                                ),
                                None,
                            )
                            if (
                                scope.get("method") == "DELETE"
                                and message["status"] < 400
                            ):
                                self.sessions.remove(session_id)
                            elif message["status"] < 400:
                                target_session = response_session or session_id
                                if target_session is not None:
                                    self.sessions.store(
                                        target_session,
                                        context,
                                        state,
                                    )
                        await send(message)

                    try:
                        inner_scope = dict(scope)
                        inner_scope["headers"] = [
                            (key, value)
                            for key, value in scope.get("headers", ())
                            if key.lower()
                            not in {b"authorization", b"origin"}
                        ]
                        await self.app(
                            inner_scope, replay_receive, capture_send
                        )
                    finally:
                        server._SESSION_BINDING.reset(binding_token)
                        server._AUTH_CONTEXT.reset(auth_token)
                finally:
                    state.reset_effective()
        except AccessError as exc:
            await _send_error(send, exc)
        except psycopg.Error:
            await _send_service_unavailable(send)


@dataclass
class HostedRuntime:
    """Prepared hosted resources; no listener exists until ``run_server``."""

    config: ServerConfig
    engine_config: Config
    pool: ConnectionPool
    app: Any
    dsn: str = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def close(self) -> None:
        if self._closed:
            return
        from . import server

        server._clear_hosted_runtime(self.pool)
        self.pool.close()
        self._closed = True


def _allowed_hosts(config: ServerConfig) -> list[str]:
    listen_host = config.server.host
    if ":" in listen_host and not listen_host.startswith("["):
        listen_host = f"[{listen_host}]"
    hosts = {f"{listen_host}:*"}
    for origin in config.server.allowed_origins:
        authority = urlsplit(origin).netloc
        hosts.add(authority)
    return sorted(hosts)


def prepare_runtime(
    config_path: str,
    *,
    environ: Mapping[str, str] | None = None,
    probe=probe_embedding_endpoint,
) -> HostedRuntime:
    """Validate and initialize every hosted dependency before listening."""
    env = os.environ if environ is None else environ
    config = load_server_config(config_path, env)
    cfg = admin._engine_config(config, env)
    probe(cfg)
    dsn = admin._dsn(config)
    require_schema_version(dsn, expected_version=7)
    require_hosted_runtime_principal(dsn)
    options = (
        f"-c statement_timeout={config.server.statement_timeout_ms} "
        f"-c lock_timeout={config.server.lock_timeout_ms}"
    )
    pool = ConnectionPool(
        dsn,
        min_size=config.server.pool_min_size,
        max_size=config.server.pool_max_size,
        kwargs={"options": options},
        name="iwiki-http",
        open=False,
    )
    try:
        pool.open(wait=True)
        from . import server

        server._install_hosted_runtime(
            pool, cfg, config.code_graph, config.specifications
        )
        server.mcp.settings.json_response = True
        server.mcp.settings.stateless_http = False
        server.mcp.settings.transport_security = TransportSecuritySettings(
            allowed_hosts=_allowed_hosts(config),
            allowed_origins=list(config.server.allowed_origins),
        )
        mcp_app = server.mcp.streamable_http_app()
        server.mcp.session_manager.session_idle_timeout = _SESSION_IDLE_SECONDS
        app = AuthenticatedMCPMiddleware(
            mcp_app,
            config=config,
            auth_store=AuthStore(dsn, connection_factory=pool.connection),
            project_dir=os.path.abspath(os.path.dirname(config_path)),
        )
    except Exception:
        from . import server

        server._clear_hosted_runtime(pool)
        pool.close()
        raise
    return HostedRuntime(config, cfg, pool, app, dsn)


def run_server(
    config_path: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Prepare hosted mode, then bind its validated loopback listener."""
    runtime = prepare_runtime(config_path, environ=environ)
    try:
        uvicorn.run(
            runtime.app,
            host=runtime.config.server.host,
            port=runtime.config.server.port,
        )
    finally:
        runtime.close()
        from .codegraph.runtime import shutdown_code_graph_workers

        shutdown_code_graph_workers()
