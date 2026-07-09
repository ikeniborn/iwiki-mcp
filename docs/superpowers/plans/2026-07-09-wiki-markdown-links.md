---
review:
  plan_hash: b221721a3a1bfb96
  last_run: 2026-07-09
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings:
    - id: F-001
      phase: coverage
      severity: INFO
      section: "Task 4"
      section_hash: null
      fragment: null
      text: "Spec asks to update any server.py tool description mentioning the legacy wiki-link syntax; verified none exist in server.py, so no task is needed."
      fix: "None — confirmed no tool description references the legacy syntax."
      verdict: open
      verdict_at: null
    - id: F-002
      phase: coverage
      severity: INFO
      section: "Task 1"
      section_hash: null
      fragment: null
      text: "Spec's 'two consumers agree on the same input' for slugify is structurally guaranteed (parser and rewriter call the same function); no explicit cross-consumer test added."
      fix: "Optional: add a test asserting parse_links and to_markdown_links agree on one heading input."
      verdict: open
      verdict_at: null
chain:
  intent: n/a
  spec: docs/superpowers/specs/2026-07-09-wiki-markdown-links-design.md
result_check:
  verdict: OK
  plan_hash: b221721a3a1bfb96
  last_run: 2026-07-09
---

# Wiki Markdown Link Graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Obsidian-style `[[slug#Heading]]` wiki-links with CommonMark relative links (`[Heading](slug.md#anchor)`) in iwiki page sources, migrating lazily at write time while the page graph reads both formats throughout the transition.

**Architecture:** One stdlib-only module (`engine/links.py`) gains three functions — `slugify_heading` (GitHub anchor algorithm), a dual-read `parse_links` (markdown links + legacy `[[...]]`, normalized to one `"slug#heading-slug"` shape), and `to_markdown_links` (code-masking rewriter). The write handlers (`wiki_write_page`, `wiki_update_page`) run `to_markdown_links` on the incoming body before validation, so new writes and edited sections migrate automatically. `lint.py` slugifies its heading-existence check and adds an advisory `legacy_wikilink` finding; `related.py` is unchanged.

**Tech Stack:** Python 3.9+ (`from __future__ import annotations`), stdlib `re` only in `links.py`/`lint.py`, pytest (`asyncio_mode=auto`, `pythonpath=["src"]`), `uv` for env/test.

## Global Constraints

- `src/iwiki_mcp/engine/links.py` and `src/iwiki_mcp/engine/lint.py` MUST stay **config-free / stdlib-only** — no `httpx`, no import of `chunk`/`embed`/`config`. `links.py` imports only `re`.
- Every module keeps `from __future__ import annotations` at the top so `str | None` / `dict[str, None]` annotations stay valid on Python 3.9.
- Tests never hit the network: `monkeypatch.setattr(indexer, "embed_texts", ...)`, dummy `IWIKI_*` env vars. Follow `tests/test_server_write.py::_seed`.
- No new MCP tool. Conversion happens only inside the existing write/update handlers.
- v1 stays **within a single domain** — no cross-domain links.
- No linter/formatter is configured — match surrounding style by hand.
- `pyproject.toml` version: **patch** bump `0.1.11` → `0.1.12`.
- The normalized parser contract is exactly `"slug#heading-slug"` (heading portion slugified) or `"slug"` (no heading), de-duplicated, order-preserving by document position.

---

### Task 1: `slugify_heading` helper (`engine/links.py`)

Adds the single shared heading→anchor function that the parser, the rewriter, and lint all call, so the three stay internally consistent and anchors resolve on GitHub.

**Files:**
- Modify: `src/iwiki_mcp/engine/links.py`
- Test: `tests/engine/test_links.py`

**Interfaces:**
- Consumes: nothing (pure, stdlib `re`).
- Produces: `slugify_heading(s: str) -> str` — lowercases; drops every character that is not a word-char, whitespace, or hyphen; collapses whitespace runs to `-`; collapses repeated `-`; strips leading/trailing `-`. Deterministic and idempotent on an already-slugified string.

