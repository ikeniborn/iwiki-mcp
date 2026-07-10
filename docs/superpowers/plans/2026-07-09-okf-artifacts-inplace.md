---
review:
  stage: plan
  plan_hash: 31318c28eddbf5b9
  last_run: 2026-07-09
  chain:
    intent: n/a
    spec:
      path: docs/superpowers/specs/2026-07-09-okf-artifacts-inplace-design.md
      spec_hash: 214dc2b159cf4025
  phases:
    structure:
      status: passed
    coverage:
      status: passed
    dependencies:
      status: passed
    verifiability:
      status: passed
    consistency:
      status: passed
  findings:
    - id: F-001
      phase: coverage
      severity: INFO
      section: "### Task 7: Docs, authoring rules, and version bump"
      section_hash: a6fa02b9d9b92e1f
      fragment: "resources.py authoring rules / README EN+RU / version bump (documents Tasks 1-6)"
      text: >-
        Spec R-14 (Transaction and sync) documents a regenerate-wins recovery — "run
        wiki_export_okf after a wiki_sync merge conflict in the derived files". The plan
        implements the enabling deterministic regeneration (Tasks 3/6) and adds no bespoke
        merge logic (correct), but Task 7's user-facing docs do not surface that recovery
        note. Advisory: R-16 did not explicitly require it and the spec already documents it.
      fix: >-
        Optional: add a one-line recovery note to README / authoring-rules ("on a derived-file
        sync conflict, re-run wiki_export_okf to regenerate index.md/log.md"), or accept the
        spec as the documentation locus.
      verdict: open
      verdict_at: null
    - id: F-002
      phase: consistency
      severity: INFO
      section: "### Task 6: Batch sweep + repurpose `wiki_export_okf`; delete `export.py`"
      section_hash: ae5fc22a5f53a611
      fragment: '"still_missing_frontmatter" / "still_legacy_wikilink" keys instead of spec R-08 "warnings"'
      text: >-
        Report-key naming diverges from the spec. Spec R-08 lists the sweep report key
        `warnings`; the plan's wiki_export_okf returns `still_missing_frontmatter` /
        `still_legacy_wikilink` (residue) plus an optional `warning` (collision) instead of a
        `warnings` key. Same intent, different names — a consumer expecting spec's `warnings`
        would not find it.
      fix: >-
        Align the names: either add a `warnings` key aggregating the residue, or update the
        spec's R-08 report list to the plan's `still_*` / `warning` keys.
      verdict: open
      verdict_at: null
result_check:
  verdict: OK
  plan_hash: 31318c28eddbf5b9
  last_run: 2026-07-10
---
# OKF Artifacts In-Place Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an iwiki domain directory always a live, conformant OKF bundle by maintaining the reserved `index.md` / `log.md` in-place on every write, and repurpose `wiki_export_okf` from a copy-to-`dest` tool into a deterministic whole-domain in-place conformance sweep.

**Architecture:** A new stdlib-only engine module generates the two reserved OKF files deterministically. A top-layer `okf.refresh_artifacts` writes them into the domain root at the end of every mutating handler (so one git commit captures pages + `.iwiki/` + reserved files). `wiki_export_okf` becomes a batch sweep that bulk-fixes frontmatter + `[[...]]` links across all pages (reusing existing `links`/`okf`/`frontmatter` helpers) then regenerates the reserved files. Reserved files are excluded from every page-enumeration site so they never enter the index, search, listings, or lint.

**Tech Stack:** Python ≥3.10, stdlib + numpy/httpx (existing), pytest, flake8. No new dependencies.

## Global Constraints

- **Python floor:** `>=3.10` (no 3.11+ only syntax). Copied from `pyproject.toml`.
- **flake8 clean:** `max-line-length = 100` (`.flake8`); no formatter — match surrounding style by hand.
- **Engine stays config-free:** `engine/okf_artifacts.py`, `engine/lint.py`, `engine/grep.py`, `engine/frontmatter.py`, `engine/validate.py` must NOT import `httpx` / `chunk` / `embed` / `config`. The new `engine/okf_artifacts.py` is pure stdlib.
- **Tests never hit the network:** `monkeypatch` `indexer.embed_texts`; set dummy `IWIKI_*` env; follow the `_seed` pattern in `tests/test_server_write.py`.
- **Determinism:** the sweep and `refresh_artifacts` never call the chat model; same domain state → identical bytes.
- **Docs/comments in English.** DRY, YAGNI, TDD, frequent commits.
- **Version:** patch bump `0.2.3` → `0.2.4` in `pyproject.toml` AND `src/iwiki_mcp/__init__.py`.

## File Structure

- **New** `src/iwiki_mcp/engine/okf_artifacts.py` — `RESERVED_OKF` constant + pure `render_index` / `render_log`. Stdlib-only, deterministic.
- **Modified** `src/iwiki_mcp/okf.py` — add `refresh_artifacts` and `batch_sweep` (+ private `_page_slugs`, `_read_log`, `_looks_authored`).
- **Modified** `src/iwiki_mcp/server.py` — call `refresh_artifacts` in mutating handlers; reserved-slug write guard; rewrite `wiki_export_okf`; reserved exclusion in `wiki_list_pages` + `_unmigrated_pages`.
- **Modified** `src/iwiki_mcp/indexer.py`, `src/iwiki_mcp/engine/grep.py`, `src/iwiki_mcp/engine/lint.py` — reserved exclusion at each page-walk.
- **Modified** `src/iwiki_mcp/resources.py`, `README.md`, `docs/README.ru.md`, `pyproject.toml`, `src/iwiki_mcp/__init__.py` — docs + version.
- **Deleted** `src/iwiki_mcp/export.py`.
- **Rewritten** `tests/test_export_okf.py`; **new** `tests/test_okf_artifacts.py`, `tests/test_okf_server.py`.

Reference spec: `docs/superpowers/specs/2026-07-09-okf-artifacts-inplace-design.md`.

---

### Task 1: Reserved-file generators (`engine/okf_artifacts.py`)

**Files:**
- Create: `src/iwiki_mcp/engine/okf_artifacts.py`
- Test: `tests/test_okf_artifacts.py`

**Interfaces:**
- Produces: `RESERVED_OKF: tuple[str, str] = ("index.md", "log.md")`; `render_index(slugs: list[str]) -> str`; `render_log(records: list[dict]) -> str`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_okf_artifacts.py`:

```python
from iwiki_mcp.engine.okf_artifacts import RESERVED_OKF, render_index, render_log


def test_reserved_okf_constant():
    assert RESERVED_OKF == ("index.md", "log.md")


def test_render_index_sorted_links():
    assert render_index(["b", "a"]) == "# Index\n\n- [a](a.md)\n- [b](b.md)\n"


def test_render_index_empty():
    assert render_index([]) == "# Index\n\n"


def test_render_log_lines():
    recs = [{"date": "2026-07-01", "op": "ingest", "page": "a.md"}]
    assert render_log(recs) == "# Log\n\n- 2026-07-01 ingest a.md\n"


def test_render_log_empty():
    assert render_log([]) == "# Log\n\n"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_okf_artifacts.py -q`
Expected: FAIL with `ModuleNotFoundError: iwiki_mcp.engine.okf_artifacts`

- [ ] **Step 3: Write minimal implementation**

Create `src/iwiki_mcp/engine/okf_artifacts.py`:

```python
"""OKF reserved-file generators (stdlib-only, deterministic) plus the shared
RESERVED_OKF name constant. Same domain state -> identical bytes. Safe to import
from the config-free engine modules (lint, grep) and the top layer."""
from __future__ import annotations

RESERVED_OKF = ("index.md", "log.md")


def render_index(slugs: list[str]) -> str:
    """OKF index.md: a heading plus a sorted markdown-link list of page slugs."""
    lines = ["# Index", ""]
    lines += [f"- [{s}]({s}.md)" for s in sorted(slugs)]
    return "\n".join(lines) + "\n"


def render_log(records: list[dict]) -> str:
    """OKF log.md: a heading plus one line per ingest-log record, in file order."""
    lines = ["# Log", ""]
    for r in records:
        lines.append(
            f"- {r.get('date', '')} {r.get('op', '')} {r.get('page', '')}".rstrip()
        )
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_okf_artifacts.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/engine/okf_artifacts.py tests/test_okf_artifacts.py
git commit -m "feat(okf): reserved-file generators + RESERVED_OKF constant"
```

---

### Task 2: Exclude reserved files from every page enumeration

The `.iwiki/` skip is inlined at each site; add the same `RESERVED_OKF` check beside it so reserved files never enter the index, lexical search, listings, migrate sweep, or lint.

**Files:**
- Modify: `src/iwiki_mcp/indexer.py` (the `files = sorted(...)` walk)
- Modify: `src/iwiki_mcp/engine/grep.py` (`grep_sections` walk)
- Modify: `src/iwiki_mcp/engine/lint.py` (`_pages`)
- Modify: `src/iwiki_mcp/server.py` (`wiki_list_pages`, `_unmigrated_pages`)
- Test: `tests/test_okf_artifacts.py`

**Interfaces:**
- Consumes: `okf_artifacts.RESERVED_OKF` (Task 1).
- Produces: no signature changes; behavior — a domain-root `index.md` / `log.md` is invisible to index/search/list/lint.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_okf_artifacts.py`:

```python
from iwiki_mcp.engine.grep import grep_sections
from iwiki_mcp.engine.lint import lint


def test_grep_skips_reserved(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / "a.md").write_text("## H\ntoken here\n", encoding="utf-8")
    (d / "index.md").write_text("## H\ntoken here\n", encoding="utf-8")
    hits = grep_sections(str(d), "token", 10)
    assert [h["file"] for h in hits] == ["a.md"]


def test_lint_skips_reserved(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / "a.md").write_text(
        "---\ntype: concept\n---\n# A\n\n## Overview\ns\n", encoding="utf-8")
    (d / "index.md").write_text("# Index\n\n- [a](a.md)\n", encoding="utf-8")
    (d / "log.md").write_text("# Log\n\n", encoding="utf-8")
    report = lint(str(d))
    assert report["pages"] == 1                      # only a.md counted
    assert report["missing_frontmatter"] == []       # index.md/log.md not flagged
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_okf_artifacts.py -q`
Expected: FAIL — `grep` returns `index.md`; `lint` counts 3 pages and flags `index.md`/`log.md` as `missing_frontmatter`.

- [ ] **Step 3: Modify `indexer.py`**

Add the import after the existing `from .base import index_path, log_path` line (line 11):

```python
from .engine.okf_artifacts import RESERVED_OKF
```

Replace the `files = sorted(...)` block in `index_domain` (currently lines 33-36):

```python
    files = sorted(
        path for path in dom_path.rglob("*.md")
        if ".iwiki" not in path.relative_to(dom_path).parts
        and path.relative_to(dom_path).as_posix() not in RESERVED_OKF
    )
```

- [ ] **Step 4: Modify `engine/grep.py`**

Add after the `import re` line (line 7):

```python
from .okf_artifacts import RESERVED_OKF
```

In `grep_sections`, extend the skip (currently lines 25-27):

```python
        rel_path = md.relative_to(root)
        if ".iwiki" in rel_path.parts or rel_path.as_posix() in RESERVED_OKF:
            continue
```

- [ ] **Step 5: Modify `engine/lint.py`**

Add after `from . import frontmatter as _fm` (line 16):

```python
from .okf_artifacts import RESERVED_OKF
```

Replace `_pages` (currently lines 25-28):

```python
def _pages(wiki_dir: str) -> list[str]:
    """All docs/wiki/**/*.md (normalised), excluding the .iwiki index dir and
    the generated OKF reserved files (index.md / log.md)."""
    files = glob.glob(os.path.join(wiki_dir, "**", "*.md"), recursive=True)
    out = []
    for f in files:
        if "/.iwiki/" in f:
            continue
        if os.path.relpath(f, wiki_dir) in RESERVED_OKF:
            continue
        out.append(os.path.normpath(f))
    return sorted(out)
```

- [ ] **Step 6: Modify `server.py` (`wiki_list_pages` + `_unmigrated_pages`)**

Add the import beside the other engine imports (after line 21, `from .engine.links import to_markdown_links`):

```python
from .engine.okf_artifacts import RESERVED_OKF
```

In `wiki_list_pages`, extend the skip (currently lines 192-194):

```python
        if ".iwiki" in rel_path.parts or rel_path.as_posix() in RESERVED_OKF:
            continue
```

In `_unmigrated_pages`, extend the skip (currently lines 766-767):

```python
        if ".iwiki" in rel.parts or rel.as_posix() in RESERVED_OKF:
            continue
```

- [ ] **Step 7: Run tests + lint to verify**

Run: `uv run pytest tests/test_okf_artifacts.py -q && uv run flake8 src tests`
Expected: PASS; flake8 clean.

- [ ] **Step 8: Commit**

```bash
git add src/iwiki_mcp/indexer.py src/iwiki_mcp/engine/grep.py src/iwiki_mcp/engine/lint.py src/iwiki_mcp/server.py tests/test_okf_artifacts.py
git commit -m "feat(okf): exclude reserved files from index/search/list/lint"
```

---

### Task 3: `okf.refresh_artifacts` — write reserved files in-place

**Files:**
- Modify: `src/iwiki_mcp/okf.py`
- Test: `tests/test_okf_artifacts.py`

**Interfaces:**
- Consumes: `okf_artifacts.render_index` / `render_log` / `RESERVED_OKF` (Task 1); `frontmatter.split` (existing, imported as `fm`).
- Produces: `okf.refresh_artifacts(base_dir: str, domain: str) -> str | None` — writes `index.md` + `log.md` into `<base_dir>/<domain>/`; returns a warning string (authored-reserved collision or failure) or `None`. Never raises. Also private `okf._page_slugs(dom_path: Path) -> list[str]` reused by Task 6.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_okf_artifacts.py`:

```python
from iwiki_mcp import okf


def test_refresh_artifacts_writes_index_and_log(tmp_path):
    dom = tmp_path / "wiki" / "d"
    (dom / ".iwiki").mkdir(parents=True)
    (dom / ".iwiki" / "log.jsonl").write_text(
        '{"op":"ingest","page":"a.md","date":"2026-07-01"}\n', encoding="utf-8")
    (dom / "a.md").write_text(
        "---\ntype: concept\n---\n# A\n\n## Overview\ns\n", encoding="utf-8")
    warn = okf.refresh_artifacts(str(tmp_path / "wiki"), "d")
    assert warn is None
    assert (dom / "index.md").read_text(encoding="utf-8") == "# Index\n\n- [a](a.md)\n"
    assert (dom / "log.md").read_text(encoding="utf-8") == \
        "# Log\n\n- 2026-07-01 ingest a.md\n"


def test_refresh_artifacts_excludes_reserved_from_index(tmp_path):
    dom = tmp_path / "wiki" / "d"
    (dom / ".iwiki").mkdir(parents=True)
    (dom / "a.md").write_text("# A\n\n## Overview\ns\n", encoding="utf-8")
    okf.refresh_artifacts(str(tmp_path / "wiki"), "d")           # first run
    okf.refresh_artifacts(str(tmp_path / "wiki"), "d")           # idempotent re-run
    assert (dom / "index.md").read_text(encoding="utf-8") == "# Index\n\n- [a](a.md)\n"


def test_refresh_artifacts_warns_on_authored_reserved(tmp_path):
    dom = tmp_path / "wiki" / "d"
    (dom / ".iwiki").mkdir(parents=True)
    (dom / "index.md").write_text(
        "---\ntype: concept\n---\n# Real\n\n## Overview\nx\n", encoding="utf-8")
    warn = okf.refresh_artifacts(str(tmp_path / "wiki"), "d")
    assert warn and "index.md" in warn
    assert "Real" in (dom / "index.md").read_text(encoding="utf-8")   # left intact
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_okf_artifacts.py -k refresh -q`
Expected: FAIL with `AttributeError: module 'iwiki_mcp.okf' has no attribute 'refresh_artifacts'`

- [ ] **Step 3: Add the implementation to `okf.py`**

Add these imports to the top of `src/iwiki_mcp/okf.py` (it already imports `json`, `subprocess`, and `frontmatter as fm`):

```python
import os
from pathlib import Path

from .engine import okf_artifacts as _oa
```

Append these functions to `src/iwiki_mcp/okf.py`:

```python
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
    meta, _ = fm.split(text)
    if meta:
        return True
    return any(ln.startswith("## ") for ln in text.splitlines())


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_okf_artifacts.py -q && uv run flake8 src tests`
Expected: PASS; flake8 clean.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/okf.py tests/test_okf_artifacts.py
git commit -m "feat(okf): refresh_artifacts writes reserved files in-place"
```

---

### Task 4: Wire `refresh_artifacts` into the mutating handlers

Every mutating handler regenerates the reserved files after re-index, before the auto-commit, so one commit captures pages + `.iwiki/` + reserved files.

**Files:**
- Modify: `src/iwiki_mcp/server.py` (`wiki_write_page`, `wiki_update_page`, `wiki_delete_page`, `wiki_apply_okf`, `wiki_migrate_okf` autonomous branch)
- Test: `tests/test_okf_server.py` (new)

**Interfaces:**
- Consumes: `okf.refresh_artifacts` (Task 3); reserved exclusion in indexer (Task 2).
- Produces: after any successful mutating call, `<base>/<domain>/index.md` + `log.md` exist and are current; a collision warning surfaces on the result `warning` key.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_okf_server.py`:

```python
import os

from iwiki_mcp import base, indexer, server
from iwiki_mcp.engine.store import VectorStore


def _seed(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
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


def test_write_refreshes_okf_artifacts(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    server.wiki_write_page("backend", "auth", "# Auth\n\n## Overview\ns\n\n## Flow\nx\n")
    dom = os.path.join(b, "backend")
    assert os.path.isfile(os.path.join(dom, "index.md"))
    assert os.path.isfile(os.path.join(dom, "log.md"))
    assert "[auth](auth.md)" in open(os.path.join(dom, "index.md"), encoding="utf-8").read()


def test_reserved_files_not_indexed(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    server.wiki_write_page("backend", "auth", "# Auth\n\n## Overview\ns\n\n## Flow\nx\n")
    server.wiki_index("backend")            # reindex with index.md/log.md present
    recs = VectorStore(base.index_path(b, "backend")).load()
    assert all(r.file not in ("index.md", "log.md") for r in recs)


def test_delete_refreshes_index(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    server.wiki_write_page("backend", "auth", "# Auth\n\n## Overview\ns\n\n## Flow\nx\n")
    server.wiki_write_page("backend", "db", "# DB\n\n## Overview\ns\n\n## Schema\nx\n")
    server.wiki_delete_page("backend", "auth")
    idx = open(os.path.join(b, "backend", "index.md"), encoding="utf-8").read()
    assert "[db](db.md)" in idx and "auth.md" not in idx
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_okf_server.py -q`
Expected: FAIL — no `index.md` / `log.md` written.

- [ ] **Step 3: Add the refresh call to `wiki_write_page`**

In `wiki_write_page`, immediately before `page_rel = f"{valid_domain}/{page_file}"` (currently line 391), insert:

```python
    art_warn = okf.refresh_artifacts(bind.base, valid_domain)
```

Then, after the existing `if fm_warning:` block (currently lines 403-404), add:

```python
    if art_warn:
        result.setdefault("warning", art_warn)
```

- [ ] **Step 4: Add the refresh call to `wiki_update_page`, `wiki_delete_page`, `wiki_apply_okf`**

For each of these three handlers, insert `art_warn = okf.refresh_artifacts(bind.base, valid_domain)` immediately before its `commit = sync.commit_and_push(...)` line, change its `return {…}` into `result = {…}`, and append before the return:

```python
    if art_warn:
        result.setdefault("warning", art_warn)
    return result
```

Concretely, in `wiki_update_page` (return currently at lines 483-494), `wiki_delete_page` (536-543), and `wiki_apply_okf` (884-887): assign the dict to `result`, add the `art_warn` call above the commit, and the two-line guard + `return result` below.

- [ ] **Step 5: Add the refresh call to `wiki_migrate_okf` (autonomous branch only)**

In `wiki_migrate_okf`, in the `if cfg.chat_model:` branch, insert before its `commit = sync.commit_and_push(...)` (currently line 809):

```python
        art_warn = okf.refresh_artifacts(bind.base, target)
```

and add `**({"warning": art_warn} if art_warn else {})` into that branch's returned dict (currently lines 811-815), after `**_fresh_warn(fresh)`. The plan-mode branch performs no writes and needs no refresh.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_okf_server.py tests/test_server_write.py -q && uv run flake8 src tests`
Expected: PASS (existing write tests still green); flake8 clean.

- [ ] **Step 7: Commit**

```bash
git add src/iwiki_mcp/server.py tests/test_okf_server.py
git commit -m "feat(okf): maintain reserved files in-place on every write"
```

---

### Task 5: Reserved-slug write guard

**Files:**
- Modify: `src/iwiki_mcp/server.py` (`wiki_write_page`)
- Test: `tests/test_okf_server.py`

**Interfaces:**
- Consumes: `RESERVED_OKF` (imported in Task 2).
- Produces: `wiki_write_page` returns `{error, hint}` (no side effects) when the slug resolves to `index.md` / `log.md`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_okf_server.py`:

```python
def test_write_rejects_reserved_slug(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    out = server.wiki_write_page("backend", "index", "# I\n\n## Overview\nx\n")
    assert "error" in out and "reserved" in out["error"]
    assert not os.path.isfile(os.path.join(b, "backend", "index.md"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_okf_server.py::test_write_rejects_reserved_slug -q`
Expected: FAIL — the page is written (or a different error).

- [ ] **Step 3: Add the guard to `wiki_write_page`**

Immediately after `page_file = PurePosixPath(*_slug_parts(slug)).as_posix() + ".md"` (currently line 358), insert:

```python
    if page_file in RESERVED_OKF:
        return {
            "error": f"slug '{slug}' is reserved for the generated OKF file "
                     f"'{page_file}'",
            "hint": "choose another slug; index/log are generated, not authored",
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_okf_server.py -q && uv run flake8 src tests`
Expected: PASS; flake8 clean.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/server.py tests/test_okf_server.py
git commit -m "feat(okf): reject reserved index/log slugs on write"
```

---

### Task 6: Batch sweep + repurpose `wiki_export_okf`; delete `export.py`

`okf.batch_sweep` fixes frontmatter + links across all pages in-place (deterministic, reusing existing helpers). `wiki_export_okf(domain=None)` runs the sweep, re-indexes, refreshes reserved files, and reports residue. `export.py` and its old tests are removed.

**Files:**
- Modify: `src/iwiki_mcp/okf.py` (`batch_sweep`)
- Modify: `src/iwiki_mcp/server.py` (rewrite `wiki_export_okf`)
- Delete: `src/iwiki_mcp/export.py`
- Rewrite: `tests/test_export_okf.py`

**Interfaces:**
- Consumes: `okf._page_slugs`, `okf.refresh_artifacts`, `okf.build_frontmatter`, `okf.latest_source` (existing/Task 3); `links.to_markdown_links`; `frontmatter` helpers; `RESERVED_OKF` (imported in Task 2).
- Produces: `okf.batch_sweep(cfg, base_dir, domain) -> {"fixed_links": list[str], "added_frontmatter": list[str]}`; `wiki_export_okf(domain: str | None = None) -> dict` with keys `domain, fixed_links, added_frontmatter, artifacts, still_missing_frontmatter, still_legacy_wikilink, indexed_chunks, committed, pushed, next_steps`.

- [ ] **Step 1: Add `batch_sweep` to `okf.py`**

Append to `src/iwiki_mcp/okf.py`:

```python
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
```

- [ ] **Step 2: Rewrite `tests/test_export_okf.py` (failing)**

Replace the entire contents of `tests/test_export_okf.py`:

```python
import os

from iwiki_mcp import indexer, server


def _seed(tmp_path, monkeypatch):
    b = tmp_path / "wiki"
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


def test_export_okf_sweep_adds_frontmatter_and_converts_links(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    dom = os.path.join(b, "backend")
    with open(os.path.join(dom, "a.md"), "w", encoding="utf-8") as fh:
        fh.write("# A\n\n## Overview\ns\n\n## B\nsee [[a#B]]\n")
    out = server.wiki_export_okf("backend")
    assert out["domain"] == "backend"
    assert "a" in out["added_frontmatter"]
    assert "a" in out["fixed_links"]
    text = open(os.path.join(dom, "a.md"), encoding="utf-8").read()
    assert text.startswith("---\n")
    assert "type: concept" in text
    assert "[B](a.md#b)" in text                 # wikilink converted in-place
    assert os.path.isfile(os.path.join(dom, "index.md"))
    assert os.path.isfile(os.path.join(dom, "log.md"))


def test_export_okf_idempotent(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    dom = os.path.join(b, "backend")
    with open(os.path.join(dom, "a.md"), "w", encoding="utf-8") as fh:
        fh.write("# A\n\n## Overview\ns\n")
    server.wiki_export_okf("backend")
    first = open(os.path.join(dom, "a.md"), encoding="utf-8").read()
    out2 = server.wiki_export_okf("backend")
    second = open(os.path.join(dom, "a.md"), encoding="utf-8").read()
    assert first == second
    assert out2["added_frontmatter"] == []
    assert out2["fixed_links"] == []


def test_export_okf_preserves_existing_type(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    dom = os.path.join(b, "backend")
    with open(os.path.join(dom, "a.md"), "w", encoding="utf-8") as fh:
        fh.write("---\ntype: api\n---\n# A\n\n## Overview\ns\n")
    server.wiki_export_okf("backend")
    text = open(os.path.join(dom, "a.md"), encoding="utf-8").read()
    assert "type: api" in text
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_export_okf.py -q`
Expected: FAIL — `wiki_export_okf` still requires `dest` / imports the old `export` module.

- [ ] **Step 4: Delete `export.py` and rewrite `wiki_export_okf`**

Delete the file:

```bash
git rm src/iwiki_mcp/export.py
```

Replace the whole `wiki_export_okf` function in `server.py` (currently lines 890-902) with:

```python
@_safe
def wiki_export_okf(domain: str | None = None) -> dict:
    bind = base.resolve_binding()
    target = domain or bind.write
    if not target:
        return {"error": "no domain given and no write-target bound",
                "hint": "pass domain= or set write in .iwiki.toml via wiki_bind"}
    valid_domain = _validate_domain(target)
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    dom_path = _domain_path(bind.base, valid_domain)
    if not dom_path.is_dir():
        return {"error": f"domain '{valid_domain}' not found",
                "hint": "create it with wiki_create_domain"}
    cfg = Config.load()
    swept = okf.batch_sweep(cfg, bind.base, valid_domain)
    stats = indexer.index_domain(cfg, bind.base, valid_domain)
    art_warn = okf.refresh_artifacts(bind.base, valid_domain)
    commit = sync.commit_and_push(bind.base, f"iwiki: export okf {valid_domain}",
                                  pathspec=valid_domain)
    from .engine.lint import lint
    report = lint(str(dom_path), project_dir=bind.project_dir)
    result = {
        "domain": valid_domain,
        "fixed_links": swept["fixed_links"],
        "added_frontmatter": swept["added_frontmatter"],
        "artifacts": list(RESERVED_OKF),
        "still_missing_frontmatter": report.get("missing_frontmatter", []),
        "still_legacy_wikilink": report.get("legacy_wikilink", []),
        "indexed_chunks": stats["indexed_chunks"],
        "committed": commit.get("committed", False),
        "pushed": commit.get("pushed", False),
        "next_steps": ["Run wiki_migrate_okf for better type/tags than the "
                       "deterministic 'concept' default on newly added frontmatter."],
        **_fresh_warn(fresh),
    }
    if art_warn:
        result.setdefault("warning", art_warn)
    return result
```

(The MCP registration `mcp.tool()(wiki_export_okf)` at the bottom is unchanged.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_export_okf.py -q && uv run flake8 src tests`
Expected: PASS; flake8 clean; no reference to the deleted `export` module.

- [ ] **Step 6: Commit**

```bash
git add src/iwiki_mcp/okf.py src/iwiki_mcp/server.py tests/test_export_okf.py
git commit -m "feat(okf): wiki_export_okf becomes an in-place conformance sweep"
```

---

### Task 7: Docs, authoring rules, and version bump

**Files:**
- Modify: `src/iwiki_mcp/resources.py`
- Modify: `README.md`, `docs/README.ru.md`
- Modify: `pyproject.toml`, `src/iwiki_mcp/__init__.py`

**Interfaces:**
- Consumes: nothing (documentation of Tasks 1-6).
- Produces: user-facing docs describing the in-place sweep + reserved slugs; version `0.2.4`.

- [ ] **Step 1: Update `resources.py` authoring rules**

In `src/iwiki_mcp/resources.py`, in the `## OKF frontmatter` block, append a bullet after the `tags` bullet (before the closing `"""`):

```python
- The slugs `index` and `log` are reserved: `index.md` / `log.md` are generated
  OKF navigation/history files kept fresh in the domain on every write. Do not
  author a page with either slug -- the write tools reject it.
```

- [ ] **Step 2: Update `README.md`**

Replace the `wiki_export_okf(domain, dest)` table row (currently line 259) with:

```markdown
| `wiki_export_okf(domain=None)` | Whole-domain, in-place OKF conformance sweep (no copy, no `dest`): converts any residual `[[wikilink]]` to Markdown links and guarantees frontmatter on every page (deterministic `type: concept` where missing; existing `type`/`tags` preserved), then regenerates the reserved `index.md` / `log.md`. Deterministic — never calls the chat model. Returns `fixed_links`, `added_frontmatter`, and `still_missing_frontmatter` / `still_legacy_wikilink`, with a `next_steps` hint to `wiki_migrate_okf` for better `type`/`tags`. The domain directory is itself the OKF bundle. |
```

After the OKF-compat intro paragraph (after line 234, the `Every page carries…` paragraph), add:

```markdown
The reserved OKF files `index.md` (navigation) and `log.md` (history) are kept fresh in the domain directory on every write, so a git-synced domain is always a complete OKF bundle read directly by external consumers — there is no separate export copy. The `index` and `log` slugs are reserved and rejected by `wiki_write_page`.
```

- [ ] **Step 3: Update `docs/README.ru.md`**

Replace the `wiki_export_okf(domain, dest)` row (currently line 255) with the Russian equivalent:

```markdown
| `wiki_export_okf(domain=None)` | Проход по всему домену, приводящий его к OKF **на месте** (без копии, без `dest`): переписывает остаточные `[[wikilink]]` в Markdown-ссылки и гарантирует фронтматтер на каждой странице (детерминированно `type: concept` там, где его нет; существующие `type`/`tags` сохраняются), затем перегенерирует зарезервированные `index.md` / `log.md`. Детерминирован — чат-модель не вызывает. Возвращает `fixed_links`, `added_frontmatter` и `still_missing_frontmatter` / `still_legacy_wikilink`, плюс `next_steps` к `wiki_migrate_okf` для более точных `type`/`tags`. Каталог домена сам является OKF-пакетом. |
```

After the OKF-compat intro (after line 230, the `Каждая страница несёт…` paragraph), add:

```markdown
Зарезервированные OKF-файлы `index.md` (навигация) и `log.md` (история) поддерживаются свежими в каталоге домена при каждой записи, поэтому git-синхронизированный домен всегда является полным OKF-пакетом, который внешний потребитель читает напрямую — отдельной копии-экспорта нет. Слаги `index` и `log` зарезервированы, `wiki_write_page` их отклоняет.
```

- [ ] **Step 4: Bump the version**

In `pyproject.toml` change `version = "0.2.3"` to `version = "0.2.4"`.

Find and bump the package version constant:

```bash
grep -n "0.2.3" src/iwiki_mcp/__init__.py
```

Edit `src/iwiki_mcp/__init__.py` so `__version__ = "0.2.4"`.

- [ ] **Step 5: Verify by inspection**

Run:

```bash
grep -n '0.2.4' pyproject.toml src/iwiki_mcp/__init__.py
grep -n 'reserved' src/iwiki_mcp/resources.py
grep -n 'wiki_export_okf(domain=None)' README.md docs/README.ru.md
```

Expected: version `0.2.4` in both files; a reserved-slug line in `resources.py`; the new row in both READMEs.

- [ ] **Step 6: Commit**

```bash
git add src/iwiki_mcp/resources.py README.md docs/README.ru.md pyproject.toml src/iwiki_mcp/__init__.py
git commit -m "docs(okf): document in-place sweep + reserved slugs; bump 0.2.4"
```

---

### Task 8: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Run the whole suite and lint**

Run: `uv run pytest -q && uv run flake8 src tests`
Expected: all tests pass; flake8 clean.

- [ ] **Step 2: Confirm no orphaned references**

Run: `grep -rn 'export_domain\|convert_wikilinks\|from . import export\|iwiki_mcp.export' src tests`
Expected: no output.

- [ ] **Step 3: Manual smoke check (optional, real run)**

Drive `wiki_export_okf` once against a scratch domain per the spec's Verification section, confirming `index.md` / `log.md` appear and a legacy page gains frontmatter + markdown links in place.
