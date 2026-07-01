---
review:
  spec_hash: bc5b2b4fb600122d
  last_run: 2026-07-01
  phases:
    structure:   { status: passed }
    coverage:    { status: passed }
    clarity:     { status: passed }
    consistency: { status: passed }
  findings:
    - id: F-001
      phase: clarity
      severity: WARNING
      section: "Components / 4. commit_and_push"
      section_hash: null
      fragment: "if `committed`, then `sync(base)`. Returns `{committed, pushed, warning?}`"
      text: "commit_and_push return/DoD mapping to sync() result was under-specified"
      fix: "spelled out explicit mapping for committed / not-committed cases"
      verdict: fixed
      verdict_at: 2026-07-01
    - id: F-002
      phase: clarity
      severity: INFO
      section: "Components / 4. commit_and_push"
      section_hash: null
      fragment: "pathspec = domain"
      text: "per-caller pathspec value not restated for all four retrofit callers"
      fix: "added note: all four pass pathspec=<domain>"
      verdict: fixed
      verdict_at: 2026-07-01
    - id: F-003
      phase: consistency
      severity: WARNING
      section: "Components / 3. Ingest-log upsert"
      section_hash: null
      fragment: "restore the prior log bytes"
      text: "rollback must not reuse _rollback_last_ingest_log (last-line only); full-file rewrite needs full-bytes snapshot or the log corrupts"
      fix: "added explicit rollback caveat forbidding _rollback_last_ingest_log reuse"
      verdict: fixed
      verdict_at: 2026-07-01
    - id: F-004
      phase: structure
      severity: INFO
      section: "Approach"
      section_hash: null
      fragment: "see §2 / §3 / §4"
      text: "numbered cross-refs use § notation targeting ### 1..4 headings; all resolve, noted for completeness"
      fix: "no change needed; refs resolve unambiguously"
      verdict: accepted
      verdict_at: 2026-07-01
chain:
  intent: null
---

# Design: `wiki_update_page` + commit-and-push on every mutation

**Date:** 2026-07-01
**Status:** approved (design)
**Topic:** wiki-update-page

## Problem

`wiki_write_page` is create-only: it refuses to overwrite an existing page
(`server.py:261`). The only way to edit page content today is to hand-edit the
`.md` on disk and call `wiki_index`, or delete-then-recreate. There is no tool to
update an existing page through the server with correct source-tracking, indexing,
and git.

Two requirements:

1. Add a tool that edits a **single `##` section** of an existing page, reindexes
   only the affected section, and commits + pushes to git.
2. Ensure **every** wiki mutation (not just this one) is committed **and** pushed.
   Today `wiki_write_page` / `wiki_create_domain` commit locally only; `wiki_index`
   does not commit at all; push happens only via `wiki_sync`.

## Key existing behavior (already correct — do not reinvent)

- **Incremental embedding.** `indexer.index_domain` loads the existing store,
  reuses every chunk whose `hash` is unchanged, and re-embeds only changed chunks
  (`indexer.py:44-55`). "Reindex only the affected section" is therefore already the
  indexer's behavior: editing one `## section` re-embeds only that section's chunks.
- **Chunking splits on `##`.** Section id is `file#heading` (`chunk.py`). The first
  `## Overview` section is the article summary and is excluded from the index; its
  text is prefixed into every other section's chunks.
- **Stale detection reads the ingest log first-hit-wins.** `_stale` dedupes by page
  and the **first** log record for a page wins (`lint.py:104`). A naive append of a
  second ingest record for the same page would leave stale detection using the old
  `src_hash`.
- **Fail-soft git.** `sync.auto_commit` commits (best-effort); `sync.sync` does
  `pull --rebase` + `push` with non-fast-forward retry. A non-repo / missing remote
  degrades to a warning dict, never an exception.

## Approach (chosen: in-place section splice)

Rejected alternatives:

- **Delete + recreate** (read old → edit in memory → delete file + trim log →
  `wiki_write_page` recreates). Two-phase, non-atomic, duplicates rollback logic,
  loses log history, more failure surface.
- **Whole-page overwrite** (`markdown` sent in full, no splice). Pushes the splice
  burden onto the caller; contradicts the section-level requirement. Kept only as a
  conceptual fallback.

## Components

### 1. `wiki_update_page` (server.py)

```python
wiki_update_page(domain: str, slug: str, heading: str, new_body: str,
                 source: str | None = None) -> dict
```

- `heading`: the section heading **text** (no `##`); defensively strip leading
  `#`/whitespace.
- `new_body`: the replacement section body (no `##` heading line).

Transactional flow (with rollback):

1. Resolve binding; run existing `domain`/`slug` path guards; `.iwikiignore` check
   on `source` (same as `wiki_write_page`).
2. Page missing → `{"error": "page '<d>/<s>' not found"}`.
3. Read `original` markdown (kept in memory for rollback).
4. `section.replace_section(original, heading, new_body)` → `new_md` (see §2).
5. `validate_page(new_md)` → reject if any blocking finding
   (`{deep_heading, pre_h2_text}`).
