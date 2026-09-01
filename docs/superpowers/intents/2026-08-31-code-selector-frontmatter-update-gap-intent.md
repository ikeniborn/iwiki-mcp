---
review:
  intent_hash: f4af3d4e3be1866a
  last_run: 2026-08-31
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings: []
workflow:
  route: chain
  continuation: full
---
# Intent: code-selector-frontmatter-update-gap

**Date:** 2026-08-31
**Status:** approved

## Objective

Make code-graph use more efficient when an existing Wiki page needs updated code
selectors. Extend the existing `wiki_update_page` contract instead of adding another MCP
tool, so a caller can set, replace, or clear `code.symbols`, `code.files`, and
`code.source_globs` in place without delete-and-rewrite, page-body changes, revision
reset, or specification-evidence loss.

## Desired Outcomes

- A code-only `wiki_update_page` call adds selectors to an existing page; a subsequent
  read returns them, the page body is byte-identical, and PostgreSQL advances the page
  revision by exactly one.
- A caller can replace the complete selector mapping, and an explicit empty selector set
  clears it; omitting `code` leaves the current selectors unchanged.
- A stale PostgreSQL revision or an invalid selector mapping returns a stable failure and
  leaves the complete page unchanged.
- After a domain snapshot is republished, selector-bearing pages produce nonzero derived
  Wiki links and `wiki_code_context(include_wiki=true)` returns the linked pages.
- A selector-only update to a specification page preserves every GWT scenario
  `source_hash`, binding, and resolution-evidence state.

## Health Metrics

- The registered MCP tool count remains exactly 35.
- The published `wiki_update_page` input schema contains an explicit `anyOf` contract:
  one branch requires both `heading` and `new_body`, and one branch requires `code`.
- Every pre-existing `wiki_update_page` section-mode test passes without changing its
  call shape, and the full `uv run pytest -q` run has zero failures.
- A code-only update of an unchanged body performs zero new embedding calls for reused
  chunks on both Git and PostgreSQL test paths.
- PostgreSQL missing/stale revision tests, Git auto-commit/reindex tests, and strict
  specification projection/evidence tests have zero regressions.

## Strategic Context

- Interacts with: MCP clients and generated SDKs; `server.py` tool signature, schema, and
  storage dispatch; Git Wiki mutation and auto-commit; PostgreSQL compare-and-swap page
  storage; code-graph selector validation and publication; strict specification
  projection and resolution evidence; README, architecture, authoring resources, and
  exact tool-matrix tests.
- Priority trade-off: trust and correctness first, backward compatibility second,
  execution speed third, and implementation cost fourth.

## Constraints

### Steering (behavioral guidance)

- Keep the change inside the existing `wiki_update_page` path and reuse current selector
  validation, mutation guards, indexing, graph-refresh, and specification-projection
  pipelines.
- Preserve existing positional and keyword section-update calls; add `code` at the end
  of the public parameter list and keep the implementation surgical.
- Validate request-mode combinations before embedding, page writes, reindexing, commits,
  or other mutation side effects.

### Hard (architectural enforcement)

- Do not add or rename an MCP tool; the registered surface remains exactly 35 tools.
- The public input schema must expose the section-update and selector-update alternatives
  through JSON Schema `anyOf`; runtime validation remains mandatory for clients that do
  not enforce the schema.
- A code-only update must preserve the page body byte-for-byte and must not change GWT
  scenario hashes, bindings, or valid resolution evidence.
- PostgreSQL requires `expected_revision` for every successful update, returns
  `expected_revision_required` when omitted, and performs no mutation on `conflict`.
- Git storage keeps its current optional/ignored `expected_revision` behavior and its
  existing auto-commit and reindex semantics.
- Omitted `code` leaves selectors unchanged; an explicit empty selector set removes the
  complete `code` mapping; a non-empty mapping replaces the complete selector set.
- Selector vocabulary and validation do not change: modules, module IDs, aliases, import
  bindings, unknown keys, unsafe paths, and malformed mappings remain rejected.
- Do not add bulk selector updates, selector inference, a database migration, or
  frontmatter-only updates for fields other than `code`.

## Autonomy Zones

- Full autonomy (reversible, low risk): implement the confirmed code-only update mode,
  focused tests, documentation updates, and synchronized patch-version changes.
- Guarded (log + confidence threshold): perform a local internal refactor only when it
  removes duplication in the affected update path and focused Git/PostgreSQL regression
  tests pass immediately after the refactor.
- Proposal-first (needs approval): change public error codes or response fields; enable
  frontmatter-only updates for other fields; or choose behavior for a request that
  supplies both a section update and `code` in one call.
- No autonomy (human only): add a new MCP tool, expand selector grammar, add a database
  migration, weaken PostgreSQL CAS, or weaken strict specification preservation.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions is marked
> HUMAN CHECKPOINT in the plan.

## Stop Rules

- Halt if: the current FastMCP registration path cannot publish the required `anyOf`
  without breaking existing section-call compatibility or changing the tool count.
- Halt if: a code-only candidate changes page-body bytes, GWT scenario hashes, bindings,
  or valid resolution evidence.
- Escalate if: preserving generated-client compatibility requires changing an existing
  public error code, response field, or the behavior of combined section-plus-code calls.
- Done when: Git and PostgreSQL tool-level scenarios demonstrate set, replace, clear,
  omission, invalid-input, and stale-revision behavior; republishing demonstrates
  nonzero Wiki links and hydrated Wiki context; all Health Metrics pass; and repository
  plus iwiki documentation describe the checked contract without lint findings for this
  task.
