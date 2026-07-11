---
review:
  stage: plan
  plan_hash: 11fe8da06c61e900
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
      text: "Type value now validated as a safe single path segment in _resolve_identity (+test)."
      verdict: fixed
    - id: F-002
      phase: dependencies
      severity: WARNING
      text: "wiki_apply_okf not-found guard runs on current path BEFORE move_page."
      verdict: fixed
    - id: F-003
      phase: dependencies
      severity: WARNING
      text: "migrate_layout sequenced AFTER autonomous frontmatter-adoption loop (one-pass completeness)."
      verdict: fixed
    - id: F-004
      phase: consistency
      severity: WARNING
      text: "wiki_write_page de-dup wording corrected (remove OLD later build_frontmatter/Config.load)."
      verdict: fixed
    - id: F-005
      phase: consistency
      severity: WARNING
      text: "Plan-mode 'no writes' contract reconciled: deterministic layout applied, no LLM adoption."
      verdict: fixed
chain:
  intent: n/a
  spec: docs/superpowers/specs/2026-07-11-iwiki-layout-retrieval-overhaul-design.md
---
# iwiki overhaul — Unit 2: type-grouping directories (D) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a page's identity its domain-relative `<type>/<slug>` path — pages live at `<base>/<domain>/<type>/<slug>.md`, the frontmatter `type` equals the first path segment — and migrate existing flat domains into that layout, keeping the wiki-link graph and retrieval intact.

**Architecture:** A single `_resolve_identity` helper turns a write's `(slug, type)` into the canonical `<type>/<slug>` identity; `wiki_write_page`/`wiki_apply_okf` place files there. `hier._adjacency` is rewritten to walk the nested tree (`rglob`) keyed by domain-relative paths so cross-type links keep forming graph edges. `wiki_migrate_okf` gains a deterministic layout pass that moves each flat page under its type dir and rewrites intra-domain link targets.

**Tech Stack:** Python 3.10+, stdlib-only engine core, `pytest`, `flake8` max-line-length 100. Builds on Unit 1 (store/log already at the domain root; no per-write OKF artifacts).

## Global Constraints

- **Depends on Unit 1 merged.** `base.index_path`/`log_path` resolve to the domain root; write handlers no longer call `refresh_artifacts`.
- Page identity == domain-relative path; type == first path segment == frontmatter `type` (normalized, lowercase, open vocab).
- Path-traversal guards (`_slug_parts`, `_validate_domain`, `_contains`) stay load-bearing — keep them before any filesystem join.
- Intra-domain links only (cross-domain links are out of scope, matching authoring rules v1).
- `flake8 src tests` clean; no new runtime deps; tests monkeypatch `indexer.embed_texts`.
- Version bump this unit: `0.6.0` → `0.6.1` (`pyproject.toml` + `__init__.__version__`).
- **Test harness.** Each new test that needs a bound server defines a local helper `_bind(tmp_path, monkeypatch, dom)`: set `IWIKI_LLM_*` env, `monkeypatch.setattr(base, "resolve_binding", lambda project_dir=None: base.Binding(base=str(tmp_path), read=(dom,), write=dom, project_dir=str(tmp_path)))`, `monkeypatch.setattr(indexer, "embed_texts", lambda cfg, t: [[0.0]*cfg.dimensions for _ in t])`, `os.makedirs(tmp_path/dom, exist_ok=True)`. Do NOT call the real `tests/test_server_write.py::_seed` as `_seed(..., "d")` — its 3rd arg is `with_domain: bool` and it seeds `backend`.

---

### Task 8: Rewrite `hier._adjacency` for nested type dirs

**Files:**
- Modify: `src/iwiki_mcp/engine/hier.py:24-42`
- Test: `tests/engine/test_hier_adjacency.py` (new)

**Interfaces:**
- Consumes: `engine.links.parse_links` (unchanged — already normalizes a nested `<type>/<slug>.md#h` target to its `<type>/<slug>` key).
- Produces: `_adjacency(domain_dir) -> dict[str, set[str]]` keyed by domain-relative posix path `<type>/<slug>.md` (undirected).

