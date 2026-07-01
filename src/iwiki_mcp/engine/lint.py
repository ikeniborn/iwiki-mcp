"""Deterministic, config-free wiki health checks — no embedding call.

Mirrors the `status` subcommand's contract: stdlib only (plus the in-package
link/heading parsers), so it imports without httpx and runs in any project.
An absent or empty docs/wiki/ is a clean no-op ({"wiki_present": false}), never
an error — this is the fix for the exit-2 seen in foreign projects.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re

from .links import parse_links
from .validate import validate_page

# Keep in sync with chunk._H2 — inlined here to avoid importing .chunk, which
# would transitively pull httpx and break the config-free contract.
_H2 = re.compile(r"^##\s+(.*?)\s*$", re.MULTILINE)


def _pages(wiki_dir: str) -> list[str]:
    """All docs/wiki/**/*.md (normalised), excluding the .iwiki index dir."""
    files = glob.glob(os.path.join(wiki_dir, "**", "*.md"), recursive=True)
    return sorted(os.path.normpath(f) for f in files if "/.iwiki/" not in f)


def _read(path: str) -> str:
    """Read a page, fail-soft to '' — a health check must never crash on one
    unreadable page (permissions / race)."""
    try:
        return open(path, encoding="utf-8").read()
    except Exception:
        return ""


def _headings(content: str) -> set[str]:
    return {m.group(1).strip() for m in _H2.finditer(content)}


def _resolve(slug: str, wiki_dir: str) -> str:
    """A link target (slug or path) → the wiki file it points at.
    'b' → <wiki>/b.md; 'sub/p' → <wiki>/sub/p.md; '*.md' → joined as-is."""
    t = slug.strip()
    if not t.endswith(".md"):
        t += ".md"
    return os.path.normpath(os.path.join(wiki_dir, t))


def _src_hash(src: str) -> str | None:
    """sha256 of the source's raw bytes, first 16 hex chars. None when the file
    cannot be read — the caller then falls back to the mtime comparison."""
    try:
        with open(src, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:16]
    except OSError:
        return None


def _fresh(src: str, page: str, src_hash: str | None) -> bool:
    """Is `page` current for `src`? Content-addressed when the log record
    carries `src_hash` and the source is readable; otherwise the page is fresh
    iff it is at least as new as the source by mtime (the prior behaviour)."""
    if src_hash:
        cur = _src_hash(src)
        if cur is not None:
            return cur == src_hash
    return os.path.getmtime(page) >= os.path.getmtime(src)


def _logged_page_path(page: str, wiki_dir: str) -> str:
    """Resolve ingest-log page paths to files in the domain wiki directory."""
    if os.path.isabs(page):
        return os.path.normpath(page)
    return os.path.normpath(os.path.join(wiki_dir, page))


def _latest_ingest_by_page(wiki_dir: str) -> dict[str, dict]:
    """Latest ingest record per page from .iwiki/log.jsonl (last-wins).

    An `ingest` record with a non-empty source sets the page's current record;
    a `delete` record clears it. Last-wins so a delete + re-ingest of the same
    slug is judged by the NEW source, not a stale earlier record. Legacy records
    without an `op` are treated as ingests (back-compat). Malformed lines, records
    without a page, and records without a source are ignored.
    """
    log = os.path.join(wiki_dir, ".iwiki", "log.jsonl")
    latest: dict[str, dict] = {}
    if not os.path.isfile(log):
        return latest
    try:
        lines = open(log, encoding="utf-8").read().splitlines()
    except Exception:
        return latest
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        page = rec.get("page")
        if not page:
            continue
        page_path = _logged_page_path(page, wiki_dir)
        if rec.get("op") == "delete":
            latest.pop(page_path, None)
            continue
        src = rec.get("source")
        if not src:
            continue
        latest[page_path] = {"page": page_path, "source": src,
                             "src_hash": rec.get("src_hash")}
    return latest


def _stale(wiki_dir: str) -> list[dict]:
    """Pages whose source changed after the last ingest (content-hash with mtime
    fallback; no git), from the latest ingest record per page."""
    out: list[dict] = []
    for page_path, rec in _latest_ingest_by_page(wiki_dir).items():
        src = rec["source"]
        if os.path.isfile(src) and os.path.isfile(page_path):
            try:
                if not _fresh(src, page_path, rec.get("src_hash")):
                    out.append({"page": page_path, "source": src})
            except Exception:
                pass
    return out


def lint(wiki_dir: str) -> dict:
    """Health report over docs/wiki/. Absent/empty wiki → {"wiki_present": false}."""
    if not os.path.isdir(wiki_dir):
        return {"wiki_present": False}
    pages = _pages(wiki_dir)
    if not pages:
        return {"wiki_present": False}

    content = {p: _read(p) for p in pages}
    headings = {p: _headings(c) for p, c in content.items()}

    broken: list[dict] = []
    referenced_by: dict[str, set[str]] = {}
    for page, c in content.items():
        for ref in parse_links(c):
            slug, _, heading = ref.partition("#")
            target = _resolve(slug, wiki_dir)
            referenced_by.setdefault(target, set()).add(page)
            if not os.path.isfile(target):
                broken.append({"page": page, "ref": ref})
                continue
            if heading:
                hs = headings.get(target)
                if hs is None:  # target exists but outside the page set
                    try:
                        hs = _headings(open(target, encoding="utf-8").read())
                    except Exception:
                        hs = set()
                if heading.strip() not in hs:
                    broken.append({"page": page, "ref": ref})

    orphans = [p for p in pages if not (referenced_by.get(p, set()) - {p})]
    sections = [{"page": p, **f} for p, c in content.items()
                for f in validate_page(c)]
    return {"wiki_present": True, "pages": len(pages),
            "broken": broken, "orphans": orphans, "stale": _stale(wiki_dir),
            "sections": sections}
