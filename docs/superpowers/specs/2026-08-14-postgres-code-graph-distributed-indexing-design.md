---
review:
  spec_hash: b9e1cbdffa84c6fa
  last_run: 2026-08-15
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings:
    - id: F-001
      phase: consistency
      severity: CRITICAL
      section: "4.1 Identity, scope, and compatibility"
      section_hash: 56a0ac04442e24ec
      fragment: "A direct runtime principal MUST NOT own protected tables"
      text: >-
        The prior design allowed the documented runtime principal to be the
        migration/table owner, which bypasses PostgreSQL row-level policies.
      fix: >-
        Require separate migrator, hosted service, and restricted direct roles;
        reject owner or BYPASSRLS credentials for direct mode.
      verdict: fixed
      verdict_at: 2026-08-15
    - id: F-002
      phase: consistency
      severity: CRITICAL
      section: "13. Migration and rollout"
      section_hash: 0971f8b8bd6e2849
      fragment: "Application rollback from schema 4 is an explicit operator procedure"
      text: >-
        The prior rollback text did not let a pre-v4 application pass its
        newer-schema guard after migration 4 changed policies on wiki tables.
      fix: >-
        Define and test an idempotent compatibility rollback that retains data
        and objects but removes the v4 migration marker under the migration lock.
      verdict: fixed
      verdict_at: 2026-08-15
    - id: F-003
      phase: clarity
      severity: CRITICAL
      section: "4.3 Sessions, concurrency, and atomic visibility"
      section_hash: a54ba96a715083d5
      fragment: "SET LOCAL lock_timeout = <configured milliseconds>"
      text: >-
        The prior design required a configurable wait but the plan selected
        pg_try_advisory_xact_lock, which never waits.
      fix: >-
        Specify blocking pg_advisory_xact_lock under transaction-local timeout
        and map only SQLSTATE 55P03 from lock acquisition to busy.
      verdict: fixed
      verdict_at: 2026-08-15
    - id: F-004
      phase: coverage
      severity: CRITICAL
      section: "7.3 PostgreSQL runtime scope"
      section_hash: 70e0407931b40c15
      fragment: "hosted service principal is a separate trusted application boundary"
      text: >-
        The hosted service principal has no defined RLS policy or principal-domain
        grants, so enabling the shared policies can hide all hosted Markdown and graph rows.
      fix: >-
        Define hosted service-role grants and policy behavior, including whether
        FORCE ROW LEVEL SECURITY is used and how hosted domain access is provisioned.
      verdict: fixed
      verdict_at: 2026-08-15
    - id: F-005
      phase: coverage
      severity: CRITICAL
      section: "4.3 Sessions, concurrency, and atomic visibility"
      section_hash: a54ba96a715083d5
      fragment: "After expiry or supersession, an old publisher MUST NOT append or finalize"
      text: >-
        The protocol has no resume, reattach, or supersession operation, so its
        fencing token never rotates after begin and adds no protection beyond session_id.
      fix: >-
        Either remove fencing and supersession from the first-release contract or
        define an ownership re-establishment operation that rotates the token atomically.
      verdict: fixed
      verdict_at: 2026-08-15
    - id: F-006
      phase: coverage
      severity: CRITICAL
      section: "13. Migration and rollout"
      section_hash: 0971f8b8bd6e2849
      fragment: "Only the existing admin migration command may apply schema changes"
      text: >-
        The rollout does not explicitly define startup behavior for both hosted HTTP
        and stdio direct PostgreSQL, while both currently run migrations at startup.
      fix: >-
        Define one runtime startup rule for both entry points and the separate
        operator migration workflow before changing either implementation.
      verdict: fixed
      verdict_at: 2026-08-15
chain:
  intent: docs/superpowers/intents/2026-08-14-postgres-code-graph-distributed-indexing-intent.md
  spec: null
---
# Distributed PostgreSQL Code Graph Design

**Date:** 2026-08-14
**Status:** approved
**Topic:** `postgres-code-graph-distributed-indexing`
**Intent:** `docs/superpowers/intents/2026-08-14-postgres-code-graph-distributed-indexing-intent.md`

## 1. Purpose

This design lets a machine that has a repository checkout build the existing Python code graph and publish one immutable snapshot to SQLite, directly to PostgreSQL, or through an authenticated remote MCP server. A server without the checkout can then answer code-graph queries from the active snapshot.

The three publication modes share one row-native, chunked protocol and one validation lifecycle. Storage and transport are adapters around that protocol, not separate indexing implementations. One bound wiki domain represents one repository and has at most one active code-graph snapshot. The domain's existing `iwiki_id`, `read`, `write`, and `primary` scope is also the code-graph scope.

## 2. Scope

### 2.1 In scope

- Local Python source discovery, extraction, resolution, and deterministic graph construction using the current indexer.
- Chunked publication to local SQLite, direct PostgreSQL, or remote MCP.
- Code-graph reads from local SQLite, direct PostgreSQL, or remote MCP.
- PostgreSQL schema and forward migration for immutable snapshots, staging sessions, batches, active pointers, and derived code-to-wiki links.
- Atomic publication, idempotent batch retry, session expiry, optimistic conflict detection, tenant/domain constraints, and bounded queries.
- Reuse of current wiki authentication and domain binding.
- Target-side derivation of code-to-wiki links from authoritative Markdown in the same domain.
- Configurable snapshot freshness with a 24-hour default.
- A first-release operating envelope of at most 20,000 indexed files.

### 2.2 Out of scope

- Uploading, storing, or serving repository source text from PostgreSQL or remote MCP.
- Server-side indexing when the server has no checkout.
- NATS, another broker, required PostgreSQL `LISTEN/NOTIFY`, background replication, or continuous synchronization.
- Multiple repositories in one domain, cross-domain code graphs, historical graph queries, graph merges, or last-writer-wins publication.
- Incremental indexing, non-Python extraction, source archives, blob snapshot protocols, or event-stream replay.
- Separate code-graph tokens, roles, ACLs, domain parameters, or identity models.
- Automatic fallback between SQLite, PostgreSQL, and MCP modes.

## 3. Acceptance (from intent)

The following outcomes and completion rule are copied verbatim from the approved intent.

### 3.1 Desired Outcomes

- A local indexer builds a graph from a local checkout and publishes it to configured SQLite, PostgreSQL, or an authenticated remote MCP publication endpoint.
- A PostgreSQL-backed remote MCP serves `wiki_code_search` and `wiki_code_context` from a ready published snapshot without access to the source checkout.
- Local consumers can use local SQLite, PostgreSQL, or a remote MCP endpoint according to explicit configuration.
- Missing, stale, or non-ready snapshots return a clear safe response and never return partial graph data.

### 3.2 Done when

- Done when: local publication and remote PostgreSQL querying work against the same ready snapshot; SQLite compatibility tests pass; configured freshness is visible in observed responses; and no query exposes source text or partial graph data.

## 4. Requirements

### 4.1 Identity, scope, and compatibility

