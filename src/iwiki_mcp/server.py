"""iwiki MCP server.

Tools are fail-soft: every handler returns a JSON-serializable dict, and
exceptions become {"error","hint"} structures.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import replace
import functools
from hashlib import sha256
import json
import logging
import os
import re
import secrets
import sys
from threading import RLock
import time
from contextvars import ContextVar
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Literal

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.stdio import stdio_server
from pydantic import Field

from . import admin as _admin  # noqa: F401
from . import base, cross_domain, graph, ignore, indexer, okf, retrieval, sync
from . import specifications as _specifications
from .specification_store import (
    GitSpecificationStore,
    semantic_markdown_revision,
)
from . import http as _http  # noqa: F401
from .telegram_bot import main as _telegram_bot_main  # noqa: F401
from .postgres import migrations as _postgres_migrations  # noqa: F401
from .postgres import auth as _postgres_auth  # noqa: F401
from .postgres import store as _postgres_store  # noqa: F401
from .postgres import codegraph as _postgres_codegraph  # noqa: F401
# Code graph adapters join the full startup import closure; their grammar and
# parser initialization remains lazy until an adapter parses source.
from .codegraph import config as _codegraph_config  # noqa: F401
from .codegraph import discovery as _codegraph_discovery  # noqa: F401
from .codegraph import fingerprint as _codegraph_fingerprint  # noqa: F401
from .codegraph import linking as _codegraph_linking
from .codegraph import application as _codegraph_application  # noqa: F401
from .codegraph import location as _codegraph_location  # noqa: F401
from .codegraph import models as _codegraph_models  # noqa: F401
from .codegraph import publication as _codegraph_publication  # noqa: F401
from .codegraph import runtime as _codegraph_runtime  # noqa: F401
from .codegraph import schema as _codegraph_schema  # noqa: F401
from .codegraph import sqlite_adapter as _codegraph_sqlite_adapter  # noqa: F401
from .codegraph import store as _codegraph_store  # noqa: F401
from .codegraph import languages as _codegraph_languages  # noqa: F401
from .lock import base_lock, mutation_lock
from .engine import classify, rerank
from .engine import frontmatter as _fm
from .engine.config import Config, ConfigError
from .engine.embed import EmbedError, probe_embedding_endpoint
from .engine.idle import IdleTracker
from .engine.links import (
    CrossDomainRewrite,
    parse_link_targets,
    rewrite_cross_domain_links,
    rewrite_relative_anchors,
    slugify_heading,
    to_markdown_links,
)
from .engine.lint import lint
from .engine.okf_artifacts import RESERVED_OKF
from .engine.related import related
# Not used directly, but engine.search must join the startup import closure:
# store.query defers its import (cycle guard), and a module missing from the
# closure gets loaded lazily from disk — after an on-disk package upgrade that
# mixes new source with stale cached modules in a long-lived stdio process.
from .engine import search  # noqa: F401
from .engine.section import (
    SectionError, delete_section, insert_section, list_sections, move_section,
    replace_section, _locate,
)
from .engine.store import VectorStore
from .engine.validate import validate_page
from .resources import AUTHORING_RULES
from .storage import expected_revision_required, section_conflict


LOGGER = logging.getLogger(__name__)


class _ActivityReceiveStream:
    """Delegate a FastMCP input stream while observing received messages."""

    def __init__(self, stream, tracker: IdleTracker) -> None:
        self._stream = stream
        self._tracker = tracker

    async def receive(self):
        message = await self._stream.receive()
        self._tracker.touch()
        return message

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.receive()
        except anyio.EndOfStream as exc:
            raise StopAsyncIteration from exc

    async def __aenter__(self):
        await self._stream.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return await self._stream.__aexit__(exc_type, exc_value, traceback)

    def __getattr__(self, name):
        return getattr(self._stream, name)


class IdleFastMCP(FastMCP):
    """FastMCP stdio server with an optional inactivity shutdown."""

    def __init__(self, *args, **kwargs) -> None:
        self._idle_timeout_seconds = 0
        self._idle_tracker: IdleTracker | None = None
        super().__init__(*args, **kwargs)

    def set_idle_timeout(self, timeout_seconds: int) -> None:
        self._idle_timeout_seconds = timeout_seconds

    async def call_tool(self, name: str, arguments: dict):
        tracker = self._idle_tracker
        if tracker is None:
            return await super().call_tool(name, arguments)
        tracker.begin_call()
        try:
            return await super().call_tool(name, arguments)
        finally:
            tracker.end_call()

    async def run_stdio_async(self) -> None:
        if self._idle_timeout_seconds == 0:
            await super().run_stdio_async()
            return
        tracker = IdleTracker()
        self._idle_tracker = tracker
        try:
            async with stdio_server() as (read_stream, write_stream):
                tracked_stream = _ActivityReceiveStream(read_stream, tracker)
                async with anyio.create_task_group() as tasks:
                    async def serve() -> None:
                        await self._mcp_server.run(
                            tracked_stream,
                            write_stream,
                            self._mcp_server.create_initialization_options(),
                        )
                        tasks.cancel_scope.cancel()

                    tasks.start_soon(serve)
                    await tracker.wait_until_idle(self._idle_timeout_seconds)
                    print("iwiki-mcp: idle timeout expired; shutting down", file=sys.stderr)
                    tasks.cancel_scope.cancel()
        finally:
            self._idle_tracker = None


mcp = IdleFastMCP("iwiki")

SOURCE_CONTENT_MAX_BYTES = 200_000

_REMEDIATION_NEXT_STEPS = [
    "Regenerate stale wiki markdown from source semantics.",
    "Use wiki_update_page for compatible section-body edits.",
    "Use wiki_insert_section, wiki_delete_section, or wiki_move_section for "
    "section-level structural changes.",
    "Use wiki_delete_page then wiki_write_page when structural changes go "
    "beyond section-level (e.g. type migration).",
    "Use wiki_delete_page for missing_source delete candidates.",
    "Run wiki_lint and report planned, updated, deleted, failed, and remaining_lint.",
]

_UPDATE_REMEDIATION_TOOLS = [
    "wiki_update_page",
    "wiki_insert_section",
    "wiki_delete_section",
    "wiki_move_section",
    "wiki_delete_page",
    "wiki_write_page",
    "wiki_lint",
]

_DELETE_REMEDIATION_TOOLS = ["wiki_delete_page", "wiki_lint"]

_MUTATION_BINDING: ContextVar[base.Binding | base.PostgresBinding | None] = ContextVar(
    "iwiki_mutation_binding", default=None
)


class _HostedSelectedState:
    """Persist the domain scope explicitly selected for one HTTP session."""

    def __init__(self, binding: base.PostgresBinding) -> None:
        self._binding = binding
        self._lock = RLock()

    def locked(self):
        return self._lock

    def get(self) -> base.PostgresBinding:
        with self._lock:
            return self._binding

    def set(self, binding: base.PostgresBinding) -> None:
        with self._lock:
            self._binding = binding


class _HostedBindingState:
    """Hold one request's effective binding beside its persisted selection."""

    def __init__(
        self,
        selected: base.PostgresBinding | _HostedSelectedState,
        effective: base.PostgresBinding | None = None,
    ) -> None:
        if isinstance(selected, base.PostgresBinding):
            selected = _HostedSelectedState(selected)
        self._selected = selected
        self._effective = effective or selected.get()
        self._auth_context: _postgres_auth.AuthContext | None = None
        self._request_lock = anyio.Lock()

    def locked(self):
        return self._selected.locked()

    def get(self) -> base.PostgresBinding:
        return self._effective

    def set(self, binding: base.PostgresBinding) -> None:
        self._selected.set(binding)
        self._effective = binding

    def set_effective(
        self,
        binding: base.PostgresBinding,
        auth_context: _postgres_auth.AuthContext,
    ) -> None:
        self._effective = binding
        self._auth_context = auth_context

    def reset_effective(self) -> None:
        self._effective = self._selected.get()
        self._auth_context = None

    def auth_context(self) -> _postgres_auth.AuthContext | None:
        return self._auth_context

    def request_lock(self):
        return self._request_lock

    def selected_state(self) -> _HostedSelectedState:
        return self._selected

    def expand_domain(
        self,
        domain: str,
        auth_context: _postgres_auth.AuthContext,
    ) -> base.PostgresBinding:
        def expanded(values):
            return tuple(dict.fromkeys((*values, domain)))

        with self.locked():
            selected_current = self._selected.get()
            selected = replace(
                selected_current,
                read=expanded(selected_current.read),
                write=expanded(selected_current.write),
                primary=domain,
            )
            effective = replace(
                self._effective,
                read=expanded(self._effective.read),
                write=expanded(self._effective.write),
                primary=domain,
            )
            self._selected.set(selected)
            self._effective = effective
            self._auth_context = replace(
                auth_context,
                read_domains=expanded(auth_context.read_domains),
                write_domains=expanded(auth_context.write_domains),
                primary=domain,
                managed_domains=expanded(auth_context.managed_domains),
            )
            return effective


_SESSION_BINDING: ContextVar[
    base.PostgresBinding | _HostedBindingState | None
] = ContextVar(
    "iwiki_postgres_session_binding", default=None
)
_AUTH_CONTEXT: ContextVar[_postgres_auth.AuthContext | None] = ContextVar(
    "iwiki_postgres_auth_context", default=None
)
_LOCAL_POSTGRES_BINDING: base.PostgresBinding | None = None
_HOSTED_POOL = None
_HOSTED_CONFIG: Config | None = None
_HOSTED_CODE_GRAPH = None


def _install_hosted_runtime(pool, cfg: Config, code_graph=None) -> None:
    global _HOSTED_POOL, _HOSTED_CONFIG, _HOSTED_CODE_GRAPH
    _HOSTED_POOL = pool
    _HOSTED_CONFIG = cfg
    _HOSTED_CODE_GRAPH = code_graph


def _clear_hosted_runtime(pool) -> None:
    global _HOSTED_POOL, _HOSTED_CONFIG, _HOSTED_CODE_GRAPH
    if _HOSTED_POOL is pool:
        _HOSTED_POOL = None
        _HOSTED_CONFIG = None
        _HOSTED_CODE_GRAPH = None


def _resolved_binding() -> base.Binding | base.PostgresBinding:
    session = _SESSION_BINDING.get()
    if isinstance(session, _HostedBindingState):
        session = session.get()
    return (
        _MUTATION_BINDING.get()
        or session
        or _LOCAL_POSTGRES_BINDING
        or base.resolve_binding()
    )


def _is_postgres(binding) -> bool:
    return isinstance(binding, base.PostgresBinding)


def _request_auth_context() -> _postgres_auth.AuthContext | None:
    session = _SESSION_BINDING.get()
    if isinstance(session, _HostedBindingState):
        return session.auth_context()
    return _AUTH_CONTEXT.get()


def _postgres_store_for_binding(binding: base.PostgresBinding):
    request_context = _request_auth_context()
    if request_context is not None and request_context.iwiki_id == binding.iwiki_id:
        auth_context = replace(
            request_context,
            read_domains=tuple(binding.read),
            write_domains=tuple(binding.write),
            primary=binding.primary,
        )
    else:
        auth_context = _postgres_auth.AuthContext(
            iwiki_id=binding.iwiki_id,
            token_id="",
            read_domains=tuple(binding.read),
            write_domains=tuple(binding.write),
            primary=binding.primary,
        )
    return _postgres_store.PostgresStore(
        binding.connection_dsn(),
        binding.iwiki_id,
        _HOSTED_CONFIG or Config.load(),
        auth_context=auth_context,
        connection_factory=(
            _HOSTED_POOL.connection if _HOSTED_POOL is not None else None
        ),
        require_database_principal=True,
    )


def _postgres_auth_for_binding(binding: base.PostgresBinding):
    return _postgres_auth.AuthStore(
        binding.connection_dsn(),
        connection_factory=(
            _HOSTED_POOL.connection if _HOSTED_POOL is not None else None
        ),
    )


def _unsupported_storage() -> dict:
    return {
        "error": "unsupported_storage",
        "storage": "postgres",
        "hint": "use this tool with Git storage",
    }


def _unsupported_hosted_transport(binding) -> dict:
    return {
        "error": "unsupported_transport",
        "storage": "postgres" if _is_postgres(binding) else "git",
        "transport": (
            "streamable-http"
            if isinstance(_SESSION_BINDING.get(), _HostedBindingState)
            else "stdio"
        ),
        "hint": "use hosted Streamable HTTP with PostgreSQL storage",
    }


def _postgres_unsupported_guard(fn):
    @functools.wraps(fn)
    def wrap(*args, **kwargs):
        if _is_postgres(_resolved_binding()):
            return _unsupported_storage()
        return fn(*args, **kwargs)

    return wrap


def _creation_binding() -> base.Binding:
    """Resolve enough base context to bootstrap a configured missing domain."""
    project_dir = base.resolve_project_dir()
    config = base.load_project_config(project_dir)
    raw_base = config.get("base") or os.environ.get("IWIKI_BASE_DIR", "")
    wiki_base = str(raw_base).strip()
    if not wiki_base:
        raise base.BaseError(
            "no wiki base configured: set IWIKI_BASE_DIR or add `base` to .iwiki.toml"
        )
    raw_write = config.get("write")
    if isinstance(raw_write, str):
        raise base.BaseError("write must be an array of domains")
    write = base._unique_str_tuple(raw_write)
    primary = config.get("primary") or (write[0] if write else None)
    return base.Binding(
        base=os.path.abspath(os.path.expanduser(wiki_base)),
        read=base._as_str_tuple(config.get("read")),
        write=write,
        primary=primary,
        project_dir=project_dir,
    )


def _safe(fn):
    @functools.wraps(fn)
    def wrap(*a, **k):
        try:
            return fn(*a, **k)
        except _codegraph_models.CodeGraphError as e:
            return _codegraph_runtime.sanitized_error(e)
        except base.BaseError as e:
            return {
                "error": str(e),
                "hint": "set IWIKI_BASE_DIR or edit .iwiki.toml manually",
            }
        except (ConfigError, EmbedError) as e:
            if _SESSION_BINDING.get() is not None:
                return {
                    "error": "model operation failed",
                    "hint": "retry or inspect sanitized server diagnostics",
                }
            return {
                "error": f"HALT: {e}",
                "hint": "set IWIKI_LLM_BASE_URL / IWIKI_LLM_KEY",
            }
        except _postgres_store.psycopg.Error:
            return {
                "error": "PostgreSQL operation failed",
                "hint": "retry or inspect sanitized server diagnostics",
            }
        except _postgres_auth.AccessError:
            return {
                "error": "access_denied",
                "hint": "the authenticated context does not allow this operation",
            }
        except cross_domain.CrossDomainError as e:
            hint = {
                "write_scope_blocked": "add every visible referrer domain to write",
                "heading_collision": "choose a heading with a unique anchor",
                "target_collision": "delete or rename the colliding page first",
                "source_changed": "retry the complete operation against current Markdown",
                "manual_recovery_required": (
                    "resolve the retained transaction journal before retrying"
                ),
            }.get(e.code, "the mutation was rolled back; retry after checking wiki state")
            result = {"error": str(e), "code": e.code, "hint": hint}
            if e.code == "mutation_failed":
                result["rolled_back"] = True
            return result
        except Exception as e:
            if _SESSION_BINDING.get() is not None:
                return {
                    "error": "operation failed",
                    "hint": "retry or inspect sanitized server diagnostics",
                }
            return {"error": str(e), "hint": "unexpected error; see server logs"}

    return wrap


def _code_safe(fn):
    """Sanitize unexpected code-tool failures without changing Wiki tools."""
    @functools.wraps(fn)
    def wrap(*args, **kwargs):
        started = time.monotonic()
        try:
            return fn(*args, **kwargs)
        except _codegraph_models.CodeGraphError as exc:
            return _codegraph_runtime.sanitized_error(exc)
        except base.BaseError:
            return _missing_code_primary()
        except (ConfigError, EmbedError):
            raise
        except Exception:
            duration_ms = max(0, int((time.monotonic() - started) * 1000))
            LOGGER.error(
                "code_graph_handler code=rebuild_failed count=1 duration_ms=%d",
                duration_ms,
            )
            return {
                "error": "code graph rebuild failed",
                "code": "rebuild_failed",
                "hint": "inspect wiki_code_status and retry",
            }

    return wrap


def _mutation_guard(fn):
    @functools.wraps(fn)
    def wrap(*args, **kwargs):
        try:
            try:
                bind = _resolved_binding()
            except base.BaseError:
                if fn.__name__ != "wiki_create_domain":
                    raise
                bind = _creation_binding()
            if _is_postgres(bind):
                token = _MUTATION_BINDING.set(bind)
                try:
                    return fn(*args, **kwargs)
                finally:
                    _MUTATION_BINDING.reset(token)
            optional_domain = fn.__name__ in {
                "wiki_index",
                "wiki_migrate_okf",
                "wiki_export_okf",
            }
            supplied_domain = args[0] if args else kwargs.get("domain")
            if optional_domain and supplied_domain is None and bind.primary is None:
                token = _MUTATION_BINDING.set(bind)
                try:
                    return fn(*args, **kwargs)
                finally:
                    _MUTATION_BINDING.reset(token)
            with mutation_lock(bind.base):
                cross_domain.recover_pending_transactions(
                    bind.base,
                    finalize_committed=lambda manifest: (
                        cross_domain._recovery_graph_safe(bind.base, manifest)
                    ),
                )
                token = _MUTATION_BINDING.set(bind)
                try:
                    return fn(*args, **kwargs)
                finally:
                    _MUTATION_BINDING.reset(token)
        except cross_domain.CrossDomainError as exc:
            return {
                "error": str(exc),
                "code": exc.code,
                "hint": "resolve the retained transaction journal before retrying",
            }
        except base.BaseError as exc:
            return {
                "error": str(exc),
                "hint": "set IWIKI_BASE_DIR or edit .iwiki.toml manually",
            }

    return wrap


