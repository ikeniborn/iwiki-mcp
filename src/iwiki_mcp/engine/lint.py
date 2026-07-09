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

from . import frontmatter as _fm
from .links import parse_links, slugify_heading, has_legacy_wikilink
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


def _source_exists(src: str, project_dir: str | None) -> bool:
    """Does the ingest source resolve to a real file? Absolute paths are checked
    as-is; a relative path is resolved against project_dir (when known) and the
    cwd. Any hit means the source still exists."""
    if os.path.isabs(src):
        return os.path.isfile(src)
    cands = [os.path.join(project_dir, src)] if project_dir else []
    cands.append(src)  # cwd-relative fallback
    return any(os.path.isfile(c) for c in cands)


def _missing_source(wiki_dir: str, project_dir: str | None) -> list[dict]:
    """Pages whose recorded (non-empty) source no longer exists on disk — the
    deletion candidates surfaced by wiki_lint. Uses the latest ingest per page."""
    out: list[dict] = []
    for page_path, rec in _latest_ingest_by_page(wiki_dir).items():
        src = rec["source"]
        if os.path.isfile(page_path) and not _source_exists(src, project_dir):
            out.append({"page": page_path, "source": src})
    return out


def _edit_distance_le1(a: str, b: str) -> bool:
    """True if a and b differ by at most one insert/delete/substitution."""
    if a == b:
        return False
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long = (a, b) if la < lb else (b, a)
    i = j = 0
    skipped = False
    while i < len(short) and j < len(long):
        if short[i] == long[j]:
            i += 1
            j += 1
        elif skipped:
            return False
        else:
            skipped = True
            j += 1
    return True


def _tag_drift(all_tags: set[str]) -> list[dict]:
    tags = sorted(all_tags)
    out = []
    for i, a in enumerate(tags):
        for b in tags[i + 1:]:
            if b.startswith(a) or a.startswith(b) or _edit_distance_le1(a, b):
                out.append({"tags": [a, b]})
    return out


def lint(wiki_dir: str, project_dir: str | None = None) -> dict:
    """Health report over docs/wiki/. Absent/empty wiki → {"wiki_present": false}."""
    if not os.path.isdir(wiki_dir):
        return {"wiki_present": False}
    pages = _pages(wiki_dir)
    if not pages:
        return {"wiki_present": False}

    raw = {p: _read(p) for p in pages}
    meta_body = {p: _fm.split(c) for p, c in raw.items()}
    content = {p: mb[1] for p, mb in meta_body.items()}   # body only
    headings = {p: _headings(c) for p, c in content.items()}

    missing_frontmatter = [p for p, (meta, _) in meta_body.items() if not meta]
    all_tags = set()
    for meta, _ in meta_body.values():
        for t in meta.get("tags", []) or []:
            all_tags.add(_fm.normalize_tag(t))
    all_tags.discard("")

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
                if heading not in {slugify_heading(h) for h in hs}:
                    broken.append({"page": page, "ref": ref})

    orphans = [p for p in pages if not (referenced_by.get(p, set()) - {p})]
    legacy_wikilink = sorted(p for p, c in content.items() if has_legacy_wikilink(c))
    sections = [{"page": p, **f} for p in pages
                for f in validate_page(raw[p])]
    return {"wiki_present": True, "pages": len(pages),
            "broken": broken, "orphans": orphans, "stale": _stale(wiki_dir),
            "missing_source": _missing_source(wiki_dir, project_dir),
            "legacy_wikilink": legacy_wikilink,
            "sections": sections,
            "missing_frontmatter": missing_frontmatter,
            "tag_drift": _tag_drift(all_tags)}
