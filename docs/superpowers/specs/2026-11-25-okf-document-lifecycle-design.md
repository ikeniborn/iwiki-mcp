---
review:
  spec_hash: ca03b0030d70a4b4
  last_run: 2026-11-25
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-11-25-okf-document-lifecycle-intent.md
---
# OKF document lifecycle for MCP tools

## Scope and authority

The approved [intent](../intents/2026-11-25-okf-document-lifecycle-intent.md) governs both backends and both clients. Implement the lifecycle subset of [OKF v0.3](https://github.com/open-knowledge-format/open-knowledge-format/blob/main/SPEC.md), especially sections 5.4 and 6. A domain is a bundle. Document succession describes separate page identities. The PostgreSQL revision continues to protect updates to one identity.

## Tool contract

- Add an optional read-search argument `lifecycle` with values `current` (default) and `all`. Apply it to lexical, semantic and hybrid search. Reject unsupported values with the existing argument-error convention. Preserve existing write-intent target selection and duplicate discovery across states.
- `current` admits a terminal page with an absent status or explicit stable status. Follow a replacement only on an explicitly deprecated page. Omit draft, deprecated terminals and unknown statuses from current results. `all` searches original pages under their original identities and retains existing section-level results. Never derive a page status from task prose.
- Offer an optional negative tag filter, `exclude_tags`, on read search. Exclude a result when any normalized excluded tag is on its returned page. Preserve positive type, heading and tag filters. A working-guidance query can exclude task and task-history, while task discovery uses all mode with the existing task tag.
- Return lifecycle metadata alongside the existing `wiki_read_page` payload for whole-page and heading reads. Preserve the distinction between absent and declared status. Direct reads return the requested page and the replacement value declared in its readable source. Never enrich that value with the existence, title or metadata of an unauthorized target.
- Preserve the distinction between absent status on existing/imported OKF documents and an author's explicit status. New pages with omitted status and no authored status should be created as stable under the new producer contract. Update legacy client guidance that assumed an automatic stub. Keep pre-existing stub and developing values intact.
- Add `superseded_by` to creation and update alongside status. Explicit arguments take precedence over create-time authored lifecycle fields. Allow a status-only or lifecycle-only update with no section edit. An omitted update field preserves its existing value. An explicit empty replacement clears it. A change away from deprecated also clears the old replacement. Reject an explicitly supplied nonempty replacement with a non-deprecated resulting status. Preserve existing code-only update restrictions.
- PostgreSQL metadata-only retirement requires the current expected_revision. Metadata, revision and search visibility follow normal transactional page-write behavior. Git mutations use the existing guarded write and indexing path. Preserve body bytes, code selectors, tags and specification bindings on retirement. Ordinary edits preserve declared lifecycle metadata.

## Search and ownership

One shared resolver owns status interpretation, path resolution, successor traversal and terminal facet checks. Git and PostgreSQL provide authorized page metadata and indexed sections through their existing read paths. Reuse materialized scope data or a per-call cache. No database schema migration or external fetch is required.

Read a relative replacement from the source directory and a leading slash from the bundle root. Normalize and validate bundle paths before lookup. Resolve only to concepts inside the source bundle and requested read scope. Reject escaping paths, external URIs and unsafe encodings during traversal. Detect cycles with visited concept identities. On a missing, inaccessible, malformed or cyclic chain, drop that alias from current results. Keep unrelated results available. Lint reports findings against the readable source and does not probe or identify an external target.

Treat an old title, path or matching old section as potential evidence for its resolved successor. Resolve and group alias matches by terminal page before candidate caps, including seed and signal caps where duplicate aliases could displace other pages. Apply type, tags and exclusions on the terminal page. Enforce any requested heading on the terminal. Preserve a directly ranked terminal section when available. Otherwise choose a matching terminal heading, then its first indexed section by ordinal. If the terminal has no eligible indexed section, omit that alias hit. Return the terminal's own file, heading and chunk. Use the best alias rank for the group, so adding more obsolete aliases cannot improve its rank by repetition. Hydrate and rerank terminal content. Preserve native section granularity for direct matches. Emit at most one alias-derived hit per successor and suppress it when a direct hit for that successor is present.

Post-processing the final top-k alone would let repeated aliases evict other pages. Materializing permanent reverse links would introduce migration and invalidation work. Both backends already have candidate preparation before final fusion. Place the shared policy in those paths and test matching by an obsolete name even when it is absent from the successor text.

## Documentation and deployment boundary

Update the server tool reference and MCP runbook, their maintained translations, and the maintained instruction sources in iclaude and icodex. Verify the generated instructions where the clients use templates. Show the sequence to read a revision, retire the source, read the original, search current guidance, and search historical tasks. Document current visibility of legacy statuses and scope-relative paths. Document that clients using these arguments require a server advertising the new signature. No manual landing page, production migration or live reclassification is required.

## Verification

Use the public MCP entry points on Git and real PostgreSQL. Verify omitted versus explicit current, all, direct reads, metadata-only updates, combined edits, conflicting revisions, exact body bytes and preserved selectors. Seed independent eligible pages, multiple aliases to one successor, a chain, a missing successor, a cycle, a draft, legacy stub/developing and a page without status. Search using words present only in the obsolete title and section. Check the returned locator by reading that exact heading. Exercise facet differences between source and successor, limited k, lexical/semantic/hybrid paths, filtered task discovery, and attempts to leave the authorized bundle. Verify lint and unchanged write-intent targeting. Run the documented examples against the resulting tool schemas for both clients.

## Acceptance (from intent)

- A read search that omits the lifecycle option returns the same results as explicit current mode. A query matching a deprecated document can return its eligible successor once, with a locator belonging to that successor.
- A caller can explicitly search all lifecycle states. A direct read at the old address returns the old document, its declared status and its replacement path when present.
- An author can create a document with lifecycle metadata and retire an existing document with a replacement in a single guarded update. A subsequent section edit preserves that metadata.
- Working instructions can be retrieved separately from task and task-history records. The MCP runbook and the maintained iclaude and icodex instructions show how to find current guidance, inspect history and retire a document. Task discovery still finds tasks of every document status.

- Done when: real MCP calls on Git and PostgreSQL demonstrate default current lookup by an old name, explicit historical lookup, exact-address reads, and guarded retirement. Verify missing, cyclic and unauthorized replacements. Verify both clients' documented workflows against the delivered contract.
