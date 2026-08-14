---
workflow:
  route: chain
  continuation: execute
review:
  intent_hash: f3a4c1e8e83e8d64
  last_run: 2026-08-14
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings: []
result_check:
  verdict: OK
  source: intent
  intent_hash: f3a4c1e8e83e8d64
  last_run: 2026-08-14
  reviewed: true
  docs_checked: true
---

# Intent: iwiki-config-template-initialization

**Date:** 2026-08-14
**Status:** approved

## Objective

Prevent server-side processes from overwriting manually maintained project
configuration. A project with no usable configuration must receive complete,
safe examples once; after that, manual configuration is authoritative.

## Desired Outcomes

- A missing or empty `.iwiki.toml` is created or filled with a complete,
  commented template covering Git storage, PostgreSQL storage, and
  `code_graph` settings.
- A missing or empty `.iwikiignore` is created or filled with a complete,
  commented template covering secrets and common project noise.
- A non-empty `.iwiki.toml` or `.iwikiignore` stays byte-identical during
  server operation.
- An operation that would automatically rewrite a non-empty configuration file
  returns a controlled server error for the agent, while unrelated agent work
  can continue.

## Health Metrics

- Existing valid project configuration remains readable and keeps its current
  binding, storage, and code-graph behavior.
- Manual edits remain the source of truth.
- Operations that do not need to write either configuration file continue to
  work when the files are absent.
- Existing `wiki_bind`, domain creation, ignore matching, and configuration
  parsing behavior remains compatible except for prohibited automatic rewrites.

## Strategic Context

- Interacts with: MCP agents, `wiki_bind`, domain creation, binding resolution,
  ignore matching, project maintainers, and code-graph configuration.
- Priority trade-off: trust in manual configuration over automatic convenience.

## Constraints

### Steering

- Use small, explicit initialization helpers and match existing error-response
  conventions.
- Keep templates educational, commented, and free of credentials or machine
  paths.
- Test observable behavior for absent, empty, and populated files.

### Hard

- Only a missing file or a file with no non-whitespace content may be populated
  automatically.
- A file with any non-whitespace content must remain byte-identical; the server
  must not correct, merge, normalize, or rewrite it.
- A prohibited automatic write must return a structured error rather than raise
  an uncaught exception or block unrelated agent work.
- `.iwiki.toml` examples must cover Git storage, PostgreSQL storage, and
  `code_graph`; `.iwikiignore` examples must cover secrets and common noise.
- Do not modify the user-owned `.iwiki.toml` worktree change.
- Every repository change includes the required patch-version bump.

## Autonomy Zones

- Full autonomy: implementation, tests, and documentation within these
  constraints.
- Guarded: server-error shape changes must follow existing API conventions and
  have focused regression coverage.
- Proposal-first: expanding the public contract or supported template modes
  beyond this intent.
- No autonomy: modifying the user-owned `.iwiki.toml`, stashing it again,
  committing, pushing, or opening a pull request without separate approval.

## Stop Rules

- Halt if a non-empty configuration file changes during a reproduced scenario.
- Escalate if preserving non-empty files conflicts with an existing public
  configuration mode or requires a new compatibility decision.
- Done when: missing and empty files receive complete templates, populated files
  stay byte-identical, prohibited rewrites return controlled errors, and focused
  plus full verification passes.