def _validate_domain(domain: str) -> str:
    if not domain:
        raise ValueError("invalid domain: empty")
    if domain.startswith("."):
        raise ValueError(f"invalid domain '{domain}'")
    if "/" in domain or "\\" in domain:
        raise ValueError(f"invalid domain '{domain}'")
    if domain in (".", ".."):
        raise ValueError(f"invalid domain '{domain}'")
    if Path(domain).is_absolute() or PureWindowsPath(domain).is_absolute():
        raise ValueError(f"invalid domain '{domain}'")
    if PureWindowsPath(domain).drive:
        raise ValueError(f"invalid domain '{domain}'")
    return domain


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _domain_path(b: str, domain: str) -> Path:
    base_path = Path(b).resolve()
    dom = Path(base.domain_dir(str(base_path), _validate_domain(domain)))
    if not _contains(base_path, dom):
        raise ValueError(f"invalid domain '{domain}'")
    return dom


def _existing_domain_write_guard(
    binding: base.Binding, domain: str
) -> tuple[Path, dict | None]:
    dom_path = _domain_path(binding.base, domain)
    if not dom_path.is_dir():
        return dom_path, None
    return dom_path, base.write_scope_error(binding, domain)


def _slug_parts(slug: str) -> tuple[str, ...]:
    if not slug:
        raise ValueError("invalid page slug: empty")
    if "\\" in slug:
        raise ValueError(f"invalid page slug '{slug}'")
    path = PurePosixPath(slug)
    win_path = PureWindowsPath(slug)
    if (
        path.is_absolute()
        or win_path.is_absolute()
        or win_path.drive
        or not path.parts
        or any(part in (".", "..") for part in path.parts)
    ):
        raise ValueError(f"invalid page slug '{slug}'")
    return path.parts


def _page_path(b: str, domain: str, slug: str) -> str:
    dom = _domain_path(b, domain)
    parts = _slug_parts(slug)
    path = dom.joinpath(*parts[:-1], parts[-1] + ".md")
    if not _contains(dom, path):
        raise ValueError(f"invalid page slug '{slug}'")
    return str(path)


def _resolve_identity(slug: str, resolved_type: str) -> str:
    """Domain-relative identity '<type>/<tail>'. A bare slug is prefixed with the
    resolved type; a slug that already carries a leading segment must match it.
    The resolved type must be a safe SINGLE path segment (guards the invariant
    'first path segment == frontmatter type': normalize_type lowercases but does
    NOT reject '/' or a leading '.', so validate it here)."""
    if (not resolved_type or "/" in resolved_type or "\\" in resolved_type
            or resolved_type.startswith(".")):
        raise ValueError(
            f"invalid frontmatter type '{resolved_type}': must be a safe single "
            "path segment (no '/', '\\', or leading '.')")
    parts = _slug_parts(slug)
    if len(parts) == 1:
        return f"{resolved_type}/{parts[0]}"
    if parts[0] != resolved_type:
        raise ValueError(
            f"slug type-segment '{parts[0]}' does not match frontmatter type "
            f"'{resolved_type}'")
    return PurePosixPath(*parts).as_posix()


def _normalize_source(project_dir: str, source: str) -> str:
    """Store the ingest source relative to the project. A relative path is
    resolved against the project dir and confirmed to stay inside it (rejects
    an escape via '..'); an absolute path under the project is relativized; a
    path (relative or absolute) that resolves outside the project is rejected
    (the server works only within the bound project)."""
    proj = Path(project_dir).resolve()
    p = Path(source)
    resolved = p.resolve() if p.is_absolute() else (proj / p).resolve()
    try:
        return resolved.relative_to(proj).as_posix()
    except ValueError:
        raise ValueError("source outside project")


def _source_within_project(project_dir: str, source: str) -> bool:
    """Read-path containment guard, mirroring _normalize_source: True iff
    `source` resolves inside project_dir. Sources reaching wiki_remediation_plan
    come from the on-disk ingest log/lint report, which may hold a record
    _normalize_source never validated (synced-in from the shared git base,
    a pre-fix legacy entry, or a manual edit) -- so re-check before opening it."""
    try:
        _normalize_source(project_dir, source)
        return True
    except ValueError:
        return False


def _slug_from_page_path(dom_path: Path, page_path: str) -> str:
    rel = Path(page_path).resolve().relative_to(dom_path.resolve())
    if rel.suffix != ".md":
        raise ValueError(f"invalid page path '{page_path}'")
    return rel.with_suffix("").as_posix()


def _h2_headings(markdown: str) -> list[str]:
    return [
        m.group(1).strip()
        for m in re.finditer(r"^##\s+(.*?)\s*$", markdown, re.MULTILINE)
    ]


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _read_source_preview(path: str) -> tuple[str, int, bool]:
    with open(path, "rb") as fh:
        data = fh.read(SOURCE_CONTENT_MAX_BYTES + 1)
    truncated = len(data) > SOURCE_CONTENT_MAX_BYTES
    if truncated:
        data = data[:SOURCE_CONTENT_MAX_BYTES]
    return data.decode("utf-8", errors="replace"), os.path.getsize(path), truncated


@_safe
def wiki_status() -> dict:
    bind = _resolved_binding()
    if _is_postgres(bind):
        domains = [
            domain
            for domain in _postgres_store_for_binding(bind).list_domains()
            if domain in bind.read
        ]
        result = {
            "storage": "postgres",
            "transport": (
                "streamable-http"
                if _SESSION_BINDING.get() is not None
                else "stdio"
            ),
            "read": list(bind.read),
            "write": list(bind.write),
            "primary": bind.primary,
            "domains": domains,
        }
        if _SESSION_BINDING.get() is None:
            result["project_dir"] = bind.project_dir
        return result
    return {
        "base": bind.base,
        "read": list(bind.read),
        "write": list(bind.write),
        "primary": bind.primary,
        "project_dir": bind.project_dir,
        "domains": base.list_domains(bind.base),
    }


def _missing_code_primary() -> dict:
    return {
        "error": "code graph is not configured",
        "code": "not_configured",
        "hint": "configure a primary domain and enable code_graph",
    }


def _invalid_code_config() -> dict:
    return {
        "error": "code graph configuration is invalid",
        "code": "invalid_config",
        "hint": "inspect code_graph project configuration",
    }


_CODE_SOURCE_UNAVAILABLE = {
    "error": "source_unavailable",
    "hint": (
        "run wiki_code_index on a local MCP server with the repository checkout"
    ),
}
_CODE_UNAUTHORIZED = {
    "error": "unauthorized",
    "hint": "publish through a writable bound primary",
}


def _hosted_code_graph_settings():
    """Return the hosted code-graph bounds a remote client cannot raise."""
    from .postgres.config import HostedCodeGraphConfig

    return _HOSTED_CODE_GRAPH or HostedCodeGraphConfig()


def _postgres_code_reader(binding: base.PostgresBinding):
    settings = _hosted_code_graph_settings()
    return _postgres_codegraph.PostgresCodeGraphReader(
        binding.connection_dsn(),
        binding.iwiki_id,
        binding.primary,
        max_snapshot_age_seconds=settings.max_snapshot_age_seconds,
    )


# Bound wait for the domain activation lock. It is deliberately separate from
# the server statement/lock timeouts: activation contends only with another
# publication of the same domain and must fail fast as retryable busy.
_CODE_PUBLICATION_LOCK_TIMEOUT_MS = 5000


def _postgres_code_store(binding: base.PostgresBinding, owner_id: str):
    return _codegraph_application.create_postgres_publisher(
        binding,
        owner_id,
        _hosted_code_graph_settings(),
        lock_timeout_ms=_CODE_PUBLICATION_LOCK_TIMEOUT_MS,
    )


def _session_reference(session_id: str):
    return _codegraph_publication.PublicationSession(
        session_id=session_id,
        lease_expires_at="",
        base_snapshot_revision=None,
        base_markdown_token=0,
    )


class _UnsupportedPublication:
    """Answer every publication call with one fixed safe refusal."""

    def __init__(self, result: dict) -> None:
        self._result = result

    def begin_from_mapping(self, _header) -> dict:
        return dict(self._result)

    def publish_from_mapping(self, *_arguments) -> dict:
        return dict(self._result)

    def finalize_from_mapping(self, _session_id) -> dict:
        return dict(self._result)

    def abort_from_mapping(self, _session_id) -> dict:
        return dict(self._result)


class _HostedPublication:
    """Validate remote publication input before any PostgreSQL dispatch."""

    def __init__(self, store, settings) -> None:
        self._store = store
        self._settings = settings

    def begin_from_mapping(self, header) -> dict:
        if not isinstance(header, dict):
            return dict(_CODE_INVALID_HEADER)
        try:
            parsed = _codegraph_publication.SnapshotHeader(
                protocol_version=header["protocol_version"],
                schema_version=header["schema_version"],
                repository_id=header["repository_id"],
                source_fingerprint=header["source_fingerprint"],
                parser_fingerprint=header["parser_fingerprint"],
                normalizer_version=header["normalizer_version"],
                unicode_data_version=header["unicode_data_version"],
                languages=tuple(header["languages"]),
                expected_counts=header["expected_counts"],
                graph_payload_revision=header["graph_payload_revision"],
            )
        except (KeyError, TypeError, ValueError):
            return dict(_CODE_INVALID_HEADER)
        if parsed.repository_id != self._store.domain:
            return {
                "error": "scope_mismatch",
                "hint": "publish the bound primary domain only",
            }
        session = self._store.begin(parsed)
        if isinstance(session, dict):
            return session
        return {
            "session_id": session.session_id,
            "lease_expires_at": session.lease_expires_at,
            "base_snapshot_revision": session.base_snapshot_revision,
            "base_markdown_token": session.base_markdown_token,
            "max_batch_rows": self._settings.max_batch_rows,
            "max_batch_bytes": self._settings.max_batch_bytes,
        }

    def publish_from_mapping(
        self, session_id, kind, ordinal, rows, payload_hash
    ) -> dict:
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) for row in rows
        ):
            return dict(_CODE_INVALID_BATCH)
        if len(rows) > self._settings.max_batch_rows:
            return {
                **_CODE_INVALID_BATCH,
                "limit": self._settings.max_batch_rows,
                "received": len(rows),
            }
        try:
            batch = _codegraph_publication.canonical_batch(kind, ordinal, rows)
        except (TypeError, ValueError):
            return dict(_CODE_INVALID_BATCH)
        if batch.payload_hash != payload_hash:
            return dict(_CODE_INVALID_BATCH)
        if batch.byte_count > self._settings.max_batch_bytes:
            return {
                **_CODE_INVALID_BATCH,
                "limit": self._settings.max_batch_bytes,
                "received": batch.byte_count,
            }
        return self._store.publish_batch(_session_reference(session_id), batch)

    def finalize_from_mapping(self, session_id) -> dict:
        return self._store.finalize(_session_reference(session_id))

    def abort_from_mapping(self, session_id) -> dict:
        return self._store.abort(_session_reference(session_id))


_CODE_INVALID_BATCH = {
    "error": "invalid_batch",
    "hint": "send batches that match the declared header",
}
_CODE_INVALID_HEADER = {
    "error": "snapshot_incomplete",
    "hint": "publish every expected batch before finalizing",
}


def _code_publication_service(binding):
    """Resolve the only publication target this authenticated call may use."""
    if not _is_postgres(binding):
        return _UnsupportedPublication(_unsupported_storage())
    context = _request_auth_context()
    if context is None or not context.token_id:
        return _UnsupportedPublication(_unsupported_hosted_transport(binding))
    if binding.primary is None or binding.primary not in binding.write:
        return _UnsupportedPublication(dict(_CODE_UNAUTHORIZED))
    return _HostedPublication(
        _postgres_code_store(binding, context.token_id),
        _hosted_code_graph_settings(),
    )


@_safe
@_code_safe
def wiki_code_status() -> dict:
    bind = _resolved_binding()
    if _is_postgres(bind):
        if bind.primary is None:
            return _missing_code_primary()
        return _postgres_code_reader(bind).status()
    if bind.primary is None:
        return _missing_code_primary()
    return _codegraph_application.code_runtime(
        _codegraph_application.source_context(bind)
    ).status()


@_safe
@_code_safe
def wiki_code_index(
    force: bool = False,
    languages: list[str] | None = None,
) -> dict:
    if languages is not None and (
        not languages
        or any(
            language not in _codegraph_config.KNOWN_LANGUAGES
            for language in languages
        )
    ):
        return _invalid_code_config()
    bind = _resolved_binding()
    if _is_postgres(bind):
        return dict(_CODE_SOURCE_UNAVAILABLE)
    if bind.primary is None:
        return _missing_code_primary()
    return _codegraph_application.index_and_publish(
        bind, force=force, languages=languages
    ).tool_result()


@_safe
@_code_safe
def wiki_code_search(
    query: str,
    kinds: list[str] | None = None,
    path: str | None = None,
    languages: list[str] | None = None,
    limit: int = 20,
) -> dict:
    # Binding-free fail-fast gate: catches malformed query/kinds/limit/path
    # input before any binding resolution (tested by
    # test_search_validation_precedes_binding_for_all_text_bounds). It
    # can't know the project's real configured languages yet -- passing
    # KNOWN_LANGUAGES here (rather than the module default ("python",))
    # keeps this gate from wrongly rejecting a "typescript" filter before
    # the project's actual code_graph.languages config gets a say; the
    # real per-project check still happens below (postgres path) or inside
    # CodeGraphRuntime.search (git/sqlite path, already wired in runtime.py).
    _codegraph_runtime.validate_search_request(
        query,
        kinds=kinds,
        path=path,
        languages=languages,
        configured_languages=tuple(sorted(_codegraph_config.KNOWN_LANGUAGES)),
        limit=limit,
    )
    bind = _resolved_binding()
    if _is_postgres(bind):
        if bind.primary is None:
            return _missing_code_primary()
        # Hosted reads answer from the published snapshot, so the snapshot
        # header -- not this server's project directory, which for the HTTP
        # transport is just wherever server.toml lives -- decides which
        # languages a filter may name. The reader resolves the active
        # snapshot and calls back with its declared languages.
        return _postgres_code_reader(bind).search(
            lambda snapshot_languages: _codegraph_runtime.validate_search_request(
                query,
                kinds=kinds,
                path=path,
                languages=languages,
                configured_languages=snapshot_languages,
                languages_source="snapshot",
                limit=limit,
            )
        )
    if bind.primary is None:
        return _missing_code_primary()
    return _codegraph_application.code_runtime(
        _codegraph_application.source_context(bind)
    ).search(
        query,
        kinds=kinds,
        path=path,
        languages=languages,
        limit=limit,
    )


@_safe
@_code_safe
def wiki_code_context(
    seeds: list[str],
    direction: Literal["in", "out", "both"] = "both",
    depth: int = 1,
    relations: list[str] | None = None,
    include_source: bool = False,
    include_wiki: bool = True,
    max_nodes: int = 50,
    max_files: int = 20,
    max_source_bytes: int = 200_000,
) -> dict:
    _codegraph_runtime.validate_context_request(
        seeds,
        direction=direction,
        depth=depth,
        relations=relations,
        include_source=include_source,
        include_wiki=include_wiki,
        max_nodes=max_nodes,
        max_files=max_files,
        max_source_bytes=max_source_bytes,
    )
    bind = _resolved_binding()
    if _is_postgres(bind):
        if bind.primary is None:
            return _missing_code_primary()
        return _postgres_code_reader(bind).context(
            _codegraph_runtime.validate_context_request(
                seeds,
                direction=direction,
                depth=depth,
                relations=relations,
                include_source=include_source,
                include_wiki=include_wiki,
                max_nodes=max_nodes,
                max_files=max_files,
                max_source_bytes=max_source_bytes,
            )
        )
    if bind.primary is None:
        return _missing_code_primary()
    return _codegraph_application.code_runtime(
        _codegraph_application.source_context(bind)
    ).context(
        seeds,
        direction=direction,
        depth=depth,
        relations=relations,
        include_source=include_source,
        include_wiki=include_wiki,
        max_nodes=max_nodes,
        max_files=max_files,
        max_source_bytes=max_source_bytes,
    )


@_safe
@_code_safe
def wiki_code_publish_begin(header: dict) -> dict:
    """Open one owned publication session for the authenticated primary."""
    return _code_publication_service(_resolved_binding()).begin_from_mapping(
        header
    )


@_safe
@_code_safe
def wiki_code_publish_batch(
    session_id: str,
    kind: str,
    ordinal: int,
    rows: list[dict],
    payload_hash: str,
) -> dict:
    """Accept one canonical row batch of an owned publication session."""
    return _code_publication_service(_resolved_binding()).publish_from_mapping(
        session_id, kind, ordinal, rows, payload_hash
    )


@_safe
@_code_safe
def wiki_code_publish_finalize(session_id: str) -> dict:
    """Recompute revisions and activate one complete staged snapshot."""
    return _code_publication_service(_resolved_binding()).finalize_from_mapping(
        session_id
    )


@_safe
@_code_safe
def wiki_code_publish_abort(session_id: str) -> dict:
    """Discard one owned staging session without touching the active snapshot."""
    return _code_publication_service(_resolved_binding()).abort_from_mapping(
        session_id
    )


@_safe
def wiki_list_domains() -> dict:
    bind = _resolved_binding()
    if _is_postgres(bind):
        domains = [
            domain
            for domain in _postgres_store_for_binding(bind).list_domains()
            if domain in bind.read
        ]
        return {
            "domains": domains,
            "detail": [{"domain": domain, "index_bytes": 0} for domain in domains],
        }
    out = []
    for d in base.list_domains(bind.base):
        base.migrate_store_location(bind.base, d)
        out.append(
            {"domain": d, "index_bytes": _index_bytes(base.index_path(bind.base, d))}
        )
    return {"domains": [d["domain"] for d in out], "detail": out}


def _index_bytes(path: str) -> int:
    return os.path.getsize(path) if os.path.exists(path) else 0


