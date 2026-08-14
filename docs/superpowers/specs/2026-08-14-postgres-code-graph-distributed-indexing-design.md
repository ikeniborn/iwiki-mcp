---
review:
  spec_hash: 6612335899a16e33
  last_run: 2026-08-14
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings: []
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
- **R-002 — Existing authorization:** Code-graph publication MUST require the bound `primary` in existing wiki write scope. Code-graph query MUST require it in existing wiki read scope. Remote MCP MUST derive `iwiki_id` and domain grants from the existing token. Direct PostgreSQL MUST use the existing database principal's wiki-domain grant. No code-graph token, role, ACL, or graph-specific grant table may be introduced. **Acceptance:** AC-02.
- **R-003 — Existing graph compatibility:** Published rows MUST preserve current schema-v2 entity identities, relation semantics, deterministic revision rules, and Python-only behavior. Existing Git/SQLite code-graph and ordinary PostgreSQL wiki contracts MUST remain usable. **Acceptance:** AC-03.
- **R-004 — Tenant integrity:** Every PostgreSQL graph row MUST carry `iwiki_id`, `domain_id`, and `snapshot_id` where applicable. Composite keys and foreign keys MUST reject cross-wiki, cross-domain, and cross-snapshot references. Direct publication MUST use the same immutable local `iwiki_id`, `read`, `write`, and `primary` configuration as direct PostgreSQL Markdown access, and the local publisher MUST reject any requested scope outside it. PostgreSQL MUST independently reject a direct principal outside its existing wiki-domain grant. **Acceptance:** AC-04.

### 4.2 Shared publication protocol

- **R-005 — One publisher lifecycle:** SQLite, direct PostgreSQL, and remote MCP publication MUST implement the same `SnapshotPublisher` lifecycle: `begin`, ordered `publish_batch`, `finalize`, and `abort`. Indexing and row serialization MUST be shared before the adapter boundary. **Acceptance:** AC-05.
- **R-006 — Row-native chunks:** Publication MUST send normalized repository metadata, files, symbols, and relations as bounded row batches. It MUST NOT send a database file, source archive, arbitrary SQL, source text, credentials, or publisher-generated code-to-wiki links. **Acceptance:** AC-06.
- **R-007 — Deterministic batches:** Each batch MUST identify `session_id`, row kind, zero-based ordinal, canonical row count, byte count, and SHA-256 payload hash. Repeating an accepted ordinal with the same hash MUST succeed idempotently; repeating it with a different hash MUST return `batch_conflict`. **Acceptance:** AC-07.
- **R-008 — Bounded and complete upload:** Adapters MUST enforce configured per-batch row and byte limits, valid row kinds, contiguous ordinals within each kind, declared total counts, and required graph relationships. `finalize` MUST fail without changing the active snapshot when any batch, count, foreign key, or graph contract is incomplete or invalid. **Acceptance:** AC-08.
- **R-009 — Canonical revisions:** The shared serializer MUST compute `graph_payload_revision` from repository, file, symbol, and relation rows. The target MUST independently recompute it and return `revision_mismatch` on disagreement. After deriving links, the target MUST compute `snapshot_revision` with the existing schema-v2 canonical algorithm over repository, file, symbol, relation, and link rows. **Acceptance:** AC-09.

### 4.3 Sessions, concurrency, and atomic visibility

