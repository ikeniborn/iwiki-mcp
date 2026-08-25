---
review:
  spec_hash: 6a05ba0041a10afc
  last_run: 2026-08-25
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-08-24-codegraph-publisher-cli-intent.md
---
# Design: codegraph-publisher-cli

**Date:** 2026-08-25
**Status:** draft
**Intent:** [docs/superpowers/intents/2026-08-24-codegraph-publisher-cli-intent.md](../intents/2026-08-24-codegraph-publisher-cli-intent.md)

## Acceptance (from intent)

Desired Outcomes (verbatim from the approved intent):

- An operator or scheduled job can run one command in a local checkout and publish a
  complete code-graph snapshot without invoking an MCP tool manually: `publish_mode =
  "sqlite"` activates the local snapshot, `publish_mode = "postgres"` publishes through
  the configured PostgreSQL publisher, and `publish_mode = "mcp"` publishes through the
  configured local or remote HTTP server.
- A successful repeated run safely replaces the active snapshot, and
  `wiki_code_status` reports the newly published snapshot as fresh and ready.
- systemd and CI receive a deterministic exit status and a concise publication summary.
- Database and remote publication credentials remain absent from command-line arguments,
  logs, generated files, and the repository.

Done when: the same command in a local checkout publishes and activates a complete
snapshot with each existing target — `sqlite`, `postgres`, and `mcp` — in focused
tests; `wiki_code_status` reports the selected target as fresh and ready; a failed run
demonstrably preserves the previous active snapshot; systemd or CI receives a stable
exit status and concise summary; database and remote credentials are absent from
arguments, logs, generated files, and the repository; and the existing code-graph test
suite passes.

## 0. Approach decision

Three approaches were evaluated during brainstorming:

- **A — shared publication application service (chosen).** Extract local build and
  publication orchestration from `server.py` into a reusable code-graph service. The
  MCP tool and CLI use the service through separate output adapters. This preserves one
  index/export/batch/finalize implementation without starting an MCP server from the
  CLI.
- **B — have the CLI invoke `wiki_code_index` through MCP.** Rejected because scheduled
  local publication would depend on a separately running MCP process and would still
  leave direct PostgreSQL publication unavailable when that server has no checkout.
- **C — duplicate the orchestration in the CLI.** Rejected because batch-limit,
  abort, and finalize behavior would have two implementations and could drift.

No architecture diagram is added: the flow is one linear source-to-target pipeline and
the source/target matrix below is clearer than a diagram.

## 1. Public command contract

The installed package exposes exactly this new command:

```text
iwiki-mcp code publish --project /path/to/repo
```

`--project` is required and identifies the Git checkout containing `.iwiki.toml` and
the source files. `--json` is the only optional flag:

```text
iwiki-mcp code publish --project /path/to/repo --json
```

The command does not add `--target`, `--force`, `--languages`, URL, token, DSN, user,
password, or fallback options. Target and languages come from the checkout's
`.iwiki.toml`. Existing secret-bearing environment variables remain authoritative:
`IWIKI_DB_PASSWORD` for direct PostgreSQL and
`IWIKI_CODE_GRAPH_MCP_URL`/`IWIKI_CODE_GRAPH_MCP_TOKEN` for MCP publication.

`server.main()` recognizes `code` as a package command and routes it to the CLI parser
without starting stdio MCP. The existing top-level `iwiki-mcp --project ...` stdio
contract and all existing administration commands retain their current parsing and
dispatch behavior.

## 2. Architecture

### 2.1 Shared application service

Add `src/iwiki_mcp/codegraph/application.py`. It owns the reusable one-shot flow:

1. Resolve the storage binding and `[code_graph]` configuration from the explicit
   project directory.
