"""Top-layer OKF frontmatter assembly: deterministic field derivation plus the
type/tags precedence (explicit params -> optional server-side classify ->
default). Kept out of the engine because it reaches git and the index."""
from __future__ import annotations
import datetime as _dt
import json
import subprocess
from pathlib import Path

from .engine import classify, frontmatter as fm
from .engine import okf_artifacts as _oa
from .engine.store import VectorStore
from . import base as _base


def git_last_commit_date(base_dir: str, path: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", base_dir, "log", "-1", "--format=%cs", "--", path],
            capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:
        return None


def domain_tag_vocab(base_dir: str, domain: str) -> list:
    seen: list = []
    for r in VectorStore(_base.index_path(base_dir, domain)).load():
        for t in r.tags or []:
            if t not in seen:
                seen.append(t)
    return seen


def build_frontmatter(cfg, base_dir, domain, slug, body, *, source,
                      explicit_type, explicit_tags, timestamp_path, tag_vocab=None):
    """Return (frontmatter_block, warning). Precedence: explicit -> classify -> default."""
    warning = None
    if explicit_type is not None:
        mtype = fm.normalize_type(explicit_type)
        mtags = fm.normalize_tags(explicit_tags or [])
    elif cfg.chat_model:
        vocab = tag_vocab if tag_vocab is not None else domain_tag_vocab(base_dir, domain)
        r = classify.classify_page(cfg, body, vocab)
        mtype = r["type"]
        mtags = fm.normalize_tags(explicit_tags) if explicit_tags else r["tags"]
        warning = r["warning"]
    else:
        mtype = fm.DEFAULT_TYPE
        mtags = fm.normalize_tags(explicit_tags or [])
        warning = "type not given and IWIKI_CHAT_MODEL unset; defaulted to concept"

    meta: dict = {"type": mtype, "title": fm.derive_title(body, slug)}
    desc = fm.derive_description(body, cfg.summary_max)
    if desc:
        meta["description"] = desc
    if source:
        meta["resource"] = source
    if mtags:
        meta["tags"] = mtags
    meta["timestamp"] = (git_last_commit_date(base_dir, timestamp_path)
                         or _dt.date.today().isoformat())
    return fm.render(meta), warning


def latest_source(base_dir, domain, page_file):
    """Return the most recent ingest-log ``source`` recorded for ``page_file``,
    or None if there is no log, no matching record, or the latest record is a delete."""
    from .base import log_path
    path = log_path(base_dir, domain)
    src = None
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("page") != page_file:
            continue
        if rec.get("op") == "delete":
            src = None
        elif rec.get("source"):
            src = rec["source"]
    return src


def _page_slugs(dom_path: Path) -> list[str]:
    """Domain page slugs, excluding the .iwiki dir and the reserved OKF files."""
    out = []
    for p in sorted(dom_path.rglob("*.md")):
        rel = p.relative_to(dom_path)
        if ".iwiki" in rel.parts or rel.as_posix() in _oa.RESERVED_OKF:
            continue
        out.append(rel.with_suffix("").as_posix())
    return out


def _read_log(dom_path: Path) -> list:
    path = dom_path / ".iwiki" / "log.jsonl"
    recs: list = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except ValueError:
                pass
    return recs


def _looks_authored(text: str) -> bool:
    """A pre-existing reserved file is 'authored' (never clobber) if it carries
    frontmatter or any ## section — the generated nav/log files have neither."""
    # Gap: a prose-only reserved file (no frontmatter, no '## ') reads as generated
    # and is overwritten. Accepted — the write guard blocks tool-creation of such
    # files, and broadening this check would misclassify the generated index.md/
    # log.md themselves as authored, breaking idempotent refresh.
    meta, _ = fm.split(text)
    if meta:
        return True
    return any(ln.startswith("## ") for ln in text.splitlines())


def batch_sweep(cfg, base_dir, domain) -> dict:
    """Deterministic whole-domain in-place OKF conformance sweep (no chat model).
    Converts residual [[...]] links and guarantees frontmatter on every page,
    preserving existing type/tags. Writes back only changed files (idempotent)."""
    from .engine.links import to_markdown_links
    dom = Path(base_dir) / domain
    fixed_links, added_frontmatter = [], []
    for slug in _page_slugs(dom):
        page_file = f"{slug}.md"
        p = dom / page_file
        original = p.read_text(encoding="utf-8")
        meta, body = fm.split(original)
        new_body = to_markdown_links(body)
        if meta:
            if meta.get("tags"):
                meta["tags"] = fm.normalize_tags(meta["tags"])
            new_full = fm.render(meta) + new_body
        else:
            src = latest_source(base_dir, domain, page_file)
            block, _ = build_frontmatter(
                cfg, base_dir, domain, slug, new_body,
                source=src, explicit_type=fm.DEFAULT_TYPE, explicit_tags=None,
                timestamp_path=f"{domain}/{page_file}")
            new_full = block + new_body
            added_frontmatter.append(slug)
        if new_full != original:
            p.write_text(new_full, encoding="utf-8")
            if new_body != body:
                fixed_links.append(slug)
    return {"fixed_links": fixed_links, "added_frontmatter": added_frontmatter}


def refresh_artifacts(base_dir, domain) -> str | None:
    """Regenerate index.md + log.md in the domain root from current state.
    Deterministic and best-effort: never raises. Returns a warning or None."""
    try:
        dom = Path(base_dir) / domain
        slugs = _page_slugs(dom)
        records = _read_log(dom)
        warnings: list = []
        for name, content in (("index.md", _oa.render_index(slugs)),
                              ("log.md", _oa.render_log(records))):
            p = dom / name
            if p.is_file() and _looks_authored(p.read_text(encoding="utf-8")):
                warnings.append(
                    f"authored page '{name}' collides with the generated OKF "
                    "file; left untouched")
                continue
            p.write_text(content, encoding="utf-8")
        return "; ".join(warnings) or None
    except Exception:
        return "okf artifact refresh failed"
