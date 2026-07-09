"""Top-layer OKF frontmatter assembly: deterministic field derivation plus the
type/tags precedence (explicit params -> optional server-side classify ->
default). Kept out of the engine because it reaches git and the index."""
from __future__ import annotations
import datetime as _dt
import subprocess

from .engine import classify, frontmatter as fm
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
                      explicit_type, explicit_tags, timestamp_path):
    """Return (frontmatter_block, warning). Precedence: explicit -> classify -> default."""
    warning = None
    if explicit_type is not None:
        mtype = fm.coerce_type(explicit_type)
        mtags = fm.normalize_tags(explicit_tags or [])
    elif cfg.chat_model:
        r = classify.classify_page(cfg, body, domain_tag_vocab(base_dir, domain))
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