- **R-001 — One domain, one repository:** Each `(iwiki_id, domain_id)` MUST identify exactly one repository and at most one active code-graph snapshot. Code-graph tools MUST use the bound `primary`; they MUST NOT accept a separate domain or repository argument. **Acceptance:** AC-01.
- **R-002 — Existing authorization:** Code-graph publication MUST require the bound `primary` in existing wiki write scope. Code-graph query MUST require it in existing wiki read scope. Remote MCP MUST derive `iwiki_id` and domain grants from the existing token. Direct PostgreSQL MUST use the existing restricted runtime principal's wiki-domain grant. The schema owner/migrator, hosted service principal, and direct runtime principal are distinct operational roles. The hosted service principal MUST receive explicit entries in the shared `database_principal_domain_grants` table for every domain served; bearer-token scope remains the application authorization boundary inside that database scope. PostgreSQL token creation MUST name the exact deployed service role through required `--hosted-principal ROLE` input. Before generating or storing token material, the admin path MUST verify that exact existing role is registered as `runtime=hosted`, is a non-owner without `BYPASSRLS`, and has read/write grants covering every requested token domain; another hosted role or an aggregate “some hosted role is ready” check is not a substitute. The command never creates a role or accepts its password. The operator MUST pass the role configured as the hosted server's `[storage].user`, and hosted startup independently validates its actual `session_user`. This separation and mapping are infrastructure, not graph-specific authorization. No code-graph token, role, ACL, or graph-specific grant table may be introduced. **Acceptance:** AC-02.
- **R-003 — Existing graph compatibility:** Published rows MUST preserve current schema-v2 entity identities, relation semantics, deterministic revision rules, and Python-only behavior. SQLite keeps `SCHEMA_VERSION = 2` and the five public entity tables. The new runtime MUST accept two exact schema-v2 profiles: the legacy five-table profile and a publication profile containing those unchanged tables plus the exact internal `code_graph_publication` table. No other extra table or index is accepted. Public row/query APIs MUST expose only the five entity tables. Existing Git/SQLite behavior and ordinary PostgreSQL wiki contracts MUST remain usable, but a pre-publication binary is not required to accept the extended publication profile; rollback uses a retained legacy snapshot or a reindex with the selected binary. **Acceptance:** AC-03.
- **R-004 — Tenant integrity:** Every PostgreSQL graph row MUST carry `iwiki_id`, `domain_id`, and `snapshot_id` where applicable. Composite keys and foreign keys MUST reject cross-wiki, cross-domain, and cross-snapshot references. Direct publication MUST use the same immutable local `iwiki_id`, `read`, `write`, and `primary` configuration as direct PostgreSQL Markdown access, and the local publisher MUST reject any requested scope outside it. PostgreSQL row-level security MUST be enabled, but not forced, on protected Markdown and graph tables. Policies MUST independently reject any hosted or direct runtime principal outside its shared wiki-domain grant. Runtime principals MUST NOT own protected tables, hold `BYPASSRLS`, run migrations, or receive database/schema `CREATE` solely for application rollback; only the non-runtime schema owner may use PostgreSQL's owner exemption. Because row-level security also governs `iwiki.domains`, a runtime principal MUST create a domain only through one reviewed `SECURITY DEFINER` function that inserts the domain and that caller's own runtime grant in a single statement; direct `INSERT` on `iwiki.domains` by a runtime principal MUST remain refused, and the function MUST reject a principal provisioned for a different wiki. Deployment and token issuance MUST stop if those properties, the exact hosted-principal identity, or required hosted/direct grants cannot be proven. **Acceptance:** AC-04.

### 4.2 Shared publication protocol

- **R-005 — One publisher lifecycle:** SQLite, direct PostgreSQL, and remote MCP publication MUST implement the same `SnapshotPublisher` lifecycle: `begin`, ordered `publish_batch`, `finalize`, and `abort`. Indexing and row serialization MUST be shared before the adapter boundary. **Acceptance:** AC-05.
- **R-006 — Row-native chunks:** Publication MUST send normalized repository metadata, files, symbols, and relations as bounded row batches. It MUST NOT send a database file, source archive, arbitrary SQL, source text, credentials, or publisher-generated code-to-wiki links. **Acceptance:** AC-06.
- **R-007 — Deterministic batches:** Each batch MUST identify `session_id`, row kind, zero-based ordinal, canonical row count, byte count, and SHA-256 payload hash. Repeating an accepted ordinal with the same hash MUST succeed idempotently; repeating it with a different hash MUST return `batch_conflict`. **Acceptance:** AC-07.
- **R-008 — Bounded and complete upload:** Adapters MUST enforce configured per-batch row and byte limits, valid row kinds, contiguous ordinals within each kind, declared total counts, and required graph relationships. `finalize` MUST fail without changing the active snapshot when any batch, count, foreign key, or graph contract is incomplete or invalid. **Acceptance:** AC-08.
- **R-009 — Canonical revisions:** The shared serializer MUST compute `graph_payload_revision` from repository, file, symbol, and relation rows. The target MUST independently recompute it and return `revision_mismatch` on disagreement. After deriving links, the target MUST compute `snapshot_revision` with the existing schema-v2 canonical algorithm over repository, file, symbol, relation, and link rows. **Acceptance:** AC-09.

### 4.3 Sessions, concurrency, and atomic visibility

- **R-010 — Staging sessions:** `begin` MUST create a staging session scoped to one `(iwiki_id, domain_id)`, capture the current active `snapshot_revision` and authoritative Markdown change token, issue an opaque session ID, and establish a configurable lease expiry. PostgreSQL uses `markdown_generation`; SQLite uses the canonical Markdown hash. The graph is completely built and canonically serialized before `begin`, minimizing the Markdown-conflict window. Multiple staging sessions MAY coexist for the same domain. **Acceptance:** AC-10.
- **R-011 — Lease and non-transferable ownership:** Every SQLite, direct PostgreSQL, and remote MCP mutating session operation MUST validate session ownership, state, and lease in the same transaction or SQLite critical section as its mutation. SQLite MUST repeat that validation in the final activation critical section after all unbounded work and immediately before replacement. A session MUST NOT be resumed, reattached, transferred, or superseded by another publisher. After expiry, the original publisher MUST NOT append or activate and MUST start a new session. A `commit_uncertain` session accepts only an idempotent `finalize` reconciliation by its original in-memory owner. No database lock may be held between calls. **Acceptance:** AC-11.
- **R-012 — Optimistic finalize:** `finalize` MUST take only a short transaction-scoped advisory lock keyed by a reserved constant namespace and a database-assigned unique domain lock ID. PostgreSQL MUST wait for that lock under `SET LOCAL lock_timeout = <configured milliseconds>` and map only SQLSTATE `55P03` from that acquisition to `busy`. Under the lock it MUST compare the captured graph revision and target-specific Markdown change token with current values. Any change MUST return `snapshot_conflict` and leave the active snapshot unchanged; last-writer-wins is forbidden. **Acceptance:** AC-12.
- **R-013 — Atomic activation:** After complete validation and target-side link derivation, PostgreSQL `finalize` MUST mark the staged snapshot ready and switch the domain's active pointer in one transaction. SQLite MUST write ready publication evidence into the staging database and use one atomic `os.replace` as its activation linearization point. A separate SQLite session journal or metadata sidecar MUST NOT determine active readiness after replacement. Readers MUST see either the previous complete ready snapshot or the new complete ready snapshot, never staging, partial rows, or a gap caused by sidecar publication. Different domains MUST publish concurrently. **Acceptance:** AC-13.
- **R-014 — Cleanup:** `abort` MUST be idempotent and make a session non-finalizable. Every `begin` MUST run bounded opportunistic cleanup for the selected domain before creating a session. Cleanup MUST remove at most the configured row/session limit and only expired, aborted, conflicted, or invalid staging data older than the configurable retention period; it MUST NOT affect an active snapshot. **Acceptance:** AC-14.
- **R-015 — Database-only coordination:** PostgreSQL transactions, constraints, and advisory locks are the only mandatory coordination primitives. Correctness MUST NOT depend on NATS, another broker, `LISTEN/NOTIFY`, process affinity, or sticky MCP sessions. **Acceptance:** AC-15.

