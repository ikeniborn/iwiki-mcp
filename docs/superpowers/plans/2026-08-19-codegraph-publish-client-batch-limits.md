---
review:
  plan_hash: ab135b82ae73af9a
  last_run: 2026-08-19
  phases:
    structure:
      status: passed
    coverage:
      status: passed
    dependencies:
      status: passed
    verifiability:
      status: passed
    consistency:
      status: passed
  findings: []
chain:
  intent: docs/superpowers/intents/2026-08-19-codegraph-publish-client-batch-limits-intent.md
  spec: docs/superpowers/specs/2026-08-19-codegraph-publish-client-batch-limits-design.md
result_check:
  verdict: OK
  plan_hash: ab135b82ae73af9a
  last_run: 2026-08-19
---
# codegraph-publish-client-batch-limits Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When publishing a code graph snapshot via `publish_mode = "mcp"`, the client
discovers and obeys the hosted server's own `max_batch_rows`/`max_batch_bytes` instead of
its local `.iwiki.toml` values, and an oversized-batch rejection names the actual limit
and what was received.

**Architecture:** `wiki_code_publish_begin`'s response gains two optional integer fields
(`max_batch_rows`, `max_batch_bytes`) sourced from the hosted server's
`HostedCodeGraphConfig`. The client (`server.py::_publish_local_snapshot`) prefers these
over its own `config` values, with a validated fallback to `config` when they are absent,
`None`, or outside this codebase's own hard ceiling (1–5000 rows, 1–5,000,000 bytes) — so
an older client or an older server both keep working unchanged. `publish_mode = "sqlite"`
is untouched: its `begin()` never populates the two new fields, so the fallback always
applies there.

**Tech Stack:** Python 3.10+, `dataclasses`, `pytest`.

**Spec:** [docs/superpowers/specs/2026-08-19-codegraph-publish-client-batch-limits-design.md](../specs/2026-08-19-codegraph-publish-client-batch-limits-design.md)

## Global Constraints

- The server's admin hard ceiling (`max_batch_rows` 1–5000, `max_batch_bytes`
  1–5,000,000, enforced in `HostedCodeGraphConfig.__post_init__`) is never bypassable by
  a client-declared value under any code path (intent hard constraint).
- `publish_mode = "sqlite"` batch sizing is untouched — its existing test suite must
  stay green unmodified.
- No change to `SnapshotHeader`, `SnapshotBatch`, `iter_snapshot_batches`,
  `canonical_batch`, or any hashing/canonical-representation code.
- New `PublicationSession` fields are additive with defaults (`int | None = None`) — no
  existing call site needs updating.
- Diagnostic `limit`/`received` fields are added ONLY to the two size-based
  `_CODE_INVALID_BATCH` branches in `server.py::_HostedPublication.publish_from_mapping`
  (`len(rows) > max_batch_rows`, `byte_count > max_batch_bytes`). No other
  `invalid_batch` trigger anywhere in the codebase (`postgres/codegraph.py`'s kind/
  ordinal validity, row-count/ordinal-contiguity, and referential-integrity checks) gets
  these fields.
- Run `uv run pytest -q -m "not slow"` after every task (the project has a known very
  slow 20,000-file benchmark test marked `slow`; skip it per-task, run it once at the
  final task).
- `flake8` (`max-line-length = 100`) must stay clean: `uv run flake8 src tests`.
- Bump `pyproject.toml` `version` (patch bump) once, in the final docs/versioning task.

---

### Task 1: `PublicationSession` gains optional batch-limit fields

**Files:**
- Modify: `src/iwiki_mcp/codegraph/publication.py` (~line 192, `PublicationSession`)
- Test: `tests/codegraph/test_publication.py`

**Interfaces:**
- Produces: `PublicationSession.max_batch_rows: int | None = None`,
  `PublicationSession.max_batch_bytes: int | None = None` — appended after the existing
  4 fields (`session_id`, `lease_expires_at`, `base_snapshot_revision`,
  `base_markdown_token`), both with a default so every existing construction site
  (`PublicationSession(session_id=..., lease_expires_at=..., base_snapshot_revision=...,
  base_markdown_token=...)`, used across `sqlite_adapter.py`, `postgres/codegraph.py`,
  `mcp_adapter.py`, and multiple test files) keeps compiling and running unchanged.

- [ ] **Step 1: Write the failing test**

Add to `tests/codegraph/test_publication.py`, near the existing
`PublicationSession(...)` construction around line 420 (read that test first — it
constructs a session inline as part of a broader frozen-dataclass smoke test; add a new,
separate test function rather than editing that one):

