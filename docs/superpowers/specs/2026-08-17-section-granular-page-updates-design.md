# Design: section-granular-page-updates

**Date:** 2026-08-17
**Status:** draft
**Intent:** [docs/superpowers/intents/2026-08-17-section-granular-page-updates-intent.md](../intents/2026-08-17-section-granular-page-updates-intent.md)

## Acceptance (from intent)

Desired Outcomes (verbatim from the approved intent):
- Editing one section in Postgres leaves the chunks of every other section of
  the same page untouched (chunk ids/hashes identical before and after).
- `wiki_read_page` can return a single section without the whole markdown.
- A `##` section can be inserted, deleted, or moved without
  `wiki_delete_page` + `wiki_write_page`.
- Two concurrent updates to different sections of the same page both succeed;
  a concurrent update to the *same* section conflicts.

Done when: all 4 Desired Outcomes are verified against real calls, `wiki_lint`
reports no new findings, and the existing test suite is green.

## 1. Architecture

Storage stays page-scoped (markdown is the single source of truth — hard
constraint of the intent; no `sections` table). What changes is the *cost* and
*addressability* of one-section operations, in the Postgres backend only. The
Git backend (`indexer.index_domain`, `stage_graph_pages`) is untouched.

Four independent slices, ordered by dependency:

1. **Section-parsing helper** (`engine/section.py`) — a shared `list_sections`
   used by every op below, plus one shared collision/validation helper.
2. **B — section read** — `wiki_read_page(domain, slug, heading=None)`.
3. **C — section insert/delete/move** — three new tools built on the parsing
   helper, reusing the existing write-transaction shape.
4. **A — incremental chunk reuse** — a chunk-hash diff inside
   `_replace_derived`, transparent to every write path above it (update,
   insert, delete, move all benefit automatically once this lands).
5. **D — section-level CAS** — an optional `expected_section_hash` argument on
   every section-mutating tool, layered on top of the mandatory
   `expected_revision`.

A and D are backend-internal / additive-parameter changes with no new public
shape; B and C are the two places the public tool surface grows.

## 2. Components

### 2.1 `engine/section.py::list_sections` (new)

```python
@dataclass(frozen=True)
class Section:
    heading: str
    body: str        # raw body text, not stripped of surrounding blank lines
    start: int        # offset of the "## " line
    body_start: int    # offset right after the heading line
    body_end: int      # offset of the next "##" or EOF
```

`list_sections(content: str) -> list[Section]` — one pass over `_H2`, same
regex already in `section.py`. `replace_section` is refactored to call
`list_sections` + a lookup by heading text instead of its own `finditer` loop;
its external behavior (including the `SectionError` messages) is unchanged —
covered by the existing `test_server_update.py` suite, which must stay green
unmodified.

A shared `_locate(sections, heading) -> int` returns the matched index or
raises `SectionError` for "not found" / "ambiguous", reused by read, replace,
delete, and move (insert uses a different, position-based lookup — see 2.3).

### 2.2 B — section-scoped read

`wiki_read_page(domain: str, slug: str, heading: str | None = None) -> dict`

- `heading=None` — unchanged: full markdown, both backends, exactly today's
  contract (regression-tested by the existing `test_server_write.py` /
  `test_cross_domain_update.py` calls that omit `heading`).
- `heading="X"` — locate the section via `list_sections` + `_locate` (postgres:
  after `_fm.split`; git: after `_fm.split` on the file content). Return
  `{"domain", "slug", "heading", "body": <exact section body>}`. Reuses
  `SectionError` → the same `{"error", "hint": "check the heading with
  wiki_read_page"}` shape already used by `wiki_update_page`.
- Both backends implement it directly (no new store method needed on the Git
  side — it is a pure function over content already read from disk).

### 2.3 C — section insert / delete / move

Three new tools, mirroring the existing `wiki_update_page` /
`wiki_delete_page` shape (fail-soft `@_safe`, `expected_revision` required on
Postgres, transactional rollback on the Git path):

```python
def wiki_insert_section(
    domain, slug, heading, body,
    after_heading: str | None = None,   # None = append at end of page
    before_heading: str | None = None,  # mutually exclusive with after_heading
    source=None, expected_revision=None,
) -> dict

def wiki_delete_section(
    domain, slug, heading,
    source=None, expected_revision=None,
) -> dict

def wiki_move_section(
    domain, slug, heading,
    after_heading: str | None = None,
    before_heading: str | None = None,  # mutually exclusive
    expected_revision=None,
) -> dict
```