- [ ] **Step 1: Write the failing test**

```python
from iwiki_mcp.engine.hier import _adjacency


def test_adjacency_crosses_type_dirs(tmp_path):
    (tmp_path / "guide").mkdir()
    (tmp_path / "api").mkdir()
    (tmp_path / "guide" / "a.md").write_text(
        "# A\n\n## S\n\nSee [B](api/b.md#s).\n", encoding="utf-8")
    (tmp_path / "api" / "b.md").write_text("# B\n\n## S\n\nBody.\n", encoding="utf-8")

    adj = _adjacency(str(tmp_path))

    assert "api/b.md" in adj["guide/a.md"]
    assert "guide/a.md" in adj["api/b.md"]   # undirected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/engine/test_hier_adjacency.py -v`
Expected: FAIL — the current flat `os.listdir` sees no top-level `*.md` and never keys by `guide/a.md`.

- [ ] **Step 3: Rewrite `_adjacency`**

Replace `src/iwiki_mcp/engine/hier.py:24-42` with an `rglob` walk keyed by domain-relative posix paths:

```python
def _adjacency(domain_dir: str) -> dict[str, set[str]]:
    """Undirected page graph keyed by domain-relative '<type>/<slug>.md'. An edge
    a->b also adds b->a. Walks the nested type-dir tree; a link target is
    normalized to '<type>/<slug>' by parse_links and matched with a '.md' suffix."""
    from pathlib import Path
    adj: dict[str, set[str]] = {}
    root = Path(domain_dir)
    if not root.is_dir():
        return adj
    for path in root.rglob("*.md"):
        name = path.relative_to(root).as_posix()
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for link in parse_links(content):
            base = link.split("#", 1)[0]
            if not base:
                continue
            tgt = base if base.endswith(".md") else f"{base}.md"
            adj.setdefault(name, set()).add(tgt)
            adj.setdefault(tgt, set()).add(name)
    return adj
```

(`os` is still used elsewhere in the module; keep its import. Add `from pathlib import Path` at module top if not already present, rather than the local import above — match surrounding style.)

- [ ] **Step 4: Run test + the retrieval suite to verify no regression**

Run: `uv run pytest tests/engine/test_hier_adjacency.py tests/test_retrieval.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/engine/hier.py tests/engine/test_hier_adjacency.py
git commit -m "feat(hier): adjacency walks nested type dirs, keyed by domain-relative path"
```

---

### Task 9: `_resolve_identity` + type-dir placement in `wiki_write_page`

**Files:**
- Modify: `src/iwiki_mcp/server.py` — add `_resolve_identity`; restructure `wiki_write_page` (~336-445) to place the page under its type dir.
- Test: `tests/test_write_type_dirs.py` (new)

**Interfaces:**
- Consumes: `okf.build_frontmatter` (returns the fm block containing the resolved `type`), `_slug_parts`, `_page_path`.
- Produces: `_resolve_identity(slug: str, resolved_type: str) -> str` returning the domain-relative identity `<type>/<tail>`; used by `wiki_write_page` and (Task 10) `wiki_apply_okf`.

- [ ] **Step 1: Write the failing tests**

```python
def test_write_places_page_under_type_dir(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch, "d")              # local harness (see Global Constraints)
    res = server.wiki_write_page("d", "retrieval",
                                 "# Retrieval\n\n## Purpose\n\nBody.\n", type="architecture")
    assert res["page"] == "d/architecture/retrieval.md"
    assert (tmp_path / "d" / "architecture" / "retrieval.md").is_file()


def test_write_rejects_type_segment_mismatch(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch, "d")
    res = server.wiki_write_page("d", "guide/retrieval",
                                 "# R\n\n## Purpose\n\nBody.\n", type="architecture")
    assert "error" in res and "type" in res["error"].lower()


def test_write_rejects_unsafe_type_segment(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch, "d")
    res = server.wiki_write_page("d", "retrieval",
                                 "# R\n\n## Purpose\n\nBody.\n", type="a/b")
    assert "error" in res and "type" in res["error"].lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_write_type_dirs.py -v`
