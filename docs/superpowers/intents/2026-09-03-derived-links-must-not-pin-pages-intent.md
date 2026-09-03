---
result_check:
  verdict: OK
  intent_hash: dbaabd8b8f38782a
  last_run: 2026-09-03
  reconciliation:
    base: cf1efa5
    outcomes:
      - id: DO-1
        status: DONE
        evidence: >-
          Migration version 8 makes the key cascade;
          `test_a_page_pinned_by_a_superseded_snapshot_still_deletes` publishes
          twice and then deletes the page through the real store. Whether the
          three named pages actually delete depends on the schema owner running
          the migration, which no diff can carry.
      - id: DO-2
        status: DONE
        evidence: >-
          The same test asserts the domain holds no link row afterwards, and the
          cascade runs inside the delete's own transaction.
      - id: DO-3
        status: PARTIAL
        evidence: >-
          `active_rows()` and the active snapshot pointer are asserted
          unchanged, and a second test shows a surviving page keeps its row. No
          test calls `wiki_code_status`, `wiki_code_search` or
          `wiki_code_context` after such a delete.
      - id: DO-4
        status: DONE
        evidence: >-
          `pg_constraint.confdeltype` and `convalidated` are asserted on a
          freshly migrated database in `test_code_graph_migrations.py`, and on
          an upgraded one after the rollback-and-reapply chain in
          `test_code_graph_rollback.py`.
      - id: DO-5
        status: DONE
        evidence: >-
          The change ships migration statements only; no manual delete and no
          data-repair step is part of it.
    excess:
      - path: src/iwiki_mcp/postgres/migrations.py rollback, src/iwiki_mcp/http.py, src/iwiki_mcp/server.py
        note: >-
          The version bump pins the runtime and demands a rollback artifact per
          the repository's existing per-version contract. The intent scoped only
          the constraint; the user authorized the full version-8 release after
          discovery surfaced the obligation.
      - path: pyproject.toml, src/iwiki_mcp/__init__.py, tests/test_package.py, uv.lock
        note: The version bump this repository requires of every change.
  findings:
    - id: R-001
      severity: CRITICAL
      text: >-
        Changed observable behavior in a `strict` domain with no
        Given-When-Then scenario covering page deletion.
      verdict: fixed
      verdict_at: 2026-09-03
      fix: >-
        Two scenarios authored in `concept/code-graph-wiki-linking`: deleting a
        page held by a superseded snapshot, and keeping the links of every page
        the delete does not name.
    - id: R-002
      severity: WARNING
      text: >-
        DO-3 has no test calling a graph read after such a delete; the counts
        and the active pointer are asserted instead.
      verdict: accepted
      verdict_at: 2026-09-03
    - id: R-003
      severity: WARNING
      text: >-
        Thirteen assertions across five files encoded version 7 as the pinned
        schema. They were updated rather than left, which widens the diff well
        past the one constraint the intent described.
      verdict: accepted
      verdict_at: 2026-09-03
review:
  intent_hash: a42470fc324335c0
  last_run: 2026-09-03
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings:
    - id: F-001
      phase: clarity
      severity: WARNING
      section: Stop Rules
      section_hash: 4016cd3f4c4611c6
      fragment: "the cost of rollout must be decidable rather than judged"
      text: >-
        The halt condition names no bound, so it cannot be decided
        mechanically. Whether a lock is unbounded is a judgement at
        implementation time rather than a criterion the rule states.
      fix: >-
        Either name the acceptable lock class, or state the observation that
        settles it: the constraint is added without a table rewrite and
        validates in one pass over the disposable database.
      verdict: fixed
      verdict_at: 2026-09-03
    - id: F-002
      phase: alignment
      severity: INFO
      section: Objective
      section_hash: 47c46625dfbc2ab8
      fragment: "lose their derived links through rebuilding or schema cascades"
      text: >-
        `concept/code-graph-wiki-linking` states the cascade as present fact.
        It is false today and stays false in any deployment until the schema
        owner runs the migration, so the page describes an intended state
        rather than the running one.
      fix: >-
        Say on that page that the cascade is the schema's rule and that a
        deployment gains it with the migration, so a reader on an unmigrated
        database is not misled.
      verdict: accepted
      verdict_at: 2026-09-03
    - id: F-003
      phase: clarity
      severity: INFO
      section: Desired Outcomes
      section_hash: 0d924c140cf6884c
      fragment: "older than the configured retention"
      text: >-
        The retention is named as configured but no key is named, so the
        outcome reads against a value the document does not identify.
      fix: >-
        Name the setting once the implementation adds it, or leave it as the
        deliberate implementation detail it is.
      verdict: accepted
      verdict_at: 2026-09-03
---
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

The same discovery found what makes the consequence permanent. `DELETE FROM
iwiki.code_graph_snapshots` appears once in the package, in `_discard_snapshot`, and only
for `state = 'staging'`. A superseded ready snapshot is never removed, so snapshots and
all their rows accumulate without bound, and every one of them keeps holding the pages it
referenced. No read path joins a superseded snapshot — every query goes through
`code_graph_domain_state.active_snapshot_id` — so retaining them indefinitely buys nothing
programmatic. This intent covers both halves: the cascade releases the page, and bounded
pruning stops the pile that made the pin permanent.

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
- A superseded ready snapshot no longer accumulates without bound. After a publication older
  than the configured retention, the snapshot and its rows are gone, while the active
  snapshot and the most recent supersessions inside the retention window are still there.

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
- Pruning never touches the active snapshot, and never removes a snapshot inside the
  retention window. A publication that fails or aborts leaves the previously active snapshot
  in place.
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
- A superseded ready snapshot is pruned by age only, never the active one, and never more
  than the configured limit in a single call. Pruning mirrors the existing staging cleanup
  rather than inventing a second retention mechanism.

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
- Halt if: recreating the key requires a table rewrite. The observation that settles it is
  that on the disposable database the constraint is added and validated in one pass, with
  `pg_constraint.convalidated` reading true immediately after the migration returns. This
  runs over production data, so the cost of rollout must be decidable rather than judged.
- Halt if: a cascade on this key could remove graph rows beyond the deleted page's links.
- Halt if: pruning could remove the active snapshot, or a snapshot a read path still joins.
- Escalate if: the disposable database holds rows the new constraint rejects, or validation
  does not complete.
- Escalate if: re-running the migration against an already-migrated schema is not idempotent.
- Done when: on a disposable PostgreSQL, publishing a graph for a page that carries a `code`
  selector, publishing again so the first snapshot is superseded, and then deleting that page
  through the real store succeeds; `code_graph_wiki_links` holds no row for it in any
  snapshot; the file, symbol and relation counts and the active snapshot pointer are
  unchanged; and `pg_constraint.confdeltype` for the key reads `c` on both a freshly migrated
  database and an upgraded one. And, on the same database, a snapshot superseded longer ago
  than the retention is gone after the next publication while the active one and a snapshot
  superseded inside the window remain.
