---
review:
  stage: spec
  spec_hash: 7f1046171e6c79ad
  last_run: 2026-07-10
  phases:
    structure: passed
    coverage: passed
    clarity: passed
    consistency: passed
  findings: []
chain:
  intent: n/a
---
# OKF Unit A — design

**Date:** 2026-07-10
**Branch:** `dev-okf-unit-a` (off `master`, PR into `master`)
**Status:** approved design, pre-implementation

## Context

Testing the okf-v2 server surfaced concrete defects independent of the (now
resolved) stale-process issue. This unit ships the low-risk fixes plus the
whole-domain OKF migration, leaving two design-heavy items to their own later
cycles:

- **Deferred — Unit B:** two-level retrieval (page vector from full
  `description` + clean section vectors without the article-summary prefix +
  two-pass search).
- **Deferred — own unit:** enforce the client/server split for classification.
  The server is a deterministic writer + embedding indexer; it must not call a
  chat LLM internally (only the embedding model). The client agent classifies
  and passes explicit `type`/`tags`; `wiki_migrate_okf` plan mode returns
  candidates for the client to drive. That unit **removes** the server-side
  chat-classification path (`engine/classify.py`, `IWIKI_CHAT_MODEL`, the
  autonomous `wiki_migrate_okf` mode) rather than extending it. MCP sampling is
  explicitly rejected — it would make the server orchestrate the client's LLM.

## Scope (Unit A)

1. **Item 3 — `description` is never truncated** (frontmatter only).
2. **Item 5 — `source`/`resource` is always project-relative**, never absolute.
3. **Item 4 — export cleanup**: drop the orphan `export.pyc`; rewrite the stale
   `okf-export` wiki page to match the current in-place-sweep tool.
4. **Items 2/6 — migrate all 9 domain pages** (frontmatter + `[[wikilink]]` →
   Markdown), with hand-assigned `type`/`tags`.

Out of scope: `chunk.py` article-prefix cap decoupling (Unit B), MCP sampling
(own unit), any retrieval/index-format change.

## Item 3 — description without truncation

**Problem:** `description` is derived from the page `## Overview` and cut at
`summary_max` (400), often mid-word (`...filter results by t`). The description
is the article summary and (today) also each vector's context prefix, so the cut
is real context loss.

**Change (code):**
- `engine/frontmatter.py::derive_description(body, max_chars=400)` — stop
  truncating for the stored description. Return the full whitespace-collapsed
  first-section text. (Make the cap optional / default to no cap; keep the
  signature backward-safe.)
- `okf.py::_strip_overview(body, max_chars)` — the returned `overview_text`
  (used to backfill `description` in `batch_sweep`) is no longer sliced
  `[:max_chars]`.

**Explicitly unchanged:** `chunk.py`'s `article_summary = ...[:summary_max]`
prefix cap stays. On disk `description` is full; the embedding prefix stays
capped as before. Decoupling `summary_max` (frontmatter vs vector) is Unit B.

## Item 5 — project-relative source

**Problem:** `build_frontmatter` writes `resource = source` verbatim and
`append_log` stores it verbatim. Two pages (`architecture.md`,
`installation.md`) carry an absolute `/home/altuser/Документы/...` path from
another machine — it never resolves, so `lint` reports a false `missing_source`.

**Rule:** the server always operates within the bound project; a source is
stored relative to `project_dir`.

**Change (code):**
- New helper `normalize_source(project_dir, source) -> str` (top layer, near the
  write path):
  - absolute path under `project_dir` → make it project-relative;
  - already-relative path → keep as-is;
  - absolute path outside `project_dir` → raise/return an error
    `source outside project` (with a hint) before any file is touched.
- Call it in the write path (`server.py`) before `append_log` and before
  `build_frontmatter`, so both the ingest log and `resource` store the relative
  form.

**Change (data cleanup, one-off):**
- Rewrite `iwiki-personal/iwiki-mcp/.iwiki/log.jsonl`: the two `altuser`
  absolute sources are base-relative self-references with no valid
  project-relative form → **drop the source** (re-ingest the page without a
  `source`, so `resource` disappears). The other eight `src/...` sources are
  already project-relative and are left untouched.

## Item 4 — export cleanup

**Facts:** `wiki_export_okf` was refactored (commit `02712e8`) into an in-place
conformance sweep. It no longer imports `export.py`; it calls `okf.batch_sweep`
+ `okf.refresh_artifacts` + `indexer.index_domain`. The `export.py` source is
gone; only a stale `__pycache__/export.cpython-311.pyc` remains, and the
`okf-export` wiki page still documents the removed `export_domain(dom_path,
dest)` bundle behavior.

**Change:**
- Delete `src/iwiki_mcp/__pycache__/export.cpython-311.pyc`.
- Fix the `okf-export.md` ingest source (`src/iwiki_mcp/export.py`, removed) →
  `src/iwiki_mcp/okf.py` (home of `batch_sweep`).
- Rewrite the `okf-export` wiki page to describe the current tool: in-place
  sweep, no `dest` argument, `batch_sweep` + `refresh_artifacts` + reindex,
  result fields `fixed_links` / `added_frontmatter` / `artifacts` /
  `still_missing_frontmatter` / `still_legacy_wikilink`.

## Items 2/6 — migrate the domain

**Order:** code fixes → data cleanup → `uv tool install --force` → reconnect MCP
in the client → migrate.

**Flow:**
1. `wiki_export_okf` (runs `batch_sweep`): converts every residual `[[...]]` to
   Markdown links (item 6), strips a first-section `## Overview` into the full
   frontmatter `description` (item 3 lands here), backfills frontmatter with the
   `concept` default (item 2), refreshes `index.md` / `log.md`.
2. Per-page `wiki_apply_okf(type, tags)` to set meaningful facets:

   | slug | type | tags |
   |---|---|---|
   | architecture | architecture | architecture, mcp, overview |
   | authoring-and-linting | reference | authoring, lint, validation, links |
   | base-binding | reference | binding, config, domains |
   | git-sync | reference | git, sync, locking |
   | indexing | architecture | indexing, embeddings, chunking, vector-store |
   | installation | guide | installation, setup, mcp |
   | mcp-server | api | mcp, tools, server |
   | okf-export | reference | okf, export, artifacts |
   | retrieval | architecture | retrieval, search, hybrid, vector |

   (`okf-governance` was already migrated during testing.)

## Verification

- `wiki_lint`: `missing_frontmatter: []`, `legacy_wikilink: []`, no `altuser`
  entry under `missing_source`.
- `wiki_search` smoke test returns expected pages.
- `uv run pytest -q` green; `uv run flake8 src tests` clean.

## Tests

- `derive_description`: a long (>400 char) first section is returned in full, not
  truncated.
- `normalize_source`: abs-under-project → relative; relative → unchanged;
  abs-outside-project → error.
- Adjust any existing test that asserts a capped `description` or an absolute
  stored source.

## Docs / version

- Wiki: rewrite `okf-export`; touch `indexing` / `okf-governance` if the
  description/source behavior description changed; `authoring-and-linting` if the
  source rule is worth noting.
- README (EN) and `docs/README.ru.md`: reflect the project-relative-source rule
  and full-description behavior if they affect documented usage.
- `docs/TODO.md`: one row, topic `okf-unit-a`.
- `pyproject.toml`: minor bump `0.3.0` → `0.4.0` (behavioral change: source rule
  + full description).

## Branch / PR

New branch `dev-okf-unit-a` off fresh `master`, no worktree (in place). PR into
`master` via the git-workflow skill.
