---
review:
  spec_hash: dd1bf5508757639d
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
and focused tests and documentation. It does not add Git/PostgreSQL
synchronization, OAuth, an external identity provider, CRDTs, peer replication,
database creation, production DNS/TLS provisioning, or physical wiki deletion.

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

The HTTP server validates `Origin`, requires a Bearer token for every endpoint,
and exposes the existing MCP tools and resources through Streamable HTTP.

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
receives privileges only for the `iwiki` schema.

Acceptance criterion: configuration tests reject missing PostgreSQL fields,
missing local `iwiki_id`, unsupported storage types, and secret values in error
text; a valid configuration reaches only its configured wiki.

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
```

Hosted configuration contains no `iwiki_id`; authentication determines it. At
startup a PostgreSQL process obtains a migration lock and idempotently creates
or upgrades only objects in schema `iwiki`. It never creates a database and
does not alter objects outside that schema. A connection, configuration, or
migration failure prevents serving and emits a safe diagnostic without a
password or token.

Acceptance criterion: two concurrent startup attempts leave one valid
migration history; a new empty database receives only `iwiki` objects; an
existing unrelated schema is unchanged.

## 4. PostgreSQL Data Model

### R6. Shared Tables and Tenant Constraints

The `iwiki` schema contains these logical tables:

| Table | Purpose | Required tenant constraint |
|---|---|---|
| `schema_migrations` | ordered applied migration versions | global migration key |
| `iwikis` | `iwiki_id`, slug, active state, timestamps | unique wiki slug |
| `domains` | domain slugs inside a wiki | unique `(iwiki_id, slug)` |
| `pages` | Markdown, revision, timestamps | unique `(iwiki_id, domain_id, slug)` |
| `chunks` | section content, ordinal, embedding | page and `iwiki_id` match |
| `links` | outgoing wiki links | source page and `iwiki_id` match |
| `tokens` | digest, owner, lifecycle metadata | exactly one `iwiki_id` |
| `token_domain_grants` | read/write permission per domain | token and domain share `iwiki_id` |
| `git_imports` | completed source fingerprint per wiki | one source/import identity per wiki |

Every tenant-scoped table includes `iwiki_id`. Composite keys and foreign keys
must prevent cross-wiki references, instead of relying only on application
filters. Query builders always qualify table names with `iwiki`; they do not
use an untrusted `search_path`. `chunks` uses pgvector for embeddings and has
an index compatible with the existing vector retrieval behavior.

Acceptance criterion: database tests prove cross-wiki page, link, chunk, token,
and domain-grant references fail; search and graph queries for one `iwiki_id`
cannot return a row from another one.

### R7. Page Mutation and Optimistic Locking

PostgreSQL `wiki_read_page` returns a numeric `revision`. PostgreSQL
`wiki_update_page` and `wiki_delete_page` accept a required
`expected_revision`; the field is additive to the existing tool contract.
`wiki_write_page` atomically reports an already-existing page. A mutation
updates the page revision, replaces derived chunks and outgoing links, and
commits all of them in one transaction. The conditional page update uses the
expected revision; no affected row produces the stable error
`{error: "conflict", current_revision: <value>}` and rolls back all derived
work.

Git-mode tools keep their existing behavior. The added optional field does not
change their established request or response semantics.

Acceptance criterion: two PostgreSQL clients writing from the same revision
produce exactly one success and one unchanged conflict; a failed embedding,
chunk, or link operation leaves the former committed page and derived rows
intact.

### R8. Search and Graph Semantics

PostgreSQL indexing, search, related-section retrieval, and link graph queries
are backend implementations behind the current public tool surface. They first
scope by `iwiki_id`, then by the active allowed domains. They require no local
Markdown, `index.jsonl`, SQLite, or Git path. Response ordering and existing
documented request/response semantics remain compatible except for the
additive page revision in PostgreSQL page reads.

Acceptance criterion: migrated fixtures return their expected page and heading
from search and expected graph neighbours, while a disallowed or different-wiki
domain returns no data.

## 5. Authentication and Administration

### R9. Bearer Tokens and Domain ACL

Each token is a cryptographically random 256-bit secret shown once at creation.
PostgreSQL stores only its SHA-256 digest, token identifier, owner, `iwiki_id`,
creation time, revocation time, and last-use time. A token belongs to exactly
one wiki. It has at least one explicitly supplied read domain; every write
domain is also readable.

HTTP authentication derives `AuthContext(iwiki_id, read_domains,
write_domains)`. Invalid, absent, revoked, disabled-wiki, and malformed tokens
receive `401`. A valid token attempting a non-granted operation or domain
receives `403`. HTTP never accepts an `iwiki_id` supplied by a client.
`wiki_bind` may narrow the context for its session but cannot expand token
grants and never writes hosted server configuration.

Acceptance criterion: integration tests prove token revocation, disabled wiki,
domain read denial, domain write denial, and cross-wiki denial; token listings
contain neither plaintext nor digest.

### R10. Administrative CLI

The database administrator runs the following explicit commands using the
server configuration and database credentials:

```text
iwiki-mcp base create --slug personal
iwiki-mcp base list
iwiki-mcp base disable --iwiki personal
iwiki-mcp base enable --iwiki personal
iwiki-mcp base import-git --iwiki personal --path /srv/wiki
iwiki-mcp token create --iwiki personal --owner alice --read-domain docs --write-domain docs
iwiki-mcp token list --iwiki personal
iwiki-mcp token revoke --token <token-id>
```

`base create` creates an empty active wiki and no implicit domains or tokens.
`base disable` is reversible: it blocks every hosted token for that wiki while
retaining data; `base enable` restores valid non-revoked tokens. Physical
deletion is outside this release. `token create` requires explicit domain
grants and prints plaintext once. List commands print identifiers and safe
metadata only.

Acceptance criterion: CLI tests prove empty-base creation, no implicit access,
one-time token output, safe listing, revocation, disable/enable behavior, and
the absence of a deletion command.

### R11. Explicit Git Import

`base import-git` is the only Git-to-PostgreSQL migration path. It reads a
specified Git directory, validates source pages, chunks, and links, and imports
an empty target wiki in one transaction. A successful import records a source
fingerprint. Repeating that same source fingerprint is a no-op; a different
source into a non-empty wiki fails before mutation. Startup never imports or
synchronizes data between backends.

Acceptance criterion: an import fixture preserves page, heading, chunk, and
link counts; a repeated import is safe; a different-source retry preserves the
first imported state.

## 6. Error Model and Documentation

Configuration and startup errors fail before MCP traffic and name safe field
names or correction steps. PostgreSQL runtime errors return existing fail-soft
tool result structures with safe error codes and hints. Authentication failures
do not reveal token ownership, valid wiki identifiers, domain names, database
credentials, SQL text, or migration details. A revision conflict is an expected
tool result rather than an internal error.

Repository documentation describes local Git, local PostgreSQL, hosted HTTP,
reverse-proxy boundary, remote-client registration, token administration,
configuration examples, schema initialization, Git import, and operator-owned
production setup. Bound iwiki pages are updated after implementation through
the MCP write tools and checked with `wiki_lint`.

Acceptance criterion: test diagnostics contain no injected password or token;
README and localized setup documentation state every supported matrix row and
the invalid HTTP-plus-Git row.

## 7. Verification Strategy

Focused tests cover configuration parsing, migrations, schema isolation,
storage operations, optimistic locking, vector search, graph traversal, import,
tokens, ACL, CLI lifecycle, and HTTP MCP integration. Integration tests use a
temporary PostgreSQL service and deterministic embeddings; they do not require
the production endpoint, real credentials, DNS, or TLS certificates. Existing
Git tests remain unchanged except for additive coverage.

Before result reconciliation, run:

```bash
uv run pytest -q
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
  idempotent version records serialize schema changes.
- Git and PostgreSQL can diverge after migration. Import is explicit and
  records its source; automatic synchronization is intentionally absent.
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
