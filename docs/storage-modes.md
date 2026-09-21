# Storage and transport modes

*Part of the [iwiki-mcp documentation](../README.md#documentation). Русская версия: [storage-modes.ru.md](storage-modes.ru.md).*

| Storage | stdio | Streamable HTTP |
| --- | --- | --- |
| Git directory | supported; default | unsupported |
| PostgreSQL | supported for one locally configured wiki | supported for hosted multi-wiki access |

## Local Git stdio

The existing local mode is unchanged:

```bash
export IWIKI_BASE_DIR=/srv/iwiki-base
iwiki-mcp --project /srv/project
```

## Local PostgreSQL stdio

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

## Hosted Streamable HTTP

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
the MCP session manager. `POST` requests are served statelessly — the middleware issues
and recognizes `mcp-session-id` itself — and `DELETE` is answered by the middleware with
`204`, releasing the session's binding.

## Supported application container

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
[deployment runbook](deployment.md) for configuration, HTTPS proxy routing,
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
does not expand an established session on its own. Only successful `wiki_create_domain`
provisioning expands the creator's current session; every other grant is selected
explicitly, because a hosted `wiki_bind` is authorized against the token's current grants
rather than against the session's current selection. Project initialization still owns local
`.iwiki.toml` and `.iwikiignore`; the hosted server creates PostgreSQL domain state but
never writes those project files.

## Session lifetime and binding provenance

A `wiki_bind` selection is **process-local and session-scoped**. It is keyed by
`mcp-session-id`, expires after 24 hours of inactivity, and does not survive a server
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
  even though it reports `state: ready` and `fresh: true`. `wiki_spec_search` and
  `wiki_search` called without `domains` take their search set from the bound read list
  and report the same warning for the same reason; naming `domains` explicitly never
  carries it. `wiki_search(intent="write")` prefers the bound primary over any named
  domain, so it always reports the warning under the fallback.
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

## PostgreSQL MCP tool contract

| PostgreSQL support | Tools |
| --- | --- |
| Supported | `wiki_status`, `wiki_list_domains`, `wiki_list_pages`, `wiki_read_page`, `wiki_search`, `wiki_related`, `wiki_write_page`, `wiki_update_page`, `wiki_insert_section`, `wiki_delete_section`, `wiki_move_section`, `wiki_delete_page`, `wiki_index`, `wiki_bind`, `wiki_lint` |
| Hosted PostgreSQL only | `wiki_create_domain`, `wiki_list_domain_grants`, `wiki_set_domain_grant`, `wiki_revoke_domain_grant` |
| Supported code graph | `wiki_code_status`, `wiki_code_search`, `wiki_code_context` |
| Hosted PostgreSQL only | `wiki_code_publish_begin`, `wiki_code_publish_batch`, `wiki_code_publish_finalize`, `wiki_code_publish_abort`, `wiki_code_refresh_links` |
| Local checkout only | `wiki_code_index` |
| Git only | `wiki_remediation_plan`, `wiki_migrate_okf`, `wiki_apply_okf`, `wiki_export_okf`, `wiki_sync` |

Git-only tools return
`{"error":"unsupported_storage","storage":"postgres","hint":"use this tool with Git storage"}`.
The three grant tools return `unsupported_transport` with actual `storage` and
`transport` outside hosted PostgreSQL. `wiki_create_domain(name)` requires
`can_create_domain`; it atomically creates the domain plus caller read/write and
`can_manage_grants` rows, returning `created`, `already_existed`, `domain`, and the
complete effective session scope. Exact retries are idempotent.

Bootstrap a project domain in this order: bind only the domains that already exist, call
`wiki_create_domain(name)` — which expands the current session with the new domain and
makes it the primary — then rebind the full project scope. Binding a scope that names a
domain the wiki has not provisioned yet is refused by the gate with
`reason: "domain_not_granted"`, because the authorization gate checks the requested scope
against the token's current grants and a domain that does not exist grants nothing. That
reason distinguishes a domain the caller may still create from a revoked grant. A token
without `can_create_domain` is refused with `reason: "domain_creation_not_allowed"`, and a
create-capable token naming a domain another token already owns gets the in-band
`{"error":"access_denied","reason":"domain_not_owned"}`: `can_create_domain` provisions a
new domain and never claims an existing one.

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