### 4.4 Markdown and code-to-wiki links

- **R-016 — Authoritative Markdown:** The publication payload MUST exclude `wiki_code_links`. During `finalize`, the target MUST read the authoritative Markdown/selectors for the same `(iwiki_id, domain_id)` and derive links with the existing selector and provenance rules. **Acceptance:** AC-16.
- **R-017 — Target-local link derivation:** SQLite finalization MUST use the local wiki base; direct PostgreSQL and remote MCP finalization MUST use Markdown rows in the target PostgreSQL database. A target without readable authoritative Markdown MUST reject finalization with `markdown_unavailable`. **Acceptance:** AC-17.
- **R-018 — Revision binding:** Every target MUST define canonical `markdown_revision` as SHA-256 over ordered `(page_slug, SHA-256(markdown UTF-8 bytes))` records for all pages in the domain. PostgreSQL domains MUST additionally carry a monotonically increasing `markdown_generation` updated in the same transaction as every authoritative Markdown create, update, delete, or import. PostgreSQL `begin` captures the generation; SQLite `begin` captures the canonical hash without holding the graph writer lock. SQLite `finalize` captures immutable Markdown selectors and a hash under the Wiki shared lock, derives links outside the graph writer lock, then reacquires the Wiki shared lock and recomputes the canonical hash before taking the graph lock. It holds Wiki shared then graph exclusive through the final owner/state/lease, graph-revision, and Markdown-revision checks and activation. The ready snapshot stores the target token and hash. A concurrent Markdown mutation between `begin` and activation MUST produce `snapshot_conflict`; the publisher must start a new session. **Acceptance:** AC-18.
- **R-019 — Markdown independence:** Publishing a graph MUST NOT mutate Markdown, chunks, vectors, or ordinary wiki links. Later Markdown writes MUST retain their current transaction behavior and MAY make derived code-to-wiki links stale. Search and non-wiki graph context MUST remain available, but context MUST omit derived wiki links and report `wiki_links_stale` until graph republish. PostgreSQL status/context MUST expose stored/current `markdown_generation` plus stored canonical `markdown_revision` without scanning all pages; SQLite MAY compare canonical hashes because its Markdown base is local. Lint MUST compute and expose stored/current canonical Markdown revisions and the `wiki_links_stale` flag for either storage. **Acceptance:** AC-19.

### 4.5 Reads, freshness, and source safety

- **R-020 — One reader contract:** SQLite, direct PostgreSQL, and remote MCP read adapters MUST implement one `CodeGraphReader` contract used by `wiki_code_status`, `wiki_code_search`, and `wiki_code_context`. Existing search/context request and response fields MUST remain compatible. **Acceptance:** AC-20.
- **R-021 — Explicit modes:** Configuration MUST select exactly one `publish_mode` and one `read_mode` from `sqlite`, `postgres`, or `mcp`. Mode-specific connection secrets MUST come from existing secret-bearing environment or server configuration, never repository config. Failure in a selected mode MUST be returned; adapters MUST NOT silently fall back. **Acceptance:** AC-21.
- **R-022 — Ready-only reads:** Search and context MUST query only the active ready snapshot. Missing, staging, expired, failed, conflicted, or incompatible snapshots MUST return a safe non-ready result with no graph rows. **Acceptance:** AC-22.
- **R-023 — Configurable freshness:** `max_snapshot_age_seconds` MUST be a non-negative configuration value with default `86400`. Value `0` MUST disable age-based rejection while status still reports snapshot age and timestamps. A ready snapshot older than a positive limit MUST return `stale_snapshot` and no search/context rows. **Acceptance:** AC-23.
- **R-024 — Query bounds:** Existing search and context limits MUST remain enforced for all read adapters. A remote caller MUST NOT be able to request an unbounded result or load an entire graph implicitly. **Acceptance:** AC-24.
- **R-025 — No remote source text:** PostgreSQL snapshots and MCP publication MUST contain no source text or absolute checkout path. For PostgreSQL or remote MCP reads, `include_source=true` MUST return graph context without source plus `source_unavailable`; it MUST NOT fetch source from the publisher. Local SQLite MAY retain current safe local-source behavior. **Acceptance:** AC-25.

### 4.6 Public tools, errors, and operating envelope

- **R-026 — Publication tools:** Remote MCP MUST expose `wiki_code_publish_begin`, `wiki_code_publish_batch`, `wiki_code_publish_finalize`, and `wiki_code_publish_abort`. Each tool MUST use the request's authenticated `iwiki_id`, bound primary, and write scope; none accepts tenant or domain override fields. **Acceptance:** AC-26.
- **R-027 — Local indexing tool:** `wiki_code_index` MUST remain a local extraction operation. It MUST feed the shared publisher selected by `publish_mode`. When the running process lacks a checkout, it MUST return `source_unavailable` and direct the operator to run a local indexer; it MUST NOT create an empty snapshot. **Acceptance:** AC-27.
- **R-028 — Stable safe errors:** New publication paths MUST use the closed codes `unauthorized`, `scope_mismatch`, `unsupported_storage`, `busy`, `session_expired`, `invalid_batch`, `batch_conflict`, `snapshot_incomplete`, `revision_mismatch`, `snapshot_conflict`, `markdown_unavailable`, and `commit_uncertain`. `commit_uncertain` is SQLite-only and means canonical replacement may have occurred but directory durability was not confirmed; it claims neither success nor rollback and permits only repeated `finalize` reconciliation. Adapter/config/index paths add only `invalid_config`, `remote_mcp_failed`, and `source_unavailable`; read readiness adds only `missing_snapshot` and `stale_snapshot`. Existing query-validation codes remain compatible. Errors MUST contain no token, DSN, password, SQL text, absolute path, source text, filesystem error, or cross-scope identifiers. **Acceptance:** AC-28.
- **R-029 — Trust boundaries:** Remote sessions MUST be owned by the authenticated token identity that created them; another token MUST NOT append, abort, or finalize them even when it has write access to the same domain. Direct PostgreSQL and SQLite adapters MUST bind sessions to an ephemeral publisher-instance identity generated for the current indexing run and kept out of repository configuration. A replacement process receives a different identity and MUST create a new session. **Acceptance:** AC-29.
- **R-030 — First-release bounds:** Discovery MUST keep the existing `max_total_files` default and hard support target of 20,000 indexed files. Publication batch bounds, session TTL, staging retention, freshness, and query limits MUST be configurable within validated server-side ceilings. **Acceptance:** AC-30.

