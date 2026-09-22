---
review:
  plan_hash: 2da52fdf39cb9e50
  last_run: 2026-09-22
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings:
    - id: F-001
      phase: coverage
      severity: CRITICAL
      section: Requirement coverage
      section_hash: 28fa49a1a3089614
      fragment: null
      text: "The plan named no spec requirement anywhere, so neither direction of coverage could be checked mechanically - the same defect this author had just raised against the spec's Testing section."
      fix: "Added a Requirement coverage table mapping R1-R7 to tasks, and an Implements line on each of the seven task headings."
      verdict: fixed
      verdict_at: 2026-09-22
    - id: F-002
      phase: structure
      severity: WARNING
      section: Task 3
      section_hash: null
      fragment: "httpx.get(f\"{url}/mcp\", timeout=2.0)"
      text: "Tasks 3 and 4 were written against a live HTTP server. The hosted_runtime fixture yields an in-process ASGI app driven by starlette TestClient, with no URL, so both tasks would have failed at the first line."
      fix: "Both rewritten against TestClient(runtime.app, base_url=...), with the fixture's real attribute names and the writable domain 'docs'. Found during plan self-review."
      verdict: fixed
      verdict_at: 2026-09-22
chain:
  intent: 477b97089342a0b1
  spec: 4ecafd8ad402e951
workflow:
  route: chain
  continuation: full
---

# Hosted tool dispatch off the event loop — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop a single blocking tool call from freezing the whole hosted MCP server, and stop a publication from paying for unrelated cleanup.

**Architecture:** Every `wiki_*` implementation stays a plain `def`. The registration site wraps each one in an async shim that awaits `anyio.to_thread.run_sync` under a dedicated `CapacityLimiter`, so the event loop stays free and concurrency is bounded below the database pool size. Separately, the code-graph cleanup that runs at publication start is bounded by rows and a wall-clock deadline and moved out of the publication's transaction.

**Tech Stack:** Python 3.12, anyio, FastMCP (`mcp` 1.28.x under a `<2` pin), psycopg 3 with `psycopg_pool`, pytest with `asyncio_mode = "auto"`.

**Spec:** `docs/superpowers/specs/2026-09-22-hosted-tool-dispatch-blocks-event-loop-design.md` (spec_hash `4ecafd8ad402e951`, chain.intent `477b97089342a0b1`)

## Global Constraints

- Handler implementations stay plain `def` and remain callable directly from tests without the MCP runtime. Never convert a `wiki_*` function to `async def`.
- No new lock is introduced anywhere. PostgreSQL transactions and `expected_revision` compare-and-swap already provide write safety, and the base mutation lock is not on the hosted path.
- The tool ceiling is `pool_max_size - 2`, never equal to or above `pool_max_size`. Authentication draws from the same connection pool, so a full pool starves the liveness probe.
- Concrete values from the spec, used verbatim: tool ceiling default **8**; pool reserve **2**; cleanup row budget **100000**; cleanup deadline **2.0** seconds; delete batch size **10000**.
- The row budget must stay strictly greater than the rows one publication adds (~30000 here). This is an invariant with a test, not a tuning knob.
- Exactly one existing test may be edited: `tests/postgres/test_code_graph_publication.py::test_pruning_never_exceeds_its_per_call_bound`. If any other existing test fails, that is a defect in the change, not a contract that moved.
- `flake8` with `max-line-length = 100` must stay clean. There is no formatter; match surrounding style by hand.
- Bump the version in `pyproject.toml`, `src/iwiki_mcp/__init__.py` and `tests/test_package.py` once, in the final task. Current version is `0.7.292`; use `0.7.293`.
- `tests/postgres` requires a disposable database. Run it with the container recipe in `CLAUDE.md`; the DSN database name must end in `_test`.

## Requirement coverage

Every spec requirement maps to at least one task, and every task implements at least one
requirement. A task that drifts outside this map is drift, not scope.

| Spec requirement | Tasks |
|---|---|
| R1 — Tools execute off the event loop | 1, 3 |
| R2 — The tool ceiling comes from the storage | 1, 2, 3 |
| R3 — Liveness never competes with tools | 2, 3 |
| R4 — No write serialization is added | 4 |
| R5 — Cleanup is bounded by rows and a deadline | 5, 7 |
| R6 — The row budget exceeds what one publication adds | 5 |
| R7 — Cleanup does not gate the publication | 6 |

