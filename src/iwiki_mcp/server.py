"""iwiki MCP server (stdio).

Tools are fail-soft: every handler returns a JSON-serializable dict, and
exceptions become {"error","hint"} structures.
"""
from __future__ import annotations

import datetime as _dt
import functools
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
import json
import logging
import os
import re
import secrets
import sys
import time
from contextvars import ContextVar
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.stdio import stdio_server

from . import base, cross_domain, graph, ignore, indexer, okf, retrieval, sync
from .postgres import migrations as _postgres_migrations  # noqa: F401
from .postgres import store as _postgres_store  # noqa: F401
# Code graph adapters join the full startup import closure; their grammar and
# parser initialization remains lazy until an adapter parses source.
from .codegraph import config as _codegraph_config  # noqa: F401
from .codegraph import discovery as _codegraph_discovery  # noqa: F401
from .codegraph import fingerprint as _codegraph_fingerprint  # noqa: F401
from .codegraph import indexer as _codegraph_indexer
from .codegraph import linking as _codegraph_linking
from .codegraph import location as _codegraph_location  # noqa: F401
from .codegraph import models as _codegraph_models  # noqa: F401
from .codegraph import runtime as _codegraph_runtime  # noqa: F401
from .codegraph import schema as _codegraph_schema  # noqa: F401
from .codegraph import store as _codegraph_store  # noqa: F401
from .codegraph import languages as _codegraph_languages  # noqa: F401
from .codegraph.languages import python as _codegraph_python  # noqa: F401
from .lock import mutation_lock
from .engine import rerank
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
from .engine.section import SectionError, replace_section
from .engine.store import VectorStore
from .engine.validate import validate_page
from .resources import AUTHORING_RULES


LOGGER = logging.getLogger(__name__)


def _distribution_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unavailable"


_PYTHON_PARSER_VERSION = (
    "tree-sitter-python:" + _distribution_version("tree-sitter-python")
)


def _code_graph_adapter_factories(repository_id):
    def create_python_adapter(source_paths):
        return _codegraph_python.PythonAdapter(
            repository_id,
            source_paths,
            parser_version=_PYTHON_PARSER_VERSION,
        )

    return {
        "python": _codegraph_indexer.AdapterFactory(
            create=create_python_adapter,
            extensions=(".py",),
            parser_version=_PYTHON_PARSER_VERSION,
            grammar_version=";".join((
                "tree-sitter:" + _distribution_version("tree-sitter"),
                "tree-sitter-language-pack:"
                + _distribution_version("tree-sitter-language-pack"),
                _PYTHON_PARSER_VERSION,
            )),
            adapter_version="python-adapter-v2",
        )
    }


def _code_runtime(binding: base.Binding):
    """Compose configured language adapters without initializing parsers."""
    runtime = _codegraph_runtime.CodeGraphRuntime(
        binding,
        adapter_factories=_code_graph_adapter_factories(binding.primary),
    )
    if runtime._indexer is not None:
        runtime._indexer.wiki_selector_resolver = (
            _codegraph_linking.WikiSelectorResolver(binding.base)
        )
    return runtime


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
    "Use wiki_delete_page then wiki_write_page when the article structure must change.",
    "Use wiki_delete_page for missing_source delete candidates.",
    "Run wiki_lint and report planned, updated, deleted, failed, and remaining_lint.",
]

_UPDATE_REMEDIATION_TOOLS = [
    "wiki_update_page",
    "wiki_delete_page",
    "wiki_write_page",
    "wiki_lint",
]

_DELETE_REMEDIATION_TOOLS = ["wiki_delete_page", "wiki_lint"]

_MUTATION_BINDING: ContextVar[base.Binding | None] = ContextVar(
    "iwiki_mutation_binding", default=None
)


