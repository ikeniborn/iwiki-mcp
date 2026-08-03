---
workflow:
  route: chain
  continuation: execute
---

# Intent: mcp-idle-timeout

**Date:** 2026-08-03
**Status:** approved

## Objective

Bound memory retained by abandoned stdio MCP server processes. The server must
terminate after 30 minutes without MCP activity, while allowing an operator to
disable the limit explicitly.

## Desired Outcomes

- A normally configured server exits cleanly after 1,800 seconds with no MCP
  requests.
- `IWIKI_IDLE_TIMEOUT_SECONDS=0` disables the timeout completely.
- A request received before expiry resets the inactivity window.
- An in-flight tool call is never interrupted by the timeout.
- Existing configurations that do not set the variable receive the 30-minute
  default.

## Constraints

### Hard

- The timeout applies only to the stdio MCP process lifecycle; it must not
  alter indexing, retrieval, write, or embedding semantics.
- The value must be a non-negative integer number of seconds. Invalid values
  must fail startup with an actionable stderr diagnostic.
- The server must exit cleanly enough for an MCP client to establish a fresh
  connection later; reconnect behavior remains the client's responsibility.
- The implementation must have deterministic tests for the default, zero,
  invalid values, reset-on-activity, and in-flight-call cases.
- Documentation must describe the default, the `0` override, and the possible
  client reconnect consequence.
- Every repository change includes the required patch-version bump.

## Scope

In scope: configuration parsing, stdio-server lifecycle handling, focused tests,
English/Russian user documentation, and project wiki documentation.

Out of scope: changing MCP client reconnect policy, terminating existing client
sessions, singleton server/proxy architecture, and altering embedding probes.

## Done When

The default is 30 minutes, `0` is unlimited, valid activity prevents premature
exit, an active tool is not interrupted, invalid settings fail clearly, and
focused plus full test suites pass.