## 5. Architecture

### 5.1 Component boundaries

| Component | Responsibility | Depends on |
|---|---|---|
| Existing discovery/extractor/resolver | Build normalized graph rows from a local checkout | Local filesystem, Python adapter |
| `SnapshotPublisher` | Define `begin`/`publish_batch`/`finalize`/`abort` and shared serialization | Graph models only |
| SQLite publisher | Stage and atomically replace a local graph database | Existing SQLite store and local wiki base |
| PostgreSQL publisher | Stage rows and finalize through transactions and advisory locks | PostgreSQL graph repository |
| MCP publisher | Project the same publisher calls over authenticated MCP tools | MCP client and existing token scope |
| `CodeGraphReader` | Define ready snapshot status/search/context reads | Existing query models |
| SQLite reader | Preserve local graph behavior and optional safe source reads | Local SQLite and checkout |
| PostgreSQL reader | Query active tenant/domain snapshot | PostgreSQL graph repository |
| MCP reader | Forward status/search/context to remote MCP | MCP client and existing token scope |
| Markdown revision service | Maintain generation on writes and compute canonical hashes only for finalize/lint | Wiki store |
| Link derivation service | Resolve authoritative Markdown selectors against staged graph | Wiki store plus staged graph |
| Publication service | Validate session, batches, revisions, links, and atomic activation | Publisher repository and lock boundary |

The local indexer ends after normalized graph construction; it does not know whether rows are written to SQLite, PostgreSQL, or MCP. The remote MCP adapter calls the same PostgreSQL publication service used by direct PostgreSQL mode, so transport does not fork storage semantics.

### 5.2 End-to-end flow

1. Local discovery validates the checkout and enforces the file limit.
2. Existing Python extraction and resolution produce deterministic repository, file, symbol, and relation rows.
3. After graph construction and canonical serialization are complete, the selected publisher calls `begin` and receives a leased session with captured graph revision and target-specific Markdown change token.
4. The shared serializer emits bounded row-native batches in repository, file, symbol, and relation order. The adapter persists each batch idempotently.
5. `finalize` validates completeness, recomputes `graph_payload_revision`, derives code-to-wiki links from the captured authoritative Markdown, computes `snapshot_revision`, and validates the complete staged snapshot without holding the domain lock.
6. PostgreSQL waits for the short domain advisory lock under the configured transaction-local lock timeout, rechecks active snapshot revision and target-specific Markdown change token, and atomically switches the active pointer. SQLite performs the same final checks under Wiki-shared then graph-exclusive locks and activates one self-describing ready database with `os.replace`.
7. Readers resolve the selected adapter, require the active ready snapshot and freshness policy, then run existing bounded status/search/context behavior.

## 6. Shared publication contract

### 6.1 Operations

```text
begin(header) -> session
publish_batch(session_id, kind, ordinal, rows, payload_hash) -> batch_ack
finalize(session_id) -> snapshot_result
abort(session_id) -> abort_result
```

`header` is the sole declaration of expected counts and `graph_payload_revision`. It also contains the schema version, normalizer and extractor versions, languages, repository identity derived from primary, source fingerprint, and safe build metadata. `finalize` independently recomputes and compares the declared revision; no duplicate finalize argument may override the header. The header excludes absolute root path, source text, credentials, and credential-bearing Git URLs.

The canonical row wire format uses explicit versioned field names and JSON-compatible scalar values. Rows are sorted by their existing stable identities before batching. Canonical JSON uses UTF-8, sorted object keys, no insignificant whitespace, and normalized scalar representations before SHA-256 hashing. The plan may choose concrete batch ceilings, but tests must run all adapters against the same configured ceiling and boundary cases.

### 6.2 Session state machine

```text
staging -> ready
staging -> aborted
staging -> expired
staging -> conflicted
staging -> failed
staging -> commit_uncertain -> ready
commit_uncertain -> conflicted
```

Only `staging` accepts batches or a first finalization. `ready`, `aborted`, `expired`, `conflicted`, and `failed` are terminal. `commit_uncertain` is a SQLite reconciliation-only state: it accepts no batch or abort and permits only repeated `finalize` by the same in-memory owner. Retrying a completed `finalize` for the same session returns its stored terminal result without another pointer switch.

The lease is renewed only by an accepted batch. Rejected operations, `busy`, `commit_uncertain`, and failed finalization do not extend it. A successful finalization makes the session terminal. Ownership is fixed at `begin`: remote MCP uses the authenticated token identity, while direct PostgreSQL and SQLite use an ephemeral publisher-instance identity held by the creating adapter. There is no resume, reattach, ownership transfer, supersession, or fencing token. A publisher that loses its in-memory session handle starts a new session; retention cleanup later removes the abandoned one.

SQLite writes the complete ready envelope, including the session identity and terminal result, into the staging database before activation. `os.replace` is the logical commit point. A successful directory sync returns `ready`. If replacement may have occurred but directory sync fails, `finalize` returns `commit_uncertain` without claiming success or rollback. A repeated `finalize` reads the active envelope: when its session and revision match, it retries directory sync and returns `ready` only after confirmation; a different active revision returns `snapshot_conflict`; unresolved durability returns `commit_uncertain` again. Failure to update a separate staging journal after confirmed activation cannot change a matching embedded ready result. After process loss, a replacement owner does not resume the session; status reports whichever complete snapshot is actually active.

## 7. Storage model

### 7.1 PostgreSQL logical tables

| Table | Purpose | Key constraints |
|---|---|---|
| `code_graph_domain_state` | Active snapshot pointer and database-assigned unique advisory-lock ID | unique `(iwiki_id, domain_id)` and unique lock ID |
| `code_graph_publication_sessions` | Fixed owner, state, lease, captured revisions, and terminal result | unique session ID plus tenant/domain FK |
| `code_graph_snapshots` | Immutable header, graph payload revision, canonical snapshot revision, Markdown revision, state, counts, timestamps | unique `(iwiki_id, domain_id, snapshot_id)` |
| `code_graph_batches` | Accepted kind/ordinal/hash/count/bytes for idempotency and audit | unique `(iwiki_id, domain_id, session_id, kind, ordinal)` |
| `code_graph_files` | Snapshot-scoped schema-v2 file/module rows | composite tenant/domain/snapshot keys |
| `code_graph_symbols` | Snapshot-scoped schema-v2 symbol rows | composite FKs to files |
| `code_graph_relations` | Snapshot-scoped typed relations | composite FKs to source and resolved targets |
| `code_graph_wiki_links` | Target-derived selector links and provenance | composite FKs to snapshot and authoritative page/domain |
| `database_principal_domain_grants` | Hosted and direct PostgreSQL principal scope shared by Markdown and code graph | unique `(principal, iwiki_id, domain_id)` with read/write flags |
| `domains.markdown_generation` | O(1) conflict and stale-link token updated with Markdown mutations | non-negative monotonic value per domain |