Expected: FAIL (page lands at `d/retrieval.md`; mismatch not rejected).

- [ ] **Step 3: Add `_resolve_identity` near `_slug_parts` in `server.py`**

```python
def _resolve_identity(slug: str, resolved_type: str) -> str:
    """Domain-relative identity '<type>/<tail>'. A bare slug is prefixed with the
    resolved type; a slug that already carries a leading segment must match it.
    The resolved type must be a safe SINGLE path segment (guards the invariant
    'first path segment == frontmatter type': normalize_type lowercases but does
    NOT reject '/' or a leading '.', so validate it here)."""
    if (not resolved_type or "/" in resolved_type or "\\" in resolved_type
            or resolved_type.startswith(".")):
        raise ValueError(
            f"invalid frontmatter type '{resolved_type}': must be a safe single "
            "path segment (no '/', '\\', or leading '.')")
    parts = _slug_parts(slug)
    if len(parts) == 1:
        return f"{resolved_type}/{parts[0]}"
    if parts[0] != resolved_type:
        raise ValueError(
            f"slug type-segment '{parts[0]}' does not match frontmatter type "
            f"'{resolved_type}'")
    return PurePosixPath(*parts).as_posix()
```

- [ ] **Step 4: Restructure `wiki_write_page` to build frontmatter first, then place by type**

Reorder the body so the resolved type is known before the path. Replace the block from `path = _page_path(...)` (line ~378) down to the exists check with:

```python
    cfg = Config.load()
    fm_block, fm_warning = okf.build_frontmatter(
        cfg, bind.base, valid_domain, _slug_parts(slug)[-1], markdown,
        source=source, explicit_type=type, explicit_tags=tags,
        explicit_description=description, explicit_status=status,
        timestamp_path=f"{valid_domain}/{slug}.md")
    meta, _ = _fm.split(fm_block)
    resolved_type = meta.get("type")
    try:
        identity = _resolve_identity(slug, resolved_type)
    except ValueError as exc:
        return {"error": str(exc),
                "hint": "pass a bare slug with a matching `type`, or a slug whose "
                        "first segment equals the frontmatter type"}
    page_file = identity + ".md"
    if PurePosixPath(page_file).name in RESERVED_OKF:
        return {
            "error": f"slug tail is reserved for the generated OKF file "
                     f"'{PurePosixPath(page_file).name}'",
            "hint": "choose another slug; index/log are generated, not authored",
        }
    path = _page_path(bind.base, valid_domain, identity)
    if os.path.exists(path):
        return {
            "error": f"page '{valid_domain}/{identity}' exists",
            "hint": "editing an existing page is a guarded op; confirm with the user",
        }
```

Then the write block continues as before, but every later `page_file`/`page_rel`/log/index/commit reference already uses `page_file = identity + ".md"` and `page_rel = f"{valid_domain}/{page_file}"`. `os.makedirs(os.path.dirname(path), exist_ok=True)` (already present) creates the type dir.

