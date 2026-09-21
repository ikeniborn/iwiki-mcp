---
review:
  plan_hash: 7d1adc01450b9bd0
  last_run: 2026-09-21
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings:
    - id: F-001
      phase: coverage
      severity: WARNING
      section: all tasks
      section_hash: null
      fragment: "no task named the requirement it implements"
      text: "Coverage could not be checked mechanically: a grep for R1-R6 across the plan returned nothing, so a missing requirement would have gone unnoticed."
      fix: "Every task now opens with an Implements line naming its requirement; Task 6 states explicitly that it carries no requirement of its own."
      verdict: fixed
      verdict_at: 2026-09-21
    - id: F-002
      phase: dependencies
      severity: INFO
      section: Task 1
      section_hash: 3d1b3a06820fdd46
      fragment: "This task comes first because it is the security precondition for Task 4"
      text: "_SessionBindings.remove pops without checking ownership today, which is safe only because the SDK verifies _session_owners first. Stateless mode drops that check, so intercepting DELETE before fixing remove would open a window where a leaked session id deletes another token's binding."
      fix: "Ordered the ownership fix ahead of the DELETE interception and said why in the task itself."
      verdict: accepted
      verdict_at: 2026-09-21
chain:
  intent: a7e80ec0610318a5
  spec: 7a6604f64a8fdb8d
workflow:
  route: chain
  continuation: full
---

# Hosted session restart survival Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a hosted MCP session survive a server restart by moving the transport to stateless mode and keeping session identity in the middleware that already owns the binding.

**Architecture:** `prepare_runtime` sets `stateless_http = True`, so the SDK stops issuing and validating session ids and starts every `ServerSession` initialized. `AuthenticatedMCPMiddleware` takes over the three jobs the SDK drops: issuing `mcp-session-id`, answering `DELETE`, and bounding the binding table. Ownership checks stay in `_SessionBindings` and remain the only thing that decides whose binding a request reaches.

**Tech Stack:** Python 3.11, the `mcp` SDK 1.28.1 pinned `<2`, Starlette/ASGI, `anyio`, psycopg pool, pytest with `asyncio_mode=auto`.

**Spec:** `docs/superpowers/specs/2026-09-21-hosted-session-restart-survival-design.md` (spec_hash `7a6604f64a8fdb8d`)

## Global Constraints

- A binding is reachable only by the token that created it: every read, write, and delete checks `token_id` and `iwiki_id`.
- No plaintext token is stored in memory or written to a log.
- The token's grants are the ceiling; nothing here may widen a scope.
- The local stdio transport and its startup path are untouched.
- The existing tests in `tests/postgres/test_http.py` are not modified — 17 test functions, 31 collected cases after parametrization. They read the session id from the response header, which keeps arriving.
- `GET /mcp` keeps answering `405`; the container healthcheck accepts `{401, 405}`.
- Bump `version` in `pyproject.toml`, `__version__` in `src/iwiki_mcp/__init__.py`, and the pin in `tests/test_package.py` together: 0.7.290 → 0.7.291.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/iwiki_mcp/http.py` | Hosted runtime and the authenticating middleware | Modify |
| `tests/postgres/test_http.py` | Hosted transport behaviour against a real database | Append cases |
| `tests/test_session_bindings.py` | Binding table semantics without a database | Create |
| `docs/architecture.md`, `docs/deployment.md` | Operator- and contributor-facing description | Modify |

`tests/postgres/test_http.py` needs a database and is skipped without `IWIKI_TEST_POSTGRES_DSN`. The binding-table rules — ownership, eviction — do not need one, so they get their own file that always runs.

---

### Task 1: Refuse to delete a binding you do not own

**Implements:** R4 (the ownership precondition it depends on) and R3.

**Files:**
- Modify: `src/iwiki_mcp/http.py` (`_SessionBindings.remove`)
- Test: `tests/test_session_bindings.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `_SessionBindings.remove(session_id: str | None, context: AuthContext) -> bool` — returns `True` when a record was removed, `False` when there was nothing to remove or the caller did not own it.

This task comes first because it is the security precondition for Task 4. Today `remove` pops unconditionally, which is safe only because the SDK checks ownership before the middleware ever calls it. Stateless mode removes that check.