```python
def test_publication_session_batch_limits_default_to_none():
    session = PublicationSession(
        session_id="opaque",
        lease_expires_at="2026-08-14T00:00:00Z",
        base_snapshot_revision=None,
        base_markdown_token=0,
    )

    assert session.max_batch_rows is None
    assert session.max_batch_bytes is None


def test_publication_session_batch_limits_can_be_set():
    session = PublicationSession(
        session_id="opaque",
        lease_expires_at="2026-08-14T00:00:00Z",
        base_snapshot_revision=None,
        base_markdown_token=0,
        max_batch_rows=250,
        max_batch_bytes=500_000,
    )

    assert session.max_batch_rows == 250
    assert session.max_batch_bytes == 500_000
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/codegraph/test_publication.py -k batch_limits -v`
Expected: FAIL — `PublicationSession.__init__() got an unexpected keyword argument
'max_batch_rows'` for the second test; the first test fails with `AttributeError:
'PublicationSession' object has no attribute 'max_batch_rows'`.

- [ ] **Step 3: Implement**

In `src/iwiki_mcp/codegraph/publication.py`, change:

```python
@dataclass(frozen=True)
class PublicationSession:
    session_id: str
    lease_expires_at: str
    base_snapshot_revision: str | None
    base_markdown_token: str | int
```

to:

```python
@dataclass(frozen=True)
class PublicationSession:
    session_id: str
    lease_expires_at: str
    base_snapshot_revision: str | None
    base_markdown_token: str | int
    max_batch_rows: int | None = None
    max_batch_bytes: int | None = None
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/codegraph/test_publication.py -v
uv run pytest -q -m "not slow"
```

Expected: PASS, full fast suite green — this is a purely additive dataclass change, no
existing test should break.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/codegraph/publication.py tests/codegraph/test_publication.py
git commit -m "feat(codegraph): add optional batch-limit fields to PublicationSession"
```

---

### Task 2: `_HostedPublication.begin_from_mapping` reports the server's batch limits

**Files:**
- Modify: `src/iwiki_mcp/server.py` (~line 1002, `_HostedPublication.begin_from_mapping`)
- Test: `tests/codegraph/test_server_tools.py`

**Interfaces:**
- Consumes: `PublicationSession.max_batch_rows`/`.max_batch_bytes` from Task 1 (not
  directly — `_HostedPublication.begin_from_mapping` reads `self._settings.
  max_batch_rows`/`.max_batch_bytes`, the SAME `HostedCodeGraphConfig` instance
  `publish_from_mapping`'s existing `len(rows) > self._settings.max_batch_rows` check
  already reads — no new config plumbing).
- Produces: `begin_from_mapping`'s returned mapping gains `"max_batch_rows": int,
  "max_batch_bytes": int` alongside the existing `session_id`/`lease_expires_at`/
  `base_snapshot_revision`/`base_markdown_token` keys. This is the dict that becomes the
  `wiki_code_publish_begin` MCP tool's JSON response.

- [ ] **Step 1: Write the failing test**

Read `src/iwiki_mcp/server.py`'s `_HostedPublication` class (~line 972-1036) and its
`__init__(self, store, settings)` first — it's a thin wrapper that can be constructed
directly with a fake `store` and a real `HostedCodeGraphConfig`, no live PostgreSQL
needed. Add to `tests/codegraph/test_server_tools.py` (search the file first for how
`SnapshotHeader`/`header_payload` are imported/constructed elsewhere in it, to match the
existing import style):

```python
from iwiki_mcp.postgres.config import HostedCodeGraphConfig
from iwiki_mcp.server import _HostedPublication


class _FakeHostedStore:
    """Records begin() calls; never touches a real database."""

    def __init__(self, session):
        self.domain = "docs"
        self._session = session
        self.begin_calls = []

    def begin(self, header):
        self.begin_calls.append(header)
        return self._session


def test_begin_from_mapping_reports_hosted_batch_limits():
    from iwiki_mcp.codegraph.publication import PublicationSession

    session = PublicationSession(
        session_id="s1",
        lease_expires_at="2026-08-19T00:00:00Z",
        base_snapshot_revision=None,
        base_markdown_token=0,
    )
    store = _FakeHostedStore(session)
    settings = HostedCodeGraphConfig(max_batch_rows=1000, max_batch_bytes=1_000_000)
    publication = _HostedPublication(store, settings)
    header = {
        "protocol_version": 1,
        "schema_version": 2,
        "repository_id": "docs",
        "source_fingerprint": "source",
        "parser_fingerprint": "parser",
        "normalizer_version": "normalizer-1",
        "unicode_data_version": "15.1",
        "languages": ["python"],
        "expected_counts": {
            "repositories": 1, "files": 0, "symbols": 0, "relations": 0
        },
        "graph_payload_revision": "sha256:" + "a" * 64,
    }

    result = publication.begin_from_mapping(header)

    assert result["max_batch_rows"] == 1000
    assert result["max_batch_bytes"] == 1_000_000
    assert result["session_id"] == "s1"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/codegraph/test_server_tools.py -k hosted_batch_limits -v`
Expected: FAIL — `KeyError: 'max_batch_rows'`.

- [ ] **Step 3: Implement**

In `src/iwiki_mcp/server.py`'s `_HostedPublication.begin_from_mapping` (~line 1002-1010),
change:

```python
        session = self._store.begin(parsed)
        if isinstance(session, dict):
            return session
        return {
            "session_id": session.session_id,
            "lease_expires_at": session.lease_expires_at,
            "base_snapshot_revision": session.base_snapshot_revision,
            "base_markdown_token": session.base_markdown_token,
        }
