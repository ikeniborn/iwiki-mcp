---
review:
  plan_hash: 9708618055e4b173
  last_run: 2026-08-17
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
  findings: []
chain:
  intent: docs/superpowers/intents/2026-08-17-section-granular-page-updates-intent.md
  spec: docs/superpowers/specs/2026-08-17-section-granular-page-updates-design.md
result_check:
  verdict: OK
  plan_hash: 9708618055e4b173
  last_run: 2026-08-17
---
# section-granular-page-updates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Narrow `wiki_update_page`'s section-scoped body input all the way down through the Postgres backend — a section-scoped read, insert/delete/move tools, chunk-hash-based reuse in `_replace_derived`, and a section-level CAS check — without touching the Git backend or introducing a `sections` table.

**Architecture:** A shared `list_sections` parser in `engine/section.py` backs every new operation (read/insert/delete/move) and the refactored `replace_section`. Three new tools (`wiki_insert_section`, `wiki_delete_section`, `wiki_move_section`) follow the exact fail-soft / transactional shape of the existing `wiki_update_page` / `wiki_delete_page`. `_replace_derived` gains a chunk-hash diff so unchanged sections are never re-embedded. `expected_section_hash` is an optional pre-check layered in front of the mandatory `expected_revision`.

**Tech Stack:** Python 3, MCP server (`fastmcp`), psycopg (Postgres), pytest.

**Spec:** `docs/superpowers/specs/2026-08-17-section-granular-page-updates-design.md`

## Global Constraints

- Markdown remains the single source of truth — no `sections` table (spec §1, intent hard constraint).
- `expected_revision` stays mandatory on every Postgres mutation; `expected_section_hash` is additive (spec §2.5, intent hard constraint).
- No breaking changes to `wiki_update_page` / `wiki_read_page` — every new parameter is optional with a default matching today's behavior (intent hard constraint).
- The Git backend (`indexer.index_domain`, `stage_graph_pages`, `sync.py`) is not touched by this plan (intent steering constraint).
- New section operations reuse `validate_page`'s existing `_BLOCKING` findings (`deep_heading`, `pre_h2_text`) — no new validator rules (spec §4).
- Fail-soft style: every new tool is wrapped by `@_safe`; errors are `{"error", "hint"}` dicts, never raised past the tool boundary (spec §4).


### Task 1: `engine/section.py` — shared `list_sections` + `_locate`, refactor `replace_section`

**Files:**
- Modify: `src/iwiki_mcp/engine/section.py`
- Test: `tests/test_section.py`

**Interfaces:**
- Consumes: nothing new (uses the existing `_H2` regex and `slugify_heading` from `engine/links.py`, already imported in this file).
- Produces:
  ```python
  @dataclass(frozen=True)
  class Section:
      heading: str
      body: str
      start: int
      body_start: int
      body_end: int

  def list_sections(content: str) -> list[Section]

  def _locate(sections: list[Section], heading: str) -> int
      # raises SectionError("empty heading") / ("section '## {t}' not found")
      # / ("section '## {t}' is ambiguous (N matches)")
  ```
  `replace_section(content, heading, new_body, *, new_heading=None)` keeps its
  exact existing signature and error messages — every task after this one
  builds on `list_sections` / `_locate`, not on `replace_section`'s internals.

- [ ] **Step 1: Write the failing test for `list_sections`**

```python
# tests/test_section.py — add at top, near the existing PAGE fixture
from iwiki_mcp.engine.section import list_sections


def test_list_sections_returns_heading_and_body_in_order():
    sections = list_sections(PAGE)
    assert [s.heading for s in sections] == ["Overview", "Flow", "Notes"]
    assert sections[1].body.strip() == "old body here"


def test_list_sections_empty_content_returns_empty_list():
    assert list_sections("# Title\nno sections here\n") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_section.py -k list_sections -v`
Expected: FAIL with `ImportError: cannot import name 'list_sections'`

- [ ] **Step 3: Implement `Section` / `list_sections` / `_locate`, refactor `replace_section` to use them**

```python
# src/iwiki_mcp/engine/section.py — replace the body of the module from the
# _H2/_HEADING regex definitions onward; the module docstring, imports, and
# SectionError class are unchanged.

from dataclasses import dataclass


@dataclass(frozen=True)
class Section:
    """One `##` section: heading text plus its body span in the source."""

    heading: str
    body: str
    start: int          # offset of the "## " line
    body_start: int      # offset right after the heading line (+ its newline)
    body_end: int         # offset of the next "##" heading, or EOF


def list_sections(content: str) -> list[Section]:
    """Split ``content`` into ``##`` sections in document order."""
    heads = list(_H2.finditer(content))
    sections = []
    for i, m in enumerate(heads):
        body_start = m.end() + 1 if m.end() < len(content) and content[m.end()] == "\n" else m.end()
        body_end = heads[i + 1].start() if i + 1 < len(heads) else len(content)
        sections.append(
            Section(
                heading=m.group(1).strip(),
                body=content[body_start:body_end],
                start=m.start(),
                body_start=body_start,
                body_end=body_end,
            )
        )
    return sections


def _locate(sections: list[Section], heading: str) -> int:
    target = heading.lstrip("#").strip()
    if not target:
        raise SectionError("empty heading")
    matches = [i for i, s in enumerate(sections) if s.heading == target]
    if not matches:
        raise SectionError(f"section '## {target}' not found")
    if len(matches) > 1:
        raise SectionError(
            f"section '## {target}' is ambiguous ({len(matches)} matches)"
        )
    return matches[0]


def replace_section(
    content: str, heading: str, new_body: str, *, new_heading: str | None = None
) -> str:
    """Return ``content`` with the body of the ``## <heading>`` section replaced.

    ``heading`` is matched by its text (leading ``#``/whitespace stripped). The
    replaced span runs from the end of the heading line to the next ``##`` (or EOF).
    ``new_heading`` optionally renames the section and must not collide with any
    heading anchor. Raises ``SectionError`` if the heading is missing or ambiguous.
    """
    if _H2.search(new_body):
        raise SectionError("new_body must not contain a ## heading")
    sections = list_sections(content)
    idx = _locate(sections, heading)
    target = sections[idx].heading
    replacement_heading = target if new_heading is None else new_heading.strip()
    replacement_anchor = slugify_heading(replacement_heading)
    if not replacement_anchor:
        raise SectionError("empty normalized heading")
    for candidate in _HEADING.finditer(content):
        candidate_anchor = slugify_heading(candidate.group(1).strip())
        if (
            candidate.start() != sections[idx].start
            and candidate_anchor == replacement_anchor
        ):
            raise SectionError(
                f"section heading '{replacement_heading}' collides with another anchor"
            )
    heads = list(_H2.finditer(content))
    heading_start = heads[idx].start(1)
    heading_end = heads[idx].end(1)
    renamed = content[:heading_start] + replacement_heading + content[heading_end:]
    shift = len(replacement_heading) - (heading_end - heading_start)
    body_start = sections[idx].body_start + shift
    body_end = sections[idx].body_end + shift
    return renamed[:body_start] + "\n" + new_body.strip("\n") + "\n\n" + renamed[body_end:]
