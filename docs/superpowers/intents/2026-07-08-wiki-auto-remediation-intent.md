---
review:
  intent_hash: 6634485d64f7f25b
  last_run: 2026-07-08
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings: []
---
# Intent: wiki auto remediation

**Date:** 2026-07-08
**Status:** approved

## Objective
Automate wiki freshness maintenance after `wiki_lint` finds source drift without duplicating existing write tools. The server should derive remediation candidates from the ingest log and provide the calling agent with enough context to regenerate stale wiki pages. The agent then applies changes through existing MCP tools, re-checks the wiki, refreshes indexes/vectors through existing write/index paths, and reports the actions taken.

## Desired Outcomes
- A maintenance planning tool returns an explicit list of update candidates and delete candidates derived from ingest history.
- The planning result gives the calling agent the source content, current wiki page, authoring rules, and action metadata needed to generate updated wiki markdown for each `stale` candidate.
- The calling agent updates stale pages and deletes missing-source pages through existing tools (`wiki_update_page`, `wiki_delete_page`, `wiki_write_page`, and `wiki_index`) without asking the user at every step.
- The final response reports which pages were updated, which pages were deleted, which actions failed, and which lint findings remain after the run.
- User feedback or a request to restore old content can happen after the report, but it is not an approval gate for the maintenance workflow.

## Health Metrics
- `wiki_lint` remains deterministic, stdlib-only, and backward-compatible.
- Existing MCP write/delete/update tools keep their current public contracts; the design should avoid introducing a second apply/update API.
- Remediation candidates come only from ingest log-backed lint findings: `stale` maps to agent-generated update candidates and `missing_source` maps to delete candidates.
- Wiki page regeneration is performed by the calling agent, not by an internal server-side LLM client.
- Pages without source history are never updated or deleted by the automated maintenance flow.
- The operation does not write outside the current project's bound write domain.

## Strategic Context
- Interacts with: MCP server tools, `engine.lint`, ingest log records, authoring rules, existing write/update/delete/index paths, indexing/vector refresh, git auto-commit, and coding agents such as Claude Code and Codex.
- Priority trade-off: trust over speed and cost.

## Constraints
### Steering (behavioral guidance)
- Prefer a best-effort run: attempt every candidate and report per-action success or failure.
- Keep the user-facing report concise but specific enough to review what changed after the workflow.
- Preserve the existing lint mental model: lint detects, planning exposes candidates, the agent regenerates stale pages, existing write/delete/update tools persist changes, and the final lint result proves the post-run state.

### Hard (architectural enforcement)
- Only `wiki_lint` findings backed by the latest ingest record are eligible for automated remediation.
- `stale` findings may trigger agent-side update actions only when the recorded source still exists.
- `missing_source` findings may trigger delete actions only for the page reported by lint.
- The planning operation must not write files, update logs, delete pages, or re-index by itself.
- The maintenance workflow must use existing write/update/delete/index tools to validate markdown, update ingest hashes, delete pages, re-index, and produce final lint evidence.
- The workflow must not modify any domain other than the current project's bound write domain.
- The workflow must not modify pages that lack ingest history.
- The server must not depend on a server-side chat/LLM configuration for regeneration.

## Autonomy Zones
- Full autonomy (reversible, low risk): building candidate lists from lint output, returning source/page context to the agent, using existing tools to update stale pages, delete missing-source pages, re-run lint, re-index, and report results inside the bound write domain.
- Guarded (log + confidence threshold): continuing after an individual update/delete/validation failure while recording the failure and attempting remaining candidates.
- Proposal-first (needs approval): changing eligibility rules, adding a server-side LLM client, adding a new apply/update API, changing how source content is transformed into wiki markdown, changing write-domain scope, or adding destructive actions beyond lint-backed `missing_source` deletion.
- No autonomy (human only): deciding to restore deleted content after reviewing the final report, resolving semantic disagreement about whether a generated wiki update is correct, or authorizing remediation outside the current bound write domain.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules
- Halt if: there is no bound write domain for the current project, the requested domain is not the current project's bound write domain, or lint cannot produce a report for the domain.
- Escalate if: any candidate requires a page without ingest history, generated markdown fails validation in existing tools, indexing fails after changes, or git auto-commit reports a state that requires manual sync/conflict handling.
- Done when: one agent-orchestrated maintenance workflow has planned every lint-backed update/delete candidate, applied every safe action it can handle through existing tools, re-indexed the affected domain, re-run lint, and returned a report containing `planned`, `updated`, `deleted`, `failed`, and `remaining_lint` sections.