- [ ] **Step 1: Write the failing tests**

Append to `tests/engine/test_links.py`:

```python
from iwiki_mcp.engine.links import slugify_heading


def test_slugify_lowercases_and_hyphenates():
    assert slugify_heading("Related Sections") == "related-sections"


def test_slugify_strips_punctuation():
    assert slugify_heading("API: the /v1 endpoint!") == "api-the-v1-endpoint"


def test_slugify_collapses_whitespace_and_hyphens():
    assert slugify_heading("Foo   ---  Bar") == "foo-bar"


def test_slugify_is_deterministic_and_idempotent():
    once = slugify_heading("Claude Binary Detection")
    assert once == "claude-binary-detection"
    assert slugify_heading(once) == once
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/engine/test_links.py -k slugify -v`
Expected: FAIL with `ImportError: cannot import name 'slugify_heading'`.

- [ ] **Step 3: Write minimal implementation**

In `src/iwiki_mcp/engine/links.py`, add after the existing regex module-level constants (after the `_INLINE` line):

```python
def slugify_heading(s: str) -> str:
    """Heading text -> GitHub-style anchor slug. Shared by the parser, the
    write-time rewriter, and lint so all three agree on the same anchor.
    Lowercase; drop non-word/space/hyphen chars; whitespace -> '-'; collapse
    repeated '-'. Deterministic and idempotent."""
    s = s.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/engine/test_links.py -k slugify -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/engine/links.py tests/engine/test_links.py
git commit -m "feat(links): add slugify_heading GitHub-anchor helper"
```

---

### Task 2: Dual-read `parse_links` + `has_legacy_wikilink` (`engine/links.py`)

Extends the parser to read **both** markdown links and legacy `[[...]]`, returning a single normalized `"slug#heading-slug"` shape so `related.py`/`lint.py` keep their contract during the transition. Adds a small sibling predicate lint uses to surface un-migrated pages. Also refreshes the module docstring.

**Files:**
- Modify: `src/iwiki_mcp/engine/links.py`
- Test: `tests/engine/test_links.py`

**Interfaces:**
- Consumes: `slugify_heading` (Task 1), existing `_strip_code`, `_LINK`, `_FENCE`, `_INLINE`.
- Produces:
  - `parse_links(content: str) -> list[str]` — unchanged signature; now returns normalized keys `"slug#heading-slug"` or `"slug"`, de-duplicated, ordered by document position. Rejects images, external (`://`, `mailto:`), absolute (`/…`), bare same-page anchors (`#…`), and non-`.md` targets. Strips a leading `./` and the `.md` suffix.
  - `has_legacy_wikilink(content: str) -> bool` — True iff `content` contains a `[[...]]` link outside code (the lint "not yet migrated" marker).

- [ ] **Step 1: Write the failing tests**

First, **update** the existing legacy-heading assertion in `tests/engine/test_links.py`. Replace:

```python
def test_section_ref_target_kept_whole():
    assert parse_links("[[nvm#Claude Binary Detection]]") == ["nvm#Claude Binary Detection"]
```

with:

```python
def test_section_ref_heading_is_slugified():
    assert parse_links("[[nvm#Claude Binary Detection]]") == ["nvm#claude-binary-detection"]
```

Then append the new cases:

