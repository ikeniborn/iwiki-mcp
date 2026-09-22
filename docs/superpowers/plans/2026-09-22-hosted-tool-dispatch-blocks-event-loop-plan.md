---
review:
  plan_hash: 770a630aead71c9c
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
    - id: F-003
      phase: coverage
      severity: CRITICAL
      section: Task 5
      section_hash: null
      fragment: "a maximum number of deleted rows and a wall-clock deadline"
      text: "The Task 5 review proved the design arithmetically impossible: a publication adds ~30000 rows and a 2-second in-band budget removes a few hundred, so begin-fast and backlog-draining could not both hold with cleanup inside begin. Returned to the spec gate rather than patched in execution."
      fix: "Spec R5-R7 rewritten and re-gated at f304669554881634; plan Tasks 5-7 rewritten to match. Cleanup now runs on a single-flight background worker, begin only schedules it, and R6 is verified by measuring the row count across publications instead of comparing constants. The review's cascade finding is folded into Task 5: the snapshot row is deleted only once code_graph_files is empty."
      verdict: fixed
      verdict_at: 2026-09-22
chain:
  intent: 477b97089342a0b1
  spec: f304669554881634
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
- Concrete values from the spec, used verbatim: tool ceiling default **8**; pool reserve **2**; delete batch size **10000**; cleanup cycle ceiling **200000**.
- The backlog must drain rather than merely stall, and that is verified by measuring the row count across publications — not by asserting one constant exceeds another.
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
| R5 — Cleanup runs outside the publication, in a single-flight worker | 5, 6 |
| R6 — The backlog drains rather than growing | 7 |
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

### Task 5: Make the batched delete resumable and cascade-safe

**Implements:** R5

**Files:**
- Modify: `src/iwiki_mcp/postgres/codegraph.py` (`_delete_snapshot_rows`, `_prune_superseded`, `_cleanup_staging`)
- Test: `tests/postgres/test_code_graph_publication.py` (adjust the three tests added in commit 9c3c910)

**Context you need.** Commit `9c3c910` already added `_CLEANUP_ROW_BUDGET`, `_CLEANUP_DEADLINE_SECONDS`, `_CLEANUP_BATCH_ROWS`, a batched `_delete_snapshot_rows`, and a 2-second deadline checked between snapshots. The design has since changed: cleanup no longer runs inside `begin` and no longer has a wall-clock deadline. This task reshapes what is there; Task 6 moves the call site.

**Interfaces:**
- Consumes: the helpers added in `9c3c910`.
- Produces: `_CLEANUP_BATCH_ROWS = 10000`, `_CLEANUP_CYCLE_ROWS = 200000`, and `_prune_superseded(cursor, domain_id, now, budget)` returning the number of rows deleted. `_CLEANUP_DEADLINE_SECONDS` and `_CLEANUP_ROW_BUDGET` are removed.

- [ ] **Step 1: Write the failing test**

Replace `test_cleanup_stops_at_its_row_budget` in `tests/postgres/test_code_graph_publication.py` with:

```python
def test_cleanup_deletes_a_snapshot_row_only_after_every_child_is_gone(pg_graph):
    """Guarding on one table lets the unindexed cascade fire mid-snapshot."""
    for _ in range(3):
        pg_graph.finalize(pg_graph.complete_session())
    pg_graph.advance_clock(pg_graph.superseded_retention_seconds + 1)

    store = pg_graph.store
    with store._transaction() as cursor:
        domain_id = store._domain_id(cursor)
        store._prune_superseded(cursor, domain_id, store._clock(), 1)

    orphans = pg_graph._query(
        "SELECT count(*) FROM iwiki.code_graph_symbols s "
        "WHERE s.iwiki_id = %s AND NOT EXISTS ("
        "SELECT 1 FROM iwiki.code_graph_snapshots p "
        "WHERE p.iwiki_id = s.iwiki_id AND p.domain_id = s.domain_id "
        "AND p.snapshot_id = s.snapshot_id)",
        (pg_graph.iwiki_id,),
        admin=True,
    )[0][0]
    assert orphans == 0, "a snapshot row was deleted while children remained"
```

Keep `test_cleanup_never_touches_the_active_snapshot` as it stands. Delete `test_the_row_budget_exceeds_what_one_publication_adds` — Task 7 replaces it with a test that measures the drain instead of comparing constants.