def _resolved_binding() -> base.Binding:
    return _MUTATION_BINDING.get() or base.resolve_binding()


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
            return {"error": str(e), "hint": "set IWIKI_BASE_DIR or run wiki_bind"}
        except (ConfigError, EmbedError) as e:
            return {
                "error": f"HALT: {e}",
                "hint": "set IWIKI_LLM_BASE_URL / IWIKI_LLM_KEY",
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
                bind = base.resolve_binding()
            except base.BaseError:
                if fn.__name__ != "wiki_create_domain":
                    raise
                bind = _creation_binding()
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
                "hint": "set IWIKI_BASE_DIR or run wiki_bind",
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
    bind = base.resolve_binding()
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


@_safe
@_code_safe
def wiki_code_status() -> dict:
    bind = base.resolve_binding()
    if bind.primary is None:
        return _missing_code_primary()
    return _code_runtime(bind).status()


@_safe
@_code_safe
def wiki_code_index(
    force: bool = False,
    languages: list[str] | None = None,
) -> dict:
    if languages is not None and (
        not languages or any(language != "python" for language in languages)
    ):
        return _invalid_code_config()
    bind = base.resolve_binding()
    if bind.primary is None:
        return _missing_code_primary()
    return _code_runtime(bind).index(force=force, languages=languages)


@_safe
@_code_safe
def wiki_code_search(
    query: str,
    kinds: list[str] | None = None,
    path: str | None = None,
    languages: list[str] | None = None,
    limit: int = 20,
) -> dict:
    _codegraph_runtime.validate_search_request(
        query,
        kinds=kinds,
        path=path,
        languages=languages,
        limit=limit,
    )
    bind = base.resolve_binding()
    if bind.primary is None:
        return _missing_code_primary()
    return _code_runtime(bind).search(
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
    bind = base.resolve_binding()
    if bind.primary is None:
        return _missing_code_primary()
    return _code_runtime(bind).context(
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
def wiki_list_domains() -> dict:
    bind = base.resolve_binding()
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
    bind = base.resolve_binding()
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
def wiki_read_page(domain: str, slug: str) -> dict:
    bind = base.resolve_binding()
    path = _page_path(bind.base, domain, slug)
    if not os.path.isfile(path):
        return {
            "error": f"page '{domain}/{slug}' not found",
            "hint": "list pages with wiki_list_pages",
        }
    return {
        "domain": domain,
        "slug": slug,
        "markdown": open(path, encoding="utf-8").read(),
    }


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
    bind = base.resolve_binding()
    cfg = Config.load()
    if intent.strip().lower() == "write":
        target = bind.primary or (domains[0] if domains else None)
        if not target:
            return {"target": {"exists": False}, "hint": "no write-target domain in scope"}
        target = _validate_domain(target)      # path guards are load-bearing
        return {"target": retrieval.locate_target(cfg, bind.base, target, query, heading)}
    resolved_mode = cfg.search_mode if mode is None else mode.strip().lower()
    allowed_modes = ("hybrid", "lexical", "semantic")
    if resolved_mode not in allowed_modes:
        return {"error": "invalid search mode; allowed values: hybrid, lexical, semantic"}
    doms = [_validate_domain(d) for d in base.resolve_scope(bind, scope, domains)]
    if not doms:
        return {"results": [], "hint": "no domains in scope"}
    q_type = (type.strip().lower() or None) if type else None
    q_tags = _fm.normalize_tags(tags) if tags else None
    q_tags = q_tags or None
    requested_top_k = cfg.top_k if k is None else k
    page_cache = {}
    try:
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
        return {"error": str(exc)}
    results = candidates[:requested_top_k]
    response = {"results": results}
    if cfg.rerank_model:
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
    bind = base.resolve_binding()
    cfg = Config.load()
    valid_domain = _validate_domain(domain)
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


@_safe
def wiki_write_page(
    domain: str, slug: str, markdown: str, source: str | None = None,
    type: str | None = None, tags: list[str] | None = None,
    description: str | None = None, status: str | None = None,
) -> dict:
    bind = _resolved_binding()
    valid_domain = _validate_domain(domain)
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
        bind.base, bind, plan, _include_index_stats=True
    )
    index_stats = evidence.pop("_index_stats")[domain]
    result = {
        "page": f"{domain}/{page_file}",
        "heading": heading.lstrip("#").strip(),
        **index_stats,
        **evidence,
    }
    warning = _compose_warnings(result.get("warning"), fresh.get("warning"))
    if warning:
        result["warning"] = warning
    else:
        result.pop("warning", None)
    return result


@_safe
def wiki_update_page(
    domain: str, slug: str, heading: str, new_body: str, source: str | None = None,
    description: str | None = None, status: str | None = None,
    new_heading: str | None = None,
) -> dict:
    bind = _resolved_binding()
    valid_domain = _validate_domain(domain)
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
def wiki_delete_page(domain: str, slug: str) -> dict:
    bind = _resolved_binding()
    valid_domain = _validate_domain(domain)
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
    page_rel = f"{valid_domain}/{page_file}"
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
            "hint": "pass domain= or set write in .iwiki.toml via wiki_bind",
        }
    valid_domain = _validate_domain(target)
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
    stats = indexer.index_domain(cfg, bind.base, valid_domain)
    commit = sync.commit_and_push(bind.base, f"iwiki: reindex {valid_domain}",
                                  pathspec=valid_domain,
                                  _after_commit=_after_commit_graph(
                                      graph_mutation, rebuild=True
                                  ))
    return {
        "domain": valid_domain,
        **stats,
        **_write_sync_result(commit, fresh.get("warning")),
    }


@_safe
def wiki_create_domain(name: str) -> dict:
    bind = _resolved_binding()
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


@_safe
def wiki_bind(
    read: list[str] | None = None,
    write: list[str] | None = None,
    primary: str | None = None,
) -> dict:
    bind = base.resolve_binding()
    current_domain = _validate_domain(base.current_project_domain(bind.project_dir))
    valid_read = None if read is None else [_validate_domain(d) for d in read]
    valid_write = None if write is None else [_validate_domain(d) for d in write]
    valid_primary = None if primary is None else _validate_domain(primary)
    merged_read = None
    if valid_read is not None:
        merged, read_error = base.merge_read_scope(
            bind.read,
            valid_read,
            current_domain,
        )
        if read_error:
            return {
                "error": read_error,
                "hint": "existing read scope is preserved; only the current "
                        "project domain may be appended automatically",
            }
        merged_read = list(merged)

    for domain in valid_read or ():
        if not _domain_path(bind.base, domain).is_dir():
            return {
                "error": f"domain '{domain}' not found",
                "hint": "create it with wiki_create_domain",
            }
    for domain in valid_write or ():
        if not _domain_path(bind.base, domain).is_dir():
            return {
                "error": f"domain '{domain}' not found",
                "hint": "create it with wiki_create_domain",
            }
    candidate_read = tuple(merged_read) if merged_read is not None else bind.read
    candidate_write = tuple(valid_write) if valid_write is not None else bind.write
    candidate_primary = valid_primary
    if candidate_primary is None:
        candidate_primary = (
            bind.primary if bind.primary in candidate_write else
            (candidate_write[0] if candidate_write else None)
        )
    if candidate_primary != current_domain:
        return {
            "error": "write domain must match current project domain",
            "hint": f"include '{current_domain}' in write and set it as primary",
        }
    try:
        base._resolved_write_domains(
            bind.base,
            candidate_read,
            candidate_write,
            candidate_primary,
        )
    except base.BaseError as exc:
        return {
            "error": str(exc),
            "hint": "write domains must exist and be included in read",
        }
    base.write_project_config(
        bind.project_dir,
        read=merged_read,
        write=valid_write,
        primary=valid_primary,
    )
    ignore.ensure_iwikiignore(bind.project_dir)
    new = base.resolve_binding()
    return {
        "read": list(new.read),
        "write": list(new.write),
        "primary": new.primary,
        "project_dir": new.project_dir,
    }


@_safe
def wiki_lint(domain: str | None = None) -> dict:
    bind = base.resolve_binding()
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
                runtime = _code_runtime(bind)
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
            "hint": "set write in .iwiki.toml via wiki_bind",
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
                "hint": "pass domain= or set write in .iwiki.toml via wiki_bind"}
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
                "hint": "pass domain= or set write in .iwiki.toml via wiki_bind"}
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


