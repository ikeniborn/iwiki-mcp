---
chain:
  intent: docs/superpowers/intents/2026-09-05-codegraph-context-path-filter-defects-intent.md
---
# Design: codegraph-context-path-filter-defects

**Date:** 2026-09-06
**Status:** approved
**Intent:** docs/superpowers/intents/2026-09-05-codegraph-context-path-filter-defects-intent.md

## 1. Scope and root causes

Fix the local code-graph read path in six sequential slices. Triage evidence (task page
`iwiki-mcp/reference/tasks/codegraph-context-path-filter-defects`, history segment 1)
established the root causes this design addresses:

- `mark_dirty_if_stale` writes SQL `state='dirty'` without republishing
  `code-<domain>.metadata.json`; the SQLite write invalidates the sealed `storage_stamp`,
  so `exact_ready_metadata` fails permanently — `metadata_reconstructed` + zeroed counts.
- `input_fingerprint` folds in an exclude-blind whole-repo `git status` marker and a
  domain-wide wiki-selector digest, so concurrent sessions flip `dirty` while the three
  reported fingerprints stay identical.
- `wiki_code_context` can never auto-rebuild a dirty graph (deadline-bounded budget is
  always below the `max_rebuild_seconds` gate) while `wiki_code_search` rebuilds
  unconditionally; the `include_wiki=True` selector-capture path relabels any failure as
  `stale` with empty nodes.
- `invalid_config` errors discard the offending key/parameter name.
- The `path` prefix filter compares the raw argument without normalization; the matchers
  themselves are correct.
- `read_mode` is validated but consulted by no read path.

## 2. R1 — Transition envelope (slice S1)

One helper (`publish_transition_envelope(state)` in the store/indexer layer) is the only
way to change the SQL repository state. Under the already-held `mutation_lock` +
`code_graph_write_lock` it: writes the SQL state, rebuilds the metadata document from the
previous valid envelope (counts, diagnostics, fingerprints inherited), sets the declared
`state`, captures a fresh `storage_stamp` taken after the SQL write, recomputes
`metadata_digest`, and publishes with the existing atomic staged-replace path.

`exact_ready_metadata` generalizes to `valid_envelope(metadata, sql_row, storage_stamp)`:
same exact key-set plus the declared `state` field, digest check, stamp check, and
state agreement with the SQL row. `_read_status` reports counts from the envelope in both
`ready` and `dirty`; `metadata_reconstructed` fires only on a genuine mismatch (corruption
or an out-of-band write). All dirty writers (`mark_dirty_if_stale`,
`_mark_selector_snapshot_dirty`, the selector-verify failure path, the `SelectorError`
path) and `_recover_stale_metadata` route through the helper, so recovery output stays
matchable. An external SQL write that bypasses the helper still breaks the stamp —
tamper-evidence and `test_external_sql_row_update_breaks_sealed_storage_stamp` are
preserved. A crash between the SQL write and the JSON publish leaves a transient mismatch
repaired by the next transition or rebuild. `pending_final_verify` semantics are
unchanged.

DoD: a second runtime instance marking the graph dirty leaves the first instance's
`wiki_code_status` reporting `dirty` with intact counts and no `metadata_reconstructed`;
two consecutive status calls on an unchanged repository are identical.

## 3. R2 — Exclude-aware input fingerprint (slice S2)

`git_dirty_marker` filters the `git status --porcelain=v1` output through the same
`GitIgnoreSpec` discovery uses (`config.exclude` + `.iwikiignore`) before folding paths
into the marker. The wiki-selector digest is computed only over pages carrying `code.*`
selectors (sorted page-id + selector-map pairs); an ordinary wiki write changes nothing in
`input_fingerprint`.

DoD: touching a file under an excluded path (e.g. `.worktrees/`) or writing a
selector-free wiki page leaves `ready_now()` true; touching an indexed source still flips
`dirty`.

## 4. R3 — Context parity with search (slice S3)

`wiki_code_context` calls `query_guard()` with the full budget (as search does) before
acquiring the wiki-selector lease, removing the deadline asymmetry without changing the
rebuild gate. A selector-capture failure no longer relabels the answer: the response
carries graph nodes/relations plus a `wiki_selector_unavailable` warning with `wiki_pages`
suppressed (mirroring the remote `include_wiki` suppression behavior).
`test_runtime_missing_wiki_domain_returns_complete_nonready_context` is updated to the new
contract.

DoD: on a dirty graph with rebuild budget, context returns an IMPORTS traversal for a
module seed; with the wiki domain missing and `include_wiki=true`, context returns nodes
plus the warning, not `stale` with empty nodes.

## 5. R4 — Diagnosable invalid_config (slice S4)

