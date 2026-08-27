---
review:
  spec_hash: 57ce971eb4ea8053
  last_run: 2026-08-27
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-08-26-bash-code-graph-support-intent.md
---

# Bash Code Graph Support Design

**Date:** 2026-08-27
**Status:** draft

## 1. Goal and scope

Add an explicitly selected Bash language adapter to the existing code graph. The adapter
covers shell-only repositories and `.sh` components in mixed repositories while
preserving the current graph schema and the output of the Python, TypeScript, and
JavaScript adapters.

The supported source set is exactly files whose case-insensitive suffix is `.sh`.
Files ending in `.bash` and extensionless files, including files selected only by a
shebang, are outside this design. Bash remains opt-in through persistent
`code_graph.languages` configuration or an explicit one-shot
`wiki_code_index(languages=["bash"])` request. The default language list remains
`("python",)`.

The adapter parses source bytes with Tree-sitter. It never starts a shell, executes a
script, follows a `source` command, evaluates `eval`, expands a variable, runs a command
substitution, or invokes an external analyzer.

## 2. Architecture and components

### 2.1 Bash adapter

Add `src/iwiki_mcp/codegraph/languages/bash.py` with `BashAdapter`. It implements the
existing `LanguageAdapter` protocol:

- `language = "bash"`;
- `prefix = "sh"`;
- `extensions = (".sh",)`;
- `parse_file(source, path)` returns a `ParsedFile` from unexecuted bytes;
- `resolve_references(parsed, project_index)` returns `DECLARES` and `CALLS`
  relations through the existing resolver contracts.

The module loads `tree_sitter_bash.language()` directly into `tree_sitter.Language` and
`tree_sitter.Parser`. Parser construction is lazy and cached per adapter instance, so
importing the application does not initialize a grammar. Add the packaged
`tree-sitter-bash` Python binding as a runtime dependency; parser loading does not use
`tree-sitter-language-pack` and performs no network access.

### 2.2 Composition and configuration

Extend `codegraph.application.code_graph_adapter_factories` with a Bash factory. Its
parser and grammar version strings include the installed `tree-sitter-bash` distribution
version, and its adapter version is `bash-adapter-v1`.

Add `bash` to `codegraph.config.KNOWN_LANGUAGES` and to the invalid-language error
message. No environment variable, new configuration field, or default-language change is
introduced. Existing one-shot `wiki_code_index(languages=...)` routing may select any
known language, so explicitly passing `languages=["bash"]` is the transient opt-in form;
omitting `languages` continues to use persistent configuration. Existing discovery and
indexer routing use the factory's `.sh` extension; the `LanguageAdapter`,
`AdapterFactory`, snapshot, publication, and database schemas do not change.

### 2.3 Resolver and context integration

Add `"bash": frozenset({"bash"})` to `resolver.LANGUAGE_FAMILIES`. This prevents a Bash
reference from resolving to an identically named Python, TypeScript, or JavaScript
symbol. Extend the canonical context entity-ID validator to accept the `sh:` prefix.
Search filtering needs no special branch because it already validates against configured
languages and reads the language stored on graph rows.

## 3. File and symbol identity

Every indexed Bash file is module-backed. For `scripts/lib.sh`:

- `module_key` is `scripts/lib.sh`;
- `module_local_name` is `lib`;
- `module_qualified_name` is `scripts.lib`;
- file, module, symbol, and relation IDs use the `sh` prefix.

Both `foo() { ...; }` and `function foo { ...; }` Tree-sitter
`function_definition` nodes produce `SymbolRecord` values with `kind = "function"`.
The function name is read only from the node's syntactic `name` field. The public
qualified name is `<module_qualified_name>.<function_name>`. Lines are one-based, byte
ranges come directly from the Tree-sitter node, `content_hash` covers the declaration
bytes, and metadata contains only normalized structural fields required by existing
resolution, such as the module name. It never contains source text.

Bash can contain repeated definitions of one function name. The adapter retains every
occurrence with the same public qualified name and a distinct deterministic symbol ID.
Singleton identities use the normal function identity; colliding identities add the
declaration start byte to the private normalized-signature input used by `symbol_id`.
The public signature remains `None`. This keeps duplicate candidates visible and lets
the existing resolver mark their calls ambiguous instead of selecting a declaration.

## 4. Static extraction

