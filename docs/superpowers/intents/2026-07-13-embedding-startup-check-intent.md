---
review:
  intent_hash: d325c9642f593740
  last_run: 2026-07-13
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
      section: "Health Metrics"
      section_hash: 87b18c39d734edaf
      fragment: "Startup remains fast when the endpoint is available"
      text: "The startup-speed health metric is observable but has no numeric threshold."
      fix: "Define a bounded probe timeout and expected added startup latency in the design."
      verdict: open
      verdict_at: null
---
# Intent: embedding-startup-check

**Date:** 2026-07-13
**Status:** approved

## Objective

Detect an unavailable configured embeddings endpoint when `iwiki-mcp` starts,
instead of discovering the failure only when an embedding-dependent tool is
first called. Prevent a silent start that makes the wiki appear fully usable
when embedding-backed behavior is unavailable.

## Desired Outcomes

- When the configured embeddings endpoint is unavailable, the MCP server
  refuses to start and emits a prominent, understandable notification.
- When the endpoint is available, the server starts normally and all existing
  wiki capabilities remain available.

## Health Metrics

- Startup remains fast when the endpoint is available, apart from one minimal
  validation request and its network latency.
- Compatibility with OpenAI-compatible embeddings endpoints is preserved.
- Unit tests make no real network requests.

## Strategic Context

- Interacts with: the stdio MCP entry point, environment-backed embedding
  configuration, the OpenAI-compatible embeddings client, MCP clients that
  spawn the server, and operators diagnosing startup failures.
- Priority trade-off: trust over startup speed and request cost. One real,
  minimal embedding request at every server start is acceptable.

## Constraints

### Steering (behavioral guidance)

- Make the failure immediate, prominent, and actionable.
- Include the full configured endpoint, embedding model, failure reason, and a
  configuration hint in the notification.
- Keep the change surgical and reuse existing configuration and embedding
  behavior where practical.

### Hard (architectural enforcement)

- Do not start the MCP server when embedding endpoint validation fails.
- Write diagnostics to stderr only; never contaminate the stdio MCP protocol on
  stdout.
- Never reveal the embedding API key in diagnostics.
- Preserve provider-neutral support for OpenAI-compatible embedding APIs.
- Tests must replace network access with deterministic fakes.

## Autonomy Zones

- Full autonomy (reversible, low risk): design code boundaries, tests, failure
  wording, and documentation within the approved intent and design.
- Guarded (log + confidence threshold): choose the minimal probe input and
  bounded timeout/retry behavior, with explicit tests for the resulting startup
  contract.
- Proposal-first (needs approval): add or change public environment variables,
  CLI options, or the externally visible startup contract beyond the behavior
  stated in this intent.
- No autonomy (human only): use or inspect real API keys, weaken secret handling,
  or permit startup after failed endpoint validation.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions is
> marked HUMAN CHECKPOINT in the plan.

## Stop Rules

- Halt if FastMCP or the stdio transport cannot emit a clear pre-protocol stderr
  diagnostic before process exit without corrupting stdout.
- Escalate if endpoint availability cannot be validated with an
  OpenAI-compatible embeddings request without introducing a provider-specific
  contract or a new public configuration surface.
- Done when an available endpoint permits normal server startup, an unavailable
  endpoint prevents startup with the required stderr diagnostic, existing
  embedding behavior remains compatible, and automated tests prove both startup
  paths without real network access.