- [ ] **Step 2: Run it to verify it fails**

Run: `IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55432/iwiki_test" uv run pytest -q tests/postgres/test_code_graph_publication.py -k child`
Expected: FAIL — `_prune_superseded` does not yet take a budget argument.

- [ ] **Step 3: Reshape the constants**

In `src/iwiki_mcp/postgres/codegraph.py`, replace the three class attributes added in `9c3c910` with:

```python
    # Cleanup runs on a background worker, not inside a publication, so it
    # carries no wall-clock deadline: nothing is waiting on it. The cycle
    # ceiling only stops one cycle running unboundedly against a pathological
    # backlog; it sits far above the ~30000 rows one publication adds, so a
    # ceiling-limited cycle still drains faster than publications accumulate.
    _CLEANUP_BATCH_ROWS = 10000
    _CLEANUP_CYCLE_ROWS = 200000
```

- [ ] **Step 4: Make the snapshot-row delete wait for every child**

In `_prune_superseded`, change the signature to `def _prune_superseded(self, cursor, domain_id, now, budget):`, drop the `deadline` variable and every `time.monotonic()` check, and use the passed `budget` in place of `self._CLEANUP_ROW_BUDGET`.

Replace the `NOT EXISTS` clause on the snapshot-row delete so it guards all four child tables rather than only `code_graph_relations`:

```python
                "AND NOT EXISTS ("
                "SELECT 1 FROM iwiki.code_graph_files f "
                "WHERE f.iwiki_id = %s AND f.domain_id = %s "
                "AND f.snapshot_id = %s)",
```

`code_graph_files` is drained last by `_delete_snapshot_rows`, so its emptiness implies the other three are empty. Adjust the parameter tuple to match. Leave the selecting query and the docstring above it unchanged, except for its final sentence — see Task 7.

- [ ] **Step 5: Restore `_cleanup_staging` to its original shape**

Undo the deadline changes `9c3c910` made to `_cleanup_staging`: remove the `deadline` variable, the `time.monotonic()` check and the `cleaned` counter, and restore `return len(expired)`. Staging discard touches no child rows, so it needs no budget.

If `time` is no longer used anywhere in the module after this step, remove the import.

- [ ] **Step 6: Run the tests**

Run: `IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55432/iwiki_test" uv run pytest -q tests/postgres/test_code_graph_publication.py`
Expected: all pass. `_prune_superseded` is still called from inside `begin` at this point, with `self._CLEANUP_CYCLE_ROWS` as the budget — Task 6 moves it.

- [ ] **Step 7: Commit**

```bash
git add src/iwiki_mcp/postgres/codegraph.py tests/postgres/test_code_graph_publication.py
git commit -m "fix(codegraph): make the batched prune resumable and cascade-safe"
```

---

### Task 6: Schedule cleanup on a single-flight worker

**Implements:** R5, R7

**Files:**
- Modify: `src/iwiki_mcp/postgres/codegraph.py` (`begin`, plus a new `_schedule_cleanup` and `_run_cleanup_cycle`)
- Test: `tests/postgres/test_code_graph_publication.py` (extend)

**Interfaces:**
- Consumes: `_prune_superseded(cursor, domain_id, now, budget)` and `_CLEANUP_CYCLE_ROWS` from Task 5.
- Produces: `begin` returns without performing cleanup; `PostgresCodeGraphStore._cleanup_running` (a `threading.Event` or flag guarded by a `threading.Lock`) and `_schedule_cleanup(domain_id)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/postgres/test_code_graph_publication.py`:

