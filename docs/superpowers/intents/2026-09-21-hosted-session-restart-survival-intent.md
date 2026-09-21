---
review:
  intent_hash: a7e80ec0610318a5
  last_run: 2026-09-21
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings:
    - id: F-001
      phase: clarity
      severity: WARNING
      section: Health Metrics
      section_hash: 4ef5a61d6411b23d
      fragment: "Response time does not visibly regress"
      text: "A health metric named no number, repeating the same defect the previous intent's gate caught."
      fix: "Bound it: hosted initialize measures 30-85 ms today; the median of five runs must stay under 150 ms."
      verdict: fixed
      verdict_at: 2026-09-21
    - id: F-002
      phase: alignment
      severity: INFO
      section: Objective
      section_hash: null
      fragment: "reference/hosted-session-binding-fallback-visibility"
      text: "The wiki records an earlier instance of this failure on 2026-09-01 whose consequence was worse than a dead session: another project's snapshot returned as fresh."
      fix: "Carried into the Objective so the spec inherits the severity, not just the symptom."
      verdict: accepted
      verdict_at: 2026-09-21
workflow:
  route: chain
  continuation: pending
---

# Intent: hosted-session-restart-survival

**Date:** 2026-09-21
**Status:** approved

## Objective

A restart of the hosted server ends every connected client's session permanently. The
MCP session table lives in the process (`stateless_http = False` in `http.py`), so after a
deploy every `mcp-session-id` is unknown and the SDK answers `404 Session not found`.
Clients do not recover: Claude Code and Codex mark the server failed for the rest of the
session, and the SDK's own Python client turns the `404` into a terminal
`Session terminated` error without re-initializing.

Observed twice on 2026-09-21 during ordinary deploys of this server, and earlier on
2026-09-01, recorded on `reference/hosted-session-binding-fallback-visibility`: that time
the lost binding was worse than a dead session — `wiki_code_search` answered with another
project's published snapshot, `fresh: true`, no warning.

A spike (issue 94) established three things. Adopting an unknown session id server-side is
not viable: `ServerSession` starts `NotInitialized`, so the first call raises and the
manager deletes the session again, and the registration points are private SDK structure
under a `mcp<2` pin. The server's `404` is correct per the 2025-06-18 transport
specification — the clients are the ones violating it, reported upstream as
`modelcontextprotocol/python-sdk#3556`. And this project's own binding layer already
survives a restart, falling back to the token's scope and reporting `binding_source:
token_default`.

What remains is the transport mode. In stateless mode the SDK never issues or validates a
session id and `ServerSession` starts initialized, so a restart becomes invisible to the
client. This deployment already forgoes everything stateless costs: `GET /mcp` answers
`405` and `json_response` is `True`, so there is no server-initiated stream to lose.

## Desired Outcomes

- A client that opened its session before the container was recreated calls a tool
  afterwards and gets `200`, not `404` and not a transport error.
- After a deploy, Claude Code does not mark the server failed and needs no `/mcp`
  reconnect — verified against a real deploy, not only with `curl`.
- When a scope comes from the token rather than from `wiki_bind`, that is visible in
  `binding_source` and `warnings`; no silent substitution of one project's scope for
  another's.
- `DELETE` still terminates a session and releases its binding, even though the SDK
  answers `405` for it in stateless mode.

## Health Metrics

- Isolation between tokens holds: two different tokens cannot reach one another's binding
  through the same session id. The ownership check in `_SessionBindings` stays strict.
- The existing suites keep passing — 3190 in the fast set, plus the PostgreSQL set run
  against a disposable database.
- Response time stays where it is: hosted `initialize` measures 30–85 ms today, and the
  median of five runs after the change must stay under 150 ms.
- Memory does not grow without bound. The SDK's idle reaping disappears in stateless mode,
  so the binding dictionary needs its own bound.

## Strategic Context

- Interacts with: `src/iwiki_mcp/http.py` (`prepare_runtime`, `AuthenticatedMCPMiddleware`,
  `_SessionBindings`, `_SESSION_IDLE_SECONDS`), the SDK's
  `streamable_http_manager.py` and `streamable_http.py`, every tool in `server.py` that
  reports `binding_source`, `tests/postgres/test_http.py`, the deployed container on the
  `framework` host, and the session-lifecycle rules documented in the iclaude, icodex and
  iwiki-mcp rule surfaces.
- Priority trade-off: trust. A leaked or silently substituted binding is worse than a
  session that has to be re-established, so correctness of ownership outranks both speed of
  delivery and economy of implementation.

## Constraints

### Steering (behavioral guidance)

- Prefer adding tests over changing ones that pass today.
- Keep the change inside the hosted transport path; do not refactor neighbouring code
  because it is nearby.
- Where the SDK stops providing something — id issuance, idle reaping, `DELETE` handling —
  replace it explicitly rather than letting it lapse silently.

### Hard (architectural enforcement)

- A binding is always checked against `token_id` and `iwiki_id`. A mismatch is refused;
  adopting another token's session id is forbidden.
- No plaintext token is stored in memory or written to a log, as `_SessionRecord` already
  guarantees.
- The token's grants remain the absolute ceiling. Neither stateless mode nor any session
  recovery may widen a scope beyond what the token allows.
- The local stdio server and its startup path are untouched; this concerns the hosted
  transport only.

## Autonomy Zones

- Full autonomy (reversible, low risk): the transport mode, the middleware's session
  handling, new tests, and the documentation of the resulting behaviour.
- Guarded (log + confidence threshold): the bound on the binding dictionary that replaces
  the SDK's idle reaping, and the `DELETE` handling that replaces the SDK's.
- Proposal-first (needs approval): none — the user granted full autonomy within this
  intent.
- No autonomy (human only): none within this intent.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules

- Halt if: making a restart survivable requires weakening the ownership check, or lets any
  scope exceed the token's grants.
- Halt if: stateless mode turns out to break a behaviour this deployment actually uses —
  the assumption that `GET` and server-initiated streaming are unused must be verified
  against the running server, not assumed from the configuration.
- Escalate if: the change cannot keep `DELETE` terminating a session, leaving bindings to
  accumulate until their bound evicts them.
- Deploy only after the full suite is green and a rollback path is confirmed: the hosted
  server is shared, and a failed transport change takes every client with it.
- Done when: a client holding a session across a real container recreation completes a tool
  call afterwards without reconnecting, `binding_source` truthfully reports where the scope
  came from, `DELETE` still releases a binding, and the suites pass unchanged.
