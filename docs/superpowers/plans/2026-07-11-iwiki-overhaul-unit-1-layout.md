---
review:
  stage: plan
  plan_hash: 06057015eea0f844
  last_run: 2026-07-11
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
      phase: consistency
      severity: WARNING
      text: "Task 2 Step 5 placeholder code block removed; real read-path migration call kept."
      verdict: fixed
    - id: F-002
      phase: consistency
      severity: WARNING
      text: "Task 6 _seed misuse replaced with local _bind harness (documented in Global Constraints)."
      verdict: fixed
    - id: F-003
      phase: consistency
      severity: WARNING
      text: "Task 3 test_lint fixture line-list corrected to grep-driven (~10 blocks)."
      verdict: fixed
result_check:
  verdict: OK
  plan_hash: 06057015eea0f844
  last_run: 2026-07-11
chain:
  intent: n/a
  spec: docs/superpowers/specs/2026-07-11-iwiki-layout-retrieval-overhaul-design.md
---
# iwiki overhaul — Unit 1: layout cleanup (C + A + E) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Relocate each domain's vector store + ingest log from `<domain>/.iwiki/` to the domain root, fix the stale two-level `AUTHORING_RULES` text, and make `index.md`/`log.md` OKF artifacts export-only (no longer written on every page write).

**Architecture:** Pure path-resolution change in `base.py` plus a best-effort one-time on-access migration that moves the two `*.jsonl` files up one level and removes the empty legacy dir. `resources.py` text edit. Remove the `okf.refresh_artifacts` call from the five write handlers, leaving it only in `wiki_export_okf`.

**Tech Stack:** Python 3.10+ (`tomllib`/`tomli` fallback), stdlib-only engine core, `pytest` (`asyncio_mode=auto`, `pythonpath=["src"]`), `flake8` max-line-length 100.

## Global Constraints

- Python 3.10+ compatible; no new runtime dependencies.
- `flake8 src tests` must stay clean (max-line-length 100); no black/ruff — match surrounding style by hand.
- Tests never hit the network: `monkeypatch` `indexer.embed_texts` and set dummy `IWIKI_*` env vars (see `tests/test_server_write.py::_seed`).
- The **base-level** `<base>/.iwiki/` dir (server lock, `lock.py`) is SEPARATE from the per-domain `<domain>/.iwiki/` and is NOT touched by this unit. Do not change `lock.py` or `tests/test_lock.py`.
- Version bump this unit: `pyproject.toml` and `src/iwiki_mcp/__init__.py` `__version__` `0.5.1` → `0.6.0` (minor — starts the layout overhaul).
- Keep the `@_safe` split (plain impl functions, `mcp.tool()(...)` registration at the bottom of `server.py`).
- **Test harness.** Where a test needs a bound server (writes/search), define a local helper in that test file: set `IWIKI_LLM_BASE_URL`/`IWIKI_LLM_KEY`, `monkeypatch.setattr(base, "resolve_binding", lambda project_dir=None: base.Binding(base=str(tmp_path), read=(dom,), write=dom, project_dir=str(tmp_path)))`, `monkeypatch.setattr(indexer, "embed_texts", lambda cfg, t: [[0.0]*cfg.dimensions for _ in t])`, and `os.makedirs(tmp_path/dom, exist_ok=True)`. The existing `tests/test_server_write.py::_seed` is `_seed(tmp_path, monkeypatch, with_domain=True) -> (base, proj)` seeding domain `backend` — do NOT call it as `_seed(..., "d")`; either reuse it as `b, _ = _seed(tmp_path, monkeypatch)` and write to `backend`, or use the local helper above.

---

### Task 1: Fix `AUTHORING_RULES` text (workstream A)

**Files:**
- Modify: `src/iwiki_mcp/resources.py:19,27-29,38`
- Test: `tests/test_resources.py`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing (text only; surfaced as the `iwiki://authoring-rules` resource).

- [ ] **Step 1: Write the failing test**