```python
from iwiki_mcp.engine.links import has_legacy_wikilink


def test_markdown_link_with_anchor_parsed():
    assert parse_links("See [Flow](auth.md#login-flow) here.") == ["auth#login-flow"]


def test_markdown_link_without_anchor_parsed():
    assert parse_links("[Auth](auth.md)") == ["auth"]


def test_markdown_link_strips_dot_slash_and_md():
    assert parse_links("[x](./guide.md)") == ["guide"]


def test_markdown_image_rejected():
    assert parse_links("![diagram](arch.md)") == []


def test_markdown_external_absolute_anchor_mailto_rejected():
    md = "[a](https://x.md) [b](/abs.md) [c](#local) [d](mailto:x@y.md)"
    assert parse_links(md) == []


def test_markdown_non_md_target_rejected():
    assert parse_links("[code](server.py) and [pdf](doc.pdf)") == []


def test_markdown_link_in_fence_ignored():
    md = "```\n[t](base.md)\n```\nreal [x](real.md)\n"
    assert parse_links(md) == ["real"]


def test_markdown_and_legacy_dedup_by_normalized_key():
    md = "[Bar](foo.md#bar-baz) and [[foo#Bar Baz]]"
    assert parse_links(md) == ["foo#bar-baz"]


def test_has_legacy_wikilink_true_false_and_code():
    assert has_legacy_wikilink("see [[x]] here") is True
    assert has_legacy_wikilink("see [x](x.md) here") is False
    assert has_legacy_wikilink("`[[ $# ]]` in code") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/engine/test_links.py -v`
Expected: `test_section_ref_heading_is_slugified` FAILS (still returns raw heading), the markdown/`has_legacy_wikilink` tests FAIL (`ImportError` / `[]` vs expected).

- [ ] **Step 3: Write minimal implementation**

In `src/iwiki_mcp/engine/links.py`, add the markdown-link regex next to the existing constants (below `_INLINE`):

```python
# Inline markdown link [text](target); leading '!' (image) captured to reject it.
_MD_LINK = re.compile(r"(!?)\[[^\]]*\]\(([^)\s]+)\)")
```

Add two normalization helpers (below `slugify_heading`):

```python
def _md_target_key(target: str) -> str | None:
    """A markdown link target -> normalized 'slug' / 'slug#anchor', or None if it
    is not a same-domain wiki-page edge (external / absolute / anchor / non-.md)."""
    if "://" in target or target.startswith(("mailto:", "/", "#")):
        return None
    path, _, anchor = target.partition("#")
    if path.startswith("./"):
        path = path[2:]
    if not path.endswith(".md"):
        return None
    slug = path[:-3]
    if not slug:
        return None
    return f"{slug}#{anchor}" if anchor else slug


def _legacy_target_key(target: str) -> str:
    """A [[...]] target -> normalized 'slug' / 'slug#heading-slug' (heading slugified
    so legacy and markdown links collapse to the same key)."""
    slug, _, heading = target.strip().partition("#")
    slug = slug.strip()
    if slug.startswith("./"):
        slug = slug[2:]
    if slug.endswith(".md"):
        slug = slug[:-3]
    heading = heading.strip()
    return f"{slug}#{slugify_heading(heading)}" if heading else slug
```

Replace the existing `parse_links` body with the dual-read version:

```python
def parse_links(content: str) -> list[str]:
    """Return every wiki-page edge as a normalized 'slug' / 'slug#heading-slug'
    key, de-duplicated and ordered by document position. Reads both CommonMark
    relative links ([text](slug.md#anchor)) and legacy [[slug#Heading]]. Links
    inside Markdown code (fenced or inline) are ignored."""
    stripped = _strip_code(content)
    hits: list[tuple[int, str]] = []
    for m in _MD_LINK.finditer(stripped):
        if m.group(1):  # leading '!' -> image, not an edge
            continue
        key = _md_target_key(m.group(2))
        if key:
            hits.append((m.start(), key))
    for m in _LINK.finditer(stripped):
        hits.append((m.start(), _legacy_target_key(m.group(1))))
    seen: dict[str, None] = {}
    for _, key in sorted(hits, key=lambda t: t[0]):
        seen.setdefault(key, None)
    return list(seen)


def has_legacy_wikilink(content: str) -> bool:
    """True if content still contains a [[...]] link outside code — the
    lazy-migration 'not yet edited' marker surfaced by lint."""
    return bool(_LINK.search(_strip_code(content)))
```

Finally, update the module docstring (line 1) to reflect dual-read:

```python
"""Parse wiki-page links from markdown — CommonMark relative links and legacy
[[target]] / [[target|alias]] — normalized to one 'slug#heading-slug' key,
ignoring code. Also rewrites [[...]] to markdown links (to_markdown_links)."""
```

- [ ] **Step 4: Run the full links test file to verify it passes**

Run: `uv run pytest tests/engine/test_links.py -v`
Expected: PASS — all prior tests (`test_ignores_fenced_code_block`, `test_ignores_inline_code`, `test_alias_form_returns_target`, `test_dedup_preserves_order`) plus the new markdown/legacy/`has_legacy_wikilink` cases.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/engine/links.py tests/engine/test_links.py
git commit -m "feat(links): dual-read parse_links (markdown + legacy) with normalized keys"
```

---

### Task 3: `to_markdown_links` rewriter (`engine/links.py`)

Adds the pure write-time conversion function: masks code spans, rewrites the four `[[...]]` forms to markdown links, restores code verbatim. Idempotent, so re-running on an already-markdown body is a no-op.

**Files:**
- Modify: `src/iwiki_mcp/engine/links.py`
- Test: `tests/engine/test_links.py`

**Interfaces:**
- Consumes: `slugify_heading` (Task 1), existing `_FENCE`, `_INLINE`.
- Produces: `to_markdown_links(body: str) -> str` — rewrites `[[slug]]`, `[[slug#Heading]]`, `[[slug|Alias]]`, `[[slug#Heading|Alias]]` to `[text](slug.md[#anchor])` where `text = alias ∨ heading ∨ slug` and `anchor = slugify_heading(heading)`. Code (fenced + inline) and existing markdown links are untouched.

- [ ] **Step 1: Write the failing tests**

Append to `tests/engine/test_links.py`:

```python
from iwiki_mcp.engine.links import to_markdown_links


def test_rewrite_plain_slug():
    assert to_markdown_links("see [[core]] now") == "see [core](core.md) now"


def test_rewrite_slug_heading():
    assert to_markdown_links("[[nvm#Claude Binary Detection]]") == \
        "[Claude Binary Detection](nvm.md#claude-binary-detection)"


def test_rewrite_slug_alias():
    assert to_markdown_links("[[core|the core]]") == "[the core](core.md)"


def test_rewrite_slug_heading_alias():
    assert to_markdown_links("[[nvm#Binary Detection|see nvm]]") == \
        "[see nvm](nvm.md#binary-detection)"


def test_bash_wikilike_in_fence_untouched():
    md = "```bash\nif [[ $# -gt 0 ]]; then :; fi\n```\n"
    assert to_markdown_links(md) == md


def test_markdown_example_in_fence_untouched():
    md = "```\n[[core]] renders as [core](core.md)\n```\n"
    assert to_markdown_links(md) == md


def test_inline_code_wikilink_untouched():
    md = "use `[[x]]` literally"
    assert to_markdown_links(md) == md


def test_idempotent_on_markdown_body():
    md = "already [core](core.md) linked"
    assert to_markdown_links(md) == md


def test_idempotent_rerun():
    once = to_markdown_links("[[a#B c]] and [[d]]")
    assert to_markdown_links(once) == once
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/engine/test_links.py -k "rewrite or fence or inline_code or idempotent" -v`
Expected: FAIL with `ImportError: cannot import name 'to_markdown_links'`.

- [ ] **Step 3: Write minimal implementation**

In `src/iwiki_mcp/engine/links.py`, add the full-wikilink regex next to the constants (below `_MD_LINK`):

```python
# Full [[slug#Heading|Alias]] with optional #Heading and |Alias, for rewriting.
_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#([^\]|]+))?(?:\|([^\]]+))?\]\]")
```

Add the rewriter (below `has_legacy_wikilink`):

```python
def to_markdown_links(body: str) -> str:
    """Rewrite the four [[...]] forms to CommonMark relative links, leaving code
    (fenced + inline) and existing markdown links untouched. Idempotent: a body
    with no [[...]] is returned unchanged."""
    masks: list[str] = []

    def _mask(m: re.Match) -> str:
        masks.append(m.group(0))
        return f"\x00{len(masks) - 1}\x00"

    masked = _INLINE.sub(_mask, _FENCE.sub(_mask, body))

    def _rewrite(m: re.Match) -> str:
        slug = m.group(1).strip()
        heading = (m.group(2) or "").strip()
        alias = (m.group(3) or "").strip()
        text = alias or heading or slug
        anchor = f"#{slugify_heading(heading)}" if heading else ""
        return f"[{text}]({slug}.md{anchor})"

    rewritten = _WIKILINK.sub(_rewrite, masked)
    return re.sub(r"\x00(\d+)\x00", lambda m: masks[int(m.group(1))], rewritten)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/engine/test_links.py -v`
Expected: PASS (all links tests, including the rewriter cases).

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/engine/links.py tests/engine/test_links.py
git commit -m "feat(links): add to_markdown_links code-masking rewriter"
```

---

### Task 4: Wire normalization into the write handlers (`server.py`)

Runs `to_markdown_links` on the incoming body inside `wiki_write_page` (whole page) and `wiki_update_page` (the edited section) before validation and persistence, so stored + re-indexed pages already carry markdown links. Lazy migration falls out for free; the existing transactional write/rollback is unchanged.

**Files:**
- Modify: `src/iwiki_mcp/server.py`
- Test: `tests/test_server_write.py`, `tests/test_server_update.py`

**Interfaces:**
- Consumes: `to_markdown_links` (Task 3).
- Produces: no signature change. `wiki_write_page` persists `to_markdown_links(markdown)`; `wiki_update_page` persists a section whose body is `to_markdown_links(new_body)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_server_write.py`:

```python
def test_write_normalizes_wikilinks_to_markdown(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch)
    md = "# Auth\n## Overview\nsummary\n## Flow\nsee [[core#Token Store]] here\n"
    server.wiki_write_page("backend", "auth", md)
    content = open(os.path.join(b, "backend", "auth.md"), encoding="utf-8").read()
    assert "[Token Store](core.md#token-store)" in content
    assert "[[core#Token Store]]" not in content
```

Append to `tests/test_server_update.py`:

```python
def test_update_normalizes_wikilinks_in_edited_section(tmp_path, monkeypatch):
    b, _ = _seed(tmp_path, monkeypatch)
    _write(BASE_MD)
    server.wiki_update_page("backend", "auth", "Flow", "see [[core|the core]] now")
    content = open(os.path.join(b, "backend", "auth.md"), encoding="utf-8").read()
    assert "[the core](core.md)" in content
    assert "[[core|the core]]" not in content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server_write.py::test_write_normalizes_wikilinks_to_markdown tests/test_server_update.py::test_update_normalizes_wikilinks_in_edited_section -v`
Expected: FAIL — stored content still contains `[[...]]`.

- [ ] **Step 3: Write minimal implementation**

In `src/iwiki_mcp/server.py`, add the import to the existing engine-import group (after line 19, `from .engine.section import SectionError, replace_section`):

```python
from .engine.links import to_markdown_links
```

In `wiki_write_page`, insert normalization immediately before the `blocking = [...]` validation line (currently line 324). The block becomes:

```python
    markdown = to_markdown_links(markdown)
    blocking = [f for f in validate_page(markdown) if f.get("type") in _BLOCKING]
```

In `wiki_update_page`, insert normalization immediately before the `try:`/`replace_section` call (currently lines 418-419). The block becomes:

```python
    new_body = to_markdown_links(new_body)
    try:
        new_md = replace_section(original, heading, new_body)
    except SectionError as e:
        return {"error": str(e), "hint": "check the heading with wiki_read_page"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_server_write.py tests/test_server_update.py -v`
Expected: PASS — the two new normalization tests plus all existing write/update tests.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/server.py tests/test_server_write.py tests/test_server_update.py
git commit -m "feat(server): normalize wiki-links to markdown on write and update"
```

---

### Task 5: Slugified heading check + `legacy_wikilink` finding (`engine/lint.py`)

Makes the broken-ref `#heading` check compare slug-to-slug (the parser now returns a slugified heading), and adds an advisory `legacy_wikilink` list of pages still containing `[[...]]` — a lazy-migration progress indicator, not a broken finding.

**Files:**
- Modify: `src/iwiki_mcp/engine/lint.py`
- Test: `tests/engine/test_lint.py`

**Interfaces:**
- Consumes: `parse_links`, `slugify_heading`, `has_legacy_wikilink` (Tasks 1-2).
- Produces: `lint(...)` return dict gains key `"legacy_wikilink": list[str]` (sorted page paths). The `#heading` broken-ref check slugifies the target's heading set before comparing.

- [ ] **Step 1: Write the failing tests**

Append to `tests/engine/test_lint.py`:

```python
def test_broken_markdown_link_flagged(tmp_path):
    wd = _wiki(tmp_path, {"a.md": "## A\nlink [x](missing.md) here\n"})
    out = lint(wd)
    assert any(b["ref"] == "missing" for b in out["broken"])


def test_valid_markdown_anchor_matches_via_slug(tmp_path):
    wd = _wiki(tmp_path, {
        "a.md": "## A\nsee [B](b.md#the-section)\n",
        "b.md": "## The Section\nbody\n",
    })
    assert lint(wd)["broken"] == []


def test_broken_markdown_anchor_flagged(tmp_path):
    wd = _wiki(tmp_path, {
        "a.md": "## A\nsee [B](b.md#no-such)\n",
        "b.md": "## The Section\nbody\n",
    })
    assert any(b["ref"] == "b#no-such" for b in lint(wd)["broken"])


def test_legacy_wikilink_lists_only_unmigrated_pages(tmp_path):
    wd = _wiki(tmp_path, {
        "a.md": "## A\nold [[b]] link\n",
        "b.md": "## B\nnew [x](a.md) link\n",
    })
    assert lint(wd)["legacy_wikilink"] == [
        os.path.normpath(os.path.join(wd, "a.md"))
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/engine/test_lint.py -k "markdown or legacy_wikilink" -v`
Expected: FAIL — `KeyError: 'legacy_wikilink'` and/or the anchor cases mismatch (raw-vs-slug comparison).

- [ ] **Step 3: Write minimal implementation**

In `src/iwiki_mcp/engine/lint.py`, change the import (line 16) from:

```python
from .links import parse_links
```

to:

```python
from .links import parse_links, slugify_heading, has_legacy_wikilink
```

In the `lint(...)` broken-ref loop, replace the `#heading` existence check (currently lines 178-186):

```python
            if heading:
                hs = headings.get(target)
                if hs is None:  # target exists but outside the page set
                    try:
                        hs = _headings(open(target, encoding="utf-8").read())
                    except Exception:
                        hs = set()
                if heading.strip() not in hs:
                    broken.append({"page": page, "ref": ref})
```

with:

```python
            if heading:
                hs = headings.get(target)
                if hs is None:  # target exists but outside the page set
                    try:
                        hs = _headings(open(target, encoding="utf-8").read())
                    except Exception:
                        hs = set()
                if heading not in {slugify_heading(h) for h in hs}:
                    broken.append({"page": page, "ref": ref})
```

Then, just before the `orphans = ...` line (currently line 188), compute the advisory list:

```python
    legacy_wikilink = sorted(p for p, c in content.items() if has_legacy_wikilink(c))
```

and add it to the return dict (currently lines 191-194):

```python
    return {"wiki_present": True, "pages": len(pages),
            "broken": broken, "orphans": orphans, "stale": _stale(wiki_dir),
            "missing_source": _missing_source(wiki_dir, project_dir),
            "legacy_wikilink": legacy_wikilink,
            "sections": sections}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/engine/test_lint.py -v`
Expected: PASS — new markdown/legacy cases plus every existing lint test (`test_detects_broken_ref`, `test_code_fence_ref_not_broken`, `test_detects_orphan`, all stale/missing_source cases).

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/engine/lint.py tests/engine/test_lint.py
git commit -m "feat(lint): slugify heading check; add legacy_wikilink advisory"
```

---

### Task 6: `related.py` regression test (no production change)

Proves `_graph_neighbours` traverses a markdown-link graph identically to a legacy `[[...]]` graph, locking in the "`related.py` unchanged" guarantee from the spec.

**Files:**
- Test: `tests/engine/test_related.py`
- Verify unchanged: `src/iwiki_mcp/engine/related.py` (do **not** edit)

**Interfaces:**
- Consumes: `_graph_neighbours` (existing) via the dual-read `parse_links` (Task 2).
- Produces: nothing — regression coverage only.

- [ ] **Step 1: Write the regression test**

Append to `tests/engine/test_related.py`:

```python
def test_graph_neighbours_identical_for_markdown_and_legacy(tmp_path, monkeypatch):
    # legacy [[...]] chain a -> b -> c
    (tmp_path / "leg_a.md").write_text("[[leg_b]]\n", encoding="utf-8")
    (tmp_path / "leg_b.md").write_text("[[leg_c]]\n", encoding="utf-8")
    (tmp_path / "leg_c.md").write_text("## C\n", encoding="utf-8")
    # markdown chain of the same shape
    (tmp_path / "md_a.md").write_text("[b](md_b.md)\n", encoding="utf-8")
    (tmp_path / "md_b.md").write_text("[c](md_c.md)\n", encoding="utf-8")
    (tmp_path / "md_c.md").write_text("## C\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert set(_graph_neighbours("leg_a.md", depth=2)) == {"leg_b", "leg_c"}
    assert set(_graph_neighbours("md_a.md", depth=2)) == {"md_b", "md_c"}
```

- [ ] **Step 2: Run the test to verify it passes**

Run: `uv run pytest tests/engine/test_related.py::test_graph_neighbours_identical_for_markdown_and_legacy -v`
Expected: PASS — the markdown chain yields `{"md_b", "md_c"}`, identical shape to legacy, with **no edit to `related.py`**.

- [ ] **Step 3: Commit**

```bash
git add tests/engine/test_related.py
git commit -m "test(related): regression — markdown graph edges match legacy"
```

---

### Task 7: Authoring rules, README, and version bump

Updates the human/agent-facing docs to the markdown link syntax, fixes the `test_resources.py` assertion that hard-codes `[[`, keeps `README.md` + `docs/README.ru.md` in sync, and patch-bumps the version.

**Files:**
- Modify: `src/iwiki_mcp/resources.py:19`
- Modify: `tests/test_resources.py`
- Modify: `README.md:271`
- Modify: `docs/README.ru.md:267`
- Modify: `pyproject.toml:3`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing (docs/version only).

- [ ] **Step 1: Update the `test_resources.py` assertion (it hard-codes `[[`)**

In `tests/test_resources.py`, replace the body of `test_authoring_rules_cover_section_format`:

```python
def test_authoring_rules_cover_section_format():
    text = AUTHORING_RULES.lower()
    assert "## overview" in text
    assert "[[" in AUTHORING_RULES
    assert "##" in AUTHORING_RULES
```

with:

```python
def test_authoring_rules_cover_section_format():
    text = AUTHORING_RULES.lower()
    assert "## overview" in text
    assert "](slug.md#heading)" in AUTHORING_RULES
    assert "[[" not in AUTHORING_RULES
    assert "##" in AUTHORING_RULES
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_resources.py -v`
Expected: FAIL — `AUTHORING_RULES` still contains `[[slug#Heading]]`.

- [ ] **Step 3: Update the authoring rule in `resources.py`**

In `src/iwiki_mcp/resources.py`, replace line 19:

```python
- Cross-link related pages with `[[slug#Heading]]` (within the same domain in v1).
```

with:

```python
- Cross-link related pages with `[Heading](slug.md#heading)` (within the same domain in v1).
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_resources.py -v`
Expected: PASS.

- [ ] **Step 5: Update `README.md`**

In `README.md`, replace line 271:

```markdown
- Wiki links are intra-domain: use `[[slug#Heading]]` within the same domain.
```

with:

```markdown
- Wiki links are intra-domain: use `[Heading](slug.md#heading)` within the same domain.
```

- [ ] **Step 6: Update `docs/README.ru.md`**

In `docs/README.ru.md`, replace line 267:

```markdown
- Wiki-ссылки внутридоменные: используйте `[[slug#Heading]]` в пределах одного домена.
```

with:

```markdown
- Wiki-ссылки внутридоменные: используйте `[Heading](slug.md#heading)` в пределах одного домена.
```

- [ ] **Step 7: Patch-bump the version**

In `pyproject.toml`, replace line 3:

```toml
version = "0.1.11"
```

with:

```toml
version = "0.1.12"
```

- [ ] **Step 8: Verify no stray legacy syntax remains in the authoring surfaces**

Run:

```bash
grep -rn '\[\[slug#Heading\]\]' src/ README.md docs/README.ru.md
```

Expected: no output (exit non-zero from grep is fine — it means no matches).

- [ ] **Step 9: Commit**

```bash
git add src/iwiki_mcp/resources.py tests/test_resources.py README.md docs/README.ru.md pyproject.toml
git commit -m "docs: switch link syntax to markdown; bump version to 0.1.12"
```

---

### Task 8: Full-suite verification

Confirms the whole change set is green together and no unrelated test regressed.

**Files:** none (verification only).

- [ ] **Step 1: Run the complete test suite**

Run: `uv run pytest -q`
Expected: PASS — entire suite, no failures, no errors.

- [ ] **Step 2: Confirm the config-free contract still holds**

Run:

```bash
uv run python -c "import iwiki_mcp.engine.links, iwiki_mcp.engine.lint; import sys; assert 'httpx' not in sys.modules, 'links/lint pulled httpx'; print('config-free OK')"
```

Expected: `config-free OK`.

---

## Self-Review

**Spec coverage:**
- Parser dual-read (markdown + legacy, rejections, `.md`/`./` stripping, code stripping, dedup/order) → Task 2. ✓
- `slugify_heading` (GitHub algorithm, shared by parser/rewrite/lint) → Task 1. ✓
- `to_markdown_links` (four forms, code masking, idempotent) → Task 3. ✓
- Write-time normalization wired into `wiki_write_page`/`wiki_update_page` → Task 4. ✓
- `lint.py` slugified heading check + `legacy_wikilink` advisory → Task 5. ✓
- `related.py` unchanged + regression → Task 6. ✓
- Authoring rules, README (EN+RU), version bump → Task 7. ✓
- Contract change to existing `test_links.py` heading assertion → Task 2, Step 1. ✓
- `test_resources.py` `[[` assertion updated → Task 7, Step 1. ✓

**Placeholder scan:** every code/test step contains complete content; no TBD/"handle edge cases"/"similar to Task N". ✓

**Type consistency:** `slugify_heading(str)->str`, `parse_links(str)->list[str]`, `has_legacy_wikilink(str)->bool`, `to_markdown_links(str)->str`, `_md_target_key(str)->str|None`, `_legacy_target_key(str)->str` — names/signatures identical across Tasks 1-6. `lint(...)` adds `legacy_wikilink` key, referenced consistently. ✓

**Note for the implementer:** `src/iwiki_mcp/engine/related.py` is intentionally **not** edited (Task 6 verifies its behaviour). Its docstrings still mention `[[refs]]`; per the spec's "unchanged (verified)" decision, leave them — do not opportunistically reword.
