# PostgreSQL provisioning and least privilege

*Part of the [iwiki-mcp documentation](../README.md#documentation). Русская версия: [postgres-setup.ru.md](postgres-setup.ru.md).*

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
the [deployment runbook](deployment.md#out-of-band-schema-migration-and-principal-provisioning).
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

## Runtime principals for the code graph

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

## Schema v5 rollback and the compatibility artifact

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