**De-dup carefully:** the reordered block above introduces the single `cfg = Config.load()` + `build_frontmatter` call. The ORIGINAL handler has its own `cfg = Config.load()` + `build_frontmatter` LATER in the file (current lines ~394-399) plus the earlier `path`/`page_file` derivation (current ~378-379). Delete the OLD (later, ~394-399) `cfg`/`build_frontmatter` pair and the OLD (~378-379) `path`/`page_file` lines — keep ONLY the new reordered block. After the edit there must be exactly one `build_frontmatter` and one `Config.load()` in the handler; grep the function to confirm.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_write_type_dirs.py tests/test_server_write.py -v`
Expected: PASS. Update any `test_server_write.py` case whose expected `page` was a flat `d/<slug>.md` to the type-prefixed identity.

- [ ] **Step 6: Commit**

```bash
git add src/iwiki_mcp/server.py tests/test_write_type_dirs.py tests/test_server_write.py
git commit -m "feat(server): wiki_write_page places pages under <type>/<slug>; identity=type/slug"
```

---

### Task 10: Type placement + move-on-type-change in `wiki_apply_okf`; `move_page` + link rewrite

**Files:**
- Modify: `src/iwiki_mcp/engine/links.py` — add `rewrite_link_targets`.
- Modify: `src/iwiki_mcp/okf.py` — add `move_page`.
- Modify: `src/iwiki_mcp/server.py` — `wiki_apply_okf` (~905-955) uses identity + moves on type change.
- Test: `tests/test_links_rewrite.py`, `tests/test_apply_move.py` (new)

**Interfaces:**
- Produces:
  - `links.rewrite_link_targets(body: str, mapping: dict[str, str]) -> str` — rewrite markdown + legacy link targets whose slug is a key of `mapping` to its value, code-safe. Idempotent when no key matches.
  - `okf.move_page(base_dir: str, domain: str, old_identity: str, new_identity: str) -> None` — rename the file and rewrite every intra-domain link `old_identity` -> `new_identity` across the domain's pages. Best-effort on link rewrite; the rename is authoritative.

- [ ] **Step 1: Write the failing tests**

```python
from iwiki_mcp.engine.links import rewrite_link_targets


def test_rewrite_link_targets_markdown_and_legacy():
    body = "See [A](alpha.md#s) and [[alpha#S]] and `alpha.md`.\n"
    out = rewrite_link_targets(body, {"alpha": "concept/alpha"})
    assert "(concept/alpha.md#s)" in out
    assert "[[concept/alpha#S]]" in out
    assert "`alpha.md`" in out          # code span untouched


def test_rewrite_is_noop_without_match():
    body = "See [B](beta.md).\n"
    assert rewrite_link_targets(body, {"alpha": "concept/alpha"}) == body
```

```python
def test_apply_moves_page_on_type_change(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch, "d")
    server.wiki_write_page("d", "x", "# X\n\n## Purpose\n\nBody.\n", type="concept")
    # a sibling links to it
    server.wiki_write_page("d", "y", "# Y\n\n## Purpose\n\nSee [X](concept/x.md).\n", type="guide")
    res = server.wiki_apply_okf("d", "concept/x", type="architecture")
    assert res["page"] == "d/architecture/x.md"
    assert (tmp_path / "d" / "architecture" / "x.md").is_file()
    assert not (tmp_path / "d" / "concept" / "x.md").exists()
    y = (tmp_path / "d" / "guide" / "y.md").read_text()
    assert "(architecture/x.md)" in y      # inbound link rewritten
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_links_rewrite.py tests/test_apply_move.py -v`
Expected: FAIL (`rewrite_link_targets`/`move_page` undefined; apply does not move).

- [ ] **Step 3: Implement `rewrite_link_targets` in `links.py`**

Reuse the masking pattern already in `to_markdown_links` so code is untouched:

```python
def rewrite_link_targets(body: str, mapping: dict[str, str]) -> str:
    """Rewrite markdown ([t](slug.md#a)) and legacy ([[slug#H|a]]) link targets
    whose slug is a key of `mapping` to its mapped slug, leaving code and text
    untouched. Idempotent when nothing matches."""
    if not mapping:
        return body
    masks: list[str] = []

    def _mask(m: "re.Match") -> str:
        masks.append(m.group(0))
        return f"\x00{len(masks) - 1}\x00"

    masked = _INLINE.sub(_mask, _FENCE.sub(_mask, body))

    def _md(m: "re.Match") -> str:
        if m.group(1):                      # image
            return m.group(0)
        target = m.group(2)
        path, sep, anchor = target.partition("#")
        clean = path[2:] if path.startswith("./") else path
        slug = clean[:-3] if clean.endswith(".md") else clean
        if slug in mapping:
            return m.group(0).replace(target, f"{mapping[slug]}.md{sep}{anchor}")
        return m.group(0)

    def _legacy(m: "re.Match") -> str:
        inner = m.group(1)
        slug, sep, rest = inner.partition("#")
        s = slug.strip()
        base = s[:-3] if s.endswith(".md") else s
        if base in mapping:
            return m.group(0).replace(inner, f"{mapping[base]}{sep}{rest}", 1)
        return m.group(0)

    out = _MD_LINK.sub(_md, masked)
    out = _LINK.sub(_legacy, out)

    def _restore(m: "re.Match") -> str:
        i = int(m.group(1))
        return masks[i] if i < len(masks) else m.group(0)

    return re.sub(r"\x00(\d+)\x00", _restore, out)
