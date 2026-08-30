---
review:
  spec_hash: 353b804c0a4829b6
  last_run: 2026-08-30
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-08-29-bdd-event-sourcing-verification-intent.md
---

# Design: BDD Event-Sourcing Verification

**Date:** 2026-08-29
**Status:** approved
**Topic:** `bdd-event-sourcing-verification`
**Intent:** `docs/superpowers/intents/2026-08-29-bdd-event-sourcing-verification-intent.md`

## 1. Summary

This design adds a server-owned specification layer to iwiki. A specification remains
human-readable Markdown, while a fenced `iwiki-gwt` TOML block gives the server a
deterministic representation of Given-When-Then behavior, executable-test evidence,
and semantic implementation targets. The design supports event-sourced aggregate
specifications and event-driven request/response scenarios without coupling ordinary
Wiki behavior to the specification layer or to the optional code graph.

Markdown is the canonical authored source. Git and PostgreSQL maintain equivalent
derived scenario, binding, and last-resolution-evidence projections. Specification
relations are separate from the structural code graph: `implements` and `verifies`
never become `DECLARES`, `IMPORTS`, `CALLS`, or `INHERITS` rows. A ready graph can
resolve declared selectors; an absent, disabled, stale, failed, or unreachable graph
produces explicit fail-soft evidence and blocks no Wiki page operation.

Projects select `disabled`, `optional`, or `strict` behavior through TOML. `optional`
is the compatibility-preserving default. `strict` rejects invalid mutations only when
the target page is classified as `type: specification`; ordinary pages remain governed
solely by existing Markdown and frontmatter rules in every mode.

## 2. Source Decisions

The approved design applies these source-backed decisions:

- Event-sourced aggregate scenarios express past events in Given, one command in When,
  and emitted events or an exception in Then.
- Event-driven boundary scenarios may express Given events, a When request, and both a
  Then response and Then events. They describe public contracts rather than internal
  state.
- Scenario text is living documentation, while executable tests remain the evidence
  that implementation satisfies the behavior.
- Semantic scenario bindings may be analyzed with a code graph, but the graph is an
  enrichment source rather than the specification authority.
- Performance is measured after implementation and is not a release threshold for this
  delivery.

## 3. User Tasks

- **T-001 — Coexisting Wiki modes:** allow ordinary Wiki pages and GWT specification
  pages in the same domain, with `disabled`, `optional`, and `strict` TOML modes.
- **T-002 — Stable executable semantics:** represent stable scenario identity, explicit
  Given-When-Then phases, domain roles, expected outcomes, implementation targets, and
  executable-test targets in readable Markdown.
- **T-003 — Durable server authority:** validate and persist scenario semantics,
  bindings, and last resolution evidence consistently in Git and PostgreSQL storage.
- **T-004 — Optional graph enrichment:** resolve specification selectors when a ready
  graph exists and retain readable fail-soft evidence when it does not.
- **T-005 — Semantic queries:** let clients search scenarios, read complete scenario
  context, and explicitly refresh persisted resolution evidence.
- **T-006 — Ordinary Wiki compatibility:** preserve existing page, indexing, search,
  retrieval, and lint behavior for non-specification pages and projects with no GWT.
- **T-007 — Agent maintenance practice:** publish durable rules for iClaude and iCodex
  skills to author, test, bind, review, and maintain behavioral specifications.
- **T-008 — Evidence-based performance follow-up:** measure the implemented paths and
  defer optimization decisions until observed results exist.

## 4. Goals and Non-Goals

### 4.1 Goals

- Give agents and people one readable, deterministic behavioral contract.
- Keep specification meaning stable across page and section moves.
- Make authored behavior, implementation targets, and test targets queryable without a
  code graph.
- Persist the last resolution attempt with enough revision evidence to distinguish a
  current result from stale code or changed specification text.
- Preserve local Git, local PostgreSQL, and hosted PostgreSQL authorization and storage
  boundaries.
- Provide verification rules that improve agent changes without making GWT mandatory
  for mechanical or documentation-only work.

### 4.2 Non-Goals

- No Gherkin feature runner, test generator, test execution service, or shell execution
  from Wiki content.
- No automatic acceptance of implementation behavior based only on a resolved symbol.
- No mandatory GWT page for every change, page, domain, or project.
- No structural code-graph relation, entity-kind, source parser, publication payload,
  or ranking change.
- No migration or rewrite of existing Markdown pages.
- No background watcher, daemon, automatic graph rebuild, or automatic evidence refresh.
- No performance acceptance threshold in this delivery.
- No implementation of the future iClaude or iCodex skills; this delivery publishes
  the rules those skills must follow.

## 5. Terms and Invariants

- A **specification page** has normalized frontmatter `type: specification`.
- A **scenario block** is one fenced code block whose info string is exactly
  `iwiki-gwt` and whose body is valid UTF-8 TOML under Section 6.
- A **scenario ID** is a lowercase kebab-case identifier unique among projected
  scenarios in one Wiki domain.
- A **scenario identity** is `<domain>#<scenario-id>`. Page slug and section anchor are
  location fields and do not participate in the identity.
- A **binding** connects a scenario or one phase to a declared code selector through
  specification relation `implements` or `verifies`.
- **Resolution evidence** is the persisted result of the most recent explicit
  `wiki_spec_resolve` attempt. It does not assert that tests passed.
- **Freshness** is computed at read time by comparing persisted specification and graph
  revisions with current revisions. Reading context never mutates evidence.
