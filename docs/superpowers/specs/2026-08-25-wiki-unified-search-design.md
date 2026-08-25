---
review:
  spec_hash: d282d70b4b47dee9
  last_run: 2026-08-26
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-08-25-wiki-unified-search-intent.md
---
# Design: wiki-unified-search

**Date:** 2026-08-25
**Status:** approved

## Summary

This design evaluates whether one public `wiki_unified_search` MCP call should replace
the client-side coordination normally required across `wiki_search`,
`wiki_code_search`, and `wiki_code_context`. The candidate is a shared orchestration
service over the existing Wiki and code primitives, not a new retrieval or ranking
engine. It keeps Wiki results, code results, confirmed Wiki associations, and bounded
code context in separate response blocks and never compares their scores.

The production tool is registered only after an unregistered prototype proves both a
coordination gain and an end-to-end workflow-quality gain. Raw Wiki and code retrieval
must remain a strict non-regression against an ideally executed specialized baseline.
If workflow-quality evidence does not justify the additional public contract, the
outcome is `do not implement` and no new MCP tool is registered.

## User Tasks

- **T-001 — Necessity decision:** determine with evidence whether the unified tool adds
  material value or merely duplicates the existing specialized workflow.
- **T-002 — Coordination and quality:** require both fewer client-visible MCP calls and
  better end-to-end agent workflow correctness; call reduction alone is insufficient.
- **T-003 — Search controls:** expose the full union of read-search filters from
  `wiki_search` and `wiki_code_search`, while excluding Wiki write intent.
- **T-004 — Automatic context:** expand depth-one context for at most the first three
  fresh code results without exposing manual traversal controls.
- **T-005 — Transparent boundaries:** keep Wiki/code rankings separate and expose stale,
  missing, failed, truncated, and revision-changed states explicitly.
- **T-006 — Shared implementation:** reuse existing Wiki/code primitives and avoid an
  independent federated retrieval engine.
- **T-007 — Safe registration:** publish the public FastMCP/HTTP contract only after the
  comparative gates pass and a human approves registration.

## Goals

- Evaluate an unregistered candidate against the current three-tool workflow.
- Preserve raw Wiki and code results for equivalent inputs.
- Make seed selection, context execution, association extraction, and degraded-state
  handling deterministic inside one server request.
- Preserve local SQLite, PostgreSQL, and hosted MCP behavior where their existing
  source-availability contracts overlap.
- Produce an evidence-backed `implement` or `do not implement` decision.

## Non-Goals

- No Markdown or frontmatter mutation.
- No graph indexing, publication, watcher, daemon, or backend fallback.
- No new Wiki/code ranking, score fusion, cross-block ordering, or candidate store.
- No schema migration, table, index, vector format, or graph format change.
- No removal or public-contract change to `wiki_search`, `wiki_code_search`, or
  `wiki_code_context`.
- No manual `seeds`, `direction`, `relations`, source inclusion, or context-budget
  controls on the unified tool.
- No concurrent branch execution in the initial design; sequential calls remain
  independent and do not short-circuit each other.

## Architecture

### Shared orchestration service

Conditional on the evaluation passing, `src/iwiki_mcp/unified_search.py` owns only:

- selection of at most the first three code-result `entity_id` values;
- one depth-one context request with existing default context budgets;
- revision-coherence enforcement between code search and code context;
- extraction of `wiki_pages` into a distinct associations block;
- normalized per-branch degradation metadata; and
- assembly of the five-block unified response.

The module has no FastMCP, authentication, binding, configuration, storage, embedding,
reranking, filesystem, database, or publication responsibility. It receives already
validated callable primitives and JSON-serializable results, which also makes the
orchestration behavior independently testable.

### Server integration

`src/iwiki_mcp/server.py` remains the binding and backend composition boundary. The
existing read paths are factored into small private primitives that accept the resolved
binding and their current configuration. The specialized handlers call those same
primitives, preserving their schemas and responses. `wiki_unified_search` resolves the
binding once, invokes the shared primitives, and delegates response assembly to the
orchestration service.

