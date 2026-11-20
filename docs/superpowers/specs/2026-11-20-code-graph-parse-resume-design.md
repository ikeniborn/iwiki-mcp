---
topic: code-graph-parse-resume
stage: spec
status: approved
chain:
  intent: docs/superpowers/intents/2026-11-20-code-graph-parse-resume-intent.md
review:
  spec_hash: 83d405e7e56b91d7
  last_run: 2026-11-20
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings: []
---
# Recover per-file parsing after a local build stops

## Operator contract

R1. From the 0.7.322 baseline, a normal build with a local checkout automatically resumes compatible completed file analysis. Repeat the existing CLI command or start another local MCP indexing job. Job IDs remain process-local. A ready/no-op build retains its fast path. A stage 2 publisher with a valid upload pin continues to use that pin, even if the checkout has changed. Parse records never authorize upload or identify its remote owner.

R2. A forced build bypasses all saved parse results and writes fresh results under the writer lock. Ignore all hits for the entire forced invocation; replace each owned path only when its new result commits. If interrupted, a subsequent ordinary build that needs reconstruction can resume the forced build's completed work. Repeating force always parses from scratch. Honor discovery exclusions, language selection, current file limits, cancellation and build deadlines on hits and misses. Preserve existing progress fields: files_parsed counts accepted files, including validated hits. Do not add hits to this count twice. Instrument adapter calls separately for performance verification.

## Storage and publication boundary

R3. Use a private scratch directory in the existing local graph cache. Scope it to canonical checkout and repository/domain under the resolved local storage binding. Use one fixed-format, versioned record per path, named by a digest of its normalized relative path. Store one generation of each path; replace invalidated records. This avoids putting recovery rows into the published SQLite database, changing its revision, or adding a hosted schema migration. An append log would require a separate index and compaction to enforce the same retention.

Only the durable build path opens the directory, inside its existing graph writer lock. Contending local builds retain existing bounded lock/job behavior. Pure build_rows remains an uncached, target-independent reference path. Follow existing lock ordering for Wiki and graph operations. Readers, status checks and the hosted server never open parse records. A previously published graph stays intact during parsing; public readers continue to enforce their existing readiness and freshness checks. A checkpoint is never a ready graph.

Write only beneath a verified private directory using owner-only permissions, descriptor-relative operations and no-follow checks as in the existing recovery code. Require regular single-link, owner-owned files. Keep raw source buffers, secrets from process configuration and arbitrary object serialization out of the records. Stored signatures and reference text are private graph data. Use an explicit data-only codec. Write a temporary record in the same directory, flush and fsync its contents, replace the destination, then fsync the directory. Persist creation of new parent entries before counting that file as durable. For eligible records acknowledge the file after this durability boundary. For unsupported adapters or oversized results acknowledge successful validation once, as today; those files must be parsed again after a restart. A process death before replacement may repeat that file.

For an explicit indexing job the existing runtime sets restore_prior_on_abort to false. During the parsing phase retain sufficient evidence of the previous verified local revision in the build transition. After writer death and before any new rebuild, allow the standard reader to recover the previous ready revision only if the canonical store still matches it and existing source, selector and integrity checks pass. When inputs changed, preserve the old snapshot and report dirty/stale according to the current reader contract. Never promote provisional or replaced-but-unverified output. Keep the existing rebuilding response while a writer is alive. Test post-crash public status/search with unchanged inputs and automatic rebuild disabled before running the recovery build.

## Identity and equivalence

R4. Bind each record digest to its format, canonical scope, normalized relative path, discovered byte digest and size, language, parser/grammar/adapter versions, normalizer and Unicode versions, and any parse-affecting options or path context. Verify the digest, types, schema and embedded FileRecord identity before accepting a hit. Use the already bound language adapter's per-file context; for Python include the effective dotted module name, including absence. Adding or removing a package marker can invalidate a byte-identical file. JS/TS path and extension determine module identity and grammar; execute TS boost and reference resolution with the current configuration. Keep versioning explicit for future changes to parser semantics. Adapters with no declared safe codec/context continue to parse normally.

