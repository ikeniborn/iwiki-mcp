---
review:
  spec_hash: b404753f7cccc92e
  last_run: 2026-07-01
  phases:
    structure:   { status: passed }
    coverage:    { status: passed }
    clarity:     { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: null
---
# Delete a stale wiki page

**Date:** 2026-07-01
**Status:** approved (design)
**Topic:** delete-stale-wiki-page

## Problem

The server can create pages (`wiki_write_page`) but never delete one. When a
page's ingest source disappears from disk (the file it was generated from was
deleted or renamed), the page lingers in the domain, in the vector index, and in
search results — stale and no longer backed by anything real. There is no way to
detect this and no way to remove the page.

## Goal

1. **Detect** pages whose recorded ingest source no longer exists — surface them
   in `wiki_lint` as deletion candidates.
2. **Delete** a single page explicitly via a new `wiki_delete_page(domain, slug)`
   tool, cleaning up file + index + log transactionally and committing to git.

Detection and deletion are decoupled: lint only *reports* candidates; an agent or
human decides and calls `wiki_delete_page`. `wiki_sync` is unchanged — it carries
the delete commit to the remote like any other commit.

## Decisions (from brainstorming)

- **Trigger:** explicit call only. `wiki_delete_page` is the sole deletion path.
  lint marks candidates; it never deletes.
- **Candidate signal:** *source gone* only. A page is a candidate iff its recorded
  source (non-empty) cannot be found on disk. `stale` (source modified) and
  `orphan` (no inbound links) keep their existing meaning and are NOT treated as
  deletion signals.
- **Granularity:** page (slug) only. No whole-domain deletion.
- **Sync:** pure git, no detection. The delete commit propagates via `wiki_sync`.
- **Mechanism:** reindex-rebuild + append a `delete` op to the log (Approach A).
- **Source path resolution:** hybrid A1+A2 — absolute sources checked as-is;
  relative sources resolved against `project_dir` (and cwd) before flagging.

## Components

### 1. Tool surface (`server.py`)

New tool, registered alongside the others (`mcp.tool()(wiki_delete_page)` at the
bottom); implementation is a plain `@_safe` function so tests call it directly.

```
wiki_delete_page(domain: str, slug: str) -> dict
```

Success result:
```json
{"deleted": "domain/slug.md", "indexed_chunks": N, "bytes": N, "committed": true}
```

Fail-soft error results (`{"error", "hint"}`):
- domain not found → hint: list/create domain
- page not found → hint: list pages with `wiki_list_pages`
- invalid domain / slug → raised by `_validate_domain` / `_page_path`, surfaced by `@_safe`

Reuses existing guards: `_validate_domain`, `_domain_path`, `_slug_parts`,
`_page_path`, `_contains`. No new path-traversal logic.

### 2. lint detector (`engine/lint.py`)

New report field `missing_source`: list of `{"page", "source"}`, mirroring
`_stale` but with inverted polarity (source *absent* instead of *modified*).

```python
def _latest_ingest_by_page(log_path: str) -> dict[str, str]:
    # scan log.jsonl IN ORDER; keep the LAST record per page.
    # op == "ingest" sets the page's current source; op == "delete" clears the
    # entry. Last-wins so a delete + re-ingest of the same slug is judged by the
    # NEW source, not a stale earlier ingest record.

def _source_exists(src: str, project_dir: str | None) -> bool:
    if os.path.isabs(src):
        return os.path.isfile(src)
    cands = [os.path.join(project_dir, src)] if project_dir else []
    cands.append(src)                      # cwd-relative fallback
    return any(os.path.isfile(c) for c in cands)

def _missing_source(wiki_dir: str, project_dir: str | None) -> list[dict]:
    # from _latest_ingest_by_page(): candidate iff source non-empty
    # AND page .md exists AND NOT _source_exists(source, project_dir)
```

- `lint(wiki_dir, project_dir=None)` gains an optional `project_dir` parameter.
  The module stays stdlib-only (a plain string keeps the config-free contract).