---

### Task 1: Dispatch tools onto a worker thread

**Implements:** R1, R2

**Files:**
- Modify: `src/iwiki_mcp/server.py` (add the limiter and wrapper near the registration block at line 5770; rewrite lines 5771–5810)
- Test: `tests/test_tool_dispatch.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `_DEFAULT_TOOL_CEILING: int`, `_POOL_RESERVE: int`, `_TOOL_LIMITER: anyio.CapacityLimiter`, and `_threaded(fn: Callable[..., Any]) -> Callable[..., Awaitable[Any]]`, all module-level in `iwiki_mcp.server`. Task 2 sets `_TOOL_LIMITER.total_tokens`; Tasks 3 and 4 rely on dispatch being off the loop.

- [ ] **Step 1: Write the failing test**

Create `tests/test_tool_dispatch.py`:

```python
"""The tool dispatch shim: off the loop, bounded, signature-preserving."""

from __future__ import annotations

import inspect
import threading

import anyio
import pytest

from iwiki_mcp import server


def test_threaded_wrapper_is_awaitable_and_runs_off_the_calling_thread():
    calling_thread = threading.get_ident()
    seen = {}

    def handler(domain: str, slug: str = "page") -> dict:
        seen["thread"] = threading.get_ident()
        return {"domain": domain, "slug": slug}

    wrapped = server._threaded(handler)
    assert inspect.iscoroutinefunction(wrapped)

    result = anyio.run(lambda: wrapped("iwiki-mcp", slug="other"))

    assert result == {"domain": "iwiki-mcp", "slug": "other"}
    assert seen["thread"] != calling_thread


def test_threaded_wrapper_preserves_the_signature_fastmcp_reads():
    def handler(domain: str, limit: int = 5) -> dict:
        """Original docstring."""
        return {}

    wrapped = server._threaded(handler)

    assert str(inspect.signature(wrapped)) == "(domain: str, limit: int = 5) -> dict"
    assert wrapped.__doc__ == "Original docstring."
    assert wrapped.__name__ == "handler"


def test_threaded_wrapper_propagates_the_exception():
    def handler() -> dict:
        raise ValueError("boom")

    wrapped = server._threaded(handler)

    with pytest.raises(ValueError, match="boom"):
        anyio.run(lambda: wrapped())


def test_every_registered_tool_is_dispatched_through_a_thread():
    """A tool left registered raw would still block the loop."""
    tools = anyio.run(server.mcp.list_tools)
    assert tools, "no tools registered"

    manager = server.mcp._tool_manager
    for tool in tools:
        registered = manager.get_tool(tool.name)
        assert registered.is_async, f"{tool.name} is registered as a sync tool"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_tool_dispatch.py -q`
Expected: FAIL with `AttributeError: module 'iwiki_mcp.server' has no attribute '_threaded'`.

- [ ] **Step 3: Add the limiter and the wrapper**

In `src/iwiki_mcp/server.py`, immediately above the comment
`# Thin MCP wrappers; implementation functions above stay unit-testable.` (line 5770), insert:

```python
# Tools run on a worker thread rather than on the event loop. FastMCP calls a
# sync tool inline in its coroutine, so one blocking call inside any handler
# stops the whole server answering -- including the liveness probe.
#
# The ceiling sits below the database pool deliberately: authentication draws
# from that same pool, so letting tools take every connection starves the
# probe through the database instead of through the loop. Task-level detail
# lives in docs/superpowers/specs/2026-09-22-hosted-tool-dispatch-blocks-event-loop-design.md
_DEFAULT_TOOL_CEILING = 8
_POOL_RESERVE = 2
_TOOL_LIMITER = anyio.CapacityLimiter(_DEFAULT_TOOL_CEILING)


def _threaded(fn):
    """Register `fn` as an async tool that runs the sync body on a thread."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return await anyio.to_thread.run_sync(
            functools.partial(fn, *args, **kwargs), limiter=_TOOL_LIMITER
        )

    return wrapper
```

