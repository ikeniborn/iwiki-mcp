---
topic: code-graph-publication-quotas
stage: intent
workflow:
  route: chain
  continuation: full
review:
  intent_hash: 09faa2f5fafb445e
  last_run: 2026-11-23
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings: []
---
# Bound resource use during hosted graph publication

**Date:** 2026-11-23
**Status:** approved

## Objective

Protect shared hosted publication capacity when publishers accumulate unfinished uploads. Per-batch limits alone cannot bound aggregate staged data or the number of open sessions.

## Desired Outcomes

- The server operator sets limits for both open session count and staged data volume. Each resource has a server-wide ceiling and a per-publisher ceiling aggregated across that owner's domains.
- Concurrent requests that fit within both ceilings can proceed. A request that would exceed either applicable ceiling receives an immediate refusal identifying the resource (sessions or staged data). The server does not queue the request or disclose another publisher's information.
- Refusal leaves the active graph readable and all acknowledged batches intact. Replaying an acknowledged batch incurs no additional charge. A publisher can retry a refused batch after capacity becomes available while the lease is valid.
- Lowering a ceiling below existing usage preserves current sessions and data. Only increases that exceed the affected ceiling are refused. Finalization, cancellation and replay remain available under the existing ownership, integrity and lease rules.
- Ending a session releases its open-session capacity. Account for retained staged data until cleanup reclaims it. Demonstrate recovery of capacity after completion, cancellation and lease expiry.

## Health Metrics

Compare against 0.7.325 on matched inputs below quota. Measure median end-to-end publication time and p95 batch acknowledgement latency separately for a single publisher and concurrent publishers in different domains. Investigate any increase above 15% in any of the four comparisons before release. Measure peak finalization memory separately from staged data volume.

## Strategic Context

- Interacts with: hosted PostgreSQL publication, MCP clients, publisher identity, batch receipts, lease expiry and staging cleanup.
- Priority trade-off: preserve ownership, retry safety and atomic graph visibility while limiting aggregate resource use. Python reference resolution and SQLite symbol lookup are subsequent, separate tasks.

## Constraints

### Steering

Reuse existing publication operations, receipts and cleanup semantics. Document the accounting unit and the point at which each resource becomes available again.

### Hard

Enforce both ceilings under concurrent admissions and retries. Preserve existing access checks and the previously active revision on refusal. Never evict accepted work solely because a limit was lowered.

## Autonomy Zones

- Full autonomy: inspect the existing lifecycle and select the accounting and atomic admission mechanisms.
- Guarded: test contention, retries, cleanup and performance on disposable data.
- Proposal-first: obtain approval for numeric defaults, the client response format and any persistent data migration.
- No autonomy: expose another publisher's usage or change the operator's live limits without authorization.

## Stop Rules

- Return for review if an internal design requires changing an agreed outcome or migrating stored data.
- Investigate regressions above the agreed performance threshold before release.
- Done when: concurrent boundary tests verify both resource limits at both scopes, retries preserve receipts and avoid double charging, cleanup restores usable capacity, and rejected publication leaves the active revision readable. Record both performance metrics for both workloads.
