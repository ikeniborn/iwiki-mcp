"""Serialize a domain into a fully OKF-conformant bundle: standard markdown
links plus reserved index.md / log.md, guaranteeing frontmatter on each.
Sources are never mutated — only copies."""
from __future__ import annotations
import datetime
import json
import os
import re
import subprocess

from .engine import frontmatter as fm

_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#([^\]|]+))?(?:\|([^\]]+))?\]\]")
_CODE = re.compile(r"```.*?```|~~~.*?~~~|`[^`]*`", re.DOTALL)

_RESERVED = ("index.md", "log.md")


def _convert_one(m):
    target, heading, alias = m.group(1).strip(), m.group(2), m.group(3)
    text = (alias or heading or target).strip()
    return f"[{text}]({target}.md)"


def convert_wikilinks(body: str) -> str:
    """Rewrite [[t]] / [[t#H]] / [[t|a]] to standard markdown links, leaving
    [[...]] inside fenced or inline code untouched."""
    out, last = [], 0
    for cm in _CODE.finditer(body):
        out.append(_WIKILINK.sub(_convert_one, body[last:cm.start()]))
        out.append(cm.group(0))
        last = cm.end()
    out.append(_WIKILINK.sub(_convert_one, body[last:]))
    return "".join(out)


def _pages(dom_path: str) -> list:
    out = []
    for root, _, files in os.walk(dom_path):
        if ".iwiki" in root.split(os.sep):
            continue
        for f in files:
            if f.endswith(".md"):
                out.append(os.path.relpath(os.path.join(root, f), dom_path))
    return sorted(out)


def _read_log(dom_path: str) -> list:
    path = os.path.join(dom_path, ".iwiki", "log.jsonl")
    recs = []
    if os.path.isfile(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line:
                try:
                    recs.append(json.loads(line))
                except Exception:
                    pass
    return recs


def _git_date(dom_path: str, rel: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", dom_path, "log", "-1", "--format=%cs", "--", rel],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:
        return None


def _derive_meta(dom_path: str, rel: str, body: str) -> dict:
    """Deterministic frontmatter for a page that was never migrated to OKF,
    so every exported page carries a frontmatter block (spec requirement)."""
    slug = rel[:-3] if rel.endswith(".md") else rel
    meta: dict = {"type": fm.DEFAULT_TYPE, "title": fm.derive_title(body, slug)}
    description = fm.derive_description(body)
    if description:
        meta["description"] = description
    meta["timestamp"] = _git_date(dom_path, rel) or datetime.date.today().isoformat()
    return meta


def export_domain(dom_path: str, dest: str) -> dict:
    rels = _pages(dom_path)
    warnings: list = []
    for rel in rels:
        meta, body = fm.split(open(os.path.join(dom_path, rel), encoding="utf-8").read())
        if meta:
            if meta.get("tags"):
                meta["tags"] = fm.normalize_tags(meta["tags"])
            block = fm.render(meta)
        else:
            block = fm.render(_derive_meta(dom_path, rel, body))
        if rel in _RESERVED:
            warnings.append(
                f"source page '{rel}' collides with the reserved OKF '{rel}' "
                "and is overwritten by the generated nav/log file")
        out_path = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(block + convert_wikilinks(body))
    os.makedirs(dest, exist_ok=True)
    index = "# Index\n\n" + "".join(
        f"- [{rel[:-3]}]({rel})\n" for rel in rels if rel not in _RESERVED)
    with open(os.path.join(dest, "index.md"), "w", encoding="utf-8") as fh:
        fh.write(index)
    log_md = "# Log\n\n" + "".join(
        f"- {r.get('date','')} {r.get('op','')} {r.get('page','')}\n" for r in _read_log(dom_path))
    with open(os.path.join(dest, "log.md"), "w", encoding="utf-8") as fh:
        fh.write(log_md)
    return {"pages": len(rels), "dest": dest, "warnings": warnings}
