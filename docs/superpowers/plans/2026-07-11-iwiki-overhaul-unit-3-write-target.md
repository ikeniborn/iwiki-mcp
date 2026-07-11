---
review:
  stage: plan
  plan_hash: e09791f0f93194c9
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
      phase: coverage
      severity: WARNING
      text: "Self-Review no longer overclaims write-tool internal wiring; marked explicit scope boundary."
      verdict: fixed
    - id: F-002
      phase: consistency
      severity: WARNING
      text: "wiki_search write-target selection text aligned to code (bind.write else domains[0])."
      verdict: fixed
    - id: F-003
      phase: verifiability
      severity: WARNING
      text: "Task 14/15 test fixtures made real (_seed_two_level / _bind defined in Global Constraints)."
      verdict: fixed
    - id: F-004
      phase: consistency
      severity: INFO
      text: "Write target run through _validate_domain; intent=write branch placed before empty-scope guard."
      verdict: fixed
result_check:
  verdict: OK
  plan_hash: e09791f0f93194c9
  last_run: 2026-07-11
chain:
  intent: n/a
  spec: docs/superpowers/specs/2026-07-11-iwiki-layout-retrieval-overhaul-design.md
---
# iwiki overhaul — Unit 3: precise write-target search mode (F) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a precise "write-target" retrieval path so an authoring agent can locate the exact article + section to upsert — a higher seed threshold plus an optional exact-heading match — surfaced through `wiki_search(intent="write", heading=...)` and reusable by write tools.

**Architecture:** A new `retrieval.locate_target` reuses the existing two-level pieces (`hier.seed_articles` / `expand_graph` / `rank_sections`) but seeds with `cfg.write_seed_threshold` and, when a heading hint is given, keeps only sections whose heading matches exactly (case-insensitive), returning the single best hit plus an `exists` flag. `wiki_search` gains an `intent` switch that selects this precise branch.

**Tech Stack:** Python 3.10+, `numpy` (already used in `retrieval.py`), stdlib engine core, `pytest`, `flake8` max-line-length 100. Builds on Unit 1 (`migrate_store_location`, domain-root store) and Unit 2 (type/slug identities).

## Global Constraints

- **Depends on Units 1 & 2 merged.**
- Read-path defaults unchanged: `intent="read"` keeps today's hybrid behavior exactly.
- `flake8 src tests` clean; no new runtime deps; tests monkeypatch `indexer.embed_texts` / `retrieval.embed_texts`.
- Version bump this unit: `0.6.1` → `0.6.2` (`pyproject.toml` + `__init__.__version__`).
- **Test harness.** `retrieval.py`-level tests build a two-level store with a deterministic embedder. Define a local helper `_seed_two_level(tmp_path, monkeypatch, dom) -> (cfg, base)` that: sets `IWIKI_LLM_*` env; `monkeypatch.setattr(retrieval, "embed_texts", <deterministic stub>)`; creates `<tmp>/<dom>/`, writes a page whose frontmatter `description` and a `## Purpose` section give distinct vectors under the stub; runs `indexer.index_domain` (embed stub) to populate `index.jsonl`; returns `(Config.load(), str(tmp_path))`. There is NO such helper in the repo today (`tests/test_retrieval.py::_seed` is 2-arg, seeds "a"/"b" with a "## S" section) — author it in the new test file.

---

### Task 13: Add `write_seed_threshold` to `Config`

**Files:**
- Modify: `src/iwiki_mcp/engine/config.py:42-45,71-74`
- Test: `tests/engine/test_config_write_threshold.py` (new)

**Interfaces:**
- Produces: `Config.write_seed_threshold: float` (env `IWIKI_WRITE_SEED_THRESHOLD`, default `0.35` — above the read `seed_threshold` 0.15 for precision).

- [ ] **Step 1: Write the failing test**

```python
import os
from iwiki_mcp.engine.config import Config


def test_write_seed_threshold_default_and_env(monkeypatch):
    monkeypatch.setenv("IWIKI_LLM_BASE_URL", "http://x")
    monkeypatch.setenv("IWIKI_LLM_KEY", "k")
    monkeypatch.delenv("IWIKI_WRITE_SEED_THRESHOLD", raising=False)
    assert Config.load().write_seed_threshold == 0.35
    monkeypatch.setenv("IWIKI_WRITE_SEED_THRESHOLD", "0.5")
    assert Config.load().write_seed_threshold == 0.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/engine/test_config_write_threshold.py -v`
Expected: FAIL (`write_seed_threshold` undefined).

- [ ] **Step 3: Add the field + loader line**

