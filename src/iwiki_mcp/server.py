"""iwiki MCP server (stdio).

Tools are fail-soft: every handler returns a JSON-serializable dict, and
exceptions become {"error","hint"} structures.
"""
from __future__ import annotations

import functools
import os
from pathlib import Path, PurePosixPath

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


def _slug_parts(slug: str) -> tuple[str, ...]:
    path = PurePosixPath(slug)
    if path.is_absolute() or not path.parts or any(part in (".", "..") for part in path.parts):
        raise ValueError(f"invalid page slug '{slug}'")
    return path.parts


def _page_path(b: str, domain: str, slug: str) -> str:
    return os.path.join(base.domain_dir(b, domain), *_slug_parts(slug)) + ".md"


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
    dom = base.domain_dir(bind.base, domain)
    if not os.path.isdir(dom):
        return {
            "error": f"domain '{domain}' not found",
            "hint": "create it with wiki_create_domain",
        }
    pages = []
    dom_path = Path(dom)
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


# Thin MCP wrappers; implementation functions above stay unit-testable.
mcp.tool()(wiki_status)
mcp.tool()(wiki_list_domains)
mcp.tool()(wiki_list_pages)
mcp.tool()(wiki_read_page)


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