- **R-010 — Staging sessions:** `begin` MUST create a staging session scoped to one `(iwiki_id, domain_id)`, capture the current active `snapshot_revision` and authoritative Markdown revision, issue an opaque session ID, and establish a configurable lease expiry. Multiple staging sessions MAY coexist for the same domain. **Acceptance:** AC-10.
- **R-011 — Lease and fencing:** Every mutating session operation MUST validate session ownership, state, lease, and monotonically increasing fencing token. After expiry or supersession, an old publisher MUST NOT append or finalize. No database lock may be held between MCP calls. **Acceptance:** AC-11.
- **R-012 — Optimistic finalize:** `finalize` MUST take only a short transaction-scoped advisory lock keyed by `(iwiki_id, domain_id)`. Under that lock it MUST compare the captured graph and Markdown revisions with current revisions. Any change MUST return `snapshot_conflict` and leave the active snapshot unchanged; last-writer-wins is forbidden. **Acceptance:** AC-12.
- **R-013 — Atomic pointer switch:** After complete validation and target-side link derivation, `finalize` MUST mark the staged snapshot ready and switch the domain's active pointer in one transaction. Readers MUST see either the previous ready snapshot or the new ready snapshot, never staging or partial rows. Different domains MUST publish concurrently. **Acceptance:** AC-13.
- **R-014 — Cleanup:** `abort` MUST be idempotent and make a session non-finalizable. Expired, aborted, conflicted, and invalid staging data MUST be removable after a configurable retention period without affecting any active snapshot. **Acceptance:** AC-14.
- **R-015 — Database-only coordination:** PostgreSQL transactions, constraints, and advisory locks are the only mandatory coordination primitives. Correctness MUST NOT depend on NATS, another broker, `LISTEN/NOTIFY`, process affinity, or sticky MCP sessions. **Acceptance:** AC-15.

### 4.4 Markdown and code-to-wiki links

- **R-016 — Authoritative Markdown:** The publication payload MUST exclude `wiki_code_links`. During `finalize`, the target MUST read the authoritative Markdown/selectors for the same `(iwiki_id, domain_id)` and derive links with the existing selector and provenance rules. **Acceptance:** AC-16.
- **R-017 — Target-local link derivation:** SQLite finalization MUST use the local wiki base; direct PostgreSQL and remote MCP finalization MUST use Markdown rows in the target PostgreSQL database. A target without readable authoritative Markdown MUST reject finalization with `markdown_unavailable`. **Acceptance:** AC-17.
- **R-018 — Revision binding:** The target MUST define `markdown_revision` as a deterministic SHA-256 hash over the complete authoritative Markdown of all pages in the domain, ordered by page slug. The ready snapshot MUST record `graph_payload_revision`, `snapshot_revision`, and the `markdown_revision` used for link derivation. A concurrent Markdown mutation between `begin` and `finalize` MUST produce `snapshot_conflict`; the publisher must start a new session. **Acceptance:** AC-18.
- **R-019 — Markdown independence:** Publishing a graph MUST NOT mutate Markdown, chunks, vectors, or ordinary wiki links. Later Markdown writes MUST retain their current transaction behavior and MAY make derived code-to-wiki links stale. Search and non-wiki graph context MUST remain available, but context MUST omit derived wiki links and report `wiki_links_stale` until graph republish; status/lint MUST expose the stored and current Markdown revisions. **Acceptance:** AC-19.

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
- **R-028 — Stable safe errors:** Publication MUST use sanitized stable errors including `unauthorized`, `scope_mismatch`, `busy`, `session_expired`, `invalid_batch`, `batch_conflict`, `snapshot_incomplete`, `revision_mismatch`, `snapshot_conflict`, and `markdown_unavailable`. Errors MUST contain no token, DSN, password, SQL text, absolute path, source text, or cross-scope identifiers. **Acceptance:** AC-28.
- **R-029 — Trust boundaries:** Remote sessions MUST be owned by the authenticated token identity that created them; another token MUST NOT append, abort, or finalize them even when it has write access to the same domain. Direct PostgreSQL and SQLite adapters MUST bind sessions to their configured local publisher identity. **Acceptance:** AC-29.
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
| Link derivation service | Resolve authoritative Markdown selectors against staged graph | Wiki store plus staged graph |
| Publication service | Validate session, batches, revisions, links, and atomic activation | Publisher repository and lock boundary |

The local indexer ends after normalized graph construction; it does not know whether rows are written to SQLite, PostgreSQL, or MCP. The remote MCP adapter calls the same PostgreSQL publication service used by direct PostgreSQL mode, so transport does not fork storage semantics.

### 5.2 End-to-end flow

