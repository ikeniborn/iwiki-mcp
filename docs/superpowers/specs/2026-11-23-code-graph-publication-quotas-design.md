---
topic: code-graph-publication-quotas
stage: spec
status: approved
chain:
  intent: docs/superpowers/intents/2026-11-23-code-graph-publication-quotas-intent.md
review:
  spec_hash: fd253a5ec468460f
  last_run: 2026-11-23
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings: []
---
# Hosted code graph publication quotas

## Boundary and accounting unit

Implement the [approved intent](../intents/2026-11-23-code-graph-publication-quotas-intent.md) on the hosted PostgreSQL publication path. A server pool covers every publisher process using one publication database. Charge each session to its authenticated publication owner across that owner's domains. Keep session authorization separate from quota attribution. Never accept quota identity or limits from MCP callers. Local parsing and SQLite-only publication keep their current contracts.

Count live staging sessions with an unexpired effective lease. Count logical bytes of surviving staged batch payloads using the validated canonical batch byte count. Physical database overhead, WAL, published graph rows and peak finalize memory require separate measurements. An expired session consumes no live slot, but its retained payload still consumes bytes until deletion commits. A zero-usage owner can begin even when bytes are exhausted; a live session can send bytes when the slot limit is exhausted.

## Database-verifiable publisher identity

Hosted requests share a database login. Migration 3 guarantees token ID uniqueness. The existing Python authentication path verifies the bearer, but a token ID alone gives the publication database no evidence of the caller's identity.

Use a separate issuer login in the trusted authentication path. After authenticating the request, issue a short-lived single-use random ticket. Bind it to the token, tenant, domain, operation and canonical request arguments, including the attempt or session and batch digest where applicable. Store only the ticket digest in a private table. Pass the ticket as a bound parameter. Exclude credentials and tickets from logs, diagnostics, pooled session state and checkpoints. Bound the retention and cleanup of unused tickets.

Verify the ticket and current token and domain permissions inside the publication transaction. Derive the hosted quota owner from the verified token. Commit consumption of the ticket with the mutation. Authenticate again when issuing a fresh ticket for a retry. Deny the publication login access to ticket issuance, authentication record changes, private usage and alternative mutation paths, including cascades. Give cleanup its own bounded operation. Prevent the hosted login from invoking the direct publication path.

For new direct sessions, derive quota identity from session_user, in a namespace separate from hosted tokens. Keep the existing per-session access checks when multiple clients share a login. Use the login's stable database identity. Before replacing a role, check for outstanding sessions.

Preserve each legacy session's persisted owner and accepted packets until ordinary release. Include its live slot and retained bytes in the global pool. Count legacy usage matching an authenticated hosted token against that token's quota. Keep unattributable direct usage under its historical owner until release. Preserve existing session access checks. Expose no legacy begin path.

## Admission and privacy

Proposed operator defaults are 64 live sessions and 2 GiB per server, 8 live sessions and 256 MiB per publisher. These are starting values for approval. The observed 20,000-file fixture transferred about 87.5 MB; it demonstrates headroom for one upload, not production concurrency. Validate positive integer limits and document their effective configuration point. All serving replicas must apply the same policy; changing policy must not leave older writers enforcing a higher ceiling. No automatic eviction or queued capacity wait.

Preserve existing authentication, domain, owner, lease, input validation and conflict precedence. On quota refusal propose the existing error envelope with a stable code `publication_quota_exceeded` and `resource` equal to `sessions` or `staged_bytes`. Do not expose which owner, balances, competing domains or competing session IDs. Preserve existing client failure categories; carry the safe resource through supported CLI recovery diagnostics so an operator can decide what to retry. Older clients must still recognize failure. Ordinary database contention uses the existing bounded busy/retry path and is distinct from exhausted capacity.

## PostgreSQL design

Prefer a private accounting table with one row per publishing session, recording session identity, its owner, live/terminal state, effective expiry and charged bytes. Index owner and expiry for admission and reclamation. This avoids repeated joins and scans of batch payloads. A process-local counter cannot enforce a ceiling across replicas. A domain-filtered sum cannot enforce a server-wide ceiling across row-level security boundaries.

