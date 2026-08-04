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
import shutil
import sqlite3
import tempfile
from pathlib import Path

from . import frontmatter as _fm
from .links import (
    has_legacy_wikilink,
    parse_heading_anchors,
    parse_link_targets,
)
from .okf_artifacts import RESERVED_OKF
from .validate import validate_page


def _pages(wiki_dir: str) -> list[str]:
    """All docs/wiki/**/*.md (normalised), excluding the generated OKF reserved
    files (index.md / log.md)."""
    files = glob.glob(os.path.join(wiki_dir, "**", "*.md"), recursive=True)
    out = []
    for f in files:
        if os.path.relpath(f, wiki_dir) in RESERVED_OKF:
            continue
        out.append(os.path.normpath(f))
    return sorted(out)


def _read(path: str) -> str:
    """Read a page, fail-soft to '' — a health check must never crash on one
    unreadable page (permissions / race)."""
    try:
        return open(path, encoding="utf-8").read()
    except Exception:
        return ""


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
    """Latest ingest record per page from <domain>/log.jsonl (last-wins).

    An `ingest` record with a non-empty source sets the page's current record;
    a `delete` record clears it. Last-wins so a delete + re-ingest of the same
    slug is judged by the NEW source, not a stale earlier record. Legacy records
    without an `op` are treated as ingests (back-compat). Malformed lines, records
    without a page, and records without a source are ignored.
    """
    log = os.path.join(wiki_dir, "log.jsonl")
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


def _page_id(domain: str, path: str, wiki_dir: str) -> str:
    file = Path(os.path.relpath(path, wiki_dir)).as_posix()
    return f"{domain}/{file[:-3]}"


def _visible_markdown(
    visible_domains: dict[str, str]
) -> tuple[set[str], dict[str, set[str]]]:
    pages: set[str] = set()
    anchors: dict[str, set[str]] = {}
    for domain, wiki_dir in visible_domains.items():
        for path in _pages(wiki_dir):
            page_id = _page_id(domain, path, wiki_dir)
            _, body = _fm.split(_read(path))
            pages.add(page_id)
            anchors[page_id] = {
                anchor.anchor for anchor in parse_heading_anchors(body)
            }
    return pages, anchors


def _finding_ref(target) -> str:
    if target.kind == "cross":
        return target.raw_target
    return (
        f"{target.target_page}#{target.target_anchor}"
        if target.target_anchor
        else target.target_page
    )


def _missing_graph_report(domain: str) -> dict:
    return {
        "available": False,
        "schema_version": None,
        "state": "missing",
        "fingerprint_match": None,
        "missing_pages": [],
        "extra_pages": [],
        "missing_edges": [],
        "extra_edges": [],
        "anchor_mismatches": [],
        "reason": "graph database is missing",
        "hint": f"run wiki_index('{domain}')",
    }


def _unavailable_graph_report(
    domain: str,
    *,
    state: str,
    reason: str,
    schema_version: int | None = None,
) -> dict:
    report = _missing_graph_report(domain)
    report.update({
        "schema_version": schema_version,
        "state": state,
        "reason": reason,
    })
    return report


