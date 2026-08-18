# Design: codegraph-publish-client-batch-limits

**Date:** 2026-08-19
**Status:** draft
**Intent:** [docs/superpowers/intents/2026-08-19-codegraph-publish-client-batch-limits-intent.md](../intents/2026-08-19-codegraph-publish-client-batch-limits-intent.md)

## Acceptance (from intent)

Desired Outcomes (verbatim from the approved intent):
- A client whose `.iwiki.toml` sets `max_batch_rows`/`max_batch_bytes` above the remote's
  actual limit publishes successfully via `wiki_code_index` without any manual
  `.iwiki.toml` edit — the client discovers and honors the server's real limit
  automatically.
- `publish_mode = "sqlite"` behavior is unchanged: local `max_batch_rows`/`max_batch_bytes`
  continue to size local batches exactly as before.
- If a client still sends an over-limit batch, the server's rejection names the actual
  limit and what was received instead of a generic `invalid_batch`.

Done when: `aioperator` (or an equivalent project) runs `wiki_code_index` and reaches
`state: ready` on the remote via `publish_mode = "mcp"` with zero manual `.iwiki.toml`
edits, AND a deliberately oversized client batch produces an error response stating the
actual limit and the received count, AND `publish_mode = "sqlite"`'s existing test suite
passes unmodified.

## 0. Approach decision (from brainstorming)

Three approaches were evaluated:

- **A — return limits in `begin()`'s response (chosen).** `wiki_code_publish_begin`
  already round-trips before any batch is built (`_publish_local_snapshot` calls
  `runtime.export_snapshot()`, then `publisher.begin(header)`, then iterates
  `iter_snapshot_batches`) — piggybacking the server's effective bounds on that existing
  response needs no new call.
- **B — separate discovery call** (e.g. extend `wiki_code_status`). Rejected: an extra
  round-trip, and touches a second public contract (`wiki_code_status`) for no added
  benefit over A.
- **C — server silently re-splits an oversized client batch.** Rejected: breaks the
  documented canonical-representation contract (`concept/code-graph-publication`) —
  `payload_hash`/ordinal continuity are client-owned identity for retry/idempotency;
  server-side re-chunking would desync a client's own retry bookkeeping from what
  actually landed.

## 1. Architecture

Two independent slices:

