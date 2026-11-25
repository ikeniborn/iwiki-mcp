---
workflow:
  route: chain
  continuation: execute
review:
  intent_hash: f0f13415b54f1bc9
  last_run: 2026-11-25
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings: []
result_check:
  verdict: OK
  source: intent
  intent_hash: f0f13415b54f1bc9
  last_run: 2026-11-25
  reviewed: true
  docs_checked: true
---
# Intent: mcp-http-request-body-limit

**Date:** 2026-11-25
**Status:** approved

## Objective

Bound the authenticated HTTP request body the hosted MCP middleware retains before JSON decoding. Apply the same 16 MiB boundary as the framework ingress to both initialization and subsequent MCP messages.

## Desired Outcomes

- Allow a body of exactly 16,777,216 bytes to continue through normal authorization and dispatch. Its length alone must not cause a refusal.
- Return HTTP 413 when an otherwise admissible request exceeds that boundary. Count the received body across chunks, including requests without Content-Length. Stop reading at the first chunk that establishes excess.
- Preserve an existing project binding across this refusal. A later valid request on the same session must succeed. A refused initialization must not issue a session.
- Allow a valid 5,000,000-byte publication batch inside its normal MCP envelope when the complete request fits the limit.

## Health Metrics

- Zero JSON decodes and downstream application calls for oversized bodies refused by the middleware.
- Zero additional receive calls after the first chunk establishes excess. Avoid retaining a list of unbounded size for fragmented requests.
- Preserve existing origin, bearer and foreign-session refusal behavior before inspecting the body.

## Strategic Context

- Interacts with: hosted MCP middleware, session binding, nginx ingress and code graph publication.
- Priority trade-off: protect application memory while preserving the current size envelope for permitted calls.

## Constraints

### Steering

- Reuse the middleware's existing HTTP error conventions and request replay contract.

### Hard

- Measure the complete encoded HTTP body in bytes, including the JSON-RPC envelope.
- Preserve existing tool-level authorization and per-batch validation. Leave local stdio and routes outside the MCP middleware unchanged.

## Autonomy Zones

- Full autonomy: choose local implementation details and synthetic acceptance fixtures.
- Guarded: change buffering while preserving the bytes delivered to authorized downstream calls.
- Proposal-first: change the limit, its scope, or the public rejection status.
- No autonomy: deploy or change production proxy configuration under this intent.

## Stop Rules

- Stop if exceeding the limit can reach JSON decoding or dispatch, or if refusing an oversized request invalidates a valid binding.
- Done when: boundary, fragmentation, authentication precedence, session continuity and a normal maximum-size publication batch pass focused tests and relevant hosted regressions.