Find the existing `tests/test_resources.py`; if absent, create it. Add:

```python
from iwiki_mcp.resources import AUTHORING_RULES


def test_description_is_separate_summary_vector_not_prefix():
    # The stale two-level lie must be gone.
    assert "context prefix" not in AUTHORING_RULES
    # The corrected model must be stated.
    assert "summary-level vector" in AUTHORING_RULES


def test_links_use_type_slug_path_and_export_only_artifacts():
    assert "(<type>/<slug>.md#heading)" in AUTHORING_RULES
    assert "export-only" in AUTHORING_RULES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_resources.py -v`
Expected: FAIL (`"context prefix"` still present / new strings absent).

- [ ] **Step 3: Edit the rules string**

In `src/iwiki_mcp/resources.py` replace the `description` bullet (lines 27-29):

```python
- `description` is the authored article summary and the single source of it. It is
  indexed as its own **summary-level vector that seeds retrieval** (two-level:
  summary seed -> graph-expanded pool -> section vectors ranked inside it), NOT
  prefixed onto section vectors. Write it rich: include `Covers:` and `Terms:`
  keyword lines so the summary matches broad queries. There is no `## Overview`.
```

Change the cross-link bullet (line 19) to the type/slug path:

```python
- Cross-link related pages with `[Heading](<type>/<slug>.md#heading)` (within the same domain in v1).
```

Change the reserved-file note (line 38) to export-only:

```python
- The slugs `index` and `log` are reserved: `index.md` / `log.md` are **export-only**
  OKF navigation/history files, generated by `wiki_export_okf` (not refreshed on every
  write). The write tools reject these slugs.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_resources.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/resources.py tests/test_resources.py
git commit -m "fix(resources): correct two-level description text + type/slug links + export-only artifacts"
```

---

### Task 2: Relocate store/log paths + on-access migration (workstream C core)

**Files:**
- Modify: `src/iwiki_mcp/base.py:113-118` (add migration helper after)
- Modify: `src/iwiki_mcp/indexer.py` (call migration in `index_domain`)
- Modify: `src/iwiki_mcp/retrieval.py:71-72` (call migration in the per-domain loop)
- Test: `tests/test_base.py:142`, new `tests/test_store_migration.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `base.index_path(base, domain) -> "<base>/<domain>/index.jsonl"`
  - `base.log_path(base, domain) -> "<base>/<domain>/log.jsonl"`
  - `base.migrate_store_location(base: str, domain: str) -> None` — best-effort, idempotent, never raises.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_store_migration.py`:

```python
import os
from iwiki_mcp import base


def test_index_and_log_paths_at_domain_root(tmp_path):
    b = str(tmp_path)
    assert base.index_path(b, "d") == os.path.join(b, "d", "index.jsonl")
    assert base.log_path(b, "d") == os.path.join(b, "d", "log.jsonl")


def test_migrate_moves_legacy_iwiki_store_to_root(tmp_path):
    dom = tmp_path / "d"
    (dom / ".iwiki").mkdir(parents=True)
    (dom / ".iwiki" / "index.jsonl").write_text("{}\n", encoding="utf-8")
    (dom / ".iwiki" / "log.jsonl").write_text("{}\n", encoding="utf-8")

    base.migrate_store_location(str(tmp_path), "d")

    assert (dom / "index.jsonl").is_file()
    assert (dom / "log.jsonl").is_file()
    assert not (dom / ".iwiki").exists()  # empty legacy dir removed


def test_migrate_is_idempotent_and_never_clobbers(tmp_path):
    dom = tmp_path / "d"
    dom.mkdir(parents=True)
    (dom / "index.jsonl").write_text("NEW\n", encoding="utf-8")
    (dom / ".iwiki").mkdir()
    (dom / ".iwiki" / "index.jsonl").write_text("OLD\n", encoding="utf-8")

    base.migrate_store_location(str(tmp_path), "d")  # root already has index.jsonl

    assert (dom / "index.jsonl").read_text() == "NEW\n"  # not clobbered
    base.migrate_store_location(str(tmp_path), "d")  # second run: no error
