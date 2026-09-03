# Intent: derived-links-must-not-pin-pages

**Date:** 2026-09-03
**Status:** approved

## Objective

A page that carried a `code` selector at the moment of any publication can never be
deleted again. The derived `DOCUMENTED_BY` rows of the superseded snapshot still reference
it, and `code_graph_wiki_links_page_fk` is declared `ON DELETE NO ACTION`, so
`DELETE FROM iwiki.pages` is refused. The user reproduced it end to end: a probe page
deleted cleanly before publication, refused deletion after it, and still refused after the
selector was cleared and the graph republished twice. Three pages are undeletable today.

The schema contradicts its own documentation. `concept/code-graph-wiki-linking` already
states that removed pages "lose their derived links through rebuilding or schema
cascades", and the two sibling foreign keys of the same table are already
`ON DELETE CASCADE`. The exception sits only on the key that points out of the graph into
authored Markdown — the one place where derived data must not outrank its source.

Now, because the defect accumulates irreversibly. Every publication adds pages to the
undeletable set, and nothing repairs it after the fact: neither clearing the selector, nor
republishing, nor the link refresh delivered in `wiki-links-staleness-precision` releases
a pinned page.

## Desired Outcomes

- A page that carried a `code` selector at publication time deletes through
  `wiki_delete_page` as any other page does, however many snapshots reference it. The three
  currently stuck pages delete once the migration has run, with no manual SQL.
- The delete takes the derived rows with it in the same transaction: no row of
  `code_graph_wiki_links` points at a page that no longer exists.
- Graph reads stay correct across such a delete. `wiki_code_status`, `wiki_code_search` and
  `wiki_code_context` answer as before and the file, symbol and relation counts are
  unchanged; only the number of derived links drops.
- A freshly created database and an upgraded one end at the same constraint definition,
  confirmed by reading `pg_constraint.confdeltype` rather than by trusting that the
  migration ran.
- The repair arrives through the ordinary migration path the schema owner already runs. No
  separate data-repair operation is required of anyone.

## Health Metrics

- The other integrity guards are untouched. `code_graph_domain_state_active_ready_fk` and
  `code_graph_relations_target_symbol_fk` keep `ON DELETE NO ACTION`; they protect
  connectivity inside the graph and must keep doing so.
- The cascade does not exceed its own rows. Deleting a page removes only its
  `code_graph_wiki_links`; files, symbols, relations, snapshots and the active snapshot
  pointer are left alone.
- Migrations stay ordered, contiguous and idempotent. `_validate_migrations` keeps holding,
  a repeated run is a no-op, and an already-migrated database is not rebuilt destructively.
- The runtime still never migrates, and the restricted runtime principal's grants are not
  widened.
- The snapshot contract is unchanged: publication writes what it wrote, and
  `snapshot_revision`, `markdown_revision` and the counts are unaffected.
- The existing runs stay green: 3050 in the default configuration and 530 in
  `tests/postgres` against a disposable database.

## Strategic Context

- Interacts with: the schema owner, who alone runs migrations, so the fix is inert in a
  deployment until they act; hosted deployments that already carry pinned pages, a set that
  grows with every publication; client domains with real selectors, such as `familybudget`
  with 27 bindings and `framework` with 230, where more selectors mean more pinned pages;
  `wiki_delete_page` and the wrapper that reports the refusal as a generic
  `PostgreSQL operation failed`; the publication path and `wiki_code_refresh_links`, neither
  of which releases a pinned page today and neither of which will after this change, because
  the release belongs to the delete; and the adjacent defect deliberately left out of scope,
  that a superseded ready snapshot is never removed.
- Priority trade-off: trust, then cost, then speed. This alters a schema over production
  data, so the precision of the cascade's blast radius comes first — an over-broad cascade
  destroys graph rows irreversibly. Cost is second: the repair must arrive by migration
  rather than by manual work on each pinned page. Speed is last; it is one `ALTER` and the
  moment of rollout does not matter.

## Constraints

### Steering (behavioral guidance)

- Verify by reading `pg_constraint.confdeltype`, not by asserting that the constraint name
  exists. The existing migration test checks only the name, which is exactly why this defect
  survived.
- Extend that existing migration test rather than adding a parallel one.
- Keep the migration re-runnable: drop the constraint if present, then add it.
- Touch the generic error wrapper only if it is cheap and only to name the class of
  violation. The cure is the cascade, not the wording.
- Match the surrounding DDL style in `migrations.py` and reformat nothing nearby.

### Hard (architectural enforcement)

- Exactly one constraint changes, `code_graph_wiki_links_page_fk`. No other foreign key's
  delete rule is touched.
- The cascade removes only the deleted page's `code_graph_wiki_links` rows. No graph rows —
  files, symbols, relations, snapshots — and no `code_graph_domain_state` change.
- Delivered as a new `Migration(version=8, ...)`. The already-applied migration texts of
  versions 1 to 7 are not rewritten; a fresh database runs 1 through 8 in order and reaches
  the same state.
- Migration versions stay ordered and contiguous, and `_validate_migrations` keeps passing.
- The runtime does not run migrations, and the restricted runtime principal's grants are not
  widened.
- No data-repair step ships with the fix: no manual `DELETE` is part of it.
- Publication and refresh semantics are unchanged — neither what activation writes nor what
  `wiki_code_refresh_links` rewrites.
- Retention of superseded snapshots is out of scope: nothing here deletes a ready snapshot.

## Autonomy Zones

- Full autonomy (reversible, low risk): the wording of the migration statements, test names
  and structure, docstrings, the package version bump.
- Guarded (log the choice and its reasoning): `DROP CONSTRAINT IF EXISTS` against a strict
  drop; where the `confdeltype` assertion lives; whether the constraint is added validated in
  one step or as `NOT VALID` followed by `VALIDATE`.
- Proposal-first (needs approval): the delete rule of any other constraint; the contract of
  the generic error wrapper; any step that deletes existing data.
- No autonomy (human only): rewriting applied migrations 1 to 7; deleting ready snapshots;
  widening the runtime principal's grants; running the migration against any database that
  is not disposable, the hosted one included.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules

- Halt if: the `ALTER` cannot be expressed without touching another constraint.
- Halt if: recreating the key requires a table rewrite, or takes a lock on `iwiki.pages` of
  unbounded duration. This runs over production data and the cost of rollout must be
  predictable.
- Halt if: a cascade on this key could remove graph rows beyond the deleted page's links.
- Escalate if: the disposable database holds rows the new constraint rejects, or validation
  does not complete.
- Escalate if: re-running the migration against an already-migrated schema is not idempotent.
- Done when: on a disposable PostgreSQL, publishing a graph for a page that carries a `code`
  selector, publishing again so the first snapshot is superseded, and then deleting that page
  through the real store succeeds; `code_graph_wiki_links` holds no row for it in any
  snapshot; the file, symbol and relation counts and the active snapshot pointer are
  unchanged; and `pg_constraint.confdeltype` for the key reads `c` on both a freshly migrated
  database and an upgraded one.
