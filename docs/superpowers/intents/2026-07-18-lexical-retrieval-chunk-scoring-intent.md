---
review:
  intent_hash: 8b0746c3e634c304
  last_run: 2026-07-18
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings: []
---

# Intent: lexical-retrieval-chunk-scoring

**Date:** 2026-07-18
**Status:** approved

## Objective

Eliminate false lexical-to-chunk attribution in long or repeated H2 sections. Lexical
retrieval currently scores a complete H2 section but assigns every match to `chunk=0`;
a term found only after the first 512-word window can therefore send unrelated opening
text to the reranker. Repeated identical H2 headings also produce colliding
`(file, heading, chunk)` identities. The change is needed now because reranker
evaluation exposed exact chunk identity as the remaining concrete candidate-selection
defect.

## Desired Outcomes

- A query term present only in `chunk > 0` returns that exact indexed chunk.
- `chunk=0` receives no lexical-section signal when its own text does not match.
- Short sections retain their current lexical behavior and deterministic ordering.
- Repeated identical H2 sections remain supported and receive distinct chunk identities,
  so retrieval can return the exact matching occurrence.
- No repeated H2 section is deleted or merged solely because its heading matches another
  section; different bodies and meanings remain independently searchable.
- The reranker receives the exact current matched chunk after index-hash verification.

## Health Metrics

- Lexical mode makes zero embedding requests.
- Each eligible Markdown page is read and chunked at most once per query.
- The candidate ceiling remains `max(top_k, 32)`.
- Public result fields, RRF signal semantics, and deterministic tie-breaking remain
  unchanged.

## Strategic Context

- Interacts with: `engine/chunk.py`, indexed section `Record` identities,
  `engine/grep.py`, `retrieval.py`, RRF fusion, candidate hydration, and optional
  reranking.
- Priority trade-off: exact matched-chunk trust first, search speed second, minimal
  change size third.

## Constraints

### Steering (behavioral guidance)

- Reuse the current windowing rules and assign collision-free indexed chunk identities
  when scoring lexical matches.
- Keep file processing bounded to one read and one chunking pass per eligible page per
  query.
- Preserve existing short-section behavior and deterministic ranking.

### Hard (architectural enforcement)

- Do not change chunk size `512`, overlap `64`, chunk text, or reranker prefixes.
- Do not change the public search API, public result shape, RRF signal model, page-seed
  and graph expansion behavior, or the 32-candidate safety ceiling.
- Do not introduce a new index schema. A documented one-time domain reindex is allowed
  to migrate repeated-H2 chunk identities.
- Keep public `chunk` values zero-based and unique within all occurrences of the same
  `(file, heading)` while preserving existing values for pages without repeated H2.
- Do not deduplicate, delete, or merge sections by heading text or inferred semantic
  similarity; preserve every source occurrence.
- Emit a lexical chunk match only when current chunk content maps to an indexed section
  record with the same hash.
- Do not claim corpus-wide Recall@32; broader recall evaluation remains separate.

## Autonomy Zones

- Full autonomy (reversible, low risk): design the internal chunk-level lexical scorer,
  indexed-record mapping, and focused tests within the approved constraints.
- Guarded (log + confidence threshold): optimize page reading or chunk materialization
  only while preserving hash verification, exact chunk identity, and deterministic
  ordering.
- Proposal-first (needs approval): change the index schema, public API, RRF model,
  candidate ceiling, page-seed behavior, graph expansion, or migration beyond the
  approved one-time domain reindex.
- No autonomy (human only): change `512/64`, chunk/reranker text prefixes, hydration
  behavior, or stale-chunk protection.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules

- Halt if: an exact current chunk cannot be safely mapped to its indexed record and
  verified by hash.
- Escalate if: the fix requires an index schema bump, changes another approved retrieval
  contract, or processes an eligible page more than once per query.
- Done when: a query whose term exists only in a later window returns that exact
  `chunk > 0`, does not assign the lexical-section match to `chunk=0`, sends the exact
  verified text to hydration/reranking; repeated identical H2 sections have distinct
  searchable chunk identities after reindex; and all focused and regression tests pass.