```python
def test_begin_does_not_wait_for_cleanup(pg_graph, monkeypatch):
    """begin's cost must not vary with the size of the backlog."""
    scheduled = []
    monkeypatch.setattr(
        type(pg_graph.store),
        "_schedule_cleanup",
        lambda self, domain_id: scheduled.append(domain_id),
    )
    for _ in range(3):
        pg_graph.finalize(pg_graph.complete_session())
    pg_graph.advance_clock(pg_graph.superseded_retention_seconds + 1)

    before = _relation_rows(pg_graph)
    pg_graph.store.begin(pg_graph.header)

    assert scheduled, "begin did not schedule cleanup"
    assert _relation_rows(pg_graph) >= before, "begin performed cleanup inline"


def test_a_failing_cleanup_leaves_the_publication_committed(pg_graph, monkeypatch):
    """Cleanup is maintenance, not a precondition for someone else's work."""

    def explode(self, domain_id):
        raise RuntimeError("cleanup exploded")

    monkeypatch.setattr(type(pg_graph.store), "_schedule_cleanup", explode)

    session = pg_graph.store.begin(pg_graph.header)

    assert session.session_id
    assert "staging" in {row[1] for row in _snapshot_states(pg_graph)}


def test_a_second_publication_mid_cycle_schedules_nothing(pg_graph, monkeypatch):
    cycles = []
    monkeypatch.setattr(
        type(pg_graph.store),
        "_run_cleanup_cycle",
        lambda self, domain_id: cycles.append(domain_id),
    )
    store = pg_graph.store
    store._cleanup_lock.acquire()
    try:
        store._cleanup_active = True
        store._schedule_cleanup(1)
    finally:
        store._cleanup_active = False
        store._cleanup_lock.release()

    assert cycles == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55432/iwiki_test" uv run pytest -q tests/postgres/test_code_graph_publication.py -k "cleanup or begin_does_not"`
Expected: FAIL — `_schedule_cleanup` does not exist.

- [ ] **Step 3: Add the single-flight worker**

Add `import threading` to `codegraph.py` if absent. In `PostgresCodeGraphStore.__init__`, after the existing assignments, add:

```python
        self._cleanup_lock = threading.Lock()
        self._cleanup_active = False
```

Add these two methods next to `_prune_superseded`:

```python
    def _schedule_cleanup(self, domain_id: int) -> None:
        """Start one cleanup cycle unless one is already running.

        Cleanup is deliberately not part of a publication: a publication adds
        about 30000 rows, and draining a backlog takes far longer than any
        budget a caller would tolerate waiting for. Single-flight keeps a burst
        of publications from stacking cycles on the same tables.
        """
        with self._cleanup_lock:
            if self._cleanup_active:
                return
            self._cleanup_active = True
        thread = threading.Thread(
            target=self._run_cleanup_cycle,
            args=(domain_id,),
            name="iwiki-code-graph-cleanup",
            daemon=True,
        )
        thread.start()

    def _run_cleanup_cycle(self, domain_id: int) -> None:
        """Drain this domain's superseded backlog, one committed batch at a time."""
        removed = 0
        try:
            while removed < self._CLEANUP_CYCLE_ROWS:
                with self._transaction() as cursor:
                    batch = self._prune_superseded(
                        cursor,
                        domain_id,
                        self._clock(),
                        self._CLEANUP_CYCLE_ROWS - removed,
                    )
                if not batch:
                    break
                removed += batch
        except Exception as exc:  # noqa: BLE001 - maintenance must not escape
            logger.warning(
                "code graph cleanup cycle failed after %s rows: %s",
                removed,
                type(exc).__name__,
            )
        finally:
            with self._cleanup_lock:
                self._cleanup_active = False
```

`_prune_superseded` must return rows deleted, not snapshots, for the loop above to terminate. Adjust its `return` accordingly.

If the module has no `logger`, read the top of `codegraph.py` and follow whatever logging idiom is already there.

- [ ] **Step 4: Take cleanup out of `begin`**

In `begin`, remove these two lines from inside `with self._transaction() as cursor:`:

```python
            self._cleanup_staging(cursor, domain_id, now)
            self._prune_superseded(cursor, domain_id, now)
```

After the `with self._transaction()` block closes and before `return PublicationSession(...)`, add:

```python
        # Scheduled, never awaited: the publication is already committed and
        # must not be charged for draining someone else's backlog.
        try:
            self._schedule_cleanup(domain_id)
        except Exception as exc:  # noqa: BLE001 - maintenance must not escape
            logger.warning(
                "code graph cleanup could not be scheduled: %s",
                type(exc).__name__,
            )
```

Keep `_cleanup_staging` running inside `begin`'s transaction as it was — it touches no child rows and is cheap.

- [ ] **Step 5: Run the tests**