@_safe
def wiki_list_pages(domain: str) -> dict:
    bind = _resolved_binding()
    if _is_postgres(bind):
        valid_domain = _validate_domain(domain)
        if valid_domain not in bind.read:
            return {
                "error": f"domain '{valid_domain}' is outside bound read scope",
                "hint": "narrow or update the authorized read scope",
            }
        store = _postgres_store_for_binding(bind)
        if valid_domain not in store.list_domains():
            return {
                "error": f"domain '{valid_domain}' not found",
                "hint": "ask an administrator to create the domain",
            }
        pages = store.list_pages(valid_domain)
        return {
            "domain": valid_domain,
            "pages": [
                {"slug": slug, "file": f"{slug}.md"} for slug in pages
            ],
        }
    dom_path = _domain_path(bind.base, domain)
    if not dom_path.is_dir():
        return {
            "error": f"domain '{domain}' not found",
            "hint": "create it with wiki_create_domain",
        }
    pages = []
    for path in sorted(dom_path.rglob("*.md")):
        rel_path = path.relative_to(dom_path)
        if rel_path.as_posix() in RESERVED_OKF:
            continue
        rel = rel_path.as_posix()
        pages.append({"slug": rel[:-3], "file": rel})
    return {"domain": domain, "pages": pages}


@_safe
def wiki_read_page(domain: str, slug: str, heading: str | None = None) -> dict:
    bind = _resolved_binding()
    if _is_postgres(bind):
        valid_domain = _validate_domain(domain)
        _slug_parts(slug)
        if valid_domain not in bind.read:
            return {
                "error": f"domain '{valid_domain}' is outside bound read scope",
                "hint": "narrow or update the authorized read scope",
            }
        page = _postgres_store_for_binding(bind).read_page(valid_domain, slug)
        if page is None:
            return {
                "error": f"page '{valid_domain}/{slug}' not found",
                "hint": "list pages with wiki_list_pages",
            }
        if heading is None:
            return page
        _, body = _fm.split(page["markdown"], strict_code=True)
        return _read_section(domain, slug, body, heading)
    path = _page_path(bind.base, domain, slug)
    if not os.path.isfile(path):
        return {
            "error": f"page '{domain}/{slug}' not found",
            "hint": "list pages with wiki_list_pages",
        }
    markdown = open(path, encoding="utf-8").read()
    if heading is None:
        return {"domain": domain, "slug": slug, "markdown": markdown}
    _, body = _fm.split(markdown, strict_code=True)
    return _read_section(domain, slug, body, heading)


def _read_section(domain: str, slug: str, body: str, heading: str) -> dict:
    try:
        sections = list_sections(body)
        idx = _locate(sections, heading)
    except SectionError as exc:
        return {"error": str(exc), "hint": "check the heading with wiki_read_page"}
    section = sections[idx]
    section_hash = sha256(section.body.strip("\n").encode("utf-8")).hexdigest()[:16]
    return {
        "domain": domain,
        "slug": slug,
        "heading": section.heading,
        "body": section.body.strip("\n"),
        "section_hash": section_hash,
    }


def _check_section_hash(body: str, heading: str, expected: str | None) -> dict | None:
    """Return a section_conflict dict if `expected` doesn't match, else None.

    Raises SectionError (propagated like other section lookups) if the
    heading is missing/ambiguous; callers catch that alongside their
    existing replace/delete/move SectionError handling.
    """
    if expected is None:
        return None
    sections = list_sections(body)
    idx = _locate(sections, heading)
    current_hash = sha256(sections[idx].body.strip("\n").encode("utf-8")).hexdigest()[:16]
    if current_hash != expected:
        return section_conflict(current_hash)
    return None


@_safe
def wiki_search(
    query: str,
    scope: str = "project",
    mode: Literal["hybrid", "lexical", "semantic"] | None = None,
    domains: list[str] | None = None,
    k: int | None = None,
    threshold: float | None = None,
    type: str | None = None,
    tags: list[str] | None = None,
    intent: str = "read",
    heading: str | None = None,
) -> dict:
    bind = _resolved_binding()
    cfg = Config.load()
    if intent.strip().lower() == "write":
        target = bind.primary or (domains[0] if domains else None)
        if not target:
            return {"target": {"exists": False}, "hint": "no write-target domain in scope"}
        target = _validate_domain(target)      # path guards are load-bearing
        if _is_postgres(bind):
            if target not in bind.write:
                return {"target": {"domain": target, "exists": False}}
            return {
                "target": _postgres_store_for_binding(bind).locate_target(
                    target, query, heading
                )
            }
        return {"target": retrieval.locate_target(cfg, bind.base, target, query, heading)}
    resolved_mode = cfg.search_mode if mode is None else mode.strip().lower()
    allowed_modes = ("hybrid", "lexical", "semantic")
    if resolved_mode not in allowed_modes:
        return {"error": "invalid search mode; allowed values: hybrid, lexical, semantic"}
    if _is_postgres(bind):
        requested = list(domains) if domains is not None else list(bind.read)
        doms = [
            _validate_domain(domain)
            for domain in requested
            if domain in bind.read
        ]
    else:
        doms = [_validate_domain(d) for d in base.resolve_scope(bind, scope, domains)]
    if not doms:
        return {"results": [], "hint": "no domains in scope"}
    q_type = (type.strip().lower() or None) if type else None
    q_tags = _fm.normalize_tags(tags) if tags else None
    q_tags = q_tags or None
    requested_top_k = cfg.top_k if k is None else k
    page_cache = {}
    try:
        if _is_postgres(bind):
            candidates = _postgres_store_for_binding(bind).prepare_read_candidates(
                doms,
                query,
                top_k=requested_top_k,
                threshold=cfg.score_threshold if threshold is None else threshold,
                mode=resolved_mode,
                type=q_type,
                tags=q_tags,
            )
        else:
            candidates = retrieval.prepare_read_candidates(
                cfg,
                bind.base,
                doms,
                query,
                top_k=requested_top_k,
                threshold=cfg.score_threshold if threshold is None else threshold,
                mode=resolved_mode,
                type=q_type,
                tags=q_tags,
                page_cache=page_cache,
            )
    except EmbedError as exc:
        if _SESSION_BINDING.get() is not None:
            return {
                "error": "model operation failed",
                "hint": "retry or inspect sanitized server diagnostics",
            }
        return {"error": str(exc)}
    results = candidates[:requested_top_k]
    response = {"results": results}
    if cfg.rerank_model:
        if _is_postgres(bind):
            hydrated = _postgres_store_for_binding(bind).hydrate_candidates(candidates)
        else:
            hydrated = retrieval.hydrate_candidates(
                cfg, bind.base, candidates, page_cache=page_cache
            )
        ranked, metadata = rerank.rerank_candidates(
            cfg, query, hydrated, top_n=requested_top_k
        )
        if metadata["applied"]:
            scored_count = metadata.pop("_scored_count", len(ranked))
            scored = ranked[:scored_count]
            scored_keys = {
                (item["domain"], item["file"], item["heading"], item["chunk"])
                for item in scored
            }
            unscored = [
                item for item in candidates
                if (item["domain"], item["file"], item["heading"], item["chunk"])
                not in scored_keys
            ]
            results = (scored + unscored)[:requested_top_k]
        response = {"results": results, "rerank": metadata}
    return response


@_safe
def wiki_related(domain: str, section_id: str) -> dict:
    bind = _resolved_binding()
    cfg = Config.load()
    valid_domain = _validate_domain(domain)
    if _is_postgres(bind):
        if valid_domain not in bind.read:
            return {
                "error": f"domain '{valid_domain}' is outside bound read scope",
                "hint": "narrow or update the authorized read scope",
            }
        return _postgres_store_for_binding(bind).related(valid_domain, section_id)
    dom_path = _domain_path(bind.base, valid_domain)
    base.migrate_store_location(bind.base, valid_domain)
    recs = VectorStore(base.index_path(bind.base, valid_domain)).load()
    cwd = os.getcwd()
    try:
        os.chdir(dom_path)
        return related(section_id, recs, cfg.top_k, cfg.graph_depth)
    finally:
        os.chdir(cwd)


_BLOCKING = {"deep_heading", "pre_h2_text"}

# Required on PostgreSQL storage (optimistic locking via `wiki_read_page`'s
# `revision`); unused and always omitted on Git storage.
_ExpectedRevision = Annotated[
    int | None,
    Field(
        description=(
            "Required on PostgreSQL storage: pass the page's current `revision` "
            "from `wiki_read_page`, or the call is rejected with "
            "`expected_revision_required`. Omit on Git storage, which has no "
            "revision counter."
        )
    ),
]


def _rollback_last_log(
    b: str, domain: str, op: str, page: str, source: str, src_hash: str | None
) -> None:
    path = base.log_path(b, domain)
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
        if not lines:
            return
        rec = json.loads(lines[-1])
        if (
            rec.get("op") != op
            or rec.get("page") != page
            or rec.get("source") != source
            or rec.get("src_hash") != src_hash
        ):
            return
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(lines[:-1])
    except Exception:
        return


def _restore_log(path: str, before: bytes | None) -> None:
    """Restore the ingest log to its pre-edit bytes (or remove it if it did not
    exist), for wiki_update_page rollback of a whole-file log upsert."""
    try:
        if before is None:
            if os.path.exists(path):
                os.remove(path)
        else:
            with open(path, "wb") as fh:
                fh.write(before)
    except OSError:
        pass


_DIVERGED = {
    "error": "base diverged from remote",
    "hint": "run wiki_sync to reconcile (pull --rebase + push), "
            "or resolve the conflict in the base repo, then retry",
}


def _fresh_warn(fresh: dict) -> dict:
    """Freshness warning as a spreadable dict fragment ({} when there is none)."""
    w = fresh.get("warning")
    return {"warning": w} if w else {}


def _compose_warnings(*warnings: str | None) -> str | None:
    parts: list[str] = []
    for warning in warnings:
        if not warning:
            continue
        for part in warning.split("; "):
            if part and part not in parts:
                parts.append(part)
    return "; ".join(parts) or None


_SPECIFICATION_WARNING = "specification projection is stale"
_SPECIFICATION_BLOCKING = {
    "missing_scenario",
    "invalid_scenario",
    "incomplete_bindings",
    "duplicate_scenario_id",
}


def _git_specification_store_factory(base_dir: str, mode: str):
    return GitSpecificationStore(base_dir, mode)


def _assemble_specification_projection(
    domain: str,
    pages: tuple[_specifications.PageSnapshot, ...],
    previous_evidence=(),
):
    specification_pages = tuple(
        page for page in pages
        if _is_specification_markdown(page.markdown)
    )
    revision = semantic_markdown_revision(
        (page.slug, page.markdown, page.revision)
        for page in specification_pages
    )
    return _specifications.assemble_projection(
        domain,
        pages,
        previous_evidence=previous_evidence,
        markdown_revision=revision,
    )


def _is_specification_markdown(markdown: str) -> bool:
    metadata, _ = _fm.split(markdown)
    page_type = metadata.get("type")
    return (
        isinstance(page_type, str)
        and _fm.normalize_type(page_type) == "specification"
    )


def _specification_finding_dict(finding) -> dict:
    result = {"type": finding.type}
    for name in ("slug", "heading", "scenario_id", "reason"):
        value = getattr(finding, name)
        if value is not None:
            result[name] = value
    if finding.missing:
        result["missing"] = list(finding.missing)
    if finding.locations:
        result["locations"] = [
            {
                "slug": item.slug,
                "heading": item.heading,
                "anchor": item.anchor,
            }
            for item in finding.locations
        ]
    return result


def _specification_result(mode: str, state: str, projection=None) -> dict:
    result = {"mode": mode, "state": state, "findings": []}
    if projection is not None:
        result.update({
            "scenarios": projection.scenario_count,
            "bindings": projection.binding_count,
            "findings": [
                _specification_finding_dict(item)
                for item in projection.findings
            ],
        })
    return result


def _domain_page_snapshots(
    base_dir: str,
    domain: str,
    *,
    target_slug: str | None = None,
    candidate_markdown: str | None = None,
    delete_target: bool = False,
    planned_markdown: dict[str, str | None] | None = None,
) -> tuple[_specifications.PageSnapshot, ...]:
    domain_path = Path(base.domain_dir(base_dir, domain))
    pages = []
    found_target = False
    found_planned: set[str] = set()
    for path in sorted(domain_path.rglob("*.md")):
        slug = path.relative_to(domain_path).as_posix()[:-3]
        if f"{slug}.md" in RESERVED_OKF:
            continue
        if planned_markdown is not None and slug in planned_markdown:
            found_planned.add(slug)
            markdown = planned_markdown[slug]
        elif slug == target_slug:
            found_target = True
            if delete_target:
                continue
            markdown = candidate_markdown
        else:
            markdown = path.read_text(encoding="utf-8")
        if markdown is None:
            continue
        pages.append(_specifications.PageSnapshot(
            slug=slug,
            markdown=markdown,
            revision=f"sha256:{sha256(markdown.encode('utf-8')).hexdigest()}",
        ))
    if planned_markdown is not None:
        for slug in sorted(set(planned_markdown) - found_planned):
            markdown = planned_markdown[slug]
            if markdown is None:
                continue
            pages.append(_specifications.PageSnapshot(
                slug=slug,
                markdown=markdown,
                revision=(
                    f"sha256:{sha256(markdown.encode('utf-8')).hexdigest()}"
                ),
            ))
    elif target_slug is not None and not found_target and not delete_target:
        if candidate_markdown is None:
            raise ValueError("specification candidate is missing")
        pages.append(_specifications.PageSnapshot(
            slug=target_slug,
            markdown=candidate_markdown,
            revision=(
                f"sha256:{sha256(candidate_markdown.encode('utf-8')).hexdigest()}"
            ),
        ))
    return tuple(sorted(pages, key=lambda item: item.slug))


def _finding_targets_slug(finding, slug: str) -> bool:
    if finding.slug == slug:
        return True
    return any(item.slug == slug for item in finding.locations)


def _prepare_git_specification(
    binding: base.Binding,
    domain: str,
    *,
    target_slug: str | None = None,
    candidate_markdown: str | None = None,
    original_markdown: str | None = None,
    delete_target: bool = False,
    rebuild: bool = False,
):
    mode = binding.specification_mode
    if mode == "disabled":
        return None
    target_is_specification = (
        original_markdown is not None
        and _is_specification_markdown(original_markdown)
        if delete_target
        else candidate_markdown is not None
        and _is_specification_markdown(candidate_markdown)
    )
    if not rebuild and not target_is_specification:
        return None
    return {
        "binding": binding,
        "domain": domain,
        "mode": mode,
        "target_slug": target_slug,
        "candidate_markdown": candidate_markdown,
        "original_markdown": original_markdown,
        "delete_target": delete_target,
        "rebuild": rebuild,
    }


def _resolve_git_specification_locked(request: dict):
    binding = request["binding"]
    domain = request["domain"]
    mode = request["mode"]
    target_slug = request["target_slug"]
    candidate_markdown = request["candidate_markdown"]
    original_markdown = request["original_markdown"]
    delete_target = request["delete_target"]
    rebuild = request["rebuild"]
    planned_markdown = request.get("planned_markdown")
    if target_slug is not None and planned_markdown is None:
        target_path = Path(_page_path(binding.base, domain, target_slug))
        current_markdown = (
            target_path.read_text(encoding="utf-8")
            if target_path.is_file()
            else None
        )
        if original_markdown is None:
            if current_markdown is not None:
                return {
                    "error": "specification candidate changed",
                    "specifications": _specification_result(mode, "failed"),
                }
        elif current_markdown != original_markdown:
            return {
                "error": "specification candidate changed",
                "specifications": _specification_result(mode, "failed"),
            }
    projection_path = Path(base.specifications_path(binding.base, domain))
    pages = _domain_page_snapshots(
        binding.base,
        domain,
        target_slug=target_slug,
        candidate_markdown=candidate_markdown,
        delete_target=delete_target,
        planned_markdown=planned_markdown,
    )
    if rebuild and not projection_path.exists() and not any(
        _is_specification_markdown(page.markdown) for page in pages
    ):
        return None
    store = _git_specification_store_factory(binding.base, mode)
    try:
        previous = store._load(domain)
        projection = _assemble_specification_projection(
            domain,
            pages,
            previous_evidence=() if previous is None else previous.evidence,
        )
    except Exception:
        if mode == "strict" and not rebuild:
            return {
                "error": "specification projection preparation failed",
                "specifications": _specification_result("strict", "failed"),
            }
        return {
            "mode": mode,
            "projection": None,
            "prepared": None,
            "warning": _SPECIFICATION_WARNING,
            "fail_soft": True,
        }
    if (
        mode == "strict"
        and not rebuild
        and target_slug is not None
        and any(
            item.type in _SPECIFICATION_BLOCKING
            and _finding_targets_slug(item, target_slug)
            for item in projection.findings
        )
    ):
        return {
            "error": "specification validation failed",
            "specifications": _specification_result("strict", "failed", projection),
        }
    try:
        prepared = store.prepare(projection)
    except Exception:
        if mode == "strict" and not rebuild:
            return {
                "error": "specification projection preparation failed",
                "specifications": _specification_result(
                    "strict", "failed", projection
                ),
            }
        prepared = None
    return {
        "mode": mode,
        "projection": projection,
        "prepared": prepared,
        "warning": None if prepared is not None else _SPECIFICATION_WARNING,
        "fail_soft": rebuild or mode == "optional",
    }


def _snapshot_files(paths: tuple[Path, ...]) -> dict[Path, bytes | None]:
    return {
        path: path.read_bytes() if path.is_file() else None
        for path in paths
    }


def _restore_files(snapshot: dict[Path, bytes | None]) -> None:
    for path, content in snapshot.items():
        if content is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _remove_empty_page_parents(page_path: Path, domain_path: Path) -> None:
    parent = page_path.parent
    while parent != domain_path:
        try:
            parent.rmdir()
        except OSError:
            return
        parent = parent.parent


def _relative_transaction_paths(
    base_dir: str, paths: tuple[Path, ...]
) -> tuple[str, ...]:
    root = Path(base_dir).resolve()
    return tuple(sorted(
        path.resolve(strict=False).relative_to(root).as_posix()
        for path in paths
    ))


