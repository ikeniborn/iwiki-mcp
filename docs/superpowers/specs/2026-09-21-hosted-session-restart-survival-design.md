---
review:
  spec_hash: 3d4a530f3ffb7ead
  last_run: 2026-09-21
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings:
    - id: F-001
      phase: consistency
      severity: WARNING
      section: R1
      section_hash: 2c15855f807aa727
      fragment: "the SDK raises RuntimeError if an idle timeout is combined with stateless mode"
      text: "True of the SDK constructor, but this code assigns session_idle_timeout after construction, so the guard never runs and the line would silently become dead rather than failing."
      fix: "R1 now states that the guard is bypassed, that nothing would fail, and that the line must be removed deliberately instead of left looking load-bearing."
      verdict: fixed
      verdict_at: 2026-09-21
    - id: F-002
      phase: coverage
      severity: INFO
      section: R6
      section_hash: null
      fragment: "the SDK creates a transport, a task and a ServerSession for every request"
      text: "Surfaced while verifying the design, not by the spike: stateless mode moves per-session setup onto every request, which bears directly on the intent's response-time metric."
      fix: "Added R6 requiring the cost to be measured before and after against the same server, with the intent's 150 ms median as the stopping bound."
      verdict: accepted
      verdict_at: 2026-09-21
chain:
  intent: a7e80ec0610318a5
workflow:
  route: chain
  continuation: full
---

# Design: hosted-session-restart-survival

**Date:** 2026-09-21
**Status:** draft
**Intent:** `docs/superpowers/intents/2026-09-21-hosted-session-restart-survival-intent.md` (approved, hash `a7e80ec0610318a5`)

## 1. Problem

The hosted transport runs stateful (`stateless_http = False` in `prepare_runtime`), so the
SDK keeps its session table in process memory. Every restart makes each `mcp-session-id`
unknown and the SDK answers `404 Session not found`. No client recovers: Claude Code and
Codex mark the server failed, and the SDK's own Python client converts the `404` into a
terminal `Session terminated` error without re-initializing — reported upstream as
`modelcontextprotocol/python-sdk#3556`.

Observed twice on 2026-09-21 during ordinary deploys, and on 2026-09-01 with a worse
consequence recorded on `reference/hosted-session-binding-fallback-visibility`: the lost
binding let `wiki_code_search` answer with another project's snapshot, `fresh: true`, no
warning.

A spike closed the obvious alternatives. Adopting an unknown session id is not viable —
`ServerSession` starts `NotInitialized`, the first call raises, and the manager deletes the
session again; the registration points are private SDK structure under a `mcp<2` pin. The
server's `404` is correct per the 2025-06-18 specification. And this project's binding
layer already survives a restart, falling back to the token's grants and reporting
`binding_source: token_default`.

## 2. Acceptance (from intent)

Desired Outcomes, carried verbatim:

- A client that opened its session before the container was recreated calls a tool
  afterwards and gets `200`, not `404` and not a transport error.
- After a deploy, Claude Code does not mark the server failed and needs no `/mcp`
  reconnect — verified against a real deploy, not only with `curl`.
- When a scope comes from the token rather than from `wiki_bind`, that is visible in
  `binding_source` and `warnings`; no silent substitution of one project's scope for
  another's.
- `DELETE` still terminates a session and releases its binding, even though the SDK
  answers `405` for it in stateless mode.

Done when, carried verbatim: a client holding a session across a real container recreation
completes a tool call afterwards without reconnecting, `binding_source` truthfully reports
where the scope came from, `DELETE` still releases a binding, and the suites pass
unchanged.

## 3. Verified assumptions

Each was checked against the installed SDK (`mcp` 1.28.1) and the running server, not
inferred:

- `_validate_session` returns `True` when `mcp_session_id` is unset, which it is in
  stateless mode — an unknown id is never refused.
- `_handle_stateless_request` builds the transport with `mcp_session_id=None`, so the SDK
  emits no session header. Issuing one becomes the middleware's job.
- `ServerSession.__init__` sets `InitializationState.Initialized if stateless else
  NotInitialized`. This is the mechanism that makes a restart invisible: a request needs no
  prior handshake.
- On the live server over 24 hours, `GET /mcp` answered `401` a hundred times (the
  healthcheck, unauthenticated) and `405` eight times, never `200`. No server-initiated
  stream is in use, so stateless mode costs this deployment nothing.
- `DELETE /mcp` answered `404` three times — session termination is already broken today,
  because the sessions were already gone.
- The 17 tests in `tests/postgres/test_http.py` read the session id from the response
  header and pass it back. The header keeps arriving, from the middleware instead of the
  SDK, so they stay valid unchanged.

## 4. Where the boundary falls

One flag changes in the SDK layer: `stateless_http = True`. Everything else lands in
`AuthenticatedMCPMiddleware`, which already sits on this path and already reads
`mcp-session-id`, the bearer token, and the binding.

