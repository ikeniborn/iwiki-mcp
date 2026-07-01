---
review:
  plan_hash: c8b83d11e7baaf79
  spec_hash: bc5b2b4fb600122d
  last_run: 2026-07-01
  phases:
    structure:     { status: passed }
    coverage:      { status: passed }
    dependencies:  { status: passed }
    verifiability: { status: passed }
    consistency:   { status: passed }
  findings:
    - id: F-001
      phase: consistency
      severity: WARNING
      section: "Task 4 Step 3c"
      section_hash: null
      fragment: "ignore.load_project_ignore(bind.project_dir)"
      text: "plan does not restate that `ignore` is already imported in server.py"
      fix: "none required — server.py:15 already imports ignore; plan works as written"
      verdict: accepted
      verdict_at: 2026-07-01
    - id: F-002
      phase: verifiability
      severity: INFO
      section: "Task 6 Step 1/2"
      section_hash: null
      fragment: "Add the tool to README.md / Document the transaction"
      text: "docs steps had no verification command (descriptive DoD only)"
      fix: "added grep verify + expected OK to both docs steps"
      verdict: fixed
      verdict_at: 2026-07-01
chain:
  intent: null
  spec: docs/superpowers/specs/2026-07-01-wiki-update-page-design.md
result_check:
  verdict: OK
  plan_hash: c8b83d11e7baaf79
  last_run: 2026-07-01
---

# wiki_update_page + commit-and-push Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `wiki_update_page` — a tool that edits one `##` section of an existing wiki page in place (reindexing only the changed section), and make every wiki mutation commit **and** push to git.

**Architecture:** A new stdlib-only `engine/section.py` does the section splice. `server.wiki_update_page` runs a transactional edit (read original → splice → validate → write → upsert ingest log → reindex → commit+push) with full rollback of the file and the log bytes. A new `sync.commit_and_push` (auto_commit → sync) replaces the `auto_commit`-only calls in `write_page`, `create_domain`, and `index`. Reindex-of-only-the-changed-section is already the behavior of `indexer.index_domain` (it re-embeds only chunks whose hash changed).

**Tech Stack:** Python 3.10+, `uv`, `pytest` (`pythonpath=["src"]`, `asyncio_mode=auto`), FastMCP, git via `subprocess`.

**Spec:** `docs/superpowers/specs/2026-07-01-wiki-update-page-design.md`

---

## File Structure

- **Create** `src/iwiki_mcp/engine/section.py` — `replace_section` + `SectionError`. Pure, stdlib-only (no `httpx`/config), mirrors `chunk._sections`.
- **Create** `tests/test_section.py` — unit tests for the splice (no env needed).
- **Modify** `src/iwiki_mcp/sync.py` — add `commit_and_push`.
- **Create** `tests/test_commit_and_push.py` — commit_and_push against a real git repo + fail-soft.
- **Modify** `src/iwiki_mcp/indexer.py` — add `upsert_ingest_log`.
- **Modify** `src/iwiki_mcp/server.py` — add `wiki_update_page` + `_restore_log`; retrofit `write_page`/`create_domain`/`index` to `commit_and_push`; register the tool.
- **Create** `tests/test_server_update.py` — update-page behavior, rollback, log upsert.
- **Modify** `README.md`, `docs/wiki/architecture.md`, `pyproject.toml` (version bump).

---

## Task 1: `engine/section.py` — section splice

