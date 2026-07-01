---
review:
  plan_hash: fdd1e7713e3fedbc
  spec_hash: b404753f7cccc92e
  last_run: 2026-07-01
  phases:
    structure:     { status: passed }
    coverage:      { status: passed }
    dependencies:  { status: passed }
    verifiability: { status: passed }
    consistency:   { status: passed }
  findings: []
chain:
  intent: null
  spec: docs/superpowers/specs/2026-07-01-delete-stale-wiki-page-design.md
result_check:
  verdict: OK
  plan_hash: fdd1e7713e3fedbc
  last_run: 2026-07-01
---
# Delete Stale Wiki Page — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit `wiki_delete_page(domain, slug)` tool and a `missing_source` lint detector that flags pages whose recorded ingest source has disappeared from disk.

**Architecture:** `engine/lint.py` gains a shared last-wins log reader (`_latest_ingest_by_page`) consumed by both the existing `_stale` detector and the new `_missing_source` detector; the module stays stdlib-only. `server.py` gains a transactional `wiki_delete_page` (remove file → append `delete` log op → reindex → git commit, with rollback) mirroring `wiki_write_page`.

**Tech Stack:** Python 3.10+, `uv`, `pytest`. stdio MCP server (`FastMCP`). JSONL vector store.

## Global Constraints

- **No linter/formatter configured** — match surrounding style by hand.
- **`engine/lint.py` must stay stdlib-only / config-free** — no `httpx`; do NOT import `chunk`/`embed`/`config`. `project_dir` is passed as a plain `str`.
- **Tests never hit the network** — `monkeypatch.setattr(indexer, "embed_texts", ...)` and set dummy `IWIKI_*` env vars (`IWIKI_BASE_DIR`, `IWIKI_PROJECT_DIR`, `IWIKI_LLM_BASE_URL`, `IWIKI_LLM_KEY`, `IWIKI_EMBED_DIMENSIONS`). `pythonpath = ["src"]`, `asyncio_mode = "auto"` — import `iwiki_mcp` directly, no `@pytest.mark.asyncio`.
- **Fail-soft handlers** — `wiki_*` implementation functions are plain (no decorator in the def), wrapped by `@_safe`, and registered at the bottom via `mcp.tool()(wiki_*)`. Tests call the plain functions directly.
- **Path-traversal guards are load-bearing** — reuse `_validate_domain`, `_domain_path`, `_slug_parts`, `_page_path`; never add new path joins that bypass them.
- **Version bump every change** — `pyproject.toml` `version = "0.1.2"` → `"0.1.3"` (patch).
- **Branch:** `dev-delete-stale-page` (already created). Commit per task. Close via PR into `master` — never commit to `master` directly.
- **Commit trailer:** end every commit message with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **Run the suite with:** `uv run pytest -q`.

## File Structure

- `src/iwiki_mcp/engine/lint.py` — **modify**: add `_latest_ingest_by_page`, `_source_exists`, `_missing_source`; refactor `_stale`; add `project_dir` param to `lint`; add `missing_source` to the report dict.
- `src/iwiki_mcp/server.py` — **modify**: generalize `_rollback_last_ingest_log` → `_rollback_last_log`; add `wiki_delete_page`; register it; pass `project_dir` into `lint` from `wiki_lint`.
- `tests/engine/test_lint.py` — **modify**: add `missing_source` + last-wins tests.
- `tests/test_server_delete.py` — **create**: `wiki_delete_page` tests.
- `README.md` — **modify**: tool table row + `wiki_lint` description.
- `pyproject.toml` — **modify**: version bump.
- `docs/wiki/` — **conditional**: update only if `wiki_status` reports a domain bound to this project.

---

### Task 1: Shared last-wins log reader + refactor `_stale`