```

Update the existing assertion in `tests/test_base.py` (line ~142) that expects `os.path.join(".iwiki", "index.jsonl")` to expect a bare `index.jsonl`:

```python
        "index.jsonl"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_store_migration.py tests/test_base.py -v`
Expected: FAIL (`index_path` still returns `.iwiki/...`; `migrate_store_location` undefined).

- [ ] **Step 3: Edit `base.py`**

Replace lines 113-118 and append the helper:

```python
def index_path(base: str, domain: str) -> str:
    return os.path.join(domain_dir(base, domain), "index.jsonl")


def log_path(base: str, domain: str) -> str:
    return os.path.join(domain_dir(base, domain), "log.jsonl")


def migrate_store_location(base: str, domain: str) -> None:
    """Best-effort one-time move of a domain's store/log out of the legacy
    ``.iwiki/`` subdir to the domain root. Idempotent, never clobbers an existing
    root file, never raises. Removes the legacy dir only when it is left empty."""
    dom = domain_dir(base, domain)
    legacy = os.path.join(dom, ".iwiki")
    for name in ("index.jsonl", "log.jsonl"):
        old = os.path.join(legacy, name)
        new = os.path.join(dom, name)
        try:
            if os.path.isfile(old) and not os.path.exists(new):
                os.replace(old, new)
        except OSError:
            pass
    try:
        if os.path.isdir(legacy) and not os.listdir(legacy):
            os.rmdir(legacy)
    except OSError:
        pass
```

- [ ] **Step 4: Call migration on the write path (`indexer.index_domain`)**

In `src/iwiki_mcp/indexer.py`, import and call at the very top of `index_domain` (before `index_path` is used). Change the import line and add the call:

```python
from .base import index_path, log_path, migrate_store_location
```

```python
def index_domain(cfg: Config, base: str, domain: str) -> dict:
    migrate_store_location(base, domain)
    dom_path = Path(base) / domain
    idx = index_path(base, domain)
```

- [ ] **Step 5: Call migration on the read path (`retrieval.vector_search`)**

In `src/iwiki_mcp/retrieval.py`, extend the top import to
`from .base import domain_dir, index_path, migrate_store_location`, then call the
migration inside the per-domain loop (lines ~71-72) before `_hier_vector`:

```python
    for d in domains:
        migrate_store_location(base, d)
        hits.extend(_hier_vector(cfg, base, d, qv, top_k, threshold, type, tags))
```

Note (spec-divergence, intentional): the spec assigns the store-relocation move to
`wiki_migrate_okf`. This unit performs it best-effort on-access (in `index_domain`
and here) so read-only domains keep working before Unit 2's migrate tool runs. A
`wiki_search` may therefore relocate a legacy `.iwiki/` store on first access —
idempotent and safe.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_store_migration.py tests/test_base.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/iwiki_mcp/base.py src/iwiki_mcp/indexer.py src/iwiki_mcp/retrieval.py tests/test_store_migration.py tests/test_base.py
git commit -m "feat(base): store/log at domain root + best-effort .iwiki->root migration"
```

---

### Task 3: Update `okf.py` + `lint.py` store/log references (workstream C)

**Files:**
- Modify: `src/iwiki_mcp/okf.py:117-124,128` (`_page_slugs` comment/guard, `_read_log`)
- Modify: `src/iwiki_mcp/engine/lint.py:32,99`
- Test: `tests/engine/test_lint.py` (log path setup), `tests/test_okf_server.py`

**Interfaces:**
- Consumes: `base.log_path` (now domain root).
- Produces: nothing new.

- [ ] **Step 1: Update `okf._read_log`**

In `src/iwiki_mcp/okf.py`, `_read_log` (line ~127-128) reads the legacy path. Change to the domain root:

```python
def _read_log(dom_path: Path) -> list:
    path = dom_path / "log.jsonl"
```