`CodeGraphConfigError` carries `field`; `CodeGraphContextError` carries `parameter`.
`sanitized_error` whitelists exactly that one identifier into the response:
`{"error": "code graph configuration is invalid", "code": "invalid_config",
"field": "<name>", "hint": …}`. No exception text leaks; existing sanitize tests stay
green. `_configuration_error` stores the field name instead of a boolean. Every non-ready
query-path answer is normalized to carry `error` + `code` + `hint` beside its empty
`results`, so it cannot be mistaken for an empty filter match.

DoD: a bad `depth` names `depth`; an unknown `[code_graph]` key names that key; no test
asserts leaked exception text fails.

## 6. R5 — Path-prefix normalization (slice S5)

The search request uses the value `_validated_relative_posix` returns: strip a leading
`./`, collapse duplicate slashes, trim surrounding whitespace, reject an empty result.
The normalized prefix feeds both the SQLite `substr` filter and the PostgreSQL
`starts_with` filter. Matching stays case-sensitive (paths are exact); the contract is
documented. Coverage extends to the canonical (non-alias) module and file branches and
adds the first PostgreSQL path-filter test.

DoD: `./deploy/services`, `deploy//services`, and a trailing-space variant return the
same hits as `deploy/services` on both backends.

## 7. R6 — read_mode routing (slice S6)

A hosted `PostgresBinding` read is unchanged. On a local binding the reader is selected by
`read_mode`, one mode, no fallback (symmetric with `publish_mode`):

- `sqlite` (default): the local snapshot via `CodeGraphRuntime` — current behavior.
- `postgres`: the published snapshot via the direct PostgreSQL reader using the DSN from
  the environment; a missing DSN returns `invalid_config` with `field: "read_mode"`.
- `mcp`: remote transit via `McpCodeGraphReader` using `IWIKI_CODE_GRAPH_MCP_URL` /
  `IWIKI_CODE_GRAPH_MCP_TOKEN`; missing credentials return the same typed error.

A failure of the selected mode is the result — never retry another mode. In `postgres` and
`mcp` read modes the local auto-rebuild on read is skipped; freshness is governed by the
published snapshot (`stale_snapshot` semantics). `wiki_code_index` remains a local build.
README, `docs/README.ru.md` (if present), and the wiki concept pages are updated.

DoD: with `read_mode="mcp"` and credentials set, `wiki_code_search` on the local server
answers from the hosted snapshot; with credentials absent the error names `read_mode`;
`read_mode="sqlite"` behavior is byte-identical to today.

## 8. R7 — Testing strategy

TDD per slice: a failing test reproducing the defect precedes each fix. S1 adds a
cross-process-shaped test (two runtime instances over one `.iwiki/` state). S5 and S6 run
their PostgreSQL parts against a disposable database per the repository recipe. Observable
acceptance behaviors get `iwiki-gwt` scenarios (domain mode `strict`, projection additive)
with `implements` + `verifies` bindings: truthful status counts, context auto-rebuild,
normalized path prefix, read_mode routing. Query-path latency is checked manually on the
full run (advisory intent finding F-001 stands).

DoD: `uv run pytest -q` and `uv run flake8 src tests` green per slice; PostgreSQL suite
green on the disposable database for S5/S6; scenarios valid in `wiki_lint`.

## 9. R8 — Delivery model

Six branches `dev-codegraph-context-path-filter-defects-s1` … `-s6`, each cut from
refreshed `master` after the previous PR merges, each in a sibling worktree, integrated
via a PR into `master` (merge is proposal-first per the intent's autonomy zones). Each PR
carries a patch version bump in `pyproject.toml` and the wiki/README updates for its
slice. The chain closes with `/check-chain result` reconciled against the plan.

DoD: six merged PRs; task page lifecycle `done` only after final evidence and a clean
`wiki_lint`.

## Acceptance (from intent)

Desired Outcomes, verbatim:

- On a `ready`/`fresh` snapshot: `wiki_code_search(path=…)` returns the prefix-matched subset; `wiki_code_context` traverses IMPORTS for a module seed; two consecutive `wiki_code_status` calls with no intervening repository change report identical state and counts.
- Concurrency: editing a file under an excluded path (e.g. `.worktrees/`) or writing an ordinary wiki page (no `code.*` selectors) in a parallel session does NOT flip a `ready` snapshot to `dirty`. When `dirty` does occur legitimately, counts never zero out while the revision is still the published one, and `wiki_code_context` recovers via bounded auto-rebuild exactly as `wiki_code_search` does.
- Diagnostics: every `invalid_config` error names the offending config field or request parameter (whitelisted name only); a non-ready-graph answer with empty `results` is unambiguous — it cannot be mistaken for an empty filter match.
- `read_mode` is honored for real: code-graph reads route through the adapter the key selects (`sqlite` | `postgres` | `mcp`), matching the documented contract.

Done when, verbatim:

- Done when: every Desired Outcome above is demonstrated observably (acceptance scenarios run against a real snapshot, not only unit tests), the full suite including live-PostgreSQL tests is green, and all slice PRs are merged into `master`.