2. Derive a local source context independently from the publication target.
3. Compose the configured language adapters and `CodeGraphRuntime`.
4. Run fingerprint-aware indexing with `force=False` for the CLI.
5. For non-SQLite targets, export the complete local snapshot.
6. Select exactly one existing publisher from `publish_mode`.
7. Begin, batch within effective limits, finalize, and best-effort abort on failure.
8. Return an internal outcome containing the unchanged index result, publication result,
   selected mode, counts, revision, and duration.

The service accepts internal `force` and `languages` inputs so `wiki_code_index` can
retain its existing tool arguments. The CLI does not expose either input. Adapter
composition, publisher selection, effective batch-limit selection, and
`_publish_local_snapshot`-equivalent logic move to or are called through this service;
the `code publish` addition to `admin.py` contains only parsing, stream formatting, exit
mapping, and top-level redaction.

`server.wiki_code_index` delegates only its current Git/local path to the service and
adapts the outcome back to its current payload shape. In particular:

- SQLite mode still returns the index result directly.
- Existing Git-bound non-SQLite publication still returns the index result with nested
  `publication` when that configured publisher is reachable.
- A `PostgresBinding` at the MCP tool boundary still returns the existing
  `source_unavailable` payload because a hosted server may have no checkout.
- `wiki_code_status`, `wiki_code_search`, and `wiki_code_context` are unchanged.

The CLI is the new boundary that combines an explicit checkout with a direct
PostgreSQL target. It does not weaken the hosted MCP tool boundary.

### 2.2 Source context and target binding

The application service uses an immutable internal `CodeGraphSourceContext` rather than
requiring every local build to masquerade as the storage binding. It contains only:

- canonical project root;
- primary domain/repository ID;
- cache base;
- optional local Wiki base used by the existing selector resolver.

Source context derivation is deterministic:

| Resolved storage binding | Local cache base | Local Wiki selector source |
| --- | --- | --- |
| Git | Existing Wiki base | Existing Wiki base |
| PostgreSQL | Explicit project root | None; target keeps its existing finalization semantics |

`CodeGraphRuntime` receives the source context fields it already needs: cache base,
project directory, and primary domain. Git indexing retains the existing
`WikiSelectorResolver`. A PostgreSQL-bound local build does not treat the checkout as a
Wiki base and therefore does not resolve destination Markdown locally. PostgreSQL and
MCP targets continue deriving destination-dependent data through their existing
publisher/server behavior.

### 2.3 PostgreSQL-bound local cache

For a PostgreSQL binding, graph construction uses:

```text
<project>/.iwiki/code-<domain>.sqlite3
```

The existing location resolver also owns the associated WAL, SHM, lock, and metadata
files below the same `.iwiki/` directory. Before creating them, the service requires the
existing root-local `/.iwiki/` rule to be present in Git's local `info/exclude`; it uses
the existing worktree-aware `ensure_graph_store_excluded(project_root)` helper. Failure
to establish that exclusion is a configuration failure and no cache or publication
session is created. The command does not edit `.gitignore` or another tracked file.

The local SQLite cache is rebuildable source-side state. It is never uploaded as a file.
Only canonical snapshot rows pass to PostgreSQL or MCP publishers.

### 2.4 Target matrix

The command selects exactly `[code_graph].publish_mode`:

| Mode | Target | Required binding/environment | Activation |
| --- | --- | --- | --- |
| `sqlite` | Existing local code-graph cache beside the Git Wiki base | Git binding and local checkout | Existing atomic local index finalize |
| `postgres` | Configured PostgreSQL Wiki database | PostgreSQL binding, local checkout, `IWIKI_DB_PASSWORD` | Existing `PostgresCodeGraphStore.finalize` |
| `mcp` | Configured local or remote HTTP MCP server | Local checkout, URL/token environment, writable primary grant | Existing MCP publication finalize call |

Unsupported binding/mode composition fails configuration validation before indexing or
opening a publication session. The command never switches mode and never falls back.
`read_mode` remains independent and unchanged; operator examples pair it with the
publication target so `wiki_code_status` verifies the target just published.

## 3. Publication flow

