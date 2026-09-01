---
type: specification
title: Code selector frontmatter update gap
review:
  spec_hash: e210981703a54e45
  last_run: 2026-09-01
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-08-31-code-selector-frontmatter-update-gap-intent.md
---
# Design: code-selector-frontmatter-update-gap

**Date:** 2026-09-01
**Status:** approved
**Intent:** [code-selector-frontmatter-update-gap intent](../intents/2026-08-31-code-selector-frontmatter-update-gap-intent.md)

## 1. Scope and acceptance

Extend the existing `wiki_update_page` tool so an existing page can set, replace, or
clear its code-graph selectors without a delete-and-rewrite cycle. The extension also
supports one atomic request that changes both a `##` section and the selector mapping.
No MCP tool is added or renamed.

The design is complete when all requirements R1-R16 below pass on the applicable Git
and PostgreSQL paths, the registered tool count remains 35, the full test suite passes,
and the checked repository and iwiki documentation describe the same contract.

## 2. Public request and response contract

### 2.1 Function signature

R1. The public function keeps `domain` and `slug` required, makes `heading` and
`new_body` optional with `None` defaults, and adds `code` as the final parameter:

```python
def wiki_update_page(
    domain: str,
    slug: str,
    heading: str | None = None,
    new_body: str | None = None,
    source: str | None = None,
    description: str | None = None,
    status: str | None = None,
    new_heading: str | None = None,
    expected_revision: _ExpectedRevision = None,
    expected_section_hash: str | None = None,
    code: dict | None = None,
) -> dict:
```

Acceptance: every existing positional and keyword section update retains its call
shape and result behavior; `inspect.signature` shows `code` last.

### 2.2 Valid operation modes

R2. A section update supplies both `heading` and `new_body`. Supplying only one is
invalid. Empty strings count as supplied values and continue into the existing section
validation, preserving its established error behavior.

R3. A code-only update supplies a non-null `code` object and omits `heading` and
`new_body`. It must also omit `source`, `description`, `status`, `new_heading`, and
`expected_section_hash`; rejecting these combinations prevents the new mode from
becoming a general frontmatter-only update path.

R4. A combined update supplies `heading`, `new_body`, and a non-null `code` object. It
may use every optional parameter already valid for a section update. The section and
selector changes commit atomically as one page mutation. The user explicitly approved
this proposal-first behavior on 2026-09-01; task-history event
`f00ffb6828abbb7f` is its durable decision record.

R5. A request with neither a complete section pair nor a non-null `code` object is
invalid. Explicit `code=None` has the same selector semantics as omission: it does not
change selectors and cannot create a code-only operation.

Acceptance: invalid mode combinations return before page reads, freshness operations,
embedding, writes, indexing, or commits. Valid section-only, code-only, and combined
calls reach the existing backend mutation pipeline exactly once.

### 2.3 Selector replacement semantics

R6. `validate_code_mapping` remains the sole selector-grammar validator. The accepted
keys remain `symbols`, `files`, and `source_globs`; modules, module IDs, aliases, import
bindings, unknown keys, unsafe paths, malformed mappings, and excessive selectors
remain rejected.

R7. A valid mapping containing at least one selector replaces the complete prior `code`
mapping with the caller-supplied valid keys and lists. `{}` or a mapping whose validated
selector lists are all empty removes the complete `code` key. Omitted or null `code` on
a section update preserves the current mapping.

Acceptance: set, replace, clear, and omission round trips produce the exact semantic
mapping above without changing selector vocabulary.

### 2.4 Response shape

R8. Section-only and combined success responses retain the existing normalized
`heading` field. A code-only success response contains the existing backend fields but
omits `heading`; it does not add an operation discriminator or any other response field.

Acceptance: existing response assertions remain unchanged, while code-only tests prove
that `heading` is absent. PostgreSQL retains `page`, `revision`, and `indexed_chunks`;
Git retains its current page, indexing, reuse, embedding, byte, cap, and sync fields.

## 3. Published JSON Schema

R9. The registered `wiki_update_page` input schema keeps only `domain` and `slug` in
its root `required` list and contains an explicit root `anyOf`:

```json
{
  "required": ["domain", "slug"],
  "anyOf": [
    {
      "required": ["heading", "new_body"],
      "properties": {
        "heading": {"type": "string"},
        "new_body": {"type": "string"}
      }
    },
    {
      "required": ["code"],
      "properties": {
        "code": {"type": "object"}
      }
    }
  ]
}
```

The object constraint in the second branch prevents `code: null` from satisfying the
code-only alternative. A combined request satisfies both alternatives, which is valid
under `anyOf`.

FastMCP 1.28.1 derives `Tool.parameters` separately from runtime argument validation
and exposes no public custom-input-schema argument. After normal registration, one
small server helper obtains the already registered `wiki_update_page` tool and replaces
only its `parameters` dictionary with a copied dictionary containing the explicit
`anyOf`. It does not alter `fn_metadata`, registration count, runtime coercion, or any
other tool. An exact MCP schema test is the compatibility tripwire for an SDK upgrade.

