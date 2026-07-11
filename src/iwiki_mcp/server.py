"""iwiki MCP server (stdio).

Tools are fail-soft: every handler returns a JSON-serializable dict, and
exceptions become {"error","hint"} structures.
"""
from __future__ import annotations

import datetime as _dt
import functools
import json
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

from mcp.server.fastmcp import FastMCP

from . import base, ignore, indexer, okf, retrieval, sync
from .engine import frontmatter as _fm
from .engine.config import Config, ConfigError
from .engine.embed import EmbedError
from .engine.links import to_markdown_links
from .engine.okf_artifacts import RESERVED_OKF
from .engine.section import SectionError, replace_section
from .engine.validate import validate_page
from .resources import AUTHORING_RULES

mcp = FastMCP("iwiki")

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


def _safe(fn):
    @functools.wraps(fn)
    def wrap(*a, **k):
        try:
            return fn(*a, **k)
        except base.BaseError as e:
            return {"error": str(e), "hint": "set IWIKI_BASE_DIR or run wiki_bind"}
        except (ConfigError, EmbedError) as e:
            return {
                "error": f"HALT: {e}",
                "hint": "set IWIKI_LLM_BASE_URL / IWIKI_LLM_KEY",
            }
        except Exception as e:
            return {"error": str(e), "hint": "unexpected error; see server logs"}

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
    """Store the ingest source relative to the project. An already-relative
    path passes through; an absolute path under the project is relativized; an
    absolute path outside the project is rejected (the server works only within
    the bound project)."""
    p = Path(source)
    if not p.is_absolute():
        return source
    proj = Path(project_dir).resolve()
    try:
        return p.resolve().relative_to(proj).as_posix()
    except ValueError:
        raise ValueError("source outside project")


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
        "write": bind.write,
        "project_dir": bind.project_dir,
        "domains": base.list_domains(bind.base),
    }


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
    mode: str = "hybrid",
    domains: list[str] | None = None,
    k: int | None = None,
    threshold: float | None = None,
    type: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    bind = base.resolve_binding()
    cfg = Config.load()
    doms = [_validate_domain(d) for d in base.resolve_scope(bind, scope, domains)]
    if not doms:
        return {"results": [], "hint": "no domains in scope"}
    q_type = (type.strip().lower() or None) if type else None
    q_tags = _fm.normalize_tags(tags) if tags else None
    q_tags = q_tags or None
    results = retrieval.hybrid_search(
        cfg,
        bind.base,
        doms,
        query,
        top_k=cfg.top_k if k is None else k,
        threshold=cfg.score_threshold if threshold is None else threshold,
        mode=mode,
        type=q_type,
        tags=q_tags,
    )
    return {"results": results}


@_safe
def wiki_related(domain: str, section_id: str) -> dict:
    from .engine.related import related
    from .engine.store import VectorStore

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


@_safe
def wiki_write_page(
    domain: str, slug: str, markdown: str, source: str | None = None,
    type: str | None = None, tags: list[str] | None = None,
    description: str | None = None, status: str | None = None,
) -> dict:
    bind = base.resolve_binding()
    valid_domain = _validate_domain(domain)
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    dom_path = _domain_path(bind.base, valid_domain)
    if not dom_path.is_dir():
        return {
            "error": f"domain '{valid_domain}' not found",
            "hint": "create it with wiki_create_domain",
        }
    base.migrate_store_location(bind.base, valid_domain)
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
        timestamp_path=f"{valid_domain}/{slug}.md")
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
    if PurePosixPath(page_file).name in RESERVED_OKF:
        return {
            "error": f"slug tail is reserved for the generated OKF file "
                     f"'{PurePosixPath(page_file).name}'",
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
                                  pathspec=valid_domain)
    result = {
        "page": page_rel,
        "indexed_chunks": stats["indexed_chunks"],
        "bytes": stats["bytes"],
        "over_cap": stats["over_cap"],
        "committed": commit.get("committed", False),
        "pushed": commit.get("pushed", False),
        **_fresh_warn(fresh),
    }
    if fm_warning:
        result.setdefault("warning", fm_warning)
    return result