**Files:**
- Create: `src/iwiki_mcp/engine/section.py`
- Test: `tests/test_section.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_section.py
import pytest

from iwiki_mcp.engine.section import SectionError, replace_section

PAGE = "# Auth\n## Overview\nsummary\n## Flow\nold body here\n## Notes\nkeep\n"


def test_replace_section_swaps_only_target_body():
    out = replace_section(PAGE, "Flow", "new body")
    assert "## Flow\nnew body" in out
    assert "old body here" not in out
    assert "## Overview\nsummary" in out   # sibling untouched
    assert "## Notes\nkeep" in out         # sibling untouched


def test_replace_section_last_section():
    out = replace_section(PAGE, "Notes", "fresh notes")
    assert "## Notes\nfresh notes" in out
    assert "keep" not in out


def test_replace_section_strips_leading_hashes_in_heading():
    out = replace_section(PAGE, "## Flow", "b2")
    assert "## Flow\nb2" in out


def test_replace_section_overview_is_editable():
    out = replace_section(PAGE, "Overview", "new summary")
    assert "## Overview\nnew summary" in out


def test_replace_section_missing_heading_raises():
    with pytest.raises(SectionError):
        replace_section(PAGE, "Nonexistent", "x")


def test_replace_section_duplicate_heading_raises():
    dup = "# T\n## Flow\na\n## Flow\nb\n"
    with pytest.raises(SectionError):
        replace_section(dup, "Flow", "x")


def test_replace_section_empty_heading_raises():
    with pytest.raises(SectionError):
        replace_section(PAGE, "  ", "x")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_section.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'iwiki_mcp.engine.section'`

- [ ] **Step 3: Write the implementation**

```python
# src/iwiki_mcp/engine/section.py
"""Replace the body of a single ``##`` section in a markdown page — stdlib only,
no config/embedding call. Used by ``wiki_update_page`` to edit one section in place.
"""
from __future__ import annotations

import re

# Keep in sync with chunk._H2 / validate._H2 / lint._H2.
_H2 = re.compile(r"^##\s+(.*?)\s*$", re.MULTILINE)


class SectionError(ValueError):
    """Raised when the target ``##`` section cannot be uniquely located."""


def replace_section(content: str, heading: str, new_body: str) -> str:
    """Return ``content`` with the body of the ``## <heading>`` section replaced.

    ``heading`` is matched by its text (leading ``#``/whitespace stripped). The
    replaced span runs from the end of the heading line to the next ``##`` (or EOF);
    the heading line itself is preserved. Raises ``SectionError`` if the heading is
    missing or appears more than once.
    """
    target = heading.lstrip("#").strip()
    if not target:
        raise SectionError("empty heading")
    heads = list(_H2.finditer(content))
    matches = [i for i, m in enumerate(heads) if m.group(1).strip() == target]
    if not matches:
        raise SectionError(f"section '## {target}' not found")
    if len(matches) > 1:
        raise SectionError(
            f"section '## {target}' is ambiguous ({len(matches)} matches)"
        )
    idx = matches[0]
    body_start = heads[idx].end()
    body_end = heads[idx + 1].start() if idx + 1 < len(heads) else len(content)
    return content[:body_start] + "\n" + new_body.strip("\n") + "\n\n" + content[body_end:]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_section.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/engine/section.py tests/test_section.py
git commit -m "feat(section): add replace_section splice for single-section edits"
```

---

## Task 2: `sync.commit_and_push`

**Files:**
- Modify: `src/iwiki_mcp/sync.py` (append a new function after `sync`)
- Test: `tests/test_commit_and_push.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_commit_and_push.py
import subprocess

from iwiki_mcp import sync


def _git(base, *args):
    subprocess.run(["git", "-C", str(base), *args], check=True, capture_output=True)


def _init_repo(base):
    base.mkdir(parents=True, exist_ok=True)
    _git(base, "init", "-q")
    _git(base, "config", "user.email", "t@example.com")
    _git(base, "config", "user.name", "t")


def test_commit_and_push_commits_then_calls_sync(tmp_path, monkeypatch):
    base = tmp_path / "wiki"
    _init_repo(base)
    (base / "backend").mkdir()
    (base / "backend" / "page.md").write_text("# P\n## Overview\nx\n")

    called = {}

    def fake_sync(b, **k):
        called["base"] = b
        return {"pulled": True, "pushed": True}

    monkeypatch.setattr(sync, "sync", fake_sync)
    out = sync.commit_and_push(str(base), "msg", pathspec="backend")

    assert out["committed"] is True
    assert out["pushed"] is True
    assert called["base"] == str(base)


def test_commit_and_push_surfaces_sync_warning(tmp_path, monkeypatch):
    base = tmp_path / "wiki"
    _init_repo(base)
    (base / "backend").mkdir()
    (base / "backend" / "page.md").write_text("# P\n## Overview\nx\n")

    monkeypatch.setattr(
        sync, "sync",
        lambda b, **k: {"pulled": True, "pushed": False, "warning": "no remote"},
    )
    out = sync.commit_and_push(str(base), "msg", pathspec="backend")

    assert out["committed"] is True
    assert out["pushed"] is False
    assert out["warning"] == "no remote"


def test_commit_and_push_non_repo_is_fail_soft_and_skips_sync(tmp_path, monkeypatch):
    base = tmp_path / "plain"
    base.mkdir()
    calls = {"n": 0}

    def fake_sync(b, **k):
        calls["n"] += 1
        return {}

    monkeypatch.setattr(sync, "sync", fake_sync)
    out = sync.commit_and_push(str(base), "msg")

    assert out["committed"] is False
    assert out["pushed"] is False
    assert calls["n"] == 0   # sync not attempted when nothing committed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_commit_and_push.py -q`