```

to:

```python
        session = self._store.begin(parsed)
        if isinstance(session, dict):
            return session
        return {
            "session_id": session.session_id,
            "lease_expires_at": session.lease_expires_at,
            "base_snapshot_revision": session.base_snapshot_revision,
            "base_markdown_token": session.base_markdown_token,
            "max_batch_rows": self._settings.max_batch_rows,
            "max_batch_bytes": self._settings.max_batch_bytes,
        }
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/codegraph/test_server_tools.py -v
uv run pytest -q -m "not slow"
uv run flake8 src/iwiki_mcp/server.py tests/codegraph/test_server_tools.py
```

Expected: PASS, full fast suite green, flake8 clean.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/server.py tests/codegraph/test_server_tools.py
git commit -m "feat(codegraph): report hosted batch limits in wiki_code_publish_begin"
```

---

### Task 3: `McpSnapshotPublisher.begin()` parses the server's reported batch limits

**Files:**
- Modify: `src/iwiki_mcp/codegraph/mcp_adapter.py` (~line 152-171, `McpSnapshotPublisher.begin`)
- Test: `tests/codegraph/test_mcp_adapter.py`

**Interfaces:**
- Consumes: the two new response keys from Task 2 (`max_batch_rows`, `max_batch_bytes`),
  present only when the remote server has shipped Task 2 — the parsing must tolerate
  their absence (a response dict without these keys, e.g. an older server) exactly as it
  already tolerates `base_snapshot_revision` being `None`.
- Produces: `McpSnapshotPublisher.begin(header)` returns a `PublicationSession` (Task 1)
  with `max_batch_rows`/`max_batch_bytes` populated from the response when present,
  `None` when absent.

- [ ] **Step 1: Write the failing test**

Read `tests/codegraph/test_mcp_adapter.py`'s `fake_session` fixture (~line 53-70) and
`test_mcp_begin_sends_only_the_shared_header` (~line 164-186) first — the existing
fixture's `wiki_code_publish_begin` reply does NOT include the two new keys, so the
EXISTING test already covers the backward-compatible "server hasn't shipped this yet"
case once you add the two assertions below to it; add ONE more, separate test for the
present case using a custom `_FakeSession` reply:

```python
def test_mcp_begin_reports_absent_batch_limits_as_none(fake_session, header):
    # fake_session's canned reply has no max_batch_rows/max_batch_bytes keys —
    # this proves an older-server response doesn't break parsing.
    publisher = McpSnapshotPublisher(_transport(fake_session))

    session = publisher.begin(header)

    assert session.max_batch_rows is None
    assert session.max_batch_bytes is None


def test_mcp_begin_parses_batch_limits_when_present(header):
    session_with_limits = _FakeSession(
        replies={
            "wiki_code_publish_begin": {
                "session_id": "remote-session",
                "lease_expires_at": "2026-08-16T10:00:00+00:00",
                "base_snapshot_revision": "sha256:base",
                "base_markdown_token": 7,
                "max_batch_rows": 1000,
                "max_batch_bytes": 1_000_000,
            },
        }
    )
    publisher = McpSnapshotPublisher(_transport(session_with_limits))

    session = publisher.begin(header)

    assert session.max_batch_rows == 1000
    assert session.max_batch_bytes == 1_000_000
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/codegraph/test_mcp_adapter.py -k batch_limits -v`
Expected: `test_mcp_begin_reports_absent_batch_limits_as_none` PASSES already (the fields
default to `None` per Task 1's dataclass default — nothing to parse yet means nothing
breaks); `test_mcp_begin_parses_batch_limits_when_present` FAILS with `AssertionError:
assert None == 1000` (the field exists but `begin()` never reads it from the response
yet).

- [ ] **Step 3: Implement**

In `src/iwiki_mcp/codegraph/mcp_adapter.py`'s `McpSnapshotPublisher.begin` (~line
152-171), change:

```python
        try:
            return PublicationSession(
                session_id=str(result["session_id"]),
                lease_expires_at=str(result["lease_expires_at"]),
                base_snapshot_revision=(
                    None
                    if result.get("base_snapshot_revision") is None
                    else str(result["base_snapshot_revision"])
                ),
                base_markdown_token=result["base_markdown_token"],
            )
        except KeyError:
            return dict(_REMOTE_FAILED)
```

to:

```python
        try:
            return PublicationSession(
                session_id=str(result["session_id"]),
                lease_expires_at=str(result["lease_expires_at"]),
                base_snapshot_revision=(
                    None
                    if result.get("base_snapshot_revision") is None
                    else str(result["base_snapshot_revision"])
                ),
                base_markdown_token=result["base_markdown_token"],
                max_batch_rows=result.get("max_batch_rows"),
                max_batch_bytes=result.get("max_batch_bytes"),
            )
        except KeyError:
            return dict(_REMOTE_FAILED)
```

