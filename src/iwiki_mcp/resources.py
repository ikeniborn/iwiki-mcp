"""Page-authoring rules, exposed as an MCP resource the agent fetches before
writing. Ported from the iwiki-ingest skill's section-formation rules.
"""

AUTHORING_RULES: str = """\
# iwiki page authoring rules

- Use **only `##`** for sections -- never `###` or deeper. Deeper headings are not
  indexed as separate units; flatten them into the `##` section's prose.
- Put **no content before the first `##`** except the frontmatter block and a
  single `# Title` H1.
- Lead with `# Title`, then the page's `##` sections directly. Do NOT write a
  `## Overview` section -- the article summary is the frontmatter `description`.
- One `##` section per concept; lead each section with a <=250-char paragraph
  stating what it covers and why it matters (intent, not just mechanics).
- Prefer a standard section name where one fits: `## Purpose`, `## Interface`,
  `## API`, `## Dependencies`, `## Data flow`, `## Errors`, `## Usage`.
- Wrap every code symbol (function, path, flag, command, config key) in backticks.
- Cross-link within the same domain with `[Heading](<type>/<slug>.md#heading)`.
  Cross domains only with `[Heading](iwiki://<domain>/<page-id>#<anchor>)`.
  Never link to `index.md` or `log.md`: generated artifacts are not graph pages.
- `wiki_update_page(..., new_heading=...)` and a moving `wiki_apply_okf` rewrite exact
  incoming links automatically only when all visible referrers are writable. A visible
  read-only referrer blocks before mutation; hidden domains are not inspected or rewritten.
- Write accurate English prose grounded in the real source; do not invent.

## Search and maintenance tools

- Read search exposes exactly `hybrid`, `lexical`, and `semantic`. The omitted mode
  comes from `IWIKI_SEARCH_MODE` (default `hybrid`); an explicit `wiki_search.mode`
  wins. `vector` is an internal embedding term, not a public mode.
- `IWIKI_RERANK_MODEL` optionally reranks the fused candidate pool through the shared
  LiteLLM URL and key. Provider failures are fail-soft and return sanitized metadata.

## Existing page updates

- Use `wiki_write_page` for a new page and `wiki_update_page` for an existing page:
  a section-only update requires both `heading` and `new_body`; a code-only update uses
  `code`; and a combined update atomically applies both. A code-only update preserves the
  page body byte-for-byte. Code-only response omits `heading` and adds no fields; section
  and combined responses retain `heading`. `new_heading` remains
  available only with the section update. Use `wiki_delete_page` only when a source was
  removed. Run `wiki_lint` after changes; use `wiki_remediation_plan` to inspect grouped
  repair actions.
- The public root schema stays a plain object with `domain` and `slug` root-required:
  client tool validation rejects a root combinator, so a root `anyOf` would drop the whole
  tool. Runtime validation enforces the mutually exclusive operations instead and rejects
  partial, no-op, or unsafe selectors before mutation.
- Optional code selectors use only nested `code.symbols`, `code.files`, and
  `code.source_globs`. Each symbol item contains exactly `qualified_name`; file and glob
  items are project-relative strings. `modules`, `module_id`, `aliases`, and import bindings
  are forbidden selectors. A nonempty valid `code` mapping completely replaces the selectors.
  An empty `{}` mapping or all-empty lists clears them. Omit `code` (or pass `null`) to
  preserve selectors during a section update. These fields remain human-authored; derived
  links never rewrite them.
- `wiki_read_page(..., heading=...)` returns only that one `##` section (with its
  `section_hash`) instead of the whole page. `wiki_insert_section`, `wiki_delete_section`,
  and `wiki_move_section` add, remove, or reorder one `##` section without rewriting the
  rest of the page. `wiki_update_page`, `wiki_delete_section`, and `wiki_move_section` all
  accept `expected_section_hash` for optimistic concurrency: a stale hash is rejected with
  `section_conflict` instead of silently overwriting a concurrent edit.
- PostgreSQL reads include a numeric `revision`. Pass it as `expected_revision` to
  PostgreSQL update/delete calls; omission or a stale value leaves the page unchanged. A
  selector update uses the current `expected_revision` CAS in one revision and transaction;
  unchanged chunks reuse embeddings. Git keeps its existing freshness and strict-spec
  transaction behavior, then reindexes, commits, and refreshes the graph once. Republish
  makes Code-graph Wiki links current.
- Code graph, remediation, OKF migration/apply/export, sync, and domain creation tools
  require Git storage. PostgreSQL domains are provisioned by an administrator.

## OKF frontmatter

- Every page carries a YAML frontmatter block above the `# Title` H1. The write
  tools fill it. Fields: `type` (required), `title`, `description`, `resource`,
  `tags`, `status`, `timestamp`.
- `description` is the authored article summary and the single source of it. It is
  indexed as its own **summary-level vector that seeds retrieval** (two-level:
  summary seed -> graph-expanded pool -> section vectors ranked inside it), NOT
  prefixed onto section vectors. Write it rich: include `Covers:` and `Terms:`
  keyword lines so the summary matches broad queries. There is no `## Overview`.
- `type` is an OPEN vocabulary. Prefer a common value -- `architecture`, `api`,
  `guide`, `reference`, `runbook`, `concept` (default) -- but any lower-case value
  is allowed (e.g. `person`, `team`); an off-list value is only advised, not rejected.
- `status` is one of `stub` (default), `developing`, `stable`, `deprecated`.
- `tags` are lowercase kebab-case, <=5 per page; reuse an existing domain tag first.
- Put relationship links in two reserved sections, `## Outgoing links` (Markdown links
  to other pages) and `## External links` (bare URLs). Both are EXCLUDED from search
  indexing but still feed the link graph (`wiki_related`, `lint`).
- The slugs `index` and `log` are reserved: `index.md` / `log.md` are **export-only**
  OKF navigation/history files, generated by `wiki_export_okf` (not refreshed on every
  write). The write tools reject these slugs.
- The base-local `.iwiki/graph.sqlite3` is a rebuildable SQLite graph cache, excluded
  from Git. Keep portable `index.jsonl` and `log.jsonl` at each domain root; never
  copy or edit the graph database between machines. A stale cache falls back to
  Markdown and is rebuilt locally when needed.

## GWT specifications

Ordinary Wiki pages and `type: specification` pages coexist. The `disabled`, `optional`,
and `strict` modes affect only explicit specification pages; ordinary Wiki validation,
storage, search, and lint never depend on GWT parsing or on a code graph. The semantic
tool surface is exactly `wiki_spec_search`, `wiki_spec_context`, and
`wiki_spec_resolve`.

Use one canonical TOML block in an H2 section:

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

Allowed phase roles are exact: Given roles: `event`, `state`, `fact`; When roles:
`command`, `request`, `action`; Then roles: `event`, `response`, `outcome`, `exception`.
An `exception` is exclusive and cannot coexist with another Then item.

Grammar is closed and bounded. `id` is required, contains 1-128 UTF-8 bytes, and
matches `[a-z0-9]+(?:-[a-z0-9]+)*`. `title` is required and nonblank, contains no NUL,
and is at most 250 Unicode code points. Every phase-item `name` is required and
nonblank, contains no NUL, and is at most 1,024 UTF-8 bytes. Unknown keys at the
top level, in phase items, or in bindings are invalid; malformed TOML and duplicate
TOML keys are invalid.

`given` is required and accepts 0 or more items. `when` is required and contains
exactly one item. `then` is required and contains 1 or more items. `code` is required
and contains 1 or more bindings. Every binding has relation `implements` or `verifies`;
`phase` is optional and, when present, is `given`, `when`, or `then`. Each binding has
exactly one of `symbol`, `file`, or `source_glob`. Completeness requires at least one
`implements` and one `verifies` binding.

Binding grammar is exact: relation is exactly `implements | verifies`; `phase` is
optional and exactly `given | when | then`. Every selector value is a nonempty UTF-8
string of at most 4,096 bytes with no NUL. `symbol` is a code-graph qualified-name
string, but the parser enforces only the shared selector scalar constraints and no
stricter symbol regex. `file` and `source_glob` are trimmed, safe, relative POSIX paths
or patterns with at most 256 path segments; they reject a backslash, absolute path,
Windows drive, empty segment, `.` or `..`. `file` forbids glob metacharacters `*`, `?`,
and `[`, while `source_glob` allows them. `code` is limited to at most 256 bindings.
Duplicate phase identity `(phase, role, name)` is invalid. Duplicate binding identity
`(relation, phase, selector kind, selector)` is invalid.

Mode and lint behavior is exact. Disabled mode produces no projection and no
specification findings for missing, invalid, duplicate, or incomplete specification
pages. Optional mode makes every specification finding advisory. In strict mode,
syntax (`missing_scenario` and `invalid_scenario`), `duplicate_scenario_id`, and
`incomplete_bindings` findings are blocking only for future mutations of the reported
explicit specification page. Projection and resolution findings remain advisory, and
ordinary Wiki pages remain unaffected in every mode.

`wiki_status` reports `domain`, `mode`, `source`, `projection_state`, `scenarios`, and
`bindings`. Source is exactly
`project | hosted_default | hosted_override | built_in_default`; projection state is
exactly `disabled | absent | ready | stale | failed`.

`wiki_lint` reports the full specification taxonomy: `missing_scenario`,
`invalid_scenario`, `duplicate_scenario_id`, `incomplete_bindings`, `projection_stale`,
`projection_failed`, `binding_unresolved`, `binding_ambiguous`,
`resolution_not_checked`, `resolution_stale_spec`, `resolution_stale_graph`, and
`graph_unavailable`. It remains read-only and always returns the complete ordinary Wiki
report.

1. Create or update a scenario for a new observable domain behavior, public contract,
   bug reproduction, or business invariant. Do not require one for formatting,
   mechanical refactoring with unchanged behavior, or ordinary Wiki maintenance.
2. Write Given as prior domain facts, events, or state; When as one observable trigger;
   and Then as public events, responses, outcomes, or an exclusive exception. Do not
   encode internal method steps or database state as expected behavior.
3. Keep the existing scenario ID when wording, page location, implementation, or test
   location changes but observable behavior remains the same. Propose a behavioral
   contract change before changing Given/When/Then meaning.
4. Add at least one `implements` and one `verifies` selector. Planned unresolved targets
   are acceptable while implementing specification-first behavior.
5. Write or update the executable test before or with implementation. Run the focused
   test and relevant regression suite; record command, exit status, and repository
   revision in the task ledger.
6. Call `wiki_spec_context` before changing an existing specification. When a ready
   graph exists, call `wiki_spec_resolve` after code or test changes. Treat ambiguous,
   stale, or unresolved evidence as a maintenance finding, not as permission to guess.
7. When the graph is absent or unusable, continue Wiki and GWT work, preserve declared
   selectors, record graph-unavailable evidence, and verify through repository search
   and executable tests. Never block ordinary Wiki work on graph recovery.
8. Review the scenario, executable test, implementation bindings, and test evidence as
   one coherent unit before reporting the change complete.
"""