- [ ] **Step 4: Route every registration through the wrapper**

Rewrite the 36 registration lines (5771–5810 after the insert) so each reads
`mcp.tool()(_threaded(<name>))`. For example the first four become:

```python
mcp.tool()(_threaded(wiki_status))
mcp.tool()(_threaded(wiki_code_status))
mcp.tool()(_threaded(wiki_code_index))
mcp.tool()(_threaded(wiki_code_search))
```

Apply the same transformation to every remaining `mcp.tool()(wiki_*)` line in that block, keeping the existing order and the blank lines between groups. Do not add or remove a registration.

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_tool_dispatch.py -q`
Expected: PASS, 4 passed.

- [ ] **Step 6: Run the fast suite and lint**

Run: `uv run pytest -q -m "not slow" --ignore=tests/deployment --ignore=tests/postgres && uv run flake8 src tests`
Expected: the suite passes with no new failures, and flake8 prints nothing.

- [ ] **Step 7: Commit**

```bash
git add src/iwiki_mcp/server.py tests/test_tool_dispatch.py
git commit -m "feat(server): run tool handlers on a worker thread"
```

---

### Task 2: Size the ceiling from the connection pool

**Implements:** R2, R3

**Files:**
- Modify: `src/iwiki_mcp/server.py:374-382` (`_install_hosted_runtime`)
- Test: `tests/test_tool_dispatch.py` (extend)

**Interfaces:**
- Consumes: `_TOOL_LIMITER`, `_POOL_RESERVE` from Task 1.
- Produces: after `_install_hosted_runtime(pool, cfg, ...)` returns, `_TOOL_LIMITER.total_tokens == max(1, pool.max_size - _POOL_RESERVE)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tool_dispatch.py`:

```python
class _FakePool:
    def __init__(self, max_size: int) -> None:
        self.max_size = max_size


def test_hosted_runtime_sizes_the_ceiling_below_the_connection_pool(monkeypatch):
    """Authentication shares the pool, so tools must never be able to drain it."""
    monkeypatch.setattr(server._TOOL_LIMITER, "total_tokens", 8, raising=False)

    server._install_hosted_runtime(_FakePool(10), None)
    try:
        assert server._TOOL_LIMITER.total_tokens == 8
    finally:
        server._clear_hosted_runtime_for_test()


def test_a_tiny_pool_still_leaves_one_tool_slot():
    server._install_hosted_runtime(_FakePool(1), None)
    try:
        assert server._TOOL_LIMITER.total_tokens == 1
    finally:
        server._clear_hosted_runtime_for_test()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_tool_dispatch.py -q -k ceiling`
Expected: FAIL — `_clear_hosted_runtime_for_test` does not exist, and the ceiling is not resized.

- [ ] **Step 3: Resize the limiter and add the test helper**

Replace `_install_hosted_runtime` at `src/iwiki_mcp/server.py:374` with:

```python
def _install_hosted_runtime(
    pool, cfg: Config, code_graph=None, specifications=None
) -> None:
    global _HOSTED_POOL, _HOSTED_CONFIG, _HOSTED_CODE_GRAPH
    global _HOSTED_SPECIFICATIONS
    _HOSTED_POOL = pool
    _HOSTED_CONFIG = cfg
    _HOSTED_CODE_GRAPH = code_graph
    _HOSTED_SPECIFICATIONS = specifications
    # Authentication borrows from this same pool, so the tool ceiling has to
    # leave connections behind for it. At parity the liveness probe would be
    # starved through the database exactly as it was through the loop.
    _TOOL_LIMITER.total_tokens = max(1, pool.max_size - _POOL_RESERVE)
