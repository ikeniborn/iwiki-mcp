---
topic: code-graph-security-performance-audit
stage: spec
chain:
  intent: docs/superpowers/intents/2026-11-19-code-graph-security-performance-audit-intent.md
review:
  spec_hash: 02c772ce9afdd589
  last_run: 2026-11-19
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings: []
---
# Live progress for local code graph jobs

## Purpose

A local job can parse files or publish a snapshot while the active hosted snapshot is stale. Operators need to see what the worker has completed even when the remote reader is slow. The originating process owns the observations. This design covers the lifetime of that process and its existing bounded job history.

## R1. Poll a job

Extend the existing wiki_code_status input with an optional boolean job_only, default false. Keep the current default snapshot response and its top-level state, freshness and error fields. Once local scope is verified, a known job may accompany a stale_snapshot, missing_snapshot or classified connection or timeout failure. Preserve the snapshot diagnostics. Check binding provenance before accepting availability errors. Return authorization, binding, unsafe-path and unclassified errors through the existing error path without attaching job data. Explicitly distinguish transport timeouts and connectivity from HTTP authorization refusals.

Use job_only=true with the job_id from wiki_code_index to poll an exact handle. Require a job_id for this mode. Return invalid_config with field job_id when it is omitted. Check local binding, primary and project ownership before looking up the handle. Match the canonical checkout, wiki base and primary before returning or joining a job. The current registry key contains only base and primary. Preserve the shared-cache writer exclusion when two checkouts use the same destination. A foreign job may keep that destination busy without revealing its identity.

For a known handle return an object with job, plus the existing publication_failed warning when applicable. The job-only response contains no graph state or freshness fields. For an unknown, evicted or foreign handle return the same warnings array containing job_unknown and omit job. On hosted HTTP apply existing binding and access checks first. An authorized hosted job-only request returns job_unknown because that process owns no local builds. Never instantiate a snapshot reader, contact the target or schedule a build to answer a job-only poll.

A combined call follows the existing reader timeout. A job-only call can return while the same target is unavailable. Adding an optional argument keeps the tool name and current callers. A combined-only design would leave the operator waiting on network timeouts. A separate tool would add discovery and registration for the same job descriptor.

## R2. Describe work completed

Return one coherent job descriptor from index and status. Keep the existing id, state, timestamps and running phase fields. Add a progress object to live and retained terminal descriptors. Use the vocabulary below.

| Field | Meaning |
| --- | --- |
| phase | Current operation while running, or the last operation reached at termination. Use queued before the worker enters its first phase. |
| files_parsed | Number of accepted parse results in this job. Start at zero. A cache hit performs zero parses. |
| files_total | Number of files scheduled for parsing. Omit while unknown. Set to zero for a confirmed cache hit. |
| batches_acknowledged | Number of distinct accepted publication batches in this job. Start at zero. |
| bytes_acknowledged | Sum of the canonical payload byte_count of those batches. Exclude transport framing. |
| updated_at | Unix seconds of the last recorded change, using the same representation as job.started_at. Polling leaves this value intact. |

Record discovery and the existing indexing phases from BuildControl. Distinguish the local publication phase from snapshot export, opening the target session, transferring batches and finalizing at the target. Name these additional phases export, publish_begin, transfer and finalize. A successful transfer acknowledgement records the pair of row kind and ordinal within the open publication session. Ordinals restart at zero per kind. Failed, pending and repeated acknowledgements add zero. Keep unpublished SQLite jobs at zero acknowledged bytes and batches. Emit no transfer total or percentage by scanning the graph a second time.

The indexer records completion after accepting a parser result. The publisher records confirmation immediately after a successful adapter response. Keep the latest counts and timestamp in process memory with the job. Publish state under a short lock and capture terminal counters in the existing bounded history. Hold no progress lock during parsing, network calls or output. A reader must see a consistent set of counters, phase and job state across terminal transitions. Keep snapshot activation, retries, worker deadlines and reconciliation of lost finalize responses under their existing owners.

## R3. Report CLI progress

For a started job, emit a flushed progress line to stderr at startup, on phase change and on termination. Within a phase, emit changed counters at most once per second using a monotonic clock. Use the same bounded progress snapshot as MCP. Render an allowlisted set of fields. Keep file names, endpoints, tokens, raw exceptions and source content out of progress. A slow or closed stderr must not hold a worker lock or change the build outcome. Coalesce intermediate updates when output falls behind.

Observe the job while the normal publish operation runs. Deliver output outside the build worker. Subscribe to the exact job started or joined by that call. Do not launch a second indexing operation or poll a remote reader to drive output. If the caller returns before the worker reaches a terminal state, stop its progress subscription without reporting a terminal job event. Preserve the existing command outcome. On an early configuration error return the existing diagnostic and exit code without fabricating a job. In text mode keep the final human-readable result. In JSON mode emit exactly the existing final document to stdout. Apply progress reporting to non-interactive stderr so scheduled publishers receive it.

## R4. Verification and compatibility

| Requirement | Acceptance evidence for implementation |
| --- | --- |
| R1 | Through the registered MCP tool, poll a known running handle with stale and missing snapshots, a connection failure and a held-open reader. The job-only call returns before releasing the held reader. Reader, rebuild and publish call counts stay at zero during these polls. The combined call retains snapshot diagnostics. |
| R1 scope | Repeat with a foreign domain, another checkout sharing the base and primary, an evicted handle, and a rejected binding. Unauthorized responses disclose no phase, counters or job identity. Hosted snapshot reads retain their existing shape. |
| R2 | Pause a deterministic worker between accepted files and batches. Verify unknown and known totals, cache hits, monotonic counts, identical replay, and ordinal zero in two kinds. Poll across success and publication failure. Verify that the last counters survive worker exit and snapshot state stays independent. |
| R3 | Observe stderr through a pipe before releasing the worker, in text and JSON modes. Check the final stdout and exit codes for success, runtime failure and configuration failure. Under a fake clock, send many same-phase updates and check the one-second bound. Exercise a closed progress sink and check redaction. |
| R4 | Retain existing polling, scope, publication, CLI and PostgreSQL integration coverage. Update schema assertions and successful-run stderr expectations for the additive contract. Keep assertions about safe output, exit codes, and final JSON. Run the full suite once on the final implementation fingerprint. |

At implementation time, update the English and Russian code graph, publication and tool references. Add GWT coverage for the new observable behavior in the bound domain and preserve the existing detached-build and terminal-outcome scenarios. Changes to durable checkpoints, hosted publication protocol, storage schema and deployment belong to separate work.

## Acceptance (from intent)

- A caller can poll a known job on its originating local MCP process during collection and publication. A stale, missing or unreachable target still permits inspection of local progress.
- The caller can distinguish the job outcome from the availability and freshness of the active target snapshot. A failed publication reports a failed job even after successful indexing.
- During a build, the operator sees the current phase, the number of files parsed and the time of the last progress update. During transfer, the operator sees acknowledged batches and bytes. A total is shown only when known.
- A CLI operator receives intermediate progress on stderr before the command finishes. A script using the existing JSON output receives one final JSON document on stdout.

- Done when: a local caller observes progress with a stale, missing or unreachable target, and sees the terminal publication outcome.
- Done when: a CLI run shows progress before completion and produces the documented final stdout and exit code on success and failure.