- [ ] **Step 1: Write the failing test**

Create `tests/test_session_bindings.py`:

```python
"""Binding table semantics. No database: this is pure in-process state."""
import pytest

from iwiki_mcp.http import _SessionBindings
from iwiki_mcp.postgres.auth import AuthContext


def _context(token_id: str = "t1", iwiki_id: str = "w1") -> AuthContext:
    return AuthContext(
        token_id=token_id,
        iwiki_id=iwiki_id,
        read_domains=("alpha",),
        write_domains=("alpha",),
        primary="alpha",
    )


def test_remove_deletes_a_binding_its_owner_asks_for():
    bindings = _SessionBindings()
    owner = _context()
    bindings.store("s1", owner, "state")

    assert bindings.remove("s1", owner) is True
    assert bindings.resolve("s1", owner) is None


def test_remove_refuses_another_tokens_binding():
    bindings = _SessionBindings()
    owner = _context(token_id="t1")
    stranger = _context(token_id="t2")
    bindings.store("s1", owner, "state")

    assert bindings.remove("s1", stranger) is False
    assert bindings.resolve("s1", owner) == "state"


def test_remove_of_an_unknown_session_is_not_an_error():
    bindings = _SessionBindings()

    assert bindings.remove("never-existed", _context()) is False
    assert bindings.remove(None, _context()) is False
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest -q tests/test_session_bindings.py`
Expected: FAIL — `remove()` currently takes one argument, so the calls raise `TypeError`.

- [ ] **Step 3: Implement**

Replace `_SessionBindings.remove`:

```python
    def remove(self, session_id: str | None, context: AuthContext) -> bool:
        """Drop one binding, but only for the token that owns it.

        Stateless mode removes the SDK's own ownership check, so this is the
        only thing standing between a leaked session id and someone else's
        binding. The boolean lets the caller answer identically either way:
        whether a session existed is not the caller's business to learn.
        """
        if session_id is None:
            return False
        with self._lock:
            record = self._records.get(session_id)
            if record is None:
                return False
            if (
                record.token_id != context.token_id
                or record.iwiki_id != context.iwiki_id
            ):
                return False
            del self._records[session_id]
            return True
```

- [ ] **Step 4: Update the one existing caller**

In `capture_send`, the DELETE branch becomes `self.sessions.remove(session_id, context)`. Leave the rest of that function alone; Task 4 replaces this branch entirely.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest -q tests/test_session_bindings.py && uv run flake8 src tests`
Expected: 3 passed, flake8 clean.

- [ ] **Step 6: Commit**

```bash
git add src/iwiki_mcp/http.py tests/test_session_bindings.py
git commit -m "fix(http): check ownership before dropping a session binding"
```

---

### Task 2: Bound the binding table

**Implements:** R5.

**Files:**
- Modify: `src/iwiki_mcp/http.py` (`_SESSION_IDLE_SECONDS` neighbourhood, `_SessionBindings._prune`)
- Test: `tests/test_session_bindings.py` (append)

**Interfaces:**
- Consumes: `_SessionBindings` from Task 1.
- Produces: module constant `_SESSION_MAX_ENTRIES = 1000`; `_prune` evicts by `last_seen` once the table exceeds it.

- [ ] **Step 1: Write the failing test**

```python
def test_the_table_evicts_the_least_recently_seen_entry_first(monkeypatch):
    from iwiki_mcp import http

    monkeypatch.setattr(http, "_SESSION_MAX_ENTRIES", 2)
    bindings = http._SessionBindings()
    owner = _context()

    bindings.store("oldest", owner, "a")
    bindings.store("middle", owner, "b")
    # Touching "oldest" makes "middle" the least recently seen.
    assert bindings.resolve("oldest", owner) == "a"
    bindings.store("newest", owner, "c")

    assert bindings.resolve("middle", owner) is None
    assert bindings.resolve("oldest", owner) == "a"
    assert bindings.resolve("newest", owner) == "c"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest -q tests/test_session_bindings.py::test_the_table_evicts_the_least_recently_seen_entry_first`
Expected: FAIL with `AttributeError: module 'iwiki_mcp.http' has no attribute '_SESSION_MAX_ENTRIES'`.

- [ ] **Step 3: Implement**

Beside `_SESSION_IDLE_SECONDS`:

```python
# The SDK reaped idle sessions while the transport was stateful. Stateless mode
# has no session table to reap, so this bound is the only thing keeping the
# binding table finite. Eviction is by last activity, never by age: a session
# open all day and used every minute must outlive one opened a minute ago and
# abandoned.
_SESSION_MAX_ENTRIES = 1000
```

Extend `_prune`, after the existing age-based sweep:

```python
        if len(self._records) <= _SESSION_MAX_ENTRIES:
            return
        ordered = sorted(
            self._records.items(), key=lambda item: item[1].last_seen
        )
        evicted = len(self._records) - _SESSION_MAX_ENTRIES
        for session_id, _record in ordered[:evicted]:
            self._records.pop(session_id, None)
        logger.warning(
            "session binding table at capacity; evicted %d least recently "
            "used entries",
            evicted,
        )