Acceptance: the MCP list-tools response exposes the structure above, all 35 tool names
are unchanged, and MCP calls that bypass client-side schema enforcement are still
rejected by R2-R7 runtime validation.

## 4. Mutation pipeline

### 4.1 Shared preparation

R10. Each storage branch performs the following side-effect-free preparation after
binding, domain validation, and write-scope authorization but before any freshness or
storage mutation:

1. Classify the request as section-only, code-only, combined, or invalid.
2. Validate non-null `code` with `validate_code_mapping` and determine whether every
   selector list is empty without otherwise normalizing the caller-supplied mapping.
3. Reject code-only use of section/frontmatter parameters listed in R3.

After preparation, the selected backend reads the page and splits it once with
`strict_code=True`. A section or combined request applies the existing section-hash
precheck, link normalization, `replace_section`, and whole-body structural validation.
A code-only request retains the returned body string without transformation. A code or
combined request replaces or removes `meta["code"]` according to R7. Existing section
metadata updates and timestamp behavior remain unchanged.

The candidate Markdown is `_fm.render(meta) + body` when metadata exists, otherwise the
body. For code-only requests, `body` is the exact string returned by `_fm.split`, so
every byte after the frontmatter delimiter is preserved.

Acceptance: a code-only update changes only rendered frontmatter, preserves body bytes,
and invokes the backend update once; a combined request produces both changes or none.

### 4.2 PostgreSQL

R11. PostgreSQL keeps the existing write-scope check and mandatory
`expected_revision`. A valid request with no revision returns
`expected_revision_required`. The page read, candidate preparation, and
`PostgresStore.update_page` call retain the existing compare-and-swap contract; a stale
revision returns `conflict` and changes neither Markdown nor derived state.

The existing store path continues to reuse unchanged chunks, increment the page
revision exactly once, replace derived records, bump the Markdown generation, and
publish the strict specification projection in the same transaction. Selector changes
therefore mark published Wiki links stale until the next code-graph snapshot
publication, without a database migration.

Acceptance: code-only and combined success advance revision by one; missing/stale
revision and invalid selectors leave the complete page and projection unchanged;
unchanged body chunks cause zero new embedding calls.

### 4.3 Git

R12. Git retains `expected_revision` as optional and ignored. After shared validation,
the existing path performs freshness handling, page read, strict frontmatter parsing,
specification preflight/transaction, file rollback on failure, domain reindex, exact
path commit, and graph refresh. A code-only request therefore creates one normal
`iwiki: update <page>` commit and runs one domain reindex.

Acceptance: set, replace, clear, and combined calls auto-commit and reindex once;
invalid inputs do neither; unchanged body chunks report `embedded == 0` and reuse the
existing records.

## 5. Failure behavior

R13. New mode-validation failures use the existing fail-soft dictionary shape and the
following exact `error` strings:

- partial section pair: `heading and new_body must be provided together`;
- neither operation present: `no update operation requested`;
- a code-only request contains a parameter reserved for section mode:
  `code-only update cannot change section metadata`.

Each includes a hint naming the valid section-only, code-only, and combined shapes.
Selector failures reuse the exact current selector error text and the hint `use only
code.symbols, code.files, and code.source_globs`. Existing page-not-found, section,
structure, source, scope, freshness, specification, `expected_revision_required`,
`section_conflict`, and `conflict` responses are not renamed or reshaped.

No validation failure may partially modify frontmatter, body, ingest logs, indexes,
specification projections, code-graph state, or Git history. Git rollback and
PostgreSQL transaction behavior remain the existing authority for failures after
candidate construction.

Acceptance: tests snapshot the page and relevant derived/commit state before each
invalid-mode, invalid-selector, stale-revision, section-conflict, and strict-spec
failure, then prove equality afterward.

## 6. Specification and code-graph integrity

R14. Because code-only updates preserve body bytes, every `iwiki-gwt` fence and its
scenario `source_hash` remain unchanged. Existing Git and PostgreSQL specification
transactions must retain scenario identities, bindings, and valid resolution-evidence
state after selector-only and combined updates whose section change does not modify a
scenario fence.

R15. After a selector update and a normal PostgreSQL code-graph snapshot
republication, derived `DOCUMENTED_BY` Wiki links for the page are nonzero and
`wiki_code_context(include_wiki=true)` returns that page. Hosted source unavailability
does not change this contract: publication is tested through the existing local/store
test harness rather than by calling hosted `wiki_code_index`.

Acceptance: focused projection tests compare scenario hashes, bindings, and evidence
before and after; publication tests assert nonzero links and hydrated Wiki context.

## 7. Components and documentation

R16. Implementation stays surgical:

- `src/iwiki_mcp/server.py` owns request classification, selector application, response
  shaping, and the one-tool schema augmentation.
- `src/iwiki_mcp/codegraph/linking.py::validate_code_mapping`, existing frontmatter and
  section helpers, Git mutation machinery, and `PostgresStore.update_page` are reused
  without grammar, migration, or public-tool expansion.