1. Local discovery validates the checkout and enforces the file limit.
2. Existing Python extraction and resolution produce deterministic repository, file, symbol, and relation rows.
3. The selected publisher calls `begin` and receives a leased session with captured graph and Markdown base revisions.
4. The shared serializer emits bounded row-native batches in repository, file, symbol, and relation order. The adapter persists each batch idempotently.
5. `finalize` validates completeness, recomputes `graph_payload_revision`, derives code-to-wiki links from the captured authoritative Markdown, computes `snapshot_revision`, and validates the complete staged snapshot without holding the domain lock.
6. The target takes the short domain advisory lock, rechecks active snapshot and Markdown revisions, and atomically switches the active pointer.
7. Readers resolve the selected adapter, require the active ready snapshot and freshness policy, then run existing bounded status/search/context behavior.

## 6. Shared publication contract

### 6.1 Operations

```text
begin(header) -> session
publish_batch(session_id, fencing_token, kind, ordinal, rows, payload_hash) -> batch_ack
finalize(session_id, fencing_token, expected_counts, expected_graph_payload_revision) -> snapshot_result
abort(session_id, fencing_token) -> abort_result
```

`header` contains the schema version, normalizer and extractor versions, languages, repository identity derived from primary, source fingerprint, entity/relation counts, and safe build metadata. It excludes absolute root path, source text, credentials, and credential-bearing Git URLs.

The canonical row wire format uses explicit versioned field names and JSON-compatible scalar values. Rows are sorted by their existing stable identities before batching. Canonical JSON uses UTF-8, sorted object keys, no insignificant whitespace, and normalized scalar representations before SHA-256 hashing. The plan may choose concrete batch ceilings, but tests must run all adapters against the same configured ceiling and boundary cases.

### 6.2 Session state machine

```text
staging -> ready
staging -> aborted
staging -> expired
staging -> conflicted
staging -> failed
```

Only `staging` accepts batches or finalization. `ready`, `aborted`, `expired`, `conflicted`, and `failed` are terminal. Retrying a completed `finalize` for the same session returns its stored terminal result without another pointer switch.

The lease is renewed only by an accepted batch or an explicit successful finalization attempt. Rejected operations do not extend it. The returned fencing token changes whenever session ownership is re-established; every write compares it inside the same transaction as the mutation.

## 7. Storage model

### 7.1 PostgreSQL logical tables

| Table | Purpose | Key constraints |
|---|---|---|
| `code_graph_domain_state` | Active snapshot pointer and domain publication revision | unique `(iwiki_id, domain_id)` |
| `code_graph_publication_sessions` | Owner, state, lease, fencing token, captured revisions, terminal result | unique session ID plus tenant/domain FK |
| `code_graph_snapshots` | Immutable header, graph payload revision, canonical snapshot revision, Markdown revision, state, counts, timestamps | unique `(iwiki_id, domain_id, snapshot_id)` |
| `code_graph_batches` | Accepted kind/ordinal/hash/count/bytes for idempotency and audit | unique `(iwiki_id, domain_id, session_id, kind, ordinal)` |
| `code_graph_files` | Snapshot-scoped schema-v2 file/module rows | composite tenant/domain/snapshot keys |
| `code_graph_symbols` | Snapshot-scoped schema-v2 symbol rows | composite FKs to files |
| `code_graph_relations` | Snapshot-scoped typed relations | composite FKs to source and resolved targets |
| `code_graph_wiki_links` | Target-derived selector links and provenance | composite FKs to snapshot and authoritative page/domain |
| `database_principal_domain_grants` | Direct PostgreSQL principal scope shared by Markdown and code graph | unique `(principal, iwiki_id, domain_id)` with read/write flags |

The migration is forward-only and transactional under the existing migration lock. Existing wiki tables are not rewritten. Graph tables use fully qualified `iwiki` names and explicit columns; untrusted `search_path` is not used. The active pointer references only a ready snapshot. Deleting staging or an inactive snapshot cannot cascade into wiki Markdown tables.

### 7.2 SQLite realization

