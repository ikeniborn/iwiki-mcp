---
review:
  intent_hash: 751bdd3769530f19
  last_run: 2026-09-05
  phases:
    structure:
      status: passed
    completeness:
      status: passed
    clarity:
      status: passed
    consistency:
      status: passed
    alignment:
      status: passed
  findings:
    - id: F-001
      phase: clarity
      severity: WARNING
      section: Health Metrics
      section_hash: 8a455fd473e4063a
      fragment: "Query-path latency does not measurably regress"
      text: "Latency metric has no named threshold or measurement command; 'measurably' lacks a criterion."
      fix: "Name a bound (e.g. status/search p50 on the test fixture within +10% of master) or a benchmark command."
      verdict: open
      verdict_at: null
    - id: F-002
      phase: alignment
      severity: INFO
      section: Desired Outcomes
      section_hash: ee6881a241215de3
      fragment: "read_mode is honored for real"
      text: "Scope extends beyond the original task page TODO (read_mode wiring); explicitly chosen by the user in Q5b."
      fix: null
      verdict: open
      verdict_at: null
---
# Intent: codegraph-context-path-filter-defects

**Date:** 2026-09-05
**Status:** approved

## Objective

The published local code-graph snapshot must stay readable and its diagnostics truthful. Today the read path defeats itself: the query-path `mark_dirty_if_stale` writes SQL `dirty` without republishing the metadata envelope, and the SQLite write invalidates the sealed `storage_stamp`, so every later `wiki_code_status` reports `metadata_reconstructed` with zeroed counts until a full rebuild. The dirty flip itself fires from an exclude-blind whole-repo `git status` marker and a domain-wide wiki-selector digest, so concurrent agent sessions render the graph effectively unusable (live incident, `aioperator`, 2026-09-05). On top of that, `wiki_code_context` can never auto-rebuild a dirty graph while `wiki_code_search` always can, `invalid_config` errors hide the offending key, the `path` prefix filter silently returns empty for unnormalized prefixes, and the `read_mode` config key is dead — validated but consulted by no read path. Fix now: the defects compound each other and block agent workflows that depend on code-graph reads.

## Desired Outcomes

- On a `ready`/`fresh` snapshot: `wiki_code_search(path=…)` returns the prefix-matched subset; `wiki_code_context` traverses IMPORTS for a module seed; two consecutive `wiki_code_status` calls with no intervening repository change report identical state and counts.
- Concurrency: editing a file under an excluded path (e.g. `.worktrees/`) or writing an ordinary wiki page (no `code.*` selectors) in a parallel session does NOT flip a `ready` snapshot to `dirty`. When `dirty` does occur legitimately, counts never zero out while the revision is still the published one, and `wiki_code_context` recovers via bounded auto-rebuild exactly as `wiki_code_search` does.
- Diagnostics: every `invalid_config` error names the offending config field or request parameter (whitelisted name only); a non-ready-graph answer with empty `results` is unambiguous — it cannot be mistaken for an empty filter match.
- `read_mode` is honored for real: code-graph reads route through the adapter the key selects (`sqlite` | `postgres` | `mcp`), matching the documented contract.

## Health Metrics

- Full test suite green: `uv run pytest -q`, including `tests/codegraph` and `tests/postgres` against a live disposable database for every PostgreSQL-touching slice.
- Sanitization guarantees intact: no exception text, secrets, or environment values leak into tool error responses (existing sanitize tests stay green).
- Publication atomicity and crash-recovery semantics of the snapshot unchanged: a failed publication still degrades safely and `_abort`/supersede paths still work.
- Query-path latency does not measurably regress: exclude-aware dirty detection must not add heavy scans to `query_guard`.
- Schema v2 and the publication protocol remain compatible — no migrations, no batch-format changes.

## Strategic Context

- Interacts with: `codegraph/runtime.py` (status/search/context state machine), `codegraph/indexer.py` (dirty transitions, `ready_now`, publication), `codegraph/fingerprint.py` (input fingerprint), `codegraph/query.py` + `postgres/codegraph.py` (path filter), `codegraph/config.py` (read_mode, error naming), `codegraph/linking.py` (wiki-selector digest), `server.py` tool surface, and every concurrent agent session sharing one checkout and `.iwiki/` state.
- Priority trade-off: **trust**. Agents make decisions from these tool answers; a false `dirty` or a silent empty result costs more than a slower fix.

## Constraints

### Steering (behavioral guidance)
- TDD per slice: each slice starts from a failing test that reproduces the defect, then the fix, then full-suite verification.
- Slice delivery model: each slice on its own `dev-codegraph-context-path-filter-defects-s<N>` branch in a sibling worktree, integrated via a PR into `master`; slices are sequential, each branch cut from refreshed `master` after the previous PR merges.
- Wiki-selector digest scope: only pages carrying `code.*` selectors (their set + values) feed `input_fingerprint`; ordinary wiki writes never dirty the graph.
- Observable-contract changes author or update `iwiki-gwt` scenarios (domain mode is `strict`; projection currently absent, scenarios are additive) with `implements` + `verifies` bindings.

### Hard (architectural enforcement)
- No schema-v2 migrations and no publication-protocol changes.
- Sanitization preserved: error responses carry a whitelisted field/parameter name only, never exception text.
- The fail-soft `@_safe` / `_code_safe` tool contract is unchanged.
- Every writer of the SQL repository state must leave the (SQL row, metadata envelope) pair consistent — no state transition may orphan the sealed metadata.

## Autonomy Zones

- Full autonomy (reversible, low risk): internal refactorings, tests, error-message wording.
- Guarded (log + confidence threshold): dirty-envelope shape and runtime state-machine changes — every change backed by invariant tests and recorded in the task ledger.
- Proposal-first (needs approval): any public tool/config contract change beyond what this intent already approves (including the final `read_mode` routing semantics); merging each slice PR.
- No autonomy (human only): schema migrations; touching other projects' bases or domains.

> These zones OVERRIDE subagent-driven-development's "continuous execution,
> don't pause" default. Any task touching proposal-first / no-go decisions
> is marked HUMAN CHECKPOINT in the plan.

## Stop Rules

- Halt if: a fix contradicts the publication atomicity/crash-recovery invariant, or a slice requires a schema or publication-protocol change.
- Escalate if: two different strategies fail on the same slice, or a concurrency test exposes a defect outside the five triaged root causes.
- Done when: every Desired Outcome above is demonstrated observably (acceptance scenarios run against a real snapshot, not only unit tests), the full suite including live-PostgreSQL tests is green, and all slice PRs are merged into `master`.
