# Design: JavaScript code-graph support

**Date:** 2026-08-20
**Intent:** `docs/superpowers/intents/2026-08-20-codegraph-javascript-support-intent.md` (approved)
**Topic:** `codegraph-javascript-support`

## Acceptance (from intent)

Desired Outcomes, carried verbatim:

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

Done when (verbatim): on a fixture repository containing Python, TypeScript, and
JavaScript sources, one `wiki_code_index` run produces `language = "javascript"` records
for `.js`/`.jsx`/`.mjs`/`.cjs`; `wiki_code_search` with `languages = ["javascript"]`
returns those symbols with correct ranges; `wiki_code_context` returns their
`IMPORTS`/`CALLS`/`INHERITS` relations; and a byte-level comparison shows the Python and
TypeScript records from the same run are unchanged against a pre-change baseline.

## 1. Current state

`codegraph/languages/` holds two adapters. `python.py` extracts `IMPORTS`, `INHERITS`,
and `CALLS` references with alias tracking. `typescript.py` extracts declarations plus
`IMPORTS` (always `resolution_hint="unresolved"`) and `INHERITS`; it emits no `CALLS`.
`server.py::_code_graph_adapter_factories` maps a language name to an `AdapterFactory`
carrying `extensions`, `parser_version`, `grammar_version`, and `adapter_version`.
`indexer.py` routes each discovered file to the first adapter whose `extensions` match,
then builds a single project-wide `SymbolIndex` across all languages
(`indexer.py:854`) before calling every adapter's `resolve_references`.
`config.py` gates the configured language list against
`KNOWN_LANGUAGES = {"python", "typescript"}`.

JavaScript is unsupported: `.js`/`.jsx`/`.mjs`/`.cjs` files are neither discovered nor
parseable, and `code_graph.languages = ["javascript"]` is rejected by config validation.

## 2. Module layout

### R2.1 — Shared ECMAScript core

A new module `src/iwiki_mcp/codegraph/languages/_ecmascript.py` holds everything the two
ECMAScript adapters share:

- the Tree-sitter parser cache and grammar loading (`_PARSERS`, `get_parser`), including
  the `tree_sitter_language_pack` → `tree_sitter_typescript` fallback chain that
  `typescript.py` uses today;
- source helpers `text`, `relative_path`, `param_signature`, `return_type_signature`,
  `visibility`;
- heritage handling: the `PendingHeritage` record, `pending_heritage_references`,
  `heritage_scope_candidates`, `resolve_heritage_references`;
- ESM import binding extraction (`import_bindings`) and ESM `IMPORTS` reference
  extraction;
- the generic declaration walker `extract_symbols(source, root, *, profile, …)`;
- symbol deduplication (`dedupe_symbols`) returning the deduped list plus the
  `duplicate_symbol_identity` warning.

### R2.2 — Language profile

`extract_symbols` is parameterized by a frozen `LanguageProfile` dataclass:

| Field | Type | TypeScript | JavaScript |
|---|---|---|---|
| `language` | `str` | `"typescript"` | `"javascript"` |
| `prefix` | `str` | `"ts"` | `"js"` |
| `kind_by_node` | `Mapping[str, str]` | `{type_alias_declaration: type_alias, enum_declaration: enum}` | `{}` |
| `handles_interface` | `bool` | `True` | `False` |
| `handles_namespace` | `bool` | `True` | `False` |
| `declaration_hooks` | `tuple[Callable, ...]` | `()` | object-literal hook, prototype hook |

A `declaration_hook` receives the walker's context (node, `owner_qualified`, the
`make_symbol` callable, and the symbols collected so far) and may add symbols. Hooks run
only for node types the base walker does not already claim, so a hook can never change
which symbols the base walker itself emits.

### R2.3 — Adapter modules

`typescript.py` keeps the public name `TypeScriptAdapter` with its current `language`,
`prefix`, `extensions`, constructor signature, tsc-boost probing, and warnings; its body
delegates to `_ecmascript`. `javascript.py` adds `JavaScriptAdapter`.

### R2.4 — Behavioural invariant for TypeScript

The refactor is output-preserving: for every TypeScript fixture, `parse_file` and
`resolve_references` produce byte-identical `FileRecord`, `SymbolRecord`,
`ReferenceRecord`, `RelationRecord`, and warning tuples before and after the change.
`typescript.py` gains no new behaviour in this work.

## 3. JavaScript adapter identity

### R3.1 — Language identity