Expected: FAIL — `AttributeError: module 'iwiki_mcp.sync' has no attribute 'commit_and_push'`

- [ ] **Step 3: Write the implementation**

Append to `src/iwiki_mcp/sync.py` (after the `sync` function):

```python
def commit_and_push(base: str, message: str, pathspec: str | None = None) -> dict:
    """Auto-commit, then push via ``sync`` when the commit landed.

    Fail-soft: when nothing is committed, ``sync`` is not attempted; a push failure
    is surfaced as a warning and the local commit stands.
    """
    commit = auto_commit(base, message, pathspec)
    if not commit.get("committed"):
        out = {"committed": False, "pushed": False}
        if commit.get("warning"):
            out["warning"] = commit["warning"]
        return out
    result = sync(base)
    out = {"committed": True, "pushed": bool(result.get("pushed"))}
    warn = result.get("warning") or result.get("error")
    if warn:
        out["warning"] = warn
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_commit_and_push.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/sync.py tests/test_commit_and_push.py
git commit -m "feat(sync): add commit_and_push (auto_commit then push, fail-soft)"
```

---

## Task 3: `indexer.upsert_ingest_log`

**Files:**
- Modify: `src/iwiki_mcp/indexer.py` (add function after `append_log`)
- Test: `tests/test_indexer_log.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_indexer_log.py
import json

from iwiki_mcp import base, indexer


def _recs(b, domain):
    text = open(base.log_path(str(b), domain), encoding="utf-8").read()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_upsert_replaces_prior_ingest_for_page(tmp_path):
    b = tmp_path / "wiki"
    (b / "backend" / ".iwiki").mkdir(parents=True)
    indexer.append_log(str(b), "backend", "ingest", "s1", "p.md", "h1")
    indexer.append_log(str(b), "backend", "ingest", "s2", "other.md", "h2")

    indexer.upsert_ingest_log(str(b), "backend", "s1b", "p.md", "h3")

    recs = _recs(b, "backend")
    p = [r for r in recs if r["page"] == "p.md"]
    assert len(p) == 1                      # single record for the page
    assert p[0]["src_hash"] == "h3"
    assert p[0]["source"] == "s1b"
    assert any(r["page"] == "other.md" for r in recs)   # sibling preserved


def test_upsert_creates_log_when_absent(tmp_path):
    b = tmp_path / "wiki"
    (b / "backend" / ".iwiki").mkdir(parents=True)

    indexer.upsert_ingest_log(str(b), "backend", "s", "p.md", "h")

    recs = _recs(b, "backend")
    assert len(recs) == 1 and recs[0]["page"] == "p.md"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_indexer_log.py -q`
Expected: FAIL — `AttributeError: module 'iwiki_mcp.indexer' has no attribute 'upsert_ingest_log'`

- [ ] **Step 3: Write the implementation**

Append to `src/iwiki_mcp/indexer.py` (after `append_log`):