SQLite keeps the current separate graph database and schema-v2 rows. The publisher writes a unique staging database plus session metadata, validates it through the shared contract, derives links from the local wiki base, and uses the existing atomic replacement boundary. Batch recording and hashes use the same protocol even though all calls occur in one local process. This preserves one publication implementation above the adapter boundary.

### 7.3 Direct PostgreSQL scope

Direct mode reuses the existing PostgreSQL storage configuration and credentials. The configured `iwiki_id` is immutable for the process; `primary` must be inside configured read and write scope. An admin-owned `database_principal_domain_grants` mapping represents the same direct principal scope for all wiki Markdown and code-graph operations; it is not graph-specific. Direct logins receive no table-owner, `BYPASSRLS`, or unrestricted graph-table DML privileges. Scope-enforcing database functions resolve `session_user`, require the mapped read/write flag, and perform graph operations with fixed qualified SQL.

The local adapter also rejects any scope outside its configuration before calling PostgreSQL. Composite constraints enforce relational tenant integrity even inside an allowed scope. The hosted server's service role remains a separate trusted application boundary: it reaches graph storage only after existing bearer-token authorization and does not expose direct database credentials to MCP clients.

## 8. MCP and configuration contracts

### 8.1 Configuration

```toml
[code_graph]
publish_mode = "sqlite" # sqlite | postgres | mcp
read_mode = "sqlite"    # sqlite | postgres | mcp
max_snapshot_age_seconds = 86400
```

Existing code-graph discovery, parser, file-size, file-count, language, and context settings remain authoritative. Session TTL, staging retention, and batch ceilings are operator/server settings with safe defaults and validated upper bounds; remote clients cannot raise them. PostgreSQL mode reuses `[storage]` and `IWIKI_DB_PASSWORD`. MCP mode uses the configured remote endpoint and existing bearer-token secret source. Secret values are never copied into `.iwiki.toml` examples, status, logs, snapshot headers, or errors.

### 8.2 Tool behavior

- `wiki_code_status`, `wiki_code_search`, and `wiki_code_context` use `read_mode` and retain their public request shapes.
- `wiki_code_index` requires a local checkout and uses `publish_mode`.
- `wiki_code_publish_begin`, `wiki_code_publish_batch`, `wiki_code_publish_finalize`, and `wiki_code_publish_abort` are remote write operations and are available only through an authenticated writable primary domain.
- Publication responses contain safe session/snapshot identifiers, accepted ordinals, state, revision, counts, timestamps, and retry guidance. They contain no source or secret material.
- Status reports selected modes, readiness, graph payload revision, active snapshot revision, stored and current Markdown revisions, generated time, age, configured freshness limit, stale reasons, counts, schema version, and last safe publication failure.

## 9. Error and recovery behavior

| Condition | Result | Active snapshot |
|---|---|---|
| Missing checkout for indexing | `source_unavailable` | unchanged |
| Missing active snapshot | non-ready `missing_snapshot` | none |
| Snapshot older than positive freshness limit | `stale_snapshot`, no rows | unchanged |
| Expired or fenced session | `session_expired` | unchanged |
| Same ordinal, different hash | `batch_conflict` | unchanged |
| Missing/invalid rows or counts | `snapshot_incomplete` or `invalid_batch` | unchanged |
| Graph payload revision mismatch | `revision_mismatch` | unchanged |
| Active graph or Markdown revision changed | `snapshot_conflict` | unchanged |
| Markdown unavailable at target | `markdown_unavailable` | unchanged |
| Selected adapter unavailable | sanitized mode-specific failure | unchanged |

`busy` is returned only when the short finalize lock cannot be obtained within its configured timeout. It is retryable against the same still-valid session. `snapshot_conflict` is not retryable within that session; the caller must begin again. Storage or validation failure marks the staging session failed when safe, rolls back the finalize transaction, and preserves the previous active pointer.

## 10. Security and trust analysis