Introduce `_latest_ingest_by_page` (last-wins: an `ingest` record sets a page's current source, a `delete` record clears it) and rewrite `_stale` on top of it. Existing `_stale` behaviour must be preserved for all current tests; the new behaviour is that a `delete` + re-`ingest` of the same slug is judged by the newest record.

**Files:**
- Modify: `src/iwiki_mcp/engine/lint.py` (`_stale` at lines 80-113)
- Test: `tests/engine/test_lint.py`

**Interfaces:**
- Produces: `_latest_ingest_by_page(wiki_dir: str) -> dict[str, dict]` — maps resolved page path → `{"page": <abs page path>, "source": <str>, "src_hash": <str|None>}`, one entry per page (latest ingest; deleted pages absent; records with empty/missing source skipped).
- Produces: `_stale(wiki_dir: str) -> list[dict]` — unchanged signature and output shape (`[{"page","source"}]`).

- [ ] **Step 1: Write the failing test** (append to `tests/engine/test_lint.py`)

```python
def test_stale_last_wins_after_delete_and_reingest(tmp_path):
    # ingest(old hash) -> delete -> ingest(new hash matching current source):
    # last-wins => judged by the NEWEST record => NOT stale.
    wd = _wiki(tmp_path, {"a.md": "## A\nbody\n"})
    src = tmp_path / "s.py"
    src.write_text("new\n", encoding="utf-8")
    iwiki = os.path.join(wd, ".iwiki")
    os.makedirs(iwiki, exist_ok=True)
    recs = [
        {"op": "ingest", "source": str(src), "page": "a.md", "src_hash": _h("old\n")},
        {"op": "delete", "source": "", "page": "a.md"},
        {"op": "ingest", "source": str(src), "page": "a.md", "src_hash": _h("new\n")},
    ]
    with open(os.path.join(iwiki, "log.jsonl"), "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")
    assert lint(wd)["stale"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/engine/test_lint.py::test_stale_last_wins_after_delete_and_reingest -v`
Expected: FAIL — current first-hit `_stale` picks the `old\n` hash record and reports the page as stale (list is non-empty).

- [ ] **Step 3: Add the helper and refactor `_stale`**

In `src/iwiki_mcp/engine/lint.py`, replace the entire `_stale` function (lines 80-113) with the helper plus a rewritten `_stale`:

```python
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
```

- [ ] **Step 4: Run the lint suite to verify all pass**

Run: `uv run pytest tests/engine/test_lint.py -v`
Expected: PASS — the new test plus all pre-existing `test_stale_*` tests (hash match/mismatch, mtime fallback, relative page resolution, legacy/malformed tolerance) are green.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/engine/lint.py tests/engine/test_lint.py
git commit -m "refactor(lint): last-wins _latest_ingest_by_page shared reader

Rewrite _stale on a last-wins per-page log reader so a delete+re-ingest
of the same slug is judged by the newest record.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `missing_source` detector + `lint(project_dir=...)`

Add the source-gone detector and thread `project_dir` from `wiki_lint` so relative sources resolve correctly (hybrid A1+A2: absolute checked as-is; relative resolved against `project_dir` and cwd).

**Files:**
- Modify: `src/iwiki_mcp/engine/lint.py` (`lint` at lines 116-152)
- Modify: `src/iwiki_mcp/server.py` (`wiki_lint` at lines 364-374)
- Test: `tests/engine/test_lint.py`, `tests/test_server_lint_sync.py`

**Interfaces:**
- Consumes: `_latest_ingest_by_page` (Task 1).
- Produces: `_source_exists(src: str, project_dir: str | None) -> bool`.
- Produces: `_missing_source(wiki_dir: str, project_dir: str | None) -> list[dict]` — `[{"page","source"}]`.
- Produces: `lint(wiki_dir: str, project_dir: str | None = None) -> dict` — report now includes `"missing_source"`.

- [ ] **Step 1: Write the failing tests** (append to `tests/engine/test_lint.py`)

```python
def test_missing_source_flags_absolute_gone(tmp_path):
    wd = _wiki(tmp_path, {"a.md": "## A\nbody\n"})
    gone = tmp_path / "gone.py"  # never created
    iwiki = os.path.join(wd, ".iwiki")
    os.makedirs(iwiki, exist_ok=True)
    rec = {"op": "ingest", "source": str(gone), "page": "a.md"}
    open(os.path.join(iwiki, "log.jsonl"), "w", encoding="utf-8").write(
        json.dumps(rec) + "\n")
    assert lint(wd)["missing_source"] == [
        {"page": os.path.normpath(os.path.join(wd, "a.md")), "source": str(gone)}
    ]


def test_missing_source_present_not_flagged(tmp_path):
    wd, src, page = _wiki_with_log(tmp_path, "## A\nb\n", "x\n")
    assert lint(wd)["missing_source"] == []


def test_missing_source_empty_source_skipped(tmp_path):
    wd = _wiki(tmp_path, {"a.md": "## A\nb\n"})
    iwiki = os.path.join(wd, ".iwiki")
    os.makedirs(iwiki, exist_ok=True)
    rec = {"op": "ingest", "source": "", "page": "a.md"}
    open(os.path.join(iwiki, "log.jsonl"), "w", encoding="utf-8").write(
        json.dumps(rec) + "\n")
    assert lint(wd)["missing_source"] == []


def test_missing_source_page_absent_skipped(tmp_path):
    wd = _wiki(tmp_path, {"keep.md": "## K\nx\n"})  # a.md never created
    gone = tmp_path / "gone.py"
    iwiki = os.path.join(wd, ".iwiki")
    os.makedirs(iwiki, exist_ok=True)
    rec = {"op": "ingest", "source": str(gone), "page": "a.md"}
    open(os.path.join(iwiki, "log.jsonl"), "w", encoding="utf-8").write(
        json.dumps(rec) + "\n")
    assert lint(wd)["missing_source"] == []


def test_missing_source_relative_found_under_project_dir(tmp_path):
    wd = _wiki(tmp_path, {"a.md": "## A\nb\n"})
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "src.py").write_text("x\n", encoding="utf-8")
    iwiki = os.path.join(wd, ".iwiki")
    os.makedirs(iwiki, exist_ok=True)
    rec = {"op": "ingest", "source": "src.py", "page": "a.md"}
    open(os.path.join(iwiki, "log.jsonl"), "w", encoding="utf-8").write(
        json.dumps(rec) + "\n")
    assert lint(wd, project_dir=str(proj))["missing_source"] == []


def test_missing_source_relative_absent_is_flagged(tmp_path, monkeypatch):
    wd = _wiki(tmp_path, {"a.md": "## A\nb\n"})
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)  # cwd fallback also lacks src.py
    iwiki = os.path.join(wd, ".iwiki")
    os.makedirs(iwiki, exist_ok=True)
    rec = {"op": "ingest", "source": "src.py", "page": "a.md"}
    open(os.path.join(iwiki, "log.jsonl"), "w", encoding="utf-8").write(
        json.dumps(rec) + "\n")
    assert lint(wd, project_dir=str(empty))["missing_source"] == [
        {"page": os.path.normpath(os.path.join(wd, "a.md")), "source": "src.py"}
    ]


def test_missing_source_last_wins_after_delete_and_reingest(tmp_path):
    wd = _wiki(tmp_path, {"a.md": "## A\nb\n"})
    newsrc = tmp_path / "new.py"
    newsrc.write_text("x\n", encoding="utf-8")
    oldsrc = tmp_path / "old.py"  # never created
    iwiki = os.path.join(wd, ".iwiki")
    os.makedirs(iwiki, exist_ok=True)
    recs = [
        {"op": "ingest", "source": str(oldsrc), "page": "a.md"},
        {"op": "delete", "source": "", "page": "a.md"},
        {"op": "ingest", "source": str(newsrc), "page": "a.md"},
    ]
    with open(os.path.join(iwiki, "log.jsonl"), "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")
    assert lint(wd)["missing_source"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/engine/test_lint.py -k missing_source -v`
Expected: FAIL — `KeyError: 'missing_source'` (the key does not exist in the report yet), or `TypeError` for the `project_dir` keyword.

- [ ] **Step 3: Implement the detector and extend `lint`**

In `src/iwiki_mcp/engine/lint.py`, add these two functions immediately after `_stale`:

```python
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
```

Change the `lint` signature (line 116) from:

```python
def lint(wiki_dir: str) -> dict:
```

to:

```python
def lint(wiki_dir: str, project_dir: str | None = None) -> dict:
```

and change the final `return` (lines 150-152) from:

```python
    return {"wiki_present": True, "pages": len(pages),
            "broken": broken, "orphans": orphans, "stale": _stale(wiki_dir),
            "sections": sections}
```

to:

```python
    return {"wiki_present": True, "pages": len(pages),
            "broken": broken, "orphans": orphans, "stale": _stale(wiki_dir),
            "missing_source": _missing_source(wiki_dir, project_dir),
            "sections": sections}
```

- [ ] **Step 4: Wire `project_dir` through `wiki_lint`**

In `src/iwiki_mcp/server.py`, in `wiki_lint` (lines 364-374), change:

```python
        reports[valid_domain] = lint(str(_domain_path(bind.base, valid_domain)))
```

to:

```python
        reports[valid_domain] = lint(
            str(_domain_path(bind.base, valid_domain)), project_dir=bind.project_dir
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/engine/test_lint.py tests/test_server_lint_sync.py -v`
Expected: PASS — all `missing_source` tests, the last-wins test, and the existing `test_lint_one_domain` server test are green.

- [ ] **Step 6: Commit**

```bash
git add src/iwiki_mcp/engine/lint.py src/iwiki_mcp/server.py tests/engine/test_lint.py
git commit -m "feat(lint): missing_source detector for source-gone pages

Add _source_exists + _missing_source (hybrid absolute/relative resolution)
and a project_dir param to lint(); wiki_lint passes bind.project_dir.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `wiki_delete_page` tool

Add the transactional delete tool and generalize the write-path rollback helper to match any op.

**Files:**
- Modify: `src/iwiki_mcp/server.py` (`_rollback_last_ingest_log` at lines 210-230; `wiki_write_page` call site at lines 290-293; tool registration at lines 384-395)
- Create: `tests/test_server_delete.py`

**Interfaces:**
- Consumes: `_domain_path`, `_page_path`, `_slug_parts`, `Config.load`, `indexer.append_log`, `indexer.index_domain`, `sync.auto_commit`.
- Produces: `wiki_delete_page(domain: str, slug: str) -> dict` → `{"deleted","indexed_chunks","bytes","committed"}` on success; `{"error","hint"}` otherwise.
- Produces: `_rollback_last_log(b, domain, op, page, source, src_hash) -> None`.

- [ ] **Step 1: Write the failing tests** (create `tests/test_server_delete.py`)

```python
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
    return str(b)


def _write():
    return server.wiki_write_page(
        "backend", "auth", "# Auth\n## Overview\nsummary\n## Flow\nlogin then token\n"
    )


def test_delete_removes_file_log_and_index_records(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    _write()
    out = server.wiki_delete_page("backend", "auth")
    assert out["deleted"] == "backend/auth.md"
    assert not os.path.exists(os.path.join(b, "backend", "auth.md"))
    log_text = open(base.log_path(b, "backend"), encoding="utf-8").read()
    assert '"op": "delete"' in log_text
    ip = base.index_path(b, "backend")
    index_text = open(ip, encoding="utf-8").read() if os.path.exists(ip) else ""
    assert "auth.md" not in index_text


def test_delete_last_page_leaves_empty_index(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    _write()
    out = server.wiki_delete_page("backend", "auth")
    assert out["indexed_chunks"] == 0


def test_delete_missing_page_errors(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_delete_page("backend", "ghost")
    assert "error" in out and "not found" in out["error"]


def test_delete_unknown_domain_errors(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_delete_page("nope", "auth")
    assert "error" in out


def test_delete_rolls_back_on_index_failure(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    _write()
    monkeypatch.setattr(
        indexer,
        "index_domain",
        lambda cfg, base, domain: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    out = server.wiki_delete_page("backend", "auth")
    assert "error" in out
    assert os.path.exists(os.path.join(b, "backend", "auth.md"))
    log_text = open(base.log_path(b, "backend"), encoding="utf-8").read()
    assert '"op": "delete"' not in log_text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server_delete.py -v`
Expected: FAIL — `AttributeError: module 'iwiki_mcp.server' has no attribute 'wiki_delete_page'`.

- [ ] **Step 3: Generalize the rollback helper**

In `src/iwiki_mcp/server.py`, replace `_rollback_last_ingest_log` (lines 210-230) with a general version keyed on op:

```python
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
```

Then update the `wiki_write_page` rollback call (lines 290-293) from:

```python
        if log_appended:
            _rollback_last_ingest_log(
                bind.base, valid_domain, page_file, log_source, log_src_hash
            )
```

to:

```python
        if log_appended:
            _rollback_last_log(
                bind.base, valid_domain, "ingest", page_file, log_source, log_src_hash
            )
```

- [ ] **Step 4: Add `wiki_delete_page`**

In `src/iwiki_mcp/server.py`, add this function immediately after `wiki_write_page` (after line 304):

```python
@_safe
def wiki_delete_page(domain: str, slug: str) -> dict:
    bind = base.resolve_binding()
    valid_domain = _validate_domain(domain)
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
    commit = sync.auto_commit(bind.base, f"iwiki: delete {page_rel}",
                              pathspec=valid_domain)
    return {
        "deleted": page_rel,
        "indexed_chunks": stats["indexed_chunks"],
        "bytes": stats["bytes"],
        "committed": commit.get("committed", False),
    }
```

- [ ] **Step 5: Register the tool**

In `src/iwiki_mcp/server.py`, in the registration block (lines 384-395), add after `mcp.tool()(wiki_write_page)`:

```python
mcp.tool()(wiki_delete_page)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_server_delete.py tests/test_server_write.py -v`
Expected: PASS — delete tests green AND the existing write/rollback tests still pass (they exercise the renamed `_rollback_last_log` via the write path).

- [ ] **Step 7: Commit**

```bash
git add src/iwiki_mcp/server.py tests/test_server_delete.py
git commit -m "feat(server): wiki_delete_page transactional page deletion

Remove file -> append delete log op -> reindex -> git commit, with rollback
restoring the file and dropping the log line on failure. Generalize the
write-path rollback helper to _rollback_last_log(op=...).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Docs + version bump

Update the user-facing tool reference, bump the version, and refresh `docs/wiki/` only when a domain is bound.

**Files:**
- Modify: `README.md` (tool table lines 206-216; overwrite note at line 218)
- Modify: `pyproject.toml` (line 3)
- Conditional: `docs/wiki/` via iwiki MCP tools

- [ ] **Step 1: Add the tool-table row and update the lint row**

In `README.md`, insert a row after the `wiki_write_page` row:

```markdown
| `wiki_delete_page` | Delete one page by domain and slug: remove the file, append a `delete` log op, reindex the domain, and commit. Rolls back on failure. |
```

Change the `wiki_lint` row from:

```markdown
| `wiki_lint` | Report domain health, including broken links, orphans, stale pages, and section gaps. |
```

to:

```markdown
| `wiki_lint` | Report domain health: broken links, orphans, stale pages, `missing_source` (pages whose ingest source no longer exists on disk — deletion candidates), and section gaps. |
```

- [ ] **Step 2: Add a note pointing stale pages at delete**

In `README.md`, immediately after the paragraph at line 218 (the `wiki_write_page` overwrite note), add:

```markdown
`wiki_lint` reports `missing_source` pages whose ingest source has disappeared. Remove such a stale page explicitly with `wiki_delete_page` after confirming with the user; `wiki_sync` then propagates the deletion to the remote like any other commit.
```

- [ ] **Step 3: Bump the version**

In `pyproject.toml`, change line 3 from:

```toml
version = "0.1.2"
```

to:

```toml
version = "0.1.3"
```

- [ ] **Step 4: Refresh `docs/wiki/` only if bound (iwiki MCP)**

Call `wiki_status`. If it reports a domain bound to this project (domain name == project basename `iwiki-mcp`), author/update the page(s) covering lint and the tool surface, then `wiki_write_page(...)` + `wiki_index(domain)` and `wiki_lint`. If no domain is bound (the current state — `docs/wiki/` is empty), **skip this step** and note it in the commit body. Do NOT fabricate wiki pages.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS — the entire suite is green (existing tests + Tasks 1-3 additions).

- [ ] **Step 6: Commit**

```bash
git add README.md pyproject.toml
git commit -m "docs: document wiki_delete_page + missing_source; bump 0.1.3

README tool table + lint description; docs/wiki skipped (no domain bound).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final: verify + close the branch

- [ ] Run `uv run pytest -q` — full suite green.
- [ ] Run `git log --oneline master..dev-delete-stale-page` — four task commits present.
- [ ] Open a PR from `dev-delete-stale-page` into `master` (use **@skill:git-workflow**). Do NOT merge to `master` directly.
- [ ] After PR review, run `/check-plan` then `/check-result` to close the IDD→SDD chain for topic `delete-stale-wiki-page`.

## Self-Review

**Spec coverage:**
- R1 `missing_source` detection → Task 2. ✓
- R2 `wiki_delete_page` transactional → Task 3. ✓
- R3 explicit-only / lint never deletes → Task 3 (tool) + Task 2 (lint only reports). ✓
- R4 source-gone only signal → Task 2 (`_missing_source`; `_stale`/`orphans` unchanged). ✓
- R5 page granularity → Task 3 (`slug` only; no domain delete). ✓
- R6 sync pure git → no code change; delete commit rides existing `auto_commit`/`wiki_sync`. ✓
- R7 Approach A (reindex + delete log op, no embed call) → Task 3 (`index_domain`, no new chunks). ✓
- R8 hybrid A1+A2 path resolution → Task 2 (`_source_exists`). ✓
- R9 fail-soft tool + guards reuse → Task 3. ✓
- R10 detector + `lint(project_dir)` + `source==""` skip → Task 2. ✓
- R10a last-wins shared helper (both detectors) → Task 1. ✓
- R11 7-step transaction → Task 3. ✓
- R12 rollback restore file + drop log line; commit best-effort → Task 3. ✓
- R13 tests (locations + re-create + empty-index) → Tasks 1-3 tests. ✓
- R14 README + docs/wiki + version bump → Task 4. ✓

**Placeholder scan:** No `TBD`/`TODO`/"handle edge cases" — every code and test step shows complete content. The only conditional is Task 4 Step 4 (docs/wiki gated on `wiki_status`), with explicit skip criteria. ✓

**Type consistency:** `_latest_ingest_by_page` returns `dict[str, dict]` consumed identically by `_stale` and `_missing_source`; `_rollback_last_log(b, domain, op, page, source, src_hash)` matches both call sites (write passes `"ingest"`, delete passes `"delete"`); `wiki_delete_page` return keys (`deleted/indexed_chunks/bytes/committed`) match the spec and tests. ✓
