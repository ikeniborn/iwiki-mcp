---
result_check:
  verdict: OK
  intent_hash: 3bf59b121a60ac41
  last_run: 2026-09-03
  reconciliation:
    base: 0ec24ab
    outcomes:
      - id: DO-1
        status: DONE
        evidence: >-
          `server.py` registers `wiki_code_refresh_links`; the store method
          derives without parsing or resolving;
          `test_refresh_rederives_wiki_links_without_touching_the_graph`
          observes `wiki_links_stale` false after one call.
      - id: DO-2
        status: PARTIAL
        evidence: >-
          Suppression in `codegraph.py` keys on the same flag the tests assert,
          so the outcome follows, but no test calls
          `wiki_code_context(include_wiki=true)` after a refresh.
      - id: DO-3
        status: DONE
        evidence: >-
          The same test asserts an unchanged `snapshot_revision` and identical
          file, symbol and relation counts. `graph_payload_revision` is
          untouched by construction but not asserted.
      - id: DO-4
        status: DONE
        evidence: >-
          `test_refresh_matches_what_a_full_publication_derives` compares the
          snapshot-scoped link rows of a refresh against a republication.
      - id: DO-5
        status: PARTIAL
        evidence: >-
          Hosted-only by construction, since the handler requires an
          authenticated token and refuses non-PostgreSQL storage, and the tool
          matrix lists it supported. No test exercises the refusals directly.
    excess:
      - path: CLAUDE.md
        note: >-
          The disposable-PostgreSQL testing rule, authorized by the user in the
          same exchange but accounted for by no Desired Outcome.
      - path: pyproject.toml, src/iwiki_mcp/__init__.py, tests/test_package.py, uv.lock
        note: The version bump this repository requires of every change.
  findings:
    - id: R-001
      severity: CRITICAL
      text: >-
        New observable behavior in a `strict` domain with no Given-When-Then
        scenario covering it.
      verdict: fixed
      verdict_at: 2026-09-03
      fix: >-
        Two scenarios authored in `concept/code-graph-wiki-linking` under a new
        `Specification` section: the refresh against an active snapshot and the
        refusal without one.
    - id: R-002
      severity: WARNING
      text: DO-2 has no test calling wiki_code_context after a refresh.
      verdict: accepted
      verdict_at: 2026-09-03
    - id: R-003
      severity: WARNING
      text: DO-5 has no test for the hosted-transport and storage refusals.
      verdict: accepted
      verdict_at: 2026-09-03
review:
  intent_hash: 3bf59b121a60ac41
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
      section: Desired Outcomes
      section_hash: 8487550d70860331
      fragment: "The operation is available on the hosted transport"
      text: >-
        The fifth outcome names an availability rather than an observed
        result. The other four state what is seen after the call; this one
        states that a capability exists.
      fix: >-
        Phrase it as the observation: on a hosted server with no checkout,
        one call clears the flag and the answer reports it.
      verdict: accepted
      verdict_at: 2026-09-03
    - id: F-002
      phase: alignment
      severity: WARNING
      section: Constraints
      section_hash: 2dccd9f357b2b10e
      fragment: "Derivation reuses the existing `_derive_links`"
      text: >-
        `concept/code-graph-wiki-linking` states that PostgreSQL derives
        `code_graph_wiki_links` inside the same transaction that activates
        the snapshot. A standalone refresh derives them outside any
        activation, which extends that documented decision rather than
        contradicting it, but leaves the page inaccurate once shipped.
      fix: >-
        Update `concept/code-graph-wiki-linking` in the same change, so the
        page names activation and refresh as the two derivation paths.
      verdict: accepted
      verdict_at: 2026-09-03
    - id: F-003
      phase: consistency
      severity: INFO
      section: Health Metrics
      section_hash: 4c7e6ea634aa4155
      fragment: "Publication remains the only writer of graph rows"
      text: >-
        Read alone the metric contradicts the feature, since the refresh
        writes `code_graph_wiki_links`. The carve-out that follows resolves
        it, but the metric's opening clause is broader than what it means.
      fix: >-
        Narrow the opening clause to the snapshot tables it actually
        protects.
      verdict: accepted
      verdict_at: 2026-09-03
---
# Intent: wiki-links-staleness-precision

**Date:** 2026-09-03
**Status:** approved

Scope: slice B of the topic, the link refresh. Slice C is merged as `0ec24ab`. Slice A,
narrowing the revision to selector-bearing pages, is deliberately out of scope here and
gets its own intent if it is pursued at all.

## Objective

Clearing `wiki_links_stale` costs a full code-graph rebuild: parsing the whole repository
and resolving it, 75 to 87 seconds in this project, to produce a snapshot identical to the
stored one. Publication is the only path that activates links, and the MCP surface exposes
no refresh at all — a hosted server without a checkout has no way to clear the flag,
because `wiki_code_index` there answers `source_unavailable`.

Now, because slice C reduced the flag to one predicate but did not make it cheap to clear.
While clearing stays expensive there is pressure to narrow the signal itself, which
inverts the failure direction from a harmless extra stale report to a missed one. A cheap
refresh removes that pressure and makes the conservative error nearly free.

This delivers requirement 7 of `reference/snapshot-freshness-and-republication-friction`:
"A link-refresh operation re-derives `DOCUMENTED_BY` from current Markdown against the
existing snapshot, without parsing or resolution, and is exposed on the MCP surface rather
than only through the publisher CLI."

## Desired Outcomes

- A selector change on an unchanged checkout is activated by one MCP call. `wiki_links_stale`
  becomes `false` and the call completes in time proportional to the page count rather than
  to the number of source files: seconds instead of 75 to 87.