(`result.get(...)` — not `result[...]` — is deliberate: these two keys are OPTIONAL in
the response, unlike the four existing required keys still accessed with `[...]`.)

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/codegraph/test_mcp_adapter.py -v
uv run pytest -q -m "not slow"
uv run flake8 src/iwiki_mcp/codegraph/mcp_adapter.py tests/codegraph/test_mcp_adapter.py
```

Expected: PASS, full fast suite green, flake8 clean.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/codegraph/mcp_adapter.py tests/codegraph/test_mcp_adapter.py
git commit -m "feat(codegraph): parse hosted batch limits in McpSnapshotPublisher.begin"
```

---

### Task 4: Client honors server-reported batch limits, validated against a hard ceiling

**Files:**
- Modify: `src/iwiki_mcp/server.py` (~line 1049, `_publish_local_snapshot`; add a new
  `_effective_batch_bounds` helper near it)
- Test: `tests/codegraph/test_server_tools.py`

**Interfaces:**
- Consumes: `PublicationSession.max_batch_rows`/`.max_batch_bytes` (Task 1),
  `CodeGraphConfig.max_batch_rows`/`.max_batch_bytes` (existing, unchanged).
- Produces: `_effective_batch_bounds(session, config) -> tuple[int, int]` — a new,
  standalone function returning `(rows_limit, bytes_limit)`: the session's reported
  value when it is a valid `int` (not `bool`) within `1..5000` / `1..5_000_000`
  respectively, else the corresponding `config` value. `_publish_local_snapshot` calls
  this once, right after `session = publisher.begin(header)` succeeds, and passes its
  result to `iter_snapshot_batches` instead of `config.max_batch_rows`/
  `config.max_batch_bytes` directly.

- [ ] **Step 1: Write the failing tests**

Read `src/iwiki_mcp/server.py`'s current `_publish_local_snapshot` (~line 1049-1070)
first. Add to `tests/codegraph/test_server_tools.py`:

```python
from iwiki_mcp.server import _effective_batch_bounds
from iwiki_mcp.codegraph.config import CodeGraphConfig
from iwiki_mcp.codegraph.publication import PublicationSession


def _session(max_batch_rows=None, max_batch_bytes=None):
    return PublicationSession(
        session_id="s",
        lease_expires_at="2026-08-19T00:00:00Z",
        base_snapshot_revision=None,
        base_markdown_token=0,
        max_batch_rows=max_batch_rows,
        max_batch_bytes=max_batch_bytes,
    )


@pytest.mark.parametrize(
    "reported_rows,reported_bytes,expected_rows,expected_bytes",
    [
        (1000, 1_000_000, 1000, 1_000_000),   # valid server value used
        (None, None, 5000, 5_000_000),        # absent -> config fallback
        (0, 1_000_000, 5000, 1_000_000),      # zero rejected -> config fallback
        (-1, 1_000_000, 5000, 1_000_000),     # negative rejected -> config fallback
        (5001, 1_000_000, 5000, 1_000_000),   # over hard ceiling -> config fallback
        (1000, 0, 1000, 5_000_000),           # zero bytes rejected -> config fallback
        (1000, 5_000_001, 1000, 5_000_000),   # over hard ceiling -> config fallback
        (True, 1_000_000, 5000, 1_000_000),   # bool-is-int trap rejected
    ],
)
def test_effective_batch_bounds_validates_server_value(
    reported_rows, reported_bytes, expected_rows, expected_bytes
):
    session = _session(reported_rows, reported_bytes)
    config = CodeGraphConfig(max_batch_rows=5000, max_batch_bytes=5_000_000)

    rows_limit, bytes_limit = _effective_batch_bounds(session, config)

    assert rows_limit == expected_rows
    assert bytes_limit == expected_bytes
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/codegraph/test_server_tools.py -k effective_batch_bounds -v`
Expected: FAIL — `ImportError: cannot import name '_effective_batch_bounds'`.

- [ ] **Step 3: Implement `_effective_batch_bounds`**

Add to `src/iwiki_mcp/server.py`, right before `_publish_local_snapshot` (~line 1049):

```python
def _effective_batch_bounds(session, config) -> tuple[int, int]:
    """Prefer the hosted server's reported batch bounds, validated against
    this codebase's own hard ceiling; fall back to local config otherwise."""
    rows_limit = session.max_batch_rows
    if (
        not isinstance(rows_limit, int)
        or isinstance(rows_limit, bool)
        or not 1 <= rows_limit <= 5000
    ):
        rows_limit = config.max_batch_rows
    bytes_limit = session.max_batch_bytes
    if (
        not isinstance(bytes_limit, int)
        or isinstance(bytes_limit, bool)
        or not 1 <= bytes_limit <= 5_000_000
    ):
        bytes_limit = config.max_batch_bytes
    return rows_limit, bytes_limit
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/codegraph/test_server_tools.py -k effective_batch_bounds -v
```