Migration 4 is transactional under the existing migration lock and all its object/policy creation is idempotent so the compatibility rollback procedure can later reapply it. Existing wiki row data is not rewritten. Graph tables use fully qualified `iwiki` names and explicit columns; untrusted `search_path` is not used. The active pointer references only a ready snapshot. Deleting staging or an inactive snapshot cannot cascade into wiki Markdown tables.

### 7.2 SQLite realization

SQLite keeps the current separate graph database, `SCHEMA_VERSION = 2`, and the five public entity tables `repositories`, `files`, `symbols`, `relations`, and `wiki_code_links`. The validator accepts two exact profiles: the legacy profile with those five tables and existing indexes, and the publication profile with the same public objects plus one exact internal `code_graph_publication` table. The internal table is not added to the public `TABLES` set, canonical entity-row APIs, row-native payload, or query/export results. Arbitrary extra tables or indexes remain incompatible.

The publisher records fixed owner, staging state, lease, accepted batches, and captured revisions in its unique staging database. At finalize it writes exactly one ready `code_graph_publication` row before activation. That row contains a format version, domain and repository identity, publisher session ID, graph payload revision, canonical snapshot revision, canonical Markdown revision, counts, indexed timestamp, ready terminal result, a `content_digest`, and an `envelope_digest`. The content digest is canonical SHA-256 over every persisted column of the five public tables in stable-key order; it is separate from the existing schema-v2 `_snapshot_revision`. The envelope digest is canonical SHA-256 over all publication fields except itself. Both use the shared canonical JSON implementation.

The complete staging database is validated and checkpointed before activation. The publisher performs unbounded batch materialization, graph validation, link derivation, SQLite construction, digest calculation, and prior backup outside the graph writer lock. It captures immutable Markdown selectors and their hash under the Wiki shared lock, performs link derivation after releasing that lock, then reacquires Wiki shared, recomputes the canonical Markdown hash, and acquires graph exclusive. Inside that ordered critical section it repeats owner/state/lease, active graph revision, and Markdown revision checks, then performs only the prepared `os.replace` and directory sync. No inverse graph-to-Wiki lock acquisition is allowed.

Readers of the publication profile treat the embedded row as readiness authority. They validate the exact internal schema, envelope, revisions, counts, and content digest. Full content validation occurs on first open and whenever the bounded storage stamp changes; a process-local cache may reuse a successful validation while the stamp is unchanged. Status, search, and context hold Wiki shared then graph read from current Markdown-hash comparison through response materialization so stale derived links cannot escape. The existing `.metadata.json` path is a best-effort cache for the publication profile: it may be absent, stale, corrupt, or regenerated without changing readiness. A legacy snapshot without the internal table continues to require the existing strict database-plus-sidecar storage-stamp validation.

A complete copied database backup remains valid after its content and envelope are revalidated; device and inode are not authority. Accidental mutation, incomplete copy, malformed internal state, or digest mismatch fails closed as `missing_snapshot`. The local threat boundary trusts a principal that can rewrite the graph database: digests detect corruption, not a malicious local writer that can also recompute them. Batch recording and hashes still use the shared protocol even though all calls occur in one local process.

### 7.3 PostgreSQL runtime scope

Direct mode reuses one restricted PostgreSQL runtime principal for both Markdown and code graph. The configured `iwiki_id` is immutable for the process; `primary` must be inside configured read and write scope. An admin-owned `database_principal_domain_grants` mapping represents that principal's shared Markdown and code-graph scope; it is not graph-specific. Direct runtime logins receive no table ownership, `BYPASSRLS`, role-management, migration, or unrestricted DML privileges. Row-level policies resolve `session_user` and require the mapped read/write flag. The documented direct-mode user MUST be this restricted role, never the schema owner shown in migration commands.

The hosted service principal is also a non-owner, non-`BYPASSRLS` runtime role. `principal grant --runtime hosted` registers one exact existing role and its shared domain grants; it never provisions login credentials. `token create` requires `--hosted-principal ROLE` and verifies that same named role, not merely any hosted-role row, covers every requested token read/write domain before token material is generated. Operator documentation requires `ROLE` to equal the deployed hosted server's `[storage].user`; startup validates the connected `session_user` against its provisioned domains so a mismatched or under-granted deployment fails closed. The hosted application still checks each bearer token's existing read/write/primary scope before SQL; the database grant limits the service role to provisioned hosted domains. Policies use ordinary `ENABLE ROW LEVEL SECURITY`, not `FORCE ROW LEVEL SECURITY`, because the schema owner/migrator is an administration-only role and never a runtime credential. The local adapter also rejects any scope outside its configuration before calling PostgreSQL. Composite constraints enforce relational tenant integrity even inside an allowed scope. Hosted domain creation, which predates this design, keeps working under those policies through the reviewed `SECURITY DEFINER` domain-creation function, and runtime roles receive only the additional privileges the hosted token and grant tools require: read and write on the token grant tables and column-scoped `UPDATE` on `iwiki.domains.markdown_generation`.

## 8. MCP and configuration contracts

### 8.1 Configuration

```toml
[code_graph]
publish_mode = "sqlite" # sqlite | postgres | mcp
read_mode = "sqlite"    # sqlite | postgres | mcp
max_snapshot_age_seconds = 86400
max_batch_rows = 1000
max_batch_bytes = 1000000
publication_session_ttl_seconds = 900
staging_retention_seconds = 86400
staging_cleanup_limit = 100
```

Existing code-graph discovery, parser, file-size, file-count, language, and context settings remain authoritative. These numeric fields configure local SQLite/direct publication. Hosted server configuration exposes the same numeric fields without mode selectors and enforces validated upper bounds; remote clients cannot raise them. PostgreSQL mode reuses `[storage]` and `IWIKI_DB_PASSWORD`. MCP mode uses the configured remote endpoint and existing bearer-token secret source. Secret values are never copied into `.iwiki.toml` examples, status, logs, snapshot headers, or errors.

### 8.2 Tool behavior

- `wiki_code_status`, `wiki_code_search`, and `wiki_code_context` use `read_mode` and retain their public request shapes.
- `wiki_code_index` requires a local checkout and uses `publish_mode`.
- FastMCP registers `wiki_code_publish_begin`, `wiki_code_publish_batch`, `wiki_code_publish_finalize`, and `wiki_code_publish_abort` statically. They execute only for authenticated hosted PostgreSQL requests with a writable bound primary; Git and local SQLite bindings return `unsupported_storage`. Local SQLite/direct PostgreSQL indexers call their publisher adapters directly rather than invoking these MCP tools.
- Publication responses contain safe session/snapshot identifiers, accepted ordinals, state, revision, counts, timestamps, and retry guidance. They contain no source or secret material.
- Status reports selected modes, readiness, graph payload revision, active snapshot revision, stored canonical Markdown revision, stored/current Markdown change tokens, generated time, age, configured freshness limit, stale reasons, counts, schema version, and last safe publication failure.