6. Write `new_md` to the file.
7. If `source`: **upsert** the ingest log for this page (see §3). Original log bytes
   kept in memory for rollback.
8. `index_domain` (re-embeds only the changed section's chunks).
9. `commit_and_push` (see §4).
- **Rollback** on any failure in steps 6–8: restore `original` file content and
  restore the prior log bytes, then re-raise (surfaced by `@_safe`).

Return: `{"page", "heading", "indexed_chunks", "reused", "embedded", "bytes",
"over_cap", "committed", "pushed"}`.

Registered at the bottom of `server.py` via `mcp.tool()(wiki_update_page)`; the
implementation function stays plain and unit-testable (existing split convention).

### 2. `engine/section.py` (new, stdlib-only)

```python
class SectionError(ValueError): ...
def replace_section(content: str, heading: str, new_body: str) -> str
```

- Parses `##` with the same `_H2` regex used by `chunk`/`validate`/`lint` (add a
  "keep in sync" comment; this module stays config-free / stdlib-only, no `httpx`).
- Matches the section whose heading equals `heading.strip()`.
  - Not found → `SectionError`.
  - Duplicate heading → `SectionError` (ambiguous target).
- Replaces the slice from the end of the `## H` line up to the next `##` (or EOF)
  with `\n{new_body}\n`, preserving the heading line.
- `server` catches `SectionError` → `{"error", "hint"}`.

Note: editing `## Overview` re-chunks the whole article (the summary is prefixed
into every section's chunks). Allowed, not blocked.

### 3. Ingest-log upsert (indexer.py)

Because `_stale` is first-hit-wins per page, `wiki_update_page` (when `source` is
given) rewrites `log.jsonl`, removing prior `ingest` records for this page and
appending a fresh one carrying the new `src_hash`. Without `source`, the log is left
untouched. The pre-edit log bytes are held in memory so the transaction can restore
them on failure.

Rollback caveat: `wiki_update_page` must **not** reuse `_rollback_last_ingest_log`
(`server.py:210`) — that helper only strips the single last log line and is unaware
of a full-file rewrite. Because the upsert rewrites `log.jsonl` wholesale, rollback
must snapshot the full pre-edit log bytes and write them back verbatim; a last-line
rollback would corrupt the log.

### 4. `commit_and_push` (sync.py) — cross-cutting

```python
def commit_and_push(base, message, pathspec=None) -> dict
```

= `auto_commit(base, message, pathspec)`; if `committed`, then `sync(base)`. Returns
`{committed, pushed, warning?}`. Result mapping (explicit DoD):

- Not committed → `{committed: False, pushed: False, warning: <auto_commit warning>}`;
  `sync` is **not** called.
- Committed → `pushed = sync(base).get("pushed", False)`; any `sync`
  `warning`/`error` is surfaced as `warning`.

Fail-soft: a push failure is a warning; the local commit stands.

Retrofit callers to use it: `wiki_write_page`, `wiki_update_page`,
`wiki_create_domain`, `wiki_index`. All four pass `pathspec=<domain>` (matching the
existing `write_page`/`create_domain` behavior), so each commit is scoped to its
domain subtree. `wiki_index` currently does not commit — it now commits + pushes its
`.iwiki/index.jsonl` change.

## Error handling (all fail-soft via `@_safe`)

| Case | Response |
|---|---|
| page missing | `error: page '<d>/<s>' not found` |
| heading not found / duplicate | `error` + hint |
| blocking structure | `error: section structure invalid` + findings |
| source in `.iwikiignore` | `error` + hint |
| write/log/index failure | rollback file + log, re-raise → `@_safe` |
| push failure | result with `pushed: false` + warning |

## Testing (`_seed` pattern, `embed_texts` monkeypatched)

- `engine/section.py`: replace ok / heading-not-found / duplicate / Overview edit.
- `wiki_update_page`: edits a section + reindexes + upserts log; page-not-found;
  blocking-heading reject; rollback on index failure (file == original, log
  unchanged); `.iwikiignore` rejection.
- `commit_and_push`: `git init` repo → `committed: true`, `sync` monkeypatched to
  assert it is invoked; non-repo → fail-soft (`committed: false`).
- Regression: `write_page` / `create_domain` / `index` tests stay green (base is a
  plain dir in tests → git is a no-op, fail-soft).

## Out of scope / notes

- No section-scoped single-file index: `index_domain` re-reads the domain's files
  each call (cheap) and already re-embeds only changed chunks. A targeted single-file
  index is a possible future optimization, not this change.
- No "create section if missing" mode: update requires an existing section (error
  otherwise). Append/create-section is a possible future extension.

## Follow-up upkeep

- `README.md`: add `wiki_update_page` to the tool list.
- `docs/wiki/architecture.md`: document the update transaction and commit-and-push
  on every mutation.
- iwiki domain `iwiki-mcp`: update the affected page via `wiki_write_page` + lint
  after implementation.
- `pyproject.toml`: patch version bump.
