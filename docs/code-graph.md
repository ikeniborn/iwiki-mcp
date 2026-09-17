# Python code graph MVP

*Part of the [iwiki-mcp documentation](../README.md#documentation). Русская версия: [code-graph.ru.md](code-graph.ru.md).*

The optional code graph is a separate, local SQLite cache for the project bound to
the primary wiki domain. It indexes Python, TypeScript/TSX, JavaScript, and/or Bash
source, depending on the configured `languages`, and does not change `wiki_search` or
the Markdown/vector wiki indexes. The cache paths are derived from the wiki base and
primary domain:

```text
<IWIKI_BASE_DIR>/.iwiki/code-<primary-domain>.sqlite3
<IWIKI_BASE_DIR>/.iwiki/code-<primary-domain>.sqlite3-wal
<IWIKI_BASE_DIR>/.iwiki/code-<primary-domain>.sqlite3-shm
<IWIKI_BASE_DIR>/.iwiki/code-<primary-domain>.lock
<IWIKI_BASE_DIR>/.iwiki/code-<primary-domain>.metadata.json
```

Configure it in the bound project's `.iwiki.toml`. All values are optional;
`languages` accepts `python`, `typescript`, `javascript`, and/or `bash`. The default
is `languages = ["python"]`; this example persistently opts in to every supported
language, including Bash. `exclude` entries must be safe relative paths.

```toml
[code_graph]
enabled = true
languages = ["python", "typescript", "javascript", "bash"]
auto_rebuild = "bounded"
max_rebuild_seconds = 10
max_full_rebuild_seconds = 10
max_file_bytes = 1000000
max_total_files = 20000
include_tests = true
exclude = []
```

`max_rebuild_seconds` bounds the query-time auto-rebuild only. `max_full_rebuild_seconds`
bounds an explicit `wiki_code_index` full build and defaults to `max_rebuild_seconds` when
unset; set it higher on large repositories so a full build is not cut short by the tighter
query-time budget. `typescript_type_boost` (default `false`) opts into an isolated,
best-effort TypeScript Compiler API subprocess for type resolution; its absence or failure
never blocks indexing — the Tree-sitter baseline always runs.

`wiki_code_index` also accepts an optional `wait_seconds`, bounding how long the call
waits for the build it starts (or joins) before returning — the build itself keeps
running toward its own `max_full_rebuild_seconds` deadline regardless of how long the
caller waited. It must be between `0` and `max_full_rebuild_seconds`; an out-of-range
value is refused with the same generic `invalid_config` shape every other bad parameter
gets — `{"error": "code graph configuration is invalid", "code": "invalid_config",
"field": "wait_seconds", "hint": "inspect code_graph project configuration"}` — the
accepted range is not repeated in the answer, only here in this documentation. A value
below `0.5` is silently floored to `0.5` seconds. Pass `wait_seconds=0` to get a job
handle back in well under a second instead of waiting for the build; poll
`wiki_code_status` with that handle until the job reaches `ready` or `failed`.

The answer at expiry depends on how long you waited. When `wait_seconds` is less than
`max_full_rebuild_seconds` and the wait expires first, the answer is
`{"state": "rebuilding", "fresh": false, "job": {...}, "hint": "poll wiki_code_status for
this job"}` and the build keeps running, uncancelled. Omitting `wait_seconds` — the
default, and what every caller used before this feature existed — makes the wait
deadline equal the build's own deadline. A build still running when that deadline
arrives answers `busy`, unless it has already entered publication, in which case it
still gets the `{"state": "rebuilding", …, "job": {...}}` descriptor; the only change
from before this feature is that the build underneath that `busy` answer is no longer
cancelled. Poll `wiki_code_status`,
whose answer carries the same `job` descriptor (`id`, `state`, `started_at`, and
`finished_at` once terminal, plus `phase`/`phases_done` while still `running`) until the
job reaches `ready` or `failed`.

The build publishes its own snapshot, whether or not you are still waiting for it: under
`publish_mode = "postgres"` or `"mcp"` a detached build activates the remote snapshot
itself, as the last thing it does before reporting a terminal state. The job's state is
therefore the publication's state too — a build that indexed but could not publish ends
`failed`, not `ready`, even though the local snapshot it produced is complete. A call
that waited for its build still gets the publication result under `publication` in its
own answer; a call that detached reads only the job, so `failed` there is what tells you
the published graph is still the previous revision. `publish_mode = "sqlite"` publishes
nothing beyond the build itself, and its job is `ready` whenever the build was.

A waiting call's own `state` keeps describing the local snapshot, which really is `ready`
when the build finished, so the answer says the publication failed rather than leaving
you to infer it from `job`: it carries `publication_failed` in `warnings` alongside the
`publication` result and the `failed` job. Treat that warning, not `state`, as the answer
to "did my snapshot reach the target".

A publication target that is missing its configuration is refused before the build
starts, not after it: `wiki_code_index` answers `invalid_config` — and `iwiki-mcp code
publish` exits `2` — without indexing, because an absent `IWIKI_CODE_GRAPH_MCP_URL` or
`IWIKI_CODE_GRAPH_MCP_TOKEN` is a configuration error no rebuild can resolve. A target
that is configured but unreachable is the other case: there the build runs, the
publication is attempted, and the failure is a `publication_failed` one (`exit 1`).

One known limit follows from the build owning its publication. While a build runs the
session stays alive — that is what keeps the job handle readable — and the publication is
part of the build, so a target that stops answering holds the process open for as long as
its transport allows. Local reads are not held with it: once the graph is written the
build stops counting as a rebuild, so all three read tools — `wiki_code_status`,
`wiki_code_search` and `wiki_code_context` — answer from the finished local snapshot
throughout the publication, exactly as they would with no build running.

There is no separate publication deadline, and the two targets are bounded differently.
`mcp` bounds every remote call at a 30-second connect and 300-second read timeout, so
its worst case is those per-call bounds times the number of batches a snapshot needs.
`postgres` has no timeout of its own on this path: the publication connects over a DSN
carrying no timeout option and sets only a lock timeout, so a database that accepts the
connection and then stops answering blocks the publication indefinitely. That path is
reached only by the one-shot `iwiki-mcp code publish` — `wiki_code_index` answers
`source_unavailable` on a PostgreSQL binding — so it hangs that command rather than an
MCP session, and it was equally unbounded before the publication moved into the build.
A stuck publication is visible as a job that stays `running` long after its build's
phases stopped advancing; ending the client process ends it.

Pass the handle you hold back as `wiki_code_status(job_id=…)` and the answer describes
that build and no other, so a later build — a query-time auto-rebuild, say — cannot take
over the report underneath you. Called without `job_id` the answer reports the domain's
current build: the one running now, and the last one that finished when none is running.
A single build runs at a time, so when you poll without the handle, always check the
returned `id` against the one you hold — a different `id` means your own build's outcome
is no longer what the answer describes.

The job is session-scoped either way. A new process (a server restart, a fresh stdio
connection) knows no build and reports no `job` key at all, and neither does a `job_id`
that has aged out of the process's bounded history of recent builds (16, shared across
every domain that process builds for) — an unknown id is not an error, so the answer
keeps its normal shape and adds `job_unknown` to its `warnings`. A handle is also
unknown to a session bound to a different primary: a build belongs to exactly one domain,
and `wiki_code_status` reports only builds of the domain it is bound to, so a rebound
session is never told its graph is `ready` on the strength of another domain's build.
Fall back to `state`/`fresh` in each of those cases rather than wait forever for a
terminal job this process cannot answer for. An answer that carries `error` never
carries a `job`: the error describes the graph, not the build.