```python
def upsert_ingest_log(base: str, domain: str, source: str, page: str,
                      src_hash: str | None) -> None:
    """Replace any prior ``ingest`` records for ``page`` with a single fresh one.

    Unlike ``append_log`` this keeps one ingest record per page, so ``lint``'s
    first-hit-wins stale detection reads the current ``src_hash`` after an edit.
    """
    path = log_path(base, domain)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    kept: list[str] = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                s = line.strip()
                if not s:
                    continue
                try:
                    rec = json.loads(s)
                except Exception:
                    kept.append(s)
                    continue
                if rec.get("op") == "ingest" and rec.get("page") == page:
                    continue   # drop the stale record for this page
                kept.append(s)
    rec = {"op": "ingest", "source": source, "page": page,
           "date": _dt.date.today().isoformat(), "src_hash": src_hash}
    kept.append(json.dumps(rec, ensure_ascii=False))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(kept) + "\n")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_indexer_log.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/indexer.py tests/test_indexer_log.py
git commit -m "feat(indexer): add upsert_ingest_log for single-record-per-page"
```

---

## Task 4: `server.wiki_update_page` + rollback helper + registration

**Files:**
- Modify: `src/iwiki_mcp/server.py` (import; add `_restore_log`; add `wiki_update_page`; register)
- Test: `tests/test_server_update.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_server_update.py
import json
import os

from iwiki_mcp import base, indexer, server


def _seed(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
    b.mkdir()
    (b / "backend" / ".iwiki").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".iwiki.toml").write_text('read = ["backend"]\nwrite = "backend"\n')
    monkeypatch.setenv("IWIKI_BASE_DIR", str(b))
    monkeypatch.setenv("IWIKI_PROJECT_DIR", str(proj))
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setenv("IWIKI_EMBED_DIMENSIONS", "2")
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, t: [[1.0, 0.0] for _ in t])
    return str(b), str(proj)


def _write(md, source=None):
    return server.wiki_write_page("backend", "auth", md, source=source)


BASE_MD = "# Auth\n## Overview\nsummary\n## Flow\nlogin then token\n"


def test_update_edits_section_and_returns_pushed_key(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch)
    _write(BASE_MD)
    out = server.wiki_update_page("backend", "auth", "Flow", "refreshed flow text")
    assert out["page"] == "backend/auth.md"
    assert out["heading"] == "Flow"
    assert "pushed" in out and "committed" in out
    content = open(os.path.join(b, "backend", "auth.md"), encoding="utf-8").read()
    assert "refreshed flow text" in content
    assert "login then token" not in content


def test_update_page_not_found(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_update_page("backend", "nope", "Flow", "x")
    assert "error" in out and "not found" in out["error"]


def test_update_missing_heading(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    _write(BASE_MD)
    out = server.wiki_update_page("backend", "auth", "Nonexistent", "y")
    assert "error" in out and "not found" in out["error"]


def test_update_rejects_deep_heading_in_body(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    _write(BASE_MD)
    out = server.wiki_update_page("backend", "auth", "Flow", "### too deep\ny")
    assert "error" in out


def test_update_upserts_log_when_source_given(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch)
    src = tmp_path / "src.txt"
    src.write_text("v1")
    _write(BASE_MD, source=str(src))
    src.write_text("v2")

    out = server.wiki_update_page("backend", "auth", "Flow", "new", source=str(src))
    assert "error" not in out

    text = open(base.log_path(b, "backend"), encoding="utf-8").read()
    recs = [json.loads(line) for line in text.splitlines() if line.strip()]
    ingest = [r for r in recs if r.get("op") == "ingest" and r["page"] == "auth.md"]
    assert len(ingest) == 1
    assert ingest[0]["source"] == str(src)


def test_update_rolls_back_file_and_log_on_index_failure(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch)
    src = tmp_path / "src.txt"
    src.write_text("v1")
    _write(BASE_MD, source=str(src))
    log_before = open(base.log_path(b, "backend"), encoding="utf-8").read()

    monkeypatch.setattr(
        indexer, "index_domain",
        lambda cfg, base, domain: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    src.write_text("v2")
    out = server.wiki_update_page("backend", "auth", "Flow", "newbody", source=str(src))

    assert "error" in out
    content = open(os.path.join(b, "backend", "auth.md"), encoding="utf-8").read()
    assert "login then token" in content and "newbody" not in content
    assert open(base.log_path(b, "backend"), encoding="utf-8").read() == log_before
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server_update.py -q`
Expected: FAIL — `AttributeError: module 'iwiki_mcp.server' has no attribute 'wiki_update_page'`

