---
review:
  intent_hash: 05f54f3547167887
  last_run: 2026-08-04
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings: []
workflow:
  route: chain
  continuation: full
---

# Intent: cross-domain-link-rewrite

**Date:** 2026-08-04
**Status:** approved

## Objective

Keep authored cross-domain `iwiki://` links valid when their target page is
moved or a referenced `##` heading is renamed. A move or anchor rename must
find and rewrite incoming references in writable domains, then leave Markdown,
vectors, ingest logs, and the local graph in one consistent observable state.

## Desired Outcomes

- Moving a page automatically preserves valid incoming `iwiki://` links from
  other writable domains in the same wiki base.
- Renaming a referenced `##` heading automatically updates incoming URI
  anchors in writable domains.
- After a successful operation, `wiki_lint` reports no broken affected
  cross-domain links and graph parity reports no affected mismatch.
- If the server cannot safely prepare, reindex, or commit every affected
  writable domain, it leaves no partially rewritten Markdown links.

## Health Metrics

- Existing intra-domain move and rewrite regression tests remain green.
- Ordinary writes and searches do not scan every domain in the base.
- Portable `index.jsonl` and `log.jsonl` remain consistent with their domain
  Markdown after a successful rewrite.
- Graph-only failure keeps Markdown authoritative and preserves the safe
  retrieval fallback.
- No modified page lies outside the resolved write scope.

## Strategic Context

- Interacts with: MCP clients and authoring agents, `wiki_apply_okf`,
  `wiki_update_page`, structured link parsing/rewrite, SQLite graph incoming
  edges, vector JSONL stores, ingest logs, Git sync, and `wiki_lint`.
- Priority trade-off: trust and recoverable consistency first; rewrite latency
  and implementation cost second.

## Constraints

### Steering

- Rewrite only exact structured `iwiki://` targets; leave link text, code
  spans/fences, images, and external URIs unchanged.
- Use SQLite incoming edges only to narrow candidate Markdown pages; verify
  each rewrite against canonical Markdown.
- Record the pages and domains rewritten by a successful operation.

### Hard

- Markdown remains authoritative over SQLite, vectors, and ingest logs.
- Never rewrite a domain outside the resolved write scope.
- A rewrite operation is atomic across affected Markdown, `index.jsonl`,
  `log.jsonl`, graph state, and Git commit preparation; failure restores the
  pre-operation state.
- A successful operation leaves no broken affected cross-domain page target or
  anchor and no graph parity mismatch according to `wiki_lint`.
- Keep existing `iwiki://` syntax, read-scope visibility rules, and
  intra-domain rewrite behavior compatible.

## Autonomy Zones

- Full autonomy: internal helper boundaries, reverse-edge lookup, deterministic
  fixtures, focused tests, and documentation wording.
- Guarded: select rewrite candidates from current write scope only and report
  the final rewrite set in the operation result.
- Proposal-first: change `iwiki://` syntax, SQLite schema, Git model, or MCP
  public response schema.
- No autonomy: rewrite pages outside write scope, alter credentials/remotes,
  or delete user-authored Markdown as recovery.

## Stop Rules

- Halt if: any affected writable domain cannot be safely prepared, indexed, or
  committed before canonical Markdown would be changed.
- Escalate if: implementation requires a different URI syntax, schema
  migration, or expanded write permissions.
- Done when: real cross-domain page-move and anchor-rename scenarios preserve
  valid links; `wiki_lint` and graph parity report no affected failures; and
  required focused and full regression checks pass.