## 9. Error and recovery behavior

| Condition | Result | Active snapshot |
|---|---|---|
| Missing checkout for indexing | `source_unavailable` | unchanged |
| Missing active snapshot | non-ready `missing_snapshot` | none |
| Snapshot older than positive freshness limit | `stale_snapshot`, no rows | unchanged |
| Expired session | `session_expired` | unchanged |
| Different publisher owns session | `unauthorized` | unchanged |
| Same ordinal, different hash | `batch_conflict` | unchanged |
| Missing/invalid rows or counts | `snapshot_incomplete` or `invalid_batch` | unchanged |
| Graph payload revision mismatch | `revision_mismatch` | unchanged |
| Active graph or Markdown revision changed | `snapshot_conflict` | unchanged |
| Markdown unavailable at target | `markdown_unavailable` | unchanged |
| SQLite replace may have succeeded but directory sync failed | `commit_uncertain` | complete previous or complete new snapshot |
| Selected adapter unavailable | sanitized mode-specific failure | unchanged |

`busy` is returned for SQLite lock-acquisition timeout or when PostgreSQL `pg_advisory_xact_lock(namespace, domain_lock_id)` raises SQLSTATE `55P03` under the configured `SET LOCAL lock_timeout`. It is retryable against the same still-valid session and does not renew its lease. Every adapter sanitizes lock and filesystem paths. `snapshot_conflict` is not retryable within that session; the caller must begin again and republish. A storage or validation failure before the activation linearization point marks the staging session failed when safe and preserves the previous active snapshot.

`commit_uncertain` is returned only after SQLite replacement may have taken effect but durable namespace sync was not confirmed. The publisher MUST NOT hide it as `ready`, attempt an automatic rollback, accept another batch, or fall back to another adapter. The same in-memory owner retries `finalize`, which reconciles the embedded active `session_id` and revision and retries directory sync. Status and readers independently report the complete snapshot that validates as active; they never expose a partial or staging state. If the publishing process is lost, a new owner starts a new session and uses status to observe the active revision.

## 10. Security and trust analysis

- Remote publication authenticates before parsing batch content and authorizes the bound primary for every call.
- Remote session ownership is token-identity-specific; possession of a session ID is insufficient. Local publisher identity is ephemeral and non-transferable, so process recovery always starts a new session.
- Payload limits are checked before full row materialization where the transport permits it, then row counts, types, string lengths, and hashes are validated again in the publication service.
- Stable graph IDs are data, never SQL identifiers. SQL is parameterized and table names are fixed.
- Snapshot metadata strips absolute paths and credential-bearing repository URLs.
- PostgreSQL and MCP readers never read publisher filesystem paths and never return source text.
- Tenant/domain/snapshot composite constraints and direct-role row-level policies protect durable relations even if an application filter is wrong.
- Direct credentials are deployment-invalid if their principal owns a protected table or has `BYPASSRLS`; tests use the same restricted role shape documented for operators.
- Advisory lock keys use a reserved integer namespace plus a database-assigned unique domain lock ID, not a hash of untrusted strings.
- Logs use safe IDs and counts only; token values, token digests, DSNs, SQL payloads, source rows, and absolute paths are excluded.
- A local principal that can rewrite the SQLite graph database is inside the trusted boundary. The internal content and envelope digests detect accidental corruption and incomplete copies, not a malicious writer that can recompute them. No graph-specific HMAC secret or device/inode binding is introduced, and a complete copied backup can be restored and revalidated.

## 11. Testing and verification

### 11.1 Shared contract suite

Run one parameterized publisher/reader contract suite against SQLite and direct PostgreSQL adapters. The MCP branch runs the same lifecycle cases through the real in-process hosted ASGI/MCP stack backed by the same disposable PostgreSQL fixture, not through a mocked transport mapper. It proves:

- identical canonical revisions and normalized query results for one fixture;
- ordered chunk publication, boundary-sized batches, empty kinds, and complete counts;
- same-ordinal/same-hash idempotency and different-hash conflict;
- abort, expiry, fixed ownership, failed validation, and idempotent terminal responses;
- independent target recomputation and `revision_mismatch` for a forged header revision;
- bounded opportunistic cleanup invoked by `begin` without deleting active data;
- ready-only reads and atomic old-or-new visibility;
- fresh, stale, and age-check-disabled behavior;
- no source text or absolute path in persisted rows, responses, errors, or logs.

### 11.2 SQLite atomicity and recovery

SQLite tests MUST prove:

- a reader paused across activation observes one complete previous or new snapshot and never depends on sidecar timing;
- process loss or a persistent sidecar failure after replacement leaves the embedded new snapshot readable after restart;
- injected directory-sync failure returns sanitized `commit_uncertain`, and repeated `finalize` reconciles the matching embedded session and returns `ready` only after sync succeeds;
- failure to update the staging session journal after replacement is reconciled to the embedded ready terminal result;
- a pre-replacement failure leaves the previous snapshot byte-for-byte authoritative;
- a complete copied backup restores successfully, while an incomplete copy, corrupt entity row, corrupt envelope, wrong domain/repository/revision, or digest mismatch fails closed;
- a legitimate `git_remote` beginning with any implementation-reserved-looking prefix remains unchanged, and raw stable-row/query/export APIs expose no publication envelope;
- expiry during unbounded finalize work is caught by the activation-time owner/state/lease check;
- Markdown mutation during derivation conflicts, a writer cannot interleave between the final hash and replacement, and reader materialization cannot return stale Wiki links;
- structural lock probes show batch materialization, graph validation, link derivation, SQLite build, digest calculation, and backup occur outside graph exclusive, with Wiki-shared then graph-exclusive as the only activation order;
- every SQLite lock timeout maps to safe `busy` without an internal path;
- the legacy five-table sidecar profile, the extended publication profile, startup import closure, local source safety, and the full existing code-graph suite remain compatible.

### 11.3 PostgreSQL integration

Integration tests MUST prove:

- two domains publish concurrently without blocking each other;
- two same-domain sessions from one base revision yield one success and one `snapshot_conflict`;
- a Markdown write between `begin` and `finalize` causes conflict and no stale link activation;
- target-derived links match authoritative PostgreSQL Markdown selectors;
- failed finalize leaves the previous pointer and rows visible;
- cross-wiki/domain/snapshot inserts and queries fail through constraints and scope checks;
- existing token read/write/primary grants authorize remote graph operations exactly like Markdown operations;
- token creation rejects missing `--hosted-principal`, an owner, `BYPASSRLS`, direct-only, unknown, or under-granted named roles before inserting a token, and accepts the explicitly named non-owner hosted role only when it covers every requested domain;
- the accepted non-owner hosted service principal can access every provisioned hosted domain through shared grants, cannot access an unprovisioned domain, and remains subject to RLS;
- the documented restricted direct PostgreSQL role authorizes Markdown and graph operations through one database-enforced scope, while an owner, `BYPASSRLS`, unmapped, or out-of-scope role is rejected as a valid direct-mode configuration;
- another writable token cannot take over an existing publication session;
- lock contention waits for the configured timeout and returns `busy` without changing or renewing the session;
- status/context expose O(1) generation state while lint reports stored/current canonical Markdown revisions and stale-link state;
- migration 5 is transactional and idempotent, preserves ordinary wiki data, and its compatibility rollback procedure lets the pinned pre-code-graph maintenance build pass both read-only runtime startup guards and execute its PostgreSQL Markdown CRUD/search smoke suite against retained data without database/schema `CREATE` or migration authority.

