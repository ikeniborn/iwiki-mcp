---
review:
  spec_hash: 3585e175863f4ba5
  last_run: 2026-08-13
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-08-12-distributed-postgres-wiki-storage-intent.md
---
# Distributed PostgreSQL Wiki Storage Design

**Date:** 2026-08-13
**Status:** approved
**Intent:** `docs/superpowers/intents/2026-08-12-distributed-postgres-wiki-storage-intent.md`

## 1. Purpose and Scope

This design preserves the current autonomous Git-backed stdio workflow and adds
one PostgreSQL storage backend. The backend is available through either a local
stdio MCP process or a hosted Streamable HTTP MCP process. Storage and MCP
transport are independent choices: Git is supported only through stdio, while
PostgreSQL is supported through both transports.

One configured PostgreSQL database contains one `iwiki` schema. It holds shared
tenant-scoped tables keyed by `iwiki_id`, so a single hosted endpoint can serve
many independent wiki bases without duplicating a schema per wiki. A local
stdio PostgreSQL configuration names a fixed `iwiki_id`; an HTTP request derives
the id exclusively from its verified Bearer token.

This design adds PostgreSQL migrations, shared storage abstractions, token and
base administration commands, explicit Git import, Streamable HTTP serving,
an explicit PostgreSQL-to-Git rollback export, and focused tests and
documentation. It does not add continuous Git/PostgreSQL synchronization,
OAuth, an external identity provider, CRDTs, peer replication, database
creation, production DNS/TLS provisioning, or physical wiki deletion.

## 2. Architecture and Deployment

### R1. Storage and Transport Matrix

The server selects exactly one backend for a process. An absent storage type
keeps the present Git behavior. The supported combinations are:

| Transport | Storage | Wiki selection | Result |
|---|---|---|---|
| stdio | absent or `git` | existing project binding | current local Git workflow |
| stdio | `postgres` | configured immutable `iwiki_id` | local process over shared PostgreSQL |
| Streamable HTTP | `postgres` | verified Bearer token | hosted shared wiki |
| Streamable HTTP | absent or `git` | n/a | startup configuration error |

Every PostgreSQL read, write, search, graph lookup, resource read, and mutation
must use the same `iwiki_id`-scoped backend regardless of transport. Git-mode
handlers continue to use current Markdown, index, graph, and Git-sync paths;
they must not load PostgreSQL dependencies at runtime.

Acceptance criterion: focused tests exercise every row in the matrix, prove the
Git default remains usable, and prove that the invalid HTTP-plus-Git row serves
no MCP traffic.

### R2. Hosted Deployment Boundary

`iwiki-mcp` without a subcommand continues to start stdio. Hosted mode uses:

```bash
iwiki-mcp serve --transport streamable-http --config /etc/iwiki/server.toml
```

The process binds a loopback host and a configured port. A reverse proxy owns
public TLS and forwards `https://iwiki.ikeniborn.ru/mcp` to that loopback
listener. PostgreSQL remains private to the server network. A client on the
same host may use the public HTTPS endpoint or the loopback endpoint with a
Bearer token; the loopback listener is not exposed on public interfaces.

This split is the approved realization of the intent's TLS constraint: the
public hosted service is available only through HTTPS, while TLS termination is
owned by the reverse proxy and the application hop is loopback-only.

The HTTP server validates every supplied `Origin` against
`server.allowed_origins`, permits an absent Origin for non-browser MCP clients,
requires a Bearer token for every MCP request, and exposes the MCP tools and
resources through Streamable HTTP.

Acceptance criterion: HTTP integration tests prove the endpoint accepts an
authorized request, rejects an invalid Origin or missing token, and leaves the
database inaccessible from the test client except through the MCP process.

## 3. Configuration and Startup

### R3. Compatible Local Git Configuration

Existing `.iwiki.toml`, `IWIKI_BASE_DIR`, project binding, and stdio command
lines remain valid. If `[storage]` is absent, the server resolves the existing
Git base and performs no PostgreSQL configuration lookup.