- **Projection state** is one tenant/domain record describing the last complete
  successful projection, including its Markdown and logical projection revisions.
  Missing state for an upgraded domain that still has specification sources or derived
  rows is stale until a successful rebuild.
- Ordinary pages are never parsed as semantic specifications, even when their prose
  contains a literal `iwiki-gwt` fence.
- Structural code graph data remains independent and optional.

## 6. Markdown and Scenario Grammar

### 6.1 Page structure

`specification` becomes a governed OKF type. A specification page follows all existing
frontmatter, H1, H2, lead, section-operation, and optimistic-concurrency rules. Each
scenario block belongs to the containing H2 section. One H2 section may contain at most
one scenario block; a page may contain multiple scenario sections and ordinary
explanatory sections.

Specification classification is explicit: a caller must supply or preserve normalized
`type: specification`. The optional server classifier never proposes `specification`
when the caller omits `type`; it continues to choose only ordinary documentation types.
This prevents prose, domain terminology, or a literal fence from silently enabling
specification validation.

Example:

````markdown
---
type: specification
title: Account opening behavior
description: Observable account opening rules.
tags: [account, event-sourcing]
status: developing
---
# Account opening behavior

## Confirm account opening

The account confirms an accepted opening request.

```iwiki-gwt
id = "confirm-account-opening"
title = "Confirm account opening"

given = [
  { role = "event", name = "AccountOpeningRequested" }
]

when = { role = "command", name = "ConfirmAccountOpening" }

then = [
  { role = "event", name = "AccountOpened" }
]

code = [
  { relation = "implements", phase = "when", symbol = "accounts.Account.confirm" },
  { relation = "verifies", symbol = "tests.accounts.test_confirm_account_opening" }
]
```
````

### 6.2 Scalar bounds

- `id` is required, contains 1–128 UTF-8 bytes, and matches
  `[a-z0-9]+(?:-[a-z0-9]+)*`.
- `title` is required, nonblank, contains no NUL, and contains at most 250 Unicode code
  points.
- Every phase-item `name` is required, nonblank, contains no NUL, and contains at most
  1,024 UTF-8 bytes.
- Unknown top-level, phase-item, or binding keys are invalid.
- Duplicate TOML keys and malformed TOML are invalid.

### 6.3 Phase grammar

- `given` is a required list and may be empty. Each item contains exactly `role` and
  `name`. Allowed roles are `event`, `state`, and `fact`.
- `when` is one required table containing exactly `role` and `name`. Allowed roles are
  `command`, `request`, and `action`.
- `then` is a required non-empty list. Each item contains exactly `role` and `name`.
  Allowed roles are `event`, `response`, `outcome`, and `exception`.
- One scenario may combine `response` and `event` outcomes. An `exception` outcome is
  exclusive and cannot coexist with any other Then item.
- Duplicate phase items with the same phase, role, and name are invalid.

### 6.4 Binding grammar

`code` is a required non-empty list. Each item contains:

- required `relation`: exactly `implements` or `verifies`;
- optional `phase`: exactly `given`, `when`, or `then`; omission binds the whole
  scenario; and
- exactly one selector: `symbol`, `file`, or `source_glob`.

Selector values reuse the existing code-selector safety, size, POSIX-path, glob, and
qualified-name rules. Duplicate bindings are invalid. A complete scenario contains at
least one `implements` binding and at least one `verifies` binding. This requirement
keeps implementation and executable-test evidence visible without requiring either
target to resolve when the specification is first written.

Specification relation types are never inserted into structural code-graph relation
tables. A deterministic binding ID is the SHA-256 digest of the NUL-joined UTF-8 values
`domain`, `scenario_id`, `relation`, normalized phase or empty string, selector kind,
and selector value, prefixed with `spec:binding:`. It therefore survives page and
section moves but changes when binding meaning changes.

## 7. Mode and Configuration Contract

### 7.1 Local project configuration

Local Git and local PostgreSQL processes accept one optional project table:

```toml
[specifications]
mode = "optional"
```

The only allowed key is `mode`; the only values are `disabled`, `optional`, and
`strict`. An absent table or absent mode means `optional`. Invalid table types, unknown
keys, non-string values, or unknown modes fail binding with a sanitized configuration
error. The project mode applies to every domain visible through that process, while
write enforcement still follows each domain's existing write authorization.

### 7.2 Hosted server configuration

Hosted PostgreSQL accepts:

```toml
[specifications]
default_mode = "optional"

[[specifications.overrides]]
iwiki_id = "team-wiki"
domain = "payments"
mode = "strict"
```

`default_mode` is optional and defaults to `optional`. Every override requires exactly
one nonblank `iwiki_id`, one valid domain, and one mode. The pair `(iwiki_id, domain)`
is the unique override key. Duplicate pairs are a startup error even when their modes
match. Unknown keys, incomplete overrides, and unknown modes are startup errors.
Effective precedence is exact override, then hosted default, then built-in `optional`.
Only the hosted operator changes this file; no MCP request or `wiki_bind` argument can
change, lower, or expand the policy.

### 7.3 Behavior matrix

| Mode | Ordinary page | Specification page without a block | Specification page with invalid or duplicate semantics | Valid specification page |
|---|---|---|---|---|
| `disabled` | Existing behavior | Existing behavior | Existing behavior | Stored as ordinary Markdown; specification tools report disabled |
| `optional` | Existing behavior | Stored; advisory `missing_scenario` finding; no projection row | Stored; advisory finding; invalid/duplicate scenarios are excluded from projection | Stored and projected |
| `strict` | Existing behavior | Mutation rejected before page/projection change | Mutation rejected before page/projection change | Stored and projected |

