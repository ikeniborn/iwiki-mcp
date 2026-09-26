---
review:
  intent_hash: 4c4b89beb78cde3a
  last_run: 2026-09-26
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

# Intent: laya-iwiki-type-guidance

**Date:** 2026-09-26
**Status:** approved

## Objective

Let the fine-tuned System One alias `laya-iwiki` influence iwiki behavior instead of
only being measured. The user wants two effects now: authors get a second opinion when
their explicit page `type` disagrees with the model, and `wiki_search` ranks pages whose
type matches the query's predicted intent higher. The shadow pilot proved the model is
fast (p95 < 300 ms) and better than the chat classifier on held-out pages, so its signal
is worth using where a wrong answer is recoverable.

## Desired Outcomes

- When System One guidance is enabled and a page is written with an explicit `type`,
  a confident non-weak `laya-iwiki` decision that differs from it adds one advisory
  warning naming the suggested type and its probability; the page, its `type`, tags,
  path, and write success stay exactly as authored.
- When the search boost is enabled, `wiki_search` read results whose page type equals
  a confident non-weak predicted query type move up by a bounded amount; the result set
  membership, `k`, and response shape are unchanged.
- On a known-item benchmark over hosted wiki pages (query = page description, target =
  the page), the boost is enabled in production only if MRR and hit@1 on the held-out
  split are not lower than without it.
- iClaude, iCodex, and the Framework-hosted server can enable each effect by
  configuration.

## Health Metrics

- With both flags off, write results, search results, and outgoing requests are
  byte-identical to 0.7.307.
- System One failure, timeout, invalid output, low confidence, or a weak predicted
  class (`runbook`, `guide`) produces no warning and no boost; the write or search
  otherwise succeeds unchanged.
- Added search latency stays within the existing two-second System One timeout.
- The full iwiki-mcp test suite passes; automated tests make no network calls.

## Strategic Context

- Interacts with: iwiki-mcp write path and `wiki_search`, the Framework System One
  endpoint `laya-iwiki`, the hosted iwiki server, iClaude and iCodex launchers, and
  authoring agents that read write warnings.
- Priority trade-off: trust first, then speed, then cost.

## Constraints

### Steering (behavioral guidance)

- Reuse the existing System One client and page-type question.
- Derive a candidate's page type from its governed path segment; pages without a
  classifiable type segment are never boosted.
- Tune boost parameters on the non-test split and judge them only on the test split.

### Hard (architectural enforcement)

- System One never changes a page's `type`, tags, path, or write outcome.
- The boost only reorders the existing candidate pool; it never adds or removes results.
- Both effects are off by default and fail open.
- Page bodies, queries, and credentials never enter logs or benchmark reports.

## Autonomy Zones

- Full autonomy (reversible, low risk): code behind default-off flags, tests, eval
  scripts, documentation.
- Guarded (log + confidence threshold): enabling warnings on hosted and client
  configurations; enabling the boost where the benchmark gate passes.
- Proposal-first (needs approval): enabling the boost when the gate fails; letting
  System One change `type` or result membership.
- No autonomy (human only): changing ACLs, bindings, or specification enforcement.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules

- Halt if: enabling either flag changes write success, page content, or result
  membership in tests or live checks.
- Escalate if: the boost lowers held-out MRR or hit@1; it stays off and the results
  are reported.
- Done when: warnings appear live on a disagreeing explicit-type write; the benchmark
  result is recorded; the boost is enabled on hosted only if the gate passed; clients
  and hosted expose the settings; the full suite passes.