New pure functions in `engine/section.py`, each operating on the full
markdown body and returning the rewritten body (same shape as
`replace_section`):

- `insert_section(content, heading, body, *, after=None, before=None)` —
  validates `heading` does not collide with an existing anchor (reuses the
  anchor-collision loop already in `replace_section`), validates the new
  section body the same way `wiki_update_page` does (`_BLOCKING` findings:
  `deep_heading`, `pre_h2_text`), locates the anchor point via `_locate`, and
  splices in `## {heading}\n{body}\n\n`. `after=None and before=None` appends
  at EOF. Both `after` and `before` set → `SectionError`.
- `delete_section(content, heading)` — locates via `_locate`, removes the
  section's full span (`start` to `body_end`). Refuses to delete a reserved
  heading (`RESERVED_SECTIONS`, `OVERVIEW_HEADING`) — those are structural,
  not content sections; same restriction `replace_section` already has
  implicitly via `_BLOCKING`/reserved-section handling in `validate.py`.
  Refuses to delete the last remaining section (a page needs ≥1 section —
  mirrors `wiki_write_page`'s requirement that a page is never bodyless).
- `move_section(content, heading, *, after=None, before=None)` — locates the
  section, removes its span, re-locates the anchor point in the *remaining*
  content (so moving a section next to itself is a no-op, and self-reference
  as the anchor is rejected as `SectionError`), re-inserts. No heading rename,
  no body change — pure reorder.

Server wiring for all three follows `wiki_update_page`'s existing pattern
exactly: Postgres branch (`_prepare_page` + `store.update_page` with the new
body), Git branch (validate → write file → `indexer.upsert_ingest_log` if
`source` → `indexer.index_domain` → commit, with the same rollback-on-exception
try/except already in `wiki_update_page`). `wiki_delete_section` is a distinct
tool name from `wiki_delete_page` — deletes a `##` span, not the page.

### 2.4 A — incremental chunk reuse (`postgres/store.py::_replace_derived`)

Today: `DELETE FROM iwiki.chunks WHERE page_id = ...` then re-insert every
chunk from `records`, unconditionally re-embedding all of them (embedding
happens earlier, in `_prepare_page` → `indexer.prepare_page_records`, which
has no reuse logic at all — unlike `indexer.index_domain`'s per-chunk hash
check on the Git path).

New flow, mirroring `indexer.index_domain`'s reuse loop:

1. `_prepare_page` (or a new `_prepare_page_incremental`) chunks the markdown
   with `chunk_markdown` (no embedding yet) and, in the same transaction,
   reads `(section_id, embedding, quantization_scale, quantized_embedding)`
   for the page's existing chunks (`section_id` already carries `hash` per
   `_record_metadata`).
2. For each new chunk: if an existing row has the same `(heading, chunk index,
   hash)` (parsed back out of the existing `section_id` JSON), reuse its
   stored `scale`/`q`/`embedding` instead of calling the embedder — same
   comparison `indexer.index_domain` does via `Record.hash`.
3. Only chunks that are new or changed go through `self._embedder`.
4. `_replace_derived` replaces its unconditional `DELETE` + full re-insert
   with: delete rows for headings/chunks no longer present, update or insert
   the rest.

This is entirely internal to `_prepare_page` / `_replace_derived` — no
signature change to `update_page`, `write_page`, or any tool. It benefits
every write on the Postgres path (`wiki_write_page`, `wiki_update_page`, and
the new insert/delete/move tools) automatically, once landed.

### 2.5 D — section-level CAS

New optional parameter `expected_section_hash: str | None = None` on
`wiki_update_page`, `wiki_delete_section`, and `wiki_move_section` (not
`wiki_insert_section` — there is no prior section state to pin).

- Hash function: reuse `Chunk.hash`'s algorithm (`sha256(text)[:16]` from
  `engine/chunk.py`), applied to the *raw section body* (not the chunked
  text) so it is computable from `list_sections` output alone, no
  chunking/embedding dependency.
- When provided: after locating the target section via `list_sections` and
  before applying the mutation, compare
  `sha256(current_section.body.strip())[:16]` (well-defined normalization —
  same `.strip("\n")` convention `replace_section` already applies to
  `new_body`) to `expected_section_hash`. Mismatch → a new error shape
  distinct from `expected_revision`'s `revision_conflict`:
  `{"error": "section_conflict", "current_section_hash": "...", "hint": "re-read the section with wiki_read_page and retry"}`.
