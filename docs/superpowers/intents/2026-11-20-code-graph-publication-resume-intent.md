---
topic: code-graph-publication-resume
stage: intent
workflow:
  route: chain
  continuation: full
review:
  intent_hash: 0872cefc9cfc9e32
  last_run: 2026-11-20
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings: []
---
# Resume publication of a completed code graph

**Date:** 2026-11-20
**Status:** approved

## Objective

Let the operator finish uploading the exact completed graph to the configured hosted MCP target after connectivity loss or CLI process restart.

## Desired Outcomes

- Restart a publisher with five of eight batches already accepted. Verify the server's receipts and send only the three missing payloads, including when the fifth reply was lost.
- If activation succeeded but its reply was lost, identify the committed revision and report success without publishing again. A later publication must never be overwritten during recovery.
- Report an actionable outcome if the lease expired, the pinned bytes are missing or corrupt, another owner attempts recovery, or the target changed. Preserve the currently visible graph.

## Health Metrics

A recoverable upload performs zero source parses, resends zero verified batches and activates at most once. Readers see one complete revision. Existing CLI output and exit codes remain compatible for ordinary publication.

## Strategic Context

Stage 1 provides process-local progress. The current CLI exports a completed graph and opens a publication session. The PostgreSQL store already checks ownership and leases and accepts idempotent batches. Design must reconcile those server records with a durable local copy across process lifetimes.

## Constraints

### Steering

Design for CLI-to-hosted MCP first. Keep the direct PostgreSQL and SQLite publication paths compatible. Measure local disk use and network round trips when selecting the recovery format.

### Hard

Preserve canonical batch identity, negotiated limits, explicit project binding and atomic activation. Protect retained graph data and omit credentials from recovery records. File parsing checkpoints belong to stage 3.

## Autonomy Zones

- Full autonomy: inspect contracts and design disposable verification fixtures.
- Guarded: propose retention limits, receipt pagination and disk cleanup based on existing limits.
- Proposal-first: agree CLI behavior, capability negotiation with 0.7.320, crash-safe ownership and recovery after lease expiry in the specification.
- No autonomy: production deployment, grant changes and deletion of operator data.

## Stop Rules

- Stop recovery if ownership, payload identity or target cannot be verified.
- Escalate if a compatibility or storage requirement needs a migration or wider authority.
- Design is ready when an approved specification and checked plan cover interruption before and after acknowledgements, restart, lost finalization, concurrent attempts, expired leases and older peers.
