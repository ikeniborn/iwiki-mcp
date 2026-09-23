# Distributed code graph publication

*Part of the [iwiki-mcp documentation](../README.md#documentation). Русская версия: [code-graph-publishing.ru.md](code-graph-publishing.ru.md).*

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
superseded_retention_seconds = 86400
staging_cleanup_limit = 100
```

`read_mode` selects where `wiki_code_status`, `wiki_code_search`, and
`wiki_code_context` are answered from, exactly as `publish_mode` selects where the built
snapshot goes. `sqlite` — the default — answers from the local code-graph cache and is
unchanged: same guarded reads, same bounded auto-rebuild. `postgres` and `mcp` answer
from the published snapshot instead, so a read never indexes, never rebuilds, and never
touches project source; freshness is whatever the snapshot itself reports
(`missing_snapshot`, `stale_snapshot`). `wiki_code_index` remains a local build under
every read mode. When the selected mode's prerequisite is absent — no PostgreSQL storage
binding for `postgres`, no `IWIKI_CODE_GRAPH_MCP_URL` / `IWIKI_CODE_GRAPH_MCP_TOKEN` for
`mcp` — the read returns `{"error": ..., "code": "invalid_config", "field":
"read_mode", "hint": ...}`, naming the key to fix and never retrying against another
mode. PostgreSQL wiki storage always reads from its own database, so `read_mode` has
nothing left to choose there.

`superseded_retention_seconds` bounds how long a snapshot that is no longer active is
kept before it becomes eligible for cleanup, never the active one. Nothing reads a
superseded snapshot — every query joins `code_graph_domain_state.active_snapshot_id` —
so the window exists only to leave an operator a manual revert target.

Cleanup runs on a single-flight background worker rather than inside a publication, so a
publication is never delayed or failed by it. A cycle drains the backlog one committed
batch at a time, where a batch is at most 10,000 rows from one child table of one
snapshot, children before parents. A cycle holds one connection for its whole duration
and stops once it hits its per-cycle row ceiling or the backlog is empty. If it stops
early — or is killed — the rows already committed stay removed and the next cycle resumes
from there, because the state lives in the database, not in the worker. Every batch
re-checks that the snapshot is still superseded, so an operator reverting to it mid-drain
loses at most one batch of its rows rather than the whole snapshot.

One sweep schedules that work, and none of its callers run it themselves.
`wiki_code_publish_begin`, any other authenticated hosted request, and the direct-
PostgreSQL CLI publisher's own `begin` (below) all call the same `schedule_wiki_cleanup`,
which queues one job per domain in `binding.write`, throttled to once per 900 seconds per
wiki — one trigger shape, not different behavior for `begin` versus everything else. On
the hosted server a fixed set of two maintenance workers drains that queue, so a growing
number of clients produces queueing rather than threads and connections. The queue is
bounded and an enqueue against a full one is dropped and counted — the work returns on a
later request, because nothing waits on it.

One batch is one delete of at most 10,000 rows from one child table of one snapshot, and
each batch commits on its own. A kill therefore costs at most that one batch, and the
next cycle resumes where it stopped. A worker holds one connection for a whole cycle and
draws it from a maintenance pool of its own, never from the pool the tools and
authentication share. The server's total PostgreSQL connections are therefore the
tools-and-auth pool (`pool_max_size`, a required setting with no shipped default — 10 in
the sample `server.toml` in [deployment.md](deployment.md)) plus the two maintenance
workers plus, at worst, the tool ceiling (`pool_max_size - 2`, 8 at the sample size) of
short-lived connections that three hosted per-request paths still open outside both pools
for principal validation — 20 at the sample size, not twelve. Those connects are transient
and already bounded by the same tool ceiling as every other tool call, so they never reach
into the reserved authentication connections; they do mean `max_connections` has to be
sized against this larger total, not against the pool and workers alone.

The direct-PostgreSQL CLI publisher has no hosted server, so no maintenance pool or worker
set exists for its sweep to queue against; its own `begin` call falls back to one raw
daemon thread per domain instead — the same local fallback a stdio session uses,
deduplicated the same way. That process still exits shortly after `finalize` returns, and
a daemon thread does not keep a process alive, so a cleanup cycle it scheduled can still be
killed before it drains: whatever it already committed stays removed, and the rest waits
for the next publication's sweep.

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

An `invalid_config` error names its cause when a safe identifier is available: the
response is `{"error": ..., "code": "invalid_config", "field": "<name>", "hint": ...}`,
where `field` is the offending configuration key or request parameter (for example
`depth`, or a misspelled `.iwiki.toml` key). The name passes a strict identifier gate,
so exception text, values, or paths never appear in it; the key is simply absent when no
safe name exists. Every non-ready query answer also carries `error`, `code`, and `hint`
beside its empty `results`, so it cannot be mistaken for an empty filter match.

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
hold write access to the project's primary domain — `wiki_bind` selects within an already
granted scope and cannot exceed it. A session belongs to the identity that created it:
another token with write access to the same domain cannot append to, abort, or finalize
it, and a replacement process must start a new session.

`wiki_code_refresh_links(domain)` re-derives the active snapshot's `DOCUMENTED_BY`
links from the domain's current Markdown. It parses no source and resolves no symbol, so
it clears `wiki_links_stale` in time proportional to the page count instead of rebuilding
the graph. The snapshot itself is untouched: `snapshot_revision`, `graph_payload_revision`
and the file, symbol and relation counts are the same afterwards, and only
`code_graph_wiki_links` plus the stored Markdown revision change. Unlike the publication
calls it names its domain, because it mutates and a lapsed session binding would otherwise
retarget it; the token must hold write access to that domain. Without an active ready
snapshot it answers `missing_snapshot` rather than starting a build.

Batches carry rows only — never a database file, source text, an absolute checkout
path, credentials, or publisher-generated wiki links. The target recomputes the payload
revision, derives code-to-wiki links from the destination Markdown itself, and activates
the snapshot in a single commit. Readers therefore observe either the previous complete
revision or the new one, never a partial upload. Repeating an accepted ordinal with the
same rows succeeds idempotently; repeating it with different rows returns
`batch_conflict`.

PostgreSQL activation inserts each graph row kind and the derived Wiki links with one
set-based, RLS-compatible statement. Pipelined `executemany` is not sufficient here: it
still executes one `INSERT` per row, so a 100,000-row snapshot can exhaust the remote
call deadline under database load even when network round trips are hidden.

Retry the whole publication after `busy`, `session_expired`, `snapshot_conflict`,
`revision_mismatch`, or `markdown_unavailable`: begin a new session and resend. A
`snapshot_conflict` means the active snapshot or the destination Markdown changed while
the session was open, so the rebuilt graph must be published against the current state.
Expired staging sessions are cleaned up in bounded batches when the next session begins,
inline and synchronously — distinct from the superseded-snapshot worker above, which is
the only background daemon in this path.

For PostgreSQL or remote MCP reads, `include_source=true` returns graph context without
source plus `source_unavailable`; the server never fetches source from the publisher.
Local SQLite reads keep their existing guarded local-source behavior. Search and context
limits are enforced for every read adapter, so a remote caller cannot request an
unbounded result or load a whole graph implicitly.

The first publication into an empty domain is an ordinary session: status reports
`missing_snapshot` until the first `finalize` succeeds.

## Scheduled publisher operation

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

When the transport loses the answer to a `finalize` — a timeout or a dropped connection —
the publisher asks the target what actually happened before it reports. An already
terminal session replays its terminal result, so a publication the target did complete
exits `0` as ready instead of `1`. Only a session the target never activated is reported
as a publication failure.

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
quality evidence returned `do_not_implement`; see the [evaluation report](superpowers/evidence/wiki-unified-search-evaluation.md)
and [machine-readable evidence](superpowers/evidence/wiki-unified-search-evaluation.json).

## SQLite snapshot profiles and commit uncertainty

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
