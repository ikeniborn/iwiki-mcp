---
workflow:
  route: chain
  continuation: execute
review:
  intent_hash: 1b2ea70211174be1
  last_run: 2026-11-26
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
  intent_hash: 1b2ea70211174be1
  last_run: 2026-11-26
  reviewed: true
  docs_checked: true
---
# Intent: code-graph-finalize-relation-batches

**Date:** 2026-11-26
**Status:** approved

## Objective

Reduce PostgreSQL backend peak RSS when activating graph relations. Insert up to 1,000 relations per SQL statement inside the existing finalize transaction.

## Desired Outcomes

- A graph with zero relations activates with an empty relation table for the new snapshot. For 1,000 and 1,001 relations, the insert sizes are respectively [1000] and [1000, 1].
- Readers observe one complete active snapshot. Persisted relation identities and contents, snapshot counts, and the final revision match the expected graph.
- A SQL error after a successful relation insert rolls back the entire activation. The previous revision remains active, staging survives, and a retry of finalize on the same upload can activate the complete graph.

## Health Metrics

Verify the statement bound and the persisted rows on PostgreSQL. Compare the integrated implementation with the existing full-snapshot measurement on a disposable target. Record peak backend RSS and finalize transaction duration separately. Report any regression together with the fixture and measurement conditions.

## Strategic Context

Interacts with: PostgreSQL publication staging, snapshot activation, graph readers and publication retries.

Priority trade-off: atomic activation and revision equality take precedence over memory savings. In the five-pair experiment with 23,137 relations, median backend peak RSS fell from 302.6 to 96.4 MiB. Median transaction duration was 1.805 s with one insert and 1.688 s with bounded inserts on that fixture.

## Constraints

### Steering

Reuse the current relation row projection and SQL insertion semantics. Use the existing disposable PostgreSQL publication tests and paired measurement as verification evidence.

### Hard

Use a fixed maximum of 1,000 relation rows per statement. Commit all activation writes together. Preserve authorization, quota accounting, upload receipts and the existing client protocol. Limit the change to relation insertion during PostgreSQL finalize.

## Autonomy Zones

- Full autonomy: choose internal names, fixtures and assertions for the approved boundaries.
- Guarded: change the relation insertion loop and its tests within the existing transaction.
- Proposal-first: change the batch size, schema, client contract or transaction boundary.
- No autonomy: deploy to production under this intent.

## Stop Rules

Stop if a failure exposes a partial revision, discards accepted staging data, or prevents retry on the same valid upload. Reassess the implementation if the integrated measurement fails to reproduce the backend memory benefit.

Done when: the boundary cases, persisted data and revision checks, rollback and retry tests pass, and the integrated measurement records the memory and timing outcome.
