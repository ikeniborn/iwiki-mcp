---
review:
  stage: plan
  plan_hash: be541bffa8ee5b7c
  last_run: 2026-07-10
  chain:
    intent: n/a
    spec: 037b7a2df1fbfedb
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
      section: "## Task 7: Wiki upkeep (iwiki MCP tools)"
      section_hash: 3de34fff66f7123f
      fragment: "Task 7 edits internal wiki pages the spec's Files-touched never lists."
      text: >-
        Task 7 edits internal wiki pages (okf-governance.md, indexing.md) that the
        spec's "Files touched" never lists and that no requirement covers. It is
        work beyond the spec scope, driven by a project convention (CLAUDE.md
        docs-upkeep rule) rather than the spec.
      fix: >-
        Accept as an intentional project-convention step (CLAUDE.md mandates wiki
        upkeep) — explicitly out-of-spec, traceable to the convention, not a gap.
      verdict: open
      verdict_at: null
    - id: F-002
      phase: verifiability
      severity: WARNING
      section: "## Task 7: Wiki upkeep (iwiki MCP tools)"
      section_hash: 3de34fff66f7123f
      fragment: "wiki new_body given only as an angle-bracket content sketch, no exact text."
      text: >-
        Task 7 Steps 1-2 specify the wiki body only as an angle-bracket content
        sketch, with no exact text and no run command; only Step 3 (wiki_lint)
        loosely verifies. Weak definition of done.
      fix: >-
        Acceptable for wiki authoring via MCP tools (content is composed at write
        time); wiki_lint (Step 3) is the objective gate. Optionally pin the exact
        section bodies before writing.
      verdict: open
      verdict_at: null
    - id: F-003
      phase: verifiability
      severity: WARNING
      section: "## Task 6: Search/graph integration + docs + version"
      section_hash: 7427779a557115eb
      fragment: "Adjust the section_id to whichever the graph resolves ..."
      text: >-
        The SC-3 integration test's exact assertion (which section_id wiki_related
        resolves, reserved node vs indexed alice.md#role) is left runtime-resolved,
        so the step's PASS condition is under-determined until executed.
      fix: >-
        During execution, pin the expected wiki_related resolution (assert against
        alice.md#role) so the expected output is deterministic. Non-blocking: the
        invariant (reserved prose not indexed; link in graph) stays testable.
      verdict: open
      verdict_at: null
    - id: F-004
      phase: consistency
      severity: INFO
      section: "## Task 1: Frontmatter schema primitives"
      section_hash: 118d686d1b82f39f
      fragment: "src/iwiki_mcp/engine/classify.py line 54"
      text: >-
        The plan modifies classify.py (coerce_type -> normalize_type), a file the
        spec's "Files touched" omits. The change is a required consequence of R-03
        removing coerce_type (classify.py:54 is a live call site), so justified but
        not enumerated by the spec.
      fix: >-
        None required for the build; optionally add classify.py to the spec's
        files-touched for traceability.
      verdict: open
      verdict_at: null
    - id: F-005
      phase: consistency
      severity: INFO
      section: "## Task 1: Frontmatter schema primitives"
      section_hash: 118d686d1b82f39f
      fragment: 'OVERVIEW_HEADING kept in frontmatter.py; keep-in-sync comment goes stale.'
      text: >-
        Tasks 2 and 3 remove OVERVIEW_HEADING from chunk.py and validate.py, but
        frontmatter.py keeps its copy (correctly — derive_description uses it). Its
        "keep in sync with chunk.OVERVIEW_HEADING" comment becomes stale once the
        peers are deleted. Not a build break.
      fix: >-
        Optionally update that comment when editing frontmatter.py in Task 1.
      verdict: open
      verdict_at: null
---
# OKF Frontmatter v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the frontmatter `description` the single authored source of the article summary (carried into the retrieval vectors), drop the `## Overview` body section, align the schema with OKF v0.1 (open `type`, add `status`), and reserve two link sections excluded from the vectors.

**Architecture:** Six code layers change in a fixed dependency order: (1) `engine/frontmatter.py` gains the schema primitives (`normalize_type`, `normalize_status`, `STATUS_VOCAB`, `RESERVED_SECTIONS`) and drops `coerce_type`; (2) `engine/chunk.py` sources the summary from `description` and excludes reserved sections; (3) `engine/validate.py` drops `missing_overview`, adds `unknown_status`, exempts reserved sections; (4) `okf.py` threads `description`/`status`/open-`type` through `build_frontmatter` and migrates pages in `batch_sweep`; (5) `server.py` exposes `description=`/`status=` on the write tools; (6) docs + version bump. Each layer is a self-contained, independently testable commit.

**Tech Stack:** Python ≥3.10, stdlib-only engine core (no pyyaml, no httpx in `frontmatter`/`validate`/`lint`), pytest (`asyncio_mode=auto`, `pythonpath=["src"]`), flake8 (`max-line-length=100`).

## Global Constraints

- Python `>=3.10`; no new runtime dependencies.
- `flake8 src tests` must stay clean at `max-line-length = 100`; no formatter — match surrounding style by hand.
- `engine/frontmatter.py`, `engine/validate.py`, `engine/lint.py` stay **config-free / stdlib-only** — they may import each other and `frontmatter`, but must NOT import `chunk`/`embed`/`httpx`. Put `RESERVED_SECTIONS` in `frontmatter.py` so `chunk` and `validate` reference `_fm.RESERVED_SECTIONS` (no re-duplication of that constant).
- Tests never hit the network: `monkeypatch.setattr(indexer, "embed_texts", ...)` and set dummy `IWIKI_LLM_BASE_URL` / `IWIKI_LLM_KEY` env vars. Follow the existing `_seed` / `_patch` helpers.
- Keep the `server.py` split: plain implementation functions + `mcp.tool()(...)` registration at the bottom, so tests call the functions directly.
- Preserve the transactional write, freshness guard, and in-place reserved-file refresh (`okf-artifacts-inplace`) unchanged.
- `derive_description` (Overview→description) is RETAINED for the transitional write/migration backfill — do not delete it.
- Final server version is `0.3.0` (minor bump) in `pyproject.toml` and `src/iwiki_mcp/__init__.py`.
- Frontmatter key order after this change: `type, title, description, resource, tags, status, timestamp`.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `src/iwiki_mcp/engine/frontmatter.py` | schema primitives, split/render | add `normalize_type`, `normalize_status`, `STATUS_VOCAB`, `DEFAULT_STATUS`, `RESERVED_SECTIONS`; drop `coerce_type`; add `status` to render order |
| `src/iwiki_mcp/engine/chunk.py` | markdown → embed chunks | summary from `description`; exclude `RESERVED_SECTIONS` **and** `## Overview` from the index; `normalize_type` |
| `src/iwiki_mcp/engine/classify.py` | optional LLM type/tags | `coerce_type` → `normalize_type` |
| `src/iwiki_mcp/engine/validate.py` | section-formation checks | drop `missing_overview`; add `unknown_status`; exempt reserved from `missing_lead` |
| `src/iwiki_mcp/okf.py` | frontmatter assembly + sweep | `build_frontmatter` `description`/`status`/open-`type`; `batch_sweep` migration; `_strip_overview` helper |
| `src/iwiki_mcp/server.py` | MCP tool surface | `description=`/`status=` on `wiki_write_page` / `wiki_update_page` |
| `src/iwiki_mcp/resources.py` | authoring rules resource | v2 schema wording |
| `README.md`, `docs/README.ru.md` | user-facing docs | schema table, description model, reserved sections, Overview removal |
| `pyproject.toml`, `src/iwiki_mcp/__init__.py` | version | `0.2.4` → `0.3.0` |
| Wiki pages (via iwiki MCP) | internal docs | `okf-governance.md`, `indexing.md` |

---

## Task 1: Frontmatter schema primitives

**Files:**
- Modify: `src/iwiki_mcp/engine/frontmatter.py`
- Modify: `src/iwiki_mcp/engine/chunk.py:92` (call site only)
- Modify: `src/iwiki_mcp/okf.py:40` (call site only)
- Modify: `src/iwiki_mcp/engine/classify.py:54` (call site only)
- Test: `tests/test_frontmatter.py`, `tests/test_frontmatter_governance.py`, `tests/test_classify.py`

**Interfaces:**
- Produces:
  - `normalize_type(s: str | None) -> str` — lower/trim; **no clamp**; empty → `DEFAULT_TYPE` (`"concept"`).
  - `normalize_status(s: str | None) -> str` — lower/trim; empty → `DEFAULT_STATUS` (`"stub"`).
  - `STATUS_VOCAB = ("stub", "developing", "stable", "deprecated")`, `DEFAULT_STATUS = "stub"`.
  - `RESERVED_SECTIONS = ("outgoing links", "external links")` (lower-case, compared case-insensitively).
  - `OKF_TYPES`, `DEFAULT_TYPE` unchanged; `coerce_type` **removed**.
  - `render(meta)` emits `status` between `tags` and `timestamp`.

- [ ] **Step 1: Replace/add the failing frontmatter tests**

In `tests/test_frontmatter.py`, replace `test_coerce_type_clamps_offvocab` (lines 45-48) with:

```python
def test_normalize_type_no_clamp():
    assert fm.normalize_type("API") == "api"          # lower/trim
    assert fm.normalize_type("  weird ") == "weird"   # open — NOT clamped
    assert fm.normalize_type(None) == fm.DEFAULT_TYPE
    assert fm.normalize_type("") == fm.DEFAULT_TYPE


def test_normalize_status_open_with_default():
    assert fm.normalize_status("Stable") == "stable"
    assert fm.normalize_status("  weird ") == "weird"     # kept as-is (advisory)
    assert fm.normalize_status(None) == fm.DEFAULT_STATUS
    assert fm.DEFAULT_STATUS == "stub"
    assert fm.STATUS_VOCAB == ("stub", "developing", "stable", "deprecated")


def test_reserved_sections_constant():
    assert fm.RESERVED_SECTIONS == ("outgoing links", "external links")


def test_render_places_status_before_timestamp():
    meta = {"type": "person", "title": "X", "status": "developing",
            "timestamp": "2026-07-10"}
    out = fm.render(meta)
    assert out.index("status:") < out.index("timestamp:")
    meta2, _ = fm.split(out + "# x\n")
    assert meta2["status"] == "developing"
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_frontmatter.py -q`
Expected: FAIL — `AttributeError: module 'iwiki_mcp.engine.frontmatter' has no attribute 'normalize_type'` (and `normalize_status`, `RESERVED_SECTIONS`).

- [ ] **Step 3: Add the primitives and update `render` in `frontmatter.py`**

Add the constants after line 10 (`MAX_TAGS = 5`):

```python
STATUS_VOCAB = ("stub", "developing", "stable", "deprecated")
DEFAULT_STATUS = "stub"
# Reserved ## sections: authored link lists, excluded from chunking/embedding and
# exempt from lead checks. Lower-case; compared case-insensitively. Referenced by
# chunk.py and validate.py so the set lives in one config-free place.
RESERVED_SECTIONS = ("outgoing links", "external links")
```

Change the `render` order line (currently line 49):

```python
    order = ["type", "title", "description", "resource", "tags", "status", "timestamp"]
```

Replace `coerce_type` (currently lines 85-87) with:

```python
def normalize_type(s: str | None) -> str:
    """Trim/lower-case a type for matching. Open vocabulary — NOT clamped to
    OKF_TYPES (that stays advisory, flagged by validate/lint). Empty -> DEFAULT_TYPE."""
    return (s or "").strip().lower() or DEFAULT_TYPE


def normalize_status(s: str | None) -> str:
    """Trim/lower-case a status. Open like type: a value outside STATUS_VOCAB is
    kept as-is (flagged advisory). Empty -> DEFAULT_STATUS."""
    return (s or "").strip().lower() or DEFAULT_STATUS
```

- [ ] **Step 4: Update the three `coerce_type` call sites to `normalize_type`**

`src/iwiki_mcp/engine/chunk.py` line 92:

```python
    ptype = _fm.normalize_type(meta.get("type")) if meta.get("type") else None
```

`src/iwiki_mcp/okf.py` line 40 (inside `build_frontmatter`, the `explicit_type is not None` branch):

```python
        mtype = fm.normalize_type(explicit_type)
```

`src/iwiki_mcp/engine/classify.py` line 54:

```python
        return {"type": fm.normalize_type(data.get("type")),
```

- [ ] **Step 5: Update the dependent tests for the open-type behavior change**

In `tests/test_frontmatter_governance.py`, replace `test_coerce_type_case_and_whitespace_insensitive` (lines 4-8) with:

```python
def test_normalize_type_case_and_whitespace_insensitive_open():
    assert fm.normalize_type("API") == "api"
    assert fm.normalize_type(" Architecture ") == "architecture"
    assert fm.normalize_type("weird") == "weird"      # open — not clamped
```

In `tests/test_classify.py`, replace `test_classify_offvocab_falls_back` (lines 22-25) with:

```python
def test_classify_keeps_type_open(monkeypatch):
    # type is now an open vocabulary: an off-list classifier value is kept
    # (normalized), not clamped — advisory unknown_type flags it downstream.
    monkeypatch.setattr(classify, "_chat", lambda cfg, prompt: '{"type": "Person", "tags": []}')
    out = classify.classify_page(_cfg(), "body", existing_tags=[])
    assert out["type"] == "person"
```

- [ ] **Step 6: Run the full suite and flake8**

Run: `uv run pytest -q && uv run flake8 src tests`
Expected: PASS — all green, lint clean. (`tests/test_chunk_frontmatter.py`, `tests/test_classify.py::test_classify_parses_and_governs`, and write tests still pass because valid types round-trip through `normalize_type` unchanged.)

- [ ] **Step 7: Commit**

```bash
git add src/iwiki_mcp/engine/frontmatter.py src/iwiki_mcp/engine/chunk.py src/iwiki_mcp/okf.py src/iwiki_mcp/engine/classify.py tests/test_frontmatter.py tests/test_frontmatter_governance.py tests/test_classify.py
git commit -m "feat(okf): open type + status/reserved-section frontmatter primitives"
```

---

## Task 2: Chunking — description summary, drop Overview + reserved sections

**Files:**
- Modify: `src/iwiki_mcp/engine/chunk.py`
- Test: `tests/test_chunk_frontmatter.py`
- Test (regression — Overview-as-summary fixtures): `tests/engine/test_chunk.py`

**Interfaces:**
- Consumes: `_fm.RESERVED_SECTIONS`, `_fm.OVERVIEW_HEADING`, `_fm.normalize_type` (Task 1).
- Produces: `chunk_markdown(file, content, size, overlap, summary_max=400)` — article summary now sourced from `meta["description"]` (whitespace-collapsed, capped to `summary_max`); sections whose heading (lower-cased) is in `RESERVED_SECTIONS` **or** equals `## Overview` are dropped before chunking, so an un-migrated Overview **never** enters the index; the summary is the `description`, never an Overview body (no Overview-as-summary fallback).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_chunk_frontmatter.py`:

```python
def test_summary_from_description_not_overview():
    page = (
        "---\ntype: person\ndescription: Alice covers AR ledger and refunds.\n---\n"
        "# Alice\n\n## Role\nreal content words here\n"
    )
    chunks = chunk_markdown("p.md", page, size=512, overlap=64)
    assert chunks
    assert all("Alice covers AR ledger and refunds." in c.text for c in chunks)


def test_overview_excluded_from_index():
    # `## Overview` is never indexed and is not the summary source. An un-migrated
    # Overview (no frontmatter description) does not enter the vectors at all.
    page = "# T\n\n## Overview\nsummary body\n\n## Body\nwords\n"
    chunks = chunk_markdown("p.md", page, size=512, overlap=64)
    headings = {c.heading for c in chunks}
    assert "Overview" not in headings
    assert headings == {"Body"}


def test_reserved_link_sections_excluded():
    page = (
        "---\ntype: person\ndescription: d\n---\n# T\n\n## Role\nprose words\n\n"
        "## Outgoing links\n- [x](y.md)\n\n## External links\n- https://example.com\n"
    )
    chunks = chunk_markdown("p.md", page, size=512, overlap=64)
    headings = {c.heading for c in chunks}
    assert headings == {"Role"}
    assert all("example.com" not in c.text and "y.md" not in c.text for c in chunks)
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_chunk_frontmatter.py -q`
Expected: FAIL — `test_summary_from_description_not_overview` (summary empty), `test_overview_excluded_from_index` (Overview still indexed), `test_reserved_link_sections_excluded` (link sections still chunked).

- [ ] **Step 3: Rewrite the summary/section logic in `chunk.py`**

Update the module docstring (lines 1-7) to:

```python
"""Split markdown on ## headings into sections, then into overlapping sub-chunks.

Each content section's sub-chunks are prefixed with the page title, the frontmatter
``description`` (the authored article summary), the section heading, and the section
lead, so every vector carries whole-article + whole-section context. The reserved
link sections (``## Outgoing links`` / ``## External links``) and any ``## Overview``
are excluded from the index; the summary lives only in ``description``.
"""
```

Remove the `OVERVIEW_HEADING` constant (line 19; keep `LEAD_MAX`).

Update the `chunk_markdown` docstring (lines 84-90) to:

```python
    """Return chunks for one markdown file.

    The article summary is the frontmatter ``description``; every section that is not
    reserved and not ``## Overview`` has its sub-chunks prefixed with title + summary
    + heading + lead, then word-split with overlap. Reserved link sections and
    ``## Overview`` are dropped, never indexed.
    """
```

Replace the body block (currently lines 91-101, from `meta, content = _fm.split(content)` through `for heading, body in secs:`) with:

```python
    meta, content = _fm.split(content)
    ptype = _fm.normalize_type(meta.get("type")) if meta.get("type") else None
    ptags = _fm.normalize_tags(meta.get("tags", [])) if meta.get("tags") else []
    out: list[Chunk] = []
    title = _page_title(content, file)
    article_summary = " ".join(meta.get("description", "").split())[:summary_max]
    # `## Overview` is never indexed: its text belongs in `description` now, and an
    # un-migrated Overview must not leak into the vectors. Excluded like the reserved
    # link sections (migration also strips it from the body).
    excluded = (*_fm.RESERVED_SECTIONS, _fm.OVERVIEW_HEADING)
    secs = [(h, b) for h, b in _sections(content) if h.lower() not in excluded]
    for heading, body in secs:
```

- [ ] **Step 4: Run the chunk tests to verify they pass**

Run: `uv run pytest tests/test_chunk_frontmatter.py -q`
Expected: PASS.

- [ ] **Step 5: Update the existing Overview-as-summary tests (regression)**

These pre-existing tests in `tests/engine/test_chunk.py` relied on `## Overview` being the article-summary source. Overview stays excluded from the index (so `test_overview_section_is_not_indexed` keeps passing **unchanged**), but the summary now comes from `description`, so replace `test_prefix_carries_title_overview_and_lead` (lines 40-46), `test_prefix_on_every_subchunk_of_a_split_section` (lines 49-57), and `test_hash_changes_when_overview_changes` (lines 73-78) with:

```python
def test_prefix_carries_title_description_and_lead():
    page = (
        "---\ntype: concept\ndescription: The gateway routes API traffic via a proxy.\n---\n"
        "# Proxy Management\n\n## TLS Handling\nThe proxy terminates TLS using a local CA.\n"
    )
    chunks = chunk_markdown("proxy.md", page, size=512, overlap=64)
    tls = next(c for c in chunks if c.heading == "TLS Handling")
    assert tls.text.startswith("# Proxy Management\n")
    assert "The gateway routes API traffic via a proxy." in tls.text  # summary from description
    assert "## TLS Handling" in tls.text
    assert "The proxy terminates TLS using a local CA." in tls.text


def test_prefix_on_every_subchunk_of_a_split_section():
    body = " ".join(str(i) for i in range(40))
    md = f"---\ndescription: summ of all.\n---\n# T\n\n## Big\n{body}\n"
    chunks = chunk_markdown("f.md", md, size=8, overlap=2)
    big = [c for c in chunks if c.heading == "Big"]
    assert len(big) > 1
    assert all(c.text.startswith("# T\n") for c in big)
    assert all("summ of all." in c.text for c in big)   # article summary from description
    assert all("## Big" in c.text for c in big)


def test_hash_changes_when_description_changes():
    # Overview is no longer indexed, so a section's hash tracks the `description`
    # prefix, not an Overview body.
    a = chunk_markdown("f.md", "---\ndescription: summ one.\n---\n# T\n\n## A\nbody.\n",
                       size=512, overlap=64)
    b = chunk_markdown("f.md", "---\ndescription: summ two.\n---\n# T\n\n## A\nbody.\n",
                       size=512, overlap=64)
    assert a[0].hash != b[0].hash
```

(Leave `test_overview_section_is_not_indexed`, `test_no_overview_yields_no_summary_line`, and `test_title_falls_back_to_humanized_basename` unchanged — Overview stays excluded from the index and an empty `description` yields no summary line, so all three still pass. `PAGE` stays referenced by `test_overview_section_is_not_indexed`. No changes to `tests/test_indexer.py` or `tests/test_server_search.py` are needed: their frontmatter-less `## Overview` fixtures still yield a single indexed section, exactly as before.)

- [ ] **Step 6: Run the full suite and flake8**

Run: `uv run pytest -q && uv run flake8 src tests`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/iwiki_mcp/engine/chunk.py tests/test_chunk_frontmatter.py tests/engine/test_chunk.py
git commit -m "feat(okf): chunk summary from description, exclude Overview + reserved link sections"
```

---

## Task 3: Validation — drop missing_overview, add unknown_status, exempt reserved

**Files:**
- Modify: `src/iwiki_mcp/engine/validate.py`
- Test: `tests/engine/test_validate.py`, `tests/engine/test_lint.py`, `tests/test_validate_frontmatter.py`

**Interfaces:**
- Consumes: `_fm.RESERVED_SECTIONS`, `_fm.STATUS_VOCAB`, `_fm.normalize_status`, `_fm.OKF_TYPES` (Task 1).
- Produces: `validate_page(content) -> list[dict]` — no `missing_overview`; reserved sections skip `missing_lead`/`long_lead`; advisory `unknown_status` when `status` present and outside `STATUS_VOCAB`; `missing_description`, `missing_type`, `unknown_type`, blocking `deep_heading`/`pre_h2_text` unchanged.

- [ ] **Step 1: Write/replace the failing validate tests**

In `tests/engine/test_validate.py`, replace `test_missing_overview_is_advisory` (lines 34-36) with:

```python
def test_no_missing_overview_finding():
    assert "missing_overview" not in _types("# T\n\n## A\nlead.\n")


def test_reserved_sections_exempt_from_missing_lead():
    page = ("# T\n\n## Role\nlead here.\n\n## Outgoing links\n- [x](y.md)\n\n"
            "## External links\n- https://example.com\n")
    fs = [f for f in validate_page(page) if f["type"] == "missing_lead"]
    assert fs == []          # link lists are not prose — no lead expected


def test_unknown_status_advisory():
    page = "---\ntype: person\ndescription: d\nstatus: bogus\n---\n# T\n\n## A\nlead.\n"
    fs = [f for f in validate_page(page) if f["type"] == "unknown_status"]
    assert fs and fs[0]["severity"] == "advisory"


def test_known_status_no_finding():
    page = "---\ntype: person\ndescription: d\nstatus: stable\n---\n# T\n\n## A\nlead.\n"
    assert "unknown_status" not in _types(page)


def test_status_as_list_does_not_crash():
    # `status: [a, b]` parses to a list; the isinstance guard skips it, no crash.
    page = "---\ntype: person\ndescription: d\nstatus: [a, b]\n---\n# T\n\n## A\nlead.\n"
    assert "unknown_status" not in _types(page)
```

In `tests/test_validate_frontmatter.py`, append:

```python
def test_missing_description_still_fires_without_overview():
    page = "---\ntype: api\n---\n# T\n\n## B\nbody\n"
    assert "missing_description" in _types(validate_page(page))
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/engine/test_validate.py tests/test_validate_frontmatter.py -q`
Expected: FAIL — `unknown_status` not produced; reserved-section pages emit `missing_lead`.

- [ ] **Step 3: Update `validate.py`**

Update the module docstring advisory list (lines 5-8) to:

```python
iwiki-validate PreToolUse hook; the advisory subset (missing_lead, long_lead, and
— only when the page has frontmatter — missing_type, unknown_type, missing_description,
unknown_status) is report-only.
```

Remove the `OVERVIEW_HEADING = "overview"` constant (line 14; keep `LEAD_MAX`).

Replace the section-checks block (currently lines 58-71, from `secs = _sections(body)` through the `long_lead` append) with:

```python
    secs = _sections(body)
    for heading, sbody in secs:
        if heading.lower() in _fm.RESERVED_SECTIONS:
            continue                    # link lists, not prose — no lead expected
        lead = _lead(sbody)
        if not lead:
            findings.append({"type": "missing_lead", "severity": "advisory",
                             "text": f"section '{heading}' has no lead paragraph"})
        elif len(lead) > LEAD_MAX:
            findings.append({"type": "long_lead", "severity": "advisory",
                             "text": f"section '{heading}' lead exceeds {LEAD_MAX} chars"})
```

In the `if meta:` block (currently lines 72-81), add the `unknown_status` check after the `missing_description` append:

```python
        if not meta.get("description"):
            findings.append({"type": "missing_description", "severity": "advisory",
                             "text": "frontmatter has no 'description'"})
        status = meta.get("status")
        if isinstance(status, str) and _fm.normalize_status(status) not in _fm.STATUS_VOCAB:
            findings.append({"type": "unknown_status", "severity": "advisory",
                             "text": f"status '{status}' not in the status vocabulary"})
```

- [ ] **Step 4: Fix the two existing tests that assert `missing_overview`**

In `tests/engine/test_lint.py`, `test_section_findings_folded_into_report` (lines 55-62) — replace its body with:

```python
def test_section_findings_folded_into_report(tmp_path):
    # page with a ### deep heading → deep_heading surfaces; missing_overview is gone
    wd = _wiki(tmp_path, {"a.md": "## A\nlead.\n\n### deep\nx\n"})
    out = lint(wd)
    types = {f["type"] for f in out["sections"]}
    assert "deep_heading" in types
    assert "missing_overview" not in types
    assert all("page" in f for f in out["sections"])
```

- [ ] **Step 5: Run the full suite and flake8**

Run: `uv run pytest -q && uv run flake8 src tests`
Expected: PASS. (`tests/engine/test_validate.py::test_clean_page_has_no_findings` still passes — its `## Overview` section carries a lead and there is no frontmatter, so no findings.)

- [ ] **Step 6: Commit**

```bash
git add src/iwiki_mcp/engine/validate.py tests/engine/test_validate.py tests/engine/test_lint.py tests/test_validate_frontmatter.py
git commit -m "feat(okf): validate drops missing_overview, adds unknown_status, exempts reserved sections"
```

---

## Task 4: Frontmatter assembly + migration (`okf.py`)

**Files:**
- Modify: `src/iwiki_mcp/okf.py`
- Test: `tests/test_okf_build_frontmatter.py` (new), `tests/test_export_okf.py`

**Interfaces:**
- Consumes: `fm.normalize_type`, `fm.normalize_status`, `fm.DEFAULT_STATUS`, `fm.RESERVED_SECTIONS`, `fm.derive_description` (retained).
- Produces:
  - `build_frontmatter(cfg, base_dir, domain, slug, body, *, source, explicit_type, explicit_tags, timestamp_path, explicit_description=None, explicit_status=None, tag_vocab=None) -> (block, warning)`. Precedence: `description` = explicit param → transitional `## Overview` derive → empty (+warning); `status` = explicit param → `DEFAULT_STATUS`; `type` stored open (normalized, not clamped). `warning` is `"; "`-joined (or `None`).
  - `_strip_overview(body, max_chars) -> (new_body, overview_text)` — drops a first-section `## Overview`, returns its collapsed/capped text.
  - `batch_sweep(cfg, base_dir, domain) -> {"fixed_links": [...], "added_frontmatter": [...]}` — additionally strips `## Overview`, backfills `description` from it when empty, and defaults `status` to `stub`; idempotent; preserves `type`/`tags`/`resource`/links.

- [ ] **Step 1: Add the `re` import and the `_H2` regex to `okf.py`**

At the top of `src/iwiki_mcp/okf.py`, add `import re` alongside the existing imports (after `import json`), then add a module-level constant below the imports (after the `from . import base as _base` line):

```python
_H2 = re.compile(r"^##\s+(.*?)\s*$", re.MULTILINE)   # keep in sync with chunk._H2
```

- [ ] **Step 2: Write the failing unit tests**

Create `tests/test_okf_build_frontmatter.py`:

```python
from iwiki_mcp import okf
from iwiki_mcp.engine import frontmatter as fm
from iwiki_mcp.engine.config import Config


def _cfg():
    return Config(base_url="http://x", api_key="k", embed_model="e", chat_model=None,
                  dimensions=2, chunk_size=512, chunk_overlap=64, summary_max=400,
                  top_k=8, score_threshold=0.2, graph_depth=2, ignore=None)


def _build(body, **kw):
    block, warning = okf.build_frontmatter(
        _cfg(), "/b", "d", "s", body, source=None, explicit_type="person",
        explicit_tags=None, timestamp_path="d/s.md", **kw)
    meta, _ = fm.split(block + "# x\n")
    return meta, warning


def test_explicit_description_and_status():
    meta, warning = _build("# X\n\n## Role\nprose\n",
                           explicit_description="Alice covers AR.", explicit_status="Stable")
    assert meta["description"] == "Alice covers AR."
    assert meta["status"] == "stable"
    assert warning is None


def test_status_defaults_to_stub():
    meta, _ = _build("# X\n\n## Role\nprose\n", explicit_description="d")
    assert meta["status"] == "stub"


def test_open_type_kept():
    meta, _ = _build("# X\n\n## Role\nprose\n", explicit_description="d")
    assert meta["type"] == "person"            # not clamped to concept


def test_transitional_overview_derive():
    meta, warning = _build("# X\n\n## Overview\nsummary here\n\n## Role\nprose\n")
    assert meta["description"] == "summary here"
    assert warning is None


def test_missing_description_warns():
    meta, warning = _build("# X\n\n## Role\nprose\n")
    assert "description" not in meta
    assert "description" in warning


def test_strip_overview_first_section_only():
    body = "# X\n\n## Overview\nsum\n\n## Role\nprose\n"
    new_body, text = okf._strip_overview(body, 400)
    assert text == "sum"
    assert "## Overview" not in new_body
    assert "## Role" in new_body


def test_strip_overview_ignores_non_first():
    body = "# X\n\n## Role\nprose\n\n## Overview\nnot first\n"
    new_body, text = okf._strip_overview(body, 400)
    assert text == ""
    assert new_body == body
```

- [ ] **Step 3: Run the unit tests to verify they fail**

Run: `uv run pytest tests/test_okf_build_frontmatter.py -q`
Expected: FAIL — `build_frontmatter` rejects `explicit_description` kwarg; `okf._strip_overview` undefined.

- [ ] **Step 4: Rewrite `build_frontmatter` and add `_strip_overview`**

Replace `build_frontmatter` (currently lines 35-63) with:

```python
def build_frontmatter(cfg, base_dir, domain, slug, body, *, source,
                      explicit_type, explicit_tags, timestamp_path,
                      explicit_description=None, explicit_status=None, tag_vocab=None):
    """Return (frontmatter_block, warning). Precedence: explicit -> classify -> default.
    description: explicit param -> transitional ## Overview derive -> empty (+warning).
    status: explicit param -> DEFAULT_STATUS. type: stored as authored (open, normalized)."""
    warnings: list = []
    if explicit_type is not None:
        mtype = fm.normalize_type(explicit_type)
        mtags = fm.normalize_tags(explicit_tags or [])
    elif cfg.chat_model:
        vocab = tag_vocab if tag_vocab is not None else domain_tag_vocab(base_dir, domain)
        r = classify.classify_page(cfg, body, vocab)
        mtype = r["type"]
        mtags = fm.normalize_tags(explicit_tags) if explicit_tags else r["tags"]
        if r["warning"]:
            warnings.append(r["warning"])
    else:
        mtype = fm.DEFAULT_TYPE
        mtags = fm.normalize_tags(explicit_tags or [])
        warnings.append("type not given and IWIKI_CHAT_MODEL unset; defaulted to concept")

    meta: dict = {"type": mtype, "title": fm.derive_title(body, slug)}
    desc = (explicit_description if explicit_description is not None
            else fm.derive_description(body, cfg.summary_max))
    if desc:
        meta["description"] = desc
    else:
        warnings.append("no description given and no ## Overview to derive from")
    if source:
        meta["resource"] = source
    if mtags:
        meta["tags"] = mtags
    meta["status"] = fm.normalize_status(explicit_status) if explicit_status else fm.DEFAULT_STATUS
    meta["timestamp"] = (git_last_commit_date(base_dir, timestamp_path)
                         or _dt.date.today().isoformat())
    return fm.render(meta), ("; ".join(warnings) or None)


def _strip_overview(body: str, max_chars: int) -> tuple[str, str]:
    """If the FIRST ## section is 'Overview', drop it and return (new_body,
    overview_text) — mirrors derive_description's first-section rule. No Overview
    -> (body, "")."""
    ms = list(_H2.finditer(body))
    if not ms or ms[0].group(1).strip().lower() != "overview":
        return body, ""
    first, end = ms[0], (ms[1].start() if len(ms) > 1 else len(body))
    overview_text = " ".join(body[first.end():end].split())[:max_chars]
    return body[:first.start()] + body[end:], overview_text
```

- [ ] **Step 5: Run the unit tests to verify they pass**

Run: `uv run pytest tests/test_okf_build_frontmatter.py -q`
Expected: PASS.

- [ ] **Step 6: Write the failing migration test**

Append to `tests/test_export_okf.py`:

```python
def test_export_okf_migrates_overview_and_status(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    dom = os.path.join(b, "backend")
    # legacy page: frontmatter present, empty description, ## Overview in body, no status
    with open(os.path.join(dom, "a.md"), "w", encoding="utf-8") as fh:
        fh.write("---\ntype: api\n---\n# A\n\n## Overview\nsummary text\n\n## B\nwords\n")
    server.wiki_export_okf("backend")
    text = open(os.path.join(dom, "a.md"), encoding="utf-8").read()
    assert "## Overview" not in text                 # section removed
    assert "description: summary text" in text       # backfilled from Overview
    assert "status: stub" in text                    # defaulted
    assert "type: api" in text                        # preserved
    # idempotent on re-run
    first = text
    server.wiki_export_okf("backend")
    assert open(os.path.join(dom, "a.md"), encoding="utf-8").read() == first
```

- [ ] **Step 7: Run the migration test to verify it fails**

Run: `uv run pytest tests/test_export_okf.py::test_export_okf_migrates_overview_and_status -q`
Expected: FAIL — `## Overview` still present, no `status`/`description`.

- [ ] **Step 8: Rewrite `batch_sweep`**

Replace `batch_sweep` (currently lines 132-161) with:

```python
def batch_sweep(cfg, base_dir, domain) -> dict:
    """Deterministic whole-domain in-place OKF conformance sweep (no chat model).
    Converts residual [[...]] links, migrates the body to the v2 model (strips a
    first-section ## Overview, backfilling frontmatter ``description`` from it when
    empty and defaulting ``status`` to stub), and guarantees frontmatter on every
    page, preserving existing type/tags. Writes back only changed files (idempotent)."""
    from .engine.links import to_markdown_links
    dom = Path(base_dir) / domain
    fixed_links, added_frontmatter = [], []
    for slug in _page_slugs(dom):
        page_file = f"{slug}.md"
        p = dom / page_file
        original = p.read_text(encoding="utf-8")
        meta, body = fm.split(original)
        linked = to_markdown_links(body)
        links_changed = linked != body
        new_body, overview_text = _strip_overview(linked, cfg.summary_max)
        if meta:
            if meta.get("tags"):
                meta["tags"] = fm.normalize_tags(meta["tags"])
            if not meta.get("description") and overview_text:
                meta["description"] = overview_text
            if not meta.get("status"):
                meta["status"] = fm.DEFAULT_STATUS
            new_full = fm.render(meta) + new_body
        else:
            src = latest_source(base_dir, domain, page_file)
            block, _ = build_frontmatter(
                cfg, base_dir, domain, slug, new_body,
                source=src, explicit_type=fm.DEFAULT_TYPE, explicit_tags=None,
                explicit_description=(overview_text or None),
                timestamp_path=f"{domain}/{page_file}")
            new_full = block + new_body
            added_frontmatter.append(slug)
        if new_full != original:
            p.write_text(new_full, encoding="utf-8")
            if links_changed:
                fixed_links.append(slug)
    return {"fixed_links": fixed_links, "added_frontmatter": added_frontmatter}
```

- [ ] **Step 9: Run the full suite and flake8**

Run: `uv run pytest -q && uv run flake8 src tests`
Expected: PASS. (The existing `test_export_okf_*` tests still pass: their assertions on `type`, converted links, and `added_frontmatter`/`fixed_links` are unaffected by the added Overview-stripping.)

- [ ] **Step 10: Commit**

```bash
git add src/iwiki_mcp/okf.py tests/test_okf_build_frontmatter.py tests/test_export_okf.py
git commit -m "feat(okf): description/status/open-type assembly and Overview->description migration"
```

---

## Task 5: Write/update tool params (`server.py`)

**Files:**
- Modify: `src/iwiki_mcp/server.py`
- Test: `tests/test_server_write_frontmatter.py`, `tests/test_server_update.py`

**Interfaces:**
- Consumes: `okf.build_frontmatter(... explicit_description=, explicit_status=)` (Task 4), `_fm.normalize_status` (Task 1).
- Produces:
  - `wiki_write_page(domain, slug, markdown, source=None, type=None, tags=None, description=None, status=None) -> dict` — passes `description`/`status` to `build_frontmatter`.
  - `wiki_update_page(domain, slug, heading, new_body, source=None, description=None, status=None) -> dict` — sets `meta["description"]`/`meta["status"]` only when the param is given; no longer re-derives description from a body Overview.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_server_write_frontmatter.py`:

```python
def test_write_with_description_and_status(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    body = "# Alice\n\n## Role\nBilling engineer work.\n"
    res = server.wiki_write_page("d", "alice", body, source=None, type="person",
                                 description="Alice covers AR ledger.", status="stable")
    assert "error" not in res
    meta, _ = fm.split((tmp_path / "d" / "alice.md").read_text(encoding="utf-8"))
    assert meta["type"] == "person"                       # open type kept
    assert meta["description"] == "Alice covers AR ledger."
    assert meta["status"] == "stable"


def test_write_missing_status_defaults_stub(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    body = "# Alice\n\n## Role\nwork.\n"
    server.wiki_write_page("d", "alice", body, source=None, type="person",
                           description="d")
    meta, _ = fm.split((tmp_path / "d" / "alice.md").read_text(encoding="utf-8"))
    assert meta["status"] == "stub"


def test_write_missing_description_warns(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    body = "# Alice\n\n## Role\nwork.\n"          # no Overview, no description param
    res = server.wiki_write_page("d", "alice", body, source=None, type="person")
    assert "warning" in res and "description" in res["warning"]
```

Append to `tests/test_server_update.py` (reuse that file's existing `_seed`/setup helpers and imports):

```python
def test_update_sets_description_and_status(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    server.wiki_write_page("backend", "alice", "# Alice\n\n## Role\nwork.\n",
                           source=None, type="person", description="old desc")
    res = server.wiki_update_page("backend", "alice", "Role", "new role prose.\n",
                                  description="new desc", status="deprecated")
    assert "error" not in res
    meta, _ = server._fm.split(
        open(server._page_path(b, "backend", "alice"), encoding="utf-8").read())
    assert meta["description"] == "new desc"
    assert meta["status"] == "deprecated"
```

> Note: confirm the `_seed`/binding helper name in `tests/test_server_update.py` and mirror it; the assertion pattern above uses `server._page_path` to locate the file — adjust to that file's existing path helper if it differs.

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_server_write_frontmatter.py tests/test_server_update.py -q`
Expected: FAIL — `wiki_write_page`/`wiki_update_page` reject `description=`/`status=`.

- [ ] **Step 3: Add params to `wiki_write_page`**

Change the signature (currently lines 320-324):

```python
@_safe
def wiki_write_page(
    domain: str, slug: str, markdown: str, source: str | None = None,
    type: str | None = None, tags: list[str] | None = None,
    description: str | None = None, status: str | None = None,
) -> dict:
```

Update the `build_frontmatter` call (currently lines 369-372):

```python
    fm_block, fm_warning = okf.build_frontmatter(
        cfg, bind.base, valid_domain, slug, markdown,
        source=source, explicit_type=type, explicit_tags=tags,
        explicit_description=description, explicit_status=status,
        timestamp_path=f"{valid_domain}/{page_file}")
```

- [ ] **Step 4: Add params to `wiki_update_page` and drop the Overview re-derive**

Change the signature (currently lines 421-424):

```python
@_safe
def wiki_update_page(
    domain: str, slug: str, heading: str, new_body: str, source: str | None = None,
    description: str | None = None, status: str | None = None,
) -> dict:
```

Replace the meta-handling block (currently lines 465-473) with:

```python
    cfg = Config.load()
    if meta:
        if description is not None:
            meta["description"] = description
        if status is not None:
            meta["status"] = _fm.normalize_status(status)
        meta["timestamp"] = _dt.date.today().isoformat()
        new_md = _fm.render(meta) + new_body
    else:
        new_md = new_body
```

- [ ] **Step 5: Register no new tools — verify the tail registration is unchanged**

The `mcp.tool()(wiki_write_page)` / `mcp.tool()(wiki_update_page)` lines already register the same function objects; added keyword params surface automatically. No edit needed at the registration block.

- [ ] **Step 6: Run the new tests to verify they pass**

Run: `uv run pytest tests/test_server_write_frontmatter.py tests/test_server_update.py -q`
Expected: PASS.

- [ ] **Step 7: Run the full suite and flake8**

Run: `uv run pytest -q && uv run flake8 src tests`
Expected: PASS. (Existing write tests whose bodies contain `## Overview` still derive a description transitionally, so no new warning surfaces there.)

- [ ] **Step 8: Commit**

```bash
git add src/iwiki_mcp/server.py tests/test_server_write_frontmatter.py tests/test_server_update.py
git commit -m "feat(okf): description=/status= params on wiki_write_page and wiki_update_page"
```

---

## Task 6: Search/graph integration + docs + version

**Files:**
- Test: `tests/test_okf_server.py`
- Test (regression — authoring-rules text): `tests/test_resources.py`, `tests/test_server_lint_sync.py`
- Modify: `src/iwiki_mcp/resources.py`
- Modify: `README.md`, `docs/README.ru.md`
- Modify: `pyproject.toml:3`, `src/iwiki_mcp/__init__.py:2`

**Interfaces:**
- Consumes: the full write→index→search/graph path (Tasks 2, 4, 5).

- [ ] **Step 1: Write the failing search/graph integration test (SC-3)**

Append to `tests/test_okf_server.py`:

```python
def test_reserved_link_sections_not_indexed_but_graphed(tmp_path, monkeypatch):
    b = _seed(tmp_path, monkeypatch)
    server.wiki_write_page("backend", "target", "# Target\n\n## Body\ntarget prose.\n",
                           source=None, type="concept", description="target page")
    page = ("# Alice\n\n## Role\nbilling prose here.\n\n"
            "## Outgoing links\n- [Target](target.md)\n\n"
            "## External links\n- https://example.com/docs\n")
    server.wiki_write_page("backend", "alice", page, source=None, type="person",
                           description="Alice covers billing.")
    recs = VectorStore(base.index_path(b, "backend")).load()
    alice = [r for r in recs if r.file == "alice.md"]
    headings = {r.heading for r in alice}
    assert headings == {"Role"}                      # link sections not indexed
    # the authored outgoing link still feeds the graph
    rel = server.wiki_related("backend", "alice.md#outgoing-links")
    dumped = str(rel)
    assert "target" in dumped
```

> Note: `wiki_related` takes a `section_id` of the form `slug.md#heading-slug`. If the reserved section is excluded from the vector store, its own node may be absent; in that case assert the edge from the whole-page graph instead by querying a real indexed section id (e.g. `alice.md#role`) and confirming `target` appears among related pages. Adjust the `section_id` to whichever the graph resolves — the invariant to prove is: reserved-section links reach `wiki_related`, reserved-section prose does not reach the index.

- [ ] **Step 2: Run the integration test to verify it passes (behavior already implemented)**

Run: `uv run pytest tests/test_okf_server.py::test_reserved_link_sections_not_indexed_but_graphed -q`
Expected: PASS (Tasks 2 + 4 already deliver the behavior). If the `wiki_related` node id does not resolve, apply the Note's adjustment, re-run to PASS.

- [ ] **Step 3: Rewrite the authoring rules in `resources.py`**

Replace the two Overview/type-vocabulary bullets and the `## OKF frontmatter` block. Change the third top-level bullet (currently "Lead with `# Title`, then a first `## Overview` section...") to:

```
- Lead with `# Title`, then the page's `##` sections directly. Do NOT write a
  `## Overview` section — the article summary is the frontmatter `description`.
```

Replace the `## OKF frontmatter` section body (currently lines 23-37) with:

```
## OKF frontmatter

- Every page carries a YAML frontmatter block above the `# Title` H1. The write
  tools fill it. Fields: `type` (required), `title`, `description`, `resource`,
  `tags`, `status`, `timestamp`.
- `description` is the authored article summary and the single source of it (it is
  embedded as each section's context prefix). Write it rich: include `Covers:` and
  `Terms:` keyword lines so retrieval matches the page. There is no `## Overview`.
- `type` is an OPEN vocabulary. Prefer a common value -- `architecture`, `api`,
  `guide`, `reference`, `runbook`, `concept` (default) -- but any lower-case value
  is allowed (e.g. `person`, `team`); an off-list value is only advised, not rejected.
- `status` is one of `stub` (default), `developing`, `stable`, `deprecated`.
- `tags` are lowercase kebab-case, <=5 per page; reuse an existing domain tag first.
- Put relationship links in two reserved sections, `## Outgoing links` (Markdown links
  to other pages) and `## External links` (bare URLs). Both are EXCLUDED from search
  indexing but still feed the link graph (`wiki_related`, `lint`).
- The slugs `index` and `log` are reserved: `index.md` / `log.md` are generated
  OKF navigation/history files kept fresh on every write. The write tools reject them.
```

Then update the two tests that assert on the removed `## Overview` rule. They pass today only because the new text still contains the literal `## Overview` (inside "Do NOT write a `## Overview`") — make them assert the v2 rules instead so they don't silently rot.

In `tests/test_resources.py`, `test_authoring_rules_cover_section_format`, replace the stale Overview line (line 6, `assert "## overview" in text`) with:

```python
    assert "description" in text          # description is the authored summary
```

In `tests/test_server_lint_sync.py` (line 65), replace `assert "## Overview" in out["authoring_rules"]` with:

```python
    assert "description" in out["authoring_rules"]
```

- [ ] **Step 4: Update `README.md`**

Replace the `type` and `description` rows of the schema table (lines 238, 240) and add a `status` row after `tags` (line 242):

```
| `type` | Required. **Open** vocabulary: prefer `architecture`, `api`, `guide`, `reference`, `runbook`, `concept` (default), but any value is accepted (e.g. `person`); off-list values get only an advisory `unknown_type`. |
```

```
| `description` | The authored article summary — the single source of the summary, embedded as each section's context prefix. Falls back to a `## Overview` section only transitionally (migration). |
```

Add after the `tags` row:

```
| `status` | Optional iwiki extension: `stub` (default), `developing`, `stable`, `deprecated`. |
```

Under the schema table, replace the sentence that begins "The reserved OKF files `index.md`..." — keep it, and add a new paragraph after line 245:

```
Pages no longer carry a `## Overview` section: the summary lives in `description`.
Relationship links go in two reserved `##` sections — `## Outgoing links` (Markdown
links) and `## External links` (bare URLs) — which are excluded from the search index
but still feed the link graph. Run `wiki_export_okf` once to migrate legacy pages
(it strips `## Overview`, backfills `description`, and defaults `status`).
```

Update the `wiki_export_okf` row (line 261) to mention the v2 migration — append to that cell:

```
It also migrates each page to the v2 body model: strips a `## Overview` section, backfilling `description` from it when empty, and defaults `status` to `stub`.
```

- [ ] **Step 5: Update `docs/README.ru.md` (equivalent Russian wording)**

Apply the same changes to the Russian schema table (lines 234, 236, add `status` after 238) and the export row (line 257):

```
| `type` | Обязательное. **Открытый** словарь: предпочтительны `architecture`, `api`, `guide`, `reference`, `runbook`, `concept` (по умолчанию), но допустимо любое значение (например, `person`); значения вне списка получают лишь рекомендательный `unknown_type`. |
```

```
| `description` | Авторский обзор статьи — единственный источник резюме, встраивается как контекстный префикс каждой секции. Переходно берётся из секции `## Overview` только при миграции. |
```

Add after the `tags` row:

```
| `status` | Опциональное расширение iwiki: `stub` (по умолчанию), `developing`, `stable`, `deprecated`. |
```

Add a paragraph after the reserved-files paragraph (line 241):

```
Страницы больше не содержат секцию `## Overview`: резюме хранится в `description`.
Связи-ссылки размещаются в двух зарезервированных секциях `##` — `## Outgoing links`
(Markdown-ссылки) и `## External links` (голые URL) — которые исключены из поискового
индекса, но по-прежнему питают граф ссылок. Запустите `wiki_export_okf` один раз для
миграции старых страниц (снимает `## Overview`, заполняет `description`, ставит `status`).
```

Append to the `wiki_export_okf` cell (line 257):

```
Также мигрирует каждую страницу к модели тела v2: снимает секцию `## Overview`, заполняя из неё `description` при пустом значении, и ставит `status` в `stub`.
```

- [ ] **Step 6: Bump the version**

`pyproject.toml` line 3:

```toml
version = "0.3.0"
```

`src/iwiki_mcp/__init__.py` line 2:

```python
__version__ = "0.3.0"
```

- [ ] **Step 7: Run the full suite and flake8**

Run: `uv run pytest -q && uv run flake8 src tests`
Expected: PASS — all green, lint clean.

- [ ] **Step 8: Commit**

```bash
git add tests/test_okf_server.py tests/test_resources.py tests/test_server_lint_sync.py src/iwiki_mcp/resources.py README.md docs/README.ru.md pyproject.toml src/iwiki_mcp/__init__.py
git commit -m "docs(okf): v2 authoring rules, README EN/RU schema, bump 0.3.0"
```

---

## Task 7: Wiki upkeep (iwiki MCP tools)

**Files:**
- Wiki pages in the `iwiki-mcp` domain (via the iwiki MCP tools, not `git`): `okf-governance.md`, `indexing.md`.

This task documents the behavior change in the project's own wiki, per the mandatory docs-upkeep rule. It uses the iwiki MCP tools, not file edits.

- [ ] **Step 1: Update the frontmatter-assembly wiki section**

`wiki_update_page(domain="iwiki-mcp", slug="okf-governance", heading="Frontmatter assembly", new_body=<v2 model: description as authored summary source, open type via normalize_type, status field/vocabulary, batch_sweep Overview→description migration>, source="src/iwiki_mcp/okf.py")`

- [ ] **Step 2: Update the chunking wiki section**

`wiki_update_page(domain="iwiki-mcp", slug="indexing", heading="Markdown chunking", new_body=<summary now from frontmatter description; the reserved link sections AND any ## Overview are excluded from the index (Overview never enters the vectors); migration strips ## Overview from the body>, source="src/iwiki_mcp/engine/chunk.py")`

- [ ] **Step 3: Lint the wiki**

`wiki_lint(domain="iwiki-mcp")` — confirm no broken `[[refs]]`, no orphan/stale pages introduced.

- [ ] **Step 4: Final verification (Success Criteria)**

Run: `uv run pytest -q && uv run flake8 src tests`
Confirm against the spec:
- **SC-1:** suite green, flake8 clean.
- **SC-2:** a `description=`-authored page with no `## Overview` chunks correctly (Task 2 tests).
- **SC-3:** reserved sections absent from the index, links present in the graph (Task 6 Step 1 test).
- **SC-4:** `wiki_export_okf` migrates a legacy page and is idempotent (Task 4 Step 6 test).
- **SC-5:** version is `0.3.0`; free-form `type` accepted with only advisory `unknown_type`.

---

## Self-Review

**Spec coverage** (each requirement → task):
- R-01/R-05 description as single authored source → Tasks 2, 4, 5.
- R-06 summary reaches retrieval via chunk prefix → Task 2.
- R-02/R-07 `## Overview` removed, no chunk fallback → Tasks 2, 4 (migration).
- R-03 open `type` (`normalize_type`) → Task 1.
- R-04/R-08 `status` field + vocabulary → Tasks 1, 4, 5.
- R-09/R-10/R-11 reserved link sections excluded from vectors, feed graph → Tasks 1, 2; verified Task 6.
- R-12 validate changes → Task 3.
- R-13 write path preserved (transactional/freshness/in-place refresh untouched) → Task 5.
- R-14 migration in `wiki_export_okf` sweep → Task 4.
- Docs (SC docs, authoring rules, README, version) → Task 6; wiki → Task 7.

**Placeholder scan:** none — every code step shows the exact block; the two integration-test Notes flag a runtime-resolved `wiki_related` node id (adjust-and-rerun), not a code placeholder.

**Type consistency:** `normalize_type` / `normalize_status` / `RESERVED_SECTIONS` / `STATUS_VOCAB` / `DEFAULT_STATUS` (Task 1) are referenced with those exact names in Tasks 2-5; `build_frontmatter`'s new kwargs `explicit_description` / `explicit_status` (Task 4) match the `server.py` call sites (Task 5); `_strip_overview(body, max_chars) -> (new_body, text)` is defined and called only within Task 4.

**Overview-exclusion invariant (user requirement, stricter than the spec's transitional double):** `## Overview` must **never** enter the index — not even for an un-migrated page. `chunk.py` therefore excludes it exactly like the reserved link sections (`excluded = (*RESERVED_SECTIONS, OVERVIEW_HEADING)`), and the summary is sourced only from `description`. The migration (Task 4) additionally strips Overview from the body. NOTE: this tightens the approved spec, whose Body-model/Risks section accepted a transitional `## Overview` double-index — the spec's "treated as an ordinary prose section (indexed)" wording should be reconciled to "excluded from the index like a reserved section".

**Regression coverage (re-review sweep):** because Overview stays excluded from the index, the frontmatter-less `## Overview` fixtures in `tests/test_indexer.py` and `tests/test_server_search.py` still yield a single indexed section (unchanged) — no edits needed there. The tests that break are only the Overview-as-summary ones in `tests/engine/test_chunk.py` (`test_prefix_carries_title_overview_and_lead`, `test_prefix_on_every_subchunk_of_a_split_section`, `test_hash_changes_when_overview_changes`) → rewritten to source the summary from `description` in Task 2 Step 5, while `test_overview_section_is_not_indexed` keeps passing unchanged. The authoring-rules literal `## Overview` in `tests/test_resources.py` + `tests/test_server_lint_sync.py` → asserted against v2 rules in Task 6 Step 3. Verified clear (no hard-coded chunk-count assertion breaks): `test_server_delete` (`indexed_chunks == 0`, page deleted), `test_retrieval` (`chunk == 1`, mocked), `test_indexer_facets` / `test_retrieval_facets` (facet sets, not counts), `test_server_migrate` / `test_okf_artifacts` (no `status`/exact-meta assertions). Code-correctness re-review: 0 BUG; one hardening applied — `validate.py unknown_status` now guards `isinstance(status, str)` so a list-valued `status: [a, b]` cannot crash `normalize_status` (Task 3).