`_page_slugs` still excludes a `.iwiki` path segment (line 121) — the relocated `*.jsonl` are not `*.md`, so this is now dead. Simplify the guard and comment:

```python
def _page_slugs(dom_path: Path) -> list[str]:
    """Domain page slugs, excluding the reserved OKF files."""
    out = []
    for p in sorted(dom_path.rglob("*.md")):
        rel = p.relative_to(dom_path)
        if rel.as_posix() in _oa.RESERVED_OKF:
            continue
        out.append(rel.with_suffix("").as_posix())
    return out
```

- [ ] **Step 2: Update `engine/lint.py`**

`lint.py:99` reads `wiki_dir/.iwiki/log.jsonl`. Change to the domain root:

```python
    log = os.path.join(wiki_dir, "log.jsonl")
```

`lint.py:32` excludes `/.iwiki/` when collecting `*.md`. The relocated `*.jsonl` are not `*.md`; keep the exclusion harmless but update the docstring at line 27 to drop the ".iwiki index dir" claim, or leave the guard (it never matches a `.jsonl`). Minimal change: leave line 32 as-is (it filters nothing now) and only fix line 99. Verify no test asserts the `.iwiki` exclusion string.

- [ ] **Step 3: Update the lint test fixtures**

In `tests/engine/test_lint.py`, the fixtures create `<wd>/.iwiki` and write `log.jsonl` there (~10 fixture blocks — do NOT trust a fixed line list; `grep -n '\.iwiki' tests/engine/test_lint.py` for the full set). Change each to write `log.jsonl` at `wd` root. Example transform:

```python
# before:  iwiki = os.path.join(wd, ".iwiki"); os.makedirs(iwiki); open(os.path.join(iwiki, "log.jsonl"), "w")...
# after:   open(os.path.join(wd, "log.jsonl"), "w")...
```

- [ ] **Step 4: Run the affected tests**

Run: `uv run pytest tests/engine/test_lint.py tests/test_okf_server.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/okf.py src/iwiki_mcp/engine/lint.py tests/engine/test_lint.py
git commit -m "refactor(okf,lint): read log.jsonl from domain root; drop dead .iwiki guards"
```

---

### Task 4: Stop creating `<domain>/.iwiki/`; clean dead guards (workstream C)

**Files:**
- Modify: `src/iwiki_mcp/server.py:642` (`wiki_create_domain`), `:208,823` (rglob guards)
- Modify: `src/iwiki_mcp/indexer.py:36` (rglob guard), `src/iwiki_mcp/engine/grep.py:28`
- Modify: `src/iwiki_mcp/sync.py:92` (hint string)
- Test: new `tests/test_create_domain_layout.py`

**Interfaces:**
- Consumes: `base.index_path`.
- Produces: nothing new.

- [ ] **Step 1: Write the failing test**

Create `tests/test_create_domain_layout.py` (follow `tests/test_server_write.py::_seed` for env + monkeypatch):

```python
import os
import iwiki_mcp.server as server
from iwiki_mcp import base, indexer


def _env(monkeypatch, base_dir):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.setattr(indexer, "embed_texts", lambda cfg, texts: [[0.0] * cfg.dimensions for _ in texts])


def test_create_domain_makes_no_iwiki_dir(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(base, "resolve_binding", lambda project_dir=None: base.Binding(
        base=str(tmp_path), read=("d",), write="d", project_dir=str(tmp_path)))
    server.wiki_create_domain("d")
    assert (tmp_path / "d").is_dir()
    assert not (tmp_path / "d" / ".iwiki").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_create_domain_layout.py -v`
Expected: FAIL (`.iwiki` dir still created).

- [ ] **Step 3: Edit `wiki_create_domain`**

`src/iwiki_mcp/server.py:642` — replace the `.iwiki` mkdir with a plain domain-dir create:

```python
    os.makedirs(dom_path, exist_ok=True)
```