Acceptance criterion: existing local Git acceptance and smoke scenarios pass
unchanged with no `[storage]` table.

### R4. Local PostgreSQL Configuration

A local project chooses shared PostgreSQL storage with this non-secret
configuration shape:

```toml
read = ["docs"]
write = ["docs"]
primary = "docs"

[storage]
type = "postgres"
host = "db.example.net"
port = 5432
database = "iwiki"
user = "iwiki_local"
sslmode = "verify-full"
iwiki_id = "personal"
```

`IWIKI_DB_PASSWORD` supplies the password. The database name is the explicit
target database. `iwiki_id` is required for local stdio PostgreSQL mode and is
fixed for the process lifetime; no MCP tool may switch it. The database role
receives privileges only for the `iwiki` schema. The local `read`, `write`, and
`primary` fields are the maximum domain scope inside that wiki. They are
required to be internally consistent; `wiki_bind` can narrow the process
session but cannot add a domain outside them.

Embedding and reranking settings are runtime process configuration, never
hard-coded PostgreSQL backend constants. The current deployment configures
`IWIKI_EMBED_MODEL=lemonade-embeddings-bge-m3-q8`,
`IWIKI_EMBED_DIMENSIONS=1024`, and
`IWIKI_RERANK_MODEL=lemonade-reranker-bge-reranker-v2-m3`; another instance may
select other compatible values before startup. The active embedding model and
dimension must match the selected database's storage metadata. The reranker
does not affect stored vectors and may be changed independently on restart.
`IWIKI_LLM_BASE_URL` and `IWIKI_LLM_KEY` remain server-side secrets; HTTP
clients never supply or receive them.

Acceptance criterion: configuration tests reject missing PostgreSQL fields,
missing local `iwiki_id` or domain scope, inconsistent scopes, unsupported
storage types, embedding metadata mismatches, and secret values in error text;
a valid configuration reaches only its configured wiki and domains.

### R5. Hosted Server Configuration and Schema Startup

Hosted mode reads a dedicated server config rather than a developer project
binding:

```toml
[storage]
type = "postgres"
host = "127.0.0.1"
port = 5432
database = "iwiki"
user = "iwiki_server"
sslmode = "verify-full"

[server]
host = "127.0.0.1"
port = 8080
allowed_origins = ["https://iwiki.ikeniborn.ru"]
pool_min_size = 1
pool_max_size = 10
statement_timeout_ms = 30000
lock_timeout_ms = 5000
```

Hosted configuration contains no `iwiki_id`; authentication determines it. At
startup a PostgreSQL process obtains a migration lock and idempotently creates
or upgrades only objects in schema `iwiki`. It never creates a database and
does not alter objects outside that schema. A connection, configuration, or
migration failure prevents serving and emits a safe diagnostic without a
password or token. Migrations are forward-only and transactional: a failed
migration rolls back, and a binary that sees a newer schema version refuses to
serve rather than attempting a downgrade.

Acceptance criterion: two concurrent startup attempts leave one valid
migration history; a new empty database receives only `iwiki` objects; an
existing unrelated schema is unchanged.

## 4. PostgreSQL Data Model

### R6. Shared Tables and Tenant Constraints

The `iwiki` schema contains these logical tables:

| Table | Purpose | Required tenant constraint |
|---|---|---|
| `schema_migrations` | ordered applied migration versions | global migration key |
| `storage_metadata` | immutable embedding model and dimension for this database | singleton global key |
| `iwikis` | `iwiki_id`, slug, active state, timestamps | unique wiki slug |
| `domains` | domain slugs inside a wiki | unique `(iwiki_id, slug)` |
| `pages` | Markdown, revision, timestamps | unique `(iwiki_id, domain_id, slug)` |
| `chunks` | section content, ordinal, quantization metadata, embedding | page and `iwiki_id` match |
| `links` | outgoing wiki links | source page and `iwiki_id` match |
| `tokens` | digest, owner, lifecycle metadata | exactly one `iwiki_id` |
| `token_domain_grants` | read/write permission per domain | token and domain share `iwiki_id` |
| `git_imports` | completed source fingerprint per wiki | one source/import identity per wiki |

