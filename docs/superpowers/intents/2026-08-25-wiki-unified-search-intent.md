---
review:
  intent_hash: f3956868a7f363e9
  last_run: 2026-08-25
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
# Intent: wiki-unified-search

**Date:** 2026-08-25
**Status:** approved

## Objective

Determine with comparative evidence whether a public `wiki_unified_search` tool adds
material value beyond the existing `wiki_search` -> `wiki_code_search` ->
`wiki_code_context` workflow. Implement the new tool only if it reduces coordination
cost for common meaning-plus-code questions without duplicating the specialized tools,
weakening result quality, or hiding freshness and ranking boundaries. If the evidence
does not justify another public tool, retain the current surface and record that decision.

## Desired Outcomes

- A representative evaluation compares the current specialized-tool workflow with the
  candidate unified workflow and produces an explicit, evidence-backed `implement` or
  `do not implement` decision.
- An accepted unified contract reduces tool calls for common meaning-plus-code queries
  while preserving the completeness and relevance available from the same bounded
  specialized calls.
- When code data is stale, missing, or unavailable, the unified workflow still returns
  Wiki results and explicitly reports the degraded code or association state.
- Existing `wiki_search`, `wiki_code_search`, and `wiki_code_context` remain available
  with unchanged contracts for documentation-only, structural, and advanced traversal
  work.
- A `do not implement` result leaves no redundant public tool and documents why the
  current specialized composition is sufficient.

## Health Metrics

- Existing Wiki and code search/context schemas, results, ranking rules, and focused
  regression suites do not degrade.
- Existing standalone-tool latency remains within its current benchmark gates; the
  evaluation records candidate end-to-end latency and tool-call count against the
  equivalent specialized baseline.
- A code-graph failure or stale snapshot blocks zero valid Wiki results.
- The candidate never loses a bounded baseline result merely because Markdown scores
  and code ranks use different scales.
- SQLite, PostgreSQL, and hosted MCP routes preserve the same normalized behavior where
  their existing source-availability contracts overlap.

## Strategic Context

- Interacts with: `server.py`, Markdown retrieval and reranking, code-graph search and
  bounded context, SQLite and PostgreSQL readers, hosted MCP authorization, agent
  workflows, code-to-Wiki associations, publication freshness, and evaluation fixtures.
- Priority trade-off: trust first, then workflow simplicity, then speed and cost.

## Constraints

### Steering (behavioral guidance)

- Evaluate orchestration value before committing to another public API.
- Prefer the smallest contract that removes demonstrated agent coordination work.
- Keep Wiki semantics, typed code discovery, and structural context independently
  inspectable in responses and evaluation evidence.
- Use representative common queries plus negative and degraded-state cases; do not
  justify the feature from a happy-path demo alone.

### Hard (architectural enforcement)

- Do not directly compare or fuse Markdown relevance scores with code match ranks.
- Return Wiki results, code results, confirmed associations, and bounded context as
  distinct response blocks when the unified contract is implemented.
- Do not remove or change the contracts of `wiki_search`, `wiki_code_search`, or
  `wiki_code_context`.
- Bound internal context to depth 1 and at most three selected code seeds.
- Preserve Wiki results and expose explicit degradation when code data or derived Wiki
  associations are stale, missing, or unavailable.
- Do not mutate Markdown or `code` frontmatter and do not trigger code-graph publication
  from a search request.
- Preserve authenticated domain scope, backend isolation, query limits, redaction, and
  fail-soft behavior across local and hosted transports.
- Do not register `wiki_unified_search` if comparative evidence shows no material value
  over the existing specialized workflow.

## Autonomy Zones

- Full autonomy (reversible, low risk): read-only repository analysis, evaluation
  fixtures, internal orchestration prototypes, focused tests, and benchmark reporting.
- Guarded (log + confidence threshold): internal top-k, seed-selection, and context
  budgets when comparative evidence justifies the chosen bound.
- Proposal-first (needs approval): the public MCP request/response contract, ranking and
  freshness semantics, and any decision to register the new tool.
- No autonomy (human only): removal of existing tools, automatic Markdown writes,
  hidden fallback between configured backends, or fusion of incomparable scores.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions is marked
> HUMAN CHECKPOINT in the plan.

## Stop Rules

- Halt if: comparative evaluation shows no material coordination or quality benefit, or
  the candidate can work only by violating a hard constraint.
- Escalate if: result completeness, latency, backend parity, or freshness behavior
  creates an unresolved trade-off that changes the proposed public contract.
- Done when: comparative evaluation records an evidence-backed `implement` or
  `do not implement` decision; for `implement`, the approved contract passes real
  meaning-plus-code, degraded-state, and specialized-tool regression scenarios; for
  `do not implement`, no new tool is registered and the retained workflow is documented.