Wiki and code configurations remain separate because they have different existing
contracts. “Resolve once” applies to project/session binding, not to merging their
configuration models.

### Hosted authorization

`src/iwiki_mcp/http.py` treats `wiki_unified_search` as a combined read operation. Before
FastMCP dispatch it:

- rejects caller-supplied `iwiki_id` and singular `domain` arguments;
- requires read authority for the bound primary code-graph domain; and
- authorizes every requested Wiki domain through the existing domain-grant logic.

No partial result is returned for authentication or authorization failure because the
request is rejected before tool execution.

### Evaluation-first boundary

The first implementation artifact is an unregistered orchestration prototype under the
evaluation surface, not a production FastMCP tool. Production module creation,
registration, HTTP authorization changes, and public documentation occur only after the
comparative gates pass and the registration checkpoint is approved. A `do not
implement` outcome may retain evaluation fixtures and evidence, but leaves no unused
production module and no public tool entry.

For agent workflow evaluation, the evaluation runner supplies a private candidate tool
schema and callback directly to its agent harness. This adapter is visible only inside
the evaluation process and does not mutate the module-level FastMCP registry, hosted
tool list, package entry point, or public documentation. The baseline runner similarly
adapts the existing specialized calls so both arms use the same harness and evidence
format.

## Public Request Contract

The conditional public handler has this logical input surface:

| Field | Source contract | Behavior |
|---|---|---|
| `query` | shared | Required string; must satisfy existing code-search structural bounds. |
| `scope` | `wiki_search` | Defaults to `project`; preserves current local scope resolution. |
| `mode` | `wiki_search` | Optional `hybrid`, `lexical`, or `semantic`; existing default applies when omitted. |
| `domains` | `wiki_search` | Optional Wiki-domain list intersected with bound and authorized read scope. |
| `k` | `wiki_search` | Optional Wiki result count with existing default and validation. |
| `threshold` | `wiki_search` | Optional Wiki score threshold with existing semantics. |
| `type` | `wiki_search` | Optional normalized Wiki page type facet. |
| `tags` | `wiki_search` | Optional normalized Wiki tag facets. |
| `kinds` | `wiki_code_search` | Optional code entity-kind filters with existing validation. |
| `path` | `wiki_code_search` | Optional code path filter with existing validation. |
| `languages` | `wiki_code_search` | Optional language filters validated against the active project or snapshot contract. |
| `limit` | `wiki_code_search` | Code result limit; preserves the existing default and bounds. |

The tool does not accept `intent`, `heading`, manual context seeds, traversal direction,
relation filters, source inclusion, or context budgets. Documentation-only, write-target,
and advanced structural work continues to use specialized tools.

Structural validation runs before binding or retrieval. Snapshot-dependent language
validation remains inside the code primitive because only the active snapshot declares
the authoritative hosted language set. Such a code-branch validation failure is
sanitized and does not erase a valid Wiki result.

## Public Response Contract

Every structurally valid request returns these top-level blocks:

```text
wiki
code
associations
context
degradation
```

- `wiki` preserves the existing Wiki read response, including `results` and optional
  rerank metadata or its sanitized branch error.
- `code` preserves the existing code-search status, freshness, revision, warnings,
  results, and sanitized branch error.
- `associations` contains only confirmed `wiki_pages` emitted by code context. It never
  infers a link from score proximity or naming similarity.
- `context` preserves seeds, nodes, relations, files, limits, truncation, warnings, and
  freshness from bounded code context, but excludes `wiki_pages` because those are in
  `associations`.
- `degradation` reports `degraded` and a sanitized `reason` independently for `wiki`,
  `code`, `context`, and `associations`.

