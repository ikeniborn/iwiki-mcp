# Intent: guard iwiki bind scope

**Date:** 2026-07-04
**Status:** approved

## Objective
Prevent iwiki MCP startup and binding flows from causing unwanted project bootstrap or isolated-environment loading, and prevent agents from silently rewriting `.iwiki.toml` binding scope in other projects.

## Desired Outcomes
- Opening a project no longer triggers iwiki-driven bootstrap or automatic isolated-environment loading through agent instructions.
- `wiki_bind(read=...)` does not overwrite an existing non-empty `read` list.
- If `read` contains only other domains and does not contain the current project domain, `wiki_bind` may add the current project domain while preserving the existing domains.
- `wiki_bind(write=...)` only accepts the current project domain as the write target.
- Tests cover forbidden `read` replacement, allowed current-project-domain append, and rejected non-current `write`.

## Health Metrics
- Existing projects with valid `.iwiki.toml` files keep their configured read domains.
- `wiki_bind` remains idempotent and continues validating all referenced domains.
- Existing project-scope search behavior remains unchanged for valid bindings.
- The full pytest suite remains green.

## Strategic Context
- Interacts with: `base.write_project_config`, `server.wiki_bind`, `.iwiki.toml`, agent instruction templates, README setup guidance, iwiki MCP callers in Codex/Claude projects.
- Priority trade-off: trust.

## Constraints
### Steering (behavioral guidance)
- Prefer refusing ambiguous binding changes over silently correcting agent intent.
- Keep changes surgical and focused on startup/binding behavior.
- Keep documentation and templates explicit that agents should not bootstrap wiki/domain state during ordinary project opening.

### Hard (architectural enforcement)
- Existing `read` domains in `.iwiki.toml` must not be removed or replaced by `wiki_bind`.
- `wiki_bind` may append only the current project domain to an existing non-empty `read` list when that domain is missing.
- `wiki_bind(write=...)` must reject any write domain other than the current project domain.
- The `write` guard must be included in scope and tests.
- `write` updates are not allowed to bypass domain validation.

## Autonomy Zones
- Full autonomy (reversible, low risk): local analysis, tests, intent/spec wording edits.
- Guarded (log + confidence threshold): editing agent instruction templates and README text when the edit only removes iwiki bootstrap behavior or documents binding guards.
- Proposal-first (needs approval): any design that changes the semantics of `read`, `write`, project domain inference, or existing project compatibility beyond the constraints above.
- No autonomy (human only): removing domains from any project `.iwiki.toml`, force-push/reset, or editing other projects' configs.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules
- Halt if: the current project domain cannot be inferred deterministically from `project_dir` or existing binding data.
- Escalate if: preserving `read` conflicts with a documented setup flow or with existing tests that intentionally rely on replacement.
- Done when: the problematic bind scenarios are reproduced in tests, tests pass, docs/wiki are updated, and `.iwiki.toml` `read`/`write` can no longer be changed contrary to the constraints above.
