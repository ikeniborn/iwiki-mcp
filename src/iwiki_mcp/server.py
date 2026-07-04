"""iwiki MCP server (stdio).

Tools are fail-soft: every handler returns a JSON-serializable dict, and
exceptions become {"error","hint"} structures.
"""
from __future__ import annotations

import functools
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath

from mcp.server.fastmcp import FastMCP

from . import base, ignore, indexer, retrieval, sync
from .engine.config import Config, ConfigError
from .engine.embed import EmbedError
from .engine.section import SectionError, replace_section
from .engine.validate import validate_page
from .resources import AUTHORING_RULES

mcp = FastMCP("iwiki")


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
        if ".iwiki" in rel_path.parts:
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
) -> dict:
    bind = base.resolve_binding()
    cfg = Config.load()
    doms = [_validate_domain(d) for d in base.resolve_scope(bind, scope, domains)]
    if not doms:
        return {"results": [], "hint": "no domains in scope"}
    results = retrieval.hybrid_search(
        cfg,
        bind.base,
        doms,
        query,
        top_k=cfg.top_k if k is None else k,
        threshold=cfg.score_threshold if threshold is None else threshold,
        mode=mode,
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
    domain: str, slug: str, markdown: str, source: str | None = None
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
    blocking = [f for f in validate_page(markdown) if f.get("type") in _BLOCKING]
    if blocking:
        return {
            "error": "section structure invalid",
            "findings": blocking,
            "hint": "use only ## headings; no text before the first ##",
        }
    if source:
        spec = ignore.load_project_ignore(bind.project_dir)
        if ignore.is_ignored(spec, source, bind.project_dir):
            return {
                "error": "source matches .iwikiignore",
                "hint": f"'{source}' is excluded by .iwikiignore; "
                        "remove the pattern to ingest, or omit source",
            }
    path = _page_path(bind.base, valid_domain, slug)
    if os.path.exists(path):
        return {
            "error": f"page '{valid_domain}/{slug}' exists",
            "hint": "editing an existing page is a guarded op; confirm with the user",
        }
    cfg = Config.load()
    page_file = PurePosixPath(*_slug_parts(slug)).as_posix() + ".md"
    log_source = source or ""
    log_src_hash = indexer.src_hash(source) if source else None
    log_appended = False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(markdown)
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
    return {
        "page": page_rel,
        "indexed_chunks": stats["indexed_chunks"],
        "bytes": stats["bytes"],
        "over_cap": stats["over_cap"],
        "committed": commit.get("committed", False),
        "pushed": commit.get("pushed", False),
        **_fresh_warn(fresh),
    }


@_safe
def wiki_update_page(
    domain: str, slug: str, heading: str, new_body: str, source: str | None = None
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
    if source:
        spec = ignore.load_project_ignore(bind.project_dir)
        if ignore.is_ignored(spec, source, bind.project_dir):
            return {
                "error": "source matches .iwikiignore",
                "hint": f"'{source}' is excluded by .iwikiignore; "
                        "remove the pattern to ingest, or omit source",
            }
    path = _page_path(bind.base, valid_domain, slug)
    if not os.path.isfile(path):
        return {
            "error": f"page '{valid_domain}/{slug}' not found",
            "hint": "list pages with wiki_list_pages",
        }
    original = open(path, encoding="utf-8").read()
    try:
        new_md = replace_section(original, heading, new_body)
    except SectionError as e:
        return {"error": str(e), "hint": "check the heading with wiki_read_page"}
    blocking = [f for f in validate_page(new_md) if f.get("type") in _BLOCKING]
    if blocking:
        return {
            "error": "section structure invalid",
            "findings": blocking,
            "hint": "new_body must use only ## headings; no ###+, no pre-## text",
        }
    cfg = Config.load()
    page_file = PurePosixPath(*_slug_parts(slug)).as_posix() + ".md"
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
            fh.write(original)
        if source:            # mirrors the upsert gate above
            _restore_log(log_file, log_before)
        raise
    page_rel = f"{valid_domain}/{page_file}"
    commit = sync.commit_and_push(bind.base, f"iwiki: update {page_rel}",
                                  pathspec=valid_domain)
    return {
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
    return {
        "deleted": page_rel,
        "indexed_chunks": stats["indexed_chunks"],
        "bytes": stats["bytes"],
        "committed": commit.get("committed", False),
        "pushed": commit.get("pushed", False),
        **_fresh_warn(fresh),
    }


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
    os.makedirs(dom_path / ".iwiki", exist_ok=True)
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
        reports[valid_domain] = lint(
            str(_domain_path(bind.base, valid_domain)), project_dir=bind.project_dir
        )
    return {"domains": list(reports.keys()), "reports": reports}


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