Every tenant-scoped table includes `iwiki_id`. Composite keys and foreign keys
must prevent cross-wiki references, instead of relying only on application
filters. Query builders always qualify table names with `iwiki`; they do not
use an untrusted `search_path`.

`storage_metadata` records the embedding model and dimension supplied to the
process that initializes an empty database. The migration creates the vector
column, dimension constraint, and cosine index from that validated runtime
dimension; it contains no model name or fixed dimension constant. The
`chunks.embedding` pgvector value stores the dequantized result of the existing
int8 quantization step, preserving the current cosine scoring input. Subsequent
startup fails closed when runtime model or dimension differs from metadata.
Changing either for an existing database requires a future explicit
re-embedding migration; silently mixing embeddings from different models is
forbidden. A newly initialized database may use any supported runtime dimension.

Acceptance criterion: database tests prove cross-wiki page, link, chunk, token,
and domain-grant references fail; wrong-dimension vectors and runtime metadata
mismatches fail; search and graph queries for one `iwiki_id` cannot return a row
from another one.

### R7. Page Mutation and Optimistic Locking

PostgreSQL `wiki_read_page` returns a numeric `revision`. PostgreSQL
`wiki_update_page` and `wiki_delete_page` add the optional schema field
`expected_revision`, but require a non-null value at runtime in PostgreSQL mode.
Omission returns `{error: "expected_revision_required", hint: "read the page and retry with its revision"}` without mutation. Git mode continues to
ignore an omitted field and preserves its existing contract.
`wiki_write_page` atomically reports an already-existing page. A mutation
updates the page revision, replaces derived chunks and outgoing links, and
commits all of them in one transaction. The conditional page update uses the
expected revision; no affected row produces the stable error
`{error: "conflict", current_revision: <value>, hint: "read the page and retry against the current revision"}` and rolls back all derived work.

Git-mode tools keep their existing behavior. The added optional field does not
change their established request or response semantics.

Acceptance criterion: omission of `expected_revision` in PostgreSQL mode makes
no change; two PostgreSQL clients writing from the same revision produce
exactly one success and one unchanged conflict; a failed embedding, chunk, or
link operation leaves the former committed page and derived rows intact.

### R8. Search and Graph Semantics

PostgreSQL indexing, search, related-section retrieval, and link graph queries
are backend implementations behind the current public tool surface. They first
scope by `iwiki_id`, then by the active allowed domains. They require no local
Markdown, `index.jsonl`, SQLite, or Git path.

The PostgreSQL backend reuses the current Python lexical scoring, RRF fusion,
deduplication key, reranking, rounded score, and deterministic tie-break rules.
pgvector supplies tenant-scoped cosine candidates over the stored dequantized
vectors. Compatibility means the same response fields and stable ranking
contract, not a promise that two floating-point engines produce identical
scores for every arbitrary corpus. A shared deterministic fixture with
non-tied scores must produce the same normalized result order in Git and
PostgreSQL modes.

Acceptance criterion: the cross-backend fixture returns the same normalized
ordered hits, expected page and heading, and graph neighbours; a disallowed or
different-wiki domain returns no data.

### R8a. Complete MCP Tool Matrix

Git mode retains all currently registered tools. PostgreSQL mode supports
`wiki_status`, `wiki_list_domains`, `wiki_list_pages`, `wiki_read_page`,
`wiki_search`, `wiki_related`, `wiki_write_page`, `wiki_update_page`,
`wiki_delete_page`, `wiki_index`, `wiki_bind`, and `wiki_lint`.
`wiki_status` reports `storage`, `transport`, authorized `read`/`write` scope,
`primary`, and visible domains; it reports no DSN or database credentials.