In all modes, ordinary page parsing, validation, writing, section editing, deletion,
indexing, retrieval, search, and lint never depend on specification parsing or storage.
`wiki_index` and `wiki_lint` remain domain-wide fail-soft operations: invalid existing
specification pages produce findings but never prevent ordinary Markdown indexing or
lint output. Strict blocking applies only to a mutation whose target page is a
specification page.

## 8. Component Boundaries

### 8.1 Engine parser and model

`src/iwiki_mcp/engine/specifications.py` is a standard-library-only core. It owns fence
discovery inside H2 sections, TOML parsing through Python 3.11 `tomllib` or the existing
Python 3.10 `tomli` dependency, immutable scenario and binding models, grammar
validation, deterministic source hashes and binding IDs, duplicate detection, and
serializable findings. It imports no server, storage, code-graph runtime, embedding,
Git, PostgreSQL, or MCP code.

### 8.2 Specification application service

`src/iwiki_mcp/specifications.py` owns domain projection assembly, mode decisions,
search/context response assembly, persisted-evidence freshness, and code-reader
adaptation. It accepts page snapshots, a storage adapter, and an optional code reader.
It never executes tests, source code, shell commands, or graph builds.

### 8.3 Storage adapters

`src/iwiki_mcp/specification_store.py` defines the logical scenario, binding, and
resolution-evidence records plus the Git JSONL adapter. PostgreSQL persistence stays in
`src/iwiki_mcp/postgres/store.py` and migrations. Both adapters expose equivalent
replace-domain-projection, search, context, record-resolution, and status behavior.

### 8.4 Server and transport integration

`src/iwiki_mcp/server.py` resolves the binding and effective domain mode, applies
specification validation only at the page-mutation boundary, composes storage and
optional code readers, and registers the three MCP tools. `src/iwiki_mcp/http.py`
authorizes tools before dispatch. Existing page helpers remain the authority for Wiki
authorization, optimistic concurrency, Git freshness, and Markdown mutation.

### 8.5 Code graph boundary

Existing code graph readers remain unchanged public primitives. The specification
service may resolve declared selectors through an adapter, but code-graph schemas,
structural relations, publication payloads, index lifecycle, and public code tools stay
unchanged. Specification failures never mark the code graph failed, and graph failures
never invalidate authored GWT semantics.

## 9. Persistence and Projection Lifecycle

### 9.1 Canonical source and logical records

Markdown remains canonical. The projection contains:

- scenario identity, title, page slug, H2 heading/anchor, scenario block source hash,
  Given/When/Then items, and page revision;
- binding ID, relation, optional phase, selector kind, and selector value; and
- last resolution attempt state, target IDs or unresolved reference, graph revision,
  sanitized graph-state fingerprint, specification source hash, checked time, and
  sanitized reason.

Projection rows are derived and rebuildable; resolution evidence is durable operational
evidence and is preserved only while its binding ID remains present.

### 9.2 Git projection

Git storage uses `<domain>/specifications.jsonl`. It is a generated, tracked domain
artifact, not a Markdown page, graph node, link target, or OKF page. The first row is
format metadata with version `1`, domain, canonical Markdown revision, scenario count,
and binding count. Remaining rows are canonically ordered scenario, binding, and
evidence records. Atomic replacement uses a same-directory temporary file, flush, and
rename under the existing cross-process Git mutation lock. The page and projection are
included in the same Git commit for a valid strict mutation. Strict preparation records
the previous page and projection bytes; a write, index, or commit failure restores both
under the same lock before returning an error, so neither working-tree nor committed
state exposes a partial strict mutation.

`disabled` never reads, creates, updates, or removes this file. `optional` projects only
syntactically valid, complete, domain-unique scenarios; invalid and duplicate scenario
IDs remain Markdown plus advisory findings. `strict` prepares a complete valid
projection before changing a specification page. After the file has first been
created, deleting or renaming the last valid scenario deterministically replaces it
with the metadata row and zero counts rather than deleting it. `wiki_index` rebuilds
the projection after out-of-band Markdown changes and preserves evidence only for
unchanged binding IDs whose stored specification source hash still matches.

An optional-mode projection failure does not roll back authored Markdown or ordinary
Wiki indexing. It leaves the previous projection untouched, commits Markdown through the
ordinary path, returns a sanitized specification warning, and reports the projection
stale. A strict specification-page mutation fails before its page commit when the
projection cannot be prepared; publication or Git commit failure restores the previous
page and projection bytes. No projection failure affects an ordinary page mutation.

### 9.3 PostgreSQL projection

PostgreSQL schema version 6 adds tenant-scoped tables for specification scenarios,
bindings, and resolution evidence. Schema version 7 adds
`specification_projection_state`, keyed by `iwiki_id` and `domain_id`, with the last
successful Markdown revision, logical projection revision, scenario/binding counts,
sanitized projection findings, and update time. Findings are stored as a non-null JSONB
array with an empty-array default and a JSON-array check. Each element uses the same
canonical sanitized finding record as the Git metadata row, including every authorized
location for a duplicate scenario ID. The metadata table uses the same runtime grants,
command-specific row level security, and tenant/domain authorization pattern as the
three projection tables. It is independent of structural code-graph tables.