| Component | Responsibility after the change |
|---|---|
| SDK | The MCP protocol and call dispatch. Knows nothing about sessions |
| `AuthenticatedMCPMiddleware` | Issuing and recognizing `mcp-session-id`, termination, routing |
| `_SessionBindings` | Storage with the ownership check, plus a ceiling on entries |

`GET` is already intercepted by the middleware and answered `405` before the SDK, so its
behaviour does not change at all. The container healthcheck accepts `{401, 405}` and keeps
working untouched.

## 5. Requirements

### R1 — Switch the hosted transport to stateless

`prepare_runtime` sets `stateless_http = True`, and the line assigning
`session_manager.session_idle_timeout` is removed with it.

The removal must be deliberate rather than left in place. The SDK refuses that combination
in `StreamableHTTPSessionManager.__init__` with `RuntimeError("session_idle_timeout is not
supported in stateless mode")` — but this code assigns the attribute *after* construction,
so the guard never runs and nothing would fail. The line would simply become dead: the
stateless path never creates the idle scope that reads it. Leaving dead configuration
that looks load-bearing is how the next reader concludes sessions are still being reaped.

### R2 — The middleware issues the session id

A request arriving without `mcp-session-id` is an `initialize`. The middleware generates
`uuid4().hex`, and on a successful response sets the `mcp-session-id` header and stores the
binding under that key. `uuid4` rather than `uuid7`: the sortability of v7 is useless here
because eviction is by `last_seen` rather than creation time, v7 carries 74 random bits
against 122, it would embed a creation timestamp in a value that reaches client logs, and
it is absent from the standard library before Python 3.14 while this project supports 3.10.

### R3 — Ownership is unchanged and still decides

`sessions.resolve(session_id, context)` keeps refusing a record whose `token_id` or
`iwiki_id` differs. A request bearing another token's session id therefore finds no
binding and falls through to the existing `token_default` path, exactly as today. No id is
ever adopted across tokens.

### R4 — The middleware answers DELETE

`DELETE` is intercepted before the SDK, beside the existing `GET` branch: it removes the
binding for the caller's own session and answers `204`. It answers `204` for an unknown or
foreign id as well, without touching a record it does not own — the response must not
disclose whether a session existed.

This repairs behaviour that is broken today rather than preserving it: `DELETE` currently
reaches the SDK, gets `404`, and `capture_send` then skips the removal because it only
removes on a status below 400.

### R5 — The binding table has a ceiling

`_SessionBindings` gains a maximum entry count beside `_SESSION_IDLE_SECONDS`. After the
existing age-based prune, if the table still exceeds the limit, the entry with the oldest
`last_seen` is evicted until it fits. By `last_seen`, not creation time: an active session
must never be evicted for having been open a long time.

The limit is 1000. An entry is two hashes and a reference; a thousand is negligible in
memory, and a server reaching a thousand concurrent live sessions has other limits first.
Eviction logs a warning with the number of entries dropped and no identifiers, so a client
that silently becomes `token_default` has an explanation in the log.

### R6 — Measure the per-request cost

In stateless mode the SDK creates a transport, a task and a `ServerSession` for every
request, which the spike did not surface. The intent bounds hosted `initialize` at a 150 ms
median over five runs against 30–85 ms today. Measure before and after against the same
server; a regression past the bound stops the change rather than being absorbed.

## 6. Error handling

| Situation | Behaviour |
|---|---|
| `GET` | `405` before the SDK, unchanged |
| `DELETE`, caller owns the session | Binding removed, `204` |
| `DELETE`, unknown or foreign id | `204`, nothing removed, existence not disclosed |
| Request with an id whose owner differs | No binding found, `token_default`, as today |
| `initialize` fails with status ≥ 400 | No binding stored, no header set |
| Ceiling reached | Oldest `last_seen` evicted, warning logged |

## 7. Testing

The 17 existing tests in `tests/postgres/test_http.py` are unchanged. New cases:

1. `initialize` without a header returns `mcp-session-id`, and a follow-up request carrying
   it finds the binding.
2. The same id presented with a different token finds no binding.
3. `DELETE` removes the binding and answers `204`; a later call reports `token_default`.
4. A fresh middleware instance with the same id — the restart case — serves the call and
   reports `binding_source: token_default`.
5. The ceiling evicts by `last_seen`, not by creation order.
6. `GET` still answers `405`, so the healthcheck cannot break unnoticed.

Test 4 exercises the loss of the binding table specifically: the SDK holds nothing between
requests in stateless mode, so there is no SDK state left to lose.

## 8. Out of scope

- Persisting bindings across a restart. The intent asks that a restart be invisible, not
  that a selected scope survive it; `token_default` remains the honest answer.
- Fixing the clients' specification violation. Reported upstream; not ours to fix.
- The local stdio transport and its startup path.