The adapter walks the Tree-sitter syntax tree and extracts only these records:

1. One file and module record for each discovered `.sh` file.
2. One function symbol for each valid `function_definition` outside a syntax-error
   range.
3. One `CALLS` reference for each `command` whose `command_name` is a literal Bash word.

A call reference uses the command-name range, not the entire command. If that command is
inside a function declaration, `source_symbol_id` is the smallest enclosing function;
otherwise `source_module_id` is the file module. Assignment prefixes and command
arguments do not alter this attribution.

The outer command is omitted when its command name contains an expansion, command
substitution, or another non-literal node. Literal commands parsed inside a command
substitution remain independently eligible because Tree-sitter exposes them as separate
`command` nodes; parsing them does not execute the substitution. A literal external
command or builtin is retained as an unresolved `CALLS` reference unless excluded below.

The commands `source` and `.` are recognized and excluded from `CALLS`. They produce no
`IMPORTS` reference and provide no private cross-file resolution evidence. This is
required because Bash resolves a relative source operand from the process working
directory, which the static index does not know. Arguments to `source`, `.`, `eval`, or
any other command are never interpreted or expanded by the adapter.

## 5. Call resolution

Each literal command name initially becomes a `ReferenceRecord` with
`relation_type = "CALLS"`. The adapter qualifies a candidate against its own file module
only when that file contains at least one function with the same local name.

Resolution has three observable outcomes:

- exactly one same-file function candidate: a resolved `CALLS` relation targets that
  function;
- two or more same-file candidates: the existing resolver emits ambiguous relations and
  never chooses one as resolved;
- no same-file candidate: one unresolved relation retains the literal command name as
  `target_reference`.

Cross-file calls never resolve in this version, even when another `.sh` file contains a
matching function or the source file contains a `source`/`.` command. A relation denotes
syntax-backed graph reference evidence; it does not claim that Bash runtime control flow
will execute the command or that a function is defined at that instant.

No `IMPORTS` relation is emitted. The current schema requires import binding fields, but
Bash `source` has no equivalent binding. Synthesizing one or changing the schema would
violate the approved scope.

## 6. Error handling and security boundary

Tree-sitter may return a tree containing `ERROR` or missing nodes. The adapter always
returns the file/module record, adds the existing `parse_error` warning once for that
file, and suppresses a function or call whose byte range intersects an error or missing
range. Valid records outside those ranges remain available. Invalid input types and
parser initialization failures surface through the existing indexer error path; there is
no fallback parser or declaration-only mode.

Static parsing is the security boundary. Test fixtures may contain commands such as
`touch`, `eval`, `source`, and command substitutions, but the adapter treats them only as
bytes and syntax nodes. Graph metadata may contain normalized identifiers, paths,
hashes, ranges, and warning codes; it must not contain the source file body or command
argument text. Existing `wiki_code_context(include_source=true)` behavior is unchanged:
when explicitly requested, context may read bounded source from the checked-out project,
but source is not stored in graph records or metadata.

## 7. Requirements and acceptance criteria

### R1 — Explicit `.sh` discovery

**User task:** index projects implemented through Bash scripts and Bash components in
mixed repositories without scanning Bash by default.

**Requirement:** `bash` is a valid persistently configured or explicitly one-shot-selected
language and claims only `.sh` files.

**Acceptance:** a Bash-only fixture configured with `languages = ("bash",)` produces
only `language = "bash"` file rows; `wiki_code_index(languages=["bash"])` passes Bash as
the explicit one-shot selection; the same fixture with neither persistent nor one-shot
Bash selection produces no `.sh` rows; `.bash` and extensionless fixture files are
excluded.

### R2 — Bash functions are searchable

**User task:** expose the structure of Bash projects through the code graph.

**Requirement:** both supported function declaration forms produce stable function
symbols with correct identifiers and source ranges.

**Acceptance:** focused adapter tests assert names, qualified names, `sh:` IDs, one-based
lines, exact byte ranges, and source-free metadata; an indexed fixture returns the
functions from `CodeGraphQuery.search` with `languages = ["bash"]`.

### R3 — Conservative call evidence

**User task:** represent statically provable function calls and never guess ambiguous or
dynamic targets.

**Requirement:** literal command names produce `CALLS`; only a unique same-file function
candidate resolves. Duplicate candidates are ambiguous, external names are unresolved,
dynamic command names are omitted, and `source`/`.` never enable cross-file resolution.

