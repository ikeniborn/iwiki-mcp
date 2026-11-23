---
topic: code-graph-publication-quotas
stage: plan
status: approved
chain:
  intent: docs/superpowers/intents/2026-11-23-code-graph-publication-quotas-intent.md
  spec: docs/superpowers/specs/2026-11-23-code-graph-publication-quotas-design.md
review:
  plan_hash: e15619e154dd6c72
  last_run: 2026-11-23
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings: []
---
# Hosted publication quotas implementation plan

**Goal.** Bound concurrent hosted uploads across the server and per publisher while preserving receipts, leases and active graph visibility. Read the [specification](../specs/2026-11-23-code-graph-publication-quotas-design.md) and [intent](../intents/2026-11-23-code-graph-publication-quotas-intent.md) before editing.

**Architecture.** Keep operator configuration in the hosted configuration layer. Keep durable usage and mutation authorization in PostgreSQL. Reuse the existing publication transactions. The MCP and CLI layers translate a quota refusal into a safe resource name. Python, psycopg, PostgreSQL, pytest and the existing publisher fixtures cover this path.

**Execution.** Follow steps 1–6 in order. For each behavior, demonstrate a failing focused test, implement the smallest change, rerun the focused and affected regressions and commit. Use subagent-driven development for in-session execution if selected, or executing-plans for a separate session. Reserve an independent final integration review for this migration.

## 1. Establish the accounting boundary and operator settings

- [ ] Separate ticket issuance in the authentication path from the publication login. Provision the issuer credential and validate privilege separation before serving traffic. Implement the ticket contract from the specification. Test expiry, rollback, reuse, altered arguments, revoked permissions and credential redaction on pooled and independent connections.
- [ ] Inventory table grants, role memberships, routines, cascades, direct publishers and cleanup. Under restricted logins verify that the serving role cannot issue tickets or invoke the direct route. Test direct quota attribution by session_user across processes and domains. Test separate tenants and preserve session authorization for clients sharing a login.
- [ ] Extend HostedCodeGraphConfig and its allowlist in postgres/config.py. Accept positive integer defaults of 64 server sessions, 8 owner sessions, 2147483648 server bytes and 268435456 owner bytes. Reject booleans, nonpositive values and values beyond the chosen database representation. Test explicit overrides and unchanged client binding policy. Operators alone control effective limits. Document coordinated config changes across all serving replicas.
- [ ] Pass when the configured limits reach the hosted store, SQLite-only paths retain their contract, and the owner-key and database-wide pool boundary have executable coverage. If the existing identity cannot support the specified isolation, stop and revisit the design.

```bash
uv run pytest -q tests/postgres/test_config.py tests/codegraph/test_config.py
```

## 2. Migrate and reconcile durable usage

- [ ] Extend postgres/migrations.py and the existing schema-version/readiness guards. Add the private session accounting table and indexes for both live-slot counts and charged-byte sums. Use a wide integer type for byte totals. Track effective expiry, terminal state, trusted owner and session identity. Cover collisions across domains and tenants.
- [ ] Backfill on a disposable database populated using the 0.7.325 schema. Include empty sessions, live sessions with receipts, expired and terminal sessions with retained bytes, old sessions without attempt keys and an owner already over limit. Include legacy usage for the matching hosted token. Keep unattributable direct usage under the original owner. Verify independent reconciliation and namespace separation. Derive usage from persisted sessions and receipt byte counts. Explicitly handle terminal attempts whose sessions were already removed.
- [ ] Restrict global accounting to narrow database routines. Verify routine ownership, fixed search path and grants. Bind each operation to the authorized session and verified mutation. Under the serving role, test that private totals cannot be read, another owner cannot be charged and capacity cannot be released independently of deletion. Keep global admission fail-closed if an expected row is inconsistent.
- [ ] Pass when an independent SQL reconciliation matches slots and retained bytes per owner and globally, existing data remains readable, and mixed old/new writers cannot alter quota-managed state during migration or rollout. Include schema guards and operational quiescence for every writer identified in step 1.

```bash
uv run pytest -q tests/postgres/test_migrations.py tests/postgres/test_code_graph_migrations.py tests/postgres/test_code_graph_rollback.py
```

## 3. Enforce positive admissions and preserve replay

- [ ] Add tests in a dedicated tests/postgres/test_publication_quotas.py. Use independent database connections and synchronize contenders for the last available slot and byte budget. Cross server/owner scopes with exact-limit and one-over-limit values. Include same-owner cross-domain requests and competing owners in distinct domains.
- [ ] Implement begin and publish_batch inside their existing PostgreSQL transactions. Resolve authorization, lease checks and receipt conflicts before quota refusals. Repeated keyed begin returns its original outcome. A matching receipt costs zero even when either limit has been lowered. A refused new batch leaves the lease and receipts unchanged.
- [ ] Serialize positive admissions and lease extensions with the database-wide transaction lock. Race receipt replay with last-slot admission at expiry. Check the original lease after locking for legacy and recoverable sessions. After acquiring it, read a fresh committed usage view, evaluate both applicable limits, then charge exactly once with the accepted mutation. Bound lock wait using the existing busy path. Document and test lock ordering with domain/session locks, unique attempt keys and cleanup. Roll back the entire unit on refusal after any tentative insert, timeout or failed commit. A returned error dictionary alone does not roll back a transaction.
- [ ] Pass when concurrent requests cannot overshoot, a denied begin leaves no orphan key/session/snapshot, and ordinary contention remains distinguishable from exhaustion. Exercise process loss before commit and after commit before acknowledgement.