`language = "javascript"`, `prefix = "js"`, `extensions = (".js", ".jsx", ".mjs", ".cjs")`.
No file extension is claimed by two adapters; `.ts`/`.tsx` remain TypeScript's.

### R3.2 — Grammar

All four JavaScript extensions parse with the `tsx` grammar, which is a syntactic
superset of JavaScript including JSX. No new runtime dependency is added;
`parser_version` reuses the `tree-sitter-typescript:<version>` string, and
`adapter_version` is `"javascript-adapter-v1"`.

### R3.3 — Module identity

Every JavaScript file is a module: `module_qualified_name` is the dotted join of the
POSIX parent parts and the file stem (`src/util.js` → `src.util`), `module_local_name` is
the stem, and `module_id` is always populated. This differs deliberately from
`typescript.py`, which requires a top-level `import`/`export`; without it a CommonJS file
that only assigns `module.exports` could never be the resolved target of an import.

## 4. Symbol extraction

### R4.1 — Base declarations

From the shared walker, unchanged in meaning from TypeScript: `class`, `method`,
`function` / `async_function` (function declarations, and `const`/`let`/`var`
declarators initialized with an arrow function or function expression). Signatures use
the parameter text; JavaScript has no return-type node, so the return-type part is always
empty. Visibility is `private` for names starting with `_` or `#`, else `public`.

### R4.2 — Object-literal methods

For a variable declarator whose initializer is an `object` node, each property that is a
shorthand method (`get() {}`), or a `pair` whose value is a function expression or arrow
function, becomes a `method` symbol scoped under the declarator's qualified name
(`src.api.get`). Keys are accepted only as `property_identifier` or a plain string
literal; computed keys (`[k]: fn`) and spread properties are skipped.

### R4.3 — Prototype methods

An assignment `C.prototype.m = function () {}` or `C.prototype.m = () => {}` becomes a
`method` symbol qualified under `C`'s qualified name — **only** when `C` resolves to a
symbol already extracted from the same file (a `class` or `function`). Otherwise the
assignment is skipped. `C.prototype = {…}` wholesale replacement is out of scope.

### R4.4 — Deduplication

Colliding `symbol_id`s keep the last declaration by start byte and raise the
`duplicate_symbol_identity` warning, matching `typescript.py`.

## 5. Reference extraction

### R5.1 — ESM imports

`import` statements produce one `IMPORTS` reference per binding, reusing the shared
binding extraction: default import and named import → `implicit_binding`; aliased named
import and `* as ns` → `explicit_alias`. A side-effect-only `import "./m"` produces one
reference with no binding. `export … from "./m"` re-exports are out of scope for this
work.

### R5.2 — CommonJS requires

A variable declarator whose initializer is a call to `require` with a single string
literal argument produces `IMPORTS` references:

- `const x = require("m")` → one reference, binding `x`, kind `implicit_binding`;
- `const { a, b: c } = require("m")` → one reference per destructured binding, `a` as
  `implicit_binding`, `c` as `explicit_alias`;
- `require(expr)` with a non-literal argument, and a bare `require("m")` statement with no
  binding, produce no reference.

`module.exports` / `exports.x` assignments produce no relation: the schema has no export
relation type.

### R5.3 — Specifier resolution

Reference extraction stores the raw specifier as `target_reference`. Resolution happens in
`JavaScriptAdapter.resolve_references`, which has the project `SymbolIndex`:

- A relative specifier (`./x`, `../y/z`) is normalized against the importing file's
  parent directory into a POSIX path, then a dotted candidate. Candidates are probed in
  order: the dotted path itself, then the dotted path plus `.index`. A candidate present
  in `index.modules_by_qualified` becomes the reference's `target_reference` with
  `resolution_hint` cleared, `resolution_scope = "project"` and
  `target_kind_hint = "module"`, so the language-neutral resolver produces a typed module
  edge — including to a TypeScript module, since the index spans all languages.
- A specifier that escapes the repository root, a bare specifier (`react`), a
  subpath-import (`#alias`), a URL, or a Node builtin (`node:fs`) keeps the raw
  specifier, `resolution_hint = "unresolved"` — the same shape TypeScript emits today.

### R5.4 — Inheritance

`class X extends Y` and `class X extends ns.Y` produce an `INHERITS` reference through the
shared heritage machinery: the target resolves against the innermost matching enclosing
scope in the file, falling back to the module scope. When `Y` is an import or require
binding, the target is the imported module's dotted candidate plus the imported name, so
the project resolver can bind it cross-file.