### 3.1 SQLite

The runtime performs the existing fingerprint-aware local build. Its atomic metadata
and database publication becomes the active local snapshot. No external publisher,
export, session, or batch loop is created. A current identical snapshot is a successful
ready result.

### 3.2 Direct PostgreSQL

The runtime builds/opens the project-local cache, exports a complete canonical snapshot,
and selects the existing direct PostgreSQL publisher constructed from
`PostgresBinding`. The CLI itself issues no SQL. The publisher begins a staging session,
accepts canonical batches, and activates only through successful finalize.

Local `max_batch_rows` and `max_batch_bytes` retain their existing authority for direct
publication. The publisher's existing scope, integrity, staging, activation-lock, and
snapshot-preservation rules remain unchanged.

### 3.3 MCP over local or remote HTTP

The runtime exports the same canonical snapshot and uses `McpSnapshotPublisher` with
`RemoteMcpTransport`. The transport binds the configured primary and uses the existing
begin/batch/finalize/abort API. Batch sizing prefers valid server-advertised limits using
the existing effective-limit logic. Authorization and domain grants stay server-owned.

Local and remote HTTP servers use the same protocol and CLI behavior; only the
configured URL differs.

## 4. Result and stream contract

### 4.1 Text mode

Success writes one concise line to stdout and leaves stderr empty:

```text
code graph ready mode=mcp revision=sha256:... files=85 symbols=1539 relations=15272 duration_ms=420
```

Expected failure leaves stdout empty and writes one redacted line to stderr:

```text
iwiki-mcp: code graph publication failed (code=publication_failed)
```

### 4.2 JSON mode

`--json` writes exactly one compact JSON object followed by a newline to stdout. It
never mixes prose into stdout. Success has this stable top-level shape:

```json
{"state":"ready","publish_mode":"mcp","snapshot_revision":"sha256:...","counts":{"files":85,"symbols":1539,"relations":15272},"duration_ms":420}
```

Failure has this stable top-level shape:

```json
{"state":"failed","publish_mode":"mcp","error":"publication_failed","duration_ms":420}
```

`publish_mode` is `null` when configuration fails before mode resolution.
Configuration, usage, indexing, publication, and unexpected internal failures map to
stable codes rather than raw exception text. When `--json` appears in a `code publish`
invocation, parser failures also use the single-object JSON failure contract. Redacted
operational diagnostics may use stderr but cannot alter the stdout object.

### 4.3 Exit status

| Exit | Meaning |
| --- | --- |
| `0` | Snapshot is ready and the selected target activation completed successfully |
| `1` | Indexing, export, publication, abort, or unexpected runtime failure |
| `2` | CLI usage, project, binding, configuration, mode-composition, or missing-secret failure |

No additional exit statuses are introduced.

## 5. Failure handling and security

- A publication session is created only after local indexing/export succeeds.
- After a successful begin, any batch or finalize failure triggers one best-effort abort.
  Abort failure does not replace the original error and does not cause a retry.
- No internal retry or alternate-target fallback is added. A scheduler may run the
  one-shot command again.
- SQLite uses the existing atomic local path. PostgreSQL and MCP activate only through
  successful publisher finalize. Therefore an incomplete run cannot replace the prior
  active snapshot.
- CLI output never includes token, password, DSN, complete URL, absolute cache path,
  raw HTTP response, raw SQL/driver error, object repr, or traceback.
- The CLI catches expected configuration/publication errors and has a final redacted
  internal-error boundary. Existing library and MCP-tool payloads are not globally
  rewritten by this CLI policy.
- Environment values are consumed in memory by the existing binding/transport code and
  are absent from generated files and scheduler examples.

## 6. Requirements

- **R-001 — Command:** expose `iwiki-mcp code publish --project <checkout>` with only
  optional `--json`; route it without starting stdio MCP. **Acceptance: AC-01.**