def _expected_graph(domain: str, wiki_dir: str) -> dict:
    page_records = set()
    anchors = set()
    edges = set()
    for path in _pages(wiki_dir):
        file = Path(os.path.relpath(path, wiki_dir)).as_posix()
        page_id = f"{domain}/{file[:-3]}"
        content = _read(path)
        page_edges = tuple(
            sorted(
                (
                    page_id,
                    f"{target.target_domain}/{target.target_page}",
                    target.target_anchor,
                    target.kind,
                    target.raw_target,
                )
                for target in parse_link_targets(content, domain)
                if not target.is_reserved
            )
        )
        normalized_links = [
            (edge[1], edge[2], edge[3]) for edge in page_edges
        ]
        link_hash = hashlib.sha256(
            json.dumps(normalized_links, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        page_records.add((
            page_id,
            domain,
            file,
            hashlib.sha256(content.encode("utf-8")).hexdigest(),
            link_hash,
        ))
        anchors.update(
            (page_id, anchor.anchor, anchor.heading)
            for anchor in parse_heading_anchors(content)
        )
        edges.update(page_edges)
    return {"pages": page_records, "anchors": anchors, "edges": edges}


def _page_dict(record: tuple) -> dict:
    return dict(zip(
        ("page_id", "domain", "file", "content_hash", "link_hash"),
        record,
    ))


def _edge_dict(record: tuple) -> dict:
    return dict(zip(
        (
            "source_page_id",
            "target_page_id",
            "target_anchor",
            "kind",
            "raw_target",
        ),
        record,
    ))


def _anchor_mismatches(expected: set[tuple], actual: set[tuple]) -> list[dict]:
    expected_by_page: dict[str, set[tuple[str, str]]] = {}
    actual_by_page: dict[str, set[tuple[str, str]]] = {}
    for page_id, anchor, heading in expected:
        expected_by_page.setdefault(page_id, set()).add((anchor, heading))
    for page_id, anchor, heading in actual:
        actual_by_page.setdefault(page_id, set()).add((anchor, heading))
    mismatches = []
    for page_id in sorted(set(expected_by_page) | set(actual_by_page)):
        missing = expected_by_page.get(page_id, set()) - actual_by_page.get(
            page_id, set()
        )
        extra = actual_by_page.get(page_id, set()) - expected_by_page.get(
            page_id, set()
        )
        if missing or extra:
            mismatches.append({
                "page_id": page_id,
                "missing": [
                    {"anchor": anchor, "heading": heading}
                    for anchor, heading in sorted(missing)
                ],
                "extra": [
                    {"anchor": anchor, "heading": heading}
                    for anchor, heading in sorted(extra)
                ],
            })
    return mismatches


def _file_stamp(path: Path) -> tuple[int, int, int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _graph_stamps(path: Path) -> tuple[tuple[int, int, int, int] | None, ...]:
    wal_path = path.with_name(path.name + "-wal")
    return (_file_stamp(path), _file_stamp(wal_path))


def _busy_graph_report(domain: str) -> dict:
    return _unavailable_graph_report(
        domain,
        state="busy",
        reason="graph database is busy",
    )


def _graph_report(base_dir: str, domain: str, expected: dict) -> dict:
    from .graph_store import SCHEMA_VERSION

    path = Path(base_dir) / ".iwiki" / "graph.sqlite3"
    if not path.is_file():
        return _missing_graph_report(domain)
    temporary = None
    try:
        stamps_before = _graph_stamps(path)
        wal_before = stamps_before[1]
        query_path = path
        if wal_before is not None and wal_before[2] > 0:
            temporary = tempfile.TemporaryDirectory(prefix="iwiki-lint-")
            query_path = Path(temporary.name) / path.name
            shutil.copyfile(path, query_path)
            shutil.copyfile(
                path.with_name(path.name + "-wal"),
                query_path.with_name(query_path.name + "-wal"),
            )
            if _graph_stamps(path) != stamps_before:
                temporary.cleanup()
                return _busy_graph_report(domain)
        with query_path.open("rb") as handle:
            header = handle.read(20)
        if header[:16] != b"SQLite format 3\x00":
            report = _unavailable_graph_report(
                domain,
                state="corrupt",
                reason="graph database is corrupt",
            )
        else:
            report = None
    except OSError:
        if temporary is not None:
            temporary.cleanup()
        return _unavailable_graph_report(
            domain,
            state="unavailable",
            reason="graph database is unavailable",
        )
    connection = None
    try:
        if report is None:
            immutable = temporary is None and header[18:20] == b"\x02\x02"
            uri = query_path.resolve().as_uri() + "?mode=ro"
            if immutable:
                uri += "&immutable=1"
            connection = sqlite3.connect(uri, uri=True, timeout=0.05)
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA busy_timeout = 50")
            schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if schema_version != SCHEMA_VERSION:
                report = _unavailable_graph_report(
                    domain,
                    state="incompatible",
                    reason="graph schema is incompatible",
                    schema_version=schema_version,
                )
            else:
                domain_row = connection.execute(
                    "SELECT markdown_fingerprint, state FROM domains "
                    "WHERE domain = ?",
                    (domain,),
                ).fetchone()
                actual_pages = {
                    tuple(row)
                    for row in connection.execute(
                        "SELECT page_id, domain, file, content_hash, link_hash "
                        "FROM pages WHERE domain = ?",
                        (domain,),
                    )
                }
                actual_anchors = {
                    tuple(row)
                    for row in connection.execute(
                        "SELECT page_id, anchor, heading FROM anchors "
                        "WHERE substr(page_id, 1, ?) = ?",
                        (len(domain) + 1, f"{domain}/"),
                    )
                }
                actual_edges = {
                    tuple(row)
                    for row in connection.execute(
                        "SELECT source_page_id, target_page_id, target_anchor, "
                        "kind, raw_target FROM edges "
                        "WHERE substr(source_page_id, 1, ?) = ?",
                        (len(domain) + 1, f"{domain}/"),
                    )
                }
                state = "missing" if domain_row is None else domain_row[1]
                fingerprint_match = False
                if domain_row is not None:
                    try:
                        from iwiki_mcp.graph import markdown_fingerprint

                        fingerprint_match = (
                            markdown_fingerprint(base_dir, domain).value
                            == domain_row[0]
                        )
                    except Exception:
                        fingerprint_match = None
                missing_pages = sorted(expected["pages"] - actual_pages)
                extra_pages = sorted(actual_pages - expected["pages"])
                missing_edges = sorted(expected["edges"] - actual_edges)
                extra_edges = sorted(actual_edges - expected["edges"])
                anchor_mismatches = _anchor_mismatches(
                    expected["anchors"], actual_anchors
                )
                report = {
                    "available": True,
                    "schema_version": schema_version,
                    "state": state,
                    "fingerprint_match": fingerprint_match,
                    "missing_pages": [
                        _page_dict(record) for record in missing_pages
                    ],
                    "extra_pages": [
                        _page_dict(record) for record in extra_pages
                    ],
                    "missing_edges": [
                        _edge_dict(record) for record in missing_edges
                    ],
                    "extra_edges": [
                        _edge_dict(record) for record in extra_edges
                    ],
                    "anchor_mismatches": anchor_mismatches,
                }
                if (
                    state != "ready"
                    or fingerprint_match is not True
                    or missing_pages
                    or extra_pages
                    or missing_edges
                    or extra_edges
                    or anchor_mismatches
                ):
                    report["hint"] = f"run wiki_index('{domain}')"
    except sqlite3.OperationalError as exc:
        message = str(exc).casefold()
        if "locked" in message or "busy" in message:
            report = _busy_graph_report(domain)
        else:
            report = _unavailable_graph_report(
                domain,
                state="unavailable",
                reason="graph database is unavailable",
            )
    except sqlite3.DatabaseError:
        report = _unavailable_graph_report(
            domain,
            state="corrupt",
            reason="graph database is corrupt",
        )
    finally:
        if connection is not None:
            connection.close()
        if temporary is not None:
            temporary.cleanup()
    try:
        stamps_after = _graph_stamps(path)
    except OSError:
        return _unavailable_graph_report(
            domain,
            state="unavailable",
            reason="graph database is unavailable",
        )
    if stamps_after != stamps_before:
        return _busy_graph_report(domain)
    return report


def lint(
    wiki_dir: str,
    project_dir: str | None = None,
    *,
    domain: str | None = None,
    base_dir: str | None = None,
    visible_domains: dict[str, str] | None = None,
) -> dict:
    """Health report over one domain without mutating its derived graph."""
    explicit_graph = domain is not None and base_dir is not None
    if not os.path.isdir(wiki_dir):
        return {"wiki_present": False}
    pages = _pages(wiki_dir)
    if not pages and not explicit_graph:
        return {"wiki_present": False}

    raw = {p: _read(p) for p in pages}
    meta_body = {p: _fm.split(c) for p, c in raw.items()}
    content = {p: mb[1] for p, mb in meta_body.items()}   # body only
    domain = domain or os.path.basename(os.path.normpath(wiki_dir))
    base_dir = base_dir or os.path.dirname(os.path.normpath(wiki_dir))
    visible_domains = dict(visible_domains or {domain: wiki_dir})
    visible_domains.setdefault(domain, wiki_dir)
    visible_pages, visible_anchors = _visible_markdown(visible_domains)

    missing_frontmatter = [p for p, (meta, _) in meta_body.items() if not meta]
    all_tags = set()
    for meta, _ in meta_body.values():
        for t in meta.get("tags", []) or []:
            all_tags.add(_fm.normalize_tag(t))
    all_tags.discard("")

    broken: list[dict] = []
    reserved_target: list[dict] = []
    unavailable_domain: list[dict] = []
    referenced_by: dict[str, set[str]] = {}
    for page, c in content.items():
        for target in parse_link_targets(c, domain):
            ref = _finding_ref(target)
            if target.is_reserved:
                reserved_target.append({"page": page, "ref": ref})
                continue
            if target.target_domain not in visible_domains:
                unavailable_domain.append({
                    "page": page,
                    "ref": ref,
                    "domain": target.target_domain,
                })
                continue
            target_id = f"{target.target_domain}/{target.target_page}"
            if target.kind == "intra":
                target_path = _resolve(target.target_page, wiki_dir)
                referenced_by.setdefault(target_path, set()).add(page)
            if target_id not in visible_pages:
                broken.append({"page": page, "ref": ref})
                continue
            if (
                target.target_anchor
                and target.target_anchor not in visible_anchors.get(target_id, set())
            ):
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
            "tag_drift": _tag_drift(all_tags),
            "reserved_target": reserved_target,
            "unavailable_domain": unavailable_domain,
            "graph": _graph_report(
                base_dir, domain, _expected_graph(domain, wiki_dir)
            )}
