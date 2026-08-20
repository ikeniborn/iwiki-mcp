# Intent: codegraph-javascript-support

**Date:** 2026-08-20
**Status:** approved

## Objective

The code graph indexes Python and TypeScript, but plain JavaScript (`.js`, `.jsx`,
`.mjs`, `.cjs`) is invisible: agents fall back to grep for symbol lookup, call graphs,
and change-impact analysis in JS sources. The TypeScript design explicitly deferred
untyped JavaScript (`docs/superpowers/specs/2026-08-18-codegraph-typescript-support-design.md:224`),
and the original code-graph technical requirements planned a `languages/javascript.py`
adapter that was never built. This closes both: the coverage gap for JS repositories and
mixed repositories, and the last language on the adapter roadmap.

## Desired Outcomes

- `wiki_code_index` with `code_graph.languages = ["javascript"]` builds a snapshot from
  `.js`, `.jsx`, `.mjs`, and `.cjs` files, and the resulting records carry
  `language = "javascript"`.
- `wiki_code_search` finds JavaScript functions, classes, and methods with accurate paths
  and line/byte ranges, and the `languages = ["javascript"]` filter selects exactly those
  records.
- `wiki_code_context` returns JavaScript relations — `DECLARES`, `IMPORTS`, `CALLS`,
  `INHERITS` — for ESM `import` and CommonJS `require`, including JS↔TS cross-file edges
  where the target resolves.
- A mixed repository (Python + TypeScript + JavaScript) builds in a single run and
  publishes without identifier collisions; languages stay distinguishable in every result.

## Health Metrics

- Python and TypeScript graphs stay bit-for-bit identical: same `file_id` / `module_id` /
  `symbol_id` / `relation_id`, same symbol kinds, same relations. The existing test suite
  stays green.
- Build time stays within `max_rebuild_seconds`; the JavaScript adapter adds no subprocess
  spawns and no network calls during indexing.
- No new runtime dependency in `pyproject.toml`.
- Schema stability: no schema-v2 migration, no change to the publication protocol; already
  published snapshots keep reading unchanged.

## Strategic Context

- Interacts with: `codegraph/config.py` (`KNOWN_LANGUAGES`, `languages` validation),
  `server.py::_code_graph_adapter_factories` (adapter registry, extension routing,
  grammar/parser/adapter versions), `codegraph/languages/typescript.py` (shared
  declaration-extraction helpers and the Tree-sitter parser cache),
  `codegraph/resolver.py` (language-neutral reference resolution),
  `codegraph/indexer.py` / `discovery.py` (file selection per language),
  `codegraph/store.py` and the publication path (language-tagged records),
  and the `wiki_code_index` / `wiki_code_search` / `wiki_code_context` tool surface.
- Priority trade-off: **trust**. Dynamic `require`, computed member access, and
  prototype patching must stay unresolved rather than being guessed.

## Constraints

### Steering (behavioral guidance)

- Reuse the TypeScript adapter's declaration extraction rather than duplicating it; the
  TSX grammar parses ESM, CommonJS, JSX, class fields, generators, and optional chaining
  without parse errors, verified locally.
- Prefer emitting no relation over emitting a speculative one; carry the existing
  `resolution_hint` / `resolution_scope` conventions from `concept/code-graph-identities`.
- Match surrounding style; keep `flake8` clean at max-line-length 100.

### Hard (architectural enforcement)

- No code execution: declarations and references come only from Tree-sitter parsing of
  source bytes. No `node`, no bundler, no `node_modules` traversal, no network.
- Adapter identity is `language = "javascript"`, `prefix = "js"` — never merged into the
  TypeScript identity.
- File contract: `.js`, `.jsx`, `.mjs`, `.cjs` route to the JavaScript adapter; `.ts` and
  `.tsx` stay with the TypeScript adapter. No file is parsed by two adapters.
- No change to the persisted schema or the publication protocol, and no behavioral change
  to the TypeScript adapter.

## Autonomy Zones

- Full autonomy (reversible, low risk): adapter implementation, `KNOWN_LANGUAGES` and
  config validation message, server adapter-factory registration, tests, wiki/README
  updates, version bump.
- Guarded (log + confidence threshold): shared-helper refactors inside
  `languages/typescript.py` that keep TypeScript output identical — permitted only with a
  regression test proving the TypeScript graph is unchanged.
- Proposal-first (needs approval): any new runtime dependency, any schema or publication
  change, any behavioral change to the TypeScript adapter.
- No autonomy (human only): merging to `master` outside a pull request.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules

- Halt if: the implementation would require a new runtime dependency, a schema/publication
  change, or a change to TypeScript adapter output.
- Escalate if: JavaScript indexing cannot reach the desired outcomes without speculative
  resolution heuristics that conflict with the trust priority.
- Done when: on a fixture repository containing Python, TypeScript, and JavaScript
  sources, one `wiki_code_index` run produces `language = "javascript"` records for
  `.js`/`.jsx`/`.mjs`/`.cjs`; `wiki_code_search` with `languages = ["javascript"]` returns
  those symbols with correct ranges; `wiki_code_context` returns their `IMPORTS`/`CALLS`/
  `INHERITS` relations; and a byte-level comparison shows the Python and TypeScript
  records from the same run are unchanged against a pre-change baseline.