- `expected_revision` stays mandatory (hard constraint from the intent) and is
  still checked by `store.update_page`'s existing `UPDATE ... WHERE revision =
  %s` — that check still guards the whole-page write. The section-hash check
  is a *pre-check* run before the page-level CAS statement executes, so a
  page-revision race on an *unrelated* section still lets the caller retry
  with a fresh `expected_revision` while the section-hash pre-check passes
  (their target section is unchanged).
- `wiki_read_page(..., heading=...)` returns `section_hash` in its response so
  a caller can round-trip read → hash → write without recomputing anything
  itself.

## 3. Data flow

```
wiki_read_page(domain, slug, heading="Flow")
  -> store.read_page -> _fm.split -> list_sections -> _locate("Flow")
  -> {domain, slug, heading, body, section_hash}

wiki_update_page(domain, slug, heading="Flow", new_body=..., expected_revision=N,
                  expected_section_hash="abc123...")
  -> store.read_page (current markdown)
  -> list_sections -> _locate("Flow") -> compare hash -> mismatch? section_conflict : continue
  -> replace_section -> validate_page (_BLOCKING)
  -> _prepare_page (chunk + incremental-diff embed, see 2.4)
  -> UPDATE ... WHERE revision = N -> row? proceed : revision_conflict
  -> _replace_derived (diff-based chunk/link replace)
  -> commit
```

Insert/delete/move follow the same shape with their own section-transform
function in place of `replace_section`.

## 4. Error handling

- All new failure modes return the existing fail-soft dict shape
  (`{"error", "hint"}`), never raise past `@_safe`.
- `SectionError` subclasses / reasons already established
  (`not found`, `ambiguous`, structure-invalid) are reused verbatim by
  insert/delete/move where applicable; new reasons (`both after and before
  given`, `cannot delete last section`, `cannot delete reserved section`,
  `move target is the section itself`) are new `SectionError` messages, no
  new exception type.
- `section_conflict` (D) is a new top-level error `type`, parallel to
  `revision_conflict`, so callers already branching on `result.get("error")`
  will not silently misread the wrong error as their own free-form message.
- No new blocking validator findings — insert/delete/move reuse
  `validate_page`'s existing `_BLOCKING` set (`deep_heading`, `pre_h2_text`)
  on the *resulting whole-page markdown*, same as `wiki_update_page` today.

## 5. Testing

- `engine/section.py`: unit tests for `list_sections`, `insert_section`,
  `delete_section`, `move_section` — happy path, ambiguous heading, missing
  heading, reserved-section rejection, last-section rejection, self-move
  rejection, anchor collision on insert.
- `wiki_read_page(..., heading=...)`: found / not-found / ambiguous, both
  backends, `section_hash` present and matches `wiki_update_page`'s CAS
  compare.
- New Postgres tool tests (`test_server_update.py` pattern): insert, delete,
  move, each with and without `expected_revision` mismatch.
- `_replace_derived` diff: a test that edits one section and asserts the
  `iwiki.chunks` rows for *other* sections keep the same `chunk_id` /
  `embedding` value (not just equal content) across the update — the direct
  check for Desired Outcome 1. Use the existing `monkeypatch`'d embedder from
  `tests/postgres/test_store.py` and assert the embedder is *not* called for
  unchanged chunks (call-count assertion), not just that output matches.
- `expected_section_hash`: two concurrent updates to different sections of
  one page both succeed (Desired Outcome 4); two concurrent updates to the
  same section — the second's stale `expected_section_hash` is rejected with
  `section_conflict`.
- Full existing suite (`uv run pytest -q`) stays green unmodified — no test in
  the current suite calls a signature this design changes in a
  backward-incompatible way (all new parameters are optional with defaults
  matching current behavior).

## Out of scope

- Option E (sections as first-class stored entities, a `sections` table,
  markdown-as-derived-view) — explicitly excluded by the intent's hard
  constraints and its "No autonomy" zone.
- Any change to the Git backend (`indexer.index_domain`, graph staging,
  `sync.py`) — intent's steering constraint.
- Chunk-level diff for the Git backend's `indexer.index_domain` — it already
  has hash-based reuse; only the Postgres path lacks it.