```

If `http.py` has no module logger, add `logger = logging.getLogger(__name__)` beside the other module-level names and import `logging`. Never log a session id, a token, or a domain here.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q tests/test_session_bindings.py && uv run flake8 src tests`
Expected: 4 passed, flake8 clean.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/http.py tests/test_session_bindings.py
git commit -m "feat(http): bound the session binding table by least recent use"
```

---

### Task 3: Issue the session id from the middleware

**Implements:** R1 and R2.

**Files:**
- Modify: `src/iwiki_mcp/http.py` (`prepare_runtime`, `AuthenticatedMCPMiddleware.__call__`, `capture_send`)
- Test: `tests/postgres/test_http.py` (append)

**Interfaces:**
- Consumes: `_SessionBindings` from Tasks 1 and 2.
- Produces: every response to a request that arrived without `mcp-session-id` carries one; the binding is stored under it.

- [ ] **Step 1: Write the failing test**

Append to `tests/postgres/test_http.py`, following the file's existing fixture style:

```python
def test_a_session_id_is_issued_and_survives_a_new_middleware_instance(
    hosted_app, token
):
    """The restart case: a fresh middleware keeps serving the same id."""
    with TestClient(hosted_app) as client:
        first = _request(client, token, _initialize_payload())
        assert first.status_code == 200
        session_id = first.headers["mcp-session-id"]
        assert session_id

        second = _call_tool(
            client, token, "wiki_status", {}, session_id=session_id
        )
        assert second.status_code == 200

    # A new app instance models the container being recreated.
    with TestClient(hosted_app) as restarted:
        after = _call_tool(
            restarted, token, "wiki_status", {}, session_id=session_id
        )
        assert after.status_code == 200
        body = _tool_payload(after)
        assert body["binding_source"] == "token_default"
```

Reuse whatever helpers that file already defines for building an initialize payload and reading a tool result; do not invent parallel ones. If a helper is missing, add it beside the existing ones rather than inlining JSON.

- [ ] **Step 2: Run it to verify it fails**

Run:
```bash
docker run -d --name iwiki-pgtest -e POSTGRES_PASSWORD=pgtest -e POSTGRES_DB=iwiki_test -p 127.0.0.1:55432:5432 pgvector/pgvector:pg16
docker exec iwiki-pgtest psql -U postgres -d iwiki_test -c "CREATE EXTENSION IF NOT EXISTS vector;"
IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55432/iwiki_test" uv run pytest -q tests/postgres/test_http.py -k session_id_is_issued
```
Expected: FAIL — the second block gets `404` from the SDK, because the id is unknown to the restarted instance.

- [ ] **Step 3: Switch the transport to stateless**

In `prepare_runtime`:

```python
        server.mcp.settings.json_response = True
        server.mcp.settings.stateless_http = True
```

and delete the line `server.mcp.session_manager.session_idle_timeout = _SESSION_IDLE_SECONDS` entirely. It would not raise — the SDK's guard lives in the constructor and this assignment happens after it — it would simply become dead configuration that reads as if sessions were still being reaped.

- [ ] **Step 4: Mint the id in the middleware**

In `__call__`, after `session_id = _one_header(scope, b"mcp-session-id")`:

```python
            issued_session_id = session_id or uuid4().hex