Version 7 also makes a stale snapshot representable after page deletion. It adds a
non-null stored `page_slug` to every scenario, backfilled from the referenced page;
`page_id` becomes nullable and its foreign key changes from `ON DELETE CASCADE` to
`ON DELETE SET NULL`. Scenario identity still excludes location. A present page keeps
its normal composite foreign-key integrity, while a deleted page leaves its last
scenario, binding, and evidence rows readable by stored slug until the next successful
projection replacement. Domain deletion still cascades every projection and metadata
row.

The v6-to-v7 migration does not invent a successful revision for existing projections.
An upgraded domain without a metadata row is reported stale when specification sources
or derived rows exist, then becomes ready after `wiki_index` or the next successful
specification mutation writes rows and metadata atomically. A domain with no
specification sources, derived rows, or metadata remains absent. A successful empty
rebuild writes metadata with zero counts and its complete findings array so removal of
the last scenario is durably distinguishable from an uninitialized domain. Every
successful replacement writes scenarios, bindings, preserved evidence, findings, and
metadata in one transaction.

The logical behavior matches Git: optional mode retains Markdown and reports a
projection warning if a derived refresh fails. Optional projection refresh runs in a
transaction savepoint: its failure rolls back derived rows to their previous state while
the outer page transaction may commit. When that mutation deletes a specification page,
the outer delete sets stale scenario `page_id` values to null rather than cascading the
snapshot; the failed savepoint leaves projection metadata unchanged, and read-time
revision comparison reports stale. Search and context may return that previous snapshot
with its stored page slug only while explicitly marking it stale. They read duplicate
and other projection findings from that same persisted metadata and never reparse
current Markdown to answer a semantic query. A successful refresh replaces all domain
projection rows, findings, and metadata together, removing deleted scenarios and
obsolete findings.

Strict mode uses one transaction and commits neither page nor projection metadata on any
failure; ordinary pages bypass specification persistence. Domain projection rebuild
scans one coherent domain page snapshot so cross-page duplicate IDs are handled
deterministically. Evidence survives a refresh only for an unchanged binding ID and
matching scenario source hash. PostgreSQL exports continue to export canonical Markdown;
derived projections rebuild after import.

Hosted and stdio startup require schema version 7. A scoped v7 compatibility rollback
restores the v6 page foreign key and removes only the v7 metadata, stored-slug column,
and migration marker under the migration advisory lock. It fails closed without any
DDL when detached scenario rows (`page_id IS NULL`) exist, because v6 cannot represent
them without data loss. The rollback helper requires literal `confirm=True`; successful
rollback and idempotent v7 reapplication are integration-tested.

### 9.4 Duplicate and move behavior

Projection assembly checks domain-wide uniqueness after parsing all specification
pages. In optional mode, every instance of a duplicated scenario ID is excluded from
the query projection and reported with every page location. PostgreSQL persists this
duplicate finding in the same successful snapshot as its projection metadata, so
`wiki_spec_context` returns `ambiguous_scenario_id` from persisted authorized locations.
If a later optional refresh fails, context continues to use the previous findings and
projection revision instead of interpreting newer Markdown. In strict mode, a mutation
that introduces a duplicate is rejected. Moving a unique scenario without changing its
block keeps scenario identity and binding IDs; location fields update and matching
resolution evidence remains valid unless the source or graph revision changed.

## 10. Resolution Evidence and Freshness

### 10.1 Persisted attempt states

The last explicit resolution attempt for each binding stores exactly one state:

- `resolved`: one target was resolved;
- `ambiguous`: more than one valid target was resolved;
- `unresolved`: a ready current graph contained no valid target; or
- `graph_unavailable`: no ready current graph could be trusted.

`graph_unavailable` includes a sanitized reason code such as `not_configured`,
`disabled`, `missing`, `dirty`, `rebuilding`, `failed`, `stale_graph`,
`source_unavailable`, or `not_primary`. It never contains a path, URL, DSN, credential,
exception string, or source text.

### 10.2 Read-time freshness

`wiki_spec_context` returns persisted evidence without resolving or mutating it. For
each binding it computes:

- `not_checked` when no evidence exists;
- `stale_spec` when the persisted scenario source hash differs from the current block;
- `stale_graph` for `resolved`, `ambiguous`, or `unresolved` evidence when the current
  graph is non-ready or its ready revision differs from the persisted revision;
- `stale_graph` for `graph_unavailable` evidence when the current sanitized graph-state
  fingerprint differs from the persisted fingerprint; or
- `fresh` when the specification hash matches and either ready-graph evidence has the
  current ready revision or `graph_unavailable` has the current non-ready fingerprint.

`stale_spec` takes precedence over `stale_graph`. The graph-state fingerprint contains
only normalized state, sanitized reason code, and an available graph revision or `null`;
it contains no raw error detail. Graph recovery or any state, reason, or revision change
makes prior `graph_unavailable` evidence `stale_graph` until explicitly resolved.
Freshness never changes the persisted attempt and never blocks scenario retrieval.

### 10.3 Explicit refresh

`wiki_spec_resolve` resolves every binding in one scenario against one guarded current
graph snapshot. It records one coherent attempt only after all selectors finish. A
snapshot revision change aborts target publication and records `graph_unavailable` with
reason `revision_changed`. Missing or non-ready graph state records fail-soft evidence
without changing Wiki Markdown, code selectors, or graph data.

