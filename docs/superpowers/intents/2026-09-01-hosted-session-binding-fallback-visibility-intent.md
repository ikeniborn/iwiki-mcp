---
review:
  intent_hash: 6d47e63defa58564
  last_run: 2026-09-01
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings:
    - id: F-001
      phase: alignment
      severity: WARNING
      section: Constraints
      section_hash: f70df0707328b54f
      fragment: "add fields only where the domain is not named by an argument"
      text: >-
        The requirement page asks every binding-dependent answer to carry
        `binding_source`; this intent narrows that to the answers whose domain is
        not named by an argument.
      fix: >-
        Record the narrowing on the requirement page, or widen the scope to every
        hosted answer.
      verdict: accepted
      verdict_at: 2026-09-01
workflow:
  route: chain
  continuation: execute
result_check:
  verdict: OK
  intent_hash: 6d47e63defa58564
  last_run: 2026-09-01
---
# Intent: hosted-session-binding-fallback-visibility

**Date:** 2026-09-01
**Status:** approved

## Objective

On the hosted HTTP transport a client's `wiki_bind` selection lives only in the server
process, keyed by `mcp-session-id`. When the container restarts, the session idles past
30 minutes, or the client reconnects, the server silently substitutes the token's own
default scope and answers as if nothing happened. Tools that name their domain keep
working; the domain-free code reads (`wiki_code_search`, `wiki_code_context`,
`wiki_code_status`) resolve `binding.primary` and answer for a different project with
`state: ready` and `fresh: true`. The only field that distinguishes such an answer from a
correct one is `domain`.

This matters now because it was reproduced on 2026-09-01 against iwiki-mcp 0.7.228 while
upgrading the hosted deployment: a session bound to `framework` received the `aioperator`
snapshot after the container was recreated, with no warning and no error. Analysis built
on that answer attributes another project's symbols, files, and call graph to the current
one, and nothing downstream can detect it.

## Desired Outcomes

- Given a fresh session with no `wiki_bind`, when `wiki_code_search` runs, then the answer
  carries `binding_source: token_default` and a `binding_defaulted` warning.
- Given the same session after `wiki_bind(primary="framework")`, when the identical call
  runs, then the answer carries `binding_source: session`, `domain: framework`, and no
  such warning.
- Given a session bound to a domain whose session state is then lost (server restart or
  idle expiry), when the client repeats the call without re-binding, then the answer
  carries `binding_source: token_default` rather than looking like an ordinary success.
- Given `wiki_bind` on the hosted transport, when it returns, then it reports the
  `session_id` it bound to and `binding_source: session`; a primary substituted by the
  write-scope intersection is reported as `primary_substituted: true` with the requested
  value, or refused.
- Given the fail-closed server option enabled, when a domain-free code read runs under
  `binding_source: token_default`, then it returns `binding_not_selected` and no snapshot
  content.
- Given `wiki_status` in either state, when it answers, then its `binding_source` matches
  what the code reads report in the same session.

## Health Metrics

- The authorization model is unchanged: the token's grants stay the absolute limit,
  nothing widens a scope, and `_authorize_tool` / `authorize_domains` behave as before.
- The fallback stays permitted by default; existing clients keep working without
  re-binding, and the fail-closed option is off by default.
- No existing answer field is renamed or removed. New keys appear only on the agreed
  answers: `wiki_status`, `wiki_bind`, the three code reads, and
  `wiki_code_publish_begin`.
- The registered tool count stays 35 and no tool's JSON Schema changes.
- stdio / local PostgreSQL and Git storage answer exactly as before — the new fields are
  absent there.
- `uv run pytest -q` and `uv run flake8 src tests` stay green with no new failures, and
  `wiki_lint` reports no new finding.
- No new locking or serialization: the provenance lives in the per-request state already
  held under `request_lock`, so hosted request latency does not grow.

## Strategic Context

- Interacts with: hosted HTTP clients (Claude Code / Codex through `iwiki-remote`), the
  MCP session manager and its idle timeout, the token grant layer (`AuthStore`,
  `AuthContext`), published code-graph snapshots in PostgreSQL, and the agent protocols in
  `CLAUDE.md` that mandate `wiki_bind` and the "trust code results only when
  `state: ready` and `fresh: true`" rule.
- The deployment operator is the second consumer: recreating the container silently
  changes what clients get, and that operator needs the fail-closed option.
- Priority trade-off: **trust**. Correct domain attribution outweighs an extra field in
  the answer and outweighs convenience — a silent substitution is worse than a refusal.

## Constraints

### Steering (behavioral guidance)

- Piecemeal growth: add fields only where the domain is not named by an argument —
  `wiki_status`, `wiki_bind`, the three code reads, and `wiki_code_publish_begin`. Blanket
  injection through `_safe` is rejected.
- Reuse existing seams: `_HostedSelectedState` / `_HostedBindingState`,
  `_effective_binding`, the reader's `warnings` list, and `HostedCodeGraphConfig`.
- Match repository style: flake8 with `max-line-length = 100`, fail-soft handlers, and
  tests that never hit the network (`monkeypatch` embeddings).
- Use the requirement page's vocabulary verbatim: `binding_source`, `session`,
  `token_default`, `binding_defaulted`, `primary_substituted`, `binding_not_selected`.

### Hard (architectural enforcement)

- No persistence of a client's binding selection in PostgreSQL; the selection stays
  session-scoped and process-local.
- No change to the authorization model; nothing widens a scope; the token's grants stay
  the absolute limit.
- The server does not read the project's `.iwiki.toml`.
- The specification-mode precedence itself is unchanged; only the visibility of the tier
  that answered changes.
- No new tool; the contract is carried by fields on existing answers, and the count stays
  35.
- The fallback stays permitted by default; fail-closed is an explicit server option and
  applies only to the domain-free code reads.
- Patch version bump with every version surface synchronized (`pyproject.toml`,
  `uv.lock`, `src/iwiki_mcp/__init__.py`, `tests/test_package.py`).
- Live PostgreSQL verification runs against a disposable container created for this task
  and removed afterwards; existing containers, volumes, and data stay untouched.

## Autonomy Zones

- Full autonomy (reversible, low risk): internal helper and private state-field names,
  test structure, `hint` wording, documentation phrasing, the patch version bump.
- Guarded (log + confidence threshold): the exact set of answers receiving the fields
  inside the agreed narrow scope; key ordering and the placement of `binding_defaulted`
  within `warnings`; repairs to existing hosted tests that break on the new keys.
- Proposal-first (needs approval): the name and default of the fail-closed server option;
  any widening of the field scope beyond the agreed set; returning `session_id` to the
  client in clear text; any change to an existing answer key.
- No autonomy (human only): changes to the authorization or grant model; persisting the
  binding in PostgreSQL; making the server read `.iwiki.toml`; flipping the fail-closed
  default; adding a new tool.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules

- Halt if: making the provenance visible would require widening a scope, changing
  authorization, or persisting the selection — that is outside the requirement's
  non-goals.
- Halt if: a change breaks an existing answer key, or the registered tool count is not 35.
- Halt if: the fail-closed gate fires on any tool other than the three domain-free code
  reads.
- Escalate if: the disposable PostgreSQL container cannot be created or the live suites
  cannot run against it — record the evidence gap instead of claiming completion.
- Done when: all six acceptance criteria are observable through the MCP tools alone;
  `uv run pytest -q` and `uv run flake8 src tests` are green against a disposable
  PostgreSQL container that is removed after the run; `wiki_lint` reports no new finding;
  and the requirement page, README files, and architecture docs state the session-lifetime
  contract.