**Acceptance:** adapter and resolver tests cover resolved, ambiguous, unresolved,
dynamic, nested-command-substitution, and source-command cases. Every relation has the
expected source owner, range, target, and resolution state.

### R4 — Mixed-language isolation

**User task:** support Bash as additional functionality in repositories using other
languages.

**Requirement:** one snapshot can contain Python and Bash without identifier collisions
or cross-language call resolution.

**Acceptance:** a mixed fixture builds and searches both languages; Bash and Python rows
have distinct prefixes; a shared local name does not create a cross-language relation;
Python rows from the mixed build equal a Python-only baseline byte for byte.

### R5 — Static-only safety

**User task:** indexing must not run project source, and source bodies must not enter the
graph.

**Requirement:** Bash parsing uses only source bytes and Tree-sitter nodes. No shell,
subprocess, `source`, `eval`, expansion, or analyzer execution occurs.

**Acceptance:** an integration fixture contains commands that would create a sentinel
file if executed; after indexing the sentinel does not exist. Serialized file, symbol,
reference, relation, warning, and metadata values contain no fixture source body or
command arguments.

### R6 — Existing-language compatibility

**User task:** add Bash without degrading current graph support.

**Requirement:** Python, TypeScript, and JavaScript adapters, records, defaults, schema,
and publication contracts remain unchanged.

**Acceptance:** existing golden and mixed-language regression tests pass unchanged; a
build with Bash neither configured nor one-shot-selected has the same rows as the
pre-Bash factory set; the complete test suite passes. Repository-wide lint is run as a
diagnostic. Every changed Python path must pass lint; any remaining diagnostic must be
confined to a path byte-identical to the diff base and recorded as pre-existing evidence.

### R7 — Query and context compatibility

**User task:** explore Bash records through the same graph APIs as other languages.

**Requirement:** query validation accepts configured Bash filters and context validation
accepts canonical `sh:file`, `sh:module`, and `sh:symbol` IDs.

**Acceptance:** search returns Bash file/function records, and context seeded by a Bash
symbol returns its `DECLARES`/`CALLS` neighborhood with source excluded by default.

## 8. Test strategy

Focused tests belong beside existing code-graph adapter, configuration, resolver,
context, and mixed-indexing tests. Add a small fixture tree under
`tests/fixtures/codegraph/` for shell-only and Python-plus-Bash scenarios. Do not add a
new test harness.

The implementation is accepted only after all of these checks succeed:

1. Bash adapter and resolver tests exercise both function forms, nested attribution,
   duplicate names, literal and dynamic commands, syntax errors, `source`/`.`, and exact
   ranges.
2. Configuration, discovery, query, context, snapshot-version, and mixed-language tests
   exercise production factories rather than test-only wiring.
3. The sentinel fixture proves indexing does not execute Bash syntax.
4. Existing Python, TypeScript, and JavaScript golden/baseline tests remain unchanged and
   pass.
5. `uv run pytest -q` exits successfully. `uv run flake8 src tests` is inspected in
   full, every changed Python path passes flake8, and any non-zero repository-wide result
   is accepted only when all reported paths are unchanged from the diff base.

## 9. Documentation and versioning

Update `README.md` and `docs/README.ru.md` when implementation lands. They must list
`bash`, show both persistent and one-shot explicit opt-in, describe `.sh` scope,
same-file-only call resolution, unresolved external commands, ignored source imports,
and the static-only security boundary. Update the bound iwiki code-graph configuration
and extraction documentation before result reconciliation.

Bump the project patch version and regenerate `uv.lock` through the normal dependency
workflow. The version and lockfile changes are part of this feature, not a separate
schema or migration.

## 10. Non-goals and risks

Non-goals are `.bash`, shebang-only discovery, shell dialects other than Bash, variables,
parameters, aliases, arrays, sourced-file imports, cross-file Bash calls, control-flow
analysis, runtime definition-order proof, command argument capture, external command
resolution, ShellCheck, shell execution, and graph schema changes.

The main residual risk is that a syntax-backed call relation describes a lexical command
reference, not guaranteed runtime availability or execution. The design exposes this
limit explicitly and resolves only a unique same-file candidate. Dynamic, cross-file,
and duplicate-name cases cannot silently become resolved edges.