- Focused coverage belongs in `tests/test_server_update.py`,
  `tests/codegraph/test_frontmatter_roundtrip.py`, `tests/test_mcp_smoke.py`, and
  `tests/postgres/test_tool_matrix.py`, with lower-level store/publication tests only
  where required to prove embedding and Wiki-link outcomes.
- `README.md`, `docs/README.ru.md`, `docs/architecture.md`, and
  `src/iwiki_mcp/resources.py` describe the repository contract. Iwiki pages
  `architecture`, `authoring-and-linting`, and `concept/code-graph-wiki-linking`
  describe the durable architecture and authoring contract.
- This task branch already bumped the base version from `0.7.225` to `0.7.226` in
  commit `27d0e16`; all later files in the same change set share that one release bump.
  If another change lands on the target branch first, rebase and select the next free
  patch version.

Acceptance: no new production module, MCP tool, selector type, database migration, or
unrelated refactor appears in the final diff; repository docs, iwiki pages, package
metadata, lockfile, and package-version test agree.

## 8. Scenario: Update selectors without rewriting the page body

```iwiki-gwt
id = "update-existing-wiki-code-selectors"
title = "Update selectors without rewriting the page body"
given = [
  { role = "state", name = "An existing Wiki page has body content, a revision, and optional code selectors" }
]
when = { role = "request", name = "wiki_update_page receives a valid code-only selector replacement and current revision" }
then = [
  { role = "outcome", name = "The selector mapping is set, replaced, or cleared in one mutation while body bytes and specification evidence remain unchanged" }
]
code = [
  { relation = "implements", phase = "when", symbol = "iwiki_mcp.server.wiki_update_page" },
  { relation = "verifies", file = "tests/codegraph/test_frontmatter_roundtrip.py" }
]
```

## 9. Scenario: Atomically update a section and selectors

```iwiki-gwt
id = "atomically-update-wiki-section-and-selectors"
title = "Atomically update a Wiki section and selectors"
given = [
  { role = "state", name = "An existing Wiki page has a target section and a current backend concurrency token" }
]
when = { role = "request", name = "wiki_update_page receives one valid request containing the section pair and code mapping" }
then = [
  { role = "outcome", name = "The section and selector mapping both commit in one revision or commit, or neither change on failure" }
]
code = [
  { relation = "implements", phase = "when", symbol = "iwiki_mcp.server.wiki_update_page" },
  { relation = "verifies", file = "tests/test_server_update.py" }
]
```

## 10. Scenario: Reject unsafe selector updates without side effects

```iwiki-gwt
id = "reject-unsafe-wiki-selector-update"
title = "Reject unsafe Wiki selector updates without side effects"
given = [
  { role = "state", name = "An existing Wiki page and its derived state are captured before an update" }
]
when = { role = "request", name = "wiki_update_page receives an invalid selector mapping, invalid mode, stale revision, or conflicting section hash" }
then = [
  { role = "outcome", name = "The request returns its stable failure and leaves Markdown, derived state, specification evidence, and Git history unchanged" }
]
code = [
  { relation = "implements", phase = "when", symbol = "iwiki_mcp.server.wiki_update_page" },
  { relation = "verifies", file = "tests/codegraph/test_frontmatter_roundtrip.py" }
]
```

## 11. Verification matrix

The implementation plan must map every row to an executable test and expected output:

| Area | Required cases | Required evidence |
|---|---|---|
| Schema | section, code-only, combined; null and missing operations | root `anyOf`; tool count 35; MCP runtime rejection |
| Compatibility | existing positional and keyword section calls | existing tests unchanged and passing |
| Selector semantics | set, replace, clear via `{}`, clear via empty lists, omission/null | read-back mapping and unchanged omitted mapping |
| Atomicity | combined success and backend failure | one revision/commit or full equality with pre-state |
| PostgreSQL CAS | missing, current, stale revision | required error, revision +1, `conflict` without mutation |
| Section CAS | section/combined match and conflict; code-only misuse | preserved behavior and no mutation |
| Git lifecycle | success, invalid input, strict-spec failure | commit/reindex once, or neither with rollback |
| Body and embedding | code-only on multi-section page | byte-identical body and zero new embedding calls |
| Specifications | code-only and safe combined update | identical hashes, bindings, and valid evidence |
| Code graph | republish after selector update | nonzero Wiki links and hydrated Wiki context |
| Documentation | repository and iwiki contract | targeted lint clean; no stale contract text |
| Regression | full repository suite | `uv run pytest -q` exits 0 |

## 12. Out of scope

- A new MCP tool or any change to the exact 35-tool registry.
- Bulk selector mutation, selector inference, new selector keys, or relaxed selector
  validation.
- Frontmatter-only mutation for fields other than `code`.
- A database migration or change to page-level PostgreSQL CAS.
- Changing existing public error names, response fields, Git synchronization semantics,
  specification strictness, or code-graph publication limits.
- Refactoring unrelated server, storage, indexing, or documentation code.