Serialize lease extensions, including extensions on receipt replay, with slot admission. Check the original effective expiry with the database clock after locking, including for sessions without a recovery deadline. Serialize positive admissions across replicas using a dedicated transaction-scoped database lock and a fresh committed view under READ COMMITTED after taking it. Enforce server and owner checks together. Keep the critical section short; bench the aggregate query as retained sessions accumulate. Do not hold the global admission lock during graph materialization, network calls or sweeping an entire backlog. Bound lock acquisition and document one order for session, receipt and accounting locks; validate concurrent begin, batch, finalize and cleanup with real connections. An atomic decrement or removal in the same transaction as a release can cause a transient conservative refusal but cannot create capacity before commit.

Use narrowly scoped database routines for global reads/updates. Restrict the table, function ownership, grants and search path. Expose only the admission decision to the serving principal. Bind mutations to the authorized session and verified byte delta. Direct database calls as the serving role must not permit choosing another owner, forging a release or reading global usage. Treat a missing or inconsistent accounting row as a storage/integrity error before positive admission.

## Lifecycle

- Begin: resolve authorized idempotent replay first. Reserve one live slot and create the attempt, session, accounting row and staging snapshot in one transaction. Refusal leaves no attempt key or orphan snapshot. Unique-key race losers reuse the winner's committed result.
- Batch: an accepted receipt with the same hash is replayable without charging. A different hash retains its conflict. For a new receipt, check both byte budgets, insert the batch, charge its bytes and renew the lease atomically. Refusal preserves the old lease and acknowledges no bytes. Never revive an expired session through renewal.
- Finalize: preserve active revision checks, atomic replacement and durable terminal replay. The current store marks the session ready but leaves its batches. Proposed change: delete staged batches and release their bytes in the activation transaction after materialization. After cleanup removes a superseded session, both inspect and finalize return the authorized attempt’s saved terminal result within its recovery deadline. Verify compatibility for both legacy and resumable sessions. If finalization rolls back, the receipts, charges and prior active graph remain intact.
- Abort and cleanup: release exactly the bytes actually deleted in that transaction. Release the slot once on terminal transition. Coordinate cascaded deletes and bounded sweeps with accounting-row deletion. Expiry frees the live slot even before cleanup. Verify session- and attempt-retention rules independently. Replay, inspect, finalize and abort preserve existing owner and deadline checks when policy is lowered.

## Migration and rollout proposal

A schema migration is required for the proposed accounting table and routines. Stop old publication writers before backfill, including background cleanup. Populate rows for existing live, expired and retained terminal sessions from stored sessions and receipts. Reconcile totals by owner and globally with an independent query before enabling admission. Reclaim historical terminal payloads via bounded maintenance only after verifying terminal replay and graph reads. Preserve over-limit data. Resume on a consistent policy across all writers. Prevent old binaries from writing around accounting. Rehearse rollback with a disposable populated database: quiesce writers, preserve accepted data and terminal evidence, validate compatibility and reconcile again before enabling writes. Obtain migration approval before implementation or live changes.

## Acceptance

Test exact boundary and one-over cases for each resource and scope. Race independent processes and different owners/domains for the last slot and bytes. Include duplicate begin, receipt replay, wrong owner, conflict, lost response, expiry, aborted transaction, restart, reduced limits and concurrent cleanup. Confirm refusals leak no cross-owner data and never change the visible revision. Test backfill over limit, rollback and restoration of capacity with no double release.

Compare 0.7.325 and the candidate using identical payloads, hardware, database state and concurrency below quota. Run repeated interleaved trials for one publisher and multiple publishers in different domains. Record median end-to-end time, p95 batch acknowledgement latency, sample counts, contention and peak finalize memory. Investigate any of the four latency comparisons above +15% before release. Keep resolver and SQLite membership optimizations in their separate topics.