- **R-002 — Shared service:** MCP local indexing and the CLI MUST share one application
  service for runtime composition, export, target selection, batching, abort, and
  finalize. **Acceptance: AC-02.**
- **R-003 — Source/target split:** a PostgreSQL binding MUST build through a local source
  context at `<project>/.iwiki/` without treating the project as a local Wiki base.
  **Acceptance: AC-03.**
- **R-004 — Cache exclusion:** PostgreSQL-bound local publication MUST establish the
  worktree-aware local Git exclusion before cache creation and MUST fail closed when it
  cannot. **Acceptance: AC-04.**
- **R-005 — Exact target:** select exactly `sqlite`, `postgres`, or `mcp` from
  `.iwiki.toml`; reject invalid composition and never override, retry, or fall back.
  **Acceptance: AC-05.**
- **R-006 — SQLite:** retain the existing fingerprint-aware and atomic local snapshot
  path without an external publication session. **Acceptance: AC-06.**
- **R-007 — PostgreSQL:** use the existing direct publisher abstraction and finalize
  path; CLI code MUST issue no raw SQL. **Acceptance: AC-07.**
- **R-008 — MCP:** use the existing HTTP publication API, project binding/grants, and
  valid server-advertised batch limits. **Acceptance: AC-08.**
- **R-009 — Output:** implement the text/JSON stream shapes and exit statuses in §4.
  **Acceptance: AC-09.**
- **R-010 — Redaction:** credentials and sensitive connection/cache details MUST be
  absent from arguments, output, logs, generated files, reprs, and tracebacks.
  **Acceptance: AC-10.**
- **R-011 — Atomic failure:** failed indexing/publication MUST preserve the prior active
  target snapshot; a begun remote/direct session receives best-effort abort.
  **Acceptance: AC-11.**
- **R-012 — Compatibility:** existing four code-graph MCP tool contracts, schema,
  snapshot format, publication protocol, and stdio/admin CLI routes MUST remain
  unchanged. **Acceptance: AC-12.**
- **R-013 — Operations docs:** document local, PostgreSQL, local-HTTP, and remote-HTTP
  use plus copy-ready systemd and generic CI examples without shipping deployment
  artifacts. **Acceptance: AC-13.**

## 7. Acceptance cases and tests

- **AC-01 — Parser/dispatch:** parser tests accept the two command forms, reject every
  unapproved flag, require `--project`, and prove `server.main()` routes the command
  without calling `mcp.run()`.
- **AC-02 — Shared orchestration:** focused tests invoke service entry points from the
  CLI and MCP adapters and assert one publisher selection and one shared batch/finalize
  path; no private server startup or duplicate CLI batch loop exists.
- **AC-03 — Source context:** a PostgreSQL fixture resolves its cache under the checkout,
  never accesses a nonexistent `PostgresBinding.base`, and does not install a local Wiki
  selector resolver.
- **AC-04 — Exclusion:** normal repo and linked-worktree tests establish exactly one
  `/.iwiki/` local exclude entry before cache creation; exclusion failure creates no
  cache and no publication session.
- **AC-05 — Mode selection:** table-driven tests cover `sqlite`, `postgres`, and `mcp`,
  plus an invalid binding/mode pair; each successful case selects one target and each
  failure records zero fallback calls.
- **AC-06 — SQLite integration:** a temporary Git Wiki/project fixture publishes a ready
  local snapshot, a repeated unchanged run succeeds, and `wiki_code_status` returns the
  ready revision.
- **AC-07 — PostgreSQL integration:** a disposable PostgreSQL fixture publishes from a
  real checkout through `PostgresCodeGraphStore`, finalizes a ready snapshot, and a
  reader/status call returns its revision and counts. A test guard proves the CLI/service
  contains no separate SQL publication implementation.
- **AC-08 — MCP integration:** a fake/local HTTP publication server advertises bounds,
  observes bound primary and bounded batches, finalizes a ready snapshot, and exposes it
  through status. Authorization or batch failure produces no fallback.
