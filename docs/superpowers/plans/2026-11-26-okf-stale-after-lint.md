---
review:
  plan_hash: f2c22cf5a6eb087c
  last_run: 2026-11-26
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings: []
result_check:
  verdict: OK
  source: plan
  plan_hash: f2c22cf5a6eb087c
  last_run: 2026-11-26
  reviewed: true
  docs_checked: true
chain:
  spec: docs/superpowers/specs/2026-11-26-okf-stale-after-lint-design.md
  intent: docs/superpowers/intents/2026-11-26-okf-stale-after-lint-intent.md
---
# OKF authorship and verification implementation plan

Implement the [checked design](../specs/2026-11-26-okf-stale-after-lint-design.md) in the topic worktree. Use the approved author format in the [intent](../intents/2026-11-26-okf-stale-after-lint-intent.md). The parent owns public integration and verification. Check these steps in order.

1. Add failing frontmatter and trust fixtures. Extend the shared engine to parse and round-trip imported actor mappings, event lists, extension fields and dates. Confirm the fixtures on legacy and new Markdown.
2. Add failing public authoring cases. Resolve explicit human and agent actors or concrete client identity. Update generation on meaningful create, update, insert, delete and move operations. Exercise presentation-only, no-op, invalid and missing author cases on Git and PostgreSQL.
3. Add guarded explicit verification. Check a whole-page Git hash within the existing mutation lock and the current PostgreSQL revision in the storage transaction. Use the ordinary indexing and rollback path. Verify conflicting writes, unchanged body bytes, trust tiers and retained history.
4. Add explicit deadline writes and one shared UTC reference time to domain lint. Run both backends against missing, malformed, past, future and boundary dates. Check repeated lint is read-only.
5. Exercise the MCP tool schema, hosted authorization and legacy inputs. Update the runbook and client instructions in isolated worktrees. Use actual tool signatures in every example.
6. Run focused tests at each integration point. Run syntax checks and the full suite on the final fingerprint. Exercise PostgreSQL with a disposable pgvector database. Reconcile the result, wiki references and remaining delivery evidence.