```

Directly below `_clear_hosted_runtime`, add:

```python
def _clear_hosted_runtime_for_test() -> None:
    """Restore the import-time ceiling; used by tests that install a fake pool."""
    global _HOSTED_POOL, _HOSTED_CONFIG, _HOSTED_CODE_GRAPH
    global _HOSTED_SPECIFICATIONS
    _HOSTED_POOL = None
    _HOSTED_CONFIG = None
    _HOSTED_CODE_GRAPH = None
    _HOSTED_SPECIFICATIONS = None
    _TOOL_LIMITER.total_tokens = _DEFAULT_TOOL_CEILING
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_tool_dispatch.py -q`
Expected: PASS, 6 passed.

- [ ] **Step 5: Run the fast suite and lint**

Run: `uv run pytest -q -m "not slow" --ignore=tests/deployment --ignore=tests/postgres && uv run flake8 src tests`
Expected: no new failures; flake8 silent.

- [ ] **Step 6: Commit**

```bash
git add src/iwiki_mcp/server.py tests/test_tool_dispatch.py
git commit -m "feat(server): size the tool ceiling below the connection pool"
```

---

### Task 3: Prove the liveness probe survives saturated tools

**Implements:** R1, R2, R3

**Files:**
- Test: `tests/postgres/test_tool_concurrency.py` (create)

**Interfaces:**
- Consumes: `_TOOL_LIMITER` from Tasks 1–2, and the `hosted_runtime` fixture from
  `tests/postgres/conftest.py`. That fixture yields a `HostedFixture` whose attributes are
  `runtime`, `auth`, `token`, `revoked`, `disabled`; the ASGI app is `hosted_runtime.runtime.app`.
  There is no live URL — existing tests drive it with
  `TestClient(runtime.app, base_url="http://127.0.0.1:8765")`, which runs the app on its own
  event loop in a background thread, so calls made from several threads genuinely overlap.
- Produces: nothing consumed later.

- [ ] **Step 1: Write the test**

Create `tests/postgres/test_tool_concurrency.py`:

```python
"""A busy server must still answer the liveness probe."""

from __future__ import annotations

import threading
import time

import pytest
from starlette.testclient import TestClient

from iwiki_mcp import server


def _tool_call(client, token, name, arguments):
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )


