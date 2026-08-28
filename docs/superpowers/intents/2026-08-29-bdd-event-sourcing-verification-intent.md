---
review:
  intent_hash: 39da18b58d94cbaa
  last_run: 2026-08-29
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

# Intent: bdd-event-sourcing-verification

**Date:** 2026-08-29
**Status:** approved

## Objective

Apply Behavior-Driven Development, including Given-When-Then scenarios for
event-sourced behavior, as an effective project-maintenance practice that improves the
quality and trustworthiness of development performed by agents. Connect specification
semantics to executable tests and implementation evidence without making ordinary Wiki
use depend on GWT support or a code graph.

## Desired Outcomes

- A project can select `disabled`, `optional`, or `strict` specification behavior in
  TOML. `optional` is the default, while `strict` applies only to pages classified as
  specifications.
- Ordinary Markdown pages and GWT specifications coexist in one Wiki domain. Existing
  pages remain readable, writable, searchable, indexable, and lintable without
  migration.
- A GWT scenario remains human-readable Markdown and exposes a stable scenario identity,
  explicit Given, When, and Then phases, domain roles, expected outcomes, executable-test
  evidence, and semantic code targets.
- When the code graph is absent, disabled, stale, failed, or unreachable, ordinary Wiki
  operations and GWT specification storage, validation, indexing, and retrieval continue
  to work. Code targets remain visible with an unresolved or graph-unavailable status.
- When a ready code graph is available, a user or agent can query the relationship from
  a scenario and its GWT phases to the relevant command, event, exception, implementation,
  and executable test.
- The Wiki contains durable, precise rules that future iClaude and iCodex skills can use
  to decide when to create or update a GWT scenario, how to bind it to code and tests,
  which verification evidence to record, and how to handle stale or unavailable graph
  evidence.

## Health Metrics

- Existing tests for Markdown, frontmatter, page read/write, indexing, search, and lint
  continue to pass for pages without GWT specifications.
- Existing projects require no page rewrite, frontmatter migration, or code-graph setup
  to retain their current Wiki behavior.
- Verification scenarios prove that ordinary page operations and GWT document operations
  succeed while the code graph is unavailable.
- GWT validation failures never cause ordinary non-specification pages to fail storage,
  indexing, retrieval, search, or lint.
- Performance is measured after implementation and test execution. This intent sets no
  performance acceptance threshold; optimization decisions require observed results.

## Strategic Context

- Interacts with: iwiki Markdown and frontmatter processing, page persistence, indexing,
  retrieval, search, lint, optional code-graph resolution, executable repository tests,
  iClaude and iCodex skills, project developers, and reviewers.
- Priority trade-off: specification trust first, agent development quality second,
  delivery speed third, and cost fourth.

## Constraints

### Steering (behavioral guidance)

- Describe observable domain behavior instead of internal method steps.
- Maintain a scenario, its executable test evidence, and its semantic code bindings as a
  coherent unit.
- Give every semantic scenario a stable identity and explicit Given, When, and Then
  phases with domain roles.
- Keep specification pages and mixed explanatory content readable as ordinary Markdown.
- Base performance optimization on measurements collected after implementation and
  testing.

### Hard (architectural enforcement)

- Ordinary Wiki authoring, validation, persistence, indexing, retrieval, search, lint,
  and page operations must work without GWT processing and without a code graph.
- `optional` is the default specification mode. `strict` may reject invalid content only
  on pages classified as specifications. `disabled` preserves ordinary Wiki-only
  behavior.
- Existing Wiki pages must not require migration or acquire new validation failures when
  specification support is introduced.
- Missing, disabled, stale, failed, or unreachable code-graph state must not block Wiki
  or GWT document operations. Semantic targets must degrade to explicit unresolved or
  graph-unavailable evidence.
- The server is the authority for specification syntax, stable identity, validation,
  persistence, resolution state, lint, and query behavior. Agent skills guide authoring,
  application, executable verification, and maintenance, but cannot define hidden
  semantic state.
- GWT must not become mandatory for every Wiki page or every project change.

## Autonomy Zones

- Full autonomy (reversible, low risk): repository and Wiki analysis, draft GWT
  scenarios, focused executable tests, documentation, and semantic bindings whose code
  targets are unambiguous.
- Guarded (log + confidence threshold): implementation of an approved specification
  contract and updates to existing scenarios or bindings, with lint, focused tests, and
  recorded verification evidence.
- Proposal-first (needs approval): changes to GWT meaning, TOML mode semantics, the
  public MCP contract, or the accepted behavior expressed by an existing scenario.
- No autonomy (human only): destructive removal of user Wiki content, mandatory
  code-graph coupling, migration that rewrites existing pages, or any ordinary-Wiki
  compatibility break.

## Stop Rules

- Halt if: ordinary Wiki behavior regresses, code-graph availability becomes a
  prerequisite, or existing pages require migration.
- Escalate if: a specification contradicts executable test or code evidence, or the
  public specification contract has more than one materially different valid meaning.
- Done when: observable verification proves all three modes follow their agreed scope;
  ordinary Wiki and GWT document operations work without a code graph; GWT semantics and
  resolution state persist and can be queried; ready graph evidence enriches scenario,
  phase, code, and test relationships; durable agent-skill rules are published in the
  Wiki; and focused plus full regression tests and intent outcome checks pass.