Resolution proves only that declared code targets exist or remain unresolved. A
`verifies` target is executable-test evidence by location; iwiki does not run it or
claim a passing result. Agents record actual test commands, exit status, and repository
revision in the task ledger.

## 11. MCP and Authorization Contracts

### 11.1 `wiki_spec_search`

Logical signature:

```text
wiki_spec_search(query, domains=None, limit=20)
```

- `query` is required, nonblank UTF-8, contains no NUL, and is at most 4,096 bytes.
- `domains` is optional and must stay inside bound read scope. Omission searches all
  readable domains.
- `limit` is an integer from 1 through 100.
- Search matches case-folded tokens against scenario ID, title, phase role/name, and
  selector values. All distinct query tokens must match at least one field.
- Results sort by exact scenario-ID match, exact title match, token coverage, domain,
  scenario ID, and page slug. Scores are not compared with Wiki or code search scores.
- Each result returns identity, title, location, matching semantic fields, binding
  summary, projection state, and effective mode. It does not require or query a code
  graph.
- A disabled domain returns a per-domain disabled state and no scenario result.

### 11.2 `wiki_spec_context`

Logical signature:

```text
wiki_spec_context(domain, scenario_id)
```

The call requires domain read scope. It returns one unique scenario's location, source
hash, full Given/When/Then semantics, bindings, persisted last-resolution evidence,
computed freshness, projection revision, and sanitized findings. It never resolves,
updates evidence, runs tests, or changes Markdown. A missing ID returns `not_found`; an
optional-mode duplicate returns `ambiguous_scenario_id` with authorized locations.
Disabled mode returns `specifications_disabled`.

### 11.3 `wiki_spec_resolve`

Logical signature:

```text
wiki_spec_resolve(domain, scenario_id)
```

The call requires domain write scope because it persists evidence. Hosted authorization
derives `iwiki_id` from the authenticated context and rejects caller-supplied tenant
identity. When the domain is not the bound primary code domain, the call records
`graph_unavailable/not_primary`. The call never expands `wiki_bind`, domain grants, or
code publication authority. It returns the same scenario/binding evidence shape as
context plus the coherent attempt state.

### 11.4 Errors and redaction

Structural input and authorization failures reject before storage or graph reads.
Specification parsing, projection, and graph errors return stable sanitized codes and
never expose credentials, environment values, private base paths, DSNs, URLs, raw
exceptions, or source text. Search and context remain available when graph resolution
cannot run. Existing `_safe` and HTTP authorization boundaries remain authoritative.

## 12. Status and Lint Contracts

### 12.1 `wiki_status`

`wiki_status` adds a `specifications` block without removing existing fields:

```text
specifications:
  domains:
    - domain
      mode
      source: project | hosted_default | hosted_override | built_in_default
      projection_state: disabled | absent | ready | stale | failed
      scenarios
      bindings
```

Hosted output reveals no unrelated `iwiki_id` or override. Local output reports
`source=project` for an explicit project mode and `built_in_default` when absent.
Projection failure does not change Wiki storage or code-graph status.

### 12.2 `wiki_lint`

Every domain report adds one independent `specifications` block containing effective
mode, projection state/revision, counts, and findings. Finding types are:

- `missing_scenario` for a specification page without a scenario block;
- `invalid_scenario` for fence/TOML/grammar violations;
- `duplicate_scenario_id` with every authorized location;
- `incomplete_bindings` when `implements` or `verifies` is missing;
- `projection_stale` or `projection_failed`;
- `binding_unresolved`, `binding_ambiguous`, `resolution_not_checked`,
  `resolution_stale_spec`, `resolution_stale_graph`, or `graph_unavailable`.

Disabled mode returns `state=disabled` and no specification findings. Optional syntax,
completeness, duplicate, projection, and resolution findings are advisory. Strict
syntax, completeness, and duplicate findings are blocking for future mutations of the
reported specification page, while projection and resolution findings remain advisory.
`wiki_lint` itself remains read-only and returns the complete ordinary Wiki report even
when specification findings exist.

## 13. Agent Authoring and Maintenance Rules

The implementation publishes an English Wiki page and updates the
`iwiki://authoring-rules` resource. These durable rules apply equally to future iClaude
and iCodex skills:

1. Create or update a scenario for a new observable domain behavior, public contract,
   bug reproduction, or business invariant. Do not require one for formatting,
   mechanical refactoring with unchanged behavior, or ordinary Wiki maintenance.
2. Write Given as prior domain facts/events/state, When as one observable trigger, and
   Then as public events, responses, outcomes, or an exclusive exception. Do not encode
   internal method steps or database state as expected behavior.
3. Keep the existing scenario ID when wording, page location, implementation, or test
   location changes but observable behavior remains the same. Propose a behavioral
   contract change before changing Given/When/Then meaning.
4. Add at least one `implements` and one `verifies` selector. Planned unresolved targets
   are acceptable while implementing specification-first behavior.
5. Write or update the executable test before or with implementation. Run the focused
   test and relevant regression suite; record command, exit status, and repository
   revision in the task ledger.
6. Call `wiki_spec_context` before changing an existing specification. When a ready
   graph exists, call `wiki_spec_resolve` after code/test changes. Treat ambiguous,
   stale, or unresolved evidence as a maintenance finding, not as permission to guess.
7. When the graph is absent or unusable, continue Wiki and GWT work, preserve declared
   selectors, record graph-unavailable evidence, and verify through repository search
   and executable tests. Never block ordinary Wiki work on graph recovery.