The descriptor does not depend on `code_graph.read_mode`. The build belongs to this
process, not to the snapshot a reader answered from, so a local server reports it whether
reads come from the local SQLite cache (`sqlite`) or from a published snapshot over MCP
(`mcp`). A hosted PostgreSQL server runs no local build — `wiki_code_index` answers
`source_unavailable` there — and its `wiki_code_status` carries no `job`; it issues no
handles either, so a `job_id` presented to it is unknown by construction and answers
`job_unknown` exactly as a local server does for an id it never issued.

Bash is opt-in. Either include `bash` in persistent `code_graph.languages` as above,
or explicitly request a one-shot rebuild with `wiki_code_index(languages=["bash"])`.
If both are omitted, the Python-only default remains in effect and no Bash files are
scanned.

The supported environment overrides are `IWIKI_CODE_GRAPH_ENABLED`,
`IWIKI_CODE_GRAPH_MAX_FILE_BYTES`, `IWIKI_CODE_GRAPH_MAX_FILES`, and
`IWIKI_CODE_GRAPH_AUTO_REBUILD`. The server never builds the code graph at startup.
Use `wiki_code_index` to request a full build; a bounded query-time rebuild is only
attempted when configured. A missing, incompatible, stale, or failed cache returns
typed diagnostics and leaves normal wiki operations available. A schema-v1 cache is
incompatible and is replaced by a deterministic full rebuild.

The MCP server exposes eight code-graph tools; the four publication tools are
documented under [distributed publication](code-graph-publishing.md):

| Tool | Contract |
| --- | --- |
| `wiki_code_status` | Reports local cache configuration, state, freshness, and diagnostics, plus a `job` descriptor while a build is running or just finished. A local server reports the job whichever reader `read_mode` selected; a hosted PostgreSQL server runs no build and carries none. Optional `job_id` reports that one build instead of the domain's current one; an id this server does not know answers normally with `job_unknown` in `warnings`. |
| `wiki_code_index` | Requests a full rebuild for the configured `languages`; `force` may rebuild an otherwise current cache. `wait_seconds` (pass `0` for an immediate job handle) bounds how long the call waits for that build without ever cancelling it: the answer is `rebuilding` with a `job` descriptor when `wait_seconds < max_full_rebuild_seconds`, or, when omitted (the default), `busy` if the build is still running at its own deadline without having entered publication — and the `rebuilding` descriptor if it has. |
| `wiki_code_search` | Searches typed file, module, and symbol entities with optional kind, path, language, and limit filters. |
| `wiki_code_context` | Expands exact typed entity-ID `seeds` through bounded relations; source inclusion defaults to `false`. |