PostgreSQL mode returns the stable error
`{error: "unsupported_storage", storage: "postgres", hint: <safe guidance>}`
for `wiki_code_status`, `wiki_code_index`, `wiki_code_search`,
`wiki_code_context`, `wiki_remediation_plan`, `wiki_migrate_okf`,
`wiki_apply_okf`, `wiki_export_okf`, `wiki_sync`, and `wiki_create_domain`.
Dispatch selects the backend before any `sync.ensure_fresh`, filesystem, Git,
JSONL, or SQLite call. PostgreSQL domains are created through the
administrative CLI described below.

Acceptance criterion: a table-driven test invokes every registered MCP tool in
both storage modes and proves either its supported behavior or its exact safe
unsupported response; PostgreSQL traces contain no Git, filesystem-index, or
SQLite access.

## 5. Authentication and Administration

### R9. Bearer Tokens and Domain ACL

Each token is a cryptographically random 256-bit secret shown once at creation.
PostgreSQL stores only its SHA-256 digest, token identifier, owner, `iwiki_id`,
creation time, revocation time, and last-use time. A token belongs to exactly
one wiki. It has at least one explicitly supplied read domain; every write
domain is also readable. To avoid a hot-row write on every MCP call,
`last_used_at` is refreshed at most once per five minutes per token.

HTTP authentication derives `AuthContext(iwiki_id, read_domains,
write_domains)`. Invalid, absent, revoked, disabled-wiki, and malformed tokens
receive `401`. A valid token attempting a non-granted operation or domain
receives `403`. HTTP never accepts an `iwiki_id` supplied by a client.
`wiki_bind` may narrow the context for its session but cannot expand token
grants and never writes hosted server configuration.

Acceptance criterion: integration tests prove token revocation, disabled wiki,
domain read denial, domain write denial, and cross-wiki denial; token listings
contain neither plaintext nor digest; repeated requests inside the throttle
window do not write the token row repeatedly.

### R10. Administrative CLI

The database administrator runs explicit commands using `--config` or
`IWIKI_SERVER_CONFIG` and server-side database credentials:

```text
iwiki-mcp base create --config /etc/iwiki/server.toml --iwiki personal
iwiki-mcp base list --config /etc/iwiki/server.toml --json
iwiki-mcp base show --config /etc/iwiki/server.toml --iwiki personal --json
iwiki-mcp base disable --config /etc/iwiki/server.toml --iwiki personal
iwiki-mcp base enable --config /etc/iwiki/server.toml --iwiki personal
iwiki-mcp domain create --config /etc/iwiki/server.toml --iwiki personal --domain docs
iwiki-mcp base import-git --config /etc/iwiki/server.toml --iwiki personal --path /srv/wiki --dry-run
iwiki-mcp base import-git --config /etc/iwiki/server.toml --iwiki personal --path /srv/wiki
iwiki-mcp base export-git --config /etc/iwiki/server.toml --iwiki personal --path /srv/export --dry-run
iwiki-mcp base export-git --config /etc/iwiki/server.toml --iwiki personal --path /srv/export
iwiki-mcp token create --config /etc/iwiki/server.toml --iwiki personal --owner alice --read-domain docs --write-domain docs
iwiki-mcp token list --config /etc/iwiki/server.toml --iwiki personal --json
iwiki-mcp token revoke --config /etc/iwiki/server.toml --token-id <token-id>
```

`base create` creates an empty active wiki and no implicit domains or tokens.
`base disable` is reversible: it blocks every hosted token for that wiki while
retaining data; `base enable` restores valid non-revoked tokens. Physical
deletion is outside this release. `token create` requires explicit domain
grants and prints plaintext once. List commands print identifiers and safe
metadata only. `domain create` is the only PostgreSQL domain-creation path;
HTTP tokens cannot create domains. `base show` reports active state and safe
domain/page/token counts. Administrative list/show/dry-run commands support
`--json`; `--token` is not accepted as an alias for `--token-id`.

