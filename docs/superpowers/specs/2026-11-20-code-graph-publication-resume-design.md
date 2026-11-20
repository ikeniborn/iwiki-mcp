---
topic: code-graph-publication-resume
stage: spec
status: approved
chain:
  intent: docs/superpowers/intents/2026-11-20-code-graph-publication-resume-intent.md
review:
  spec_hash: 736de9111a569d76
  last_run: 2026-11-20
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings: []
---
# Recover a hosted code graph upload

## Scope and operator contract

Design baseline is 0.7.320, commit 50728b7. The new opt-in CLI invocation is `iwiki-mcp code publish --project PATH --resume [--json]`. The first invocation prepares a recoverable upload. Repeating it continues the outstanding attempt for this checkout and target. Ordinary publication, SQLite, direct PostgreSQL and local MCP jobs retain their existing contracts. Reject --resume for unsupported modes or peers before building or opening a session. Document that an ordinary upload has no checkpoint.

A successful command reports the revision committed by this attempt, even if a later publication is now active. Preserve stdout JSON shape, stderr progress conventions and exit codes (success 0, operational failure 1, invalid usage/configuration 2). Report recovery-specific failures using bounded diagnostic codes. Distinguish retryable transport loss, expired recovery, incompatible peer, checkpoint corruption and active revision conflict. Never claim success from an active revision comparison alone.

## R1. Pin the input before remote mutation

Use a private directory under the existing local graph cache, scoped by canonical checkout and domain. Bind its record to the configured target and project. Acquire one OS-backed exclusive lock for preparation, reconciliation and local cleanup. A competing CLI returns busy. Changing endpoint or primary requires a separate explicit attempt; never send a cached payload to a substituted target. Use verified session binding for every remote operation. The hosted side derives tenant, primary and owner from authenticated context.

Persist a versioned manifest, random attempt key, absolute recovery deadline, snapshot header, graph revision, and the immutable canonical projected rows in stable per-kind order. Write under a temporary name, flush files, atomically rename and sync the containing directory before begin. Use owner-only permissions and reject symlinks. Store neither credentials nor source files. Graph rows can contain sensitive text. Fail on insufficient space before remote work; an interrupted preparation is disposable. A restart after durable pinning performs zero source parses, even if the checkout or live cache changes.

## R2. Recover begin without creating a second attempt

Discover the resume protocol through the advertised MCP tool schema and the presence of the new inspect tool. Extend begin with optional attempt key and deadline. Legacy calls keep their existing behavior. Propose an additive migration for a durable attempt record keyed by tenant, domain, authenticated owner and attempt key. Store the canonical header digest, session ID, negotiated limits, deadline and terminal outcome. Insert it and the staging session in one transaction. Concurrent identical begins return the original session and limits. Reject a reused key with a different header or deadline. An owner cannot inspect another owner's session. Credential rotation that changes owner requires a new authorized attempt.

Bound recovery to a documented server-capped window (proposed 24 hours, independent of the shorter staging lease). Reject expired attempt requests before allocation. Keep the attempt record through its deadline even if staging is cleaned or its revision superseded. Persist the terminal outcome in the activation transaction. Clean records in bounded batches after the deadline. A retry must carry the original deadline. After expiry return an expired/unknown outcome; never silently allocate another session or assert the old attempt failed. Explicit fresh publication follows the normal command. The migration, window and retention cost require operator review.

## R3. Verify receipts and transfer missing bytes

After begin, persist the actual server limits and batch manifest before the first transfer. Derive batch boundaries with 0.7.320's canonical array framing, per-kind ordinals and row/byte limits. Store offsets into the pinned row streams, counts and payload hashes; reconstruct and verify exact bytes on restart. This avoids a second complete local payload copy. Reject unknown manifest/encoding versions, stale target identity, oversize rows or a hash mismatch. Resume with the session's original limits; if they can no longer be honored stop safely.

Add a read-only, owner-scoped inspect operation taking a session ID and optional continuation cursor. Return state, pinned payload identity, effective limits and lease, plus bounded pages of receipts (kind, ordinal, payload hash) for staging, or the recorded revision for committed attempts. Derive receipts from existing code_graph_batches. Use stable keyset order by kind and ordinal, an exclusive cursor bound to the session, and a server cap (proposed 256 receipts/page). Inspection never renews a lease. Conflicting or unexpected receipts stop transfer. Count previously verified receipts once and send only missing batches. Concurrent changes after a page read remain safe through existing identical-hash idempotency and final transaction guards. A competing machine is not guaranteed zero duplicate transmissions.

## R4. Recover finalization and terminate safely

On lost begin reply, repeat begin with the persisted key. On ambiguous transfer or finalize, retain the checkpoint and inspect before sending again. Never invoke abort to discover status in resume mode. If staging is intact, transfer missing batches and finalize. If terminal success is recorded, replay its revision. If the lease expired, the owner changed, the base revision conflicted or the receipt set disagrees, stop without replacing the current graph. Preserve finalization's existing snapshot and Markdown consistency checks. Keep legacy error handling unchanged.

Persist the terminal revision locally before removing the pinned payload. Keep a small completion record so a restart after finalization can replay the result even after local payload cleanup. Repeated --resume replays that attempt until explicitly cleared. Provide a local-only --forget-resume action, mutually exclusive with publication, under the same lock. It clears only this checkout's selected record and never aborts a server session. An operator may explicitly discard an unresolved attempt after being told its outcome may still be unknown. No background retry, grant changes or parse checkpoints.

## Evidence and acceptance

| Decision | Baseline evidence | Acceptance |
| --- | --- | --- |
| Keyed begin | postgres/codegraph.py begin allocates a random session ID before replying. | Drop begin reply and kill client. Two retries yield one staging session and the original limits. |
| Read-only reconciliation | application.py publish_snapshot calls abort on failure. Postgres abort deletes staging batches. | Drop reply for batch 5 of 8. Restart sends only 6–8, parses zero files and never calls abort. |
| Stable boundaries | codegraph/publication.py iter_snapshot_batches resets ordinals per kind and uses negotiated size limits. | Change local config and live cache; restart emits original hashes. Corrupt pin or incompatible codec stops safely. |
| Terminal authority | finalize checks the base revision and activates transactionally. Staging cleanup can delete session state. | Lose finalize reply, activate a later revision, then resume. Report original success and leave later revision active. |
| Cost and compatibility | Existing receipts already contain hash, kind and ordinal. 0.7.320 lacks inspect. | Inspect returns metadata only within cap. Record disk peak, receipt round trips and transferred bytes. Old clients still publish to upgraded hosts; new resume against old host fails before mutation. |