def _has_unrelated_domain_changes(
    base_dir: str, domain: str, allowed: tuple[str, ...]
) -> bool:
    status = sync._run(
        base_dir,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        domain,
    )
    if status.returncode != 0:
        return True
    allowed_set = set(allowed)
    records = status.stdout.split("\0")
    for record in records:
        if not record:
            continue
        path = record[3:] if len(record) >= 3 else record
        if path and path not in allowed_set:
            return True
    return False


def _execute_git_specification_transaction(
    binding: base.Binding,
    domain: str,
    prepared_specification: dict,
    *,
    page_path: str | None,
    mutate_page,
    cfg: Config,
    message: str,
    after_commit=None,
    mutates_log: bool = False,
) -> dict:
    projection_path = Path(base.specifications_path(binding.base, domain))
    paths = tuple(dict.fromkeys(path for path in (
        Path(page_path) if page_path is not None else None,
        Path(base.index_path(binding.base, domain)),
        Path(base.log_path(binding.base, domain)),
        projection_path,
    ) if path is not None))
    commit_files = tuple(
        path for path in paths
        if mutates_log or path != Path(base.log_path(binding.base, domain))
    )
    transaction_paths = _relative_transaction_paths(binding.base, commit_files)
    commit_pathspec = (
        domain if prepared_specification.get("rebuild") else transaction_paths
    )
    resolved_specification = None
    prepared = None
    published = False
    snapshot = None
    head_before = None
    try:
        with base_lock(binding.base, 15.0):
            dirty_allowed = (
                _relative_transaction_paths(
                    binding.base, (Path(page_path),)
                )
                if page_path is not None
                else ()
            )
            if not prepared_specification.get("rebuild") and _has_unrelated_domain_changes(
                binding.base, domain, dirty_allowed
            ):
                raise RuntimeError("unrelated domain changes present")
            resolved_specification = _resolve_git_specification_locked(
                prepared_specification
            )
            if (
                isinstance(resolved_specification, dict)
                and "error" in resolved_specification
            ):
                return resolved_specification
            if resolved_specification is not None:
                prepared = resolved_specification["prepared"]
            snapshot = _snapshot_files(paths)
            head_before = sync._head_revision(binding.base)
            try:
                mutate_page()
                stats = indexer.index_domain(cfg, binding.base, domain)
                if prepared is not None:
                    try:
                        prepared.publish()
                        published = True
                    except BaseException:
                        if not resolved_specification.get("fail_soft"):
                            raise
                        _restore_files({
                            projection_path: snapshot[projection_path]
                        })
                        prepared.cleanup()
                        resolved_specification["warning"] = _SPECIFICATION_WARNING
                commit = sync.commit_locked(
                    binding.base,
                    message,
                    pathspec=commit_pathspec,
                )
                if (
                    sync.is_git_repo(binding.base)
                    and not commit.get("committed")
                    and commit.get("warning") != "nothing to commit"
                ):
                    raise RuntimeError("local commit failed")
            except BaseException:
                if (
                    head_before is not None
                    and sync._head_revision(binding.base) != head_before
                ):
                    sync._run(binding.base, "reset", "--soft", head_before)
                if snapshot is not None:
                    _restore_files(snapshot)
                    if page_path is not None and snapshot.get(Path(page_path)) is None:
                        _remove_empty_page_parents(
                            Path(page_path),
                            Path(base.domain_dir(binding.base, domain)),
                        )
                try:
                    sync.unstage_locked(binding.base, commit_pathspec)
                except Exception:
                    pass
                if prepared is not None:
                    prepared.cleanup()
                raise
    except BaseException:
        return {
            "error": "specification transaction failed",
            "rolled_back": True,
            "specifications": _specification_result(
                prepared_specification["mode"],
                "failed",
                (
                    resolved_specification.get("projection")
                    if isinstance(resolved_specification, dict)
                    else None
                ),
            ),
        }
    commit = sync.publish_committed(
        binding.base, commit, after_commit=after_commit
    )
    if resolved_specification is None:
        return {
            "stats": stats,
            "commit": commit,
            "specifications": None,
            "specification_warning": None,
        }
    specification_result = _specification_result(
        resolved_specification["mode"],
        (
            "ready"
            if published
            else "failed"
            if resolved_specification["mode"] == "strict"
            else "stale"
        ),
        resolved_specification["projection"],
    )
    if resolved_specification["warning"]:
        specification_result["warning"] = resolved_specification["warning"]
    return {
        "stats": stats,
        "commit": commit,
        "specifications": specification_result,
        "specification_warning": resolved_specification["warning"],
    }


def _maybe_execute_specification_page_transaction(
    binding: base.Binding,
    domain: str,
    *,
    slug: str,
    candidate_markdown: str | None,
    original_markdown: str | None,
    delete_target: bool,
    path: str,
    mutate_page,
    cfg: Config,
    message: str,
    after_commit,
    mutates_log: bool,
    freshness_warning: str | None,
    response_fields: dict,
) -> dict | None:
    prepared_specification = _prepare_git_specification(
        binding,
        domain,
        target_slug=slug,
        candidate_markdown=candidate_markdown,
        original_markdown=original_markdown,
        delete_target=delete_target,
    )
    if prepared_specification is None:
        return None
    if "error" in prepared_specification:
        return prepared_specification
    transaction = _execute_git_specification_transaction(
        binding,
        domain,
        prepared_specification,
        page_path=path,
        mutate_page=mutate_page,
        cfg=cfg,
        message=message,
        after_commit=after_commit,
        mutates_log=mutates_log,
    )
    if "error" in transaction:
        return transaction
    stats = transaction["stats"]
    result = {
        **response_fields,
        "indexed_chunks": stats["indexed_chunks"],
        "bytes": stats["bytes"],
    }
    if "deleted" not in response_fields:
        for name in ("reused", "embedded", "over_cap"):
            if name in stats:
                result[name] = stats[name]
    result.update(_write_sync_result(
        transaction["commit"],
        freshness_warning,
        graph_warning=transaction["specification_warning"],
    ))
    result["specifications"] = transaction["specifications"]
    return result


def _after_commit_graph(
    mutation,
    *,
    refresh_files: tuple[str, ...] = (),
    delete_files: tuple[str, ...] = (),
    rebuild: bool = False,
):
    if mutation is None:
        return None

    def update_graph() -> str | None:
        if rebuild:
            warning = indexer.stage_graph_rebuild(mutation)
        else:
            warning = indexer.stage_graph_pages(
                mutation,
                refresh_files=refresh_files,
                delete_files=delete_files,
            )
        if warning is not None:
            return warning
        return indexer.finalize_graph_mutation(mutation)

    return update_graph


def _write_sync_result(
    commit: dict, freshness_warning: str | None = None,
    frontmatter_warning: str | None = None,
    graph_warning: str | None = None,
) -> dict:
    result = {
        "committed": commit.get("committed", False),
        "pushed": commit.get("pushed", False),
    }
    for key in (
        "sync_attempts", "push_attempts", "failure_class", "conflict", "hint"
    ):
        if key in commit:
            result[key] = commit[key]
    warning = _compose_warnings(
        commit.get("warning"), freshness_warning, frontmatter_warning, graph_warning
    )
    if warning:
        result["warning"] = warning
    return result


def _prepare_postgres_page(
    cfg: Config,
    domain: str,
    slug: str,
    markdown: str,
    *,
    source: str | None,
    type: str | None,
    tags: list[str] | None,
    description: str | None,
    status: str | None,
) -> tuple[str, str, str | None] | dict:
    """Build canonical PostgreSQL Markdown without touching local storage."""
    try:
        authored_meta, body = _fm.split(markdown, strict_code=True)
    except _fm.FrontmatterError as exc:
        return {
            "error": str(exc),
            "hint": "use only code.symbols, code.files, and code.source_globs",
        }
    authored_code = authored_meta.get("code")
    if authored_code is not None:
        try:
            _codegraph_linking.validate_code_mapping(authored_code)
        except _codegraph_linking.SelectorError as exc:
            return {
                "error": str(exc),
                "hint": "use only code.symbols, code.files, and code.source_globs",
            }
    body = to_markdown_links(body)
    blocking = [
        finding
        for finding in validate_page(body)
        if finding.get("type") in _BLOCKING
    ]
    if blocking:
        return {
            "error": "section structure invalid",
            "findings": blocking,
            "hint": "use only ## headings; no text before the first ##",
        }

    warnings = []
    requested_type = type or authored_meta.get("type")
    if requested_type is not None:
        page_type = _fm.normalize_type(requested_type)
        page_tags = _fm.normalize_tags(
            tags if tags is not None else authored_meta.get("tags", [])
        )
    elif cfg.chat_model:
        classified = classify.classify_page(cfg, body, [])
        page_type = classified["type"]
        page_tags = (
            _fm.normalize_tags(tags) if tags is not None else classified["tags"]
        )
        if classified["warning"]:
            warnings.append(classified["warning"])
    else:
        page_type = _fm.DEFAULT_TYPE
        page_tags = _fm.normalize_tags(tags or [])
        warnings.append(
            "type not given and IWIKI_CHAT_MODEL unset; defaulted to concept"
        )
    try:
        identity = _resolve_identity(slug, page_type)
    except ValueError as exc:
        return {
            "error": str(exc),
            "hint": "pass a bare slug with a matching `type`, or a slug whose "
                    "first segment equals the frontmatter type",
        }
    page_file = f"{identity}.md"
    if page_file in RESERVED_OKF:
        return {
            "error": f"slug tail is reserved for the generated OKF file '{page_file}'",
            "hint": "choose another slug; index/log are generated, not authored",
        }

    meta = {
        "type": page_type,
        "title": _fm.derive_title(body, _slug_parts(identity)[-1]),
    }
    page_description = (
        description
        if description is not None
        else authored_meta.get("description") or _fm.derive_description(body)
    )
    if page_description:
        meta["description"] = page_description
    else:
        warnings.append("no description given and no ## Overview to derive from")
    if source:
        meta["resource"] = source
    if page_tags:
        meta["tags"] = page_tags
    meta["status"] = _fm.normalize_status(
        status or authored_meta.get("status") or _fm.DEFAULT_STATUS
    )
    if authored_code is not None:
        meta["code"] = authored_code
    meta["timestamp"] = _dt.date.today().isoformat()
    return identity, _fm.render(meta) + body, "; ".join(warnings) or None


@_safe
def wiki_write_page(
    domain: str, slug: str, markdown: str, source: str | None = None,
    type: str | None = None, tags: list[str] | None = None,
    description: str | None = None, status: str | None = None,
) -> dict:
    bind = _resolved_binding()
    valid_domain = _validate_domain(domain)
    if _is_postgres(bind):
        scope_error = base.write_scope_error(bind, valid_domain)
        if scope_error:
            return scope_error
        store = _postgres_store_for_binding(bind)
        if valid_domain not in store.list_domains():
            return {
                "error": f"domain '{valid_domain}' not found",
                "hint": "ask an administrator to create the domain",
            }
        cfg = Config.load()
        prepared = _prepare_postgres_page(
            cfg,
            valid_domain,
            slug,
            markdown,
            source=source,
            type=type,
            tags=tags,
            description=description,
            status=status,
        )
        if isinstance(prepared, dict):
            return prepared
        identity, full_markdown, warning = prepared
        result = store.write_page(valid_domain, identity, full_markdown)
        if warning and "error" not in result:
            result["warning"] = warning
        return result
    dom_path, scope_error = _existing_domain_write_guard(bind, valid_domain)
    if scope_error:
        return scope_error
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    if not dom_path.is_dir():
        return {
            "error": f"domain '{valid_domain}' not found",
            "hint": "create it with wiki_create_domain",
        }
    base.migrate_store_location(bind.base, valid_domain)
    try:
        authored_meta, authored_body = _fm.split(markdown, strict_code=True)
    except _fm.FrontmatterError as exc:
        return {
            "error": str(exc),
            "hint": "use only code.symbols, code.files, and code.source_globs",
        }
    authored_code = authored_meta.get("code") if "code" in authored_meta else None
    if authored_code is not None:
        try:
            _codegraph_linking.validate_code_mapping(authored_code)
        except _codegraph_linking.SelectorError as exc:
            return {
                "error": str(exc),
                "hint": "use only code.symbols, code.files, and code.source_globs",
            }
        markdown = authored_body
    markdown = to_markdown_links(markdown)
    blocking = [f for f in validate_page(markdown) if f.get("type") in _BLOCKING]
    if blocking:
        return {
            "error": "section structure invalid",
            "findings": blocking,
            "hint": "use only ## headings; no text before the first ##",
        }
    # The .iwikiignore gate must see the source exactly as the caller gave it:
    # ignore.is_ignored abspath-resolves a relative source against the process
    # CWD (not project_dir), so a path-anchored pattern would miss once the
    # source is relativized. Check ignore first, then normalize for storage.
    if source:
        spec = ignore.load_project_ignore(bind.project_dir)
        if ignore.is_ignored(spec, source, bind.project_dir):
            return {
                "error": "source matches .iwikiignore",
                "hint": f"'{source}' is excluded by .iwikiignore; "
                        "remove the pattern to ingest, or omit source",
            }
    if source is not None:
        try:
            source = _normalize_source(bind.project_dir, source)
        except ValueError as exc:
            return {"error": str(exc),
                    "hint": "pass a source path inside the bound project"}
    cfg = Config.load()
    fm_block, fm_warning = okf.build_frontmatter(
        cfg, bind.base, valid_domain, _slug_parts(slug)[-1], markdown,
        source=source, explicit_type=type, explicit_tags=tags,
        explicit_description=description, explicit_status=status,
        timestamp_path=f"{valid_domain}/{slug}.md",
        authored_code=authored_code)
    meta, _ = _fm.split(fm_block)
    resolved_type = meta.get("type")
    try:
        identity = _resolve_identity(slug, resolved_type)
    except ValueError as exc:
        return {"error": str(exc),
                "hint": "pass a bare slug with a matching `type`, or a slug whose "
                        "first segment equals the frontmatter type"}
    page_file = identity + ".md"
    # Reject reserved slugs BEFORE the exists check: index.md/log.md may already
    # exist from a prior wiki_export_okf run, so on such a domain the exists
    # check would otherwise mask this with a misleading "page exists" error.
    # RESERVED_OKF holds domain-ROOT-relative names ("index.md"/"log.md"); compare
    # the full identity, not its basename -- a type-dir identity like
    # "concept/index.md" is a distinct, non-reserved page (basename comparison
    # would wrongly reject it).
    if page_file in RESERVED_OKF:
        return {
            "error": f"slug tail is reserved for the generated OKF file '{page_file}'",
            "hint": "choose another slug; index/log are generated, not authored",
        }
    path = _page_path(bind.base, valid_domain, identity)
    if os.path.exists(path):
        return {
            "error": f"page '{valid_domain}/{identity}' exists",
            "hint": "editing an existing page is a guarded op; confirm with the user",
        }
    full_md = fm_block + markdown
    log_source = source or ""
    log_src_hash = indexer.src_hash(source) if source else None
    log_appended = False
    graph_mutation = indexer.prepare_graph_mutation(bind.base, valid_domain)
    prepared_specification = _prepare_git_specification(
        bind,
        valid_domain,
        target_slug=identity,
        candidate_markdown=full_md,
    )
    if (
        isinstance(prepared_specification, dict)
        and "error" in prepared_specification
    ):
        return prepared_specification
    if prepared_specification is not None:
        def mutate_specification_page() -> None:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(full_md)
            indexer.append_log(
                bind.base,
                valid_domain,
                "ingest",
                log_source,
                page_file,
                log_src_hash,
            )

        page_rel = f"{valid_domain}/{page_file}"
        transaction = _execute_git_specification_transaction(
            bind,
            valid_domain,
            prepared_specification,
            page_path=path,
            mutate_page=mutate_specification_page,
            cfg=cfg,
            message=f"iwiki: ingest {page_rel}",
            after_commit=_after_commit_graph(
                graph_mutation, refresh_files=(page_file,)
            ),
            mutates_log=True,
        )
        if "error" in transaction:
            return transaction
        stats = transaction["stats"]
        return {
            "page": page_rel,
            "indexed_chunks": stats["indexed_chunks"],
            "bytes": stats["bytes"],
            "over_cap": stats["over_cap"],
            **_write_sync_result(
                transaction["commit"],
                fresh.get("warning"),
                fm_warning,
                transaction["specification_warning"],
            ),
            "specifications": transaction["specifications"],
        }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(full_md)
        indexer.append_log(
            bind.base,
            valid_domain,
            "ingest",
            log_source,
            page_file,
            log_src_hash,
        )
        log_appended = True
        stats = indexer.index_domain(cfg, bind.base, valid_domain)
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        if log_appended:
            _rollback_last_log(
                bind.base, valid_domain, "ingest", page_file, log_source, log_src_hash
            )
        raise
    page_rel = f"{valid_domain}/{page_file}"
    commit = sync.commit_and_push(bind.base, f"iwiki: ingest {page_rel}",
                                  pathspec=valid_domain,
                                  _after_commit=_after_commit_graph(
                                      graph_mutation, refresh_files=(page_file,)
                                  ))
    result = {
        "page": page_rel,
        "indexed_chunks": stats["indexed_chunks"],
        "bytes": stats["bytes"],
        "over_cap": stats["over_cap"],
        **_write_sync_result(commit, fresh.get("warning"), fm_warning),
    }
    return result