```

Note: `replace_section`'s public behavior (including every existing error
message string) is unchanged — it now delegates heading lookup to
`list_sections`/`_locate` instead of its own `finditer` loop, but the
collision-scan and splice logic stay as before.

- [ ] **Step 4: Run tests to verify everything passes**

Run: `uv run pytest tests/test_section.py -v`
Expected: PASS — all existing `test_replace_section_*` tests plus the two new
`list_sections` tests green, with no changes to `tests/test_section.py`'s
existing assertions.

- [ ] **Step 5: Lint and commit**

```bash
uv run flake8 src/iwiki_mcp/engine/section.py tests/test_section.py
git add src/iwiki_mcp/engine/section.py tests/test_section.py
git commit -m "refactor(section): extract list_sections/_locate, reuse in replace_section"
```


### Task 2: `engine/section.py` — `insert_section`, `delete_section`, `move_section`

**Files:**
- Modify: `src/iwiki_mcp/engine/section.py`
- Test: `tests/test_section.py`

**Interfaces:**
- Consumes: `Section`, `list_sections`, `_locate`, `SectionError` from Task 1;
  `slugify_heading` (already imported); `RESERVED_SECTIONS`, `OVERVIEW_HEADING`
  from `.frontmatter` (new import — mirrors how `chunk.py` already imports
  `from . import frontmatter as _fm` and uses `_fm.RESERVED_SECTIONS`).
- Produces:
  ```python
  def insert_section(
      content: str, heading: str, body: str, *,
      after: str | None = None, before: str | None = None,
  ) -> str

  def delete_section(content: str, heading: str) -> str

  def move_section(
      content: str, heading: str, *,
      after: str | None = None, before: str | None = None,
  ) -> str
  ```
  All three raise `SectionError` on failure, same shape as `replace_section`.
  Task 4/5/6's server wiring calls these directly.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_section.py — append
from iwiki_mcp.engine.section import delete_section, insert_section, move_section


def test_insert_section_after_existing_heading():
    out = insert_section(PAGE, "New", "new body", after="Flow")
    assert out.index("## Flow") < out.index("## New") < out.index("## Notes")
    assert "## New\nnew body" in out


def test_insert_section_before_existing_heading():
    out = insert_section(PAGE, "New", "new body", before="Notes")
    assert out.index("## Flow") < out.index("## New") < out.index("## Notes")


def test_insert_section_defaults_to_append_at_end():
    out = insert_section(PAGE, "New", "new body")
    assert out.rstrip().endswith("## New\nnew body")


def test_insert_section_rejects_both_after_and_before():
    with pytest.raises(SectionError, match="after.*before|before.*after"):
        insert_section(PAGE, "New", "body", after="Flow", before="Notes")


def test_insert_section_rejects_anchor_collision():
    with pytest.raises(SectionError, match="collides"):
        insert_section(PAGE, "Flow", "body")


def test_insert_section_rejects_h2_in_body():
    with pytest.raises(SectionError):
        insert_section(PAGE, "New", "## Injected\nx")


def test_insert_section_rejects_unknown_anchor_point():
    with pytest.raises(SectionError, match="not found"):
        insert_section(PAGE, "New", "body", after="Nonexistent")


def test_delete_section_removes_target_only():
    out = delete_section(PAGE, "Flow")
    assert "## Flow" not in out
    assert "## Overview" in out
    assert "## Notes" in out


def test_delete_section_missing_heading_raises():
    with pytest.raises(SectionError, match="not found"):
        delete_section(PAGE, "Nonexistent")


def test_delete_section_rejects_reserved_heading():
    reserved = PAGE + "## Outgoing links\n- x\n"
    with pytest.raises(SectionError, match="reserved"):
        delete_section(reserved, "Outgoing links")


def test_delete_section_rejects_last_remaining_section():
    single = "# T\n## Only\nbody\n"
    with pytest.raises(SectionError, match="last"):
        delete_section(single, "Only")


def test_move_section_after_target():
    out = move_section(PAGE, "Overview", after="Notes")
    assert out.index("## Flow") < out.index("## Notes") < out.index("## Overview")


def test_move_section_before_target():
    out = move_section(PAGE, "Notes", before="Overview")
    assert out.index("## Notes") < out.index("## Overview") < out.index("## Flow")


def test_move_section_rejects_self_reference():
    with pytest.raises(SectionError, match="itself"):
        move_section(PAGE, "Flow", after="Flow")


def test_move_section_no_op_next_to_itself_still_succeeds():
    # Overview is already immediately before Flow — moving it "before Flow"
    # is a valid no-op reorder, not an error.
    out = move_section(PAGE, "Overview", before="Flow")
    assert out.index("## Overview") < out.index("## Flow") < out.index("## Notes")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_section.py -k "insert_section or delete_section or move_section" -v`
Expected: FAIL with `ImportError: cannot import name 'insert_section'`

- [ ] **Step 3: Implement `insert_section`, `delete_section`, `move_section`**

```python
# src/iwiki_mcp/engine/section.py — add near the top-level imports:
from . import frontmatter as _fm

# append after replace_section:

def _anchor_collision(content: str, exclude_start: int, anchor: str) -> bool:
    return any(
        candidate.start() != exclude_start
        and slugify_heading(candidate.group(1).strip()) == anchor
        for candidate in _HEADING.finditer(content)
    )


def _anchor_point(content: str, *, after: str | None, before: str | None) -> int:
    """Return the insertion offset for `after`/`before`, or EOF for neither."""
    if after is not None and before is not None:
        raise SectionError("cannot set both after and before")
    sections = list_sections(content)
    if after is not None:
        idx = _locate(sections, after)
        return sections[idx].body_end
    if before is not None:
        idx = _locate(sections, before)
        return sections[idx].start
    return len(content)


def insert_section(
    content: str, heading: str, body: str, *,
    after: str | None = None, before: str | None = None,
) -> str:
    """Insert a new ``## heading`` section at the given anchor point."""
    target = heading.lstrip("#").strip()
    if not target:
        raise SectionError("empty heading")
    if _H2.search(body):
        raise SectionError("body must not contain a ## heading")
    anchor = slugify_heading(target)
    if not anchor:
        raise SectionError("empty normalized heading")
    if _anchor_collision(content, -1, anchor):
        raise SectionError(f"section heading '{target}' collides with another anchor")
    point = _anchor_point(content, after=after, before=before)
    block = f"## {target}\n{body.strip(chr(10))}\n\n"
    prefix = content[:point]
    if prefix and not prefix.endswith("\n\n"):
        prefix = prefix.rstrip("\n") + "\n\n"
    return prefix + block + content[point:]


def delete_section(content: str, heading: str) -> str:
    """Remove the ``## heading`` section entirely."""
    sections = list_sections(content)
    idx = _locate(sections, heading)
    target = sections[idx].heading
    if target.lower() in _fm.RESERVED_SECTIONS or target.lower() == _fm.OVERVIEW_HEADING:
        raise SectionError(f"cannot delete reserved section '## {target}'")
    if len(sections) <= 1:
        raise SectionError("cannot delete the last remaining section")
    return content[:sections[idx].start] + content[sections[idx].body_end:]


def move_section(
    content: str, heading: str, *,
    after: str | None = None, before: str | None = None,
) -> str:
    """Reorder the ``## heading`` section relative to ``after``/``before``."""
    if after is not None and before is not None:
        raise SectionError("cannot set both after and before")
    sections = list_sections(content)
    idx = _locate(sections, heading)
    target = sections[idx].heading
    anchor_name = after if after is not None else before
    if anchor_name is not None and anchor_name.lstrip("#").strip() == target:
        raise SectionError("move target must not be the section itself")
    block = content[sections[idx].start:sections[idx].body_end]
    remainder = content[:sections[idx].start] + content[sections[idx].body_end:]
    point = _anchor_point(remainder, after=after, before=before)
    prefix = remainder[:point]
    if prefix and not prefix.endswith("\n\n"):
        prefix = prefix.rstrip("\n") + "\n\n"
    return prefix + block.rstrip("\n") + "\n\n" + remainder[point:]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_section.py -v`