- After the call, `wiki_code_context(include_wiki=true)` stops suppressing `wiki_pages` and
  returns the pages bound to the same selectors a full publication would bind.
- The refresh leaves the graph untouched: `snapshot_revision`, `graph_payload_revision` and
  the file, symbol and relation counts are the same before and after. Only
  `code_graph_wiki_links` and the stored Markdown revision change.
- The refreshed link set is identical to what a full publication of the same Markdown
  against the same snapshot produces.
- The operation is available on the hosted transport, where no checkout exists, which is
  exactly the case nothing covers today.

## Health Metrics

- Markdown authority. The refresh writes no page bytes and generates no selectors, exactly
  as no code-graph path does today.
- Publication remains the only writer of graph rows. The refresh touches
  `code_graph_wiki_links` and the stored Markdown revision, never
  `code_graph_files` / `code_graph_symbols` / `code_graph_relations`, and never the active
  `snapshot_id`.
- Conflict detection does not weaken. `snapshot_conflict` and `markdown_unavailable` keep
  working on the full publication path, and the refresh does not become a way to activate a
  snapshot against Markdown it never saw.
- Atomicity. Either the link set is replaced whole and the revision advances, or nothing
  changes. No state exists where some links are new and some are old.
- Authorization does not widen. The refresh needs the same write scope on the bound primary
  that publication needs, and the token's grants stay the absolute limit.
- Storage split. Backends that do not support publication keep answering exactly as they do
  now; the new tool does not silently start working where the rest of the path returns
  `unsupported_storage`.
- The existing runs stay green: 526 in `tests/postgres` against a disposable database, and
  3050 in the default configuration.

## Strategic Context

- Interacts with: the hosted HTTP server, which today has no way to clear the flag; the
  publisher CLI and the `begin` / `batch` / `finalize` path, whose activation derives links
  through `_derive_links`; `wiki_code_context(include_wiki=true)`, which consumes the flag by
  suppressing `wiki_pages`; `wiki_lint` and `wiki_code_status`, which answer the flag with one
  predicate since slice C; client domains that carry real selectors, such as `familybudget`
  with 12 scenarios and 27 bindings, since this domain declares none and cannot show a
  regression by observation; and operators, who currently choose between an 87-second rebuild
  and suppressed `wiki_pages`.
- Priority trade-off: trust, then speed, then cost. Cheapness is the point of the slice but
  not at the price of correctness. A refresh that yields a different link set than a full
  publication of the same Markdown against the same snapshot is worse than a slow rebuild,
  because the flag feeds reads: a fast wrong answer is more dangerous here than a slow right
  one.

## Constraints

### Steering (behavioral guidance)

- Name and response shape stay in the `wiki_code_*` family, with a payload aligned to
  `wiki_code_status`.
- Keep the fail-soft handler contract: `@_safe`, sanitized errors, no raw exception chains.
- Advance the stored revision even when the derived link set is unchanged. Clearing the flag
  is the purpose, not a side effect.
- Piecemeal: no batching, streaming or session machinery until the page count demands it.
  Publication carries those because of graph volume, which a refresh does not have.
- A refresh that meets a missing or non-ready snapshot refuses with a named code rather than
  quietly falling back to a rebuild.

### Hard (architectural enforcement)

- No source parsing and no resolution. The refresh only re-derives links from current
  Markdown against the existing snapshot.
- Markdown is never mutated and selectors are never generated.
- `code_graph_files`, `code_graph_symbols`, `code_graph_relations` and the active
  `snapshot_id` do not change.
- One transaction, all or nothing.
- Derivation reuses the existing `_derive_links` rather than introducing a second
  implementation. Two implementations could diverge, which is the defect class slice C just
  removed.
- `snapshot_conflict` and `markdown_unavailable` on the full publication path are not
  weakened.
- Authorization does not widen: the same write scope on the bound primary, with the token's
  grants as the absolute limit.
- Backends without publication support return the same refusal they return today.
- The Markdown revision stays domain-wide. Narrowing it to selector-bearing pages is slice A
  and is not done here, not even partially.

## Autonomy Zones

- Full autonomy (reversible, low risk): private helper names, test structure, wording of
  sanitized messages within the existing contract, docstrings, the package version bump.
- Guarded (log the choice and its reasoning): how snapshot rows are loaded for
  `_derive_links`, rehydration in memory against SQL joins; the field set of the tool
  response; where the transaction boundary falls.
- Proposal-first (needs approval): the public tool name and its parameters, because that is a
  permanent MCP contract; any schema change or migration; any change to what the local SQLite
  path exposes.
- No autonomy (human only): anything that changes the authorization model; weakening
  publication conflict detection; writing to Markdown; narrowing the revision to
  selector-bearing pages, which is slice A; editing the frozen chain artifacts of other
  topics.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules

- Halt if: the refresh cannot be made byte-identical to the publication's derivation without
  duplicating the link-derivation logic.
- Halt if: the operation turns out to require writing graph rows or advancing the active
  `snapshot_id`.
- Escalate if: the local SQLite path needs a materially different design from PostgreSQL. Two
  designs instead of one is a scope revision, not an implementation detail.
- Escalate if: the `tests/postgres` run against the disposable container exposes a defect in
  existing publication semantics rather than in the new code.
- Done when: on a disposable PostgreSQL carrying a domain with real selectors, changing a
  selector and calling the tool once yields `wiki_links_stale: false` from both
  `wiki_code_status` and `wiki_lint`; `snapshot_revision`, `graph_payload_revision` and the
  file, symbol and relation counts are unchanged; and the resulting `code_graph_wiki_links`
  rows equal those produced by a full publication of the same Markdown against the same
  snapshot.