- [ ] **Step 3a: Add the import**

In `src/iwiki_mcp/server.py`, change the validate import line:

```python
from .engine.validate import validate_page
```

to:

```python
from .engine.section import SectionError, replace_section
from .engine.validate import validate_page
```

- [ ] **Step 3b: Add the `_restore_log` helper**

In `src/iwiki_mcp/server.py`, immediately after `_rollback_last_ingest_log` (ends near line 231), add:

```python
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
```

- [ ] **Step 3c: Add `wiki_update_page`**

In `src/iwiki_mcp/server.py`, add this function after `wiki_write_page` (before `wiki_index`):

```python
@_safe
def wiki_update_page(
    domain: str, slug: str, heading: str, new_body: str, source: str | None = None
) -> dict:
    bind = base.resolve_binding()
    valid_domain = _validate_domain(domain)
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
        if source:
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
    }
```

- [ ] **Step 3d: Register the tool**

In `src/iwiki_mcp/server.py`, after `mcp.tool()(wiki_write_page)` add:

```python
mcp.tool()(wiki_update_page)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_server_update.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/server.py tests/test_server_update.py
git commit -m "feat(server): add wiki_update_page (in-place section edit + commit/push)"
```

---

## Task 5: Retrofit existing mutations to commit_and_push

**Files:**
- Modify: `src/iwiki_mcp/server.py` (`wiki_write_page`, `wiki_create_domain`, `wiki_index`)
- Test: `tests/test_server_write.py` (add one assertion)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_server_write.py`:

```python
def test_index_commits_and_reports_push(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch)
    server.wiki_write_page("backend", "auth", "# A\n## Overview\no\n## Flow\nx\n")
    out = server.wiki_index("backend")
    assert out["domain"] == "backend"
    assert "committed" in out and "pushed" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_server_write.py::test_index_commits_and_reports_push -q`
Expected: FAIL — `KeyError: 'committed'` (wiki_index does not yet commit)

- [ ] **Step 3a: Retrofit `wiki_write_page`**

In `src/iwiki_mcp/server.py`, in `wiki_write_page`, replace:

```python
    commit = sync.auto_commit(bind.base, f"iwiki: ingest {page_rel}",
                              pathspec=valid_domain)
    return {
        "page": page_rel,
        "indexed_chunks": stats["indexed_chunks"],
        "bytes": stats["bytes"],
        "over_cap": stats["over_cap"],
        "committed": commit.get("committed", False),
    }
```

with:

```python
    commit = sync.commit_and_push(bind.base, f"iwiki: ingest {page_rel}",
                                  pathspec=valid_domain)
    return {
        "page": page_rel,
        "indexed_chunks": stats["indexed_chunks"],
        "bytes": stats["bytes"],
        "over_cap": stats["over_cap"],
        "committed": commit.get("committed", False),
        "pushed": commit.get("pushed", False),
    }
```

- [ ] **Step 3b: Retrofit `wiki_create_domain`**

In `wiki_create_domain`, replace:

```python
    commit = sync.auto_commit(bind.base, f"iwiki: create domain {valid_domain}",
                              pathspec=valid_domain)
    return {"created": valid_domain, "committed": commit.get("committed", False)}
```

with:

```python
    commit = sync.commit_and_push(bind.base, f"iwiki: create domain {valid_domain}",
                                  pathspec=valid_domain)
    return {"created": valid_domain, "committed": commit.get("committed", False),
            "pushed": commit.get("pushed", False)}
