---
review:
  intent_hash: d7d7ae6ed589ff4a
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
  continuation: execute
---
# Intent: specification-mode-gwt-parameters

**Date:** 2026-08-31
**Status:** approved

## Objective

Allow project-declared specification mode parameters to take effect on the hosted server without requiring an operator to change one server-side value that affects every domain. Keep the effective mode isolated to the bound session and resolved independently for each bound domain.

## Desired Outcomes

- A project that binds with a declared specification mode applies that mode only to the domains bound in its current hosted session.
- Other projects and sessions retain their own effective modes and are not changed by another project's declaration.
- An operator does not need to edit hosted server configuration merely to apply a stricter mode declared by one project.

## Health Metrics

- An exact hosted override retains highest precedence.
- A project declaration cannot weaken the hosted default.
- Local Git and SQLite specification-mode behavior remains unchanged.
- Clients that omit the new parameter retain existing behavior.
- A rejected `wiki_bind` value leaves the previous binding unchanged.
- Existing focused and full regression tests remain passing.

## Strategic Context

- Interacts with: project `.iwiki.toml`, `wiki_bind`, hosted HTTP session state, the per-domain specification policy resolver, PostgreSQL server configuration, `wiki_status`, specification mutation enforcement, project agents, and deployment operators.
- Priority trade-off: trust first, compatibility second, speed and cost third.

## Constraints

### Steering (behavioral guidance)

- Carry the project declaration through `wiki_bind`; do not make the hosted server read a client checkout.
- Keep the change narrow to session-scoped project mode selection and its observable status.
- Resolve the project declaration independently for each bound domain.

### Hard (architectural enforcement)

- Store the project-declared mode only in hosted session state; do not persist it or add a database migration.
- An exact hosted override always wins.
- A project mode may only be at least as strict as the hosted default; a looser declaration is suppressed.
- `[specifications].allow_project_mode = false` disables the project tier completely.
- Accept only `disabled`, `optional`, or `strict` as `wiki_bind.specification_mode`.
- Invalid input must not mutate the previous binding.
- A client that omits `specification_mode` must observe existing hosted behavior.
- Local Git and SQLite mode resolution and `source: project` reporting must remain unchanged.

## Autonomy Zones

- Full autonomy (reversible, low risk): add focused tests, make scoped local code changes, update English documentation and Wiki pages, and bump the patch version.
- Guarded (log + confidence threshold): change `wiki_bind`, hosted session state, configuration parsing, and the per-domain policy resolver only within this approved intent and with focused plus full regression evidence.
- Proposal-first (needs approval): change precedence, the tighten-only guard, accepted mode values, session scope, or compatibility for clients that omit the parameter.
- No autonomy (human only): add a database migration, persist project mode, introduce a global project setting shared across unrelated sessions, or allow a project to weaken server policy.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules

- Halt if: implementation requires weakening server policy, changes an unrelated project's effective mode, contradicts the documented precedence, or requires persistent storage.
- Escalate if: session isolation cannot be proven, invalid bind input can partially mutate state, or per-domain resolution conflicts with existing mutation enforcement.
- Done when: executable Given-When-Then scenarios prove project-level mode application, cross-project isolation, exact-override precedence, tighten-only suppression, server-switch suppression, per-domain resolution, invalid-bind rollback, local-backend compatibility, and strict mutation enforcement; focused and full pytest suites pass; operator documentation and project Wiki are consistent.