`iwiki-mcp` without a subcommand alone accepts `--project` and starts stdio.
`serve`, `base`, `domain`, and `token` reject `--project` and use only server
configuration. `serve --transport` remains accepted for clarity and defaults
to its only v1 value, `streamable-http`.

Acceptance criterion: CLI tests prove empty-base creation, explicit domain
creation, no implicit access, one-time token output, safe human/JSON listing,
revocation by token id, disable/enable behavior, config-source precedence, and
the absence of a deletion command.

### R11. Explicit Git Import and Rollback Export

`base import-git` is the only Git-to-PostgreSQL migration path. It reads a
specified Git directory, validates source pages, chunks, and links, and imports
an empty target wiki in one transaction. A successful import records a source
fingerprint. Import checks a matching completed fingerprint first and returns a
no-op even though the prior import made the wiki non-empty; otherwise any
non-empty target fails before mutation. `--dry-run` validates and reports
domain, page, chunk, and link counts without database mutation. Startup never
imports or synchronizes data between backends.

`base export-git` is the explicit PostgreSQL-to-Git rollback path. It reads one
consistent PostgreSQL snapshot, requires an absent or empty destination, writes
the domain/page Markdown tree and a manifest with page hashes and counts,
initializes a Git repository, and creates one export commit. It exports no
tokens, token digests, grants, database credentials, embeddings, or derived
graph rows. Derived indexes are rebuilt by the existing local `wiki_index`
workflow after switching the client to the exported Git base. `--dry-run`
reports the same manifest without filesystem mutation. Repeating an export
requires a new empty destination; no force-overwrite option exists.

Operational disaster recovery additionally uses PostgreSQL-native encrypted
backup/restore of the `iwiki` schema. Those operator commands and retention are
documented but not executed by the application or test suite.

Acceptance criterion: an import fixture preserves page, heading, chunk, and
link counts; a repeated import is safe; a different-source retry preserves the
first imported state. Export/import round-trip tests prove page hashes and link
targets survive, the destination contains no secrets, the generated Git tree
is usable by local stdio after reindex, and dry runs mutate neither side.

## 6. Error Model and Documentation

Configuration and startup errors fail before MCP traffic and name safe field
names or correction steps. PostgreSQL runtime errors return existing fail-soft
tool result structures with safe error codes and hints. Authentication failures
do not reveal token ownership, valid wiki identifiers, domain names, database
credentials, SQL text, or migration details. A revision conflict is an expected
tool result rather than an internal error.

Repository documentation describes local Git, local PostgreSQL, hosted HTTP,
reverse-proxy boundary, remote-client registration, token administration,
configuration examples and current deployment values, server-side embedding credentials,
schema initialization, Git import/export rollback, PostgreSQL backup/restore,
and operator-owned production setup. Repository changes update `README.md`,
`docs/README.ru.md`, and `docs/architecture.md`; no nonexistent `docs/wiki/`
directory is introduced. Bound iwiki pages are updated after implementation
through the MCP write tools and checked with `wiki_lint`.

Acceptance criterion: test diagnostics contain no injected password or token;
README and localized setup documentation state every supported matrix row and
the invalid HTTP-plus-Git row.

## 7. Verification Strategy

Focused tests cover configuration parsing, migrations, schema isolation,
storage operations, optimistic locking, cross-backend ranking, graph traversal,
import/export, tokens, ACL, the complete MCP tool matrix, CLI lifecycle, and
HTTP MCP integration. Integration tests use a disposable PostgreSQL service
with pgvector selected only by `IWIKI_TEST_POSTGRES_DSN` and deterministic
embeddings; they do not require the production endpoint, real credentials,
DNS, or TLS certificates. Without that variable, PostgreSQL integration tests
are explicitly marked skipped and `uv run pytest -q` remains offline and green.
The fixture accepts only a database name ending in `_test` and rejects every
other DSN before schema mutation. Unit tests for configuration and pure logic
still run.