In `src/iwiki_mcp/engine/config.py`, add to the dataclass (after `seed_threshold`, line ~44):

```python
    write_seed_threshold: float = 0.35
```

And in `Config.load()` (after the `seed_threshold=` line, ~73):

```python
            write_seed_threshold=float(getenv("IWIKI_WRITE_SEED_THRESHOLD", "0.35")),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/engine/test_config_write_threshold.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/engine/config.py tests/engine/test_config_write_threshold.py
git commit -m "feat(config): add write_seed_threshold (IWIKI_WRITE_SEED_THRESHOLD, default 0.35)"
```

---

### Task 14: `retrieval.locate_target` — precise single-target locate

**Files:**
- Modify: `src/iwiki_mcp/retrieval.py` — add `locate_target`.
- Test: `tests/test_locate_target.py` (new)

**Interfaces:**
- Consumes: `hier.seed_articles`/`expand_graph`/`rank_sections`, `VectorStore`, `embed_texts`, `base.migrate_store_location`.
- Produces: `retrieval.locate_target(cfg, base, domain, query, heading=None) -> dict` returning `{"domain","file","heading","score","exists": True}` for the best precise hit, or `{"domain","exists": False}` when nothing clears `write_seed_threshold` (or no exact-heading match).

- [ ] **Step 1: Write the failing test**

Follow the fixture style of `tests/test_retrieval.py` (build a domain with `summary`+`section` records via a monkeypatched embedder). Assert:

```python
def test_locate_target_exact_heading(tmp_path, monkeypatch):
    cfg, b = _seed_two_level(tmp_path, monkeypatch, "d")   # local harness (see Global Constraints)
    hit = retrieval.locate_target(cfg, b, "d", "purpose of retrieval", heading="Purpose")
    assert hit["exists"] is True
    assert hit["heading"] == "Purpose"


def test_locate_target_miss_returns_exists_false(tmp_path, monkeypatch):
    cfg, b = _seed_two_level(tmp_path, monkeypatch, "d")
    hit = retrieval.locate_target(cfg, b, "d", "totally unrelated", heading="Nonexistent")
    assert hit["exists"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_locate_target.py -v`
Expected: FAIL (`locate_target` undefined).

- [ ] **Step 3: Implement `locate_target`**

Add to `src/iwiki_mcp/retrieval.py` (import `migrate_store_location` from `.base` — already importing `domain_dir, index_path`):

```python
def locate_target(cfg: Config, base: str, domain: str, query: str,
                  heading: str | None = None) -> dict:
    """Precise write-target locate: seed with the higher write_seed_threshold and,
    when a heading hint is given, keep only the exact (case-insensitive) heading
    match. Returns the single best hit with exists=True, else {exists: False}."""
    migrate_store_location(base, domain)
    qv = list(np.asarray(embed_texts(cfg, [query])[0], dtype=np.float32))
    recs = [r for r in VectorStore(index_path(base, domain)).load() if r.dim == len(qv)]
    summ = [r for r in recs if r.kind == "summary"]
    secs = [r for r in recs if r.kind == "section"]
    if not summ or not secs:
        return {"domain": domain, "exists": False}
    seeds = hier.seed_articles(qv, summ, cfg.seed_top_k, cfg.write_seed_threshold)
    if not seeds:
        return {"domain": domain, "exists": False}
    pool = hier.expand_graph([f for f, _ in seeds], domain_dir(base, domain),
                             cfg.graph_depth, cfg.bfs_top_k)
    ranked = hier.rank_sections(qv, secs, pool, cfg.top_k)
    if heading is not None:
        want = heading.strip().lower()
        ranked = [h for h in ranked if h["heading"].lower() == want]
    if not ranked:
        return {"domain": domain, "exists": False}
    best = ranked[0]
    return {"domain": domain, "file": best["file"], "heading": best["heading"],
            "score": best["score"], "exists": True}
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_locate_target.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/retrieval.py tests/test_locate_target.py
git commit -m "feat(retrieval): locate_target precise write-target (write_seed_threshold + exact heading)"
```

---

### Task 15: Surface via `wiki_search(intent="write", heading=None)`

**Files:**
- Modify: `src/iwiki_mcp/server.py` — `wiki_search` (~232-261) gains `intent` + `heading`.
- Test: `tests/test_server_search_write_intent.py` (new)

**Interfaces:**
- Consumes: `retrieval.locate_target`.
- Produces: `wiki_search(..., intent: str = "read", heading: str | None = None)`. `intent="write"` returns `{"target": <locate_target dict>}` scoped to the write-target domain (first in-scope domain, or `bind.write`); `intent="read"` is unchanged (`{"results": [...]}`).