@_safe
def wiki_update_page(
    domain: str, slug: str, heading: str, new_body: str, source: str | None = None,
    description: str | None = None, status: str | None = None,
) -> dict:
    bind = base.resolve_binding()
    valid_domain = _validate_domain(domain)
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    dom_path = _domain_path(bind.base, valid_domain)
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
    meta, original_body = _fm.split(original_full)
    new_body = to_markdown_links(new_body)
    try:
        new_body = replace_section(original_body, heading, new_body)
    except SectionError as e:
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
    log_file = base.log_path(bind.base, valid_domain)
    log_before = None
    if source and os.path.exists(log_file):
        with open(log_file, "rb") as fh:
            log_before = fh.read()
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
                                  pathspec=valid_domain)
    result = {
        "page": page_rel,
        "heading": heading.lstrip("#").strip(),
        "indexed_chunks": stats["indexed_chunks"],
        "reused": stats["reused"],
        "embedded": stats["embedded"],
        "bytes": stats["bytes"],
        "over_cap": stats["over_cap"],
        "committed": commit.get("committed", False),
        "pushed": commit.get("pushed", False),
        **_fresh_warn(fresh),
    }
    return result


@_safe
def wiki_delete_page(domain: str, slug: str) -> dict:
    bind = base.resolve_binding()
    valid_domain = _validate_domain(domain)
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    dom_path = _domain_path(bind.base, valid_domain)
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
                                  pathspec=valid_domain)
    result = {
        "deleted": page_rel,
        "indexed_chunks": stats["indexed_chunks"],
        "bytes": stats["bytes"],
        "committed": commit.get("committed", False),
        "pushed": commit.get("pushed", False),
        **_fresh_warn(fresh),
    }
    return result


@_safe
def wiki_index(domain: str | None = None) -> dict:
    bind = base.resolve_binding()
    target = domain or bind.write
    if not target:
        return {
            "error": "no domain given and no write-target bound",
            "hint": "pass domain= or set write in .iwiki.toml via wiki_bind",
        }
    valid_domain = _validate_domain(target)
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    dom_path = _domain_path(bind.base, valid_domain)
    if not dom_path.is_dir():
        return {
            "error": f"domain '{valid_domain}' not found",
            "hint": "create it with wiki_create_domain",
        }
    cfg = Config.load()
    stats = indexer.index_domain(cfg, bind.base, valid_domain)
    commit = sync.commit_and_push(bind.base, f"iwiki: reindex {valid_domain}",
                                  pathspec=valid_domain)
    return {"domain": valid_domain, **stats,
            "committed": commit.get("committed", False),
            "pushed": commit.get("pushed", False),
            **_fresh_warn(fresh)}


@_safe
def wiki_create_domain(name: str) -> dict:
    bind = base.resolve_binding()
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
def wiki_bind(read: list[str] | None = None, write: str | None = None) -> dict:
    bind = base.resolve_binding()
    current_domain = _validate_domain(base.current_project_domain(bind.project_dir))
    valid_read = None if read is None else [_validate_domain(d) for d in read]
    valid_write = None if write is None else _validate_domain(write)
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
    if valid_write is not None:
        if not _domain_path(bind.base, valid_write).is_dir():
            return {
                "error": f"domain '{valid_write}' not found",
                "hint": "create it with wiki_create_domain",
            }
        if valid_write != current_domain:
            return {
                "error": "write domain must match current project domain",
                "hint": f"use write='{current_domain}' for this project",
            }
    elif bind.write is not None and bind.write != current_domain:
        return {
            "error": "write domain must match current project domain",
            "hint": f"use write='{current_domain}' for this project",
        }
    base.write_project_config(bind.project_dir, read=merged_read, write=valid_write)
    ignore.ensure_iwikiignore(bind.project_dir)
    new = base.resolve_binding()
    return {"read": list(new.read), "write": new.write, "project_dir": new.project_dir}