```

- [ ] **Step 3c: Retrofit `wiki_index`**

In `wiki_index`, replace:

```python
    cfg = Config.load()
    stats = indexer.index_domain(cfg, bind.base, valid_domain)
    return {"domain": valid_domain, **stats}
```

with:

```python
    cfg = Config.load()
    stats = indexer.index_domain(cfg, bind.base, valid_domain)
    commit = sync.commit_and_push(bind.base, f"iwiki: reindex {valid_domain}",
                                  pathspec=valid_domain)
    return {"domain": valid_domain, **stats,
            "committed": commit.get("committed", False),
            "pushed": commit.get("pushed", False)}
```

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS (all tests green, including the new `test_index_commits_and_reports_push`). Base is a plain dir in tests, so git is a no-op and `committed`/`pushed` are `False` — the assertions only check the keys exist.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/server.py tests/test_server_write.py
git commit -m "feat(server): commit+push on write_page/create_domain/index"
```

---

## Task 6: Docs, version bump, wiki upkeep

**Files:**
- Modify: `README.md`
- Modify: `docs/wiki/architecture.md`
- Modify: `pyproject.toml`

- [ ] **Step 1: Add the tool to `README.md`**

Find the tool list in `README.md` (search for `wiki_write_page`) and add a sibling line describing the new tool, matching the surrounding format, e.g.:

```markdown
- `wiki_update_page(domain, slug, heading, new_body, source=None)` — replace the body of one `##` section of an existing page; reindexes only the changed section, then commits and pushes.
```

Also, if the README documents that mutations commit but do not push, update that wording: every mutating tool (`wiki_write_page`, `wiki_update_page`, `wiki_create_domain`, `wiki_index`) now commits **and** pushes (fail-soft).

Verify: `grep -q wiki_update_page README.md && echo OK`
Expected: `OK`

- [ ] **Step 2: Document the transaction in `docs/wiki/architecture.md`**

Add a short subsection near the existing write-transaction description covering:
- `wiki_update_page` flow: read original → `replace_section` → `validate_page` → write → `upsert_ingest_log` (when `source`) → `index_domain` → `commit_and_push`, with rollback of the file and the log bytes.
- `commit_and_push` is now used by all four mutating tools; `wiki_index` commits its `.iwiki/index.jsonl` change too.
- Reindex is section-scoped for free: `index_domain` re-embeds only chunks whose hash changed.

Verify: `grep -q wiki_update_page docs/wiki/architecture.md && echo OK`
Expected: `OK`

- [ ] **Step 3: Bump the version**

In `pyproject.toml`, bump the patch version (e.g. `0.1.x` → `0.1.(x+1)`).

- [ ] **Step 4: Run the full suite once more**

Run: `uv run pytest -q`
Expected: PASS (all green)

- [ ] **Step 5: Commit**

```bash
git add README.md docs/wiki/architecture.md pyproject.toml
git commit -m "docs: document wiki_update_page and commit-and-push; bump version"
```

- [ ] **Step 6: Update the iwiki domain (runtime, after implementation)**

Regenerate the affected wiki page in the `iwiki-mcp` domain and lint:
- `wiki_write_page("iwiki-mcp", <slug>, <updated markdown>, source="src/iwiki_mcp/server.py")` for the architecture/tool-surface page (delete-then-write if the page already exists, since writes are create-only).
- `wiki_index("iwiki-mcp")`
- `wiki_lint("iwiki-mcp")` — expect no broken refs / orphans / stale.

---

## Verification (Definition of Done)

- [ ] `uv run pytest -q` fully green.
- [ ] `wiki_update_page` edits a single section, leaves siblings byte-identical, and reindexes (embedding only the changed section's chunks).
- [ ] A failed reindex leaves the page file and the ingest log exactly as before the call.
- [ ] `wiki_write_page`, `wiki_update_page`, `wiki_create_domain`, `wiki_index` each return `committed` and `pushed`, and call `commit_and_push`.
- [ ] README + `docs/wiki/architecture.md` updated; version bumped.