def _planned_ingest_log_edit(
    base_dir: str, domain: str, source: str, page_file: str
) -> cross_domain.PlannedEdit:
    path = Path(base.log_path(base_dir, domain))
    existed = path.is_file()
    before = path.read_bytes() if existed else b""
    kept: list[str] = []
    for line in before.decode("utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except ValueError:
            kept.append(stripped)
            continue
        if record.get("op") == "ingest" and record.get("page") == page_file:
            continue
        kept.append(stripped)
    record = {
        "op": "ingest",
        "source": source,
        "page": page_file,
        "date": _dt.date.today().isoformat(),
        "src_hash": indexer.src_hash(source),
    }
    kept.append(json.dumps(record, ensure_ascii=False))
    after = ("\n".join(kept) + "\n").encode("utf-8")
    return cross_domain.PlannedEdit(
        domain,
        "log.jsonl",
        sha256(before).hexdigest() if existed else None,
        after,
    )


def _apply_heading_rename(
    bind: base.Binding,
    domain: str,
    slug: str,
    heading: str,
    new_heading: str,
    new_md: str,
    original_full: str,
    source: str | None,
    fresh: dict,
) -> dict:
    """Execute a Git-backed heading rename and every exact anchor rewrite."""
    page_file = PurePosixPath(*_slug_parts(slug)).as_posix() + ".md"
    target_page_id = f"{domain}/{page_file[:-3]}"
    old_anchor = slugify_heading(heading)
    new_anchor = slugify_heading(new_heading)
    target_bytes = original_full.encode("utf-8")
    prepared_specification = _prepare_git_specification(
        bind,
        domain,
        target_slug=slug,
        candidate_markdown=new_md,
        original_markdown=original_full,
    )
    if (
        isinstance(prepared_specification, dict)
        and "error" in prepared_specification
    ):
        return prepared_specification
    edits: dict[tuple[str, str], cross_domain.PlannedEdit] = {
        (domain, page_file): cross_domain.PlannedEdit(
            domain,
            page_file,
            sha256(target_bytes).hexdigest(),
            new_md.encode("utf-8"),
        )
    }
    rewritten_pages: set[str] = set()
    rewritten_links = 0

    if old_anchor != new_anchor:
        domain_root = Path(base.domain_dir(bind.base, domain))
        for page_slug in okf._page_slugs(domain_root):
            file = f"{page_slug}.md"
            source_bytes = graph._read_scoped_markdown(bind.base, domain, file)
            if source_bytes is None:
                raise cross_domain.CrossDomainError("source_changed")
            targets_page = file == page_file or any(
                target.kind == "intra"
                and target.target_page == page_file[:-3]
                and target.target_anchor == old_anchor
                for target in parse_link_targets(source_bytes.decode("utf-8"), domain)
            )
            if not targets_page:
                continue
            key = (domain, file)
            existing = edits.get(key)
            content = existing.after if existing is not None else source_bytes
            rewritten, count = rewrite_relative_anchors(
                content.decode("utf-8"),
                old_anchor,
                new_anchor,
                target_page=page_file[:-3],
                source_page=page_slug,
            )
            if not count:
                continue
            edits[key] = cross_domain.PlannedEdit(
                domain,
                file,
                (
                    existing.before_hash
                    if existing is not None
                    else sha256(source_bytes).hexdigest()
                ),
                rewritten.encode("utf-8"),
            )
            rewritten_pages.add(f"{domain}/{file}")
            rewritten_links += count

        visible_domains = tuple(base.resolve_scope(bind, "project", None))
        candidates = graph.incoming_candidates(
            bind.base, visible_domains, target_page_id, old_anchor
        )
        if candidates is None:
            try:
                candidates = graph.markdown_incoming_snapshot(
                    bind.base, visible_domains, target_page_id, old_anchor
                ).candidates
            except graph.MarkdownSnapshotChanged as exc:
                raise cross_domain.CrossDomainError("source_changed") from exc
            except graph.GraphRuntimeError as exc:
                raise cross_domain.CrossDomainError("mutation_failed") from exc
        candidates = tuple(
            sorted(set(candidates), key=lambda item: (item.domain, item.file))
        )
        writable = set(base.writable_domains(bind))
        if any(candidate.domain not in writable for candidate in candidates):
            raise cross_domain.CrossDomainError("write_scope_blocked")
        rewrite = CrossDomainRewrite(
            domain,
            page_file[:-3],
            page_file[:-3],
            old_anchor,
            new_anchor,
        )
        for candidate in candidates:
            source_bytes = graph._read_scoped_markdown(
                bind.base, candidate.domain, candidate.file
            )
            if source_bytes is None:
                raise cross_domain.CrossDomainError("source_changed")
            key = (candidate.domain, candidate.file)
            existing = edits.get(key)
            content = existing.after if existing is not None else source_bytes
            rewritten, count = rewrite_cross_domain_links(
                content.decode("utf-8"), candidate.domain, rewrite
            )
            if not count:
                continue
            edits[key] = cross_domain.PlannedEdit(
                candidate.domain,
                candidate.file,
                (
                    existing.before_hash
                    if existing is not None
                    else sha256(source_bytes).hexdigest()
                ),
                rewritten.encode("utf-8"),
            )
            rewritten_pages.add(f"{candidate.domain}/{candidate.file}")
            rewritten_links += count

    if source is not None:
        log_edit = _planned_ingest_log_edit(
            bind.base, domain, source, page_file
        )
        edits[(domain, log_edit.file)] = log_edit

    def prepare_specification_extension():
        if bind.specification_mode == "disabled":
            return cross_domain.PreparedPlanExtension()
        planned_by_domain: dict[str, dict[str, str | None]] = {}
        projected_domains = set()
        for edit in edits.values():
            if not edit.file.endswith(".md") or edit.file in RESERVED_OKF:
                continue
            markdown = (
                edit.after.decode("utf-8") if edit.after is not None else None
            )
            planned_by_domain.setdefault(edit.domain, {})[
                edit.file[:-3]
            ] = markdown
            if markdown is not None and _is_specification_markdown(markdown):
                projected_domains.add(edit.domain)
        if not projected_domains:
            return cross_domain.PreparedPlanExtension()

        extension_edits = []
        fail_soft_files = []
        resolved_by_domain = {}
        projected_domains = tuple(sorted(projected_domains))
        response_domain = domain if domain in projected_domains else projected_domains[0]
        for projected_domain in projected_domains:
            request = {
                "binding": bind,
                "domain": projected_domain,
                "mode": bind.specification_mode,
                "target_slug": (
                    slug
                    if projected_domain == domain
                    and prepared_specification is not None
                    else None
                ),
                "candidate_markdown": None,
                "original_markdown": None,
                "delete_target": False,
                "rebuild": False,
                "planned_markdown": planned_by_domain[projected_domain],
            }
            resolved = _resolve_git_specification_locked(request)
            if isinstance(resolved, dict) and "error" in resolved:
                return cross_domain.PreparedPlanExtension(error=resolved)
            resolved_by_domain[projected_domain] = resolved
            prepared_projection = resolved["prepared"]
            if prepared_projection is None:
                continue
            projection_path = Path(base.specifications_path(
                bind.base, projected_domain
            ))
            projection_before = (
                projection_path.read_bytes()
                if projection_path.is_file()
                else None
            )
            projection_after = prepared_projection.temporary_path.read_bytes()
            prepared_projection.abort()
            relative = f"{projected_domain}/specifications.jsonl"
            extension_edits.append(cross_domain.PlannedEdit(
                projected_domain,
                "specifications.jsonl",
                (
                    sha256(projection_before).hexdigest()
                    if projection_before is not None
                    else None
                ),
                projection_after,
            ))
            if resolved["mode"] == "optional":
                fail_soft_files.append(relative)
        response_resolved = resolved_by_domain[response_domain]
        metadata = {
            "resolved": response_resolved,
            "projection_edit": any(
                edit.domain == response_domain for edit in extension_edits
            ),
            "projection_incomplete": any(
                resolved["prepared"] is None
                for resolved in resolved_by_domain.values()
            ),
        }
        return cross_domain.PreparedPlanExtension(
            edits=tuple(extension_edits),
            fail_soft_files=tuple(fail_soft_files),
            metadata=metadata,
        )
    affected_domains = tuple(sorted({edit.domain for edit in edits.values()}))
    plan = cross_domain.MutationPlan(
        operation=f"rename heading in {domain}/{page_file}",
        transaction_id=secrets.token_hex(16),
        base_head=sync._head_revision(bind.base),
        edits=tuple(sorted(edits.values(), key=lambda edit: (edit.domain, edit.file))),
        affected_domains=affected_domains,
        rewritten_pages=tuple(sorted(rewritten_pages)),
        rewritten_links=rewritten_links,
    )
    evidence = cross_domain.execute_plan(
        bind.base,
        bind,
        plan,
        _include_index_stats=True,
        _prepare_locked=prepare_specification_extension,
    )
    if "error" in evidence:
        return evidence
    extension = evidence.pop("_extension", {})
    index_stats = evidence.pop("_index_stats")[domain]
    result = {
        "page": f"{domain}/{page_file}",
        "heading": heading.lstrip("#").strip(),
        **index_stats,
        **evidence,
    }
    resolved_specification = extension.get("resolved")
    projection_edit_failed = (
        extension.get("fail_soft_edit_failed", False)
        or extension.get("projection_incomplete", False)
    )
    specification_warning = (
        _SPECIFICATION_WARNING
        if projection_edit_failed
        else (
            resolved_specification.get("warning")
            if isinstance(resolved_specification, dict)
            else None
        )
    )
    warning = _compose_warnings(
        result.get("warning"), fresh.get("warning"), specification_warning
    )
    if warning:
        result["warning"] = warning
    else:
        result.pop("warning", None)
    if resolved_specification is not None:
        specification_published = (
            extension.get("projection_edit", False)
            and not projection_edit_failed
        )
        result["specifications"] = _specification_result(
            resolved_specification["mode"],
            (
                "ready"
                if specification_published
                else "failed"
                if resolved_specification["mode"] == "strict"
                else "stale"
            ),
            resolved_specification["projection"],
        )
        if specification_warning:
            result["specifications"]["warning"] = specification_warning
    return result


@_safe
def wiki_update_page(
    domain: str, slug: str, heading: str, new_body: str, source: str | None = None,
    description: str | None = None, status: str | None = None,
    new_heading: str | None = None,
    expected_revision: _ExpectedRevision = None,
    expected_section_hash: str | None = None,
) -> dict:
    bind = _resolved_binding()
    valid_domain = _validate_domain(domain)
    if _is_postgres(bind):
        scope_error = base.write_scope_error(bind, valid_domain)
        if scope_error:
            return scope_error
        if expected_revision is None:
            return expected_revision_required()
        _slug_parts(slug)
        store = _postgres_store_for_binding(bind)
        page = store.read_page(valid_domain, slug)
        if page is None:
            return {
                "error": f"page '{valid_domain}/{slug}' not found",
                "hint": "list pages with wiki_list_pages",
            }
        try:
            meta, original_body = _fm.split(
                page["markdown"], strict_code=True
            )
        except _fm.FrontmatterError as exc:
            return {
                "error": str(exc),
                "hint": "fix nested code frontmatter before updating",
            }
        try:
            conflict = _check_section_hash(original_body, heading, expected_section_hash)
            if conflict is not None:
                return conflict
            updated_body = replace_section(
                original_body,
                heading,
                to_markdown_links(new_body),
                new_heading=new_heading,
            )
        except SectionError as exc:
            return {
                "error": str(exc),
                "hint": "check the heading with wiki_read_page",
            }
        blocking = [
            finding
            for finding in validate_page(updated_body)
            if finding.get("type") in _BLOCKING
        ]
        if blocking:
            return {
                "error": "section structure invalid",
                "findings": blocking,
                "hint": "new_body must use only ## headings; no ###+, no pre-## text",
            }
        if description is not None:
            meta["description"] = description
        if status is not None:
            meta["status"] = _fm.normalize_status(status)
        if source is not None:
            meta["resource"] = source
        if meta:
            meta["timestamp"] = _dt.date.today().isoformat()
            updated_markdown = _fm.render(meta) + updated_body
        else:
            updated_markdown = updated_body
        result = store.update_page(
            valid_domain,
            slug,
            updated_markdown,
            expected_revision,
        )
        if "error" not in result:
            result["heading"] = heading.lstrip("#").strip()
        return result
    dom_path, scope_error = _existing_domain_write_guard(bind, valid_domain)
    if scope_error:
        return scope_error
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    if not dom_path.is_dir():
        return {
            "error": f"domain '{valid_domain}' not found",
            "hint": "create it with wiki_create_domain",
        }
    base.migrate_store_location(bind.base, valid_domain)
    # See wiki_write_page: ignore gate on the raw source first, then normalize.
    if source:
        spec = ignore.load_project_ignore(bind.project_dir)
        if ignore.is_ignored(spec, source, bind.project_dir):
            return {
                "error": "source matches .iwikiignore",
                "hint": f"'{source}' is excluded by .iwikiignore; "
                        "remove the pattern to ingest, or omit source",
            }
    if source is not None:
        try:
            source = _normalize_source(bind.project_dir, source)
        except ValueError as exc:
            return {"error": str(exc),
                    "hint": "pass a source path inside the bound project"}
    path = _page_path(bind.base, valid_domain, slug)
    if not os.path.isfile(path):
        return {
            "error": f"page '{valid_domain}/{slug}' not found",
            "hint": "list pages with wiki_list_pages",
        }
    page_file = PurePosixPath(*_slug_parts(slug)).as_posix() + ".md"
    original_full = open(path, encoding="utf-8").read()
    try:
        meta, original_body = _fm.split(original_full, strict_code=True)
    except _fm.FrontmatterError as exc:
        return {
            "error": str(exc),
            "hint": "fix nested code frontmatter before updating",
        }
    new_body = to_markdown_links(new_body)
    try:
        conflict = _check_section_hash(original_body, heading, expected_section_hash)
        if conflict is not None:
            return conflict
        new_body = replace_section(
            original_body, heading, new_body, new_heading=new_heading
        )
    except SectionError as e:
        if new_heading is not None and "collides with another anchor" in str(e):
            raise cross_domain.CrossDomainError("heading_collision", str(e))
        return {"error": str(e), "hint": "check the heading with wiki_read_page"}
    blocking = [f for f in validate_page(new_body) if f.get("type") in _BLOCKING]
    if blocking:
        return {
            "error": "section structure invalid",
            "findings": blocking,
            "hint": "new_body must use only ## headings; no ###+, no pre-## text",
        }
    cfg = Config.load()
    if meta:
        if description is not None:
            meta["description"] = description
        if status is not None:
            meta["status"] = _fm.normalize_status(status)
        meta["timestamp"] = _dt.date.today().isoformat()
        new_md = _fm.render(meta) + new_body
    else:
        new_md = new_body
    if new_heading is not None and sync.is_git_repo(bind.base):
        return _apply_heading_rename(
            bind,
            valid_domain,
            slug,
            heading,
            new_heading,
            new_md,
            original_full,
            source,
            fresh,
        )
    log_file = base.log_path(bind.base, valid_domain)
    log_before = None
    if source and os.path.exists(log_file):
        with open(log_file, "rb") as fh:
            log_before = fh.read()
    graph_mutation = indexer.prepare_graph_mutation(bind.base, valid_domain)
    prepared_specification = _prepare_git_specification(
        bind,
        valid_domain,
        target_slug=slug,
        candidate_markdown=new_md,
        original_markdown=original_full,
    )
    if (
        isinstance(prepared_specification, dict)
        and "error" in prepared_specification
    ):
        return prepared_specification
    if prepared_specification is not None:
        def mutate_specification_page() -> None:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(new_md)
            if source:
                indexer.upsert_ingest_log(
                    bind.base,
                    valid_domain,
                    source,
                    page_file,
                    indexer.src_hash(source),
                )

        page_rel = f"{valid_domain}/{page_file}"
        transaction = _execute_git_specification_transaction(
            bind,
            valid_domain,
            prepared_specification,
            page_path=path,
            mutate_page=mutate_specification_page,
            cfg=cfg,
            message=f"iwiki: update {page_rel}",
            after_commit=_after_commit_graph(
                graph_mutation, refresh_files=(page_file,)
            ),
            mutates_log=source is not None,
        )
        if "error" in transaction:
            return transaction
        stats = transaction["stats"]
        return {
            "page": page_rel,
            "heading": heading.lstrip("#").strip(),
            "indexed_chunks": stats["indexed_chunks"],
            "reused": stats["reused"],
            "embedded": stats["embedded"],
            "bytes": stats["bytes"],
            "over_cap": stats["over_cap"],
            **_write_sync_result(
                transaction["commit"],
                fresh.get("warning"),
                graph_warning=transaction["specification_warning"],
            ),
            "specifications": transaction["specifications"],
        }
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new_md)
        if source:
            indexer.upsert_ingest_log(
                bind.base, valid_domain, source, page_file, indexer.src_hash(source)
            )
        stats = indexer.index_domain(cfg, bind.base, valid_domain)
    except Exception:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(original_full)
        if source:            # mirrors the upsert gate above
            _restore_log(log_file, log_before)
        raise
    page_rel = f"{valid_domain}/{page_file}"
    commit = sync.commit_and_push(bind.base, f"iwiki: update {page_rel}",
                                  pathspec=valid_domain,
                                  _after_commit=_after_commit_graph(
                                      graph_mutation, refresh_files=(page_file,)
                                  ))
    result = {
        "page": page_rel,
        "heading": heading.lstrip("#").strip(),
        "indexed_chunks": stats["indexed_chunks"],
        "reused": stats["reused"],
        "embedded": stats["embedded"],
        "bytes": stats["bytes"],
        "over_cap": stats["over_cap"],
        **_write_sync_result(commit, fresh.get("warning")),
    }
    return result