@_safe
def wiki_lint(domain: str | None = None) -> dict:
    from .engine.lint import lint

    bind = base.resolve_binding()
    targets = [domain] if domain else base.resolve_scope(bind, "project", None)
    reports = {}
    for target in targets:
        valid_domain = _validate_domain(target)
        base.migrate_store_location(bind.base, valid_domain)
        reports[valid_domain] = lint(
            str(_domain_path(bind.base, valid_domain)), project_dir=bind.project_dir
        )
    return {"domains": list(reports.keys()), "reports": reports}


@_safe
def wiki_remediation_plan(domain: str | None = None) -> dict:
    from .engine.lint import lint

    bind = base.resolve_binding()
    if not bind.write:
        return {
            "error": "no write domain bound",
            "hint": "set write in .iwiki.toml via wiki_bind",
        }
    target = _validate_domain(domain or bind.write)
    if target != bind.write:
        return {
            "error": "domain must match bound write domain",
            "hint": f"use the bound write domain '{bind.write}'",
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
        if source and ignore.is_ignored(ignore_spec, source, bind.project_dir):
            blocked_candidates.append({
                "domain": target,
                "page": page,
                "source": source,
                "reason": "source_ignored",
            })
            continue
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
    bind = base.resolve_binding()
    target = domain or bind.write
    if not target:
        return {"error": "no domain given and no write-target bound",
                "hint": "pass domain= or set write in .iwiki.toml via wiki_bind"}
    target = _validate_domain(target)
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    dom_path = _domain_path(bind.base, target)
    if not dom_path.is_dir():
        return {"error": f"domain '{target}' not found",
                "hint": "create it with wiki_create_domain"}
    base.migrate_store_location(bind.base, target)
    cfg = Config.load()
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
                                      pathspec=target)
        result = {"domain": target, "mode": "autonomous", "migrated": migrated,
                  "skipped": skipped, "warnings": warnings, "moved": layout["moved"],
                  "layout_collisions": layout.get("collisions", []),
                  "indexed_chunks": stats["indexed_chunks"],
                  "committed": commit.get("committed", False),
                  "pushed": commit.get("pushed", False), **_fresh_warn(fresh)}
        return result
    # plan mode: no LLM writes (frontmatter adoption is only proposed as
    # candidates); the deterministic <type>/<slug> layout move + store
    # relocation ARE applied below.
    layout = okf.migrate_layout(bind.base, target)
    indexer.index_domain(cfg, bind.base, target)   # store reflects moved paths
    if layout["moved"]:
        commit = sync.commit_and_push(bind.base, f"iwiki: migrate okf {target}",
                                      pathspec=target)
    else:
        commit = {"committed": False, "pushed": False}
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
            "type_vocabulary": list(_fm.OKF_TYPES),
            "authoring_rules": AUTHORING_RULES,
            "next_steps": ["Classify each candidate's type (from type_vocabulary) "
                           "and tags (reuse tag_vocab first), then call "
                           "wiki_apply_okf(domain, slug, type, tags).",
                           "Run wiki_lint to confirm no missing_frontmatter remains."],
            "committed": commit.get("committed", False),
            "pushed": commit.get("pushed", False),
            **_fresh_warn(fresh)}


