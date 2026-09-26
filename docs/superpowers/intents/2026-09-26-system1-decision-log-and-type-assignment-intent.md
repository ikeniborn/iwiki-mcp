---
review:
  intent_hash: 6f1e6e24f94b91d5
  last_run: 2026-09-26
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings:
    - id: F-001
      phase: alignment
      severity: INFO
      section: null
      section_hash: null
      fragment: null
      text: "Intent recorded after implementation; the task page carried the goal meanwhile."
      fix: "none"
      verdict: accepted
      verdict_at: 2026-09-26
workflow:
  route: chain
  continuation: execute
result_check:
  verdict: OK
  source: intent
  intent_hash: 6f1e6e24f94b91d5
  last_run: 2026-09-26
  reviewed: true
  docs_checked: true
---
# Intent: system1-decision-log-and-type-assignment

**Date:** 2026-09-26
**Status:** approved

## Objective

Make System One decisions accumulate into evidence and training data, and let the
fine-tuned `laya-iwiki` type pages that authors leave untyped. Until now each write's
decision was computed and dropped, so warning usefulness could not be measured and no
reviewed labels could be collected for the next fine-tune; the hosted server typed every
untyped page as `concept` because it has no chat classifier.

## Desired Outcomes

- Every System One write decision on the local and hosted servers is appended to a
  private log that references the page (domain, identity, body hash) without its body.
- `python -m eval.system1_decisions` reports warning count and acceptance and exports
  human-reviewed labels, excluding System One's own assignments.
- A page written without `type` gets a confident, non-weak System One type with a
  warning; an explicit type is never replaced.
- A written rule separates `concept` from `reference`, and the disputed pages are
  listed for their owners.

## Health Metrics

- Writes succeed and keep explicit types regardless of System One or log failures.
- The log never contains page bodies and is owner-only (`0600`).
- The hosted container stays read-only except for the decision-log mount.
- The full test suite passes; tests make no network calls.

## Strategic Context

- Interacts with: iwiki-mcp write paths, the Framework System One alias `laya-iwiki`,
  the hosted container and its host, iClaude and iCodex launchers, page owners.
- Priority trade-off: trust first, then speed, then cost.

## Constraints

### Steering (behavioral guidance)

- Prefer an append-only JSONL log over a PostgreSQL migration.
- Decide `concept` versus `reference` by the question a page answers.

### Hard (architectural enforcement)

- Page bodies and credentials never enter the decision log.
- Explicit page types are never replaced; retyping existing pages is the owner's call.
- Every System One feature fails open.

## Autonomy Zones

- Full autonomy (reversible, low risk): code, tests, report tool, docs, client config.
- Guarded (log + confidence threshold): enabling assignment and logging on hosted.
- Proposal-first (needs approval): retyping existing pages of any domain.
- No autonomy (human only): using logged data to train without review.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules

- Halt if: the log would need page bodies, or a failure path changes a write outcome.
- Escalate if: the hosted host cannot provide a private writable directory.
- Done when: a live untyped hosted write is typed by System One and logged without its
  body; local logging works; the report tool passes its tests; the rule and review list
  are published; the full suite passes.