# Every overlapping mutation recovers journals before any other side effect.
wiki_write_page = _mutation_guard(wiki_write_page)
wiki_update_page = _mutation_guard(wiki_update_page)
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
mcp.tool()(wiki_list_domains)
mcp.tool()(wiki_list_pages)
mcp.tool()(wiki_read_page)
mcp.tool()(wiki_search)
mcp.tool()(wiki_related)
mcp.tool()(wiki_write_page)
mcp.tool()(wiki_update_page)
mcp.tool()(wiki_delete_page)
mcp.tool()(wiki_index)
mcp.tool()(wiki_create_domain)
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


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(prog="iwiki-mcp")
    p.add_argument("--project", help="project dir (overrides cwd / IWIKI_PROJECT_DIR)")
    args = p.parse_args()
    if args.project:
        os.environ["IWIKI_PROJECT_DIR"] = os.path.abspath(args.project)
    cfg = None
    try:
        cfg = Config.load()
        probe_embedding_endpoint(cfg)
    except (ConfigError, EmbedError) as exc:
        _print_startup_failure(str(exc), cfg)
        raise SystemExit(1) from None
    mcp.set_idle_timeout(cfg.idle_timeout_seconds)
    try:
        mcp.run()
    finally:
        _codegraph_runtime.shutdown_code_graph_workers()


if __name__ == "__main__":
    main()