- Remote publication authenticates before parsing batch content and authorizes the bound primary for every call.
- Session ownership is token-identity-specific; possession of a session ID is insufficient.
- Payload limits are checked before full row materialization where the transport permits it, then row counts, types, string lengths, and hashes are validated again in the publication service.
- Stable graph IDs are data, never SQL identifiers. SQL is parameterized and table names are fixed.
- Snapshot metadata strips absolute paths and credential-bearing repository URLs.
- PostgreSQL and MCP readers never read publisher filesystem paths and never return source text.
- Tenant/domain/snapshot composite constraints and database-principal scope functions protect durable relations even if an application filter is wrong.
- Advisory lock keys are derived from resolved database identifiers, not untrusted free-form strings.
- Logs use safe IDs and counts only; token values, token digests, DSNs, SQL payloads, source rows, and absolute paths are excluded.

## 11. Testing and verification

### 11.1 Shared contract suite

Run one parameterized publisher/reader contract suite against SQLite and PostgreSQL adapters. The MCP adapter runs the same cases through the tool boundary. It proves:

- identical canonical revisions and normalized query results for one fixture;
- ordered chunk publication, boundary-sized batches, empty kinds, and complete counts;
- same-ordinal/same-hash idempotency and different-hash conflict;
- abort, expiry, fencing, failed validation, and idempotent terminal responses;
- ready-only reads and atomic old-or-new visibility;
- fresh, stale, and age-check-disabled behavior;
- no source text or absolute path in persisted rows, responses, errors, or logs.

### 11.2 PostgreSQL integration

Integration tests MUST prove:

- two domains publish concurrently without blocking each other;
- two same-domain sessions from one base revision yield one success and one `snapshot_conflict`;
- a Markdown write between `begin` and `finalize` causes conflict and no stale link activation;
- target-derived links match authoritative PostgreSQL Markdown selectors;
- failed finalize leaves the previous pointer and rows visible;
- cross-wiki/domain/snapshot inserts and queries fail through constraints and scope checks;
- existing token read/write/primary grants authorize remote graph operations exactly like Markdown operations;
- direct PostgreSQL principal grants authorize Markdown and graph operations through one database-enforced scope, while an unmapped or out-of-scope principal is rejected;
- another writable token cannot take over an existing publication session;
- forward migration is transactional, idempotent under concurrent startup, and preserves ordinary wiki data.

### 11.3 Regression and scale

- Existing SQLite code-graph tests remain green, including local `include_source=true` safety.
- Existing ordinary PostgreSQL wiki tool-matrix, authentication, page mutation, search, and migration tests remain green.
- A deterministic fixture at the 20,000-file support boundary completes indexing and chunked publication under documented benchmark conditions without violating configured memory, batch, or query bounds.
- Warm search/context benchmarks retain existing bounded limits; this design adds no promise of identical SQLite and PostgreSQL latency.

## 12. Acceptance criteria