```

- [ ] **Step 4: Implement `okf.move_page`**

```python
def move_page(base_dir, domain, old_identity: str, new_identity: str) -> None:
    """Rename <domain>/<old_identity>.md to <new_identity>.md and rewrite every
    intra-domain link old_identity -> new_identity across the domain's pages.
    No-op when old == new. The rename is authoritative; link rewrite is best-effort."""
    from .engine.links import rewrite_link_targets
    if old_identity == new_identity:
        return
    dom = Path(base_dir) / domain
    old_p = dom / f"{old_identity}.md"
    new_p = dom / f"{new_identity}.md"
    new_p.parent.mkdir(parents=True, exist_ok=True)
    os.replace(old_p, new_p)
    mapping = {old_identity: new_identity}
    for slug in _page_slugs(dom):
        p = dom / f"{slug}.md"
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        new_text = rewrite_link_targets(text, mapping)
        if new_text != text:
            p.write_text(new_text, encoding="utf-8")
```

Add `import os` if not already imported in `okf.py` (it imports `subprocess`, `json`; add `os`).

- [ ] **Step 5: Wire `wiki_apply_okf` to identity + move**

In `wiki_apply_okf` (server.py ~905): after resolving `valid_domain`, compute the current identity from `slug` and the new identity from the requested `type`; if they differ, `okf.move_page` first, then operate on the new path:

```python
    cfg = Config.load()
    current_identity = PurePosixPath(*_slug_parts(slug)).as_posix()
    current_path = _page_path(bind.base, valid_domain, current_identity)
    # not-found guard MUST run on the CURRENT path BEFORE move_page: os.replace on a
    # missing source raises FileNotFoundError -> @_safe generic error, losing the
    # friendly "page not found" hint.
    if not os.path.isfile(current_path):
        return {"error": f"page '{valid_domain}/{current_identity}' not found",
                "hint": "list pages with wiki_list_pages"}
    new_identity = _resolve_identity(_slug_parts(slug)[-1], _fm.normalize_type(type))
    if current_identity != new_identity:
        okf.move_page(bind.base, valid_domain, current_identity, new_identity)
    identity = new_identity
    page_file = identity + ".md"
    path = _page_path(bind.base, valid_domain, identity)
```

Replace the existing "page not found" check and the later `page_file`/`path` derivations in the handler with the block above, and set `page_rel = f"{valid_domain}/{page_file}"`. Note the existing rollback (`server.py:941-943`) rewrites `original` to the new `path` on failure but does NOT undo a completed `move_page`; on a rewrite/index failure after a move, the file stays at the new identity with the original bytes — acceptable (structure is valid, next index picks it up), but leave a code comment noting it.

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_links_rewrite.py tests/test_apply_move.py tests/test_okf_server.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/iwiki_mcp/engine/links.py src/iwiki_mcp/okf.py src/iwiki_mcp/server.py tests/test_links_rewrite.py tests/test_apply_move.py
git commit -m "feat(okf): move_page + link rewrite; wiki_apply_okf moves file on type change"
```

---