Expected: PASS, 8/8 parametrized cases.

- [ ] **Step 5: Write the failing wiring test**

```python
def test_publish_local_snapshot_uses_session_limits_over_config(monkeypatch):
    from iwiki_mcp import server as server_module

    captured = {}
    real_iter = server_module._codegraph_publication.iter_snapshot_batches

    def spying_iter(rows, *, max_rows, max_bytes):
        captured["max_rows"] = max_rows
        captured["max_bytes"] = max_bytes
        return real_iter(rows, max_rows=max_rows, max_bytes=max_bytes)

    monkeypatch.setattr(
        server_module._codegraph_publication, "iter_snapshot_batches", spying_iter
    )

    class _StubPublisher:
        def begin(self, header):
            return _session(max_batch_rows=1000, max_batch_bytes=1_000_000)

        def publish_batch(self, session, batch):
            return {"accepted": True}

        def finalize(self, session):
            return {"state": "ready"}

        def abort(self, session):
            return {"state": "aborted"}

    class _StubRuntime:
        def export_snapshot(self):
            header = _fake_header()  # see note below
            rows = {"repositories": [], "files": [], "symbols": [], "relations": []}
            return header, rows

    monkeypatch.setattr(
        server_module, "_code_publisher", lambda *a, **k: _StubPublisher()
    )
    config = CodeGraphConfig(max_batch_rows=5000, max_batch_bytes=5_000_000)

    server_module._publish_local_snapshot(_StubRuntime(), object(), config)

    assert captured["max_rows"] == 1000
    assert captured["max_bytes"] == 1_000_000
```

Before writing this test for real, search `tests/codegraph/test_server_tools.py` (and
`tests/codegraph/conftest.py`) for an existing helper that builds a minimal valid
`SnapshotHeader` for exactly this kind of `export_snapshot()`-stubbing test — reuse it
instead of inventing `_fake_header()` from scratch; several existing tests in this file
already stub `runtime.export_snapshot()` for `_publish_local_snapshot`-adjacent
coverage (search for `export_snapshot` in this file first). If no such helper exists,
build the `SnapshotHeader` inline using the same field values already used in Task 2's
test.

- [ ] **Step 6: Run the test to verify it fails**

Run: `uv run pytest tests/codegraph/test_server_tools.py -k session_limits_over_config -v`
Expected: FAIL — `captured["max_rows"] == 5000` (still using `config`, not `session`).

- [ ] **Step 7: Implement the wiring**

In `src/iwiki_mcp/server.py`'s `_publish_local_snapshot` (~line 1049-1070), change:

```python
def _publish_local_snapshot(runtime, binding, config) -> dict:
    """Send the freshly indexed local snapshot to the selected publisher."""
    publisher = _code_publisher(binding, config.publish_mode, config)
    if publisher is None:
        return {}
    exported = runtime.export_snapshot()
    if isinstance(exported, dict):
        return exported
    header, rows = exported
    session = publisher.begin(header)
    if isinstance(session, dict):
        return session
    for batch in _codegraph_publication.iter_snapshot_batches(
        rows,
        max_rows=config.max_batch_rows,
        max_bytes=config.max_batch_bytes,
    ):
```

to:

```python
def _publish_local_snapshot(runtime, binding, config) -> dict:
    """Send the freshly indexed local snapshot to the selected publisher."""
    publisher = _code_publisher(binding, config.publish_mode, config)
    if publisher is None:
        return {}
    exported = runtime.export_snapshot()
    if isinstance(exported, dict):
        return exported
    header, rows = exported
    session = publisher.begin(header)
    if isinstance(session, dict):
        return session
    max_rows, max_bytes = _effective_batch_bounds(session, config)
    for batch in _codegraph_publication.iter_snapshot_batches(
        rows,
        max_rows=max_rows,
        max_bytes=max_bytes,
    ):
```

- [ ] **Step 8: Run the tests to verify they pass**

```bash
uv run pytest tests/codegraph/test_server_tools.py -v
uv run pytest -q -m "not slow"
uv run flake8 src/iwiki_mcp/server.py tests/codegraph/test_server_tools.py
```

Expected: PASS, full fast suite green, flake8 clean. This is the core behavioral fix —
if any pre-existing `publish_mode = "sqlite"` test fails here, `SqliteSnapshotPublisher.
begin()` is returning a session with non-`None` `max_batch_rows`/`max_batch_bytes`
somewhere it shouldn't (it must not — verify `src/iwiki_mcp/codegraph/sqlite_adapter.py`
was NOT touched by this task) or the two new fields are somehow always failing the
Task 4 Step 3 validation and always falling through — trace `_effective_batch_bounds`
before assuming the test is wrong.

- [ ] **Step 9: Commit**

