---
topic: codegraph-index-job-handle
stage: intent
review:
  intent_hash: 90b8c7961bfb3679
  last_run: 2026-09-10
  phases:
    structure: passed
    completeness: passed
    clarity: passed
    consistency: passed
    alignment: passed
  findings:
    - id: F-001
      phase: clarity
      severity: WARNING
      section: Health Metrics
      section_hash: d26f68f764520f77
      fragment: "stays at or under ~86 s and its publication phase at or under ~2 s"
      text: "The rebuild-speed metric was stated approximately; a tilde threshold cannot be
        passed or failed deterministically."
      fix: "Named the exact bound and its measurement source: 100 s end to end and 5 s in
        the publication phase, read from duration_ms and phase_timings_ms.publication."
      verdict: fixed
      verdict_at: 2026-09-10
    - id: F-002
      phase: alignment
      severity: INFO
      section: Objective
      section_hash: d3ef7b04bed7cde1
      fragment: "The change is a full job handle rather than only the missing cancel"
      text: "Scope is wider than the measured defect: the session measured cancel-versus-keep,
        and the user chose the full job handle over the minimal fix."
      fix: "None required; recorded so the spec keeps the wider scope deliberate."
      verdict: open
      verdict_at: null
---
# Intent: codegraph-index-job-handle

**Date:** 2026-09-10
**Status:** approved

## Objective

`wiki_code_index` runs its rebuild on a background worker but waits for it inside the call, and when that wait expires it cancels the worker and answers `busy`. Cancellation is cooperative and only checked at phase boundaries, so the interrupted build keeps parsing for another minute or more and then leaves the graph `failed` — the whole rebuild is paid for and discarded, and a retry pays it again.

Measured on this repository (113 files, ~86 s full rebuild) with an identical 5 s wait: the shipped path answers `busy` in 5.01 s, the worker stays busy ~75 s, and the run ends `failed`; a prototype that does not cancel answers `rebuilding` in 5.01 s, spends the same ~75 s, and ends `ready`, `fresh: true`.

The change is a full job handle rather than only the missing cancel: `wiki_code_index` returns a job descriptor immediately, and `wiki_code_status` reports that job's progress and outcome. This is requirement 8 of `reference/snapshot-freshness-and-republication-friction`, and it is due now because the publication cost was just removed (`codegraph-publication-timeout`), leaving the client call budget as the only remaining reason a rebuild cannot be driven over MCP.

## Desired Outcomes

- `wiki_code_index` returns a job descriptor in under one second on a repository of any size, without waiting for the build.
- A build that a call started reaches `ready` and publishes regardless of whether the caller waited for the answer; no rebuild is discarded because a client deadline expired.
- `wiki_code_status` reports the running job's phase and progress while it runs, and the same job's terminal outcome (`ready` or `failed`) after it finishes.
- While a job is live, a second `wiki_code_index` joins it instead of answering `busy` or starting a second build.

## Health Metrics

- Publication atomicity is unchanged: the replace → provisional `rebuilding` → verification #1 → `ready` → verification #2 order, the writer lock, and the rule that readers never observe an unproven snapshot all hold.
- `iwiki-mcp code publish` stays synchronous with its current exit codes (0 ready, 1 runtime/publication failure, 2 usage/configuration) and its current text and `--json` shapes.
- Rebuild speed does not regress: on this repository a full rebuild stays at or under 100 s end to end and its publication phase at or under 5 s, measured through `wiki_code_index`'s own `duration_ms` and `phase_timings_ms.publication` (86.1 s / 2.0 s on the run that opened this topic).
- Exactly one build worker per process, with no new contention for the per-domain writer lock.

## Strategic Context

- Interacts with: `wiki_code_index` and `wiki_code_status` on the MCP surface; `codegraph/runtime.py` (`_BUILD_WORKERS`, `_index_with_deadline`, `status`); `codegraph/indexer.py` (`BuildControl`, phase-boundary cancellation); `codegraph/application.py` (`index_and_publish`, used by both the tool and the CLI); `admin.py` (`iwiki-mcp code publish`); the `code_graph` block of `wiki_lint`; and the stdio server's idle shutdown, which calls `shutdown_code_graph_workers` and cancels a live job.
- Priority trade-off: **trust**. An answer that cannot be proven is refused or reported as unknown rather than guessed; a fast answer that misstates the graph's state is worse than a slower honest one.

## Constraints

### Steering (behavioral guidance)

- Prefer the existing state vocabulary (`ready`, `rebuilding`, `dirty`, `failed`, `busy`) over new words; a job descriptor identifies work, it does not invent a new lifecycle.
- Keep cancellation for the two cases that mean it — the build's own deadline and server shutdown — and stop using it to end a caller's wait.
- Report progress from facts the build already produces (phase names, phase timings), not from an estimate.

### Hard (architectural enforcement)

- No new MCP tool. The registered surface stays at 35 tools; the descriptor and the progress are carried by `wiki_code_index` and `wiki_code_status`.
- No external job store. Job state lives in the existing snapshot metadata and in process memory — no new table, file, or queue.
- Surviving the death of the MCP session is not required: a stdio server lives only as long as its client session, and a job may die with it, provided the state it leaves behind is truthful.
- A way to wait for the complete report inside one call must remain available, for scripts and tests.

## Autonomy Zones

- Full autonomy (reversible, low risk): implementation, refactoring inside `codegraph/`, test design, internal names, phase-progress plumbing.
- Guarded (log + confidence threshold): changes to cancellation call sites, deadline arithmetic, and worker-registry lifecycle.
- Proposal-first (needs approval): the final answer shape of `wiki_code_index` and `wiki_code_status`, and any new or renamed `[code_graph]` configuration key with its default.
- No autonomy (human only): merging the pull request.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules

- Halt if: publication atomicity cannot be preserved alongside a non-cancelled worker — for example if leaving a build running past the caller's wait can make two writers race for the domain lock.
- Halt if: an honest terminal state cannot be reported after the job's process or session ends, since a stale `rebuilding` would violate the trust priority.
- Escalate if: the answer shape needed for a job descriptor cannot be expressed without a new MCP tool or a new persistent store.
- Escalate if: rebuild wall time regresses beyond the health metric on this repository.
- Done when: on this repository, `wiki_code_index` returns in under one second; `wiki_code_status` shows the job progressing and then reports `ready` with a fresh revision for the same run; a second `wiki_code_index` issued while the job runs joins it rather than answering `busy`; and the CLI publish path still exits 0 with an unchanged answer shape.