1. **Limit reporting** — `PublicationSession` (frozen, `codegraph/publication.py`) gains
   two new optional fields, `max_batch_rows: int | None = None` and
   `max_batch_bytes: int | None = None`. Populated on exactly two of the three
   `begin()` implementations; the third stays untouched:
   - `_HostedPublication.begin_from_mapping` (`server.py`) — this wrapper already holds
     `self._settings` (a `HostedCodeGraphConfig`, the SAME instance
     `publish_from_mapping`'s existing len-check reads) and already hand-assembles the
     response dict crossing the wire (`{"session_id": ..., "lease_expires_at": ...,
     ...}`) from `PostgresCodeGraphStore.begin()`'s return value. Add
     `"max_batch_rows": self._settings.max_batch_rows, "max_batch_bytes":
     self._settings.max_batch_bytes` to that dict. `PostgresCodeGraphStore.begin()`
     itself (`postgres/codegraph.py`) is UNCHANGED — it never holds a
     `HostedCodeGraphConfig` and does not need to; the wrapper is the sole place that
     does, and the wrapper is what actually builds the wire response.
   - `McpSnapshotPublisher.begin()` (`codegraph/mcp_adapter.py`, the remote CLIENT side)
     — parses the two optional keys from the decoded response into the returned
     `PublicationSession`.
   - `SqliteSnapshotPublisher.begin()` (`codegraph/sqlite_adapter.py`) — leaves both
     fields `None` (local target, client's own `config` values are already authoritative
     there — Health Metric: this path is untouched).

2. **Client-side limit selection** — `server.py::_publish_local_snapshot` reads
   `session.max_batch_rows`/`session.max_batch_bytes` (present only for the `mcp` target,
   per above) instead of `config.max_batch_rows`/`config.max_batch_bytes` when both are
   present and pass validation (§3); otherwise falls back to `config`'s value unchanged.
   This is the ONLY behavioral branch point — everything else in the batching/publish
   flow (`iter_snapshot_batches`, `canonical_batch`, hashing) is untouched.

Diagnostic detail (independent of the above, applies to both remaining checks):
`_CODE_INVALID_BATCH` (`server.py`) and `_INVALID_BATCH` (`postgres/codegraph.py`) each
gain `"limit": <int>, "received": <int>` in the returned dict specifically for the
`len(rows) > max_batch_rows` rejection branch (the one this whole intent is about); the
`byte_count > max_batch_bytes` branch gains the equivalent `"limit"`/`"received"` pair.
Other `invalid_batch` triggers (malformed `kind`, negative `ordinal`, hash mismatch)
keep their current shape — they are not size-mismatch diagnostics and adding
limit/received to them would be misleading.

## 2. Components touched

| File | Change |
|---|---|
| `codegraph/publication.py` | `PublicationSession`: add `max_batch_rows`, `max_batch_bytes` (both `int \| None = None`) |
| `postgres/codegraph.py` | No change to `begin()`/`PublicationSession` construction. `_INVALID_BATCH`-adjacent len-check in `publish_batch`/`_materialize`: add `limit`/`received` (this is the direct-postgres-store's own diagnostic, independent of the `server.py` wrapper's). |
| `server.py` | `_HostedPublication.begin_from_mapping`: include `max_batch_rows`/`max_batch_bytes` from `self._settings` in the returned mapping. `_HostedPublication.publish_from_mapping`: `_CODE_INVALID_BATCH` len/byte-count branches gain `limit`/`received`. `_publish_local_snapshot`: select batch-sizing source per §3. |
| `codegraph/mcp_adapter.py` | `McpSnapshotPublisher.begin()`: parse `max_batch_rows`/`max_batch_bytes` (optional keys) from the decoded remote response into the returned `PublicationSession`. |
| `codegraph/sqlite_adapter.py` | `SqliteSnapshotPublisher.begin()`: no change to logic — its returned `PublicationSession` simply omits the two new fields (defaults apply). |

No change to `SnapshotHeader`, `SnapshotBatch`, `iter_snapshot_batches`, `canonical_batch`,
or any hashing/canonical-representation code — the fix is entirely in what informs the
`max_rows`/`max_bytes` arguments passed into the existing, unmodified batching call.

## 3. Client-side validation of server-reported limits

`_publish_local_snapshot` does not trust a server-reported value blindly (a malformed or
buggy server response must not silently degrade to zero-row batches, an infinite loop, or
a value exceeding this codebase's own known hard ceiling):

```python
def _effective_batch_bounds(session, config):
    rows_limit = session.max_batch_rows
    bytes_limit = session.max_batch_bytes
    if (
        not isinstance(rows_limit, int) or isinstance(rows_limit, bool)
        or not 1 <= rows_limit <= 5000
    ):
        rows_limit = config.max_batch_rows
    if (
        not isinstance(bytes_limit, int) or isinstance(bytes_limit, bool)
        or not 1 <= bytes_limit <= 5_000_000
    ):
        bytes_limit = config.max_batch_bytes
    return rows_limit, bytes_limit
```

The `(1, 5000)` / `(1, 5_000_000)` bounds are the same hard ceiling already enforced
server-side in `HostedCodeGraphConfig.__post_init__` — reused here as the client's own
sanity check, not a new number invented for this fix. A `None` (sqlite target, or an
older server that hasn't shipped this field yet) falls through the `isinstance` check and
uses `config`'s value, preserving today's behavior with zero special-casing for "old
server" — this is what makes the change backward compatible with no version negotiation.

## 4. Error handling

- Server-reported limits are advisory-but-validated inputs, never trusted blindly (§3).
- If the server's own configured limit is itself pathological (e.g. an admin misconfigures
  `HostedCodeGraphConfig` outside its own `(1, 5000)` bounds) — `HostedCodeGraphConfig.
  __post_init__` already raises `ConfigError` at server startup; this design adds no new
  failure mode there.
- A client on an old cached `PublicationSession` shape (e.g. a test double built before
  this change) that omits the new attributes entirely — `session.max_batch_rows` would
  raise `AttributeError` unless the dataclass default (`None`) is honored; since it's a
  dataclass field addition with a default, every existing construction site
  (`PublicationSession(session_id=..., ...)`) keeps working unchanged (Python dataclass
  fields with defaults don't require call-site updates).

## 5. Testing

- **Unit — `PublicationSession`:** construct with and without the two new fields, confirm
  defaults.
- **Unit — `_effective_batch_bounds`:** table-driven — valid server value used; `None`
  falls back to config; `0`, negative, `5001`, `True` (bool-is-int trap), non-int all
  fall back to config.
- **Unit — `_HostedPublication.begin_from_mapping`:** returned mapping carries
  `max_batch_rows`/`max_batch_bytes` from `self._settings`, for a real
  `PostgresCodeGraphStore.begin()` success value.
- **Unit — `SqliteSnapshotPublisher.begin()`:** returned session's two new fields are
  `None` (regression guard — this is the "sqlite path untouched" Health Metric made
  concrete).
- **Unit — diagnostic fields:** both `_CODE_INVALID_BATCH` triggers (rows, bytes) in
  `server.py` and both in `postgres/codegraph.py` include correct `limit`/`received`
  values for a constructed over-limit batch; other `invalid_batch` triggers (bad `kind`,
  negative `ordinal`, hash mismatch) do NOT gain these fields (scope guard).
- **Integration — end-to-end via `_HostedPublication`/`PostgresCodeGraphStore` directly**
  (no live network, mirrors this plan's own diagnostic session against `aioperator`):
  build a `CodeGraphIndexer` snapshot with a symbol/relation count exceeding a
  deliberately small `HostedCodeGraphConfig(max_batch_rows=...)`, run it through
  `_publish_local_snapshot`-equivalent logic, confirm `state: ready` (previously this
  reproduced `invalid_batch` before the fix — this is the regression test for the exact
  bug that motivated this intent).
- **Regression — `publish_mode = "sqlite"`:** run the existing `sqlite_adapter.py` test
  suite unmodified; must stay green (Health Metric).

## 6. Out of scope

- Changing the admin hard ceiling (1–5000 rows, 1–5,000,000 bytes) — explicit No-Go per
  intent.
- The other 4 fields shared between `CodeGraphConfig`/`HostedCodeGraphConfig`
  (`max_snapshot_age_seconds`, `publication_session_ttl_seconds`,
  `staging_retention_seconds`, `staging_cleanup_limit`) — confirmed during intent capture
  to already be scoped exclusively to `publish_mode = "sqlite"`, need no change.
- A version-negotiation handshake, capability flags, or protocol version bump — the
  `None`-defaults-to-config fallback already gives full backward compatibility without one.