### Task 11: Flat→type layout migration in `wiki_migrate_okf`

**Files:**
- Modify: `src/iwiki_mcp/okf.py` — add `migrate_layout`.
- Modify: `src/iwiki_mcp/server.py` — `wiki_migrate_okf` (~830) runs the layout pass first.
- Test: `tests/test_migrate_layout.py` (new)

**Interfaces:**
- Consumes: `okf.move_page`, `base.migrate_store_location` (Unit 1).
- Produces: `okf.migrate_layout(base_dir, domain) -> dict` returning `{"moved": [<old->new>...]}`; moves each flat, frontmatter-typed page under its type dir and relocates the store.

- [ ] **Step 1: Write the failing test**

```python
def test_migrate_layout_moves_flat_pages_and_rewrites_links(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch, "d")
    dom = tmp_path / "d"
    (dom / "alpha.md").write_text(
        "---\ntype: concept\ntitle: Alpha\n---\n# Alpha\n\n## S\n\nBody.\n", encoding="utf-8")
    (dom / "beta.md").write_text(
        "---\ntype: guide\ntitle: Beta\n---\n# Beta\n\n## S\n\nSee [A](alpha.md#s).\n",
        encoding="utf-8")

    res = server.wiki_migrate_okf("d")

    assert (dom / "concept" / "alpha.md").is_file()
    assert (dom / "guide" / "beta.md").is_file()
    assert not (dom / "alpha.md").exists()
    assert "(concept/alpha.md#s)" in (dom / "guide" / "beta.md").read_text()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_migrate_layout.py -v`
Expected: FAIL (pages stay flat).

- [ ] **Step 3: Implement `okf.migrate_layout`**

```python
def migrate_layout(base_dir, domain) -> dict:
    """Deterministic flat->type layout migration. For each page whose identity has
    no type segment (a bare '<slug>.md' at the domain root) and carries a
    frontmatter 'type', move it under '<type>/<slug>.md' and rewrite intra-domain
    links. Also relocates the store/log to the domain root. Idempotent."""
    from . import base as _base
    _base.migrate_store_location(base_dir, domain)
    dom = Path(base_dir) / domain
    moved = []
    for slug in _page_slugs(dom):
        if "/" in slug:                     # already under a type dir
            continue
        text = (dom / f"{slug}.md").read_text(encoding="utf-8")
        meta, _ = fm.split(text)
        ptype = meta.get("type")
        if not ptype:
            continue
        new_identity = f"{fm.normalize_type(ptype)}/{slug}"
        move_page(base_dir, domain, slug, new_identity)
        moved.append(f"{slug} -> {new_identity}")
    return {"moved": moved}
```

- [ ] **Step 4: Sequence `migrate_layout` correctly in both modes**

Ordering matters: `migrate_layout` only moves pages that ALREADY carry a frontmatter
`type` (`if not ptype: continue`). In autonomous mode the adoption loop (server.py
848-864) is what ADDS `type` to flat pages, so the layout move must run **after** that
loop — otherwise newly-typed pages stay flat and need a second `wiki_migrate_okf`.

- **Autonomous mode (`cfg.chat_model`):** keep the adoption loop as-is, then, immediately
  before `stats = indexer.index_domain(...)` (line ~865), insert:
  ```python
      layout = okf.migrate_layout(bind.base, target)
  ```
  Thread `"moved": layout["moved"]` into the result dict.
- **Plan mode (no LLM writes):** the deterministic layout migration is NOT an LLM write —
  redefine plan mode as "no LLM frontmatter *adoption* (candidates are proposed), but the
  deterministic `<type>/<slug>` layout move + store relocation ARE applied". Before building
  `candidates`, insert:
  ```python
      layout = okf.migrate_layout(bind.base, target)
      indexer.index_domain(cfg, bind.base, target)   # store reflects moved paths
  ```
  Add `"moved": layout["moved"]` to the plan-mode result and update the handler's docstring/
  return note so the "no writes" contract now reads "no LLM writes; deterministic layout is
  applied".