Existing error codes, errors, hints, and warnings are preserved inside their owning
branch. The only new orchestration reasons are `not_run` for a context branch skipped
because its code prerequisite failed and `revision_changed` when search and context do
not describe the same graph revision. A fresh code search with zero results returns an
empty context and associations without marking either block degraded.

Markdown relevance scores and code match ranks stay inside their source blocks. The
unified response has no combined score, global order, preferred block, or implicit
cross-block threshold.

## Data Flow

1. Hosted authorization checks primary code-read authority and requested Wiki domains.
2. The handler performs structural request validation. A structurally invalid request
   returns a top-level sanitized error and performs no retrieval.
3. The handler resolves the project/session binding once and loads the existing Wiki and
   code configurations through their separate contracts.
4. Wiki search and code search execute sequentially but independently. A branch result,
   including a sanitized error, never short-circuits the other branch.
5. No separate `wiki_code_status` call occurs. Code search already returns its state,
   freshness, revision, warnings, and results under its guarded snapshot read.
6. When code search returns `fresh=true` with results, the orchestrator selects at most
   the first three unique `entity_id` values in existing code rank order.
7. The code-context primitive runs once with `direction=both`, `depth=1`,
   `include_source=false`, `include_wiki=true`, and the existing default node, file, and
   byte budgets.
8. Context is accepted only when `fresh=true` and its `revision` equals the code-search
   revision. On mismatch, Wiki/code results remain, while nodes, relations, files, and
   associations are cleared and `revision_changed` is reported.
9. Accepted `wiki_pages` move into `associations`; all other accepted context fields stay
   in `context`.
10. The orchestrator derives independent degradation entries without comparing or
    merging scores.

The revision check closes the activation race present on both SQLite and PostgreSQL,
where search and context use separate guarded reads or transactions. It deliberately
chooses fail-soft consistency over a new cross-operation transaction or snapshot lease.

## Error and Degradation Semantics

| Condition | Wiki | Code | Context | Associations |
|---|---|---|---|---|
| Authentication, authorization, or structural validation failure | Request rejected | Request rejected | Request rejected | Request rejected |
| Wiki branch failure | Sanitized error, empty results | Continues | Follows code state | Follows context state |
| Code missing, dirty, busy, stale, or failed | Preserved | Sanitized state/error, empty results | Empty, `not_run` | Empty, `not_run` |
| Fresh code with no results | Preserved | Empty fresh results | Empty, not degraded | Empty, not degraded |
| Context failure | Preserved | Preserved | Empty with existing reason | Empty with same prerequisite reason |
| Search/context revision mismatch | Preserved | Preserved | Empty, `revision_changed` | Empty, `revision_changed` |
| `wiki_links_stale` | Preserved | Preserved | Nodes/relations/files preserved | Empty, `wiki_links_stale` |
| Context truncation | Preserved | Preserved | Partial result with limits/warnings | Confirmed returned associations only |
| Wiki rerank failure | Existing preliminary Wiki order and rerank metadata | Continues | Continues | Continues |

All branches preserve current sanitization and redaction. The unified layer never emits
exception details, credentials, DSNs, URLs, private base paths, source text, or alternate
backend diagnostics. It never retries through a different configured backend.

## Comparative Evaluation

### Ideal specialized baseline

For raw parity, the baseline executes the specialized tools correctly with equivalent
filters, selects the same first three unique code IDs, and calls depth-one context once.
For each fixture:

- candidate `wiki` must equal the baseline Wiki response;
- candidate `code` must equal the baseline code-search response;
- candidate `associations` must equal baseline context `wiki_pages`;
- candidate `context` must equal baseline context after removing `wiki_pages`; and
- candidate degradation must describe, not hide or replace, the baseline state.

No raw retrieval improvement is expected from shared primitives. Equality is the raw
quality gate.

### End-to-end workflow baseline

Workflow quality compares representative agent tasks against the current multi-call
surface and the candidate surface. Both arms use the same model, task prompt, tool
descriptions, domain scope, repository snapshot, and expected-fact rubric. Evidence
records the model identifier, prompts, run count, tool traces, outputs, and scoring.

