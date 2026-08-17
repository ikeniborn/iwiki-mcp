# Intent: tool-contract-consistency

**Date:** 2026-08-17
**Status:** approved

## Objective
Fix five schema/behaviour and code/code inconsistencies in `iwiki-mcp`, found by an
external client audit ("iwiki-mcp server issues"): (B1) `expected_revision` is declared
optional in the tool schema but is mandatory on the PostgreSQL backend; (B2) access-denial
responses are returned at different transport layers with different error code spellings;
(B3) `wiki_code_context` reports `invalid_config` instead of `missing_snapshot` for a
missing snapshot; (B4) `wiki_related` does not dedupe multiple entries for the same
section; (B5) section mutations write the body with no blank line under the `##` heading.
Now — because these mismatches break schema-driven client call generation and mislead
failure diagnosis.

## Desired Outcomes
- B1: the schema description of `expected_revision` on all five write tools
  (`wiki_update_page`, `wiki_insert_section`, `wiki_delete_section`, `wiki_move_section`,
  `wiki_delete_page`) states it is required on PostgreSQL storage and unused on Git storage.
- B2: a grant tool call without authorization returns one JSON-RPC error with
  `code: access_denied`, not an HTTP 403 at the transport layer.
- B3: `wiki_code_context` against a missing snapshot reports `missing_snapshot`, matching
  `wiki_code_search` and `wiki_code_status`.
- B4: `wiki_related`'s `vector` list never contains two entries with the same `id`.
- B5: `wiki_insert_section`, `wiki_update_page` (via `replace_section`), and
  `wiki_move_section` write `## Heading\n\nbody` (blank line under the heading).

## Health Metrics
- Full test suite (`uv run pytest -q`) stays green — 2029/2029, no new failures.
- `flake8` stays clean.
- Git-storage mutation calls (`wiki_update_page`/`wiki_insert_section`/
  `wiki_delete_section`/`wiki_move_section` without `expected_revision`) keep working
  unchanged — B1 only changes descriptions, not behaviour.
- No silent breakage for text-matching clients on the `access denied` /
  `access_denied` wording — any format change is covered by
  `test_http_unit.py` / `test_http_streamable_transport.py`.
- `wiki_related` dedupe removes only exact `id` duplicates, never distinct-section
  neighbours.

## Strategic Context
- Interacts with: `server.py` (tool schema, `@_safe`), `http.py` (`_authorize_tool`,
  `_send_error`), `postgres/auth.py` (`AccessError`), `codegraph/context.py` +
  `codegraph/reader.py` (readiness protocol), `engine/chunk.py` + `engine/related.py`
  (section id), `engine/section.py` (all three section functions) and their callers in
  `server.py`. External consumers: MCP clients (Claude Code, Codex) generating calls from
  the published schema, and any HTTP client of the hosted server.
- Priority trade-off: trust — this is a contract fix for external clients; nothing moves
  toward speed or cost, predictability and behavioural compatibility (outside the fixed
  mismatch itself) matter most.

## Constraints
### Steering (behavioral guidance)
- Match existing code style (no autoformatter, flake8 max-line-length 100).
- Do not touch unrelated code or tests outside B1-B5.
- B1 changes only schema field descriptions (docstring/Field description), not signatures
  or behaviour.

### Hard (architectural enforcement)
- Do not change public tool names or required parameters (other than the already-optional
  `expected_revision`).
- Git storage (`storage=git`) must not start requiring `expected_revision` — this
  constraint stays as-is.
- `_safe`/`_code_safe` keep intercepting every exception; no new error code may propagate
  as an unhandled exception.
- Do not change the `AccessError` format for 401 (`authentication required`) — only the
  403 denial path for grant/`wiki_create_domain` tools is in scope for B2.
- B4's dedupe must not change `wiki_related`'s public response shape (`{vector, graph}`),
  only the contents of `vector`.

## Autonomy Zones
- Full autonomy (reversible, low risk): B1 (description text), B5 (section formatting),
  B4 (dedupe by id).
- Guarded (log + confidence threshold): B3 (reordering the readiness check in
  `wiki_code_context`) — verify all existing codegraph tests (`test_graph_runtime.py` and
  related) stay green before commit.
- Proposal-first (needs approval): B2 — changes the transport layer
  (`http.py:_send_error`) and the access-denial response format visible to external
  clients; show the exact diff and the new JSON-RPC error shape for approval before
  implementing.
- No autonomy (human only): none.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules
- Halt if: the B2 fix would require changing `AccessError` for 401 (authentication)
  paths — out of scope.
- Escalate if: `pytest` shows a regression outside the five affected modules after any fix.
- Done when: `uv run pytest -q` reports 2029+/2029 passed (0 failed), `flake8` is clean,
  and for each of B1-B5 the original bug scenario is reproduced manually with a new
  observed result (not just a green test).