```bash
git add src/iwiki_mcp/server.py tests/codegraph/test_server_tools.py
git commit -m "feat(codegraph): use server-reported batch limits when publishing over mcp"
```

---

### Task 5: Diagnostic `limit`/`received` fields on oversized-batch rejection

**Files:**
- Modify: `src/iwiki_mcp/server.py` (~line 1012-1030, `_HostedPublication.publish_from_mapping`)
- Test: `tests/codegraph/test_server_tools.py`

**Interfaces:**
- Consumes: `_FakeHostedStore` and `HostedCodeGraphConfig` construction pattern from
  Task 2's test (reuse, don't duplicate).
- Produces: when `publish_from_mapping` rejects a batch via `len(rows) >
  self._settings.max_batch_rows`, the returned dict is `{"error": "invalid_batch",
  "hint": "send batches that match the declared header", "limit": <int>, "received":
  <int>}` — same shape for the `byte_count > self._settings.max_batch_bytes` branch
  (`"limit"`/`"received"` there report the byte values, not row counts). No other
  `invalid_batch` return site anywhere in the codebase gains these keys.

- [ ] **Step 1: Write the failing tests**

Add to `tests/codegraph/test_server_tools.py`, reusing `_FakeHostedStore` from Task 2:

```python
def test_publish_from_mapping_reports_row_limit_on_rejection():
    store = _FakeHostedStore(_session())
    settings = HostedCodeGraphConfig(max_batch_rows=10, max_batch_bytes=1_000_000)
    publication = _HostedPublication(store, settings)
    rows = [{"file_id": f"f{i}"} for i in range(11)]

    result = publication.publish_from_mapping("s1", "files", 0, rows, "sha256:x")

    assert result == {
        "error": "invalid_batch",
        "hint": "send batches that match the declared header",
        "limit": 10,
        "received": 11,
    }


def test_publish_from_mapping_reports_byte_limit_on_rejection():
    store = _FakeHostedStore(_session())
    settings = HostedCodeGraphConfig(max_batch_rows=1000, max_batch_bytes=10)
    publication = _HostedPublication(store, settings)
    rows = [{"file_id": "f0", "note": "x" * 50}]

    result = publication.publish_from_mapping(
        "s1", "files", 0, rows, "sha256:" + "0" * 64
    )

    assert result["error"] == "invalid_batch"
    assert result["limit"] == 10
    assert result["received"] > 10
```

Read `_HostedPublication.publish_from_mapping`'s current body (~line 1012-1030) first to
confirm the exact byte-count source (`batch.byte_count`, computed by `canonical_batch`
from the row list you pass) before asserting an exact `received` value for the second
test — use `>` against the limit rather than a hardcoded byte count, since
`canonical_batch`'s exact JSON encoding isn't this task's concern.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/codegraph/test_server_tools.py -k reports_row_limit -v`
Run: `uv run pytest tests/codegraph/test_server_tools.py -k reports_byte_limit -v`
Expected: both FAIL — `KeyError: 'limit'` (current `_CODE_INVALID_BATCH` has no such key).

- [ ] **Step 3: Implement**

In `src/iwiki_mcp/server.py`'s `_HostedPublication.publish_from_mapping` (~line
1012-1030), change:

```python
    def publish_from_mapping(
        self, session_id, kind, ordinal, rows, payload_hash
    ) -> dict:
        if (
            not isinstance(rows, list)
            or len(rows) > self._settings.max_batch_rows
            or any(not isinstance(row, dict) for row in rows)
        ):
            return dict(_CODE_INVALID_BATCH)
        try:
            batch = _codegraph_publication.canonical_batch(kind, ordinal, rows)
        except (TypeError, ValueError):
            return dict(_CODE_INVALID_BATCH)
        if (
            batch.payload_hash != payload_hash
            or batch.byte_count > self._settings.max_batch_bytes
        ):
            return dict(_CODE_INVALID_BATCH)
        return self._store.publish_batch(_session_reference(session_id), batch)
```

to:

```python
    def publish_from_mapping(
        self, session_id, kind, ordinal, rows, payload_hash
    ) -> dict:
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) for row in rows
        ):
            return dict(_CODE_INVALID_BATCH)
        if len(rows) > self._settings.max_batch_rows:
            return {
                **_CODE_INVALID_BATCH,
                "limit": self._settings.max_batch_rows,
                "received": len(rows),
            }
        try:
            batch = _codegraph_publication.canonical_batch(kind, ordinal, rows)
        except (TypeError, ValueError):
            return dict(_CODE_INVALID_BATCH)
        if batch.payload_hash != payload_hash:
            return dict(_CODE_INVALID_BATCH)
        if batch.byte_count > self._settings.max_batch_bytes:
            return {
                **_CODE_INVALID_BATCH,
                "limit": self._settings.max_batch_bytes,
                "received": batch.byte_count,
            }
        return self._store.publish_batch(_session_reference(session_id), batch)
```