- [ ] **Step 1: Write the failing test**

```python
def test_search_write_intent_returns_single_target(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch, "d")         # local server-bind helper (env+binding+embed+makedirs)
    server.wiki_write_page("d", "retrieval",
                           "# Retrieval\n\n## Purpose\n\nBody.\n", type="architecture")
    res = server.wiki_search("purpose of retrieval", intent="write", heading="Purpose")
    assert "target" in res
    assert res["target"]["exists"] in (True, False)
    # read intent unchanged
    assert "results" in server.wiki_search("retrieval")
```

`_bind` is the server-bind helper used in Units 1-2 (env + `base.resolve_binding` monkeypatch + `indexer.embed_texts` stub + `os.makedirs(tmp_path/dom)`); define it locally in this test file too.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_server_search_write_intent.py -v`
Expected: FAIL (`intent` param unknown / no `target` key).

- [ ] **Step 3: Extend `wiki_search`**

Add `intent: str = "read"` and `heading: str | None = None` as trailing kwargs of `wiki_search` (additive — back-compat preserved). Insert the write branch **immediately after `cfg = Config.load()`** and **before** the `doms = [...]` / `if not doms: return {"results": [], ...}` guard, so an empty read-scope does not short-circuit a write-intent call:

```python
    if intent == "write":
        target = bind.write or (domains[0] if domains else None)
        if not target:
            return {"target": {"exists": False}, "hint": "no write-target domain in scope"}
        target = _validate_domain(target)      # path guards are load-bearing
        return {"target": retrieval.locate_target(cfg, bind.base, target, query, heading)}
```

The write target is the bound write domain (`bind.write`), else the first explicitly-requested `domains[0]` — it is run through `_validate_domain` before `locate_target` builds any filesystem path. Keep the existing read branch (`doms` resolution + `hybrid_search` + `return {"results": results}`) unchanged for `intent == "read"`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_server_search_write_intent.py tests/test_server_search.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/server.py tests/test_server_search_write_intent.py
git commit -m "feat(server): wiki_search intent=write returns a single precise upsert target"
```

---

### Task 16: Docs, version bump, full verification

**Files:**
- Modify: `pyproject.toml`, `src/iwiki_mcp/__init__.py`, `README.md` (+ `docs/README.ru.md` if present), `docs/wiki/retrieval.md`.

**Interfaces:** none.

- [ ] **Step 1: Document the write-target mode**

In `README.md` (and `docs/README.ru.md` if it exists) add `IWIKI_WRITE_SEED_THRESHOLD` to the env reference and a line on `wiki_search(intent="write", heading=...)`. Update `docs/wiki/retrieval.md` with the precise-locate path.

- [ ] **Step 2: Bump version + full suite + lint**

Set `0.6.2` in `pyproject.toml` and `__init__.__version__`. Run:

```bash
uv run pytest -q
uv run flake8 src tests
```

Expected: all PASS; flake8 clean.

- [ ] **Step 3: Live smoke on the real domain**

```bash
uv run iwiki-mcp --help
```

Then, via the MCP client, `wiki_search(query, intent="write", heading=...)` on the `iwiki-mcp` domain returns a single `target` with `exists` set correctly.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml src/iwiki_mcp/__init__.py README.md docs/
git commit -m "docs+chore: document write-target mode; bump 0.6.1 -> 0.6.2"
```

---

## Self-Review

- **Spec coverage (Unit 3 = F):**
  - `write_seed_threshold` config (§F) → Task 13. ✓
  - `locate_target` precise mode: higher threshold + exact heading + `exists` (§F) → Task 14. ✓
  - `wiki_search(intent="write", heading=None)` surface (§F) → Task 15. ✓
  - Reusable by write tools (§F) → `locate_target` is exposed as an importable helper and surfaced via `wiki_search(intent="write")`; the authoring agent uses it to decide create-vs-update. Deeper auto-routing INSIDE `wiki_write_page`/`wiki_update_page` is intentionally NOT force-wired — the handlers keep their current guarded semantics and the capability is made *available*. This is an explicit scope boundary, not a coverage gap. ✓
  - Read-path unchanged (§F) → `intent="read"` default branch untouched (Task 15). ✓
- **Out of scope here:** any change to the two-level read ranking (B verified in Unit 1).
- **Placeholder scan:** none.
- **Type consistency:** `locate_target(cfg, base, domain, query, heading=None) -> dict` defined in Task 14, consumed in Task 15; the `{exists: bool, ...}` shape is asserted in both tasks' tests.