8. Review scenario, executable test, implementation bindings, and test evidence as one
   coherent unit before reporting the change complete.

The Wiki page includes the syntax example, mode matrix, lifecycle checklist, and
graph-unavailable fallback. It contains no client-specific hidden semantics; clients
consume the server contract documented here.

## 14. Data Flows

### 14.1 Specification-page mutation

1. Resolve binding, authorization, target page identity, and effective domain mode.
2. Apply the requested page/section mutation in memory under existing CAS and structure
   rules.
3. If mode is disabled or the result is not a specification page, use the existing Wiki
   path without specification work.
4. Parse the complete candidate page and assemble a coherent domain projection snapshot.
5. Optional mode records advisory findings and excludes invalid/incomplete/duplicate
   scenarios; strict mode rejects the target mutation before any page/projection change.
6. Publish Markdown, ordinary indexes, and specification projection according to the
   storage transaction/lock contract.
7. Preserve evidence only for unchanged binding IDs and matching scenario source hashes.
8. Return ordinary Wiki fields plus sanitized specification counts/findings.

### 14.2 Ordinary-page mutation

1. Resolve existing Wiki authorization and mutation rules.
2. Determine that normalized type is not `specification`.
3. Run the existing page, index, graph-link, Git/PostgreSQL, and CAS flow.
4. Do not parse scenario fences, open the specification projection, or inspect the code
   graph.

### 14.3 Search and context

1. Validate inputs and read authorization.
2. Resolve each domain's effective mode and current projection state.
3. Read persisted scenario semantics without a graph call.
4. For context, compare persisted evidence revisions with current specification and
   graph status to compute freshness without mutation.
5. Return semantic results and sanitized degraded-state metadata.

### 14.4 Explicit resolution

1. Validate input and write authorization.
2. Read one unique current scenario and its bindings.
3. Acquire one guarded graph snapshot when the target is the primary domain.
4. Resolve every selector or build one coherent graph-unavailable attempt.
5. Recheck scenario source and graph revision before persistence.
6. Persist evidence transactionally and return it with current freshness.

## 15. Requirements

### 15.1 Syntax and identity

- **R-001 — Page classification:** only normalized `type: specification` pages are
  semantic specification pages, and automatic classification must never assign that
  type. Ordinary pages never enter GWT validation. **Acceptance:** AC-001, AC-002.
- **R-002 — Deterministic grammar:** the server must parse and validate exactly the
  fenced TOML grammar and bounds in Section 6. **Acceptance:** AC-003.
- **R-003 — Stable scenario identity:** domain-wide identity must exclude page and
  section location and must preserve bindings across moves. **Acceptance:** AC-004.
- **R-004 — Observable roles:** Given, When, and Then must enforce their approved role
  vocabularies, single-trigger rule, and exception exclusivity. **Acceptance:** AC-005.
- **R-005 — Complete evidence bindings:** every projected scenario must contain at
  least one implementation binding and one executable-test binding using approved
  selectors. **Acceptance:** AC-006.

### 15.2 Modes and compatibility

- **R-006 — Three modes:** local and hosted TOML must implement the exact default,
  precedence, validation, and behavior matrix in Section 7. **Acceptance:** AC-007,
  AC-008.
- **R-007 — Optional compatibility:** default optional mode must add no blocking
  behavior to existing pages or projects and require no page migration. **Acceptance:**
  AC-009.
- **R-008 — Strict isolation:** strict mode may reject only a mutation whose resulting
  target is a specification page; domain-wide index/lint and ordinary-page operations
  remain available. **Acceptance:** AC-010.
- **R-009 — Disabled isolation:** disabled mode must avoid specification parsing,
  projection mutation, and graph resolution. **Acceptance:** AC-011.

### 15.3 Persistence and queries

- **R-010 — Equivalent projections:** Git JSONL and PostgreSQL tables must preserve the
  same logical scenario, binding, evidence, and sanitized finding records, including
  duplicate handling and last-success stale-snapshot semantics.
  **Acceptance:** AC-012.
- **R-011 — Atomic strict mutation:** a strict specification-page mutation must commit
  page and projection together or change neither. **Acceptance:** AC-013.
- **R-012 — Durable resolution evidence:** the most recent explicit attempt must persist
  state, selector, targets/reference, graph revision, sanitized graph-state fingerprint,
  specification hash, checked time, and sanitized reason. **Acceptance:** AC-014.
- **R-013 — Read-only freshness:** context must return persisted evidence plus computed
  freshness without resolution or mutation. **Acceptance:** AC-015.
- **R-014 — Semantic search/context:** search and context must work inside read scope
  without a configured or ready graph. **Acceptance:** AC-016.
- **R-015 — Explicit resolution:** resolution must require write scope, use one coherent
  graph snapshot, and persist fail-soft evidence without changing Markdown or graph
  data. **Acceptance:** AC-017.

### 15.4 Boundaries, diagnostics, and workflow

- **R-016 — Structural graph preservation:** specification entities and relations must
  not change structural code-graph schema, publication, ranking, or public code tools.
  **Acceptance:** AC-018.
- **R-017 — Status and lint:** status and lint must expose exact mode, source,
  projection state, counts, syntax findings, and resolution findings without removing
  existing report fields. **Acceptance:** AC-019.
- **R-018 — Authorization and redaction:** hosted tools must preserve tenant/domain
  grants and redact protected configuration and runtime details. **Acceptance:** AC-020.
