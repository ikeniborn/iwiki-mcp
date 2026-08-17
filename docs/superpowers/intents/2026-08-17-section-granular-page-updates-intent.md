# Intent: section-granular-page-updates

**Date:** 2026-08-17
**Status:** approved

## Objective
`wiki_update_page` already accepts a single `##` section body on input, but every
layer below it works per page or per domain: the Postgres backend rewrites the
whole `markdown` column and `_replace_derived` deletes and re-embeds every chunk
of the page; there is no section-scoped read; adding, removing, or reordering a
section requires `wiki_delete_page` + `wiki_write_page`; and optimistic
concurrency (`expected_revision`) is page-scoped, so two agents editing
different sections of the same page collide. Narrow the expensive operations
(re-embed, rewrite, lock) down to the section actually touched.

## Desired Outcomes
- Editing one section in Postgres leaves the chunks of every other section of
  the same page untouched (chunk ids/hashes identical before and after).
- `wiki_read_page` can return a single section without the whole markdown.
- A `##` section can be inserted, deleted, or moved without
  `wiki_delete_page` + `wiki_write_page`.
- Two concurrent updates to different sections of the same page both succeed;
  a concurrent update to the *same* section conflicts.

## Health Metrics
- Markdown remains the single source of truth; the Git backend and its
  behavior are unchanged.
- `wiki_lint` reports no new findings after the change.
- Existing `wiki_update_page` / `wiki_read_page` calls (no new parameters)
  behave exactly as before — no breaking change to current signatures.
- Page edit latency does not regress versus current behavior.

## Strategic Context
- Interacts with: `src/iwiki_mcp/postgres/store.py` (`update_page`,
  `_replace_derived`, `_prepare_page`), `src/iwiki_mcp/server.py`
  (`wiki_update_page`, `wiki_read_page`, new tool registration),
  `src/iwiki_mcp/engine/section.py`, `src/iwiki_mcp/engine/links.py`
  (cross-domain link rewrite on rename).
- Explicitly not touched: the Git backend (`indexer.index_domain`,
  `stage_graph_pages`, `finalize_graph_mutation`) — this intent targets the
  Postgres storage path only.
- Priority trade-off: **trust** — data/CAS integrity over edit speed or
  implementation cost.

## Constraints
### Steering (behavioral guidance)
- Follow the existing fail-soft style (`@_safe`) and dict errors with `hint`.
- Do not touch the Git backend (`indexer.index_domain`) in this intent.

### Hard (architectural enforcement)
- Markdown remains the single source of truth; no separate `sections` table
  (that is option E, out of scope).
- `expected_revision` stays mandatory on Postgres mutations
  (`expected_revision_required()`); section-level CAS is additive, not a
  replacement.
- No breaking changes to the current `wiki_update_page` / `wiki_read_page`
  signatures — new parameters are optional only.
- Section operations (insert/delete/move) must pass the same validation
  guards as `replace_section` (`_BLOCKING` findings, anchor collision check).

## Autonomy Zones
- Full autonomy (reversible, low risk): refactor `_replace_derived` to a
  chunk-hash diff instead of delete-all/re-embed-all (A); add an optional
  `heading` parameter to `wiki_read_page` (B); tests for both.
- Guarded (log + confidence threshold): section-level CAS field/format (D),
  if it fits the already-accepted `expected_revision` pattern.
- Proposal-first (needs approval): new tool signatures for section
  insert/delete/move (C) — names, parameters, collision behavior — shown
  before implementation.
- No autonomy (human only): any move toward "sections as first-class stored
  entities" (option E, already excluded) — if implementation reveals this is
  needed, stop and escalate rather than silently widening scope.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules
- Halt if: C or D turns out to require a Git-backend change or a database
  schema change beyond new columns/indexes on existing tables.
- Escalate if: the `_replace_derived` diff logic conflicts with the current
  `_bump_markdown_generation` / graph fingerprint invariants.
- Done when: all 4 Desired Outcomes are verified against real calls (not
  just unit tests), `wiki_lint` reports no new findings, and the existing
  test suite is green.