Pages still lacking a `type` are untouched by `migrate_layout` in both modes (they stay flat until frontmatter is adopted), so the pass is safe and idempotent.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_migrate_layout.py tests/test_server_migrate.py -v`
Expected: PASS. Update `test_server_migrate.py` fixtures/expectations for the type-dir layout.

- [ ] **Step 6: Commit**

```bash
git add src/iwiki_mcp/okf.py src/iwiki_mcp/server.py tests/test_migrate_layout.py tests/test_server_migrate.py
git commit -m "feat(okf): wiki_migrate_okf moves flat pages under type dirs + relocates store"
```

---

### Task 12: Confirm read/update/delete + lint cross-refs on type/slug; version bump

**Files:**
- Modify: `pyproject.toml`, `src/iwiki_mcp/__init__.py`
- Test: `tests/test_type_dir_roundtrip.py` (new)

**Interfaces:** none new — this task proves `_slug_parts`-based addressing already handles nested identities.

- [ ] **Step 1: Write the round-trip test**

```python
def test_read_update_delete_by_type_slug(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch, "d")
    server.wiki_write_page("d", "cfg", "# Cfg\n\n## Purpose\n\nBody.\n", type="reference")
    assert server.wiki_read_page("d", "reference/cfg")["page"].endswith("reference/cfg.md")
    server.wiki_update_page("d", "reference/cfg", "Purpose", "New body.\n")
    assert "New body." in server.wiki_read_page("d", "reference/cfg")["markdown"]
    server.wiki_delete_page("d", "reference/cfg")
    assert "error" in server.wiki_read_page("d", "reference/cfg")
```

- [ ] **Step 2: Run it (should pass without server changes)**

Run: `uv run pytest tests/test_type_dir_roundtrip.py -v`
Expected: PASS — `_slug_parts`/`_page_path` already join nested identities. If a lint cross-reference test fails because it resolves bare slugs, adjust the lint fixture to type/slug identities (no code change expected; `parse_links` already yields `<type>/<slug>` keys).

- [ ] **Step 3: Bump version + full suite**

Set `0.6.1` in `pyproject.toml` and `__init__.__version__`. Run:

```bash
uv run pytest -q
uv run flake8 src tests
```

Expected: all PASS; flake8 clean.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml src/iwiki_mcp/__init__.py tests/test_type_dir_roundtrip.py
git commit -m "chore: bump 0.6.0 -> 0.6.1 (type-dir layout); prove type/slug read/update/delete"
```

---

## Self-Review

- **Spec coverage (Unit 2 = D):**
  - Identity = type/slug, type == first segment (§D, Page identity rule) → Tasks 9, 10 (`_resolve_identity`). ✓
  - Write-tool placement + mismatch reject (§D) → Task 9. ✓
  - Type value validated as a safe single path segment (§D "reject `.`-prefix, `/`, `..`") → Task 9 `_resolve_identity` guard + `test_write_rejects_unsafe_type_segment`. ✓
  - `hier._adjacency` nested rewrite (§D "BREAKS") → Task 8. ✓
  - Links `<type>/<slug>.md` (§D) → `parse_links` unchanged (Task 8) + `rewrite_link_targets` (Task 10). ✓
  - Type change moves file + rewrites inbound links (§D) → Task 10. ✓
  - Migration flat→type + intra-domain link rewrite (§D, Migration) → Task 11. ✓
  - read/update/delete by type/slug (§D addressing) → Task 12. ✓
- **Out of scope here:** write-target precise mode (F → Unit 3); auto-move on `update_page` (explicitly excluded).
- **Placeholder scan:** none.
- **Type consistency:** `_resolve_identity(slug, resolved_type) -> str` used in Tasks 9 & 10; `move_page(base_dir, domain, old_identity, new_identity)` used in Tasks 10 & 11; `rewrite_link_targets(body, mapping)` used in Task 10 & inside `move_page`.