(The row-count check is split out and moved before `canonical_batch(...)` — it was
already effectively checked first via short-circuit `or` in the original `if`, so this
preserves exact prior behavior/ordering, just makes the size branches individually
distinguishable so each can carry its own `limit`/`received`.)

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/codegraph/test_server_tools.py -v
uv run pytest -q -m "not slow"
uv run flake8 src/iwiki_mcp/server.py tests/codegraph/test_server_tools.py
```

Expected: PASS, full fast suite green, flake8 clean. Specifically confirm no existing
test asserts `_CODE_INVALID_BATCH`'s dict shape via exact equality without expecting the
new keys for the size-triggered cases — if one does, it was asserting the OLD (soon to
be diagnostic-poor) behavior on purpose or by omission; if it's the malformed-`kind`
non-list, or hash-mismatch path, it must NOT gain `limit`/`received` and should still
pass unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/server.py tests/codegraph/test_server_tools.py
git commit -m "feat(codegraph): report actual limit and received count on oversized batch rejection"
```

---

### Task 6: Mixed-scale regression fixture, docs, and version bump

**Files:**
- Create: `tests/codegraph/test_publish_batch_limits_regression.py`
- Modify: `README.md` (~line 448-451)
- Modify: `docs/README.ru.md` (~line 448-451, translated)
- Modify: `pyproject.toml` (`version`)
- Wiki (via iwiki MCP tools, not a repo file): `concept/code-graph-publication`

**Interfaces:** none (verification + documentation only).

- [ ] **Step 1: Write the end-to-end regression test**