### 11.4 Regression and scale

- Existing SQLite code-graph tests remain green, including local `include_source=true` safety.
- Existing ordinary PostgreSQL wiki tool-matrix, authentication, page mutation, search, and migration tests remain green.
- A deterministic fixture at the 20,000-file support boundary completes indexing and chunked publication under documented benchmark conditions, records elapsed time and peak Python heap, and respects configured batch and query bounds.
- Warm search/context benchmarks retain existing bounded limits; this design adds no promise of identical SQLite and PostgreSQL latency.

## 12. Acceptance criteria

- **AC-01:** Two snapshots cannot be active for one domain; code tools resolve only bound primary.
- **AC-02:** Existing token plus hosted-service and restricted-direct database-principal tests prove graph read/write scope parity with Markdown and absence of graph-specific auth state; token creation names and validates the exact deployed hosted role before storing token material.
- **AC-03:** Existing schema-v2 identities and SQLite regression fixtures produce unchanged normalized results; the new runtime accepts the exact legacy and publication schema-v2 profiles, keeps the five public entity-table APIs unchanged, and rejects every other extra schema object. A pre-publication binary rollback uses a retained legacy snapshot or reindexes rather than opening the publication profile.
- **AC-04:** Composite constraints, row-level policies, privilege inspection, exact hosted-principal token provisioning, and direct config tests reject cross-scope rows, runtime owner/`BYPASSRLS`/migration-capable credentials, an absent or under-granted named hosted role, unprovisioned hosted domains, and a primary outside immutable scope.
- **AC-05:** All publication modes pass one lifecycle contract suite with the same serialized fixture.
- **AC-06:** Payload inspection proves only safe normalized graph rows are transmitted.
- **AC-07:** Batch replay tests prove idempotent equality and conflicting-hash rejection.
- **AC-08:** Missing, extra, out-of-order, oversized, and invalid rows cannot activate a snapshot.
- **AC-09:** Target recomputation rejects a forged graph payload revision and produces the existing canonical snapshot revision after link derivation.
- **AC-10:** Parallel staging sessions capture explicit graph revision and target-specific Markdown change tokens after graph construction is complete.
- **AC-11:** SQLite, direct PostgreSQL, and MCP expiry/ownership tests prove an expired, replacement, or different publisher cannot mutate or activate and that no session can be transferred, with no lock retained between calls; SQLite repeats the check after unbounded work immediately before replacement and restricts `commit_uncertain` to same-owner reconciliation.
- **AC-12:** Concurrent graph or Markdown mutation produces a stable conflict and preserves active state.
- **AC-13:** Concurrent readers observe only complete old or complete new snapshots; SQLite pause/crash, sidecar-failure, directory-sync-failure, and post-replace-journal-failure tests preserve that visibility, while separate domains finalize concurrently.
- **AC-14:** Abort is retry-safe, and a subsequent `begin` invokes bounded retention cleanup that removes only eligible non-active staging data.
- **AC-15:** Integration tests pass without broker or pub/sub services configured.
- **AC-16:** Publisher payload rejects `wiki_code_links`; target-generated links preserve selector provenance.
- **AC-17:** Each target uses its authoritative Markdown source and rejects unavailable Markdown.
- **AC-18:** Ready metadata records graph payload, canonical snapshot, target-specific Markdown change token, and exact canonical Markdown hash; any intervening Markdown mutation conflicts, activation follows Wiki-shared then graph-exclusive order, and reader materialization cannot emit stale derived links.
- **AC-19:** Graph publication leaves Markdown/chunks/vectors unchanged; a later change-token mismatch suppresses derived links, appears in context/status, and lint independently reports stored/current canonical hashes.
- **AC-20:** All readers pass shared status/search/context compatibility fixtures.
- **AC-21:** Config tests cover every explicit mode and prove failures do not cross-fallback.
- **AC-22:** Every non-ready state returns no search/context graph rows.
- **AC-23:** Tests cover default 24 hours, custom positive age, boundary age, and `0` disabled rejection.
- **AC-24:** Invalid or excessive query limits are rejected or capped by existing public bounds.
- **AC-25:** PostgreSQL/MCP `include_source=true` returns `source_unavailable` and no source bytes.
- **AC-26:** Remote publication tools enforce implicit authenticated tenant/domain scope.
- **AC-27:** Remote server indexing without a checkout returns `source_unavailable` and creates no session/snapshot.
- **AC-28:** Error snapshots cover the complete R-028 code set, including SQLite `commit_uncertain` plus reconciliation, and prove secret/path/source/filesystem-error redaction.
- **AC-29:** Cross-token and stale-local-session takeover attempts fail.
- **AC-30:** The 20,000-file fixture and server-side ceiling tests pass under recorded benchmark conditions.

## 13. Migration and rollout

1. Provision separate schema-owner/migrator, hosted service, and restricted direct runtime roles. Apply PostgreSQL graph objects, Markdown generation, non-forced row-level policies, the shared runtime-principal scope mapping, and the domain-creation SECURITY DEFINER function in migration 5 under the existing migration framework and lock. Provision explicit hosted-service and direct-role grants before starting either runtime. Every PostgreSQL token-creation command names the deployed hosted role with `--hosted-principal`; token creation is rejected before persistence when that exact role is absent, unsafe, or lacks any requested domain grant. A create-only bootstrap token requests no domain, so before the tenant's first domain exists only the restricted role shape is provable and is therefore the whole check.
2. Add shared publisher/reader contracts and make the current SQLite path satisfy them without changing public query semantics. New publications use the exact schema-v2 publication profile with internal authority; legacy five-table snapshots remain readable through strict sidecar validation. Before rolling back to a pre-publication binary, retain or restore a legacy snapshot or reindex with that binary because it is not required to accept the internal publication table.
3. Add PostgreSQL staging, finalize, active-read, and target-link implementations.
4. Add MCP publication and read adapters over those services.
5. Replace current PostgreSQL `unsupported_storage` results for `wiki_code_status`, `wiki_code_search`, and `wiki_code_context`; enable MCP publication tools only on authenticated hosted PostgreSQL; make `wiki_code_index` publish from a local checkout and fail with `source_unavailable` without one.
6. Document local SQLite, direct PostgreSQL, and remote MCP configurations and operator recovery.

