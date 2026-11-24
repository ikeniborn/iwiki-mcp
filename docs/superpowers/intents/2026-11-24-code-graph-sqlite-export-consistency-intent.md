---
workflow:
  route: chain
  continuation: execute
review:
  intent_hash: 411faa887e39ca1f
  last_run: 2026-11-24
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings: []
---
# Intent: code-graph-sqlite-export-consistency

**Date:** 2026-11-24
**Status:** approved

## Objective

Give each successful export of a ready SQLite code graph one source revision. At e243769, switching snapshots between table reads produced revision A's repository record and revision B's files. A payload digest computed from those rows cannot detect their different origins.

## Desired Outcomes

- Compare the exported header, ordered repository, file, symbol and relation rows, and payload digest with a reference export of A. They must match exactly when activation of B is requested during capture of A.
- Observe activation of B after local copying finishes, even while transfer of A is stalled. A subsequent export must match the reference export of B.
- When a writer already holds the local graph lock, an attempt to capture a new export reports contention immediately through the existing caller error format. It opens zero remote publication sessions and preserves any existing checkpoint.
- A retry with a valid pinned upload continues to use that pin. Preparing a new resumable upload retains the existing indexed-revision conflict check.

## Health Metrics

- Limit candidate median local row-copy time to 1.15 times baseline on the same 20,000-file SQLite fixture, host and runtime. Use one warm-up followed by five timed copies per revision. Exclude hashing, checkpoint creation and transfer.
- The baseline median at e243769 was 0.4367 seconds for one repository, 20,000 files, 39,998 symbols and 59,997 relations. The relative threshold governs comparable measurements.
- Use synchronization points to verify that activation completes before a stalled transfer is released. Check contention using the existing nonblocking reader contract.

## Strategic Context

- Interacts with: the local SQLite graph store, indexer activation, ordinary publication and creation of a resumable upload pin.
- Priority trade-off: preserve snapshot integrity first. Bound the cost to the local copying phase and preserve the existing writer deadline.

## Constraints

### Steering

- Reuse the current graph coordination and failure handling where they meet these outcomes.
- Exercise the real activation path with two distinguishable ready snapshots.

### Hard

- Release capture protection before hashing the payload, preparing the checkpoint or contacting the publisher to begin transfer.
- Preserve canonical ordering, payload encoding, publication protocol, schema, checkpoint compatibility and existing CLI error formats.
- Preserve the existing activation checks for an in-use canonical database and its SQLite sidecar files.

## Autonomy Zones

- Full autonomy: choose internal read helpers and deterministic test fixtures.
- Guarded: adjust lock scope within the existing coordination model and verify release on success and failure.
- Proposal-first: request approval for a new public error, a different contention policy or changes to performance limits.
- No autonomy: deploy to framework or migrate production storage under this intent.

## Stop Rules

- Halt if a successful export mixes revisions, or a failed capture opens a remote upload or alters an existing checkpoint.
- Escalate if meeting the outcomes requires changing the public contract or exceeding the agreed copy-time threshold.
- Done when: the A/B comparison, writer-contention, stalled-transfer and resume checks pass, existing publication tests pass, and the measured median ratio is at most 1.15.