- [ ] **Step 4: Remove now-dead `.iwiki` rglob guards**

These filter a `.iwiki` path segment when collecting `*.md`; the relocated store is not `*.md`, so drop the `.iwiki` clause, keeping the `RESERVED_OKF` check:
- `src/iwiki_mcp/server.py:208` → `if rel_path.as_posix() in RESERVED_OKF:`
- `src/iwiki_mcp/server.py:823` → `if rel.as_posix() in RESERVED_OKF:`
- `src/iwiki_mcp/indexer.py:36-37` → the `rglob` filter becomes `if path.relative_to(dom_path).as_posix() not in RESERVED_OKF` (drop the `.iwiki` parts check).
- `src/iwiki_mcp/engine/grep.py:28` → `if rel_path.as_posix() in RESERVED_OKF:`

Update `src/iwiki_mcp/sync.py:92` hint string: replace `.iwiki/index.jsonl` with `index.jsonl`.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_create_domain_layout.py tests/test_indexer.py tests/test_grep.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/iwiki_mcp/server.py src/iwiki_mcp/indexer.py src/iwiki_mcp/engine/grep.py src/iwiki_mcp/sync.py tests/test_create_domain_layout.py
git commit -m "refactor(server): create bare domain dir; drop dead .iwiki rglob guards"
```

---

### Task 5: Mechanical test migration (workstream C)

**Files:**
- Modify: the ~24 test files that create `<domain>/.iwiki/` as setup or assert an `.iwiki/*.jsonl` path.
- Do NOT modify: `tests/test_lock.py` (base-level `.iwiki` lock — unrelated).

**Interfaces:** none.

- [ ] **Step 1: List the offenders**

Run:

```bash
grep -rln '\.iwiki' tests/ | grep -v 'iwikiignore\|test_lock.py'
```

- [ ] **Step 2: Apply the two mechanical transforms**

For each listed file:
1. Setup dirs — `(X / ".iwiki").mkdir(parents=True)` → `(X).mkdir(parents=True)` (or drop the line if `X` is already created elsewhere). `os.makedirs(os.path.join(b, "<dom>", ".iwiki"))` → `os.makedirs(os.path.join(b, "<dom>"))`.
2. Path assertions/constructions — `.iwiki/log.jsonl` → `log.jsonl`, `.iwiki/index.jsonl` → `index.jsonl` (e.g. `tests/test_server_write.py:30`, `tests/test_server_migrate.py:84,138`, `tests/test_store_interface.py:10`).

- [ ] **Step 3: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS (all green). Fix any file the grep missed.

- [ ] **Step 4: Commit**

```bash
git add tests/
git commit -m "test: migrate fixtures from <domain>/.iwiki to domain-root store/log"
```

---

### Task 6: `index.md`/`log.md` export-only (workstream E)

**Files:**
- Modify: `src/iwiki_mcp/server.py` — remove `okf.refresh_artifacts(...)` from `wiki_write_page` (~429), `wiki_update_page` (~530), `wiki_delete_page` (~587), `wiki_migrate_okf` (~866), `wiki_apply_okf` (~945). Keep it in `wiki_export_okf` (~976).
- Test: new `tests/test_export_only_artifacts.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: write handlers no longer return an `art_warn`; drop the `art_warn` local and the trailing `if art_warn: result.setdefault(...)` in each edited handler.

- [ ] **Step 1: Write the failing test**

Create `tests/test_export_only_artifacts.py`. Seed a domain, write a page, assert no `index.md`/`log.md`; then run `wiki_export_okf` and assert both appear. Reuse the `_seed`/env pattern from `tests/test_server_write.py`:

```python
def test_write_emits_no_okf_artifacts_but_export_does(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch, "d")                # local helper (see Global Constraints)
    server.wiki_write_page("d", "guide/x", "# X\n\n## Purpose\n\nBody.\n", type="guide")
    assert not (tmp_path / "d" / "index.md").exists()
    assert not (tmp_path / "d" / "log.md").exists()
    server.wiki_export_okf("d")
    assert (tmp_path / "d" / "index.md").is_file()
    assert (tmp_path / "d" / "log.md").is_file()
```

`_bind` is the local harness helper described in Global Constraints (env + `base.resolve_binding` monkeypatch + `indexer.embed_texts` stub + `os.makedirs(tmp_path/dom)`). Note: on the flat layout `wiki_write_page("d", "guide/x", type="guide")` writes `d/guide/x.md` only if Unit 2's identity logic is present; in Unit 1 (pre-D) pass a bare slug `"x"` — the artifact assertions are layout-independent, so use `slug="x"` here.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_export_only_artifacts.py -v`
Expected: FAIL (write currently emits `index.md`/`log.md`).

- [ ] **Step 3: Remove `refresh_artifacts` from the five write handlers**

In each of `wiki_write_page`, `wiki_update_page`, `wiki_delete_page`, `wiki_migrate_okf`, `wiki_apply_okf`: delete the `art_warn = okf.refresh_artifacts(bind.base, valid_domain)` line and the trailing:

```python
    if art_warn:
        result.setdefault("warning", art_warn)
```

Leave `wiki_export_okf` (line ~976) unchanged — it keeps calling `refresh_artifacts`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_export_only_artifacts.py tests/test_export_okf.py tests/test_server_write.py -v`
Expected: PASS. (If `test_export_okf.py`/`test_okf_server.py` assert artifacts after a plain write, update them to expect none until `wiki_export_okf`.)

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/server.py tests/test_export_only_artifacts.py
git commit -m "feat(server): index.md/log.md are export-only (wiki_export_okf), not per-write"
```

---

### Task 7: Version bump, full verification, B acceptance

**Files:**
- Modify: `pyproject.toml` (`version`), `src/iwiki_mcp/__init__.py` (`__version__`)

**Interfaces:** none.

- [ ] **Step 1: Bump the version**

Set both to `0.6.0`:
- `pyproject.toml`: `version = "0.6.0"`
- `src/iwiki_mcp/__init__.py`: `__version__ = "0.6.0"`

- [ ] **Step 2: Full suite + lint**

Run:

```bash
uv run pytest -q
uv run flake8 src tests
```

Expected: all tests PASS; flake8 clean.

- [ ] **Step 3: B acceptance — confirm the read flow still works (no code change)**

Workstream B is verification only: the two-level flow (seed summary → graph expand → rank sections) is unchanged. Confirm the existing retrieval tests still cover it:

```bash
uv run pytest tests/test_retrieval.py tests/test_retrieval_facets.py -v
```

Expected: PASS — this is the standing acceptance evidence for B. A `kind="summary"` vector still seeds retrieval (asserted by the existing two-level retrieval tests).

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml src/iwiki_mcp/__init__.py
git commit -m "chore: bump 0.5.1 -> 0.6.0 (layout: domain-root store, export-only artifacts)"
```

---

## Self-Review

- **Spec coverage (Unit 1 = C + A + E, plus B acceptance):**
  - A (§A) → Task 1. ✓
  - C store relocation (§C) → Tasks 2-5 (base paths, migration, okf/lint, create-domain, dead guards, tests). ✓
  - C `wiki_create_domain` `.iwiki` site (§C) → Task 4. ✓
  - E export-only (§E) → Task 6. ✓
  - B verification (§B) → Task 7 Step 3. ✓
  - Version 0.6.0 → Task 7. ✓
- **Out of scope here (Unit 2/3):** type-dir identity (D), `hier._adjacency` rewrite (D), the flat→type page move in `wiki_migrate_okf` (D), write-target mode (F). The store-relocation half of migration lands here (Task 2 helper); the page-move half lands in Unit 2.
- **Placeholder scan:** none — every step has concrete code/commands.
- **Type consistency:** `migrate_store_location(base, domain) -> None` used identically in `indexer` and `retrieval`; `index_path`/`log_path` signatures unchanged.