Metrics are:

- client-visible MCP call count;
- task completion correctness against expected facts;
- incorrect or missing seed selection;
- omitted required context calls;
- incorrect claims under missing, stale, or revision-changed graph state; and
- loss of required Wiki or code facts.

The candidate passes the workflow gate only when it completes more scenarios correctly,
uses fewer client-visible calls for meaning-plus-code tasks, and regresses no individual
scenario. A smaller call count without higher workflow correctness fails the gate.

### Scenario set

The fixed scenario set includes:

- linked meaning-plus-code discovery;
- relevant code without a confirmed Wiki association;
- Wiki-only and code-empty queries;
- missing, dirty, busy, and stale code graph;
- `wiki_links_stale` with otherwise usable code context;
- context truncation;
- search/context revision mismatch;
- Wiki embedding or rerank failure;
- code-reader failure;
- invalid filters and out-of-scope domains; and
- overlapping SQLite, PostgreSQL, and hosted MCP behavior.

### Registration decision

Both raw parity and workflow quality must pass. The evidence-backed decision is then
presented at a HUMAN CHECKPOINT:

- `implement` authorizes production module creation, FastMCP registration, hosted
  authorization wiring, public documentation, and registry tests; or
- `do not implement` leaves the existing specialized surface unchanged and records why
  the additional public contract was rejected.

## Requirements

- **R-001 — Evidence before registration (T-001, T-007):** evaluate an unregistered
  prototype before any public tool registration. **Acceptance:** AC-001 and AC-010.
- **R-002 — Shared primitives (T-006):** specialized and candidate workflows use the
  same internal Wiki/code primitives with no independent retrieval engine.
  **Acceptance:** AC-002.
- **R-003 — Full read-search filter union (T-003):** expose every selected read-search
  field in the request table and expose none of the excluded write/context controls.
  **Acceptance:** AC-003.
- **R-004 — Separate result blocks (T-005):** return the five named blocks with no score
  fusion or cross-block order. **Acceptance:** AC-004.
- **R-005 — Automatic bounded context (T-004):** use at most three first-ranked unique
  code IDs and fixed depth-one, source-free context. **Acceptance:** AC-005.
- **R-006 — Revision coherence (T-004, T-005):** accept context and associations only
  for the code-search revision. **Acceptance:** AC-006.
- **R-007 — Independent fail-soft branches (T-005):** code failures block zero valid
  Wiki results; Wiki failures do not erase valid code/context results.
  **Acceptance:** AC-007.
- **R-008 — Authorization and backend isolation (T-005, T-007):** require primary
  code-read authority plus requested Wiki-domain grants and never fall back between
  backends. **Acceptance:** AC-008.
- **R-009 — Coordination plus workflow quality (T-001, T-002):** require fewer calls,
  higher workflow correctness, raw parity, and no per-scenario regression.
  **Acceptance:** AC-009.
- **R-010 — Existing contract preservation (T-005, T-006):** specialized tool schemas,
  responses, tests, and standalone benchmark gates remain unchanged.
  **Acceptance:** AC-002 and AC-011.
- **R-011 — Read-only behavior (T-005):** unified evaluation and production search do
  not mutate Wiki, graph, frontmatter, publication state, or durable schema.
  **Acceptance:** AC-012.

## Acceptance Criteria

- **AC-001:** evaluation can execute the candidate without listing
  `wiki_unified_search` in a real FastMCP tool registry.
- **AC-002:** focused tests prove specialized handlers and the candidate call the same
  private primitives; no second retrieval/ranking implementation exists.
- **AC-003:** generated FastMCP schema, if registration is authorized, contains exactly
  the request-table fields and excludes `intent`, `heading`, seeds, traversal, source,
  and context-budget fields.
- **AC-004:** response tests prove five separate blocks, unchanged intra-block ranks,
  confirmed associations only, and absence of a combined score/order.