Expected: PASS — full file green, including Task 1's tests.

- [ ] **Step 5: Lint and commit**

```bash
uv run flake8 src/iwiki_mcp/engine/section.py tests/test_section.py
git add src/iwiki_mcp/engine/section.py tests/test_section.py
git commit -m "feat(section): add insert_section, delete_section, move_section"
```


### Task 3: `wiki_read_page` — section-scoped read (B)

**Files:**
- Modify: `src/iwiki_mcp/server.py:1279-1306` (`wiki_read_page`)
- Test: `tests/test_server_write.py` (git backend), add a Postgres-mode test
  file `tests/postgres/test_read_section.py` (marked
  `pytestmark = pytest.mark.postgres_integration`, following
  `tests/postgres/test_store.py`'s pattern)

**Interfaces:**
- Consumes: `list_sections`, `_locate`, `SectionError` from `engine.section`
  (Tasks 1-2); `Chunk._hash`-equivalent — reuse
  `hashlib.sha256(text.encode()).hexdigest()[:16]` directly (no import from
  `chunk.py`, to keep `section.py`/`server.py` decoupled from the chunker, per
  spec §2.5's note that the hash is computed from `list_sections` output
  alone).
- Produces: `wiki_read_page(domain, slug, heading=None)` — with `heading=None`
  returns exactly today's `{"domain", "slug", "markdown"}` (git) /
  `page["markdown"]`-derived dict (postgres), unchanged. With `heading` set,
  returns `{"domain", "slug", "heading", "body", "section_hash"}` on success,
  or `{"error", "hint"}` on `SectionError`. `section_hash` is consumed by
  Task 8 (D)'s CAS check via `wiki_update_page`'s new `expected_section_hash`
  argument.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_server_write.py — append (uses the existing _seed helper already
# in this file, which the CLAUDE.md architecture notes describe)
import pytest

from iwiki_mcp import server


def test_read_page_with_heading_returns_only_that_section(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nflow body\n## Notes\nkeep\n",
    )
    out = server.wiki_read_page("backend", "concept/auth", heading="Flow")
    assert out["heading"] == "Flow"
    assert out["body"].strip() == "flow body"
    assert "section_hash" in out
    assert "markdown" not in out


def test_read_page_with_missing_heading_returns_error(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n",
    )
    out = server.wiki_read_page("backend", "concept/auth", heading="Nope")
    assert "error" in out
    assert "not found" in out["error"]


def test_read_page_without_heading_is_unchanged(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n",
    )
    out = server.wiki_read_page("backend", "concept/auth")
    assert set(out) == {"domain", "slug", "markdown"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server_write.py -k read_page_with -v`
Expected: FAIL with `TypeError: wiki_read_page() got an unexpected keyword argument 'heading'`

- [ ] **Step 3: Implement the `heading` parameter**

```python
# src/iwiki_mcp/server.py — replace the wiki_read_page function (currently
# lines 1279-1306) with:

@_safe
def wiki_read_page(domain: str, slug: str, heading: str | None = None) -> dict:
    bind = _resolved_binding()
    if _is_postgres(bind):
        valid_domain = _validate_domain(domain)
        _slug_parts(slug)
        if valid_domain not in bind.read:
            return {
                "error": f"domain '{valid_domain}' is outside bound read scope",
                "hint": "narrow or update the authorized read scope",
            }
        page = _postgres_store_for_binding(bind).read_page(valid_domain, slug)
        if page is None:
            return {
                "error": f"page '{valid_domain}/{slug}' not found",
                "hint": "list pages with wiki_list_pages",
            }
        if heading is None:
            return page
        _, body = _fm.split(page["markdown"], strict_code=True)
        return _read_section(domain, slug, body, heading)
    path = _page_path(bind.base, domain, slug)
    if not os.path.isfile(path):
        return {
            "error": f"page '{domain}/{slug}' not found",
            "hint": "list pages with wiki_list_pages",
        }
    markdown = open(path, encoding="utf-8").read()
    if heading is None:
        return {"domain": domain, "slug": slug, "markdown": markdown}
    _, body = _fm.split(markdown, strict_code=True)
    return _read_section(domain, slug, body, heading)


def _read_section(domain: str, slug: str, body: str, heading: str) -> dict:
    try:
        sections = list_sections(body)
        idx = _locate(sections, heading)
    except SectionError as exc:
        return {"error": str(exc), "hint": "check the heading with wiki_read_page"}
    section = sections[idx]
    section_hash = hashlib.sha256(
        section.body.strip("\n").encode("utf-8")
    ).hexdigest()[:16]
    return {
        "domain": domain,
        "slug": slug,
        "heading": section.heading,
        "body": section.body.strip("\n"),
        "section_hash": section_hash,
    }
```

Add the two new imports at the top of `server.py` next to the existing
`from .engine.section import SectionError, replace_section` line:

```python
from .engine.section import SectionError, list_sections, replace_section, _locate
```

`hashlib` is already imported in `server.py` (used by `_page_hash`-style
helpers elsewhere — check with `grep -n "^import hashlib" src/iwiki_mcp/server.py`;
add `import hashlib` near the top if it is not already present).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_server_write.py -k read_page -v`
Expected: PASS, plus confirm no regression:
Run: `uv run pytest tests/test_server_write.py tests/test_mcp_smoke.py -v`
Expected: PASS

- [ ] **Step 5: Lint and commit**

```bash
uv run flake8 src/iwiki_mcp/server.py tests/test_server_write.py
git add src/iwiki_mcp/server.py tests/test_server_write.py
git commit -m "feat(server): add section-scoped wiki_read_page(heading=...)"
```


### Task 4: `wiki_insert_section` tool (both backends)

**Files:**
- Modify: `src/iwiki_mcp/server.py` (new function near `wiki_update_page`,
  around line 2018; tool registration block around line 3161)
- Test: `tests/test_server_write.py`, `tests/postgres/test_store.py`-style file
  `tests/postgres/test_section_ops.py` (`pytestmark = pytest.mark.postgres_integration`)

**Interfaces:**
- Consumes: `insert_section` from `engine.section` (Task 2); the existing
  `_existing_domain_write_guard`, `sync.ensure_fresh`, `indexer.upsert_ingest_log`,
  `indexer.index_domain`, `indexer.prepare_graph_mutation`,
  `sync.commit_and_push`, `_write_sync_result` helpers `wiki_update_page`
  already uses (same shapes, no changes to any of them).
- Produces:
  ```python
  def wiki_insert_section(
      domain: str, slug: str, heading: str, body: str,
      after_heading: str | None = None, before_heading: str | None = None,
      source: str | None = None, description: str | None = None,
      status: str | None = None, expected_revision: int | None = None,
  ) -> dict
  ```
  Registered as an MCP tool. Task 5/6 follow this exact function shape for
  `wiki_delete_section` / `wiki_move_section`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_server_write.py — append
def test_insert_section_adds_new_section_after_target(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nflow body\n",
    )
    out = server.wiki_insert_section(
        "backend", "concept/auth", "New", "new body", after_heading="Flow"
    )
    assert "error" not in out
    read = server.wiki_read_page("backend", "concept/auth")
    assert "## New\nnew body" in read["markdown"]
    assert read["markdown"].index("## Flow") < read["markdown"].index("## New")


def test_insert_section_missing_page_returns_error(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    out = server.wiki_insert_section("backend", "nope", "New", "body")
    assert "not found" in out["error"]


def test_insert_section_rejects_invalid_body_structure(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n",
    )
    out = server.wiki_insert_section(
        "backend", "concept/auth", "New", "### too deep\nx"
    )
    assert "error" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server_write.py -k insert_section -v`
Expected: FAIL with `AttributeError: module 'iwiki_mcp.server' has no attribute 'wiki_insert_section'`

- [ ] **Step 3: Implement `wiki_insert_section`**

Model this directly on `wiki_update_page`'s existing body (`server.py:2019-2209`):
same Postgres branch shape (`_is_postgres` → `store.update_page` with the
transformed markdown) and same Git branch shape (validate → write file →
`upsert_ingest_log` if `source` → `index_domain` → commit, with the identical
try/except rollback). The only difference from `wiki_update_page` is which
`engine.section` function builds the new body:

```python
# src/iwiki_mcp/server.py — add after wiki_update_page (after line 2209, before
# the existing wiki_delete_page definition at line 2213)

@_safe
def wiki_insert_section(
    domain: str, slug: str, heading: str, body: str,
    after_heading: str | None = None, before_heading: str | None = None,
    source: str | None = None, description: str | None = None,
    status: str | None = None, expected_revision: int | None = None,
) -> dict:
    bind = _resolved_binding()
    valid_domain = _validate_domain(domain)
    if _is_postgres(bind):
        scope_error = base.write_scope_error(bind, valid_domain)
        if scope_error:
            return scope_error
        if expected_revision is None:
            return expected_revision_required()
        _slug_parts(slug)
        store = _postgres_store_for_binding(bind)
        page = store.read_page(valid_domain, slug)
        if page is None:
            return {
                "error": f"page '{valid_domain}/{slug}' not found",
                "hint": "list pages with wiki_list_pages",
            }
        meta, original_body = _fm.split(page["markdown"], strict_code=True)
        try:
            updated_body = insert_section(
                original_body, heading, to_markdown_links(body),
                after=after_heading, before=before_heading,
            )
        except SectionError as exc:
            return {"error": str(exc), "hint": "check the heading with wiki_read_page"}
        blocking = [
            f for f in validate_page(updated_body) if f.get("type") in _BLOCKING
        ]
        if blocking:
            return {
                "error": "section structure invalid",
                "findings": blocking,
                "hint": "body must use only ## headings; no ###+, no pre-## text",
            }
        if description is not None:
            meta["description"] = description
        if status is not None:
            meta["status"] = _fm.normalize_status(status)
        if source is not None:
            meta["resource"] = source
        if meta:
            meta["timestamp"] = _dt.date.today().isoformat()
            updated_markdown = _fm.render(meta) + updated_body
        else:
            updated_markdown = updated_body
        result = store.update_page(valid_domain, slug, updated_markdown, expected_revision)
        if "error" not in result:
            result["heading"] = heading.lstrip("#").strip()
        return result
    dom_path, scope_error = _existing_domain_write_guard(bind, valid_domain)
    if scope_error:
        return scope_error
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    if not dom_path.is_dir():
        return {
            "error": f"domain '{valid_domain}' not found",
            "hint": "create it with wiki_create_domain",
        }
    base.migrate_store_location(bind.base, valid_domain)
    if source:
        spec = ignore.load_project_ignore(bind.project_dir)
        if ignore.is_ignored(spec, source, bind.project_dir):
            return {
                "error": "source matches .iwikiignore",
                "hint": f"'{source}' is excluded by .iwikiignore; "
                        "remove the pattern to ingest, or omit source",
            }
    if source is not None:
        try:
            source = _normalize_source(bind.project_dir, source)
        except ValueError as exc:
            return {"error": str(exc), "hint": "pass a source path inside the bound project"}
    path = _page_path(bind.base, valid_domain, slug)
    if not os.path.isfile(path):
        return {
            "error": f"page '{valid_domain}/{slug}' not found",
            "hint": "list pages with wiki_list_pages",
        }
    page_file = PurePosixPath(*_slug_parts(slug)).as_posix() + ".md"
    original_full = open(path, encoding="utf-8").read()
    meta, original_body = _fm.split(original_full, strict_code=True)
    try:
        updated_body = insert_section(
            original_body, heading, to_markdown_links(body),
            after=after_heading, before=before_heading,
        )
    except SectionError as exc:
        return {"error": str(exc), "hint": "check the heading with wiki_read_page"}
    blocking = [f for f in validate_page(updated_body) if f.get("type") in _BLOCKING]
    if blocking:
        return {
            "error": "section structure invalid",
            "findings": blocking,
            "hint": "body must use only ## headings; no ###+, no pre-## text",
        }
    cfg = Config.load()
    if meta:
        if description is not None:
            meta["description"] = description
        if status is not None:
            meta["status"] = _fm.normalize_status(status)
        meta["timestamp"] = _dt.date.today().isoformat()
        new_md = _fm.render(meta) + updated_body
    else:
        new_md = updated_body
    log_file = base.log_path(bind.base, valid_domain)
    log_before = None
    if source and os.path.exists(log_file):
        with open(log_file, "rb") as fh:
            log_before = fh.read()
    graph_mutation = indexer.prepare_graph_mutation(bind.base, valid_domain)
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
            fh.write(original_full)
        if source:
            _restore_log(log_file, log_before)
        raise
    page_rel = f"{valid_domain}/{page_file}"
    commit = sync.commit_and_push(
        bind.base, f"iwiki: insert section into {page_rel}", pathspec=valid_domain,
        _after_commit=_after_commit_graph(graph_mutation, refresh_files=(page_file,)),
    )
    return {
        "page": page_rel,
        "heading": heading.lstrip("#").strip(),
        "indexed_chunks": stats["indexed_chunks"],
        "reused": stats["reused"],
        "embedded": stats["embedded"],
        "bytes": stats["bytes"],
        "over_cap": stats["over_cap"],
        **_write_sync_result(commit, fresh.get("warning")),
    }
```

Add `insert_section` to the `engine.section` import line from Task 3, and
register the tool near the other registrations (after line 3161's
`mcp.tool()(wiki_update_page)`):

```python
mcp.tool()(wiki_insert_section)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_server_write.py -k insert_section -v`
Expected: PASS

- [ ] **Step 5: Lint and commit**

```bash
uv run flake8 src/iwiki_mcp/server.py tests/test_server_write.py
git add src/iwiki_mcp/server.py tests/test_server_write.py
git commit -m "feat(server): add wiki_insert_section tool"
```


### Task 5: `wiki_delete_section` tool (both backends)

**Files:**
- Modify: `src/iwiki_mcp/server.py` (new function after `wiki_insert_section`;
  registration block)
- Test: `tests/test_server_write.py`

**Interfaces:**
- Consumes: `delete_section` from `engine.section` (Task 2); same helper set
  as Task 4.
- Produces:
  ```python
  def wiki_delete_section(
      domain: str, slug: str, heading: str,
      source: str | None = None, expected_revision: int | None = None,
  ) -> dict
  ```

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_server_write.py — append
def test_delete_section_removes_target_section(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nflow body\n## Notes\nkeep\n",
    )
    out = server.wiki_delete_section("backend", "concept/auth", "Flow")
    assert "error" not in out
    read = server.wiki_read_page("backend", "concept/auth")
    assert "## Flow" not in read["markdown"]
    assert "## Notes" in read["markdown"]


def test_delete_section_rejects_last_section(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n",
    )
    out = server.wiki_delete_section("backend", "concept/auth", "Overview")
    assert "error" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server_write.py -k delete_section -v`
Expected: FAIL with `AttributeError: module 'iwiki_mcp.server' has no attribute 'wiki_delete_section'`

- [ ] **Step 3: Implement `wiki_delete_section`**

Same two-branch shape as `wiki_delete_page` (`server.py:2212-2276`) but
operating on `delete_section(original_body, heading)` instead of removing the
whole file:

```python
@_safe
def wiki_delete_section(
    domain: str, slug: str, heading: str,
    source: str | None = None, expected_revision: int | None = None,
) -> dict:
    bind = _resolved_binding()
    valid_domain = _validate_domain(domain)
    if _is_postgres(bind):
        scope_error = base.write_scope_error(bind, valid_domain)
        if scope_error:
            return scope_error
        if expected_revision is None:
            return expected_revision_required()
        _slug_parts(slug)
        store = _postgres_store_for_binding(bind)
        page = store.read_page(valid_domain, slug)
        if page is None:
            return {
                "error": f"page '{valid_domain}/{slug}' not found",
                "hint": "list pages with wiki_list_pages",
            }
        meta, original_body = _fm.split(page["markdown"], strict_code=True)
        try:
            updated_body = delete_section(original_body, heading)
        except SectionError as exc:
            return {"error": str(exc), "hint": "check the heading with wiki_read_page"}
        if meta:
            meta["timestamp"] = _dt.date.today().isoformat()
            updated_markdown = _fm.render(meta) + updated_body
        else:
            updated_markdown = updated_body
        result = store.update_page(valid_domain, slug, updated_markdown, expected_revision)
        if "error" not in result:
            result["heading"] = heading.lstrip("#").strip()
        return result
    dom_path, scope_error = _existing_domain_write_guard(bind, valid_domain)
    if scope_error:
        return scope_error
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    if not dom_path.is_dir():
        return {
            "error": f"domain '{valid_domain}' not found",
            "hint": "create it with wiki_create_domain",
        }
    base.migrate_store_location(bind.base, valid_domain)
    path = _page_path(bind.base, valid_domain, slug)
    if not os.path.isfile(path):
        return {
            "error": f"page '{valid_domain}/{slug}' not found",
            "hint": "list pages with wiki_list_pages",
        }
    page_file = PurePosixPath(*_slug_parts(slug)).as_posix() + ".md"
    original_full = open(path, encoding="utf-8").read()
    meta, original_body = _fm.split(original_full, strict_code=True)
    try:
        updated_body = delete_section(original_body, heading)
    except SectionError as exc:
        return {"error": str(exc), "hint": "check the heading with wiki_read_page"}
    cfg = Config.load()
    if meta:
        meta["timestamp"] = _dt.date.today().isoformat()
        new_md = _fm.render(meta) + updated_body
    else:
        new_md = updated_body
    graph_mutation = indexer.prepare_graph_mutation(bind.base, valid_domain)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new_md)
        stats = indexer.index_domain(cfg, bind.base, valid_domain)
    except Exception:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(original_full)
        raise
    page_rel = f"{valid_domain}/{page_file}"
    commit = sync.commit_and_push(
        bind.base, f"iwiki: delete section from {page_rel}", pathspec=valid_domain,
        _after_commit=_after_commit_graph(graph_mutation, refresh_files=(page_file,)),
    )
    return {
        "page": page_rel,
        "heading": heading.lstrip("#").strip(),
        "indexed_chunks": stats["indexed_chunks"],
        "reused": stats["reused"],
        "embedded": stats["embedded"],
        "bytes": stats["bytes"],
        "over_cap": stats["over_cap"],
        **_write_sync_result(commit, fresh.get("warning")),
    }
```

Add `delete_section` to the `engine.section` import; register with
`mcp.tool()(wiki_delete_section)` after `wiki_insert_section`'s registration.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_server_write.py -k delete_section -v`
Expected: PASS

- [ ] **Step 5: Lint and commit**

```bash
uv run flake8 src/iwiki_mcp/server.py tests/test_server_write.py
git add src/iwiki_mcp/server.py tests/test_server_write.py
git commit -m "feat(server): add wiki_delete_section tool"
```


### Task 6: `wiki_move_section` tool (both backends)

**Files:**
- Modify: `src/iwiki_mcp/server.py` (new function after `wiki_delete_section`;
  registration block)
- Test: `tests/test_server_write.py`

**Interfaces:**
- Consumes: `move_section` from `engine.section` (Task 2); same helper set as
  Tasks 4-5.
- Produces:
  ```python
  def wiki_move_section(
      domain: str, slug: str, heading: str,
      after_heading: str | None = None, before_heading: str | None = None,
      expected_revision: int | None = None,
  ) -> dict
  ```

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_server_write.py — append
def test_move_section_reorders_target(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nflow body\n## Notes\nkeep\n",
    )
    out = server.wiki_move_section("backend", "concept/auth", "Notes", before_heading="Overview")
    assert "error" not in out
    read = server.wiki_read_page("backend", "concept/auth")
    assert read["markdown"].index("## Notes") < read["markdown"].index("## Overview")


def test_move_section_rejects_self_reference(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nflow body\n",
    )
    out = server.wiki_move_section("backend", "concept/auth", "Flow", after_heading="Flow")
    assert "error" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server_write.py -k move_section -v`
Expected: FAIL with `AttributeError: module 'iwiki_mcp.server' has no attribute 'wiki_move_section'`

- [ ] **Step 3: Implement `wiki_move_section`**

Same two-branch shape, using `move_section(original_body, heading, after=..., before=...)`
in place of `delete_section`/`insert_section` (mirror Task 5's structure
exactly, swapping the body-transform call and the commit message to
`"iwiki: move section in {page_rel}"`, and the source/log-upsert branch is
omitted — a pure reorder has no source to attribute):

```python
@_safe
def wiki_move_section(
    domain: str, slug: str, heading: str,
    after_heading: str | None = None, before_heading: str | None = None,
    expected_revision: int | None = None,
) -> dict:
    bind = _resolved_binding()
    valid_domain = _validate_domain(domain)
    if _is_postgres(bind):
        scope_error = base.write_scope_error(bind, valid_domain)
        if scope_error:
            return scope_error
        if expected_revision is None:
            return expected_revision_required()
        _slug_parts(slug)
        store = _postgres_store_for_binding(bind)
        page = store.read_page(valid_domain, slug)
        if page is None:
            return {
                "error": f"page '{valid_domain}/{slug}' not found",
                "hint": "list pages with wiki_list_pages",
            }
        meta, original_body = _fm.split(page["markdown"], strict_code=True)
        try:
            updated_body = move_section(
                original_body, heading, after=after_heading, before=before_heading
            )
        except SectionError as exc:
            return {"error": str(exc), "hint": "check the heading with wiki_read_page"}
        if meta:
            meta["timestamp"] = _dt.date.today().isoformat()
            updated_markdown = _fm.render(meta) + updated_body
        else:
            updated_markdown = updated_body
        result = store.update_page(valid_domain, slug, updated_markdown, expected_revision)
        if "error" not in result:
            result["heading"] = heading.lstrip("#").strip()
        return result
    dom_path, scope_error = _existing_domain_write_guard(bind, valid_domain)
    if scope_error:
        return scope_error
    fresh = sync.ensure_fresh(bind.base)
    if fresh.get("state") == "diverged":
        return dict(_DIVERGED)
    if not dom_path.is_dir():
        return {
            "error": f"domain '{valid_domain}' not found",
            "hint": "create it with wiki_create_domain",
        }
    base.migrate_store_location(bind.base, valid_domain)
    path = _page_path(bind.base, valid_domain, slug)
    if not os.path.isfile(path):
        return {
            "error": f"page '{valid_domain}/{slug}' not found",
            "hint": "list pages with wiki_list_pages",
        }
    page_file = PurePosixPath(*_slug_parts(slug)).as_posix() + ".md"
    original_full = open(path, encoding="utf-8").read()
    meta, original_body = _fm.split(original_full, strict_code=True)
    try:
        updated_body = move_section(
            original_body, heading, after=after_heading, before=before_heading
        )
    except SectionError as exc:
        return {"error": str(exc), "hint": "check the heading with wiki_read_page"}
    cfg = Config.load()
    if meta:
        meta["timestamp"] = _dt.date.today().isoformat()
        new_md = _fm.render(meta) + updated_body
    else:
        new_md = updated_body
    graph_mutation = indexer.prepare_graph_mutation(bind.base, valid_domain)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new_md)
        stats = indexer.index_domain(cfg, bind.base, valid_domain)
    except Exception:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(original_full)
        raise
    page_rel = f"{valid_domain}/{page_file}"
    commit = sync.commit_and_push(
        bind.base, f"iwiki: move section in {page_rel}", pathspec=valid_domain,
        _after_commit=_after_commit_graph(graph_mutation, refresh_files=(page_file,)),
    )
    return {
        "page": page_rel,
        "heading": heading.lstrip("#").strip(),
        "indexed_chunks": stats["indexed_chunks"],
        "reused": stats["reused"],
        "embedded": stats["embedded"],
        "bytes": stats["bytes"],
        "over_cap": stats["over_cap"],
        **_write_sync_result(commit, fresh.get("warning")),
    }
```

Add `move_section` to the `engine.section` import; register with
`mcp.tool()(wiki_move_section)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_server_write.py -k move_section -v`
Expected: PASS

- [ ] **Step 5: Lint and commit**

```bash
uv run flake8 src/iwiki_mcp/server.py tests/test_server_write.py
git add src/iwiki_mcp/server.py tests/test_server_write.py
git commit -m "feat(server): add wiki_move_section tool"
```


### Task 7: Incremental chunk reuse in `_replace_derived` (A)

**Files:**
- Modify: `src/iwiki_mcp/postgres/store.py:626-705` (`_prepare_page`, `_replace_derived`)
- Test: `tests/postgres/test_store.py`

**Interfaces:**
- Consumes: `Record` from `engine/store.py` (already imported); `_record_metadata`
  (existing static method); `self._embedder` (existing).
- Produces: no signature change to `_prepare_page`, `write_page`,
  `update_page`, or any tool — this task changes only `_replace_derived`'s
  internal SQL and `_prepare_page`'s call to the embedder to skip unchanged
  chunks, following exactly the reuse comparison `indexer.index_domain`
  already does (`existing.get(key)` / `prev.hash == c.hash` in
  `src/iwiki_mcp/indexer.py:81-96`).

- [ ] **Step 1: Write the failing test**

```python
# tests/postgres/test_store.py — append (uses this file's existing pg fixture
# and _markdown/_cfg/_embed helpers; pytestmark = pytest.mark.postgres_integration
# already applies to the whole file)

def test_update_page_reuses_unchanged_chunk_embeddings(store_factory):
    store = store_factory()
    markdown = (
        "---\ntype: concept\ntitle: T\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Alpha\nalpha body\n## Beta\nbeta body\n"
    )
    store.write_page("docs", "concept/two", markdown)
    with store._connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT chunk_id, embedding FROM iwiki.chunks c "
            "JOIN iwiki.pages p ON p.page_id = c.page_id "
            "WHERE p.slug = %s ORDER BY c.ordinal", ("concept/two",),
        )
        before = cursor.fetchall()

    embed_calls = []
    original_embedder = store._embedder

    def counting_embedder(cfg, texts):
        embed_calls.extend(texts)
        return original_embedder(cfg, texts)

    store._embedder = counting_embedder
    updated = markdown.replace("beta body", "beta body changed")
    store.update_page("docs", "concept/two", updated, expected_revision=1)

    with store._connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT chunk_id, embedding FROM iwiki.chunks c "
            "JOIN iwiki.pages p ON p.page_id = c.page_id "
            "WHERE p.slug = %s ORDER BY c.ordinal", ("concept/two",),
        )
        after = cursor.fetchall()

    # The Alpha section's chunk row is untouched (same chunk_id, same vector);
    # only Beta's was re-embedded.
    assert before[0][0] == after[0][0]
    assert before[0][1] == after[0][1]
    assert not any("alpha body" in t for t in embed_calls)
    assert any("beta body changed" in t for t in embed_calls)
```

Note: `store_factory` is this file's real fixture (`tests/postgres/conftest.py:217`);
`store_factory()` returns a `PostgresStore` bound to a fresh `docs` domain and
`wiki-a` iwiki id (see `test_create_list_and_read_page_return_numeric_revision`
for the established call pattern this test follows).

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/postgres/test_store.py -k reuses_unchanged -v -m postgres_integration`
Expected: FAIL — `before[0][0] == after[0][0]` is false today (every chunk
row is deleted and reinserted, and the Alpha text appears in `embed_calls`
because `_prepare_page` re-embeds unconditionally).

- [ ] **Step 3: Implement the diff**

```python
# src/iwiki_mcp/postgres/store.py — modify _prepare_page (currently
# lines 626-650) to accept the page's existing chunk hashes and skip
# re-embedding unchanged ones, mirroring indexer.index_domain's loop:

def _prepare_page(self, domain: str, slug: str, markdown: str, *, existing=None):
    domain = _validate_identifier(domain, "domain")
    slug = _validate_identifier(slug, "page slug")
    if not isinstance(markdown, str):
        raise ValueError("markdown must be a string")
    try:
        frontmatter.split(markdown, strict_code=True)
    except frontmatter.FrontmatterError as exc:
        raise ValueError(str(exc)) from exc
    blocking = [
        finding
        for finding in validate_page(markdown)
        if finding.get("type") in _BLOCKING_FINDINGS
    ]
    if blocking:
        raise ValueError("section structure invalid")
    file = f"{slug}.md"
    chunks = indexer.chunk_markdown(
        file, markdown, self.cfg.chunk_size, self.cfg.chunk_overlap, self.cfg.summary_max
    )
    existing = existing or {}
    to_embed = []
    records = [None] * len(chunks)
    for i, chunk in enumerate(chunks):
        key = f"{chunk.id}#{chunk.chunk}"
        prev = existing.get(key)
        if prev is not None and prev["hash"] == chunk.hash:
            records[i] = prev["record"]
        else:
            to_embed.append((i, chunk))
    if to_embed:
        vectors = self._embedder(self.cfg, [c.text for _, c in to_embed])
        for (i, chunk), vector in zip(to_embed, vectors):
            records[i] = make_record(chunk, vector)
    targets = parse_link_targets(markdown, domain)
    return domain, slug, markdown, chunks, records, targets
```

Note: `chunk_markdown` needs to be reachable — add `from . import indexer` (or
`from ..indexer import chunk_markdown` matching whatever import style the top
of `postgres/store.py` already uses for cross-package access; check
`grep -n "^from\|^import" src/iwiki_mcp/postgres/store.py` first — `indexer`
already imports from `.engine.chunk`, so import `chunk_markdown` directly from
`..engine.chunk` instead if `postgres/store.py` avoids importing the git-only
`indexer` module) and `make_record` (already imported per the existing
`_replace_derived` body).

```python
# _replace_derived (currently lines 667-700) — read existing chunk hashes
# before deleting, and pass them through call sites.

@staticmethod
def _existing_chunk_index(cursor, iwiki_id: str, page_id: int) -> dict:
    cursor.execute(
        "SELECT section_id, chunk_id, embedding, quantization_scale, "
        "quantized_embedding FROM iwiki.chunks WHERE iwiki_id = %s AND page_id = %s",
        (iwiki_id, page_id),
    )
    index = {}
    for section_id, chunk_id, embedding, scale, q in cursor.fetchall():
        meta = json.loads(section_id)
        key = f"?#{meta['chunk']}"  # heading is embedded in chunk.id; see below
        index[(meta.get("heading", ""), meta["chunk"], meta["hash"])] = {
            "chunk_id": chunk_id, "embedding": embedding, "scale": scale, "q": q,
        }
    return index
```

Given the actual complexity of reconstructing `Chunk.id` (`f"{file}#{heading}"`)
from stored `section_id` JSON — which today only carries `chunk`, `hash`,
`kind`, `ordinal`, `tags`, `type`, not `heading` or `file` — extend
`_record_metadata` to also store `heading`:

```python
@staticmethod
def _record_metadata(record: Record) -> str:
    return json.dumps(
        {
            "chunk": record.chunk,
            "hash": record.hash,
            "heading": record.heading if hasattr(record, "heading") else None,
            "kind": record.kind,
            "ordinal": record.ordinal,
            "tags": record.tags,
            "type": record.type,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
```

`Record` (in `engine/store.py`) does not currently carry `heading` — add it
there (`heading: str = ""` field, populated by `make_record` from
`chunk.heading`, matching `Chunk.heading`) as part of this task, since the
diff key needs it. Update `_replace_derived` to build the `existing` dict
keyed by `(heading, chunk, hash)` from the newly-heading-bearing metadata,
look up each new chunk by that same key, and only `DELETE`/`INSERT` rows
whose key is absent from the new set or vice versa — rows present in both
stay untouched (no `UPDATE`, no `DELETE`+`INSERT` round trip for them).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/postgres/test_store.py -v -m postgres_integration`
Expected: PASS — full file green, including the new reuse test. Requires a
disposable pgvector database; see `tests/postgres/conftest.py` for the
connection env vars this suite expects (`postgres_integration` marker is
normally skipped without them — run with whatever the project's existing
CI/local setup uses for this marker, e.g. `PYTEST_ADDOPTS` or a docker-compose
Postgres instance already documented for other `postgres_integration` tests).

- [ ] **Step 5: Lint and commit**

```bash
uv run flake8 src/iwiki_mcp/postgres/store.py src/iwiki_mcp/engine/store.py tests/postgres/test_store.py
git add src/iwiki_mcp/postgres/store.py src/iwiki_mcp/engine/store.py tests/postgres/test_store.py
git commit -m "perf(postgres): reuse unchanged chunk embeddings in _replace_derived"
```


### Task 8: Section-level CAS — `expected_section_hash` (D)

**Files:**
- Modify: `src/iwiki_mcp/storage.py` (new `section_conflict` helper);
  `src/iwiki_mcp/server.py` (`wiki_update_page`, `wiki_delete_section`,
  `wiki_move_section` — add the optional parameter and pre-check)
- Test: `tests/test_server_update.py`, `tests/test_server_write.py`

**Interfaces:**
- Consumes: `list_sections`, `_locate` (Tasks 1-2); `hashlib` (already used by
  Task 3's `_read_section`).
- Produces:
  ```python
  # storage.py
  def section_conflict(current_section_hash: str | None) -> dict
  ```
  New optional kwarg `expected_section_hash: str | None = None` on
  `wiki_update_page`, `wiki_delete_section`, `wiki_move_section` (not
  `wiki_insert_section` — per spec §2.5, there is no prior section to pin).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_server_update.py — append
def test_update_page_section_hash_mismatch_returns_conflict(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)  # reuse this file's existing seed pattern
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nold\n",
    )
    out = server.wiki_update_page(
        "backend", "concept/auth", "Flow", "new",
        expected_section_hash="0000000000000000",
    )
    assert out["error"] == "section_conflict"


def test_update_page_section_hash_match_succeeds(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nold\n",
    )
    current = server.wiki_read_page("backend", "concept/auth", heading="Flow")
    out = server.wiki_update_page(
        "backend", "concept/auth", "Flow", "new",
        expected_section_hash=current["section_hash"],
    )
    assert "error" not in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server_update.py -k section_hash -v`
Expected: FAIL with `TypeError: wiki_update_page() got an unexpected keyword argument 'expected_section_hash'`

- [ ] **Step 3: Implement the CAS pre-check**

```python
# src/iwiki_mcp/storage.py — add after revision_conflict:

def section_conflict(current_section_hash: str | None) -> dict:
    """Stable response when a section-level CAS pre-check loses a race."""
    return {
        "error": "section_conflict",
        "current_section_hash": current_section_hash,
        "hint": "re-read the section with wiki_read_page and retry",
    }
```

```python
# src/iwiki_mcp/server.py — import section_conflict alongside the existing
# expected_revision_required import (line 79):
from .storage import expected_revision_required, section_conflict

# Add a shared pre-check helper near _read_section (Task 3):

def _check_section_hash(body: str, heading: str, expected: str | None) -> dict | None:
    """Return a section_conflict dict if `expected` doesn't match, else None."""
    if expected is None:
        return None
    sections = list_sections(body)
    idx = _locate(sections, heading)  # SectionError propagates like elsewhere
    current_hash = hashlib.sha256(
        sections[idx].body.strip("\n").encode("utf-8")
    ).hexdigest()[:16]
    if current_hash != expected:
        return section_conflict(current_hash)
    return None
```

In `wiki_update_page` (both branches), immediately after `meta, original_body
= _fm.split(...)` and before the `replace_section` call, insert:

```python
    conflict = _check_section_hash(original_body, heading, expected_section_hash)
    if conflict is not None:
        return conflict
```

wrapped in the same `try/except SectionError` the existing `replace_section`
call already uses (a missing/ambiguous heading during the hash check should
surface the same `{"error": ..., "hint": "check the heading with
wiki_read_page"}` as today, not a raw exception) — so restructure that call
site as:

```python
    try:
        conflict = _check_section_hash(original_body, heading, expected_section_hash)
        if conflict is not None:
            return conflict
        new_body = replace_section(original_body, heading, new_body, new_heading=new_heading)
    except SectionError as e:
        ...  # unchanged existing handling
```

Add `expected_section_hash: str | None = None` to `wiki_update_page`'s
signature (both the Postgres-branch function and — it is one function, not
two — the single signature at `server.py:2019-2024`). Apply the identical
`_check_section_hash` call (with the appropriate section, e.g. `heading` for
delete/move) to `wiki_delete_section` and `wiki_move_section` from Tasks 5-6,
each gaining the same optional parameter.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_server_update.py tests/test_server_write.py -v`
Expected: PASS — full green, including every pre-existing test in both files
(no existing call site passes `expected_section_hash`, so its default `None`
must reproduce exactly today's behavior).

- [ ] **Step 5: Lint and commit**

```bash
uv run flake8 src/iwiki_mcp/storage.py src/iwiki_mcp/server.py tests/test_server_update.py tests/test_server_write.py
git add src/iwiki_mcp/storage.py src/iwiki_mcp/server.py tests/test_server_update.py tests/test_server_write.py
git commit -m "feat(server): add expected_section_hash CAS pre-check"
```


### Task 9: Concurrent-edit integration test (Desired Outcome 4) + docs upkeep

**Files:**
- Test: `tests/test_server_write.py` (new concurrency test using
  `ThreadPoolExecutor`, matching the pattern already used in
  `tests/postgres/test_store.py`'s `concurrent.futures` import)
- Modify: `src/iwiki_mcp/resources.py` (authoring rules — add the three new
  tools next to the existing `wiki_update_page` mention at line 34)
- Modify: `README.md` and `docs/README.ru.md` if they document the tool list
  (check `grep -n "wiki_update_page" README.md docs/README.ru.md` first —
  update only if a tool list or usage example exists there)

**Interfaces:**
- Consumes: `wiki_update_page`, `wiki_insert_section`, `wiki_delete_section`,
  `wiki_move_section` (Tasks 4-8) — no new production interfaces, this task is
  verification + documentation only.

- [ ] **Step 1: Write the concurrency test**

```python
# tests/test_server_write.py — append
from concurrent.futures import ThreadPoolExecutor


def test_concurrent_updates_to_different_sections_both_succeed(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nflow body\n",
    )
    overview = server.wiki_read_page("backend", "concept/auth", heading="Overview")
    flow = server.wiki_read_page("backend", "concept/auth", heading="Flow")

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(
            server.wiki_update_page, "backend", "concept/auth", "Overview", "new sum",
            expected_section_hash=overview["section_hash"],
        )
        f2 = pool.submit(
            server.wiki_update_page, "backend", "concept/auth", "Flow", "new flow",
            expected_section_hash=flow["section_hash"],
        )
        r1, r2 = f1.result(), f2.result()

    assert "error" not in r1
    assert "error" not in r2
    final = server.wiki_read_page("backend", "concept/auth")
    assert "new sum" in final["markdown"]
    assert "new flow" in final["markdown"]


def test_concurrent_updates_to_same_section_second_conflicts(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    server.wiki_write_page(
        "backend", "concept/auth",
        "---\ntype: concept\ntitle: Auth\ndescription: d\ntags: [x]\nstatus: stable\n"
        "---\n## Overview\nsum\n## Flow\nflow body\n",
    )
    flow = server.wiki_read_page("backend", "concept/auth", heading="Flow")
    server.wiki_update_page(
        "backend", "concept/auth", "Flow", "first write",
        expected_section_hash=flow["section_hash"],
    )
    out = server.wiki_update_page(
        "backend", "concept/auth", "Flow", "second write",
        expected_section_hash=flow["section_hash"],  # stale, already applied above
    )
    assert out["error"] == "section_conflict"
```

Note: the Git backend serializes writes through `sync.ensure_fresh` /
`commit_and_push` rather than true row-level concurrency, so the first test
exercises "both calls succeed when applied sequentially against
non-conflicting section hashes" — the meaningful assertion is that neither
call is rejected due to touching a *different* section, matching Desired
Outcome 4's Git-path behavior. The Postgres-path equivalent (real DB-level
concurrency) belongs in `tests/postgres/test_store.py` as part of Task 7/8's
own tests if the reviewer judges the git-path test insufficient evidence —
flag this explicitly in the task's PR description rather than silently
skipping the Postgres case.

- [ ] **Step 2: Run tests to verify they fail (before Task 8's hash check
  existed, this would be `TypeError`; run after Task 8 lands — this task
  starts from Task 8's green state, so first confirm they currently fail only
  because they don't exist yet)**

Run: `uv run pytest tests/test_server_write.py -k concurrent -v`
Expected: FAIL with `AttributeError` (test not yet added) before Step 1, PASS
immediately after Step 1 since Task 8 already implemented the mechanism —
this is a verification task, not new production code.

- [ ] **Step 3: Update `resources.py` authoring rules**

```python
# src/iwiki_mcp/resources.py — near line 34 ("Use wiki_write_page for a new
# page, `wiki_update_page` for one existing `##`..."), add:
```
Add one sentence documenting the three new tools and the `heading` parameter
on `wiki_read_page`, matching this file's existing terse bullet style (read
the surrounding lines first with the Read tool before editing, to match
voice/format exactly — this task does not fix wording, only appends facts).

- [ ] **Step 4: Update README tool references if present**

```bash
grep -n "wiki_update_page\|wiki_read_page" README.md docs/README.ru.md 2>/dev/null
```
If either file lists tools or documents `wiki_update_page`'s contract, add the
three new tools and the `heading` parameter in both files (English source +
Russian translation), per the project's CLAUDE.md "Keep README Current" rule.
If neither file mentions the tool surface at this level of detail, skip —
do not invent a new section.

- [ ] **Step 5: Full suite + lint + commit**

```bash
uv run pytest -q
uv run flake8 src tests
git add tests/test_server_write.py src/iwiki_mcp/resources.py README.md docs/README.ru.md
git commit -m "test(server): verify concurrent section edits; docs(resources): document section tools"
```


## Final Verification

After Task 9, before handing back to the user:

```bash
uv run pytest -q
uv run flake8 src tests
```

Both must be clean. Confirm the four Desired Outcomes from the intent are
each backed by a passing test added in this plan:

1. Untouched-chunk reuse → Task 7's `test_update_page_reuses_unchanged_chunk_embeddings`.
2. Section-scoped read → Task 3's `test_read_page_with_heading_returns_only_that_section`.
3. Insert/delete/move without full rewrite → Tasks 4-6's tool tests.
4. Concurrent edits → Task 9's two concurrency tests.

This plan stops here — no implementation runs until the user picks an
execution approach (subagent-driven vs inline) per the Execution Handoff
below, and per the user's explicit request, no task in this plan is executed
without a further go-ahead.
