"""iwiki MCP server (stdio).

Tools are fail-soft: every handler returns a JSON-serializable dict, and
exceptions become {"error","hint"} structures.
"""
from __future__ import annotations

import functools
import os
from pathlib import Path, PurePosixPath, PureWindowsPath

from mcp.server.fastmcp import FastMCP

from . import base, indexer, retrieval, sync
from .engine.config import Config, ConfigError
from .engine.embed import EmbedError

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
    recs = VectorStore(base.index_path(bind.base, _validate_domain(domain))).load()
    return related(section_id, recs, cfg.top_k, cfg.graph_depth)


# Thin MCP wrappers; implementation functions above stay unit-testable.
mcp.tool()(wiki_status)
mcp.tool()(wiki_list_domains)
mcp.tool()(wiki_list_pages)
mcp.tool()(wiki_read_page)
mcp.tool()(wiki_search)
mcp.tool()(wiki_related)


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