`wiki_code_search`'s `path` filter is a literal, case-sensitive prefix over the stored
project-relative path. It is normalized before matching: surrounding whitespace is
trimmed, a leading `./` is dropped, and duplicate slashes are collapsed. A trailing `/`
is significant and preserved — `deploy/` scopes to that directory alone, while the bare
`deploy` is the wider prefix that also matches `deployment/`.

`wiki_code_context` accepts only exact file/module/symbol entity IDs returned by the
code graph. Its default direction is `both`, depth is `1`, and its bounded defaults
are 50 nodes, 20 files, and 200,000 source bytes. `include_source` is `false` by
default. Source discovery rejects unsafe paths and symlink escapes; query and context
calls fail safely if the local cache cannot be used.

Incremental indexing is not part of the Python MVP; it needs a separate specification
and delivery. TypeScript support is Tree-sitter-only static extraction (declarations,
imports, class/interface heritage); it does not extract interface members, and
`typescript_type_boost`'s Compiler API subprocess is opt-in, best-effort, and does not
yet wire real type information into resolution.

JavaScript support (extensions `.js`, `.jsx`, `.mjs`, `.cjs`) is Tree-sitter-only static
extraction, parsed with the same `tsx` grammar as TypeScript/TSX — a syntactic superset
of JavaScript including JSX, so no new parser dependency was added. Unlike TypeScript,
every JavaScript file is unconditionally module-backed (no top-level import/export
probe), because a CommonJS file that only assigns `module.exports` must still be a
resolvable import target. Extracted declarations cover classes, methods, functions
(including `async`), `const`/`let`/`var` arrow and function expressions, object-literal
methods (shorthand and `key: function`/`key: arrow`), and ES5 prototype methods
(`C.prototype.m = ...`, only when `C` is already a symbol declared in the same file).
Relations are `DECLARES`, `IMPORTS` (both ESM `import` and CommonJS `require`,
including destructured `require`), `CALLS`, and `INHERITS`. A relative specifier
(`./util.js`) resolves to a project module with its extension stripped, with a
`<dir>.index` fallback for directory imports — this is what makes a JavaScript file
import a TypeScript module. `wiki_code_context` accepts `js:` and `ts:` entity-ID
seeds, not only `py:`.

JavaScript's design priority is trust over coverage: it never emits a speculative
edge. JS-to-TS imports resolve, but TS-to-JS imports do not — TypeScript's own import
resolution was not changed and stays unresolved there. There is no type inference, no
execution of `node`/`tsc`/a bundler, and `node_modules` is not traversed. tsconfig/
jsconfig path aliases and `package.json` `imports`/`exports` maps are not read, so a
bare specifier stays unresolved. A dynamic `require(expr)`, a computed member access
(`o[k]()`), a call of a call (`f()()`), and a tagged template produce no edge. A bare
call inside a class method or an object-literal method does not bind to a sibling
member, because JavaScript itself requires `this.`/the object name for that — only a
function-like enclosing scope or the module scope is probed, and `this.m()` and
`super.m()` are not extracted. A value imported as a default export
(`import thing from './m'`) is never expanded into module members: `thing` is the
default-exported value, whose shape is not statically known, so `thing.build()` is not
treated as the module's named export `build` and stays unresolved. A namespace import
(`import * as ns`) and a whole-module `const m = require('./m')` do expand, because
both genuinely bind the module object. The same non-expandability reaches `extends`: a
class extending a default-imported base (`import Base from './base'; class X extends
Base {}`) produces no project-scoped, resolved INHERITS edge — the heritage target
falls back to the module-qualified name in the importing file and stays unresolved,
matching what the TypeScript adapter already does for every imported heritage target.
A named import (`import { Base } from './base'`) still resolves INHERITS across files.
One known limitation: a local binding or parameter that shadows an imported name still
expands to the import when a call target is built, because the resolver does not track
real lexical scope; fixing that is out of this MVP's scope.

## Bash support

Bash discovery considers only files whose case-insensitive suffix is `.sh`; it does
not discover `.bash` files or extensionless files selected only by a shebang. Both
`name() { ...; }` and `function name { ...; }` declarations become function symbols.
Literal command names become `CALLS` relations, but resolve only when exactly one
function with that name exists in the same file. External commands remain unresolved,
and dynamic command names are omitted.

`source` and `.` commands are parsed syntactically but are never followed: they emit no
`IMPORTS` relations and never enable cross-file resolution. Parsing never invokes a
shell, `source`, `eval`, expansions, or substitutions; graph metadata stores neither
source bodies nor command arguments. `wiki_code_context` accepts `sh:` entity IDs, and
source remains excluded by default.