@_safe
def wiki_insert_section(
    domain: str, slug: str, heading: str, body: str,
    after_heading: str | None = None, before_heading: str | None = None,
    source: str | None = None, description: str | None = None,
    status: str | None = None, expected_revision: _ExpectedRevision = None,
) -> dict:
    bind = _resolved_binding()
    valid_domain = _validate_domain(domain)
    if _is_postgres(bind):
        scope_error = base.write_scope_error(bind, valid_domain)
        if scope_error:
            return scope_error
        if expected_revision is None:
            return expected_revision_required()
        _slug_parts(slug)
        store = _postgres_store_for_binding(bind)
        page = store.read_page(valid_domain, slug)
        if page is None:
            return {
                "error": f"page '{valid_domain}/{slug}' not found",
                "hint": "list pages with wiki_list_pages",
            }
        try:
            meta, original_body = _fm.split(
                page["markdown"], strict_code=True
            )
        except _fm.FrontmatterError as exc:
            return {
                "error": str(exc),
                "hint": "fix nested code frontmatter before updating",
            }
        try:
            updated_body = insert_section(
                original_body,
                heading,
                to_markdown_links(body),
                after=after_heading,
                before=before_heading,
            )
        except SectionError as exc:
            return {
                "error": str(exc),
                "hint": "check the heading with wiki_read_page",
            }
        blocking = [
            finding
            for finding in validate_page(updated_body)
            if finding.get("type") in _BLOCKING
        ]
        if blocking:
            return {
                "error": "section structure invalid",
                "findings": blocking,
                "hint": "body must use only ## headings; no ###+, no pre-## text",
            }
        if description is not None:
            meta["description"] = description
        if status is not None:
            meta["status"] = _fm.normalize_status(status)
        if source is not None:
            meta["resource"] = source
        if meta:
            meta["timestamp"] = _dt.date.today().isoformat()
            updated_markdown = _fm.render(meta) + updated_body
        else:
            updated_markdown = updated_body
        result = store.update_page(
            valid_domain,
            slug,
            updated_markdown,
            expected_revision,
        )
        if "error" not in result:
            result["heading"] = heading.lstrip("#").strip()
        return result
    dom_path, scope_error = _existing_domain_write_guard(bind, valid_domain)
    if scope_error:
        return scope_error
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    if not dom_path.is_dir():
        return {
            "error": f"domain '{valid_domain}' not found",
            "hint": "create it with wiki_create_domain",
        }
    base.migrate_store_location(bind.base, valid_domain)
    # See wiki_write_page: ignore gate on the raw source first, then normalize.
    if source:
        spec = ignore.load_project_ignore(bind.project_dir)
        if ignore.is_ignored(spec, source, bind.project_dir):
            return {
                "error": "source matches .iwikiignore",
                "hint": f"'{source}' is excluded by .iwikiignore; "
                        "remove the pattern to ingest, or omit source",
            }
    if source is not None:
        try:
            source = _normalize_source(bind.project_dir, source)
        except ValueError as exc:
            return {"error": str(exc),
                    "hint": "pass a source path inside the bound project"}
    path = _page_path(bind.base, valid_domain, slug)
    if not os.path.isfile(path):
        return {
            "error": f"page '{valid_domain}/{slug}' not found",
            "hint": "list pages with wiki_list_pages",
        }
    page_file = PurePosixPath(*_slug_parts(slug)).as_posix() + ".md"
    original_full = open(path, encoding="utf-8").read()
    try:
        meta, original_body = _fm.split(original_full, strict_code=True)
    except _fm.FrontmatterError as exc:
        return {
            "error": str(exc),
            "hint": "fix nested code frontmatter before updating",
        }
    body = to_markdown_links(body)
    try:
        new_body = insert_section(
            original_body, heading, body, after=after_heading, before=before_heading
        )
    except SectionError as e:
        return {"error": str(e), "hint": "check the heading with wiki_read_page"}
    blocking = [f for f in validate_page(new_body) if f.get("type") in _BLOCKING]
    if blocking:
        return {
            "error": "section structure invalid",
            "findings": blocking,
            "hint": "body must use only ## headings; no ###+, no pre-## text",
        }
    cfg = Config.load()
    if meta:
        if description is not None:
            meta["description"] = description
        if status is not None:
            meta["status"] = _fm.normalize_status(status)
        meta["timestamp"] = _dt.date.today().isoformat()
        new_md = _fm.render(meta) + new_body
    else:
        new_md = new_body
    log_file = base.log_path(bind.base, valid_domain)
    log_before = None
    if source and os.path.exists(log_file):
        with open(log_file, "rb") as fh:
            log_before = fh.read()
    graph_mutation = indexer.prepare_graph_mutation(bind.base, valid_domain)
    page_rel = f"{valid_domain}/{page_file}"

    def mutate_specification_section_insert() -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new_md)
        if source:
            indexer.upsert_ingest_log(
                bind.base,
                valid_domain,
                source,
                page_file,
                indexer.src_hash(source),
            )

    specification_result = _maybe_execute_specification_page_transaction(
        bind,
        valid_domain,
        slug=slug,
        candidate_markdown=new_md,
        original_markdown=original_full,
        delete_target=False,
        path=path,
        mutate_page=mutate_specification_section_insert,
        cfg=cfg,
        message=f"iwiki: insert section into {page_rel}",
        after_commit=_after_commit_graph(
            graph_mutation, refresh_files=(page_file,)
        ),
        mutates_log=source is not None,
        freshness_warning=fresh.get("warning"),
        response_fields={
            "page": page_rel,
            "heading": heading.lstrip("#").strip(),
        },
    )
    if specification_result is not None:
        return specification_result
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new_md)
        if source:
            indexer.upsert_ingest_log(
                bind.base, valid_domain, source, page_file, indexer.src_hash(source)
            )
        stats = indexer.index_domain(cfg, bind.base, valid_domain)
    except Exception:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(original_full)
        if source:            # mirrors the upsert gate above
            _restore_log(log_file, log_before)
        raise
    commit = sync.commit_and_push(bind.base, f"iwiki: insert section into {page_rel}",
                                  pathspec=valid_domain,
                                  _after_commit=_after_commit_graph(
                                      graph_mutation, refresh_files=(page_file,)
                                  ))
    result = {
        "page": page_rel,
        "heading": heading.lstrip("#").strip(),
        "indexed_chunks": stats["indexed_chunks"],
        "reused": stats["reused"],
        "embedded": stats["embedded"],
        "bytes": stats["bytes"],
        "over_cap": stats["over_cap"],
        **_write_sync_result(commit, fresh.get("warning")),
    }
    return result


@_safe
def wiki_delete_section(
    domain: str, slug: str, heading: str,
    expected_revision: _ExpectedRevision = None,
    expected_section_hash: str | None = None,
) -> dict:
    bind = _resolved_binding()
    valid_domain = _validate_domain(domain)
    if _is_postgres(bind):
        scope_error = base.write_scope_error(bind, valid_domain)
        if scope_error:
            return scope_error
        if expected_revision is None:
            return expected_revision_required()
        _slug_parts(slug)
        store = _postgres_store_for_binding(bind)
        page = store.read_page(valid_domain, slug)
        if page is None:
            return {
                "error": f"page '{valid_domain}/{slug}' not found",
                "hint": "list pages with wiki_list_pages",
            }
        try:
            meta, original_body = _fm.split(page["markdown"], strict_code=True)
        except _fm.FrontmatterError as exc:
            return {
                "error": str(exc),
                "hint": "fix nested code frontmatter before updating",
            }
        try:
            conflict = _check_section_hash(original_body, heading, expected_section_hash)
            if conflict is not None:
                return conflict
            updated_body = delete_section(original_body, heading)
        except SectionError as exc:
            return {"error": str(exc), "hint": "check the heading with wiki_read_page"}
        blocking = [
            finding
            for finding in validate_page(updated_body)
            if finding.get("type") in _BLOCKING
        ]
        if blocking:
            return {
                "error": "section structure invalid",
                "findings": blocking,
                "hint": "resulting page must use only ## headings; no ###+, no pre-## text",
            }
        if meta:
            meta["timestamp"] = _dt.date.today().isoformat()
            updated_markdown = _fm.render(meta) + updated_body
        else:
            updated_markdown = updated_body
        result = store.update_page(
            valid_domain, slug, updated_markdown, expected_revision
        )
        if "error" not in result:
            result["heading"] = heading.lstrip("#").strip()
        return result
    dom_path, scope_error = _existing_domain_write_guard(bind, valid_domain)
    if scope_error:
        return scope_error
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    if not dom_path.is_dir():
        return {
            "error": f"domain '{valid_domain}' not found",
            "hint": "create it with wiki_create_domain",
        }
    base.migrate_store_location(bind.base, valid_domain)
    path = _page_path(bind.base, valid_domain, slug)
    if not os.path.isfile(path):
        return {
            "error": f"page '{valid_domain}/{slug}' not found",
            "hint": "list pages with wiki_list_pages",
        }
    page_file = PurePosixPath(*_slug_parts(slug)).as_posix() + ".md"
    original_full = open(path, encoding="utf-8").read()
    try:
        meta, original_body = _fm.split(original_full, strict_code=True)
    except _fm.FrontmatterError as exc:
        return {
            "error": str(exc),
            "hint": "fix nested code frontmatter before updating",
        }
    try:
        conflict = _check_section_hash(original_body, heading, expected_section_hash)
        if conflict is not None:
            return conflict
        new_body = delete_section(original_body, heading)
    except SectionError as e:
        return {"error": str(e), "hint": "check the heading with wiki_read_page"}
    blocking = [f for f in validate_page(new_body) if f.get("type") in _BLOCKING]
    if blocking:
        return {
            "error": "section structure invalid",
            "findings": blocking,
            "hint": "resulting page must use only ## headings; no ###+, no pre-## text",
        }
    cfg = Config.load()
    if meta:
        meta["timestamp"] = _dt.date.today().isoformat()
        new_md = _fm.render(meta) + new_body
    else:
        new_md = new_body
    graph_mutation = indexer.prepare_graph_mutation(bind.base, valid_domain)
    page_rel = f"{valid_domain}/{page_file}"

    def mutate_specification_section_delete() -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new_md)

    specification_result = _maybe_execute_specification_page_transaction(
        bind,
        valid_domain,
        slug=slug,
        candidate_markdown=new_md,
        original_markdown=original_full,
        delete_target=False,
        path=path,
        mutate_page=mutate_specification_section_delete,
        cfg=cfg,
        message=f"iwiki: delete section from {page_rel}",
        after_commit=_after_commit_graph(
            graph_mutation, refresh_files=(page_file,)
        ),
        mutates_log=False,
        freshness_warning=fresh.get("warning"),
        response_fields={
            "page": page_rel,
            "heading": heading.lstrip("#").strip(),
        },
    )
    if specification_result is not None:
        return specification_result
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new_md)
        stats = indexer.index_domain(cfg, bind.base, valid_domain)
    except Exception:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(original_full)
        raise
    commit = sync.commit_and_push(
        bind.base, f"iwiki: delete section from {page_rel}", pathspec=valid_domain,
        _after_commit=_after_commit_graph(graph_mutation, refresh_files=(page_file,)),
    )
    return {
        "page": page_rel,
        "heading": heading.lstrip("#").strip(),
        "indexed_chunks": stats["indexed_chunks"],
        "reused": stats["reused"],
        "embedded": stats["embedded"],
        "bytes": stats["bytes"],
        "over_cap": stats["over_cap"],
        **_write_sync_result(commit, fresh.get("warning")),
    }


@_safe
def wiki_move_section(
    domain: str, slug: str, heading: str,
    after_heading: str | None = None, before_heading: str | None = None,
    expected_revision: _ExpectedRevision = None,
    expected_section_hash: str | None = None,
) -> dict:
    bind = _resolved_binding()
    valid_domain = _validate_domain(domain)
    if _is_postgres(bind):
        scope_error = base.write_scope_error(bind, valid_domain)
        if scope_error:
            return scope_error
        if expected_revision is None:
            return expected_revision_required()
        _slug_parts(slug)
        store = _postgres_store_for_binding(bind)
        page = store.read_page(valid_domain, slug)
        if page is None:
            return {
                "error": f"page '{valid_domain}/{slug}' not found",
                "hint": "list pages with wiki_list_pages",
            }
        try:
            meta, original_body = _fm.split(page["markdown"], strict_code=True)
        except _fm.FrontmatterError as exc:
            return {
                "error": str(exc),
                "hint": "fix nested code frontmatter before updating",
            }
        try:
            conflict = _check_section_hash(original_body, heading, expected_section_hash)
            if conflict is not None:
                return conflict
            updated_body = move_section(
                original_body, heading, after=after_heading, before=before_heading
            )
        except SectionError as exc:
            return {"error": str(exc), "hint": "check the heading with wiki_read_page"}
        blocking = [
            finding
            for finding in validate_page(updated_body)
            if finding.get("type") in _BLOCKING
        ]
        if blocking:
            return {
                "error": "section structure invalid",
                "findings": blocking,
                "hint": "resulting page must use only ## headings; no ###+, no pre-## text",
            }
        if meta:
            meta["timestamp"] = _dt.date.today().isoformat()
            updated_markdown = _fm.render(meta) + updated_body
        else:
            updated_markdown = updated_body
        result = store.update_page(
            valid_domain, slug, updated_markdown, expected_revision
        )
        if "error" not in result:
            result["heading"] = heading.lstrip("#").strip()
        return result
    dom_path, scope_error = _existing_domain_write_guard(bind, valid_domain)
    if scope_error:
        return scope_error
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    if not dom_path.is_dir():
        return {
            "error": f"domain '{valid_domain}' not found",
            "hint": "create it with wiki_create_domain",
        }
    base.migrate_store_location(bind.base, valid_domain)
    path = _page_path(bind.base, valid_domain, slug)
    if not os.path.isfile(path):
        return {
            "error": f"page '{valid_domain}/{slug}' not found",
            "hint": "list pages with wiki_list_pages",
        }
    page_file = PurePosixPath(*_slug_parts(slug)).as_posix() + ".md"
    original_full = open(path, encoding="utf-8").read()
    try:
        meta, original_body = _fm.split(original_full, strict_code=True)
    except _fm.FrontmatterError as exc:
        return {
            "error": str(exc),
            "hint": "fix nested code frontmatter before updating",
        }
    try:
        conflict = _check_section_hash(original_body, heading, expected_section_hash)
        if conflict is not None:
            return conflict
        new_body = move_section(
            original_body, heading, after=after_heading, before=before_heading
        )
    except SectionError as e:
        return {"error": str(e), "hint": "check the heading with wiki_read_page"}
    blocking = [f for f in validate_page(new_body) if f.get("type") in _BLOCKING]
    if blocking:
        return {
            "error": "section structure invalid",
            "findings": blocking,
            "hint": "resulting page must use only ## headings; no ###+, no pre-## text",
        }
    cfg = Config.load()
    if meta:
        meta["timestamp"] = _dt.date.today().isoformat()
        new_md = _fm.render(meta) + new_body
    else:
        new_md = new_body
    graph_mutation = indexer.prepare_graph_mutation(bind.base, valid_domain)
    page_rel = f"{valid_domain}/{page_file}"

    def mutate_specification_section_move() -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new_md)

    specification_result = _maybe_execute_specification_page_transaction(
        bind,
        valid_domain,
        slug=slug,
        candidate_markdown=new_md,
        original_markdown=original_full,
        delete_target=False,
        path=path,
        mutate_page=mutate_specification_section_move,
        cfg=cfg,
        message=f"iwiki: move section in {page_rel}",
        after_commit=_after_commit_graph(
            graph_mutation, refresh_files=(page_file,)
        ),
        mutates_log=False,
        freshness_warning=fresh.get("warning"),
        response_fields={
            "page": page_rel,
            "heading": heading.lstrip("#").strip(),
        },
    )
    if specification_result is not None:
        return specification_result
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new_md)
        stats = indexer.index_domain(cfg, bind.base, valid_domain)
    except Exception:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(original_full)
        raise
    commit = sync.commit_and_push(
        bind.base, f"iwiki: move section in {page_rel}", pathspec=valid_domain,
        _after_commit=_after_commit_graph(graph_mutation, refresh_files=(page_file,)),
    )
    return {
        "page": page_rel,
        "heading": heading.lstrip("#").strip(),
        "indexed_chunks": stats["indexed_chunks"],
        "reused": stats["reused"],
        "embedded": stats["embedded"],
        "bytes": stats["bytes"],
        "over_cap": stats["over_cap"],
        **_write_sync_result(commit, fresh.get("warning")),
    }


@_safe
def wiki_delete_page(
    domain: str, slug: str, expected_revision: _ExpectedRevision = None
) -> dict:
    bind = _resolved_binding()
    valid_domain = _validate_domain(domain)
    if _is_postgres(bind):
        scope_error = base.write_scope_error(bind, valid_domain)
        if scope_error:
            return scope_error
        if expected_revision is None:
            return expected_revision_required()
        _slug_parts(slug)
        return _postgres_store_for_binding(bind).delete_page(
            valid_domain, slug, expected_revision
        )
    dom_path, scope_error = _existing_domain_write_guard(bind, valid_domain)
    if scope_error:
        return scope_error
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    if not dom_path.is_dir():
        return {
            "error": f"domain '{valid_domain}' not found",
            "hint": "create it with wiki_create_domain",
        }
    base.migrate_store_location(bind.base, valid_domain)
    path = _page_path(bind.base, valid_domain, slug)
    if not os.path.isfile(path):
        return {
            "error": f"page '{valid_domain}/{slug}' not found",
            "hint": "list pages with wiki_list_pages",
        }
    cfg = Config.load()
    page_file = PurePosixPath(*_slug_parts(slug)).as_posix() + ".md"
    with open(path, encoding="utf-8") as fh:
        content = fh.read()
    log_appended = False
    graph_mutation = indexer.prepare_graph_mutation(bind.base, valid_domain)
    page_rel = f"{valid_domain}/{page_file}"

    def mutate_specification_page_delete() -> None:
        os.remove(path)
        indexer.append_log(
            bind.base, valid_domain, "delete", "", page_file, None
        )

    specification_result = _maybe_execute_specification_page_transaction(
        bind,
        valid_domain,
        slug=slug,
        candidate_markdown=None,
        original_markdown=content,
        delete_target=True,
        path=path,
        mutate_page=mutate_specification_page_delete,
        cfg=cfg,
        message=f"iwiki: delete {page_rel}",
        after_commit=_after_commit_graph(
            graph_mutation, delete_files=(page_file,)
        ),
        mutates_log=True,
        freshness_warning=fresh.get("warning"),
        response_fields={"deleted": page_rel},
    )
    if specification_result is not None:
        return specification_result
    os.remove(path)
    try:
        indexer.append_log(bind.base, valid_domain, "delete", "", page_file, None)
        log_appended = True
        stats = indexer.index_domain(cfg, bind.base, valid_domain)
    except Exception:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        if log_appended:
            _rollback_last_log(bind.base, valid_domain, "delete", page_file, "", None)
        raise
    commit = sync.commit_and_push(bind.base, f"iwiki: delete {page_rel}",
                                  pathspec=valid_domain,
                                  _after_commit=_after_commit_graph(
                                      graph_mutation, delete_files=(page_file,)
                                  ))
    result = {
        "deleted": page_rel,
        "indexed_chunks": stats["indexed_chunks"],
        "bytes": stats["bytes"],
        **_write_sync_result(commit, fresh.get("warning")),
    }
    return result