- **AC-01:** Two snapshots cannot be active for one domain; code tools resolve only bound primary.
- **AC-02:** Existing token and direct database-principal tests prove graph read/write scope parity with Markdown and absence of graph-specific auth state.
- **AC-03:** Existing schema-v2 identities and SQLite regression fixtures produce unchanged normalized results.
- **AC-04:** Composite constraints, scope functions, and direct config tests independently reject cross-scope rows and a primary outside immutable scope.
- **AC-05:** All publication modes pass one lifecycle contract suite with the same serialized fixture.
- **AC-06:** Payload inspection proves only safe normalized graph rows are transmitted.
- **AC-07:** Batch replay tests prove idempotent equality and conflicting-hash rejection.
- **AC-08:** Missing, extra, out-of-order, oversized, and invalid rows cannot activate a snapshot.
- **AC-09:** Target recomputation rejects a forged graph payload revision and produces the existing canonical snapshot revision after link derivation.
- **AC-10:** Parallel staging sessions capture explicit graph and Markdown base revisions.
- **AC-11:** Expiry and fencing tests prove an old publisher cannot mutate or finalize, with no lock retained between calls.
- **AC-12:** Concurrent graph or Markdown mutation produces a stable conflict and preserves active state.
- **AC-13:** Concurrent readers observe only complete old or complete new snapshots; separate domains finalize concurrently.
- **AC-14:** Abort and cleanup remove only non-active staging data and are retry-safe.
- **AC-15:** Integration tests pass without broker or pub/sub services configured.
- **AC-16:** Publisher payload rejects `wiki_code_links`; target-generated links preserve selector provenance.
- **AC-17:** Each target uses its authoritative Markdown source and rejects unavailable Markdown.
- **AC-18:** Ready metadata records graph payload, canonical snapshot, and exact Markdown revisions; any intervening Markdown mutation conflicts.
- **AC-19:** Graph publication leaves Markdown/chunks/vectors unchanged; a later mismatch suppresses derived links and appears in context/status/lint.
- **AC-20:** All readers pass shared status/search/context compatibility fixtures.
- **AC-21:** Config tests cover every explicit mode and prove failures do not cross-fallback.
- **AC-22:** Every non-ready state returns no search/context graph rows.
- **AC-23:** Tests cover default 24 hours, custom positive age, boundary age, and `0` disabled rejection.
- **AC-24:** Invalid or excessive query limits are rejected or capped by existing public bounds.
- **AC-25:** PostgreSQL/MCP `include_source=true` returns `source_unavailable` and no source bytes.
- **AC-26:** Remote publication tools enforce implicit authenticated tenant/domain scope.
- **AC-27:** Remote server indexing without a checkout returns `source_unavailable` and creates no session/snapshot.
- **AC-28:** Error snapshots prove stable codes and secret/path/source redaction.
- **AC-29:** Cross-token and stale-local-session takeover attempts fail.
- **AC-30:** The 20,000-file fixture and server-side ceiling tests pass under recorded benchmark conditions.

## 13. Migration and rollout

1. Add PostgreSQL graph objects and the shared direct-principal scope mapping in one new forward migration under the existing migration framework and lock.
2. Add shared publisher/reader contracts and make the current SQLite path satisfy them without changing public query semantics.
3. Add PostgreSQL staging, finalize, active-read, and target-link implementations.
4. Add MCP publication and read adapters over those services.
5. Replace the current PostgreSQL `unsupported_storage` results only for supported `wiki_code_status`, `wiki_code_search`, `wiki_code_context`, and publication tools. Keep `wiki_code_index` fail-soft when no checkout exists.
6. Document local SQLite, direct PostgreSQL, and remote MCP configurations and operator recovery.

No existing code-graph row migration to PostgreSQL is automatic. Operators explicitly run the local indexer and publish the first snapshot. Until then, PostgreSQL code tools report `missing_snapshot`. Rollback uses the previous application version after ensuring it does not see an unsupported newer schema; destructive down-migration is outside scope.

## 14. Risks and mitigations

| Risk | Mitigation | Evidence |
|---|---|---|
| Partial or corrupt upload becomes visible | Complete staging validation plus atomic active pointer | AC-08, AC-13 |
| Concurrent developers overwrite each other | Captured base revisions, short domain lock, optimistic conflict | AC-10, AC-12 |
| Stale local Markdown creates wrong links | Target-only link derivation and Markdown revision binding | AC-16–AC-19 |
| Tenant data crosses scope | Existing auth, direct-principal functions, and composite tenant/domain/snapshot constraints | AC-02, AC-04 |
| Remote publication leaks source or credentials | Row allowlist, metadata redaction, no source fields | AC-06, AC-25, AC-28 |
| Three modes drift into separate implementations | Shared contract and parameterized adapter suite | AC-05, AC-20 |
| Abandoned sessions consume storage | Lease, terminal states, bounded retention cleanup | AC-11, AC-14 |
| Broker becomes an operational dependency | Transactions and advisory locks are complete correctness boundary | AC-15 |

## 15. Human checkpoints

- Approve this checked specification before implementation planning.
- Review the concrete PostgreSQL migration and privilege diff during plan execution before any production deployment.
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

This specification is ready for implementation planning only when `$check-chain spec` returns `OK`, the user approves the checked source, and the spec/profile/version commit is complete. Implementation must stop and return to this gate if planning discovers a contradiction in authentication scope, migration safety, canonical revision compatibility, target-side link derivation, or atomic publication.