```bash
uv run pytest -q tests/postgres/test_publication_quotas.py tests/postgres/test_code_graph_publication.py tests/postgres/test_code_resume_process.py
```

## 4. Return capacity at the correct lifecycle boundary

- [ ] Extend the quota integration tests for successful finalize, failed materialization, cancellation, lost finalize response, expiry, retention and cascades. Use independent SQL totals after each transition. Race terminal operations with batch acceptance and sweep; exercise repeated execution after restart.
- [ ] In postgres/codegraph.py finalize only after existing integrity and revision checks, and delete staged batches plus release their charge atomically with activation. Repeated inspect/finalize must still return the terminal revision for the original attempt even after another snapshot activates. Test authorized terminal replay after session deletion, before and after the recovery deadline. Verify both legacy and resumable sessions. Never hold the global admission lock during materialization or an entire sweep.
- [ ] Coordinate abort, bounded cleanup, historical terminal-data reclamation and all session deletion paths with accounting. Expiry frees a live slot immediately; bytes stay charged until their deletion commits. Honor the existing retention window. Test lowering a single limit below usage: permit zero-cost replay and release, reject only increases of the exhausted resource.
- [ ] Pass when failed finalization leaves the prior revision, receipts and usage intact; committed release and retries free capacity exactly once. Report peak finalize memory separately from staged payload size.

```bash
uv run pytest -q tests/postgres/test_publication_quotas.py tests/postgres/test_code_resume_process.py tests/postgres/test_code_graph_publication.py
```

## 5. Prove client behavior and document recovery

- [ ] Test hosted MCP, the publisher adapter and the normal and resumable CLI through their public entry points. Preserve existing failure categories and add the safe code publication_quota_exceeded and resource sessions or staged_bytes as specified. Confirm that older clients still detect failure. Exercise lost responses and a retry after capacity is released within the lease.
- [ ] Test privacy with unrelated principals and foreign session IDs. No owner identities, limits of another owner, domain names, usage totals or session IDs from competing publishers may appear in refusal output. Preserve existing permission and conflict precedence.
- [ ] Update docs/code-graph-publishing.md, its Russian counterpart and the hosted configuration example/runbook with operator settings, logical byte units, immediate refusal, cleanup timing and retry instructions. Update the bound Wiki specification with implements/verifies bindings and actual command evidence during execution.
- [ ] Pass when all public routes report the correct safe resource, busy remains separate, and clients can complete the originally refused upload without changing its active predecessor on rejection.

```bash
uv run pytest -q tests/test_code_publish_cli.py tests/codegraph/test_resume_application.py tests/postgres/test_code_resume_process.py tests/postgres/test_publication_quotas.py
```

## 6. Benchmark, rehearse rollback and reconcile the result

- [ ] Use identical payloads, batch sizes, database state and hardware for 0.7.325 and the candidate. Interleave repeated runs below quota for one publisher and for concurrent publishers in distinct domains. Capture acknowledged batch samples, median end-to-end time, p95 acknowledgement latency, contention and peak finalize memory. Record sample counts, revisions and ratios separately for all four latency comparisons. Investigate any increase above 15% before release.
- [ ] Rehearse migration and rollback on a populated disposable database. Include issuer provisioning and removal, ticket invalidation, privilege restoration and legacy direct sessions. Quiesce all writers and cleanup, retain a restorable backup and old binary, verify backfill independently, and enable a consistent policy across replicas. Create new accepted work and terminal receipts on the candidate. Demonstrate rollback preserving those records and the readable active revision. Restoring an older dump alone cannot prove preservation of post-upgrade writes. Reopen the design if rollback cannot preserve them.
- [ ] Run lint, compile checks and the final full suite once with disposable pgvector endpoints and Docker. Require PostgreSQL acceptance tests to run. Inspect skips individually, preserve exact commands and code fingerprints, and obtain independent migration/integration review. Reconcile the implementation against this plan with check-chain result before branch finishing.
- [ ] Pass when functional invariants, compatibility and rollback are verified, performance findings are dispositioned, and review findings are resolved. Record the evidence in the task ledger. A live migration or operator policy change requires its own authorized window.

```bash
uv run flake8 src tests
uv run python -m compileall -q src/iwiki_mcp
uv run pytest -q -rs
git diff --check
```