```

Add `from uuid import uuid4` to the imports. In `capture_send`, replace the header lookup and the store branch:

```python
                    async def capture_send(message):
                        if message["type"] == "http.response.start":
                            if message["status"] < 400:
                                headers = list(message.get("headers", ()))
                                if session_id is None:
                                    headers.append(
                                        (
                                            b"mcp-session-id",
                                            issued_session_id.encode("latin-1"),
                                        )
                                    )
                                    message = dict(message, headers=headers)
                                self.sessions.store(
                                    issued_session_id, context, state
                                )
                        await send(message)
```

The DELETE branch disappears from here: Task 4 answers `DELETE` before the request ever reaches the SDK.

- [ ] **Step 5: Run the test**

Run the same command as Step 2.
Expected: PASS. Then `docker rm -f iwiki-pgtest`.

- [ ] **Step 6: Run the whole hosted suite**

Run: `IWIKI_TEST_POSTGRES_DSN=... uv run pytest -q tests/postgres/test_http.py`
Expected: every existing test still passes. If one fails because it expected the SDK to issue the id, stop and report it — that is a spec-level surprise, not a test to edit.

- [ ] **Step 7: Commit**

```bash
git add src/iwiki_mcp/http.py tests/postgres/test_http.py
git commit -m "feat(http): serve the hosted transport statelessly"
```

---

### Task 4: Answer DELETE in the middleware

**Implements:** R4.

**Files:**
- Modify: `src/iwiki_mcp/http.py` (`AuthenticatedMCPMiddleware.__call__`)
- Test: `tests/postgres/test_http.py` (append)

**Interfaces:**
- Consumes: `remove(session_id, context)` from Task 1.
- Produces: `DELETE /mcp` answers `204` and releases the caller's own binding.

- [ ] **Step 1: Write the failing test**

```python
def test_delete_releases_the_callers_binding_and_not_a_strangers(
    hosted_app, token, other_token
):
    with TestClient(hosted_app) as client:
        first = _request(client, token, _initialize_payload())
        session_id = first.headers["mcp-session-id"]

        stranger = _delete(client, other_token, session_id=session_id)
        assert stranger.status_code == 204

        still_mine = _call_tool(
            client, token, "wiki_status", {}, session_id=session_id
        )
        assert _tool_payload(still_mine)["binding_source"] == "session"

        mine = _delete(client, token, session_id=session_id)
        assert mine.status_code == 204

        after = _call_tool(
            client, token, "wiki_status", {}, session_id=session_id
        )
        assert _tool_payload(after)["binding_source"] == "token_default"
```

Add a `_delete` helper beside `_request` if the file has none, issuing `client.delete("/mcp", headers=...)` with the same header construction.

- [ ] **Step 2: Run it to verify it fails**

Run: `IWIKI_TEST_POSTGRES_DSN=... uv run pytest -q tests/postgres/test_http.py -k delete_releases`
Expected: FAIL — `DELETE` currently reaches the SDK, which answers `405` in stateless mode.

- [ ] **Step 3: Implement**

In `__call__`, directly after the existing `GET` branch:

```python
            if scope.get("method") == "DELETE":
                session_id = _one_header(scope, b"mcp-session-id")
                self.sessions.remove(session_id, context)
                await _send_no_content(send)
                return
```

and beside `_send_method_not_allowed`:

```python
async def _send_no_content(send) -> None:
    """Acknowledge a termination without saying whether it found anything.

    The same 204 answers an owned session, a stranger's id, and an id that
    never existed: a caller learns nothing about sessions it does not own.
    """
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})
```

- [ ] **Step 4: Run the tests**

Run: `IWIKI_TEST_POSTGRES_DSN=... uv run pytest -q tests/postgres/test_http.py`
Expected: all pass, including the two new cases.

- [ ] **Step 5: Commit**

```bash
git add src/iwiki_mcp/http.py tests/postgres/test_http.py
git commit -m "feat(http): terminate a session in the middleware"
```

---

### Task 5: Measure the per-request cost

**Implements:** R6.

**Files:** none — this is the intent's health metric.

In stateless mode the SDK builds a transport, a task, and a `ServerSession` for every request. The intent caps hosted `initialize` at a 150 ms median over five runs; it measures 30–85 ms today.

- [ ] **Step 1: Measure the deployed server before the change**

```bash
for i in 1 2 3 4 5; do
  curl -s -o /dev/null -w "%{time_total}\n" -X POST https://iwiki.ikeniborn.ru/mcp \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "Authorization: Bearer $IWIKI_CODE_GRAPH_MCP_TOKEN" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"bench","version":"0"}}}' \
    --max-time 20
