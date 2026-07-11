---
review:
  intent_hash: ceeb55c8e3301186
  last_run: 2026-07-11
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings: []
---
# Intent: Wiki Push Recovery

**Date:** 2026-07-11
**Status:** approved

## Objective

Investigate both observed push failures from the MCP server process and the missing
push warning in mutating tool results. Confirm whether the server process has a
different credential environment from an interactive shell, determine viable fixes
when confirmed, and make push failures visible to tool callers. This work is needed
because a recent write returned `pushed: false` without an actionable warning and
required a subagent to push the local commit manually.

## Desired Outcomes

- Reproduce the shell/server credential difference, identify its root cause, and
  select a verifiable fix that requires no MCP client configuration change.
- After credentials become available, the same mutating tool call automatically
  recovers and pushes within at most three attempts.
- If all push attempts fail, `wiki_write_page` and `wiki_update_page` return an
  accurate push warning with `pushed: false` while preserving the successful local
  page write and commit.

## Health Metrics

- Page writes and local commits remain fail-soft when the remote push is unavailable.
- The MCP server never opens an interactive credential prompt.
- Existing inter-process locking for git operations remains effective.
- Existing tool result fields retain their current meaning; the warning is additive.
- Retry work is bounded to three attempts per mutating tool call.

## Strategic Context

- Interacts with: `wiki_write_page`, `wiki_update_page`, `sync.commit_and_push`, git
  credential helpers or SSH agents, the MCP server process environment, and MCP
  clients that consume tool results.
- Priority trade-off: trust over speed over cost. A push is reported successful only
  after Git confirms it; bounded retry is acceptable to improve recovery.

## Constraints

### Steering (behavioral guidance)

- Diagnose the actual server-process credential path before selecting a recovery
  mechanism.
- Preserve the existing fail-soft local-write behavior and return precise terminal
  push failure information.
- Prefer the smallest recovery mechanism compatible with current git synchronization
  and locking boundaries.

### Hard (architectural enforcement)

- The solution must not require changes to MCP client configuration.
- Do not store or copy credentials into iwiki configuration, the wiki base, or this
  repository.
- Do not invoke interactive credential prompts or weaken Git, SSH, or host-verification
  checks.
- Do not exceed three push attempts within one mutating tool call.

## Autonomy Zones

- Full autonomy (reversible, low risk): reproduce the environment difference; add
  focused tests; propagate an existing push warning; implement up to three bounded
  attempts within the existing call.
- Guarded (log + confidence threshold): change discovery or use of an existing
  credential helper or SSH agent without changing client configuration.
- Proposal-first (needs approval): add a background process, credential broker, or new
  persistent state.
- No autonomy (human only): store or copy secrets, disable host verification, or use
  force Git operations.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules

- Halt if no reliable non-interactive credential source can be made available without
  changing MCP client configuration.
- Escalate before adding a background process, credential broker, persistent state, or
  any mechanism that handles credential material directly.
- Done when the server-process credential failure has a reproduced and evidenced root
  cause; an allowed fix recovers push within three attempts after credentials become
  available; and exhausted attempts return an exact warning from both write tools while
  retaining the local commit.
