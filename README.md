# iwiki-mcp

*Русская версия: [docs/README.ru.md](docs/README.ru.md).*

## What it is

iwiki-mcp is a shared wiki service split into domains and queried over MCP from Codex
and Claude Code. It supports a Git-synced local base or tenant-isolated PostgreSQL,
over stdio or hosted Streamable HTTP as described below.

The supported container deployment runs hosted iwiki MCP, nginx, and the
[Telegram bot service](docs/telegram-bot.md) together. Allowlisted employees can
select domains, ask text or voice questions, and confirm page changes. See the
[deployment runbook](docs/deployment.md) for the operator path and migration steps.

## Install

Requires Python `>=3.10`. The recommended tool is [`uv`](https://docs.astral.sh/uv/); `pipx` works as a drop-in alternative.

### As a global tool (recommended for use)

iwiki-mcp is **not published to PyPI yet**, so install from a local checkout. Clone the repo and run this from the repo root:

```bash
git clone https://github.com/ikeniborn/iwiki-mcp.git
cd iwiki-mcp
uv tool install .
# or
pipx install .
```

This puts an `iwiki-mcp` executable on your `PATH` (e.g. `~/.local/bin/iwiki-mcp`), which is what the MCP client spawns. Verify with `iwiki-mcp --help`.

Once the package is published, a global install will be a one-liner — `uv tool install iwiki-mcp` (or `pipx install iwiki-mcp`). Until then those commands fail with `No matching distribution found for iwiki-mcp`; use the local-checkout install above.

### From source (development)

Clone, sync dependencies (including the `dev` extra), and run the tests:

```bash
git clone https://github.com/ikeniborn/iwiki-mcp.git
cd iwiki-mcp
uv sync --extra dev
uv run pytest -q
```

`uv run iwiki-mcp` then runs the server from the checkout without a global install.

## Storage and transport modes

| Storage | stdio | Streamable HTTP |
| --- | --- | --- |
| Git directory | supported; default | unsupported |
| PostgreSQL | supported for one locally configured wiki | supported for hosted multi-wiki access |

### Local Git stdio

The existing local mode is unchanged:

```bash
export IWIKI_BASE_DIR=/srv/iwiki-base
iwiki-mcp --project /srv/project
```

### Local PostgreSQL stdio

Create `/srv/project/.iwiki.toml` with an explicit maximum domain scope. Unlike Git
storage, PostgreSQL requires non-empty `read` and `write` arrays and a `primary`
domain. The named wiki and domains must already have been created by an administrator.

```toml
read = ["backend", "frontend"]
write = ["backend"]
primary = "backend"

[storage]
type = "postgres"
host = "db.internal.example"
port = 5432
database = "iwiki"
user = "iwiki_app"
sslmode = "verify-full"
iwiki_id = "team-wiki"
```

Supply secrets and model identity only through the process environment:

```bash
export IWIKI_DB_PASSWORD='<database-password>'
export IWIKI_LLM_BASE_URL='https://models.internal.example/v1'
export IWIKI_LLM_KEY='<model-api-key>'
export IWIKI_EMBED_MODEL='lemonade-embeddings-bge-m3-q8'
export IWIKI_EMBED_DIMENSIONS='1024'
export IWIKI_RERANK_MODEL='lemonade-reranker-bge-reranker-v2-m3'
iwiki-mcp --project /srv/project
```

`wiki_bind` may narrow this maximum scope for the current process; it cannot widen it.
PostgreSQL update and delete calls require `expected_revision` from `wiki_read_page`.

### Hosted Streamable HTTP

Hosted mode requires PostgreSQL and a separate server TOML. It rejects `iwiki_id`:
the bearer token selects one wiki and its maximum read/write grants.

```toml
[storage]
type = "postgres"
host = "db.internal.example"
port = 5432
database = "iwiki"
user = "iwiki_app"
sslmode = "verify-full"

[server]
host = "127.0.0.1"
port = 8765
allowed_origins = ["https://iwiki.example"]
pool_min_size = 2
pool_max_size = 10
statement_timeout_ms = 30000
lock_timeout_ms = 5000
```

```bash
export IWIKI_SERVER_CONFIG=/etc/iwiki/server.toml
export IWIKI_DB_PASSWORD='<database-password>'
export IWIKI_LLM_BASE_URL='https://models.internal.example/v1'
export IWIKI_LLM_KEY='<model-api-key>'
export IWIKI_EMBED_MODEL='lemonade-embeddings-bge-m3-q8'
export IWIKI_EMBED_DIMENSIONS='1024'
export IWIKI_RERANK_MODEL='lemonade-reranker-bge-reranker-v2-m3'
iwiki-mcp serve --transport streamable-http
```

The MCP endpoint is `/mcp`. Put the loopback listener behind a reverse proxy that
terminates public TLS, forwards the exact `Origin`, and does not log `Authorization`.
Browser requests must match `allowed_origins`; clients without an `Origin` are allowed,
but every MCP request still needs `Authorization: Bearer <token>`. Invalid credentials,
grants, sessions, and unavailable storage return sanitized 401/403/404/503 responses.
Hosted mode does not emit server-initiated notifications: after Bearer authentication,
`GET /mcp` returns `405 Method Not Allowed` with `Allow: POST, DELETE` without entering
the MCP session manager. Stateful `POST` requests and `DELETE` session termination remain
available.

### Supported application container

Production deployment uses the repository `compose.yaml` as one hardened application
service with three supervised children: hosted MCP on `127.0.0.1:8765`, nginx on the
operator-selected LAN/Traefik listener, and `iwiki-telegram-bot`. Supply exactly these
host-side files:

```text
/opt/iwiki-mcp/server.toml       hosted MCP and external PostgreSQL endpoint
/opt/iwiki-mcp/nginx.conf        LAN/Traefik listener and loopback upstream
/opt/iwiki-mcp/runtime.env       owner-only runtime secrets and bot settings
```

PostgreSQL remains an external, operator-managed durable service. A same-host database
container must publish a host port such as `127.0.0.1:55432`; a remote database supplies
its host and custom port and should use `sslmode = "verify-full"`. This Compose project
and its runtime create no PostgreSQL service, database, or schema objects and run no
migrations. An operator must provision the exact compatible schema out of band with the
repository's administration/migrator path. Follow the
[deployment runbook](docs/deployment.md) for configuration, HTTPS proxy routing,
isolated-host validation, migration, cutover, and rollback. Because production uses
host networking with a fixed MCP listener on `127.0.0.1:8765`, a full combined-container
precheck cannot run concurrently on that host; without an isolated host or VM, schedule
maintenance downtime and retain the old services for rollback.

The server opens a bounded connection pool and applies the configured statement and
lock timeouts. Startup probes the model endpoint, validates model metadata, and requires
the exact schema version and provisioned runtime principal before opening the listener;
it never runs migrations. One database can hold many isolated wikis under distinct
`iwiki_id` values. The configured embedding model and dimension are database-wide
metadata: a mismatch refuses startup; changing them is an operator-managed migration,
not an automatic re-embedding. Embedding and rerank credentials remain server-only.

Every request reloads current token authority. A session keeps its explicit `selected`
scope separately from the fresh-grant `effective` scope: revocation applies on the next
request, restored access reappears only when it remained selected, and a new target grant
does not expand an established session. Only successful `wiki_create_domain` provisioning
expands the creator's current session. Project initialization still owns local
`.iwiki.toml` and `.iwikiignore`; the hosted server creates PostgreSQL domain state but
never writes those project files.

### Session lifetime and binding provenance

A `wiki_bind` selection is **process-local and session-scoped**. It is keyed by
`mcp-session-id`, expires after 30 minutes of inactivity, and does not survive a server
restart. When no selection is found the server falls back to the token's own default
scope and keeps answering — the fallback is permitted, but never silent:

- `wiki_status`, `wiki_bind`, `wiki_code_status`, `wiki_code_search`,
  `wiki_code_context`, `wiki_code_publish_begin`, `wiki_spec_search`,
  `wiki_spec_context`, and `wiki_spec_resolve` carry `binding_source`, either
  `session` (a selection made by `wiki_bind` in this session) or `token_default` (the
  fallback built from the token's grants).
- A `tools/call` refused at the authorization gate carries the same `binding_source` in
  its `access_denied` payload, so a refusal caused by a lost selection is recognizable
  without a second call.
- The domain-free code reads additionally add `binding_defaulted` to their `warnings`
  under `token_default`, so an answer from another project's snapshot is recognizable
  even though it reports `state: ready` and `fresh: true`.
- `wiki_bind` returns the `session_id` it bound to, so an answer belonging to a different
  session is recognizable.
- When the write-scope intersection replaces the selected primary, the answer carries
  `primary_substituted: true` and `requested_primary`.

The client's contract is therefore: re-bind after a reconnect, after an idle period, and
whenever an answer reports `binding_source: token_default`. A hosted server may turn that
fallback into a refusal for code reads with `code_graph.require_session_binding = true`;
those three tools then return `{"error": "binding_not_selected"}` and no snapshot content
until `wiki_bind` runs. The option is off by default and never affects Markdown tools,
which name their domain explicitly.

```toml
[code_graph]
require_session_binding = false # true refuses defaulted domain-free code reads
```

### PostgreSQL provisioning and least privilege

The operator creates the database and installs the `vector` extension. A dedicated
administration-only schema owner/migrator uses the repository's admin commands to create
and migrate only the `iwiki` schema before runtime starts. Never configure that
credential as the running server login. Grant the runtime role only `CONNECT`, `USAGE`,
and the required table and sequence privileges after migration; it owns no schema,
receives no `CREATE`, and runs no migrations. Do not grant access to unrelated schemas.
Use `sslmode="verify-full"` with a trusted CA and matching database hostname outside an
isolated development host.

All PostgreSQL admin commands accept `--config PATH`; otherwise they read
`IWIKI_SERVER_CONFIG`. Only the bare stdio command accepts `--project`; `serve` accepts
only `--transport streamable-http`. `--read-domain` and `--write-domain` may be repeated.
`base show`, `base list`, `token list`, import, and export support machine-readable
`--json`; import/export alone support `--dry-run`.

```bash
iwiki-mcp base create --iwiki team-wiki
iwiki-mcp base list
iwiki-mcp base show --iwiki team-wiki
iwiki-mcp base disable --iwiki team-wiki
iwiki-mcp base enable --iwiki team-wiki
iwiki-mcp domain create --iwiki team-wiki --domain backend
iwiki-mcp token list --iwiki team-wiki
iwiki-mcp token set-create-domain --iwiki team-wiki --token-id replace-with-token-id --enabled
iwiki-mcp token set-domain-management --iwiki team-wiki --token-id replace-with-token-id --domain backend --enabled
iwiki-mcp token revoke --token-id replace-with-token-id
iwiki-mcp base import-git --iwiki team-wiki --path /srv/old-wiki --dry-run --json
iwiki-mcp base export-git --iwiki team-wiki --path /srv/rollback-wiki --dry-run --json
```

`token create` prints plaintext token material once. The examples below therefore print
to the terminal: do not run them in a recorded session, and store the result directly in
a secret manager. Production operators should use the non-printing capture procedure in
the [deployment runbook](docs/deployment.md#out-of-band-schema-migration-and-principal-provisioning).
`token list` never returns it and reports `can_create_domain`, `managed_domains`,
`read_domains`, and `write_domains` in both default JSON and `--json` output.
`set-create-domain` and `set-domain-management` are server-side recovery operations;
exactly one of `--enabled` or `--disabled` is required. Revocation and wiki disable take
effect on later requests. Token revocation atomically removes its content and management
grant rows while retaining the revoked token audit record. There is intentionally no
physical-delete command.

Import reads a Git wiki repository and writes one PostgreSQL wiki. Export requires an
empty destination, writes a portable Git repository, and creates its initial commit.
`--dry-run` validates and reports without mutation. For local rollback, export, point a
project's `.iwiki.toml` back to Git storage and the exported base, then run `wiki_index`.
Import/export never run `wiki_sync` automatically.

Database backup, encryption, retention, and restore drills are operator responsibilities.
Use PostgreSQL-native tools and a service definition so credentials do not enter shell
history. The restore target database must already exist.

Migration v4 is forward-only and adds `can_create_domain`,
`token_domain_management_grants`, and domain-leading grant indexes. There is no down
migration. An older binary rejects schema v4, so binary rollback requires restoring a
pre-v4 database backup or deploying a compatibility release before startup.

```bash
pg_dump --dbname=service=iwiki --format=custom --schema=iwiki --file=/secure/encrypted-volume/iwiki.dump
pg_restore --dbname=service=iwiki_restore --clean --if-exists --schema=iwiki /secure/encrypted-volume/iwiki.dump
```

#### Runtime principals for the code graph

Three database roles stay separate. The schema owner and migrator is an
administration-only credential: it owns the `iwiki` schema and applies migrations
through the admin commands, and it is never configured as a running server's login.
The hosted service principal is the role a hosted server connects as. The direct
runtime principal is the role a local direct-PostgreSQL indexer connects as. Both
runtime roles are non-owner, hold no `BYPASSRLS`, run no migrations, and receive no
database or schema `CREATE`. Row-level security is enabled with ordinary
`ENABLE ROW LEVEL SECURITY`, never `FORCE`, because the owner is administration-only.

First use the administration-only configuration to apply migrations and create the base
and domains. Any non-dry-run admin command except the schema compatibility path checks
and advances the schema before its requested operation; `base list` is the explicit
operator migration trigger used by the deployment runbook.

```bash
iwiki-mcp base list --config /opt/iwiki-mcp/admin-server.toml --json
iwiki-mcp base create --config /opt/iwiki-mcp/admin-server.toml --iwiki team-wiki
iwiki-mcp domain create --config /opt/iwiki-mcp/admin-server.toml --iwiki team-wiki --domain backend
```

Create the PostgreSQL runtime login out of band before registering it. Its password and
runtime configuration stay separate from the schema-owner configuration. `principal
grant` never creates a role and never accepts its password. Register each runtime role
and its domain grants explicitly, then inspect the exact hosted role before issuing any
token.

```bash
iwiki-mcp principal grant --config /opt/iwiki-mcp/admin-server.toml --iwiki team-wiki --principal iwiki_hosted --runtime hosted --read-domain backend --write-domain backend
iwiki-mcp principal grant --config /opt/iwiki-mcp/admin-server.toml --iwiki team-wiki --principal iwiki_indexer --runtime direct --read-domain backend --write-domain backend
iwiki-mcp principal inspect --config /opt/iwiki-mcp/admin-server.toml --principal iwiki_hosted --json
```

Only after that inspection, issue tokens against the exact deployed hosted role.
`token create` requires `--hosted-principal ROLE`, where `ROLE` equals the hosted
server's `[storage].user`. It verifies that this named role is registered as
`runtime=hosted`, is a non-owner without `BYPASSRLS`, and already covers every requested
read and write domain before any token material is generated. This also applies to a
bootstrap token with `--can-create-domain`; another hosted role, or a generic "some
hosted role exists" check, is not a substitute.

```bash
iwiki-mcp token create --config /opt/iwiki-mcp/admin-server.toml --iwiki team-wiki --owner deploy --hosted-principal iwiki_hosted --read-domain backend --write-domain backend
iwiki-mcp token create --config /opt/iwiki-mcp/admin-server.toml --iwiki team-wiki --owner bootstrap --hosted-principal iwiki_hosted --read-domain backend --write-domain backend --can-create-domain
iwiki-mcp serve --transport streamable-http
```

Startup performs the same schema check for hosted HTTP and stdio: the server validates
the exact expected schema version and its own connected `session_user` against the
provisioned grants, and refuses to start otherwise. It never runs migrations implicitly.

#### Schema v5 rollback and the compatibility artifact

Migration v5 adds the code-graph tables. Rolling back the application to a
pre-code-graph release is a maintenance procedure, not a redeploy of an arbitrary older
commit: the raw pre-code-graph commit is not a supported rollback binary, because a
restricted runtime role holds no schema `CREATE` and that binary would try to create
schema objects at startup.

The supported path is the pinned maintenance artifact `compat/postgres-v4-runtime-guard.json`
with its patch. The manifest records the base commit, the patch digest, the source-tree
digest, and the schema version the patched runtime accepts. Rebuild and verify it by
checking out the recorded base commit, applying the recorded patch, and confirming both
digests before deployment.

```bash
iwiki-mcp schema rollback-v5-compat --json
iwiki-mcp schema rollback-v5-compat --confirm --json
```

The dry run reports the marker it would remove and changes nothing. Only `--confirm`
removes the schema-5 marker, leaving the code-graph tables in place and unused. After
the rollback, smoke the patched maintenance artifact against the database: it must start
read-only under the restricted runtime role and must hold no `CREATE` or
`schema_migrations` mutation privilege. Re-applying migration v5 later is the ordinary
forward migration; it is idempotent.

Stop the production rollout, rather than working around it, when the exact hosted
principal cannot be proven, when a required domain grant is missing, when the connected
`session_user` differs from the provisioned role, when the schema version does not match
exactly, or when the maintenance artifact digests do not reproduce.

### PostgreSQL MCP tool contract

| PostgreSQL support | Tools |
| --- | --- |
| Supported | `wiki_status`, `wiki_list_domains`, `wiki_list_pages`, `wiki_read_page`, `wiki_search`, `wiki_related`, `wiki_write_page`, `wiki_update_page`, `wiki_insert_section`, `wiki_delete_section`, `wiki_move_section`, `wiki_delete_page`, `wiki_index`, `wiki_bind`, `wiki_lint` |
| Hosted PostgreSQL only | `wiki_create_domain`, `wiki_list_domain_grants`, `wiki_set_domain_grant`, `wiki_revoke_domain_grant` |
| Supported code graph | `wiki_code_status`, `wiki_code_search`, `wiki_code_context` |
| Hosted PostgreSQL only | `wiki_code_publish_begin`, `wiki_code_publish_batch`, `wiki_code_publish_finalize`, `wiki_code_publish_abort` |
| Local checkout only | `wiki_code_index` |
| Git only | `wiki_remediation_plan`, `wiki_migrate_okf`, `wiki_apply_okf`, `wiki_export_okf`, `wiki_sync` |

Git-only tools return
`{"error":"unsupported_storage","storage":"postgres","hint":"use this tool with Git storage"}`.
The three grant tools return `unsupported_transport` with actual `storage` and
`transport` outside hosted PostgreSQL. `wiki_create_domain(name)` requires
`can_create_domain`; it atomically creates the domain plus caller read/write and
`can_manage_grants` rows, returning `created`, `already_existed`, `domain`, and the
complete effective session scope. Exact retries are idempotent.

`wiki_list_domain_grants(domain)` exposes token owner and content/management flags for
audit. `wiki_set_domain_grant(domain, token_id, can_read, can_write)` and
`wiki_revoke_domain_grant(domain, token_id)` may change only another active token's
content row. Write requires read, empty grants must be revoked, self-target is denied,
and management authority cannot be delegated over HTTP: no MCP schema accepts a
management-write field. CLI recovery is the only post-bootstrap path for management
authority.

Hosted creation returns the complete creator scope:

```json
{"created":"new-project","already_existed":false,"domain":"new-project","read":["new-project"],"write":["new-project"],"primary":"new-project"}
```

An exact retry changes only `already_existed` to `true`. Grant list returns
`{"domain":<domain>,"grants":[{"token_id":...,"owner":...,"can_read":...,"can_write":...,"can_manage_grants":...}]}`.
Set returns the named `domain`, `token_id`, `can_read`, and `can_write`; revoke returns
the named `domain`, `token_id`, and `revoked` boolean. A single `tools/call` refused
before dispatch — missing capability, malformed protected arguments, or a client-supplied
`iwiki_id` — answers HTTP 200 with one JSON-RPC error
`{"code":-32001,"message":"access_denied","data":{"hint":...}}`, so an MCP client can
correlate the refusal with its request id. That `data` also carries the caller's own
`binding_source` and, when the gate can attribute the refusal, a `reason`. The hint stays
deliberately vague and no field ever names a domain, a wiki, or another token.
A batch request refused the same way keeps
HTTP 403 `{"error":"access denied"}`, since a batch carries no single id; authentication,
origin, and session failures likewise stay on HTTP 401/403/404. Authority lost after
dispatch, self-target, and foreign/missing transactional state return HTTP 200 with the
in-band `{"error":"access_denied",...}` tool result. Invalid syntax or grant flags
return a sanitized MCP/tool validation failure.

PostgreSQL `wiki_status` reports `storage`, `transport`, effective `read`/`write`,
`primary`, and visible `domains`; local stdio also reports `project_dir`. It never
reports the DSN or credentials:

```json
{"storage":"postgres","transport":"streamable-http","read":["backend"],"write":["backend"],"primary":"backend","domains":["backend"]}
```

PostgreSQL `wiki_read_page` includes the optimistic revision alongside the authored
Markdown. Pass that value to update or delete:

```json
{"domain":"backend","slug":"architecture/auth","markdown":"# Auth\n\n## Flow\n...\n","revision":2}
```

Omitting or losing an optimistic revision returns stable shapes. Read the page again
before retrying a conflict:

```json
{"error":"expected_revision_required","hint":"read the page and retry with its revision"}
{"error":"conflict","current_revision":2,"hint":"read the page and retry against the current revision"}
```

Current non-goals: HTTP with Git storage, automatic Git sync, database or extension
creation, physical wiki deletion, and automatic embedding-model/dimension migration.

## Python code graph MVP

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
documented under distributed publication below:

| Tool | Contract |
| --- | --- |
| `wiki_code_status` | Reports local cache configuration, state, freshness, and diagnostics. |
| `wiki_code_index` | Requests a full rebuild for the configured `languages`; `force` may rebuild an otherwise current cache. |
| `wiki_code_search` | Searches typed file, module, and symbol entities with optional kind, path, language, and limit filters. |
| `wiki_code_context` | Expands exact typed entity-ID `seeds` through bounded relations; source inclusion defaults to `false`. |

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

### Bash support

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

### Distributed code graph publication

The code graph is always built from a local checkout, but the resulting snapshot may
live somewhere else. One machine with the repository indexes it and publishes one
immutable snapshot; a server without the checkout answers `wiki_code_status`,
`wiki_code_search`, and `wiki_code_context` from the active snapshot.

Select exactly one publication target and one read target in the bound project's
`.iwiki.toml`. There is no fallback: a failure in the selected mode is returned to the
caller and never retried against another mode.

```toml
[code_graph]
publish_mode = "sqlite" # sqlite | postgres | mcp
read_mode = "sqlite"    # sqlite | postgres | mcp
max_snapshot_age_seconds = 86400 # 0 disables age rejection
max_batch_rows = 1000
max_batch_bytes = 1000000
publication_session_ttl_seconds = 900
staging_retention_seconds = 86400
staging_cleanup_limit = 100
```

The published snapshot — not the reading server's own configuration — decides which
languages a hosted read may return. `wiki_code_search` on PostgreSQL storage derives its
language filter from the active snapshot's header, intersected with the languages the
running server binary can query, so a hosted server needs no `code_graph.languages` of
its own and its project directory may be empty. An unfiltered search returns rows in
every language the snapshot declares; a filter naming a language the snapshot lacks
returns `{"error": ..., "code": "unsupported_language", "hint": "the active snapshot
declares: ..."}` (previously the misleading `invalid_config`), while a language this
build cannot parse still returns `invalid_config`. A language the snapshot declares but
the server binary does not know is dropped from the filter and reported in `warnings` as
`unknown_snapshot_language:<name>`. Publishing a broader language set therefore widens
what that domain's hosted reads return. Local `sqlite` reads are unchanged: there the
project's own `code_graph.languages` stays authoritative.

A ready snapshot older than a positive `max_snapshot_age_seconds` returns
`stale_snapshot` and no rows, while status keeps reporting age and timestamps. Value
`0` disables age rejection entirely. The hosted server enforces its own validated
ceilings for the numeric fields; a remote client cannot raise them. For `max_batch_rows`
and `max_batch_bytes` specifically, `publish_mode = "mcp"` discovers the server's actual
limits from `wiki_code_publish_begin`'s response and sizes batches to them automatically
— a local `.iwiki.toml` value larger than the server's own is never sent as-is, and a
rejection states the exact limit and what was received instead of a bare `invalid_batch`.

Secrets never enter `.iwiki.toml`. MCP mode reads `IWIKI_CODE_GRAPH_MCP_URL` and
`IWIKI_CODE_GRAPH_MCP_TOKEN` from the runtime environment only, and both are absent
from status, logs, snapshot headers, errors, and object reprs. Direct PostgreSQL mode
reuses the existing `[storage]` block and requires `IWIKI_DB_PASSWORD`,
`IWIKI_EMBED_MODEL`, and `IWIKI_EMBED_DIMENSIONS` (plus optional
`IWIKI_RERANK_MODEL` when configured).

| Mode | Publishes to | Requires |
| --- | --- | --- |
| `sqlite` | The local code-graph cache next to the wiki base | A local checkout; no mode-specific publication environment variables |
| `postgres` | The configured PostgreSQL wiki database | A local checkout plus `[storage]`, `IWIKI_DB_PASSWORD`, `IWIKI_EMBED_MODEL`, and `IWIKI_EMBED_DIMENSIONS` (optional `IWIKI_RERANK_MODEL`) |
| `mcp` | An authenticated Streamable HTTP endpoint on same machine or remote | A local checkout plus `IWIKI_CODE_GRAPH_MCP_URL` and `IWIKI_CODE_GRAPH_MCP_TOKEN` |

`wiki_code_index` stays a local extraction operation. On a server without a checkout it
returns `source_unavailable` and creates no session and no snapshot; run the indexer on
a machine that holds the repository. One primary domain maps to exactly one repository.

Remote publication is a four-call lifecycle over the existing bearer-token
authorization: `wiki_code_publish_begin`, repeated `wiki_code_publish_batch`,
then `wiki_code_publish_finalize` or `wiki_code_publish_abort`. None of them accepts a
tenant or domain field; the client binds each remote session to the local project's
`primary` (from `.iwiki.toml`) with `wiki_bind` right after `session.initialize()`, and
the server derives `iwiki_id` and the bound primary from that session, so the token must
hold write access to the project's primary domain — `wiki_bind` narrows an already
granted scope and cannot widen it. A session belongs to the identity that created it:
another token with write access to the same domain cannot append to, abort, or finalize
it, and a replacement process must start a new session.

Batches carry rows only — never a database file, source text, an absolute checkout
path, credentials, or publisher-generated wiki links. The target recomputes the payload
revision, derives code-to-wiki links from the destination Markdown itself, and activates
the snapshot in a single commit. Readers therefore observe either the previous complete
revision or the new one, never a partial upload. Repeating an accepted ordinal with the
same rows succeeds idempotently; repeating it with different rows returns
`batch_conflict`.

Retry the whole publication after `busy`, `session_expired`, `snapshot_conflict`,
`revision_mismatch`, or `markdown_unavailable`: begin a new session and resend. A
`snapshot_conflict` means the active snapshot or the destination Markdown changed while
the session was open, so the rebuilt graph must be published against the current state.
Expired staging sessions are cleaned up in bounded batches when the next session begins;
no background daemon runs.

For PostgreSQL or remote MCP reads, `include_source=true` returns graph context without
source plus `source_unavailable`; the server never fetches source from the publisher.
Local SQLite reads keep their existing guarded local-source behavior. Search and context
limits are enforced for every read adapter, so a remote caller cannot request an
unbounded result or load a whole graph implicitly.

The first publication into an empty domain is an ordinary session: status reports
`missing_snapshot` until the first `finalize` succeeds.

### Scheduled publisher operation

Run publisher on machine holding checkout. For every valid exactly-one `publish_mode`
(`sqlite`, `postgres`, or `mcp`), use same command:

```bash
iwiki-mcp code publish --project <checkout> [--json]
```

`sqlite` publishes to the local target/cache under the configured Git Wiki base at
`<wiki-base>/.iwiki/code-<domain>.sqlite3`; `postgres` uses existing publisher
abstraction with configured direct PostgreSQL binding, never raw SQL; and `mcp` uses
same publication protocol through a local or remote Streamable HTTP endpoint configured
by `IWIKI_CODE_GRAPH_MCP_URL` and its token. A local endpoint is an HTTP server on same
machine, never stdio. Local and remote HTTP publication are equivalent targets: choose
one configured by single `publish_mode`; do not improvise fallback. Only the PostgreSQL
source cache remains local at `<project>/.iwiki/code-<domain>.sqlite3`, is excluded
through `.git/info/exclude`, and is not fallback target.

| Output | Meaning | Exit status |
| --- | --- | --- |
| Text | Human-readable output format | Either format exits by outcome |
| `--json` | Compact machine-readable output format | Either format exits by outcome |

Text and `--json` choose only output format. Either format exits `0` when ready, `1`
for runtime/publication failure, or `2` for usage/configuration failure.

Both text stderr and compact JSON redact secrets and operational location data: no
password, token, URL, DSN, or checkout path is emitted. `postgres` reads
`IWIKI_DB_PASSWORD`, `IWIKI_EMBED_MODEL`, and `IWIKI_EMBED_DIMENSIONS` (plus optional
`IWIKI_RERANK_MODEL` when configured); `mcp` reads `IWIKI_CODE_GRAPH_MCP_URL` and
`IWIKI_CODE_GRAPH_MCP_TOKEN` from protected runtime environment.

Install scheduling outside this repository. Save the service as
`/etc/systemd/system/iwiki-codegraph-publisher.service` and the timer as
`/etc/systemd/system/iwiki-codegraph-publisher.timer`. Keep the protected environment
file root-owned mode `0600`; it supplies `IWIKI_DB_PASSWORD`, `IWIKI_EMBED_MODEL`, and
`IWIKI_EMBED_DIMENSIONS` (plus optional `IWIKI_RERANK_MODEL`) without embedding values
in the unit. The dedicated `iwiki` account needs access to the checkout.
Mode-specific EnvironmentFile contents: `postgres` uses `IWIKI_DB_PASSWORD`,
`IWIKI_EMBED_MODEL`, and `IWIKI_EMBED_DIMENSIONS` (plus optional `IWIKI_RERANK_MODEL`);
`mcp` uses `IWIKI_CODE_GRAPH_MCP_URL` and `IWIKI_CODE_GRAPH_MCP_TOKEN`; `sqlite` needs
no mode-specific publication variables.

```ini
[Unit]
Description=Publish iwiki code graph

[Service]
Type=oneshot
User=iwiki
WorkingDirectory=/srv/project
EnvironmentFile=/etc/iwiki/codegraph-publisher.env
ExecStart=/usr/local/bin/iwiki-mcp code publish --project /srv/project --json
```

```ini
[Unit]
Description=Schedule iwiki code graph publication

[Timer]
OnCalendar=hourly
Persistent=true
Unit=iwiki-codegraph-publisher.service

[Install]
WantedBy=timers.target
```

For any CI provider, make protected secret variables available to job environment and
run identical command; this documentation intentionally adds no provider workflow file:

```bash
export IWIKI_DB_PASSWORD
export IWIKI_EMBED_MODEL
export IWIKI_EMBED_DIMENSIONS
export IWIKI_CODE_GRAPH_MCP_URL
export IWIKI_CODE_GRAPH_MCP_TOKEN
iwiki-mcp code publish --project <checkout> --json
```

Before `wiki_code_search` or `wiki_code_context`, verify `wiki_code_status` reports
`fresh == true`. Use Markdown `wiki_search` separately when only wiki semantics are
needed. The supported daily sequence is `wiki_search → wiki_code_search → wiki_code_context`.
Unified wiki/code search is future work and not implemented. `wiki_unified_search` remains intentionally unregistered because
quality evidence returned `do_not_implement`; see the [evaluation report](docs/superpowers/evidence/wiki-unified-search-evaluation.md)
and [machine-readable evidence](docs/superpowers/evidence/wiki-unified-search-evaluation.json).

### SQLite snapshot profiles and commit uncertainty

The local SQLite cache has exactly two accepted schema-v2 profiles. The legacy profile
holds the five public entity tables and requires the strict database-plus-sidecar
storage-stamp validation. The publication profile adds the internal
`code_graph_publication` table, which then carries the authoritative ready evidence; on
that profile `.metadata.json` is a cache only and may be absent, stale, or regenerated
without changing readiness.

SQLite publication may return `commit_uncertain`. It means the canonical replacement may
have happened but directory durability was not confirmed. It claims neither success nor
rollback, and it permits exactly one recovery: repeating `finalize` in the same process.
Batch, abort, automatic rollback, and adapter fallback are all refused. If the process is
lost before reconciliation, inspect `wiki_code_status` and start a new session. Direct
PostgreSQL and remote MCP never emit `commit_uncertain`.

Before rolling back to a pre-publication binary, retain or restore a legacy snapshot, or
reindex with that binary, because it may reject the internal table.

### Code graph benchmark

Run the offline release evidence from the repository root:

```bash
uv run python -m eval.code_graph --fixture-root tests/fixtures/codegraph --output /tmp/iwiki-code-graph-evidence
```

It writes JSON and Markdown reports to the output directory. Every search case must
have a warm maximum below `<500 ms`; this is the blocking first-release gate. The
strict `<150 ms` comparison is reported as a non-blocking post-v1 target. Any other
blocking gate miss writes evidence and exits nonzero.

## Search pipeline benchmark

The bounded fusion benchmark under `eval/search_pipeline/` is evaluation-only: it does not change production search behavior, production fusion weights, or production rerank settings. Rerank-budget changes are deferred.

Replay existing evidence without credentials:

```bash
uv run python -m eval.search_pipeline --domain iwiki-mcp --out replace-with-report-dir --pareto --replay-evidence replace-with-evidence.json
```

After the replay passes, obtain operator confirmation before running a live benchmark. The live command uses an operator-created environment file; do not read or copy its credentials into the repository:

```bash
uv run python -m eval.search_pipeline --domain iwiki-mcp --out replace-with-report-dir --modes hybrid,lexical,semantic --pareto --env-file replace-with-operator-env-file
```

### Hard-negative gate

The bounded fusion decision derives hard-negative activation from the captured
baseline. It evaluates two reviewed hard-negative contracts; each is reported as
`active`, `unavailable`, or `invalid` from that baseline. A candidate can pass the
hard-negative gate only when at least two contracts are active.

`hard_negative_evidence_invalid` means one or more reviewed contracts have invalid
baseline evidence. `hard_negative_evidence_incomplete` means the evidence is valid
but fewer than two contracts are active. These diagnostic outcomes are distinct from
a candidate quality rejection after the gate is evaluated. Absolute ranks remain
diagnostic only; production search behavior, fusion weights, and rerank settings are
unchanged.

### Requirements

iwiki-mcp requires an OpenAI-compatible embeddings endpoint. Set `IWIKI_LLM_BASE_URL` and `IWIKI_LLM_KEY` in the MCP client environment (see [Register in Claude Code](#register-in-claude-code) / [Register in Codex](#register-in-codex)).

The MCP client spawns `iwiki-mcp` over stdio at session start. It is not a daemon; it lives for the client session. Before opening MCP stdio, normal startup sends one minimal request to the configured embeddings endpoint, with a 10-second timeout and no retries. Missing or invalid configuration, an unavailable endpoint, or an invalid response blocks startup and prints an actionable diagnostic to stderr; any literal configured API key in diagnostic values is redacted. `iwiki-mcp --help` remains offline and does not run the probe.

## Register in Claude Code

Step by step:

1. **Confirm the executable resolves.** `iwiki-mcp --help` should print usage. If not, the global install did not land on `PATH` — reinstall (`uv tool install .`) or use `uv run iwiki-mcp` as the command.
2. **Register the server.** Either run the CLI from the project root:

   ```bash
   claude mcp add iwiki \
     --env IWIKI_LLM_BASE_URL=https://.../v1 \
     --env IWIKI_LLM_KEY=... \
     --env IWIKI_BASE_DIR=/home/user/wiki \
     -- iwiki-mcp
   ```

   or add the same block to `.mcp.json` in the project root by hand:

   ```json
   {
     "mcpServers": {
       "iwiki": {
         "command": "iwiki-mcp",
         "env": {
           "IWIKI_LLM_BASE_URL": "https://.../v1",
           "IWIKI_LLM_KEY": "...",
           "IWIKI_BASE_DIR": "/home/user/wiki"
         }
       }
     }
   }
   ```

3. **Verify.** Run `claude mcp list` — `iwiki` should show as connected. Inside a session, `/mcp` lists the `wiki_*` tools.
4. **Keep secrets out of git.** Put `IWIKI_LLM_KEY` (and usually `IWIKI_LLM_BASE_URL`) in a user-level or `.local` config, not in a committed `.mcp.json`.

The client launches the server with `cwd` at the project root, so `.iwiki.toml` (see [Bind a project](#bind-a-project)) is picked up automatically.

## Register in Codex

Step by step:

1. **Confirm the executable resolves:** `iwiki-mcp --help`.
2. **Add the server** to `~/.codex/config.toml`:

   ```toml
   [mcp_servers.iwiki]
   command = "iwiki-mcp"
   env = { IWIKI_LLM_BASE_URL = "https://.../v1", IWIKI_LLM_KEY = "...", IWIKI_BASE_DIR = "/home/user/wiki" }
   ```

   To run from a source checkout instead of a global install, use `command = "uv"` with `args = ["run", "iwiki-mcp", "--project", "/abs/path/to/project"]`.
3. **Restart Codex** so it re-reads `config.toml`, then start a session in the project. The `wiki_*` tools become available.

Codex does not set the server `cwd` to your project, so pass `iwiki-mcp --project /abs/path/to/project` (or set `IWIKI_PROJECT_DIR` in `env`) when the project root differs from where Codex launches — that is how `.iwiki.toml` is resolved.

## The base and domains

`IWIKI_BASE_DIR` points at the shared wiki base. The base is intended to be a git repository, so writes can be committed and synced between machines or projects.

Each domain is a subdirectory under the base. A page's identity is its domain-relative `<type>/<slug>` path: `wiki_write_page` places the file under a directory named for its (resolved) frontmatter `type`, and that same `<type>/<slug>` value — without the `.md` suffix — is what `wiki_list_pages` returns and what `wiki_read_page` / `wiki_update_page` / `wiki_delete_page` expect as `slug`. Each domain's portable vector store (`index.jsonl`) and ingest log (`log.jsonl`) live at the domain root; a legacy `.iwiki/index.jsonl` / `.iwiki/log.jsonl` domain is migrated to the root automatically the first time any tool touches it. The base-local `.iwiki/graph.sqlite3` is a separate rebuildable SQLite cache, excluded from Git alongside its WAL/SHM files. The base also keeps `.iwiki/lock` for the cross-process git lock.

```text
/home/user/wiki/
  .iwiki/
    graph.sqlite3        # local derived cache, not committed
    lock
  backend/
    architecture/
      auth.md
    guide/
      onboarding.md
    index.jsonl
    log.jsonl
  frontend/
    concept/
      routing.md
    index.jsonl
    log.jsonl
```

Use one base across projects. Bind each project to the domains it should read from and the domain it should write to.

## Graph cache and links

The graph cache stores directed page links and heading anchors in SQLite, but search walks it as a bounded undirected neighbourhood inside the visible read scope. It never mixes code search with wiki dependencies. After clone, pull, corruption, or a fingerprint mismatch, the server rebuilds the affected local cache from Markdown without embedding calls; while unavailable it safely falls back to Markdown traversal. A graph-refresh failure never discards the committed Markdown, `index.jsonl`, or `log.jsonl` mutation: affected domains are marked `dirty` and a fingerprint-checked Markdown fallback remains authoritative until local repair succeeds.

Use relative Markdown links within one domain: `[Auth](architecture/auth.md#flow)`. For a page in another visible domain use the canonical URI: `[Routing](iwiki://frontend/concept/routing#flow)`. Root `index.md` and `log.md` are generated OKF artifacts, never graph pages or traversal targets; `wiki_lint` reports an authored link to either as `reserved_target`.

## Bind a project

The server resolves project binding from `.iwiki.toml` in the project root. The client normally starts the server with `cwd` set to the project root; override that with `IWIKI_PROJECT_DIR` or `iwiki-mcp --project DIR`.

When `.iwiki.toml` is missing or contains only whitespace, the server creates a
commented template with Git, PostgreSQL, and `code_graph` examples. It does the
same for `.iwikiignore`, whose template covers secrets and common project noise
and is optionally extended with the current `.gitignore`. Once either file has
non-whitespace content, server operations leave its bytes unchanged. Edit both
files manually after initialization.

```toml
# .iwiki.toml
read = ["backend", "frontend"]
write = ["backend", "frontend"]
primary = "backend"
# base = "/home/user/wiki"
```

`read` controls the default project search scope. To read from **every** domain in the base, set `read = []` or omit the line entirely — an empty or absent `read` falls back to all domains. `read = ["all"]` is **not** a wildcard; it is treated as a literal domain named `all`. `write` is the list of domains mutating tools may change. `primary` selects the default target for tools such as `wiki_index` without a `domain` argument and must belong to `write`. Every write domain must also belong to `read`. `base` is optional and overrides `IWIKI_BASE_DIR` for this project.

For Git storage, `wiki_bind` does not write project configuration. An attempted
automatic binding change returns a controlled response and leaves the file
unchanged:

```json
{"error":"project configuration cannot be changed automatically","code":"project_config_manual_edit_required","hint":"edit .iwiki.toml manually; populated configuration is never rewritten automatically"}
```

PostgreSQL `wiki_bind` remains session-only: it may narrow the configured maximum
scope. In a hosted HTTP session it can also carry the project's
`[specifications].mode` as `specification_mode`; local PostgreSQL stdio rejects that
parameter. It never changes `.iwiki.toml` or persists the mode. `wiki_create_domain` may bootstrap an empty
missing Git domain outside the current write list; it creates no page, index, or
log. Add that domain to `.iwiki.toml` manually before writing to it.

## Given-When-Then specifications

Ordinary Wiki pages and explicit `type: specification` pages coexist in every domain.
Ordinary Wiki pages keep their existing write, index, search, and lint behavior in all
specification modes and never require a code graph.

Local Git and local PostgreSQL read the project policy from `.iwiki.toml`:

```toml
[specifications]
mode = "optional"
```

Hosted PostgreSQL reads operator policy from server TOML:

```toml
[specifications]
default_mode = "optional"
allow_project_mode = true

[[specifications.overrides]]
iwiki_id = "team-wiki"
domain = "payments"
mode = "strict"
```

Local policy applies to all visible domains. A hosted client passes the project value
through optional `wiki_bind.specification_mode`. Hosted precedence is exact
`(iwiki_id, domain)` override, project mode, hosted default, then built-in `optional`.
The project tier applies only when it is at least as strict as the hosted default and
`allow_project_mode` is true; otherwise `wiki_status` reports
`project_mode_suppressed: true`. The value is session-scoped and resolved separately for
each bound domain. `disabled` stores specification pages as ordinary
Markdown and disables semantic tools. `optional` stores Markdown, reports advisory
findings, and projects only valid, complete, unique scenarios. `strict` rejects an
invalid target specification mutation before page or projection changes.

Each scenario is one fenced TOML block inside its H2 section:

```iwiki-gwt
id = "confirm-account-opening"
title = "Confirm account opening"
given = [{ role = "event", name = "AccountOpeningRequested" }]
when = { role = "command", name = "ConfirmAccountOpening" }
then = [{ role = "event", name = "AccountOpened" }]
code = [
  { relation = "implements", phase = "when", symbol = "accounts.Account.confirm" },
  { relation = "verifies", symbol = "tests.accounts.test_confirm_account_opening" }
]
```

`given` roles are `event`, `state`, or `fact`; `when` roles are `command`, `request`,
or `action`; `then` roles are `event`, `response`, `outcome`, or an exclusive
`exception`. A complete scenario has at least one `implements` and one `verifies`
binding. Its ID remains stable while wording or locations change without changing
observable behavior.

Grammar is closed and bounded. `id` is required, contains 1-128 UTF-8 bytes, and
matches `[a-z0-9]+(?:-[a-z0-9]+)*`. `title` is required and nonblank, contains no NUL,
and is at most 250 Unicode code points. Every phase-item `name` is required and
nonblank, contains no NUL, and is at most 1,024 UTF-8 bytes. Unknown keys at the top
level, in phase items, or in bindings are invalid; malformed TOML and duplicate TOML
keys are invalid.

`given` is required and accepts 0 or more items. `when` is required and contains
exactly one item. `then` is required and contains 1 or more items. `code` is required
and contains 1 or more bindings. In each binding, `phase` is optional and exactly one
of `symbol`, `file`, or `source_glob` is required. Completeness requires at least one
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

The mode matrix also governs existing invalid content. Disabled mode produces no
projection and no specification findings for missing, invalid, duplicate, or incomplete
specification pages. Optional mode makes every specification finding advisory. In
strict mode, syntax (`missing_scenario` and `invalid_scenario`),
`duplicate_scenario_id`, and `incomplete_bindings` findings are blocking only for future
mutations of the reported explicit specification page. Projection and resolution
findings remain advisory; ordinary Wiki pages remain unaffected in every mode.

The exact semantic tool surface is:

- `wiki_spec_search`: search projected scenarios inside the caller's read scope.
- `wiki_spec_context`: read one scenario plus persisted evidence and freshness without
  resolving or mutating it.
- `wiki_spec_resolve`: resolve declared bindings and persist sanitized evidence; it
  requires write scope and never expands binding or code-publication authority. Hosted,
  its `domain` must be the bound primary. A refusal names which of the caller's own
  binding relations answered — `invalid_domain`, `primary_not_selected`,
  `primary_not_writable`, or `not_bound_primary` — so a scenario living outside the
  bound primary is not mistaken for a missing grant.

`wiki_status` reports effective mode and projection state. `wiki_lint` adds independent
specification findings without hiding ordinary Wiki findings. Git stores the canonical
Markdown plus tracked, rebuildable `<domain>/specifications.jsonl`; PostgreSQL stores the
same logical projection transactionally under tenant/domain authorization. Markdown
remains canonical in both backends.

The `wiki_status` specification record contains `domain`, `mode`, `source`,
`projection_state`, `scenarios`, and `bindings`, plus
`project_mode_suppressed: true` when a received project mode is disabled by the server
switch or rejected by the tighten-only guard. Source is
`project | hosted_default | hosted_override | built_in_default`; projection state is
`disabled | absent | ready | stale | failed`. The complete `wiki_lint` finding taxonomy
is `missing_scenario`, `invalid_scenario`, `duplicate_scenario_id`,
`incomplete_bindings`, `projection_stale`, `projection_failed`, `binding_unresolved`,
`binding_ambiguous`, `resolution_not_checked`, `resolution_stale_spec`,
`resolution_stale_graph`, and `graph_unavailable`. Lint stays read-only and retains the
complete ordinary Wiki report.

Resolution evidence records `resolved`, `ambiguous`, `unresolved`, or
`graph_unavailable`. Context computes `fresh`, `stale_spec`, or `stale_graph` against
current specification and graph revisions. If the graph is absent or unusable, preserve
selectors and continue with repository search and executable tests; never block ordinary
Wiki work. Record the actual test command, exit status, and repository revision separately
because a `verifies` binding identifies a test but does not run it.

Run the deterministic, non-timing-gated path measurement with:

```bash
uv run pytest -q -m measurement tests/measurement/test_specification_paths.py -s
```

## Teach the agent to use iwiki

Registering the server exposes the tools, but the agent still needs instructions on *when* to call them. The repo ships ready-made snippets in [`templates/`](templates):

- `templates/CLAUDE.md.snippet` — append to the project's `CLAUDE.md` (Claude Code).
- `templates/AGENTS.md.snippet` — append to the project's `AGENTS.md` (Codex).

Both carry the same guidance: search before a task, do not mutate binding during ordinary startup, author pages after functionality changes, and `wiki_sync` at end of session. Append the matching snippet once per project:

```bash
cat templates/CLAUDE.md.snippet >> CLAUDE.md   # Claude Code
cat templates/AGENTS.md.snippet >> AGENTS.md   # Codex
```

The snippets reference `.iwiki.toml`, so bind the project (above) first.

## Env reference

**Required**

| Variable | Default | Meaning |
|---|---|---|
| `IWIKI_LLM_BASE_URL` | none | Base URL for an OpenAI-compatible embeddings endpoint, usually ending in `/v1`. |
| `IWIKI_LLM_KEY` | none | API key for the embeddings endpoint. |

**Embedding model**

| Variable | Default | Meaning |
|---|---|---|
| `IWIKI_EMBED_MODEL` | `text-embedding-3-small` | Embedding model name. |
| `IWIKI_EMBED_DIMENSIONS` | `1536` | Vector size. Must match the configured embedding model. |

**Chat model**

| Variable | Default | Meaning |
|---|---|---|
| `IWIKI_CHAT_MODEL` | empty | Optional chat model name for server-side `type`/`tags` classification. Reuses `IWIKI_LLM_BASE_URL` and `IWIKI_LLM_KEY`. When unset, frontmatter defaults to `type="concept"` with no tags. |

**Server lifecycle**

| Variable | Default | Meaning |
|---|---|---|
| `IWIKI_IDLE_TIMEOUT_SECONDS` | `1800` | End a stdio MCP process after this many seconds with no incoming MCP activity. `0` disables the limit. Active tool calls are allowed to finish. A client that needs the server later must reconnect or start a new MCP process. |

**Search tuning**

| Variable | Default | Meaning |
|---|---|---|
| `IWIKI_TOP_K` | `8` | Default maximum results for search and related-section lookup. |
| `IWIKI_SCORE_THRESHOLD` | `0.2` | Default minimum vector similarity for a returned section hit. |
| `IWIKI_SEARCH_MODE` | `hybrid` | Omitted `wiki_search.mode` default. Values are `hybrid`, `lexical`, or `semantic`; whitespace/case are normalized and an explicit mode wins. |
| `IWIKI_RERANK_MODEL` | empty | Optional LiteLLM-compatible reranker model. Reuses `IWIKI_LLM_BASE_URL` / `IWIKI_LLM_KEY`, scores one full candidate batch with a 60-second timeout, limits only the provider response rows to the final result count, and fails soft with sanitized metadata. |
| `IWIKI_GRAPH_DEPTH` | `2` | Wiki-link hop depth for the retrieval graph-expansion and related-section lookup. |
| `IWIKI_SEED_TOP_K` | `5` | How many articles the summary-vector pass seeds before graph expansion. |
| `IWIKI_BFS_TOP_K` | `10` | Cap on graph-expanded (non-seed) articles added to the candidate pool. |
| `IWIKI_SEED_THRESHOLD` | `0.15` | Minimum summary-vector similarity for an article to seed the search. |
| `IWIKI_WRITE_SEED_THRESHOLD` | `0.35` | Minimum summary-vector similarity to seed the precise write-target locate path used by `wiki_search(intent="write")`. Higher than `IWIKI_SEED_THRESHOLD` so an unrelated page is not offered as an upsert target. |

**Indexing**

| Variable | Default | Meaning |
|---|---|---|
| `IWIKI_CHUNK_SIZE` | `512` | Target token count per indexed chunk. |
| `IWIKI_CHUNK_OVERLAP` | `64` | Token overlap between adjacent chunks. |
| `IWIKI_SUMMARY_MAX_CHARS` | `400` | Maximum page summary length. |

**Location**

| Variable | Default | Meaning |
|---|---|---|
| `IWIKI_BASE_DIR` | none | Shared wiki base directory. Can be overridden by `.iwiki.toml` `base`. |
| `IWIKI_PROJECT_DIR` | process `cwd` | Project directory used to read `.iwiki.toml`. Can be overridden with `--project DIR`. |

## Tools

| Tool | What it does |
|---|---|
| `wiki_search` | Read modes are exactly `hybrid`, `lexical`, and `semantic`; an explicit mode overrides `IWIKI_SEARCH_MODE` (default `hybrid`), while `vector` is rejected as a public mode. Semantic page descriptions, lexical page matches, graph pages, global semantic chunks, and lexical sections are ranked independently and fused with RRF before final top-k. Results contain `hit` (`semantic`/`lexical`/`both`) and `source` (`seed`/`graph`/`global`/`lexical`). When `IWIKI_RERANK_MODEL` is set, exact current chunks from the full candidate ceiling are sent in one authenticated 60-second LiteLLM batch, while provider `top_n` is limited to requested final `k`; failure preserves preliminary order and returns only sanitized `rerank` metadata. `scope`, `domains`, `k`, `threshold`, `type`, and `tags` constrain read search. `intent="write"` remains the isolated summary-vector write-target lookup and ignores read mode/reranking. |
| `wiki_read_page` | Read one Markdown page by domain and slug. With `heading`, return only that one `##` section (including its `section_hash`) instead of the whole page. |
| `wiki_list_pages` | List page slugs and files in a domain. |
| `wiki_related` | Return related sections for a section id within one domain; its `{"vector": [], "graph": []}` shape and domain-local fallback stay unchanged. |
| `wiki_write_page` | Validate and write a new page, index the domain, commit and push. |
| `wiki_update_page` | Update one existing page: section-only (`heading` + `new_body`), code-only (`code`), or combined atomically. With `new_heading`, rename that heading and atomically rewrite exact visible incoming links when their domains are writable. Accepts `expected_section_hash` for optimistic concurrency. |
| `wiki_insert_section` | Insert one new `##` section (positioned with `after_heading` / `before_heading`) without rewriting the rest of the page. |
| `wiki_delete_section` | Delete one existing `##` section without rewriting the rest of the page. Accepts `expected_section_hash`. |
| `wiki_move_section` | Reorder one existing `##` section (positioned with `after_heading` / `before_heading`) without rewriting its body. Accepts `expected_section_hash`. |
| `wiki_delete_page` | Delete one page by domain and slug: remove the file, append a `delete` log op, reindex the domain, commit and push. Rolls back on failure. |
| `wiki_index` | Rebuild one domain index (defaulting to the bound write domain when omitted), commit and push. |
| `wiki_list_domains` | List visible domain directories in the base with index sizes. |
| `wiki_create_domain` | Create an empty domain directory and return whether the base auto-commit succeeded; the domain's `index.jsonl` / `log.jsonl` are created lazily at the domain root on first write or index. |
| `wiki_bind` | Narrow PostgreSQL scope and, in a hosted HTTP session, optionally carry project `specification_mode` (`disabled`, `optional`, or `strict`) for that session; local PostgreSQL stdio rejects this parameter, while Git configuration changes return `project_config_manual_edit_required` and must be made manually. |
| `wiki_status` | Show resolved base, project directory, read domains, write domain, and available domains. |
| `wiki_lint` | Read-only Markdown-authoritative health report: broken/reserved/unavailable-domain links, orphans, stale pages, `missing_source`, and section gaps, plus an independent per-domain SQLite graph parity report (`state`, fingerprint, pages, edges, anchors). It never creates or rebuilds the cache; non-ready or mismatched graph state includes a `wiki_index` remediation hint. |
| `wiki_remediation_plan` | Group current lint findings into read-only update/delete remediation actions. |
| `wiki_migrate_okf` | Backfill OKF frontmatter and normalize type-directory layout, autonomously with a chat model or as a review plan without one. |
| `wiki_apply_okf` | Apply reviewed OKF metadata and layout decisions; a type-directory move atomically rewrites exact visible incoming links. |
| `wiki_export_okf` | Run the deterministic in-place OKF conformance sweep and regenerate root `index.md` / `log.md`. |
| `wiki_sync` | Run `git pull --rebase` and `git push` in the base. |

`wiki_write_page` refuses to overwrite an existing page in v1. `wiki_update_page` has three modes: section-only requires paired `heading` and `new_body`; code-only uses `code` and preserves the page body byte-for-byte; combined atomically performs both. The published root JSON Schema stays a plain object with `domain` and `slug` root-required: client tool validation rejects a root combinator, so a root `anyOf` would make clients drop the tool. Runtime validation enforces the mutually exclusive operations instead and rejects partial, no-op, or unsafe selectors before mutation. `new_heading` is optional with a section update: it rewrites exact incoming relative links in the page domain and exact `iwiki://` links from visible read domains. A nonempty valid `code` mapping completely replaces selectors (`code.symbols`, `code.files`, `code.source_globs`); `{}` or all-empty lists clears them; omitted or `null` `code` preserves them on a section update. A code-only response omits `heading` and adds no fields; section and combined responses retain `heading`.

```json
{"domain":"engineering","slug":"api/auth","code":{"symbols":[{"qualified_name":"auth.login"}],"files":[],"source_globs":[]}}
{"domain":"engineering","slug":"api/auth","heading":"API","new_body":"Updated login contract.","code":{"source_globs":["src/auth/**/*.py"]}}
```

Git retains its existing freshness and strict-spec transaction, then reindexes, commits, and refreshes the graph once. PostgreSQL uses the current `expected_revision` CAS in one revision and transaction; unchanged chunks reuse embeddings. Republish makes Code-graph Wiki links current. `wiki_apply_okf` applies the same transaction only when a type change moves a page. `wiki_insert_section` and `wiki_delete_section` add or remove one `##` section, and `wiki_move_section` reorders one, all without rewriting the rest of the page. `wiki_update_page`, `wiki_delete_section`, and `wiki_move_section` accept `expected_section_hash` (from a prior `wiki_read_page(..., heading=...)`) for optimistic concurrency: a stale hash is rejected with `section_conflict` instead of silently overwriting a concurrent edit.

The cross-domain operation starts only when every discovered visible referrer is in `write`; a visible read-only referrer blocks before any Markdown changes. Hidden domains are not inspected or reported, and are never rewritten. Results include `transaction_id`, `rewritten_pages`, `affected_domains`, and `rewritten_links` in addition to normal write fields.

Each cross-domain operation holds the base mutation lock, stages only affected Markdown plus domain-root `index.jsonl` / `log.jsonl`, and creates one local commit carrying `Iwiki-Transaction: <id>`. Its fsynced local journal is `.iwiki/transactions/<id>` and advances `prepared` → `applied` → `committed` → `finalized`. A pre-commit interruption restores snapshots; a post-commit interruption repairs/marks the derived graph and finalizes the journal before another overlapping mutation. Ambiguous recovery returns `manual_recovery_required`. Push remains fail-soft: a local commit and authoritative portable files are retained if publication fails.

`wiki_lint` reports `missing_source` pages whose ingest source has disappeared. Remove such a stale page explicitly with `wiki_delete_page` after confirming with the user; `wiki_sync` then propagates the deletion to the remote like any other commit.

Project-relative stale-source resolution remains a separate audited follow-up; this
release does not silently change that behavior.

The server also exposes the MCP resource `iwiki://authoring-rules` for page-structure rules.

## Pareto benchmark

Run the evaluation-only Pareto experiment against a live, labeled search corpus:

```bash
uv run python -m eval.search_pipeline --domain iwiki-mcp --out ./pareto-evidence --env-file /path/to/operator.env --pareto
```

`--env-file` reads an operator-created environment file for that process only; it does
not create, modify, or write credentials back to the file. Keep the file outside the
report output directory and out of version control. The reports record sanitized
evidence only.

`--pareto` is an evaluation command, not a production configuration switch. Production
fusion weights or rerank batch constants are applied only after the report contains a
passing recommendation for the corresponding quality and latency gates. A
`needs_work` decision, including `no_passing_weight_map`, leaves production retrieval
behavior unchanged.

## OKF compatibility

Every page carries a small YAML frontmatter block above the `# Title` H1, written automatically by `wiki_write_page` / `wiki_update_page` / `wiki_apply_okf`. Fields:

| Field | Meaning |
|---|---|
| `type` | Required. **Open** vocabulary: prefer `architecture`, `api`, `guide`, `reference`, `runbook`, `concept` (default), but any value is accepted (e.g. `person`); off-list values get only an advisory `unknown_type`. Also the page's directory: `wiki_write_page` places the file at `<type>/<slug>.md` under the domain root — a bare `slug` is prefixed with the resolved `type`, and a `slug` that already carries a leading segment must match it. |
| `title` | Derived from the page's `# Title` H1. |
| `description` | The authored article summary — the single source of the summary, stored as a separate summary vector for page seeding. It is never prefixed to section vectors and is stored in full (never truncated). Falls back to a `## Overview` section only transitionally (migration). |
| `resource` | The `source` passed to the write tool, if any; `wiki_apply_okf` and `wiki_migrate_okf` fall back to the page's last logged ingest source when none is given. The stored path is project-relative — an absolute path under the project is relativized, and any path (absolute or relative, e.g. `../../etc/hosts`) that resolves outside the project is rejected. |
| `tags` | Lowercase kebab-case labels, at most 5 per page. |
| `status` | Optional iwiki extension: `stub` (default), `developing`, `stable`, `deprecated`. |
| `timestamp` | On create (`wiki_write_page`, `wiki_apply_okf`, `wiki_migrate_okf`): the page file's last git-commit date, or today's date if not yet committed. On edit (`wiki_update_page`): always today's date. |

The reserved OKF files `index.md` (navigation) and `log.md` (history) are export-only: `wiki_write_page` / `wiki_update_page` / `wiki_delete_page` no longer regenerate them on every change. Run `wiki_export_okf` to (re)generate current `index.md` / `log.md` in the domain root before treating the domain as a complete OKF bundle for an external consumer. `index`/`log` stay reserved only at the domain **root**, and `wiki_write_page` rejects those two full identities specifically — a type-dir slug like `concept/index` is a distinct, ordinary page and is allowed.

Pages no longer carry a `## Overview` section: the summary lives in `description`.
Relationship links go in two reserved `##` sections — `## Outgoing links` (Markdown
links) and `## External links` (bare URLs) — which are excluded from the search index
but still feed the link graph. Run `wiki_export_okf` once to migrate legacy pages
(it strips `## Overview`, backfills `description`, and defaults `status`).

`type` and `tags` are resolved with this precedence: an **explicit** `type`/`tags` argument on the write tool wins; otherwise, when `IWIKI_CHAT_MODEL` is set, the server classifies the page body with that chat model; otherwise it defaults to `type="concept"` with no tags.

Faceted search narrows `wiki_search` to a `type` and/or a set of `tags`; the query values are normalized the same way as stored frontmatter (case-insensitive `type`, kebab-case `tags`), so `type="API"` still matches a page whose frontmatter says `type: api`:

```text
wiki_search(query="deploy steps", type="runbook", tags=["ci"])
```

Tools for adopting OKF frontmatter on an existing domain:

| Tool | What it does |
|---|---|
| `wiki_migrate_okf(domain=None)` | Backfill frontmatter for every page missing it. Dual-mode: **autonomous** (writes frontmatter directly) when `IWIKI_CHAT_MODEL` is set; otherwise returns a **plan** — a list of candidates with derived title/description/timestamp and the domain's existing tag vocabulary — for the calling agent to classify and apply. In autonomous mode, each page's `resource` falls back to its last logged ingest source, and tags coined for one page are reused as vocabulary for later pages in the same run. In both modes it also deterministically moves any flat page (a bare `<slug>.md` at the domain root) that already carries a frontmatter `type` under `<type>/<slug>.md`, rewriting intra-domain links; a page whose move target already exists is skipped and reported under `layout_collisions` instead of being clobbered, and a page whose frontmatter `type` doesn't resolve to a safe single path segment (e.g. contains `/` or `..`) is left in place and reported under `layout_skipped_unsafe`. |
| `wiki_apply_okf(domain, slug, type, tags)` | Apply agent-classified `type`/`tags` (plus derived fields) as frontmatter to one page, reindex, commit and push. Omitting `tags` preserves the page's existing tags instead of clearing them; the existing `description` and `status` are always carried over unchanged. |
| `wiki_export_okf(domain=None)` | Whole-domain, in-place OKF conformance sweep (no copy, no `dest`): converts any residual `[[wikilink]]` to Markdown links and guarantees frontmatter on every page (deterministic `type: concept` where missing; existing `type`/`tags` preserved), then regenerates the reserved `index.md` / `log.md`. Deterministic — never calls the chat model. Returns `fixed_links`, `added_frontmatter`, and `still_missing_frontmatter` / `still_legacy_wikilink`, with a `next_steps` hint to `wiki_migrate_okf` for better `type`/`tags`. The domain directory is itself the OKF bundle. It also migrates each page to the v2 body model: strips a `## Overview` section, backfilling `description` from it when empty, and defaults `status` to `stub`. |

`IWIKI_CHAT_MODEL` (default: empty) is optional; leaving it unset disables server-side classification and `wiki_migrate_okf` falls back to plan mode.

## Git sync of the base

When `IWIKI_BASE_DIR` is a git repository, every mutating tool — `wiki_write_page`, `wiki_update_page`, `wiki_create_domain`, and `wiki_index` — stages, commits, and pushes the base after successful changes (fail-soft: push errors are reported but do not roll back the write). Before writing, each mutating tool first fetches and fast-forwards the base when it is cleanly behind its remote, so the change lands on the current tip and the push is a fast-forward. If the base has genuinely diverged (local unpushed commits *and* the remote moved ahead), the tool refuses with `base diverged from remote` and a hint to run `wiki_sync` (or resolve the conflict in the base repo) before retrying — it does not stack another commit onto the divergence. If the base is not a git repo, the write or create still succeeds on disk and the tool response returns `committed: false`. Use `wiki_sync`, `wiki_status`, or git commands in the base repo to diagnose repository and remote setup.

Use `wiki_sync` to share the base:

```text
wiki_sync()
```

`wiki_sync` runs `git pull --rebase` and then `git push` in the base. Recoverable remote failures (`non_fast_forward`, `credential_unavailable`, and `transport_unavailable`) retry the standard Git pull/push path up to three sync attempts, with a 250 ms delay between attempts. Responses include `sync_attempts` and `push_attempts`; classified pull/push failures also include `failure_class`. That field can be absent for outcomes before a remote attempt, including a non-repository base, missing remote, or lock timeout. Failed pushes remain fail-soft warnings and preserve the local commit. The server does not change client Git configuration, source shell profiles, search for authentication sockets, or broker credentials.

Git runs non-interactively (`GIT_TERMINAL_PROMPT=0`, closed stdin), so credentials must already be available to the MCP server process through standard Git mechanisms. A credential helper configured in an interactive shell does not by itself prove that the MCP process can use it. If credentials are unavailable, configure a non-interactive helper for the server account and transport, launch the MCP server from an environment that already has the required credential context, or perform `wiki_sync` from a trusted terminal with that context. Do not put tokens, passwords, remote URLs with embedded credentials, or authentication socket paths in MCP configuration or logs.

If `pull --rebase` conflicts, `wiki_sync` aborts the rebase and returns `conflict: true`, `failure_class: rebase_conflict`, attempt metadata, and a hint. Conflicts are never retried automatically: resolve them manually in the base repo. If generated index files are involved, regenerate the affected domain indexes with `wiki_index`, commit the regenerated files in the base repo if needed, then run `wiki_sync` again.

## Quick start

1. Install `iwiki-mcp` and register it in Claude Code or Codex with `IWIKI_LLM_BASE_URL`, `IWIKI_LLM_KEY`, and `IWIKI_BASE_DIR`.
2. In the agent session, create a domain:

```text
wiki_create_domain(name="backend")
```

3. Edit the initialized `.iwiki.toml` manually, then append the agent snippet (see [Teach the agent to use iwiki](#teach-the-agent-to-use-iwiki)):

```toml
read = ["backend"]
write = ["backend"]
primary = "backend"
```

4. Write the first page:

```text
wiki_write_page(
  domain="backend",
  slug="auth",
  markdown="# Auth\n\n## Purpose\nAuth verifies users and protects private routes.\n",
  description="Token authentication flow.",
  type="architecture"
)
```

This writes `backend/architecture/auth.md`; pass that same `architecture/auth` identity as `slug` to `wiki_read_page` / `wiki_update_page` / `wiki_delete_page`.

5. Search it:

```text
wiki_search(query="how does auth work?")
```

## Limitations (v1)

- Within one domain use `[Heading](<type>/<slug>.md#heading)`; across domains use `iwiki://<domain>/<page-id>#<anchor>`.
- `.iwiki/graph.sqlite3` is a local derived cache, not a portable vector/log replacement and not a code-dependency graph.
- Git storage uses numpy brute-force vector search over portable JSONL indexes;
  PostgreSQL storage uses tenant/domain-scoped pgvector cosine candidates before the
  shared lexical fusion, deduplication, and optional reranking stages.
- Staleness checks are project-local and depend on available source paths and ingest logs.