done
```

Record all five numbers and their median.

- [ ] **Step 2: Measure again after deploying the change**

Same command, same host, no other load. Record the five numbers and the median.

- [ ] **Step 3: Compare against the bound**

Median must stay under 150 ms. Over it, stop and report rather than adjusting the bound.

- [ ] **Step 4: Record both sets on the ledger page**

Ten numbers and two medians, on `iwiki-mcp/reference/tasks/hosted-session-restart-survival`, section `Evidence`.

---

### Task 6: Describe the transport that now exists

**Implements:** the operator-facing consequence of R1 through R5; no requirement of its own.

**Files:**
- Modify: `docs/architecture.md`, `docs/deployment.md`

- [ ] **Step 1: Find every claim about hosted sessions**

Run: `grep -rn "session_idle_timeout\|stateless\|mcp-session-id\|session id" docs/architecture.md docs/deployment.md`
Expected: the statements describing a stateful hosted transport and SDK-side idle reaping.

- [ ] **Step 2: Rewrite them**

State what is true after this change: the hosted transport runs stateless; the SDK issues no session id and keeps no session table; the middleware issues `mcp-session-id`, owns the binding, answers `DELETE` with `204`, and bounds the table at `_SESSION_MAX_ENTRIES` by least recent use. Say plainly that a restart no longer ends a client's session, and that a selected scope does not survive one — the client falls back to the token's grants and sees `binding_source: token_default`.

Do not describe the SDK's idle reaping as still happening. It is gone.

- [ ] **Step 3: Verify no stale claim survives**

Run the Step 1 grep again and read each hit.
Expected: nothing describing SDK-side session tracking or idle reaping.

- [ ] **Step 4: Commit**

```bash
git add docs/architecture.md docs/deployment.md
git commit -m "docs: describe the stateless hosted transport"
```

---

### Task 7: Verify against a real restart

**Implements:** the spec's Acceptance section, carried from the intent's Done-when.

**Files:** none — this is the intent's "Done when".

- [ ] **Step 1: Deploy**

Only after the full suite is green, per the intent's stop rule. Follow the sequence already used on this host: update `/opt/iwiki-mcp/source` to the merge commit, `docker compose -p iwiki-mcp-app build iwiki`, then `up -d`.

- [ ] **Step 2: Open a session and hold it**

```bash
SID=$(curl -s -D - -o /dev/null -X POST https://iwiki.ikeniborn.ru/mcp \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer $IWIKI_CODE_GRAPH_MCP_TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"restart-probe","version":"0"}}}' \
  | awk -F': ' '/[Mm]cp-[Ss]ession-[Ii]d/{print $2}' | tr -d '\r')
echo "session: ${#SID} chars"
```

- [ ] **Step 3: Recreate the container**

```bash
ssh framework 'cd /opt/iwiki-mcp/source && sudo -n docker compose -p iwiki-mcp-app up -d --force-recreate'
```

- [ ] **Step 4: Use the same session afterwards**

```bash
curl -s -o /tmp/after.json -w "status=%{http_code}\n" -X POST https://iwiki.ikeniborn.ru/mcp \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer $IWIKI_CODE_GRAPH_MCP_TOKEN" -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"wiki_status","arguments":{}}}' --max-time 20
grep -o '"binding_source":"[a-z_]*"' /tmp/after.json
```

Expected: `status=200`, and `binding_source` reads `token_default`. Before this change the same sequence answered `404`.

- [ ] **Step 5: Confirm in a real client**

Reconnect this session's `iwiki-remote` and call any wiki tool after a deploy, without `/mcp` reconnect. The intent asks for a real client, not only `curl`.

- [ ] **Step 6: Record the outcome and close**

Append the evidence to the ledger page, then run `/check-chain result` against this plan.