- **AC-09 — Streams/exits:** matrix tests cover text and JSON success, usage/config
  failure, indexing failure, and publication failure; JSON stdout always decodes as
  exactly one object and all expected exit values match §4.3.
- **AC-10 — Secret scanning:** failures containing sentinel token, password, DSN, URL,
  path, HTTP body, and driver text emit none of those sentinels to captured stdout,
  stderr, logging, JSON, or repr output.
- **AC-11 — Preservation:** SQLite, PostgreSQL, and MCP failure tests seed an active
  revision, fail after the new run starts, and prove the old revision remains active and
  queryable. Begun PostgreSQL/MCP sessions observe one best-effort abort call.
- **AC-12 — Regression:** existing `wiki_code_index` payload tests, complete code-graph
  tests, package CLI/admin tests, MCP smoke tests, and full pytest suite remain green;
  schema/protocol golden values are unchanged.
- **AC-13 — Documentation:** README, Russian README, architecture/operator Wiki pages,
  systemd unit/timer text, and generic CI command are checked for all supported targets,
  environment-only secrets, stable exits, and absence of committed deployment files.

Primary verification commands:

```bash
uv run iwiki-mcp code publish --help
uv run pytest -q
uv run flake8 src tests
```

PostgreSQL integration uses the repository's existing explicit disposable-database test
setup and marker; no production database is touched.

## 8. Documentation changes

- `README.md`: command, target matrix, output/exit contract, secret environment, and
  systemd/CI examples.
- `docs/README.ru.md`: equivalent Russian operator guidance.
- `docs/architecture.md`: shared application service and source-context/target-binding
  boundary.
- iwiki `concept/code-graph-publication`: publisher CLI as freshness mechanism and
  project-local PostgreSQL source cache.
- iwiki `guide/using-the-server`: agent/operator rule for choosing local SQLite, direct
  PostgreSQL, or MCP HTTP publication and verifying freshness before code-graph use.

The systemd example contains copy-ready service and timer bodies in documentation. It
uses `WorkingDirectory`, the explicit `--project`, and an external protected
`EnvironmentFile`; it does not embed secrets. The generic CI example shows shell setup
and command invocation without a provider-specific workflow file. No `.service`,
`.timer`, or CI workflow artifact is added to the repository.

## 9. Components expected to change

| Component | Intended change |
| --- | --- |
| `src/iwiki_mcp/codegraph/application.py` | New source context, shared orchestration, outcome, target composition |
| `src/iwiki_mcp/codegraph/runtime.py` | Accept explicit source context fields without changing graph behavior |
| `src/iwiki_mcp/admin.py` | Thin `code publish` parser, formatting, exit/redaction adapter |
| `src/iwiki_mcp/server.py` | Route `code`; delegate local MCP indexing orchestration; preserve tool payloads |
| `tests/codegraph/` | Service, cache, mode, atomicity, MCP, and regression coverage |
| `tests/postgres/` | Direct PostgreSQL publication and CLI parser/dispatch coverage |
| `README.md`, `docs/README.ru.md`, `docs/architecture.md` | Operator and architecture documentation |
| `pyproject.toml` | Required patch version bump |

Existing snapshot publishers, PostgreSQL schema, canonical row/hash model, and hosted
publication endpoints change only if implementation reveals a contradiction with this
design. Such a contradiction triggers the intent stop rule and requires renewed design
approval rather than an implementation-side workaround.

## 10. Out of scope

- `wiki_unified_search`, federated ranking, Wiki/code association retrieval, or changes
  to separate Markdown and code search tools.
- A daemon, file watcher, scheduler process, concurrent multi-target publication, retry
  policy, queue, or automatic fallback.
- New publish modes, public CLI overrides, read-mode changes, schema migration, protocol
  version change, domain-grant management, or credential storage.
- Supported deployment artifacts or provider-specific CI workflows.