- **R-019 — Agent rules:** the Wiki and authoring resource must publish all eight
  authoring, testing, binding, and graph-unavailable rules in Section 13. **Acceptance:**
  AC-021.
- **R-020 — Regression protection:** all existing ordinary Wiki tests and contracts
  must pass, and injected specification/code-graph failures must block zero ordinary
  Wiki calls. **Acceptance:** AC-022.
- **R-021 — Performance evidence:** implementation verification must record fixture
  size and elapsed time for projection rebuild, search, context, and resolution without
  applying a pass/fail threshold. **Acceptance:** AC-023.
- **R-022 — Durable optional deletion:** PostgreSQL schema v7 must preserve the previous
  logical projection and its metadata when an optional specification-page deletion
  commits but derived refresh fails, while a later successful rebuild removes obsolete
  rows and returns the domain to ready. **Acceptance:** AC-024.

## 16. Acceptance Criteria

- **AC-001:** a normal page containing literal or malformed `iwiki-gwt` text passes the
  same write, update, index, search, read, and lint paths in all three modes.
- **AC-002:** an explicit `type: specification` is accepted case-insensitively after
  normalization, while an omitted type never becomes `specification` through automatic
  classification, including when content contains GWT terms or an `iwiki-gwt` fence.
- **AC-003:** table-driven parser tests cover every allowed role/selector, unknown keys,
  bounds, malformed TOML, multiple blocks per section, duplicate items, and exception
  exclusivity.
- **AC-004:** moving a unique unchanged scenario between pages/sections preserves
  `<domain>#<id>`, binding IDs, and matching evidence; domain duplicates receive the
  optional/strict outcomes in Section 9.4.
- **AC-005:** aggregate command/event/exception and request/response/event examples
  round-trip to the exact semantic model.
- **AC-006:** optional mode stores but excludes incomplete scenarios with advisories;
  strict mode rejects missing `implements` or `verifies` bindings.
- **AC-007:** local configuration tests prove absent/default/three valid modes and reject
  wrong tables, unknown keys, non-string values, and unknown modes.
- **AC-008:** hosted tests prove override precedence, tenant/domain isolation,
  operator-only policy, and startup rejection for duplicate or incomplete overrides.
- **AC-009:** existing project fixtures require no Markdown rewrite or new blocking
  finding under default optional mode.
- **AC-010:** strict invalid-specification mutations change neither page nor projection,
  while ordinary page calls and domain-wide index/lint still succeed.
- **AC-011:** disabled-mode instrumentation proves zero parser, projection, and graph
  calls during all ordinary Wiki operations.
- **AC-012:** shared golden records produced through Git and PostgreSQL adapters are
  byte/field equivalent except backend revision types and storage metadata. A PostgreSQL
  optional-mode integration case persists duplicate locations, forces a later projection
  refresh failure after Markdown changes, and proves search/context keep the previous
  scenarios, findings, and revision without reparsing the newer Markdown.
- **AC-013:** fault injection at parse, projection preparation, projection publication,
  PostgreSQL transaction, and Git commit boundaries proves strict all-or-nothing
  behavior and optional fail-soft behavior.
- **AC-014:** resolve tests persist each state and its required revision, graph-state
  fingerprint, specification hash, time, and reason fields across server restart or new
  database connection.
- **AC-015:** read-only context returns `not_checked`, `fresh`, `stale_spec`, and
  `stale_graph` correctly for ready-graph and `graph_unavailable` evidence, including
  graph recovery and non-ready state/reason changes, and performs no write or
  graph-resolution call.
- **AC-016:** search/context fixtures return semantics and declared selectors for absent,
  disabled, stale, failed, and unreachable graph states.
- **AC-017:** resolution tests prove write authorization, coherent snapshot checks,
  resolved/ambiguous/unresolved evidence, revision-change handling, and no Markdown or
  structural-graph mutation.
- **AC-018:** code-graph schema, model, publication, and query contract tests remain
  unchanged and no specification relation appears in structural relation rows.
- **AC-019:** exact response tests cover `wiki_status.specifications` and every lint
  finding/severity in Section 12 without removing ordinary fields.
- **AC-020:** hosted HTTP tests prove read/write domain authorization, cross-tenant
  denial, caller `iwiki_id` rejection, and sanitized errors.
- **AC-021:** documentation/resource tests prove the Wiki agent rules contain creation
  criteria, stable identity, phase roles, bindings, executable verification, stale
  handling, graph-unavailable fallback, and coherent-unit review.
- **AC-022:** focused no-graph scenarios plus the complete existing pytest suite prove
  ordinary page, index, retrieval, search, and lint compatibility.
- **AC-023:** a deterministic measurement command reports corpus/page/scenario/binding
  counts and elapsed projection/search/context/resolution timings; results are evidence
  only and create no threshold.
- **AC-024:** PostgreSQL migration tests prove v6-to-v7 backfill, nullable page identity
  with `ON DELETE SET NULL`, metadata RLS/grants, exact schema-7 startup guards, and
  fail-closed compatibility rollback. Transaction tests inject optional refresh failure
  after deleting both the last and a non-last specification page, then prove the page
  commit, detached previous rows, unchanged metadata, durable stale status across a new
  connection, readable stale search/context, and successful rebuild to ready with no
  obsolete rows.

## 17. Verification Strategy

### 17.1 Unit tests