Serialize FileRecord, SymbolRecord sequence, ReferenceRecord sequence, file warnings and adapter-private evidence needed by resolution (including Python class-member mutations). Preserve tuple order, duplicates, nulls and types. Restore only known record variants. Reject an unknown version/variant or mismatched payload and reparse the affected file. Whole-manifest, Git or Wiki changes alone do not invalidate all file results. Precompute Python module names once for the current discovery set; looking up a per-file context must not rescan the manifest.

R5. On every required rebuild, rediscover the current source set and keep its deterministic order. Validate a hit or parse from the same captured bytes as a clean run. Assemble a fresh SymbolIndex from all accepted results. Run current cross-file resolution, Wiki linking, source/selector verification and atomic publication through their existing paths. A removed or renamed-away path can never contribute rows. Compare canonical published payloads and revision with a clean build of the same final checkout and Wiki state. Exclude clocks, job IDs and timings from that comparison.

## Failure and retention

R6. Keep records across interruption before local build completion. Under the writer lock prune owned records for paths outside current discovery and discard abandoned temporary files. After verified local success, clean the recovery set, independent of remote upload. If death intervenes after activation, the next locked invocation can finish cleanup only when it has verified the same ready state or has superseded those records. An older binary ignores the separate directory. Never recursively delete unknown files or follow links. Scope validation protects neighboring checkouts and the stage 2 pin.

Treat an absent or corrupt owned record as a cache miss. A failed durability operation stops the build with a sanitized local storage failure and keeps prior completed records. Reject unsafe paths before reading or modifying their contents. Cleanup failure after local activation cannot revoke the committed snapshot; surface a sanitized maintenance warning and retry cleanup on the next eligible invocation. Bound record reads before allocation; use a 16 MiB ceiling per encoded record and test both sides of the boundary with an uncached parse path for oversized results. The source admission limits and one-record-per-current-path policy bound record count. Include temporary bytes and encoded record sizes in measurements.

## Acceptance and delivery

R7. Kill a real CLI process during parsing after several durable acknowledgments, then edit, remove, rename and add files. Restart in a fresh process and compare with an independent clean build. Prove zero parser calls for compatible completed files, one per required miss, and matching canonical output. Test package marker changes, cross-file imports and Python mutation evidence, all four supported languages, corrupt records, unsafe paths, write failure, lock contention, interruption around replacement, force, no-op, old caches, and an outstanding stage 2 pin. Test stdio polling against the original job and after restart. Verify the previous published snapshot survives interruption.

Measure cold, restarted and clean runs on the same representative checkout, with file counts, real parser calls, parse and total wall time, and peak checkpoint bytes. Include the 20,000-file publication scale case and compare against 0.7.322 under matching settings. Use at least three interleaved runs on the representative fixture, report medians and investigate a cold-build slowdown over 10% before release. Run the established 20,000-file case once on the final code state; the measurements from the earlier audit used different workloads and instrumentation.

When implementation is authorized, run focused tests, related regressions and the full suite against disposable PostgreSQL/Docker. Update EN/RU operator docs, release metadata, Wiki scenarios, and client/runbook guidance where behavior changes. Deploy the tested revision to framework after a disposable rehearsal. Verify the installed version, service and watchdog, authenticated MCP and active snapshot, plus local restart on a publisher with a checkout. Retain the previous image for rollback.

## Acceptance (from intent)

- After a process interruption, a fresh process reuses durably completed, compatible file results. It parses unfinished, new and invalidated files. Deleted files disappear.
- After edits, renames or deletions between runs, the completed graph matches a clean build of the final checkout. Cross-file references reflect the current files and symbols, including references from unchanged files.
- An interruption leaves the previously ready graph readable. Existing progress reporting and recovery of an already pinned upload continue to work.

- Done when: a real restarted process passes the changed-checkout comparison with zero parser calls for compatible completed files, regression tests pass, and the tested release is healthy on framework. Verify parse recovery on a machine with a checkout and hosted compatibility through the deployed MCP.