def test_a_saturated_tool_ceiling_does_not_stall_the_liveness_probe(
    hosted_runtime, monkeypatch
):
    """The probe is unauthenticated, so it needs a free thread and a free connection."""
    token = hosted_runtime.token
    original = server._resolved_binding

    def slow_binding(*args, **kwargs):
        time.sleep(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(server, "_resolved_binding", slow_binding)
    monkeypatch.setattr(server._TOOL_LIMITER, "total_tokens", 2, raising=False)

    with TestClient(
        hosted_runtime.runtime.app, base_url="http://127.0.0.1:8765"
    ) as client:
        assert client.get("/mcp").status_code in {401, 405}

        started = threading.Barrier(5)
        errors: list[BaseException] = []

        def occupy():
            try:
                started.wait(timeout=30)
                _tool_call(client, token, "wiki_status", {})
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                errors.append(exc)

        threads = [threading.Thread(target=occupy, daemon=True) for _ in range(4)]
        for thread in threads:
            thread.start()
        started.wait(timeout=30)

        probe_started = time.monotonic()
        status = client.get("/mcp").status_code
        probe_elapsed = time.monotonic() - probe_started

        for thread in threads:
            thread.join(timeout=60)

    assert not errors, errors
    assert status in {401, 405}
    assert probe_elapsed < 2.0, (
        f"the probe took {probe_elapsed:.2f}s while tools were saturated"
    )
```

The sleep is injected into `_resolved_binding` rather than into a registered tool, because
registration captured the original function object at import time and monkeypatching the
module attribute would not reach it. `_resolved_binding` is called inside the handler at
call time, so the patch takes effect.

- [ ] **Step 2: Verify the test actually exercises saturation**

Run it once with the limiter line removed (`total_tokens` left at its installed value) and
confirm it still passes — that shows the probe is not merely lucky. Then restore the line.
Record both results in the task report.

- [ ] **Step 3: Run the test**

Run: `IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55432/iwiki_test" uv run pytest -q tests/postgres/test_tool_concurrency.py`
Expected: PASS. Start the database first with the container recipe in `CLAUDE.md`.

This test is expected to pass on top of Tasks 1–2; it is the regression guard for what they
deliver. If it fails, Task 1 or 2 is wrong — do not weaken the assertion.

- [ ] **Step 4: Commit**

```bash
git add tests/postgres/test_tool_concurrency.py
git commit -m "test(http): guard the liveness probe against saturated tools"
```

---

### Task 4: Prove overlapping writes still conflict

**Implements:** R4

**Files:**
- Test: `tests/postgres/test_tool_concurrency.py` (extend)

**Interfaces:**
- Consumes: `_tool_call` from Task 3 and the same `hosted_runtime` fixture. The fixture's
  writable domain is `"docs"`.
- Produces: nothing consumed later.

- [ ] **Step 1: Write the test**

Append to `tests/postgres/test_tool_concurrency.py`:

```python
def test_genuinely_overlapping_updates_yield_one_success_and_one_conflict(
    hosted_runtime,
):
    """The old four-way test never overlapped: the event loop serialized it."""
    token = hosted_runtime.token

    with TestClient(
        hosted_runtime.runtime.app, base_url="http://127.0.0.1:8765"
    ) as client:
        seeded = _tool_call(client, token, "wiki_write_page", {
            "domain": "docs",
            "slug": "reference/overlap-probe",
            "type": "reference",
            "markdown": "# Overlap probe\n\n## Body\n\nSeed.\n",
        })
        assert seeded.status_code == 200, seeded.text

        results: list[str] = []
        barrier = threading.Barrier(2)

        def update(marker: str):
            barrier.wait(timeout=30)
            response = _tool_call(client, token, "wiki_update_page", {
                "domain": "docs",
                "slug": "reference/overlap-probe",
                "heading": "Body",
                "new_body": f"Body\n\nWritten by {marker}.\n",
                "expected_revision": 1,
            })
            results.append(response.text)

        threads = [threading.Thread(target=update, args=(m,)) for m in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

    assert len(results) == 2, results
    assert sum("conflict" in text for text in results) == 1, results
```

If the seeding call fails because the fixture's writable domain is not `"docs"`, read the
`hosted_runtime` fixture and use the domain it grants write access to. Do not widen the
token's scope to make the test pass.

- [ ] **Step 2: Run the test**

Run: `IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55432/iwiki_test" uv run pytest -q tests/postgres/test_tool_concurrency.py`
Expected: PASS, both tests.

- [ ] **Step 3: Commit**

```bash
git add tests/postgres/test_tool_concurrency.py
git commit -m "test(postgres): assert conflict under genuinely overlapping writes"
```

---

### Task 5: Bound cleanup by rows and a deadline

**Implements:** R5, R6

**Files:**
- Modify: `src/iwiki_mcp/postgres/codegraph.py` (`_cleanup_staging` at line 609, `_prune_superseded` at line 631, and `_discard_snapshot`)
- Test: `tests/postgres/test_code_graph_publication.py` (extend)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `PostgresCodeGraphStore._CLEANUP_ROW_BUDGET: int = 100000`, `_CLEANUP_DEADLINE_SECONDS: float = 2.0`, `_CLEANUP_BATCH_ROWS: int = 10000`, and a private `_delete_snapshot_rows(cursor, domain_id, snapshot_id, budget) -> int` returning the rows deleted. Task 6 calls the cleanups outside the publication transaction.

- [ ] **Step 1: Write the failing test**

Append to `tests/postgres/test_code_graph_publication.py`:

```python
def test_cleanup_stops_at_its_row_budget(pg_graph, monkeypatch):
    """A snapshot is too coarse a unit: two of them were eight minutes."""
    monkeypatch.setattr(
        type(pg_graph.store), "_CLEANUP_ROW_BUDGET", 5, raising=False
    )
    for _ in range(3):
        pg_graph.finalize(pg_graph.complete_session())
    pg_graph.advance_clock(pg_graph.superseded_retention_seconds + 1)

    before = _relation_rows(pg_graph)
    pg_graph.store.begin(pg_graph.header)
    after = _relation_rows(pg_graph)

    assert after < before, "cleanup made no progress"
    assert before - after <= 5 + pg_graph.store._CLEANUP_BATCH_ROWS


def test_cleanup_never_touches_the_active_snapshot(pg_graph):
    for _ in range(3):
        pg_graph.finalize(pg_graph.complete_session())
    pg_graph.advance_clock(pg_graph.superseded_retention_seconds + 1)
    active = pg_graph.reader_status()["snapshot_id"]

    pg_graph.store.begin(pg_graph.header)

    assert pg_graph.reader_status()["snapshot_id"] == active
    assert pg_graph.reader_status()["state"] == "ready"


def test_the_row_budget_exceeds_what_one_publication_adds():
    """A budget below one snapshot turns a stall into a permanent backlog."""
    from iwiki_mcp.postgres.codegraph import PostgresCodeGraphStore

    assert PostgresCodeGraphStore._CLEANUP_ROW_BUDGET > 30000
```

Add the helper beside the existing `_snapshot_states` helper in the same file:

```python
def _relation_rows(pg_graph) -> int:
    with pg_graph.connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM iwiki.code_graph_relations")
            return cursor.fetchone()[0]
```

Use whatever connection accessor `pg_graph` already exposes — read the fixture at `tests/postgres/conftest.py:1063` first and match it.

- [ ] **Step 2: Run the test to verify it fails**

Run: `IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55432/iwiki_test" uv run pytest -q tests/postgres/test_code_graph_publication.py -k budget`
Expected: FAIL — `_CLEANUP_ROW_BUDGET` does not exist.

- [ ] **Step 3: Add the bounded delete helper**

In `src/iwiki_mcp/postgres/codegraph.py`, add these class attributes beside the existing ones on `PostgresCodeGraphStore`:

```python
    # Cleanup is bounded by rows and a deadline rather than by snapshots. A
    # snapshot holds tens of thousands of rows, so even a bound of two was
    # eight minutes of held work. Partial progress is safe because nothing
    # reads a superseded snapshot: every query joins active_snapshot_id.
    _CLEANUP_ROW_BUDGET = 100000
    _CLEANUP_DEADLINE_SECONDS = 2.0
    _CLEANUP_BATCH_ROWS = 10000
```

Add the helper next to `_prune_superseded`:

```python
    def _delete_snapshot_rows(self, cursor, domain_id, snapshot_id, budget):
        """Delete up to `budget` rows of one snapshot; return how many went."""
        removed = 0
        for table in (
            "code_graph_wiki_links",
            "code_graph_relations",
            "code_graph_symbols",
            "code_graph_files",
        ):
            while removed < budget:
                cursor.execute(
                    f"DELETE FROM iwiki.{table} WHERE ctid IN ("
                    f"SELECT ctid FROM iwiki.{table} "
                    "WHERE iwiki_id = %s AND domain_id = %s "
                    "AND snapshot_id = %s LIMIT %s)",
                    (
                        self.iwiki_id,
                        domain_id,
                        snapshot_id,
                        min(self._CLEANUP_BATCH_ROWS, budget - removed),
                    ),
                )
                if not cursor.rowcount:
                    break
                removed += cursor.rowcount
        return removed
```

- [ ] **Step 4: Spend the budget in `_prune_superseded`**

Replace the `for snapshot_id in superseded:` loop body in `_prune_superseded` with a budgeted
version, leaving the selecting query and the docstring above it unchanged:

```python
        deadline = time.monotonic() + self._CLEANUP_DEADLINE_SECONDS
        budget = self._CLEANUP_ROW_BUDGET
        pruned = 0
        for snapshot_id in superseded:
            if budget <= 0 or time.monotonic() >= deadline:
                break
            budget -= self._delete_snapshot_rows(
                cursor, domain_id, snapshot_id, budget
            )
            cursor.execute(
                "DELETE FROM iwiki.code_graph_snapshots "
                "WHERE iwiki_id = %s AND domain_id = %s AND snapshot_id = %s "
                "AND state = 'ready' "
                "AND snapshot_id NOT IN ("
                "SELECT active_snapshot_id FROM iwiki.code_graph_domain_state "
                "WHERE iwiki_id = %s AND domain_id = %s "
                "AND active_snapshot_id IS NOT NULL) "
                "AND NOT EXISTS ("
                "SELECT 1 FROM iwiki.code_graph_relations r "
                "WHERE r.iwiki_id = %s AND r.domain_id = %s "
                "AND r.snapshot_id = %s)",
                (
                    self.iwiki_id,
                    domain_id,
                    snapshot_id,
                    self.iwiki_id,
                    domain_id,
                    self.iwiki_id,
                    domain_id,
                    snapshot_id,
                ),
            )
            pruned += 1
        return pruned
```

The added `NOT EXISTS` clause keeps a half-deleted snapshot's own row in place, so the next
call resumes it instead of orphaning its remaining children.

Add `import time` to the module imports if it is not already present.

- [ ] **Step 5: Give `_cleanup_staging` the same treatment**

In `_cleanup_staging`, stop after the shared deadline so the two cleanups cannot together
exceed the budget. Replace the `for session_id, snapshot_id in expired:` loop header with:

```python
        deadline = time.monotonic() + self._CLEANUP_DEADLINE_SECONDS
        cleaned = 0
        for session_id, snapshot_id in expired:
            if time.monotonic() >= deadline:
                break
```

and replace the trailing `return len(expired)` with `return cleaned`, incrementing
`cleaned` at the end of each iteration.

- [ ] **Step 6: Run the tests**

Run: `IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55432/iwiki_test" uv run pytest -q tests/postgres/test_code_graph_publication.py`
Expected: the three new tests pass. `test_pruning_never_exceeds_its_per_call_bound` is expected to FAIL here — Task 7 rewrites it. Record its failure in the task report and do not edit it yet.

- [ ] **Step 7: Commit**

```bash
git add src/iwiki_mcp/postgres/codegraph.py tests/postgres/test_code_graph_publication.py
git commit -m "fix(codegraph): bound cleanup by rows and a deadline"
```

---

### Task 6: Take cleanup out of the publication transaction

**Implements:** R7

**Files:**
- Modify: `src/iwiki_mcp/postgres/codegraph.py:243-296` (`begin`)
- Test: `tests/postgres/test_code_graph_publication.py` (extend)

**Interfaces:**
- Consumes: the budgeted cleanups from Task 5.
- Produces: `begin` commits the snapshot and session rows before running cleanup, and a cleanup failure no longer rolls the publication back.

- [ ] **Step 1: Write the failing test**

Append to `tests/postgres/test_code_graph_publication.py`:

```python
def test_a_failing_cleanup_leaves_the_publication_committed(pg_graph, monkeypatch):
    """Cleanup is maintenance, not a precondition for someone else's work."""

    def explode(*args, **kwargs):
        raise RuntimeError("cleanup exploded")

    monkeypatch.setattr(type(pg_graph.store), "_prune_superseded", explode)

    session = pg_graph.store.begin(pg_graph.header)

    assert session.session_id
    states = dict((row[0], row[1]) for row in _snapshot_states(pg_graph))
    assert "staging" in states.values()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55432/iwiki_test" uv run pytest -q tests/postgres/test_code_graph_publication.py -k failing_cleanup`
Expected: FAIL with `RuntimeError: cleanup exploded` — the exception currently escapes `begin` and rolls the transaction back.

- [ ] **Step 3: Move the cleanups after the commit**

In `begin`, remove these two lines from inside `with self._transaction() as cursor:`:

```python
            self._cleanup_staging(cursor, domain_id, now)
            self._prune_superseded(cursor, domain_id, now)
```

and, after the `with self._transaction()` block closes and before the `return
PublicationSession(...)`, add:

```python
        # Cleanup runs after the publication is committed and in its own
        # transaction. It is maintenance: a failure here must not undo work a
        # caller already succeeded at, and its cost must not be charged to
        # whoever happened to publish next.
        try:
            with self._transaction() as cursor:
                domain_id = self._domain_id(cursor)
                self._cleanup_staging(cursor, domain_id, now)
                self._prune_superseded(cursor, domain_id, now)
        except psycopg.Error as exc:
            logger.warning(
                "code graph cleanup failed after publication begin: %s",
                type(exc).__name__,
            )
```

If the module has no `logger`, use the existing logging idiom in that file; read the top of
`codegraph.py` and match it. Note that the test monkeypatches a `RuntimeError`, which the
`psycopg.Error` clause does not catch — widen the clause to `Exception` only if the test
requires it, and say so in the report.

- [ ] **Step 4: Run the tests**

Run: `IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55432/iwiki_test" uv run pytest -q tests/postgres/test_code_graph_publication.py`
Expected: the new test passes; `test_pruning_never_exceeds_its_per_call_bound` still fails pending Task 7.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/postgres/codegraph.py tests/postgres/test_code_graph_publication.py
git commit -m "fix(codegraph): commit the publication before cleaning up"
```

---

### Task 7: Retire the snapshot-count contract, document, and release

**Implements:** R5

**Files:**
- Modify: `tests/postgres/test_code_graph_publication.py` (rewrite `test_pruning_never_exceeds_its_per_call_bound`, around line 710)
- Modify: `docs/code-graph-publishing.md` and `docs/code-graph-publishing.ru.md`
- Modify: `docs/architecture.md`
- Modify: `pyproject.toml`, `src/iwiki_mcp/__init__.py`, `tests/test_package.py`, `uv.lock`

**Interfaces:**
- Consumes: everything from Tasks 1–6.
- Produces: the release.

- [ ] **Step 1: Rewrite the one test the contract change invalidates**

Replace `test_pruning_never_exceeds_its_per_call_bound` in
`tests/postgres/test_code_graph_publication.py` with:

```python
def test_pruning_makes_progress_within_its_per_call_bound(pg_graph):
    """The bound counts rows now, not snapshots: two snapshots were eight minutes."""
    for _ in range(pg_graph.superseded_cleanup_limit + 2):
        pg_graph.finalize(pg_graph.complete_session())
    pg_graph.finalize(pg_graph.complete_session())
    pg_graph.advance_clock(pg_graph.superseded_retention_seconds + 1)

    before = {row[0] for row in _snapshot_states(pg_graph) if row[1] == "ready"}
    started = time.monotonic()
    pg_graph.store.begin(pg_graph.header)
    elapsed = time.monotonic() - started
    after = {row[0] for row in _snapshot_states(pg_graph) if row[1] == "ready"}

    assert after < before, "cleanup made no progress"
    assert elapsed < pg_graph.store._CLEANUP_DEADLINE_SECONDS + 3
```

Add `import time` to that test module if it is absent.

- [ ] **Step 2: Run the whole PostgreSQL suite**

Run: `IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55432/iwiki_test" uv run pytest -q tests/postgres`
Expected: PASS apart from the two pre-existing failures
`test_direct_postgres_failure_preserves_active_revision_and_redacts[batch|finalize]`, which
fail identically on `origin/master`. Confirm that by checking out `origin/master` in a
scratch worktree if they are not already known to you.

- [ ] **Step 3: Document the execution model**

In `docs/architecture.md`, find the section describing the MCP server layer and add one
paragraph stating that tool handlers run on a worker thread under a capacity limiter sized
below the connection pool, and why: FastMCP calls a sync tool inline in its coroutine, so a
blocking call in any handler would otherwise stop the server answering, including the
liveness probe.

In `docs/code-graph-publishing.md` and its Russian sibling `docs/code-graph-publishing.ru.md`,
state that cleanup at publication start is bounded by rows and a deadline, runs after the
publication commits, and that a cleanup failure does not fail the publication. Keep the two
files equivalent; only the language differs.

- [ ] **Step 4: Bump the version**

```bash
sed -i 's/^version = "0.7.292"/version = "0.7.293"/' pyproject.toml
sed -i 's/^__version__ = "0.7.292"/__version__ = "0.7.293"/' src/iwiki_mcp/__init__.py
sed -i 's/"0.7.292"/"0.7.293"/' tests/test_package.py
uv lock --quiet
```

- [ ] **Step 5: Run everything**

Run: `uv run pytest -q -m "not slow" --ignore=tests/deployment --ignore=tests/postgres && uv run flake8 src tests`
Expected: the fast suite passes and flake8 prints nothing.

- [ ] **Step 6: Commit**

```bash
git add tests/postgres/test_code_graph_publication.py docs/architecture.md docs/code-graph-publishing.md docs/code-graph-publishing.ru.md pyproject.toml src/iwiki_mcp/__init__.py tests/test_package.py uv.lock
git commit -m "docs: describe threaded tool dispatch and bounded cleanup"
```