Run: `IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55432/iwiki_test" uv run pytest -q tests/postgres/test_code_graph_publication.py`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/iwiki_mcp/postgres/codegraph.py tests/postgres/test_code_graph_publication.py
git commit -m "fix(codegraph): schedule cleanup instead of charging the publication"
```

---

### Task 7: Measure the drain, document, and release

**Implements:** R6

**Files:**
- Modify: `tests/postgres/test_code_graph_publication.py` (rewrite `test_pruning_never_exceeds_its_per_call_bound`, add the drain test)
- Modify: `src/iwiki_mcp/postgres/codegraph.py` (the `_prune_superseded` docstring's final sentence)
- Modify: `docs/code-graph-publishing.md`, `docs/code-graph-publishing.ru.md`, `docs/architecture.md`
- Modify: `pyproject.toml`, `src/iwiki_mcp/__init__.py`, `tests/test_package.py`, `uv.lock`

**Interfaces:**
- Consumes: everything from Tasks 1–6.

- [ ] **Step 1: Replace the snapshot-count contract with a measured drain**

Replace `test_pruning_never_exceeds_its_per_call_bound` with:

```python
def test_the_backlog_drains_across_successive_publications(pg_graph):
    """R6 measured as behaviour: a constant comparison passes while it stalls."""
    for _ in range(pg_graph.superseded_cleanup_limit + 3):
        pg_graph.finalize(pg_graph.complete_session())
    pg_graph.advance_clock(pg_graph.superseded_retention_seconds + 1)

    store = pg_graph.store
    before = _relation_rows(pg_graph)
    with store._transaction() as cursor:
        domain_id = store._domain_id(cursor)
    store._run_cleanup_cycle(domain_id)
    after = _relation_rows(pg_graph)

    assert after < before, "the backlog did not shrink"
```

`_run_cleanup_cycle` is called directly rather than through `_schedule_cleanup`, so the test
observes the drain synchronously instead of racing a background thread.

- [ ] **Step 2: Correct the docstring sentence the design reversed**

In `_prune_superseded`'s docstring, replace the final sentence — "A snapshot holds tens of thousands of rows, so the per-call bound counts snapshots, not rows, and must stay small." — with:

```
        A snapshot holds tens of thousands of rows, so the bound counts rows
        and the work is resumable: a cycle may stop mid-snapshot, and the next
        one continues, because nothing reads a superseded snapshot.
```

Leave the rest of the docstring, including the 49-minute cascade reasoning, untouched.

- [ ] **Step 3: Run the whole PostgreSQL suite**

Run: `IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55432/iwiki_test" uv run pytest -q tests/postgres`
Expected: PASS apart from the two pre-existing failures in `tests/postgres/test_code_publish_cli.py`, which fail identically on `origin/master`.

- [ ] **Step 4: Document the execution model and the worker**

In `docs/architecture.md`, add one paragraph to the MCP server section: tool handlers run on a worker thread under a capacity limiter sized below the connection pool, because FastMCP calls a sync tool inline in its coroutine and a blocking call would otherwise stop the server answering, including the liveness probe.

In `docs/code-graph-publishing.md` and its Russian sibling `docs/code-graph-publishing.ru.md`, state that superseded-snapshot cleanup runs on a single-flight background worker rather than inside a publication, that a publication is never delayed or failed by it, and that a cycle drains the backlog in committed batches and resumes after an interruption. Keep the two files equivalent; only the language differs.

- [ ] **Step 5: Bump the version**

```bash
sed -i 's/^version = "0.7.292"/version = "0.7.293"/' pyproject.toml
sed -i 's/^__version__ = "0.7.292"/__version__ = "0.7.293"/' src/iwiki_mcp/__init__.py
sed -i 's/"0.7.292"/"0.7.293"/' tests/test_package.py
uv lock --quiet
```

- [ ] **Step 6: Run everything**

Run: `uv run pytest -q -m "not slow" --ignore=tests/deployment --ignore=tests/postgres && uv run flake8 src tests`
Expected: the fast suite passes and flake8 prints nothing.

- [ ] **Step 7: Commit**

```bash
git add tests/postgres/test_code_graph_publication.py src/iwiki_mcp/postgres/codegraph.py docs/architecture.md docs/code-graph-publishing.md docs/code-graph-publishing.ru.md pyproject.toml src/iwiki_mcp/__init__.py tests/test_package.py uv.lock
git commit -m "docs: describe threaded dispatch and the cleanup worker"
```
