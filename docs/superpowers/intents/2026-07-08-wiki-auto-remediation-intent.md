---
review:
  intent_hash: 9f76d91778aafb76
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
Automate wiki freshness maintenance after `wiki_lint` finds source drift. The server should derive remediation candidates from the ingest log, apply safe update and delete actions in one run, re-check the wiki, refresh the index and vectors, and return a report of all actions taken.

## Desired Outcomes
- A maintenance tool returns an explicit list of update candidates and delete candidates derived from ingest history.
- The tool automatically updates wiki pages for `stale` findings and deletes wiki pages for `missing_source` findings without asking the user at every step.
- The final response reports which pages were updated, which pages were deleted, which actions failed, and which lint findings remain after the run.
- User feedback or a request to restore old content can happen after the report, but it is not an approval gate for the maintenance run.

## Health Metrics
- `wiki_lint` remains deterministic, stdlib-only, and backward-compatible.
- Existing MCP tools keep their current public contracts unless the design explicitly calls out an additive change.
- Remediation candidates come only from ingest log-backed lint findings: `stale` maps to update candidates and `missing_source` maps to delete candidates.
- Pages without source history are never updated or deleted by the automated maintenance flow.
- The operation does not write outside the current project's bound write domain.

## Strategic Context
- Interacts with: MCP server tools, `engine.lint`, ingest log records, indexing/vector refresh, git auto-commit, and coding agents such as Claude Code and Codex.
- Priority trade-off: trust over speed and cost.

## Constraints
### Steering (behavioral guidance)
- Prefer a best-effort run: attempt every candidate and report per-action success or failure.
- Keep the user-facing report concise but specific enough to review what changed after the run.
- Preserve the existing lint mental model: lint detects, remediation acts on lint findings, and the final lint result proves the post-run state.

### Hard (architectural enforcement)
- Only `wiki_lint` findings backed by the latest ingest record are eligible for automated remediation.
- `stale` findings may trigger update actions only when the recorded source still exists.
- `missing_source` findings may trigger delete actions only for the page reported by lint.
- The operation must re-run lint after applying actions and include the remaining findings in the result.
- The operation must re-index the affected domain so the vector store reflects page updates and deletions.
- The operation must not modify any domain other than the current project's bound write domain.
- The operation must not modify pages that lack ingest history.

## Autonomy Zones
- Full autonomy (reversible, low risk): building candidate lists from lint output, updating pages for `stale` findings, deleting pages for `missing_source` findings, re-running lint, re-indexing, and reporting results inside the bound write domain.
- Guarded (log + confidence threshold): continuing after an individual update or delete failure while recording the failure and attempting remaining candidates.
- Proposal-first (needs approval): changing eligibility rules, changing how source content is transformed into wiki markdown, changing write-domain scope, or adding destructive actions beyond lint-backed `missing_source` deletion.
- No autonomy (human only): deciding to restore deleted content after reviewing the final report, resolving semantic disagreement about whether a generated wiki update is correct, or authorizing remediation outside the current bound write domain.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules
- Halt if: there is no bound write domain for the current project, the requested domain is not the current project's bound write domain, or lint cannot produce a report for the domain.
- Escalate if: any candidate requires a page without ingest history, a source transform is ambiguous, indexing fails after changes, or git auto-commit reports a state that requires manual sync/conflict handling.
- Done when: one maintenance run has processed every lint-backed update/delete candidate it can safely handle, re-indexed the affected domain, re-run lint, and returned a report containing `updated`, `deleted`, `failed`, and `remaining_lint` sections.