@_safe
def wiki_index(domain: str | None = None) -> dict:
    bind = _resolved_binding()
    target = domain or bind.primary
    if not target:
        return {
            "error": "no domain given and no write-target bound",
            "hint": "pass domain= or edit write in .iwiki.toml manually",
        }
    valid_domain = _validate_domain(target)
    if _is_postgres(bind):
        scope_error = base.write_scope_error(bind, valid_domain)
        if scope_error:
            return scope_error
        return _postgres_store_for_binding(bind).index_domain(valid_domain)
    dom_path, scope_error = _existing_domain_write_guard(bind, valid_domain)
    if scope_error:
        return scope_error
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    if not dom_path.is_dir():
        return {
            "error": f"domain '{valid_domain}' not found",
            "hint": "create it with wiki_create_domain",
        }
    cfg = Config.load()
    graph_mutation = indexer.prepare_graph_mutation(
        bind.base, valid_domain, whole_domain=True
    )
    try:
        prepared_specification = _prepare_git_specification(
            bind, valid_domain, rebuild=True
        )
    except Exception:
        prepared_specification = None
        specification_warning = _SPECIFICATION_WARNING
    else:
        specification_warning = None
    if prepared_specification is not None:
        transaction = _execute_git_specification_transaction(
            bind,
            valid_domain,
            prepared_specification,
            page_path=None,
            mutate_page=lambda: None,
            cfg=cfg,
            message=f"iwiki: reindex {valid_domain}",
            after_commit=_after_commit_graph(graph_mutation, rebuild=True),
            mutates_log=False,
        )
        if "error" in transaction:
            return transaction
        result = {
            "domain": valid_domain,
            **transaction["stats"],
            **_write_sync_result(
                transaction["commit"],
                fresh.get("warning"),
                graph_warning=transaction["specification_warning"],
            ),
        }
        if transaction["specifications"] is not None:
            result["specifications"] = transaction["specifications"]
        return result
    stats = indexer.index_domain(cfg, bind.base, valid_domain)
    commit = sync.commit_and_push(bind.base, f"iwiki: reindex {valid_domain}",
                                  pathspec=valid_domain,
                                  _after_commit=_after_commit_graph(
                                      graph_mutation, rebuild=True
                                  ))
    result = {
        "domain": valid_domain,
        **stats,
        **_write_sync_result(
            commit, fresh.get("warning"), graph_warning=specification_warning
        ),
    }
    if specification_warning:
        result["specifications"] = {
            "mode": bind.specification_mode,
            "state": "failed" if bind.specification_mode == "strict" else "stale",
            "findings": [],
            "warning": specification_warning,
        }
    return result


@_safe
def wiki_create_domain(name: str) -> dict:
    bind = _resolved_binding()
    if _is_postgres(bind):
        session = _SESSION_BINDING.get()
        if not isinstance(session, _HostedBindingState):
            return _unsupported_storage()
        context = _request_auth_context()
        if context is None:
            raise _postgres_auth.AccessError(403)
        valid_domain = _postgres_auth.validate_domain_identifier(name)
        provisioned = _postgres_auth_for_binding(bind).provision_domain(
            context, valid_domain
        )
        effective = session.expand_domain(valid_domain, context)
        return {
            "created": valid_domain,
            "already_existed": provisioned["already_existed"],
            "domain": valid_domain,
            "read": list(effective.read),
            "write": list(effective.write),
            "primary": effective.primary,
        }
    valid_domain = _validate_domain(name)
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    dom_path = _domain_path(bind.base, valid_domain)
    if dom_path.is_dir():
        return {"error": f"domain '{valid_domain}' already exists"}
    os.makedirs(dom_path, exist_ok=True)
    ignore.ensure_iwikiignore(bind.project_dir)
    commit = sync.commit_and_push(bind.base, f"iwiki: create domain {valid_domain}",
                                  pathspec=valid_domain)
    return {"created": valid_domain, "committed": commit.get("committed", False),
            "pushed": commit.get("pushed", False), **_fresh_warn(fresh)}


def _hosted_domain_authority():
    binding = _resolved_binding()
    session = _SESSION_BINDING.get()
    if not _is_postgres(binding) or not isinstance(
        session, _HostedBindingState
    ):
        return binding, None, _unsupported_hosted_transport(binding)
    context = _request_auth_context()
    if context is None:
        raise _postgres_auth.AccessError(403)
    return binding, context, None


@_safe
def wiki_list_domain_grants(domain: str) -> dict:
    binding, context, unsupported = _hosted_domain_authority()
    if unsupported is not None:
        return unsupported
    grants = _postgres_auth_for_binding(binding).list_domain_grants(
        context, domain
    )
    return {"domain": domain, "grants": grants}


@_safe
def wiki_set_domain_grant(
    domain: str,
    token_id: str,
    can_read: bool,
    can_write: bool,
) -> dict:
    binding, context, unsupported = _hosted_domain_authority()
    if unsupported is not None:
        return unsupported
    return _postgres_auth_for_binding(binding).set_domain_grant(
        context,
        domain,
        token_id,
        can_read=can_read,
        can_write=can_write,
    )


@_safe
def wiki_revoke_domain_grant(domain: str, token_id: str) -> dict:
    binding, context, unsupported = _hosted_domain_authority()
    if unsupported is not None:
        return unsupported
    return _postgres_auth_for_binding(binding).revoke_domain_grant(
        context, domain, token_id
    )


def _wiki_bind(
    read: list[str] | None = None,
    write: list[str] | None = None,
    primary: str | None = None,
) -> dict:
    bind = _resolved_binding()
    if _is_postgres(bind):
        global _LOCAL_POSTGRES_BINDING
        valid_read = (
            list(bind.read)
            if read is None
            else [_validate_domain(domain) for domain in read]
        )
        valid_write = (
            list(bind.write)
            if write is None
            else [_validate_domain(domain) for domain in write]
        )
        if primary is None:
            valid_primary = (
                bind.primary
                if bind.primary in valid_write
                else (valid_write[0] if valid_write else None)
            )
        else:
            valid_primary = _validate_domain(primary)
        if any(domain not in bind.read for domain in valid_read):
            return {
                "error": "read scope is protected",
                "hint": "wiki_bind may narrow PostgreSQL read scope but cannot expand it",
            }
        if any(domain not in bind.write for domain in valid_write):
            return {
                "error": "write scope is protected",
                "hint": "wiki_bind may narrow PostgreSQL write scope but cannot expand it",
            }
        if any(domain not in valid_read for domain in valid_write):
            return {
                "error": "write scope must be a subset of read scope",
                "hint": "include every write domain in read scope",
            }
        if (
            (valid_write and valid_primary not in valid_write)
            or (not valid_write and valid_primary is not None)
        ):
            return {
                "error": "primary domain must belong to write scope",
                "hint": "select a primary from the narrowed write scope",
            }
        existing = set(_postgres_store_for_binding(bind).list_domains())
        missing = [
            domain
            for domain in (*valid_read, *valid_write)
            if domain not in existing
        ]
        if missing:
            return {
                "error": f"domain '{missing[0]}' not found",
                "hint": "ask an administrator to create the domain",
            }
        narrowed = replace(
            bind,
            read=tuple(dict.fromkeys(valid_read)),
            write=tuple(dict.fromkeys(valid_write)),
            primary=valid_primary,
        )
        session = _SESSION_BINDING.get()
        if isinstance(session, _HostedBindingState):
            session.set(narrowed)
        elif session is not None:
            _SESSION_BINDING.set(narrowed)
        else:
            _LOCAL_POSTGRES_BINDING = narrowed
        result = {
            "read": list(narrowed.read),
            "write": list(narrowed.write),
            "primary": narrowed.primary,
        }
        if session is None:
            result["project_dir"] = narrowed.project_dir
        return result
    return {
        "error": "project configuration cannot be changed automatically",
        "code": "project_config_manual_edit_required",
        "hint": (
            "edit .iwiki.toml manually; populated configuration is never "
            "rewritten automatically"
        ),
    }


@_safe
def wiki_bind(
    read: list[str] | None = None,
    write: list[str] | None = None,
    primary: str | None = None,
) -> dict:
    session = _SESSION_BINDING.get()
    if isinstance(session, _HostedBindingState):
        with session.locked():
            return _wiki_bind(read=read, write=write, primary=primary)
    return _wiki_bind(read=read, write=write, primary=primary)


@_safe
def wiki_lint(domain: str | None = None) -> dict:
    bind = _resolved_binding()
    if _is_postgres(bind):
        targets = [domain] if domain else list(bind.read)
        valid_targets = [
            _validate_domain(target)
            for target in targets
            if target in bind.read
        ]
        store = _postgres_store_for_binding(bind)
        reports = {
            target: store.lint_domain(target, list(bind.read))
            for target in valid_targets
        }
        return {"domains": list(reports), "reports": reports}
    targets = [domain] if domain else base.resolve_scope(bind, "project", None)
    valid_targets = [_validate_domain(target) for target in targets]
    visible_domains = {
        target: str(_domain_path(bind.base, target)) for target in valid_targets
    }
    reports = {}
    for target in valid_targets:
        report = lint(
            visible_domains[target],
            project_dir=bind.project_dir,
            domain=target,
            base_dir=bind.base,
            visible_domains=visible_domains,
        )
        try:
            if target != bind.primary:
                code_report = {
                    "available": False,
                    "state": "disabled",
                    "revision": None,
                    "findings": [],
                    "hint": "code graph follows the bound primary domain",
                }
            else:
                runtime = _codegraph_application.code_runtime(
                    _codegraph_application.source_context(bind)
                )
                code_report = _codegraph_linking.lint_domain(
                    visible_domains[target],
                    domain=target,
                    runtime=runtime,
                )
        except Exception:
            code_report = {
                "available": False,
                "state": "failed",
                "revision": None,
                "findings": [],
                "hint": "inspect wiki_code_status and retry",
            }
        report["code_graph"] = code_report
        reports[target] = report
    return {"domains": list(reports.keys()), "reports": reports}


@_safe
def wiki_remediation_plan(domain: str | None = None) -> dict:
    bind = base.resolve_binding()
    if not bind.primary:
        return {
            "error": "no write domain bound",
            "hint": "edit .iwiki.toml manually to set write",
        }
    target = _validate_domain(domain or bind.primary)
    if target != bind.primary:
        return {
            "error": "domain must match bound write domain",
            "hint": f"use the bound primary domain '{bind.primary}'",
        }
    dom_path = _domain_path(bind.base, target)
    base.migrate_store_location(bind.base, target)
    report = lint(str(dom_path), project_dir=bind.project_dir)

    update_candidates = []
    delete_candidates = []
    blocked_candidates = []
    ignore_spec = ignore.load_project_ignore(bind.project_dir)

    for finding in report.get("stale", []):
        page = finding.get("page", "")
        source = finding.get("source", "")
        try:
            slug = _slug_from_page_path(dom_path, page)
            current_markdown = _read_text(page)
        except Exception as e:
            blocked_candidates.append({
                "domain": target,
                "page": page,
                "source": source,
                "reason": "page_unreadable",
                "error": str(e),
            })
            continue
        if source and not _source_within_project(bind.project_dir, source):
            blocked_candidates.append({
                "domain": target,
                "slug": slug,
                "page": page,
                "source": source,
                "reason": "source_outside_project",
            })
            continue
        if source and ignore.is_ignored(ignore_spec, source, bind.project_dir):
            blocked_candidates.append({
                "domain": target,
                "page": page,
                "source": source,
                "reason": "source_ignored",
            })
            continue
        try:
            source_content, source_bytes, source_truncated = _read_source_preview(source)
        except OSError as e:
            blocked_candidates.append({
                "domain": target,
                "slug": slug,
                "page": page,
                "source": source,
                "reason": "source_unreadable",
                "error": str(e),
            })
            continue
        update_candidates.append({
            "domain": target,
            "slug": slug,
            "page": page,
            "source": source,
            "current_markdown": current_markdown,
            "source_content": source_content,
            "source_bytes": source_bytes,
            "source_truncated": source_truncated,
            "current_headings": _h2_headings(current_markdown),
            "recommended_tools": list(_UPDATE_REMEDIATION_TOOLS),
        })

    for finding in report.get("missing_source", []):
        page = finding.get("page", "")
        source = finding.get("source", "")
        try:
            slug = _slug_from_page_path(dom_path, page)
        except Exception as e:
            blocked_candidates.append({
                "domain": target,
                "page": page,
                "source": source,
                "reason": "page_unreadable",
                "error": str(e),
            })
            continue
        delete_candidates.append({
            "domain": target,
            "slug": slug,
            "page": page,
            "source": source,
            "recommended_tools": list(_DELETE_REMEDIATION_TOOLS),
        })

    return {
        "domain": target,
        "lint": report,
        "update_candidates": update_candidates,
        "delete_candidates": delete_candidates,
        "blocked_candidates": blocked_candidates,
        "authoring_rules": AUTHORING_RULES,
        "next_steps": list(_REMEDIATION_NEXT_STEPS),
    }


def _unmigrated_pages(dom_path: Path):
    """Yield (slug, page_file, body, has_frontmatter) for each page."""
    for path in sorted(dom_path.rglob("*.md")):
        rel = path.relative_to(dom_path)
        if rel.as_posix() in RESERVED_OKF:
            continue
        meta, body = _fm.split(path.read_text(encoding="utf-8"))
        yield rel.with_suffix("").as_posix(), rel.as_posix(), body, bool(meta)


@_safe
def wiki_migrate_okf(domain: str | None = None) -> dict:
    """Backfill missing frontmatter (autonomous when IWIKI_CHAT_MODEL is set,
    else a plan of candidates) and, in both modes, deterministically move every
    flat page that already carries a frontmatter `type` under `<type>/<slug>.md`
    (see okf.migrate_layout). Plan mode makes no LLM writes; the deterministic
    layout move is applied regardless of mode. Note: even in plan mode this
    layout move is itself a write -- the domain is always reindexed, and
    committed only when something actually moved."""
    bind = _resolved_binding()
    target = domain or bind.primary
    if not target:
        return {"error": "no domain given and no write-target bound",
                "hint": "pass domain= or edit write in .iwiki.toml manually"}
    target = _validate_domain(target)
    dom_path, scope_error = _existing_domain_write_guard(bind, target)
    if scope_error:
        return scope_error
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    if not dom_path.is_dir():
        return {"error": f"domain '{target}' not found",
                "hint": "create it with wiki_create_domain"}
    try:
        for page_path in sorted(dom_path.rglob("*.md")):
            if page_path.relative_to(dom_path).as_posix() in RESERVED_OKF:
                continue
            _fm.split(
                page_path.read_text(encoding="utf-8"), strict_code=True
            )
    except _fm.FrontmatterError as exc:
        return {
            "error": str(exc),
            "hint": "fix nested code frontmatter before migrating OKF",
        }
    base.migrate_store_location(bind.base, target)
    cfg = Config.load()
    graph_mutation = indexer.prepare_graph_mutation(
        bind.base, target, whole_domain=True
    )
    if cfg.chat_model:
        migrated, skipped, warnings = [], [], []
        vocab = okf.domain_tag_vocab(bind.base, target)
        for slug, page_file, body, has_fm in _unmigrated_pages(dom_path):
            if has_fm:
                skipped.append(slug)
                continue
            src = okf.latest_source(bind.base, target, page_file)
            fm_block, warn = okf.build_frontmatter(
                cfg, bind.base, target, slug, body,
                source=src, explicit_type=None, explicit_tags=None,
                timestamp_path=f"{target}/{page_file}", tag_vocab=vocab)
            (dom_path / page_file).write_text(fm_block + body, encoding="utf-8")
            migrated.append(slug)
            if warn:
                warnings.append({"slug": slug, "warning": warn})
            m, _ = _fm.split(fm_block)
            for t in m.get("tags", []):
                if t not in vocab:
                    vocab.append(t)
        # runs AFTER the adoption loop: it moves pages by their frontmatter
        # `type`, and the loop above is what just added `type` to flat pages.
        layout = okf.migrate_layout(bind.base, target)
        stats = indexer.index_domain(cfg, bind.base, target)
        commit = sync.commit_and_push(bind.base, f"iwiki: migrate okf {target}",
                                      pathspec=target,
                                      _after_commit=_after_commit_graph(
                                          graph_mutation, rebuild=True
                                      ))
        result = {"domain": target, "mode": "autonomous", "migrated": migrated,
                  "skipped": skipped, "warnings": warnings, "moved": layout["moved"],
                  "layout_collisions": layout.get("collisions", []),
                  "layout_skipped_unsafe": layout.get("skipped_unsafe", []),
                  "indexed_chunks": stats["indexed_chunks"],
                  **_write_sync_result(commit, fresh.get("warning"))}
        return result
    # plan mode: no LLM writes (frontmatter adoption is only proposed as
    # candidates); the deterministic <type>/<slug> layout move + store
    # relocation ARE applied below.
    layout = okf.migrate_layout(bind.base, target)
    indexer.index_domain(cfg, bind.base, target)   # store reflects moved paths
    if layout["moved"]:
        commit = sync.commit_and_push(bind.base, f"iwiki: migrate okf {target}",
                                      pathspec=target,
                                      _after_commit=_after_commit_graph(
                                          graph_mutation, rebuild=True
                                      ))
        graph_warning = None
    else:
        commit = {"committed": False, "pushed": False}
        callback = _after_commit_graph(graph_mutation, rebuild=True)
        graph_warning = callback() if callback is not None else None
    vocab = okf.domain_tag_vocab(bind.base, target)
    candidates = []
    for slug, page_file, body, has_fm in _unmigrated_pages(dom_path):
        if has_fm:
            continue
        candidates.append({
            "slug": slug,
            "body": body,
            "derived": {
                "title": _fm.derive_title(body, slug),
                "description": _fm.derive_description(body, cfg.summary_max),
                "timestamp": okf.git_last_commit_date(bind.base, f"{target}/{page_file}"),
            },
            "tag_vocab": vocab,
            "recommended_tools": ["wiki_apply_okf"],
        })
    return {"domain": target, "mode": "plan", "candidates": candidates,
            "moved": layout["moved"],
            "layout_collisions": layout.get("collisions", []),
            "layout_skipped_unsafe": layout.get("skipped_unsafe", []),
            "type_vocabulary": list(_fm.OKF_TYPES),
            "authoring_rules": AUTHORING_RULES,
            "next_steps": ["Classify each candidate's type (from type_vocabulary) "
                           "and tags (reuse tag_vocab first), then call "
                           "wiki_apply_okf(domain, slug, type, tags).",
                           "Run wiki_lint to confirm no missing_frontmatter remains."],
            **_write_sync_result(
                commit, fresh.get("warning"), graph_warning=graph_warning
            )}


