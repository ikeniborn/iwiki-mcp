---
workflow:
  route: chain
  continuation: full
review:
  intent_hash: 4239237161c0338e
  last_run: 2026-11-25
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings: []
---
# Intent: okf-document-lifecycle

**Date:** 2026-11-25
**Status:** approved

## Objective

Let agents find the current instruction when they search using an obsolete document name. Preserve access to the original evidence. Use the document lifecycle defined by Open Knowledge Format v0.3.

## Desired Outcomes

- A read search that omits the lifecycle option returns the same results as explicit current mode. A query matching a deprecated document can return its eligible successor once, with a locator belonging to that successor.
- A caller can explicitly search all lifecycle states. A direct read at the old address returns the old document, its declared status and its replacement path when present.
- An author can create a document with lifecycle metadata and retire an existing document with a replacement in a single guarded update. A subsequent section edit preserves that metadata.
- Working instructions can be retrieved separately from task and task-history records. The MCP runbook and the maintained iclaude and icodex instructions show how to find current guidance, inspect history and retire a document. Task discovery still finds tasks of every document status.

## Health Metrics

- Zero unauthorized successor reads or disclosures across domain boundaries.
- Zero body changes during metadata-only updates. Zero changes after a rejected PostgreSQL revision check.
- Both storage backends pass the same lifecycle examples. Direct reads and explicit all-mode searches retain access to authorized legacy documents.

## Strategic Context

- Interacts with: iwiki-mcp read, search, write, update and lint tools, Git and PostgreSQL storage, the MCP runbook, and both client instruction sources.
- Priority trade-off: prefer explicit lifecycle evidence over a guess from document age, title or edit revision.

## Constraints

### Steering

- Use existing type and tag information to distinguish operational guidance from task history. Keep task lifecycle distinct from OKF document status.
- Explain the changed search default and give existing callers a reproducible way to recover historical results.

### Hard

- Apply current mode by default to read searches, including existing clients. Preserve the target-selection semantics of write-intent searches.
- Follow OKF v0.3 lifecycle semantics. An absent status defaults to stable. Preserve unknown authored values, including legacy stub and developing, and exclude them from stable current guidance. Keep them discoverable in all mode.
- Respect caller read scope and the bundle path rules. A broken or cyclic replacement must terminate and must not present a retired page as current. Lint must identify the affected authorized source.
- Preserve existing revision guards, code selectors and specification bindings. Keep the rollout independent of a manually authored root page.

## Autonomy Zones

- Full autonomy: choose internal representations, test fixtures and documentation examples.
- Guarded: change indexing and tool metadata handling with Git and real PostgreSQL verification.
- Proposal-first: change the public default, broaden the implementation beyond lifecycle-aware retrieval, or introduce a persistent data migration.
- No autonomy: reclassify live documents in bulk, change domain grants or deploy to production without separate authorization.

## Stop Rules

- Halt dependent work if resolving a successor would disclose an inaccessible target.
- Escalate any need to rewrite existing production metadata or to change a previously accepted public contract.
- Done when: real MCP calls on Git and PostgreSQL demonstrate default current lookup by an old name, explicit historical lookup, exact-address reads, and guarded retirement. Verify missing, cyclic and unauthorized replacements. Verify both clients' documented workflows against the delivered contract.
