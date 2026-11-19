---
topic: code-graph-security-performance-audit
stage: intent
workflow:
  route: chain
  continuation: full
review:
  intent_hash: 30f4ac48d9c08b4d
  last_run: 2026-11-19
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings: []
---
# Intent for stage 1 of the code graph audit

**Date:** 2026-11-19
**Status:** approved

## Objective

Let an operator tell what an ongoing local build and its publication have completed. The audit measured 106.75 seconds in parsing alone. It also found that a stale target snapshot hides a known running job from its local poller.

## Desired Outcomes

- A caller can poll a known job on its originating local MCP process during collection and publication. A stale, missing or unreachable target still permits inspection of local progress.
- The caller can distinguish the job outcome from the availability and freshness of the active target snapshot. A failed publication reports a failed job even after successful indexing.
- During a build, the operator sees the current phase, the number of files parsed and the time of the last progress update. During transfer, the operator sees acknowledged batches and bytes. A total is shown only when known.
- A CLI operator receives intermediate progress on stderr before the command finishes. A script using the existing JSON output receives one final JSON document on stdout.

## Health Metrics

- Existing CLI exit codes and final JSON fields keep their documented meanings in success and failure cases.
- Parsed-file and acknowledged-transfer counts are monotonic within one job. Repeated acknowledgement of the same batch counts once.
- Idle polling causes zero additional builds or publication writes. The number of emitted CLI updates stays bounded by elapsed time and phase changes.

## Strategic Context

- Interacts with: local MCP callers, CLI operators, scheduled publishers, the indexer and the selected publication adapter.
- Priority trade-off: preserve truthful status and compatibility first. Limit reporting overhead during parsing and transfer.
- Existing evidence: [job handle intent](2026-09-10-codegraph-index-job-handle-intent.md) and [publication contract](../../code-graph-publishing.md).

## Constraints

### Steering

- Reuse the process-local build handle and progress already available from the worker.
- Make counters interpretable during discovery, parsing, transfer, finalization and terminal failure.

### Hard

- Keep progress scoped to the authorized local project and job. Preserve access checks before any job lookup.
- Expose only operational counts, phases, timestamps and existing safe outcome codes. Exclude source content, credentials, endpoints and checkout paths.
- Keep hosted snapshot reads, atomic activation and idempotent batch handling compatible with current clients.
- Limit stage 1 to observability while the originating process is alive. Durable upload recovery and parse checkpoints belong to stages 2 and 3.

## Autonomy Zones

- Full autonomy: choose internal naming and focused fixtures within the approved design.
- Guarded: choose throttling and refresh intervals after checking progress visibility and output volume.
- Proposal-first: agree the MCP response contract, CLI output behavior and verification approach during specification review.
- No autonomy: deployment, production load testing and changes to domain permissions require separate authorization.

## Stop Rules

- Halt if an authorization error could expose another project's job or if progress would disclose sensitive input.
- Escalate if the agreed outcomes require durable job storage, hosted observation of a remote worker, or a breaking client change.
- Done when: a local caller observes progress with a stale, missing or unreachable target, and sees the terminal publication outcome.
- Done when: a CLI run shows progress before completion and produces the documented final stdout and exit code on success and failure.