- `wiki_lint` calls `lint(str(_domain_path(...)), project_dir=bind.project_dir)`.
- `source == ""` (page ingested without a source) → skipped, never a candidate.
- **Last-wins dedup (shared).** `_stale` is refactored to consume the same
  `_latest_ingest_by_page` helper, so both detectors pick the latest ingest per
  page. This is required because `wiki_delete_page` makes `ingest → delete →
  ingest` sequences for one slug possible; the old first-hit dedup would judge a
  re-created page by a dead earlier source. `_stale`'s stale-detection semantics
  are otherwise unchanged; its existing tests are updated for the re-create case.
- Report shape becomes
  `{..., "stale": [...], "missing_source": [...], "sections": [...]}`.

### 3. wiki_delete_page transaction (`server.py`)

Symmetric with `wiki_write_page`, with the destructive step (file removal) as the
rollback anchor:

```
1. resolve_binding; _validate_domain; dom_path.is_dir()?        -> else error
2. path = _page_path(...); os.path.isfile(path)?                -> else "not found"
3. content = read(path)                  # captured for rollback
4. os.remove(path)                       # destructive step
5. append_log(op="delete", source="", page=page_file, src_hash=None)
6. index_domain(cfg, base, domain)       # reindex drops the page's records;
                                         # no new chunks => embed_texts NOT called (no network)
7. auto_commit(base, "iwiki: delete <domain/page>", pathspec=domain)
return {"deleted", "indexed_chunks", "bytes", "committed"}
```

Why reindex is free here: `index_domain` only calls `embed_texts` for chunks not
already in the store. Deletion removes files, adds none, so every surviving chunk
is reused and no embedding call is made. The deleted page's records simply fall
out of the rebuilt set.

### 4. Error handling / rollback

- Failure at step 5 or 6 → restore the file (`open(path, "w").write(content)`) and
  drop the appended log line. Extend the existing `_rollback_last_ingest_log` to
  also match `op == "delete"` (or add a sibling `_rollback_last_log`) keyed on
  op + page. Invariant: no orphaned file, log line, or index record — same
  invariant `wiki_write_page` tests already assert.
- Commit (step 7) is best-effort: on failure, do not roll back; return
  `committed: false` (matches `wiki_write_page`).
- `@_safe` catches everything else and returns `{"error", "hint"}`.

## Testing (`_seed` + monkeypatch `embed_texts`)

Tool tests in `tests/test_server_*.py`; detector tests in `tests/engine/test_lint.py`.

`wiki_delete_page` (`tests/test_server_*.py`):
- delete success: file removed; log gains a `delete` record; index rebuilt; the
  page's records are gone from the index.
- delete the last page in a domain → index becomes an empty store (no crash).
- delete nonexistent page / invalid slug / unknown domain → error dict.
- rollback: monkeypatch `index_domain` to raise → file restored, no orphan log line.

lint `missing_source` (`tests/engine/test_lint.py`):
- absolute source that no longer exists → flagged
- existing source → not flagged
- relative source resolved against `project_dir` → correct flag/no-flag
- `source == ""` → not a candidate
- page `.md` absent → not a candidate

last-wins dedup (`tests/engine/test_lint.py`, both detectors):
- `ingest(oldsrc) → delete → ingest(newsrc)` for one slug: `_missing_source` and
  `_stale` judge the page by `newsrc`, not by `oldsrc`.

Follow the existing no-network pattern: monkeypatch `indexer.embed_texts`, set
dummy `IWIKI_*` env vars.

## Docs / versioning

- `README.md`: add a `wiki_delete_page` row to the tool table; extend the
  `wiki_lint` row to mention `missing_source` (source-gone deletion candidates).
- `docs/wiki/`: document the lint `missing_source` field and the
  `wiki_delete_page` tool; update `architecture.md` entry. Then ingest via the
  iwiki MCP tools (`wiki_write_page` + `wiki_index`) and run `wiki_lint`.
- `pyproject.toml`: patch version bump.

## Out of scope (YAGNI)

- Whole-domain deletion (`wiki_delete_domain`).
- Auto-deletion inside lint/sync.
- Soft delete / tombstones / trash.
- Treating `stale` or `orphan` as deletion signals.
- Surgical VectorStore record removal (full reindex is cheaper and reuses code).