This proves the exact bug that motivated this plan (a project whose local
`max_batch_rows` exceeds the hosted server's default) no longer reproduces, using only
the fakes/helpers already built in Tasks 2 and 4 — no live PostgreSQL needed:

```python
"""Regression: a client's local max_batch_rows must not leak into publish_mode='mcp'
batch sizing when it exceeds the hosted server's own limit — reproduces the exact
aioperator publish failure this plan fixes."""
from __future__ import annotations

from iwiki_mcp import server as server_module
from iwiki_mcp.codegraph.config import CodeGraphConfig
from iwiki_mcp.codegraph.publication import SnapshotHeader
from iwiki_mcp.postgres.config import HostedCodeGraphConfig
from iwiki_mcp.server import _HostedPublication


class _FakeHostedStore:
    def __init__(self, session):
        self.domain = "docs"
        self._session = session

    def begin(self, header):
        return self._session


def test_client_local_max_batch_rows_above_server_default_no_longer_rejected():
    from iwiki_mcp.codegraph.publication import PublicationSession

    # The hosted server's own (unconfigured-default-shaped) limit — matches
    # HostedCodeGraphConfig's real default of 1000, reproducing the aioperator case
    # where the remote server.toml had no [code_graph] section at all.
    settings = HostedCodeGraphConfig()
    assert settings.max_batch_rows == 1000

    session = PublicationSession(
        session_id="s1",
        lease_expires_at="2026-08-19T00:00:00Z",
        base_snapshot_revision=None,
        base_markdown_token=0,
        max_batch_rows=settings.max_batch_rows,
        max_batch_bytes=settings.max_batch_bytes,
    )
    # The client project's local .iwiki.toml — matches aioperator's real config,
    # larger than the server's default.
    config = CodeGraphConfig(max_batch_rows=5000, max_batch_bytes=1_000_000)

    max_rows, max_bytes = server_module._effective_batch_bounds(session, config)

    # Before this plan: this would be 5000 (config), producing a single oversized
    # batch of e.g. 4001 symbol rows that the server then rejects with invalid_batch.
    assert max_rows == 1000
    # A batch built with this bound can never exceed the server's real limit.
    assert max_rows <= settings.max_batch_rows

    store = _FakeHostedStore(session)
    publication = _HostedPublication(store, settings)
    oversized_rows = [{"symbol_id": f"sym{i}"} for i in range(4001)]

    # Confirm the OLD failure mode is real and reproducible against these exact
    # numbers (this assertion documents the bug this plan fixes, it is not itself
    # the fix under test):
    rejected = publication.publish_from_mapping(
        "s1", "symbols", 0, oversized_rows, "sha256:" + "0" * 64
    )
    assert rejected["error"] == "invalid_batch"
    assert rejected["limit"] == 1000
    assert rejected["received"] == 4001

    # But a client using the FIXED _effective_batch_bounds never builds a batch
    # this large in the first place — chunking 4001 rows at max_rows=1000 yields
    # batches of size <= 1000, all of which pass:
    chunk = oversized_rows[:max_rows]
    assert len(chunk) <= settings.max_batch_rows
    accepted = publication.publish_from_mapping(
        "s1", "symbols", 0, chunk, "sha256:" + "1" * 64
    )
    assert "error" not in accepted
```

- [ ] **Step 2: Run the test to verify it passes**

```bash
uv run pytest tests/codegraph/test_publish_batch_limits_regression.py -v
```

Expected: PASS (Tasks 1-5 already implement everything this test exercises — this task
adds no new production code, only the regression proof).

- [ ] **Step 3: Update README.md**

Change (~line 448-451):
```
A ready snapshot older than a positive `max_snapshot_age_seconds` returns
`stale_snapshot` and no rows, while status keeps reporting age and timestamps. Value
`0` disables age rejection entirely. The hosted server enforces its own validated
ceilings for the numeric fields; a remote client cannot raise them.
```
to:
```
A ready snapshot older than a positive `max_snapshot_age_seconds` returns
`stale_snapshot` and no rows, while status keeps reporting age and timestamps. Value
`0` disables age rejection entirely. The hosted server enforces its own validated
ceilings for the numeric fields; a remote client cannot raise them. For `max_batch_rows`
and `max_batch_bytes` specifically, `publish_mode = "mcp"` discovers the server's actual
limits from `wiki_code_publish_begin`'s response and sizes batches to them automatically
— a local `.iwiki.toml` value larger than the server's own is never sent as-is, and a
rejection states the exact limit and what was received instead of a bare `invalid_batch`.
```

- [ ] **Step 4: Apply the same change to `docs/README.ru.md`** (translated)

```
Готовый снапшот старше положительного `max_snapshot_age_seconds` возвращает
`stale_snapshot` без строк, при этом статус продолжает сообщать возраст и метки
времени. Значение `0` полностью отключает отбраковку по возрасту. Хостящий сервер
применяет собственные проверенные потолки для числовых полей; удалённый клиент не
может их поднять. Для `max_batch_rows` и `max_batch_bytes` в частности,
`publish_mode = "mcp"` узнаёт реальные лимиты сервера из ответа
`wiki_code_publish_begin` и автоматически подгоняет под них размер батчей — локальное
значение в `.iwiki.toml`, большее серверного, никогда не отправляется как есть, а
отказ называет точный лимит и полученное значение вместо голого `invalid_batch`.
```

- [ ] **Step 5: Bump the version**

In `pyproject.toml`, bump `version = "0.7.150"` to `version = "0.7.151"` (patch bump per
`CLAUDE.md` Versioning — this plan is a bug fix plus a small backward-compatible
protocol addition, not a breaking or minor-worthy release).

- [ ] **Step 6: Update the wiki (iwiki MCP tools)**

Apply the iwiki Project Binding protocol from `CLAUDE.md` (bind `read`/`write`/
`primary` from `.iwiki.toml`, confirm with `wiki_status`), then:

- `wiki_read_page(domain="iwiki-mcp", slug="concept/code-graph-publication")` to get
  its current `revision`, then `wiki_update_page(domain="iwiki-mcp",
  slug="concept/code-graph-publication", heading="Header and errors",
  expected_revision=<revision>, new_body=..., source=...)` — read that section's
  current full text first (it documents the closed error-code-set contract this plan's
  `limit`/`received` addition must respect) and add one clarifying sentence: the
  publication error set gains no new codes; `invalid_batch`'s two size-triggered
  branches additionally carry safe numeric `limit`/`received` metadata, and
  `wiki_code_publish_begin`'s response additionally reports the hosted server's
  effective `max_batch_rows`/`max_batch_bytes` so a remote client can size batches to
  match instead of guessing from its own local config.
- `wiki_lint()` — confirm no new finding.

- [ ] **Step 7: Final full-suite verification and commit**

```bash
uv run pytest -q -m "not slow"
uv run pytest -q
uv run flake8 src tests
git add tests/codegraph/test_publish_batch_limits_regression.py README.md docs/README.ru.md pyproject.toml
git commit -m "docs: document hosted batch-limit discovery, bump version to 0.7.151"
```

Run BOTH the fast suite and the full suite (including the slow 20,000-file benchmark) —
this is the final task, confirm zero regressions across the whole plan before it's
considered done.

---

## Definition of Done (traces to spec Acceptance)

- [ ] A client whose `.iwiki.toml` `max_batch_rows`/`max_batch_bytes` exceed the hosted
      server's real limit publishes without any manual `.iwiki.toml` edit — Task 4
      (`_effective_batch_bounds`), proven end-to-end in Task 6's regression test.
- [ ] `publish_mode = "sqlite"` behavior is unchanged — `SqliteSnapshotPublisher` is
      never modified by any task; Task 4's wiring falls back to `config` whenever
      `session.max_batch_rows`/`.max_batch_bytes` are `None` (always true for sqlite).
- [ ] An oversized-batch rejection states the actual limit and what was received — Task 5.
- [ ] The server's admin hard ceiling (1–5000 rows, 1–5,000,000 bytes) is never
      bypassable — Task 4's `_effective_batch_bounds` validates the server-reported
      value against exactly this range before trusting it.
- [ ] `/check-chain result docs/superpowers/plans/2026-08-19-codegraph-publish-client-batch-limits.md`
      run after Task 6, reconciling this plan's steps against the final `git diff`.