def _apply_okf_page_move(
    bind: base.Binding,
    domain: str,
    slug: str,
    type: str,
    tags: list[str] | None,
    current_identity: str,
    new_identity: str,
    fresh: dict,
) -> dict:
    """Prepare and execute one type-changing page move transaction."""
    prepared = okf.prepare_page_move(
        bind.base, domain, current_identity, new_identity
    )
    current_file = f"{current_identity}.md"
    new_file = f"{new_identity}.md"
    current_path = _page_path(bind.base, domain, current_identity)
    original = Path(current_path).read_text(encoding="utf-8")
    existing_meta, _ = _fm.split(original, strict_code=True)
    target_edit = next(
        edit
        for edit in prepared.edits
        if edit.domain == domain and edit.file == new_file
    )
    _, rewritten_body = _fm.split(target_edit.after.decode("utf-8"))
    apply_tags = tags if tags is not None else (existing_meta.get("tags") or None)
    resolved = existing_meta.get("resource") or okf.latest_source(
        bind.base, domain, current_file
    )
    cfg = Config.load()
    fm_block, fm_warning = okf.build_frontmatter(
        cfg,
        bind.base,
        domain,
        slug,
        rewritten_body,
        source=resolved,
        explicit_type=type,
        explicit_tags=apply_tags,
        explicit_description=existing_meta.get("description"),
        explicit_status=existing_meta.get("status"),
        timestamp_path=f"{domain}/{current_file}",
        authored_code=existing_meta.get("code"),
    )

    edits = {(edit.domain, edit.file): edit for edit in prepared.edits}
    edits[(domain, new_file)] = cross_domain.PlannedEdit(
        domain, new_file, None, (fm_block + rewritten_body).encode("utf-8")
    )
    rewritten_pages = {
        f"{edit.domain}/{edit.file}"
        for edit in prepared.edits
        if edit.after is not None
        and edit.file.endswith(".md")
        and edit.file != new_file
    }

    target_page_id = f"{domain}/{current_identity}"
    visible_domains = tuple(base.resolve_scope(bind, "project", None))
    candidates = graph.incoming_candidates(
        bind.base, visible_domains, target_page_id
    )
    if candidates is None:
        try:
            candidates = graph.markdown_incoming_snapshot(
                bind.base, visible_domains, target_page_id
            ).candidates
        except graph.MarkdownSnapshotChanged as exc:
            raise cross_domain.CrossDomainError("source_changed") from exc
        except graph.GraphRuntimeError as exc:
            raise cross_domain.CrossDomainError("mutation_failed") from exc
    candidates = tuple(sorted(set(candidates), key=lambda item: (item.domain, item.file)))
    writable = set(base.writable_domains(bind))
    if any(candidate.domain not in writable for candidate in candidates):
        raise cross_domain.CrossDomainError("write_scope_blocked")

    rewrite = CrossDomainRewrite(domain, current_identity, new_identity)
    rewritten_links = prepared.rewritten_links
    for candidate in candidates:
        source = graph._read_scoped_markdown(
            bind.base, candidate.domain, candidate.file
        )
        if source is None:
            raise cross_domain.CrossDomainError("source_changed")
        source_key = (candidate.domain, candidate.file)
        destination_key = (
            (domain, new_file)
            if source_key == (domain, current_file)
            else source_key
        )
        existing_edit = edits.get(destination_key)
        content = existing_edit.after if existing_edit is not None else source
        if content is None:
            raise cross_domain.CrossDomainError("source_changed")
        rewritten, count = rewrite_cross_domain_links(
            content.decode("utf-8"), candidate.domain, rewrite
        )
        if not count:
            continue
        before_hash = (
            existing_edit.before_hash
            if existing_edit is not None
            else sha256(source).hexdigest()
        )
        edits[destination_key] = cross_domain.PlannedEdit(
            destination_key[0], destination_key[1], before_hash, rewritten.encode("utf-8")
        )
        rewritten_pages.add(f"{destination_key[0]}/{destination_key[1]}")
        rewritten_links += count

    affected_domains = tuple(sorted({edit.domain for edit in edits.values()}))
    plan = cross_domain.MutationPlan(
        operation=f"move {domain}/{current_identity} to {domain}/{new_identity}",
        transaction_id=secrets.token_hex(16),
        base_head=sync._head_revision(bind.base),
        edits=tuple(sorted(edits.values(), key=lambda edit: (edit.domain, edit.file))),
        affected_domains=affected_domains,
        rewritten_pages=tuple(sorted(rewritten_pages)),
        rewritten_links=rewritten_links,
    )
    evidence = cross_domain.execute_plan(bind.base, bind, plan)
    meta, _ = _fm.split(fm_block + rewritten_body)
    result = {
        "page": f"{domain}/{new_file}",
        "type": meta.get("type"),
        "tags": meta.get("tags", []),
        "indexed_chunks": len(VectorStore(base.index_path(bind.base, domain)).load()),
        **evidence,
    }
    warning = _compose_warnings(
        result.get("warning"), fresh.get("warning"), fm_warning
    )
    if warning:
        result["warning"] = warning
    else:
        result.pop("warning", None)
    return result


@_safe
def wiki_apply_okf(domain: str, slug: str, type: str,
                   tags: list[str] | None = None) -> dict:
    bind = _resolved_binding()
    valid_domain = _validate_domain(domain)
    dom_path, scope_error = _existing_domain_write_guard(bind, valid_domain)
    if scope_error:
        return scope_error
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    if not dom_path.is_dir():
        return {"error": f"domain '{valid_domain}' not found",
                "hint": "create it with wiki_create_domain"}
    base.migrate_store_location(bind.base, valid_domain)
    current_identity = PurePosixPath(*_slug_parts(slug)).as_posix()
    current_path = _page_path(bind.base, valid_domain, current_identity)
    # not-found guard MUST run on the CURRENT path BEFORE move_page: os.replace on a
    # missing source raises FileNotFoundError -> @_safe generic error, losing the
    # friendly "page not found" hint.
    if not os.path.isfile(current_path):
        return {"error": f"page '{valid_domain}/{current_identity}' not found",
                "hint": "list pages with wiki_list_pages"}
    try:
        _fm.split(
            Path(current_path).read_text(encoding="utf-8"), strict_code=True
        )
    except _fm.FrontmatterError as exc:
        return {
            "error": str(exc),
            "hint": "fix nested code frontmatter before applying OKF",
        }
    new_identity = _resolve_identity(_slug_parts(slug)[-1], _fm.normalize_type(type))
    move_change = okf.MoveChange((), ())
    if current_identity != new_identity:
        new_path = _page_path(bind.base, valid_domain, new_identity)
        if os.path.exists(new_path):
            return {"error": f"page '{valid_domain}/{new_identity}' exists",
                    "hint": "delete or rename the colliding page first"}
        if sync.is_git_repo(bind.base):
            return _apply_okf_page_move(
                bind,
                valid_domain,
                slug,
                type,
                tags,
                current_identity,
                new_identity,
                fresh,
            )
        move_change = okf.move_page(
            bind.base, valid_domain, current_identity, new_identity
        )
    graph_mutation = indexer.prepare_graph_mutation(bind.base, valid_domain)
    identity = new_identity
    page_file = identity + ".md"
    path = _page_path(bind.base, valid_domain, identity)
    original = open(path, encoding="utf-8").read()
    existing_meta, body = _fm.split(original, strict_code=True)
    apply_tags = tags if tags is not None else (existing_meta.get("tags") or None)
    apply_desc = existing_meta.get("description")
    apply_status = existing_meta.get("status")
    resolved = (
        existing_meta.get("resource")
        or okf.latest_source(bind.base, valid_domain, page_file)
    )
    cfg = Config.load()
    fm_block, fm_warning = okf.build_frontmatter(
        cfg, bind.base, valid_domain, slug, body,
        source=resolved, explicit_type=type, explicit_tags=apply_tags,
        explicit_description=apply_desc, explicit_status=apply_status,
        # git has no history yet at the NEW (just-moved) path -- look up the
        # PRE-move identity so an existing page's original timestamp survives
        # a type change instead of resetting to today.
        timestamp_path=f"{valid_domain}/{current_identity}.md",
        authored_code=existing_meta.get("code"))
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(fm_block + body)
        stats = indexer.index_domain(cfg, bind.base, valid_domain)
    except Exception:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(original)
        raise
    page_rel = f"{valid_domain}/{page_file}"
    refresh_files = tuple(sorted({*move_change.refresh_files, page_file}))
    commit = sync.commit_and_push(bind.base, f"iwiki: apply okf {page_rel}",
                                  pathspec=valid_domain,
                                  _after_commit=_after_commit_graph(
                                      graph_mutation,
                                      refresh_files=refresh_files,
                                      delete_files=move_change.delete_files,
                                  ))
    meta, _ = _fm.split(fm_block + body)
    result = {"page": page_rel, "type": meta.get("type"), "tags": meta.get("tags", []),
              "indexed_chunks": stats["indexed_chunks"],
              **_write_sync_result(
                  commit, fresh.get("warning"), fm_warning
              )}
    return result


@_safe
def wiki_export_okf(domain: str | None = None) -> dict:
    bind = _resolved_binding()
    target = domain or bind.primary
    if not target:
        return {"error": "no domain given and no write-target bound",
                "hint": "pass domain= or edit write in .iwiki.toml manually"}
    valid_domain = _validate_domain(target)
    dom_path, scope_error = _existing_domain_write_guard(bind, valid_domain)
    if scope_error:
        return scope_error
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    if not dom_path.is_dir():
        return {"error": f"domain '{valid_domain}' not found",
                "hint": "create it with wiki_create_domain"}
    cfg = Config.load()
    base.migrate_store_location(bind.base, valid_domain)
    graph_mutation = indexer.prepare_graph_mutation(
        bind.base, valid_domain, whole_domain=True
    )
    try:
        swept = okf.batch_sweep(cfg, bind.base, valid_domain)
    except _fm.FrontmatterError as exc:
        return {
            "error": str(exc),
            "hint": "fix nested code frontmatter before exporting OKF",
        }
    stats = indexer.index_domain(cfg, bind.base, valid_domain)
    art_warn = okf.refresh_artifacts(bind.base, valid_domain)
    commit = sync.commit_and_push(bind.base, f"iwiki: export okf {valid_domain}",
                                  pathspec=valid_domain,
                                  _after_commit=_after_commit_graph(
                                      graph_mutation, rebuild=True
                                  ))
    report = lint(str(dom_path), project_dir=bind.project_dir)
    result = {
        "domain": valid_domain,
        "fixed_links": swept["fixed_links"],
        "added_frontmatter": swept["added_frontmatter"],
        "artifacts": list(RESERVED_OKF),
        "still_missing_frontmatter": report.get("missing_frontmatter", []),
        "still_legacy_wikilink": report.get("legacy_wikilink", []),
        "indexed_chunks": stats["indexed_chunks"],
        **_write_sync_result(commit, fresh.get("warning")),
        "next_steps": ["Run wiki_migrate_okf for better type/tags than the "
                       "deterministic 'concept' default on newly added frontmatter."],
    }
    if art_warn:
        result["warning"] = _compose_warnings(result.get("warning"), art_warn)
    return result


@_safe
def wiki_sync() -> dict:
    bind = _resolved_binding()
    return sync.sync(bind.base)


# PostgreSQL rejects Git-only tools before any local lock, Git, filesystem, or
# SQLite helper can run.
wiki_remediation_plan = _postgres_unsupported_guard(wiki_remediation_plan)
wiki_migrate_okf = _postgres_unsupported_guard(wiki_migrate_okf)
wiki_apply_okf = _postgres_unsupported_guard(wiki_apply_okf)
wiki_export_okf = _postgres_unsupported_guard(wiki_export_okf)
wiki_sync = _postgres_unsupported_guard(wiki_sync)


# Every overlapping mutation recovers journals before any other side effect.
wiki_write_page = _mutation_guard(wiki_write_page)
wiki_update_page = _mutation_guard(wiki_update_page)
wiki_insert_section = _mutation_guard(wiki_insert_section)
wiki_delete_section = _mutation_guard(wiki_delete_section)
wiki_move_section = _mutation_guard(wiki_move_section)
wiki_delete_page = _mutation_guard(wiki_delete_page)
wiki_index = _mutation_guard(wiki_index)
wiki_create_domain = _mutation_guard(wiki_create_domain)
wiki_migrate_okf = _mutation_guard(wiki_migrate_okf)
wiki_apply_okf = _mutation_guard(wiki_apply_okf)
wiki_export_okf = _mutation_guard(wiki_export_okf)
wiki_sync = _mutation_guard(wiki_sync)


# Thin MCP wrappers; implementation functions above stay unit-testable.
mcp.tool()(wiki_status)
mcp.tool()(wiki_code_status)
mcp.tool()(wiki_code_index)
mcp.tool()(wiki_code_search)
mcp.tool()(wiki_code_context)
mcp.tool()(wiki_code_publish_begin)
mcp.tool()(wiki_code_publish_batch)
mcp.tool()(wiki_code_publish_finalize)
mcp.tool()(wiki_code_publish_abort)
mcp.tool()(wiki_list_domains)
mcp.tool()(wiki_list_pages)
mcp.tool()(wiki_read_page)
mcp.tool()(wiki_search)
mcp.tool()(wiki_related)
mcp.tool()(wiki_write_page)
mcp.tool()(wiki_update_page)
mcp.tool()(wiki_insert_section)
mcp.tool()(wiki_delete_section)
mcp.tool()(wiki_move_section)
mcp.tool()(wiki_delete_page)
mcp.tool()(wiki_index)
mcp.tool()(wiki_create_domain)
mcp.tool()(wiki_list_domain_grants)
mcp.tool()(wiki_set_domain_grant)
mcp.tool()(wiki_revoke_domain_grant)
mcp.tool()(wiki_bind)
mcp.tool()(wiki_lint)
mcp.tool()(wiki_remediation_plan)
mcp.tool()(wiki_migrate_okf)
mcp.tool()(wiki_apply_okf)
mcp.tool()(wiki_export_okf)
mcp.tool()(wiki_sync)


@mcp.resource("iwiki://authoring-rules")
def authoring_rules() -> str:
    return AUTHORING_RULES


def _redact_startup_value(value: str, api_key: str) -> str:
    return value.replace(api_key, "<redacted>") if api_key else value


def _print_startup_failure(reason: str, cfg: Config | None = None) -> None:
    base_url = os.environ.get("IWIKI_LLM_BASE_URL", "").strip()
    if cfg is not None:
        endpoint = f"{cfg.base_url}/embeddings"
        model = cfg.embed_model or "<not set>"
        api_key = cfg.api_key
    else:
        if base_url.endswith("/"):
            base_url = base_url[:-1]
        endpoint = f"{base_url}/embeddings" if base_url else "<not set>"
        model = os.environ.get(
            "IWIKI_EMBED_MODEL", "text-embedding-3-small"
        ).strip() or "<not set>"
        api_key = os.environ.get("IWIKI_LLM_KEY", "").strip()
    print("iwiki-mcp: startup failed", file=sys.stderr)
    print(f"Embeddings endpoint: {_redact_startup_value(endpoint, api_key)}", file=sys.stderr)
    print(f"Model: {_redact_startup_value(model, api_key)}", file=sys.stderr)
    print(f"Reason: {_redact_startup_value(reason, api_key)}", file=sys.stderr)
    print(
        "Hint: verify IWIKI_LLM_BASE_URL, IWIKI_LLM_KEY, "
        "IWIKI_EMBED_MODEL, and IWIKI_EMBED_DIMENSIONS",
        file=sys.stderr,
    )


def _initialize_postgres_storage(cfg: Config) -> None:
    project_dir = base.resolve_project_dir()
    project_config = base.load_project_config(project_dir)
    storage = project_config.get("storage")
    if not isinstance(storage, dict) or storage.get("type") != "postgres":
        return
    binding = base.resolve_storage_binding(project_dir)
    if not _is_postgres(binding):
        return
    _postgres_migrations.require_schema_version(
        binding.connection_dsn(), expected_version=6
    )


def main() -> None:
    import argparse

    argv = sys.argv[1:]
    if _admin.is_admin_command(argv):
        raise SystemExit(_admin.run(argv))

    p = argparse.ArgumentParser(prog="iwiki-mcp")
    p.add_argument("--project", help="project dir (overrides cwd / IWIKI_PROJECT_DIR)")
    args = p.parse_args(argv)
    if args.project:
        os.environ["IWIKI_PROJECT_DIR"] = os.path.abspath(args.project)
    cfg = None
    try:
        cfg = Config.load()
        _initialize_postgres_storage(cfg)
        probe_embedding_endpoint(cfg)
    except (
        base.BaseError,
        ConfigError,
        EmbedError,
        _postgres_migrations.MigrationError,
    ) as exc:
        _print_startup_failure(str(exc), cfg)
        raise SystemExit(1) from None
    mcp.set_idle_timeout(cfg.idle_timeout_seconds)
    try:
        mcp.run()
    finally:
        _codegraph_runtime.shutdown_code_graph_workers()


if __name__ == "__main__":
    main()