- **AC-005:** zero code hits perform no context read; one to three hits produce one
  depth-one context read; more than three hits still use exactly three unique seeds.
- **AC-006:** a forced revision change preserves Wiki/code results and returns empty
  context/associations with `revision_changed`.
- **AC-007:** each failure-table row produces its specified partial response and only
  sanitized metadata.
- **AC-008:** hosted tests reject missing primary read authority, unauthorized requested
  domains, `iwiki_id`, and singular `domain`; authorized mixed reads succeed without
  backend fallback.
- **AC-009:** the recorded comparison has raw parity, fewer client-visible calls, higher
  aggregate workflow correctness, and no scenario regression.
- **AC-010:** failed evaluation leaves the real MCP registry and public docs without the
  tool; passed evaluation still requires explicit registration approval.
- **AC-011:** focused specialized-tool tests, package/tool registry tests, standalone
  benchmark gates, and the full test suite pass without contract changes.
- **AC-012:** mutation spies and storage assertions observe no Wiki write, graph index,
  publication, schema migration, or alternate-backend call.

## Verification Strategy

The plan must map requirements to focused tests in the existing test layout:

- pure orchestration and response-shape tests under `tests/`;
- evaluation runner/report tests under `tests/eval/`;
- SQLite runtime and revision-race fixtures under `tests/codegraph/`;
- PostgreSQL reader and hosted authorization fixtures under `tests/postgres/`;
- real FastMCP registry/schema checks in `tests/test_package.py`; and
- the existing full suite through `uv run pytest -q`.

The comparative report is a versioned evidence artifact. It names the selected decision,
scenario outcomes, raw parity result, workflow metrics, tool traces, registration state,
and any blocker. Tests that do not require an external agent remain deterministic. Agent
workflow runs record their exact environment instead of being presented as deterministic
unit tests.

## Risks and Mitigations

- **Agent-evaluation variance:** record exact prompts, model, runs, traces, and per-case
  scores; do not infer quality from one favorable example.
- **Public API duplication:** registration requires workflow-quality evidence; otherwise
  stop at `do not implement`.
- **Search/context activation race:** require matching revisions and fail soft only the
  dependent blocks.
- **Hosted authority confusion:** authorize both primary code read and explicit Wiki
  domains before dispatch.
- **Backend drift:** share primitives and run overlapping parity fixtures across SQLite,
  PostgreSQL, and hosted MCP.
- **Response-size growth:** keep existing Wiki/code limits, three context seeds, depth
  one, source disabled, and existing context budgets.
- **Hidden score fusion:** retain raw block order and prohibit a combined score or rank.

## Human Checkpoints

- Approve this checked design before implementation planning.
- After comparative evaluation, approve or reject public registration using the recorded
  evidence.
- Any proposal to change ranking, freshness semantics, request/response fields, context
  bounds, or the registration gates returns to design review.
- Removal of specialized tools, automatic Wiki writes, hidden backend fallback, and
  incomparable score fusion remain outside human-delegated implementation authority.

## Documentation Impact

Before public registration, evaluation evidence and the task decision are documented,
but README/tool matrices continue to state that unified search is not implemented. If
registration is approved, update `README.md`, `docs/README.ru.md`,
`docs/architecture.md`, package/tool lists, and the bound iwiki pages describing the MCP
surface and daily agent workflow. If registration is rejected, update the workflow docs
with the retained specialized composition and the evidence-backed rationale.

## Definition of Done

The design is complete when the comparative evidence yields one of two observable
outcomes:

- **Implement:** raw parity passes, workflow correctness improves without scenario
  regression, client-visible calls decrease, registration is explicitly approved, the
  production contract and documentation match this design, and all required checks pass.
- **Do not implement:** evidence fails to prove material workflow value, no public tool or
  unused production module remains, the specialized workflow stays unchanged, and the
  decision and evidence are documented.