@_safe
def wiki_apply_okf(domain: str, slug: str, type: str,
                   tags: list[str] | None = None) -> dict:
    bind = base.resolve_binding()
    valid_domain = _validate_domain(domain)
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    dom_path = _domain_path(bind.base, valid_domain)
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
    new_identity = _resolve_identity(_slug_parts(slug)[-1], _fm.normalize_type(type))
    if current_identity != new_identity:
        new_path = _page_path(bind.base, valid_domain, new_identity)
        if os.path.exists(new_path):
            return {"error": f"page '{valid_domain}/{new_identity}' exists",
                    "hint": "delete or rename the colliding page first"}
        # move_page's link rewrite is best-effort; if a later step (frontmatter
        # write, index) fails below, the rollback restores the original bytes at
        # the NEW path but does not move the file back — acceptable, the page
        # keeps valid structure at its new identity and the next index run
        # reconciles it.
        okf.move_page(bind.base, valid_domain, current_identity, new_identity)
    identity = new_identity
    page_file = identity + ".md"
    path = _page_path(bind.base, valid_domain, identity)
    original = open(path, encoding="utf-8").read()
    existing_meta, body = _fm.split(original)
    apply_tags = tags if tags is not None else (existing_meta.get("tags") or None)
    apply_desc = existing_meta.get("description")
    apply_status = existing_meta.get("status")
    resolved = (
        existing_meta.get("resource")
        # log entries are keyed by the page name at ingest time, i.e. the
        # PRE-move name when this call also moved the page: look up under
        # current_identity, not the (possibly moved-to) page_file.
        or okf.latest_source(bind.base, valid_domain, current_identity + ".md")
    )
    cfg = Config.load()
    fm_block, _ = okf.build_frontmatter(
        cfg, bind.base, valid_domain, slug, body,
        source=resolved, explicit_type=type, explicit_tags=apply_tags,
        explicit_description=apply_desc, explicit_status=apply_status,
        # git has no history yet at the NEW (just-moved) path -- look up the
        # PRE-move identity so an existing page's original timestamp survives
        # a type change instead of resetting to today.
        timestamp_path=f"{valid_domain}/{current_identity}.md")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(fm_block + body)
        stats = indexer.index_domain(cfg, bind.base, valid_domain)
    except Exception:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(original)
        raise
    page_rel = f"{valid_domain}/{page_file}"
    commit = sync.commit_and_push(bind.base, f"iwiki: apply okf {page_rel}",
                                  pathspec=valid_domain)
    meta, _ = _fm.split(fm_block + body)
    result = {"page": page_rel, "type": meta.get("type"), "tags": meta.get("tags", []),
              "indexed_chunks": stats["indexed_chunks"],
              "committed": commit.get("committed", False),
              "pushed": commit.get("pushed", False), **_fresh_warn(fresh)}
    return result


@_safe
def wiki_export_okf(domain: str | None = None) -> dict:
    bind = base.resolve_binding()
    target = domain or bind.write
    if not target:
        return {"error": "no domain given and no write-target bound",
                "hint": "pass domain= or set write in .iwiki.toml via wiki_bind"}
    valid_domain = _validate_domain(target)
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    dom_path = _domain_path(bind.base, valid_domain)
    if not dom_path.is_dir():
        return {"error": f"domain '{valid_domain}' not found",
                "hint": "create it with wiki_create_domain"}
    cfg = Config.load()
    base.migrate_store_location(bind.base, valid_domain)
    swept = okf.batch_sweep(cfg, bind.base, valid_domain)
    stats = indexer.index_domain(cfg, bind.base, valid_domain)
    art_warn = okf.refresh_artifacts(bind.base, valid_domain)
    commit = sync.commit_and_push(bind.base, f"iwiki: export okf {valid_domain}",
                                  pathspec=valid_domain)
    from .engine.lint import lint
    report = lint(str(dom_path), project_dir=bind.project_dir)
    result = {
        "domain": valid_domain,
        "fixed_links": swept["fixed_links"],
        "added_frontmatter": swept["added_frontmatter"],
        "artifacts": list(RESERVED_OKF),
        "still_missing_frontmatter": report.get("missing_frontmatter", []),
        "still_legacy_wikilink": report.get("legacy_wikilink", []),
        "indexed_chunks": stats["indexed_chunks"],
        "committed": commit.get("committed", False),
        "pushed": commit.get("pushed", False),
        "next_steps": ["Run wiki_migrate_okf for better type/tags than the "
                       "deterministic 'concept' default on newly added frontmatter."],
        **_fresh_warn(fresh),
    }
    if art_warn:
        result.setdefault("warning", art_warn)
    return result


@_safe
def wiki_sync() -> dict:
    bind = base.resolve_binding()
    return sync.sync(bind.base)


# Thin MCP wrappers; implementation functions above stay unit-testable.
mcp.tool()(wiki_status)
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


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(prog="iwiki-mcp")
    p.add_argument("--project", help="project dir (overrides cwd / IWIKI_PROJECT_DIR)")
    args = p.parse_args()
    if args.project:
        os.environ["IWIKI_PROJECT_DIR"] = os.path.abspath(args.project)
    mcp.run()


if __name__ == "__main__":
    main()