- `tests/engine/test_specifications.py`: fence location, TOML grammar, roles, bounds,
  identities, binding IDs, duplicates, and serializable findings.
- `tests/test_specification_config.py`: local and hosted TOML models, defaults,
  precedence, duplicate overrides, and safe errors.
- `tests/test_specification_store.py`: canonical JSONL, atomic replacement, zero-count
  lifecycle, evidence preservation/invalidation, and fault injection.
- `tests/test_specifications.py`: projection assembly, search ranking, context freshness,
  and fail-soft graph adaptation.

### 17.2 Server and Git integration

- Extend write/update/insert/delete/move tests with the exact three-mode matrix.
- Prove ordinary pages do not enter specification code paths.
- Prove strict page/projection atomicity and optional warning behavior.
- Prove out-of-band changes rebuild through `wiki_index` without blocking ordinary
  chunks, links, or search.
- Prove `wiki_spec_search`, `wiki_spec_context`, `wiki_spec_resolve`, status, and lint
  response contracts with ready and unavailable graphs.

### 17.3 PostgreSQL and HTTP integration

- Migration, grants, RLS, transaction, CAS, duplicate-domain-snapshot, and restart
  persistence tests cover schema versions 6 and 7, including projection metadata,
  detached stale rows, fail-closed v7 rollback, and rebuild recovery.
- Hosted tests cover policy overrides, tenant/domain authorization, read-versus-write
  tools, redaction, and streamable HTTP dispatch.
- Git/PostgreSQL golden fixtures compare logical projection and evidence behavior.

### 17.4 Regression and measurement

Required final commands include focused specification suites and `uv run pytest -q`.
Existing code-graph contract suites run unchanged. A deterministic measurement fixture
records, but does not gate on, elapsed time for projection rebuild, search, context, and
resolution. `wiki_lint` must show no new task/documentation finding before result close.

## 18. Risks and Mitigations

| Risk | Mitigation | Acceptance evidence |
|---|---|---|
| Specification parsing breaks ordinary pages | Type classification precedes parser use; disabled and ordinary paths bypass the subsystem | AC-001, AC-010, AC-011, AC-022 |
| Derived projection contradicts Markdown | Store source/page revisions and findings, rebuild from coherent snapshots, compute freshness, and keep Markdown canonical; stale queries use one persisted last-success snapshot | AC-004, AC-012, AC-015 |
| Persisted targets become stale | Persist graph revision and source hash; context reports stale without mutation; refresh is explicit | AC-014, AC-015, AC-017 |
| Code graph becomes mandatory | Search/context never call it; resolve records graph-unavailable evidence | AC-011, AC-016, AC-017 |
| New relations corrupt structural graph | Keep specification relations in separate records/adapters | AC-018 |
| Optional mode creates compatibility failures | Optional findings are advisory and projection failure is fail-soft | AC-009, AC-010, AC-022 |
| Page deletion destroys the last readable stale projection | Schema v7 stores page slug, detaches page ID with `ON DELETE SET NULL`, and keeps last-success metadata until rebuild | AC-013, AC-024 |
| Hosted policy leaks or conflicts across tenants | Exact tenant/domain override key, startup duplicate rejection, existing auth context | AC-008, AC-020 |
| Agents treat resolved selectors as passing behavior | Authoring rules require executable tests and task-ledger evidence | AC-021 |
| Generated Git projection conflicts | Canonical order, one lock, atomic replacement, same commit, rebuild via `wiki_index` | AC-012, AC-013 |
| Initial implementation is slower than expected | Record deterministic measurements, then decide optimization separately | AC-023 |

## 19. Documentation and Wiki Deliverables

- Update `README.md` with syntax, local/hosted configuration, modes, tools, status,
  lint, persistence, and no-graph behavior.
- Update `docs/README.ru.md` with equivalent user guidance in Russian.
- Update `docs/architecture.md` with specification parser, projection, evidence,
  storage, authorization, and optional graph boundaries.
- Update `src/iwiki_mcp/resources.py` so `iwiki://authoring-rules` contains the Section
  13 workflow.
- Update project configuration templates without secrets or private paths.
- Publish or update an English iwiki guide page for BDD/event-sourcing agent rules and
  update the relevant architecture/configuration Wiki pages through MCP tools.
- Run `wiki_lint`; unrelated existing advisories remain out of scope, while every new
  broken, stale, contradictory, or task-page finding blocks completion.

## 20. Traceability

| Intent outcome / user task | Requirements | Acceptance criteria |
|---|---|---|
| T-001; three modes and coexistence | R-001, R-006–R-009 | AC-001, AC-002, AC-007–AC-011 |
| T-002; stable readable executable semantics | R-002–R-005 | AC-003–AC-006 |
| T-003; durable server authority | R-010–R-013, R-022 | AC-012–AC-015, AC-024 |
| T-004; optional graph enrichment | R-015, R-016, R-020 | AC-016–AC-018, AC-022 |
| T-005; semantic queries | R-013–R-015, R-017, R-018 | AC-015–AC-020 |
| T-006; ordinary Wiki compatibility | R-001, R-007–R-009, R-020 | AC-001, AC-009–AC-011, AC-022 |
| T-007; durable agent practice | R-005, R-019 | AC-006, AC-021 |
| T-008; post-implementation measurement | R-021 | AC-023 |

The design contains no remaining proposal-first decision. Future changes to scenario
meaning, mode semantics, public MCP inputs/responses, or accepted behavior return to a
human checkpoint before implementation.