CI and result reconciliation have two required jobs: the default offline suite,
and the PostgreSQL integration suite with an explicitly provisioned disposable
DSN. A skipped integration suite is not acceptance evidence and keeps the
result gate incomplete.

Before result reconciliation, run:

```bash
env -u IWIKI_TEST_POSTGRES_DSN uv run pytest -q
test -n "${IWIKI_TEST_POSTGRES_DSN:?set IWIKI_TEST_POSTGRES_DSN to a disposable pgvector database}"
uv run pytest -q tests/postgres
uv run flake8 src tests
uv run iwiki-mcp --help
git diff --check
```

The plan adds exact focused test commands for each component. Deployment of a
production database, reverse proxy, DNS, TLS certificate, production role, or
production token remains a human checkpoint and is not performed by tests or
the implementation workflow.

## 8. Risks and Mitigations

- A tenant filter omission could expose another wiki. Composite tenant keys,
  foreign keys, mandatory backend context, and cross-wiki tests provide
  overlapping checks.
- A pooled database connection could retain tenant state. Storage uses explicit
  `iwiki_id` predicates and schema-qualified names rather than session
  `search_path` state.
- A token leak grants its permitted access until revocation. Tokens are high
  entropy, displayed once, stored only as digests, scoped to one wiki, and
  revocable without data loss.
- A migration can collide with a concurrent start. A migration lock and
  idempotent version records serialize schema changes; transactional rollback
  and a newer-schema refusal prevent implicit downgrade.
- An embedding model mismatch can make stored vectors invalid. Runtime
  configuration initializes database metadata and dimension-specific DDL;
  later startup refuses mismatches until an explicit future re-embedding
  migration exists. Reranking remains independent runtime configuration.
- Git and PostgreSQL can diverge after migration. Import is explicit and
  records its source; export creates a rollback snapshot; automatic
  synchronization is intentionally absent.
- Public HTTP can be misconfigured. The service binds loopback, the proxy owns
  TLS, and application authentication and Origin validation are mandatory.

## 9. Acceptance (from intent)

### Desired Outcomes

- A client selects one storage backend: `git` or `postgres`; an absent storage type preserves the existing Git-backed behavior.
- A local stdio server supports `git` storage through a configured Git repository directory, with no hosted-server or PostgreSQL dependency.
- A local stdio server supports `postgres` storage using configured address, port, target database name, user, TLS settings, and a runtime password secret.
- The hosted endpoint `https://iwiki.ikeniborn.ru/mcp` is a Streamable HTTP MCP server backed by PostgreSQL; `HTTP + git` is rejected at startup as an invalid configuration.
- Two independent authenticated HTTP MCP clients observe a committed page update without cloning, pulling, syncing a local base, or holding PostgreSQL credentials.
- Each personal Bearer token has `read_domains` and `write_domains`; `wiki_bind` can only narrow those grants.
- Search and link-graph queries return results from PostgreSQL without requiring local Markdown, `index.jsonl`, SQLite, or Git at runtime.
- A concurrent update to the same page returns a documented, deterministic optimistic-lock conflict rather than losing either client's data silently.
- A PostgreSQL-backed server starts against an empty selected database by creating its required schema idempotently.

### Done when

Done when local stdio passes the current Git-backed acceptance scenarios using an absent or explicit `git` type and passes PostgreSQL acceptance scenarios against a configured target database using explicit `postgres`; an empty target database receives only the idempotent iwiki schema initialization; two independent Bearer-authenticated HTTP MCP clients read and write one PostgreSQL-backed hosted wiki at the configured endpoint; HTTP plus Git is rejected deterministically; token domain grants and revocation are enforced; PostgreSQL concurrent-write conflicts are deterministic; every supported mode provides search and link-graph queries; and no supported mode requires another mode's storage dependencies at runtime.