No existing code-graph row migration to PostgreSQL is automatic. Operators explicitly run the local indexer and publish the first snapshot. Until then, PostgreSQL code tools report `missing_snapshot`.

Runtime startup never applies migrations. The code-graph schema is migration 5 because migration 4 already carries the released domain-authority schema. Both hosted HTTP `prepare_runtime` and stdio direct PostgreSQL initialization perform the same read-only schema-version-5 check and fail before serving when it is absent or newer than supported. Hosted startup additionally validates its connected `session_user` as a restricted role provisioned for at least one hosted domain. Only the existing operator/admin migration command runs migration 5 with schema-owner credentials. Deployment order is therefore migrate, provision or verify runtime grants, then start hosted and direct runtimes.

Application rollback from schema 5 is an explicit operator procedure, shipped as reviewed idempotent SQL and tested against a disposable database. It stops writers, takes the migration advisory lock, verifies the exact named hosted-service and restricted-direct grants, and removes only migration version 5 from `schema_migrations`; graph objects, generation data, policies, grants, and data remain intact.

The supported rollback binary is a reproducible compatibility maintenance artifact, not the raw pre-code-graph commit. Repository files `compat/postgres-v4-runtime-guard.json` and `compat/postgres-v4-runtime-guard.patch` are its manifest and reviewed patch. The manifest pins base commit `1197208ea3bf953664f726e7602b6c0843f58057`, `patch_sha256`, and `source_tree_sha256`. The source-tree digest is SHA-256 over canonical JSON containing sorted `[relative_posix_path, file_sha256]` pairs for every file in the exported, patched tree. The patch is limited to a read-only schema-version-4 guard and replacement of runtime `run_migrations` calls in both hosted HTTP `prepare_runtime` and stdio direct PostgreSQL initialization; it MUST NOT add DDL, migration execution, owner credentials, or database/schema `CREATE` requirements. The build procedure exports the pinned commit, verifies the exact patch hash, applies it with `git apply --check` followed by `git apply`, verifies the resulting source-tree digest, and packages that verified tree before rollback begins.

The compatibility smoke reconstructs that artifact from the manifest, launches both patched runtime initialization paths with the current test interpreter and installed dependency environment plus isolated configs and disposable restricted-role DSNs, and writes a distinct sentinel only after each read-only schema guard returns. It then proves page create/read/update/delete and lexical search under the hosted service role. The test asserts the runtime roles lack database/schema `CREATE` and mutation privileges on `schema_migrations`, startup leaves migration history unchanged, and a negative-control launch of the raw unpatched commit fails before its sentinel under the same restricted privileges. Any manifest, patch, digest, import, config, dependency, connection, startup, privilege, CRUD, or search failure fails the smoke. Migration 5 statements must tolerate reapplication when the new version returns. Dropping graph tables, policies, generation data, or grants is a destructive down-migration and remains outside scope.

## 14. Risks and mitigations

| Risk | Mitigation | Evidence |
|---|---|---|
| Partial or corrupt upload becomes visible | Complete staging validation plus atomic active pointer | AC-08, AC-13 |
| SQLite database and sidecar disagree during activation | Embedded ready authority switches with the database; sidecar is cache-only | AC-03, AC-13 |
| SQLite replacement is visible but directory durability is unknown | Explicit `commit_uncertain`, same-owner reconciliation, and no hidden rollback | AC-13, AC-28 |
| Local backup is rejected or accidental corruption is accepted | Content/envelope digest validation without device/inode binding; local filesystem writer is trusted | AC-03, AC-13 |
| Concurrent developers overwrite each other | Captured base revisions, short domain lock, optimistic conflict | AC-10, AC-12 |
| Stale local Markdown creates wrong links | Target-only link derivation and Markdown revision binding | AC-16–AC-19 |
| Tenant data crosses scope | Existing token auth, shared hosted/direct RLS grants, and composite tenant/domain/snapshot constraints | AC-02, AC-04 |
| Remote publication leaks source or credentials | Row allowlist, metadata redaction, no source fields | AC-06, AC-25, AC-28 |
| Three modes drift into separate implementations | Shared contract and parameterized adapter suite | AC-05, AC-20 |
| Abandoned sessions consume storage | Lease, terminal states, bounded retention cleanup | AC-11, AC-14 |
| Broker becomes an operational dependency | Transactions and advisory locks are complete correctness boundary | AC-15 |
| Runtime principal bypasses or lacks row policies | Non-forced RLS plus exact hosted-principal token validation, explicit hosted/direct grants, non-owner runtime roles, privilege inspection, and deployment stop | AC-02, AC-04 |
| Raw pre-code-graph startup attempts migrations or requires `CREATE` during rollback | Reproducible pinned maintenance artifact replaces both runtime migration calls with a read-only schema-4 guard; smoke proves restricted-role startup and retained-data CRUD/search | PostgreSQL migration integration |
| Busy contract returns immediately or waits without bound | Transaction-local lock timeout plus blocking advisory lock and contention test | AC-12, AC-28 |

## 15. Human checkpoints

- Approve this checked specification before implementation planning.
- Approve the SQLite `commit_uncertain` retry contract, internal schema-v2 publication profile, pre-publication-binary rollback limitation, and trusted-local-filesystem boundary before Task 2 resumes.
- Review the concrete PostgreSQL migration, exact hosted-principal token gate, hosted/direct role grants, non-forced row-level policies, runtime schema checks, compatibility patch manifest, and rollback SQL during plan execution before any production deployment.
- Provision and test real direct-database credentials outside the repository; no agent may create, expose, or use production credentials.
- Run production publication only through an operator-controlled deployment procedure after migration and integration evidence passes.

## 16. Requirement coverage

| Intent commitment | Requirements | Acceptance |
|---|---|---|
| Local indexer publishes to SQLite, PostgreSQL, or MCP | R-005–R-009, R-021, R-026–R-027 | AC-05–AC-09, AC-21, AC-26–AC-27 |
| Remote PostgreSQL queries without checkout | R-020, R-022, R-024–R-025 | AC-20, AC-22, AC-24–AC-25 |
| Explicit local/remote consumer modes | R-020–R-021 | AC-20–AC-21 |
| Safe missing, stale, and non-ready responses | R-013, R-022–R-023, R-028 | AC-13, AC-22–AC-23, AC-28 |
| Trust, atomicity, and domain isolation | R-001–R-004, R-010–R-019, R-029 | AC-01–AC-04, AC-10–AC-19, AC-29 |
| 20,000 files and configurable 24-hour freshness | R-023, R-030 | AC-23, AC-30 |
| No source text or partial graph data | R-006, R-013, R-022, R-025 | AC-06, AC-13, AC-22, AC-25 |

## 17. Design completion rule

This specification is ready for implementation planning only when `$check-chain spec` returns `OK`, the user approves the checked source, and the spec/profile/version commit is complete. Implementation and deployment must stop and return to this gate if role separation, row-policy enforcement, compatibility rollback, canonical revision behavior, target-side link derivation, or atomic publication cannot be proven with the documented runtime credentials and integration fixtures.
