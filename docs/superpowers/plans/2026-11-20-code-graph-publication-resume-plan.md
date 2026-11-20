---
topic: code-graph-publication-resume
stage: plan
status: approved
chain:
  intent: docs/superpowers/intents/2026-11-20-code-graph-publication-resume-intent.md
  spec: docs/superpowers/specs/2026-11-20-code-graph-publication-resume-design.md
review:
  plan_hash: adb7e8ac4fd8574e
  last_run: 2026-11-20
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings: []
---
# Code graph publication resume implementation plan

**Goal.** Recover the original upload and its outcome after a CLI restart. **Architecture.** A private immutable pin supplies bytes. An owner-scoped hosted attempt supplies transaction and receipt evidence. **Stack.** Python CLI, MCP HTTP, PostgreSQL, existing graph exporter and pytest.

## 1. Establish the durable server contract (R2, R4)

Review the additive migration and retention policy before implementation. Start with disposable PostgreSQL tests for lost begin, simultaneous begins, conflicting header, expired key, wrong owner and superseded terminal revision. Verify failure against baseline. Extend postgres/migrations.py and postgres/codegraph.py. Couple begin and activation writes to their attempt records. Preserve legacy rows and requests. Run migration from the deployed schema with staging and terminal fixtures. Check old binary compatibility against the expanded schema, bounded cleanup, and concurrency under separate database connections. Release gate: one session per key and no activation without a durable result.

## 2. Expose and verify bounded receipts (R2, R3)

Extend server.py, codegraph/mcp_adapter.py and publication types. Write HTTP tool-schema and authorization regressions first. Cover zero receipts, exactly one page, multiple kinds at ordinal zero, a missing ordinal, cursor misuse, expired lease, rejected binding and later activation during pagination. Verify inspect causes no writes or lease extension. Verify old begin still accepts only a header and new resume detects a 0.7.320 peer before mutation. Test over actual MCP HTTP with a disposable database.

## 3. Pin and recover local state (R1, R3)

Add a focused checkpoint module beside codegraph/application.py. Reuse the exporter and canonical serializer from codegraph/publication.py. Start with golden byte comparisons against iter_snapshot_batches at row and byte boundaries. Implement private row streams and atomic manifest updates. Test process termination immediately before and after each durable rename, lock release on process exit, concurrent local invocation, disk full, mismatched target, edited checkout, incompatible format and corrupt bytes. Verify no transfer before the manifest is durable and no source parse on resume after pinning.

## 4. Integrate CLI recovery (R1–R4)

Extend admin.py argument handling and application.py orchestration. Keep ordinary publish_snapshot behavior available to legacy callers. Test --resume and --forget-resume in the installed CLI, including legacy peers, negative responses, full stderr, JSON, exit status and replay of terminal success after cleanup. Simulate accepted batches with lost replies and assert exact outgoing ordinals and payload hashes. Record begin, inspect, batch, abort and finalize call counts. Verify that timeout preserves the checkpoint and no ambiguous response causes abort. Test stale-base conflict, lease expiry during transfer and loss of finalize reply.

## 5. Acceptance, migration review and delivery

Use two CLI processes against a disposable hosted MCP and PostgreSQL. Kill the first after begin commit, batch 5 commit, finalize commit and local completion-record commit, each before the next observable acknowledgment. For each boundary run the second process and verify the relevant acceptance row in the specification. Exercise later activation by an independent publisher. Compare old-client/new-host and new-client/old-host combinations. Inspect disk usage and peak retained bytes, confirm bounded receipt pages and zero retransmission of verified batches with one publisher. Check that logs, progress and checkpoint metadata expose no credentials.

Run focused application, CLI, server tool and PostgreSQL publication suites during iteration. Run the complete pytest suite with disposable pgvector and Docker on the stable implementation. Require independent migration/integration review. Update English and Russian publication docs, GWT scenarios, package version and Wiki evidence. Document server-first rollout, compatibility window and binary rollback; obtain separate approval for production deployment. A failed new-client recovery must leave legacy reads and the active snapshot usable.