### R5.5 — Calls

`call_expression` and `new_expression` produce `CALLS` references when the callee is:

- an `identifier` — target is an import/require alias expansion when the name is bound by
  an import in this file, else the innermost enclosing file scope match, else the raw name
  with `resolution_hint = "unresolved"`;
- a non-computed `member_expression` whose object chain is made of identifiers and
  `property_identifier`s (`a.b.c()`) — target is the dotted chain, alias-expanded at its
  head under the same rule.

A computed callee (`obj[k]()`), a callee that is itself a call (`f()()`), a tagged
template, and an optional call on a computed member are not extracted. `require(...)`
calls already handled by R5.2 produce no `CALLS` reference.

### R5.6 — Reference source attribution

Each reference carries `source_symbol_id` = the innermost extracted symbol whose byte
range contains the reference node, or `None` at module level; `source_file_id` and
`source_module_id` are always set. This is what makes `wiki_code_context` able to answer
"what does this function call".

### R5.7 — Trust rule

No relation is emitted on a guess. Dynamic `require`, computed member access, runtime
prototype juggling beyond R4.3, re-export forwarding, and bundler/tsconfig path aliases
stay either absent or explicitly `unresolved` with the raw text preserved.

## 6. Configuration and server wiring

### R6.1 — Known languages

`codegraph/config.py`: `KNOWN_LANGUAGES = frozenset({"python", "typescript", "javascript"})`
and the validation message becomes
`code_graph.languages supports only python, typescript, javascript`.

### R6.2 — Adapter factory

`server.py::_code_graph_adapter_factories` registers a `"javascript"` factory with the
extensions from R3.1, `parser_version`/`grammar_version` from R3.2, and
`adapter_version = "javascript-adapter-v1"`. `_code_graph_configured_languages`,
`runtime.py`, and `query.py` need no change — they read the configured list.

### R6.3 — Snapshot invalidation

Adding a language to `code_graph.languages` changes the configured-language fingerprint,
so an existing snapshot rebuilds; no schema migration and no publication-protocol change
is involved.

## 7. Testing

### R7.1 — TypeScript regression baseline

Before the refactor, a golden fixture set is captured from the current `typescript.py`:
for each TypeScript/TSX fixture, the serialized `FileRecord`, `SymbolRecord`s,
`ReferenceRecord`s, resolved `RelationRecord`s, and warnings. A test replays the fixtures
through the refactored adapter and asserts equality against the stored golden data.

### R7.2 — JavaScript adapter unit tests

`tests/codegraph/test_javascript_adapter.py` covers: base declarations (R4.1), object
literals (R4.2), prototype methods including the skipped unresolvable case (R4.3),
duplicate identity warning (R4.4), ESM imports with every binding form (R5.1), CommonJS
requires including the skipped dynamic form (R5.2), relative-specifier resolution
including the `.index` candidate and the unresolved bare specifier (R5.3), inheritance
across an import (R5.4), calls including the skipped computed forms (R5.5), source
attribution (R5.6), and JSX parsing without error.

### R7.3 — Mixed-language indexing

`tests/codegraph/test_mixed_language_indexing.py` gains JavaScript sources: one build over
Python + TypeScript + JavaScript produces distinct `language` values, no `symbol_id` or
`relation_id` collision, and a resolved JS→TS import edge.

### R7.4 — Config and server tests

`code_graph.languages = ["javascript"]` validates; an unknown language still fails with
the updated message; `wiki_code_index` / `wiki_code_search` accept the `javascript`
language filter.

### R7.5 — Suite health

`uv run pytest -q` passes and `uv run flake8 src tests` is clean.

## 8. Documentation and release

`docs/architecture.md`, `README.md`, and `docs/README.ru.md` gain JavaScript in the
code-graph language list with its extension set and its stated limits (no type inference,
no bundler alias resolution). The bound wiki page covering code-graph languages is updated
through the iwiki MCP tools. `pyproject.toml` gets a patch version bump.

## 9. Out of scope

- Type inference of any kind; no `node`, `tsc`, bundler, or `node_modules` involvement.
- `export … from` re-export forwarding and `export` relation types.
- tsconfig/jsconfig path aliases, `package.json` `imports`/`exports` maps.
- Flow-annotated JavaScript, Vue/Svelte single-file components, `.d.ts` handling.
- Any behavioural change to the Python or TypeScript adapters.
