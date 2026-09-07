---
chain:
  intent: docs/superpowers/intents/2026-09-05-codegraph-context-path-filter-defects-intent.md
  spec: docs/superpowers/specs/2026-09-06-codegraph-context-path-filter-defects-design.md
review:
  plan_hash: 99b0b16c7c530f83
  last_run: 2026-09-07
  phases:
    structure:
      status: passed
    coverage:
      status: passed
    dependencies:
      status: passed
    verifiability:
      status: passed
    consistency:
      status: passed
  findings: []
---
# Codegraph Context and Path Filter Defects Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the local code-graph read path truthful and usable under concurrent agent sessions: consistent (SQL, metadata) state transitions, exclude-aware dirty detection, context/search rebuild parity, named `invalid_config` fields, normalized path-prefix filtering, and real `read_mode` routing.

**Architecture:** Six sequential slices (S1–S6), each on its own `dev-codegraph-context-path-filter-defects-s<N>` branch in a sibling worktree with a PR into `master`. S1 introduces a single transition-envelope helper that every SQL state writer routes through; S2 narrows the input fingerprint; S3 reorders context's guard; S4 threads a whitelisted field name through sanitized errors; S5 applies the existing `module_key` normalizer to the search prefix; S6 wires `read_mode` to reader selection.

**Tech Stack:** Python 3.11+, sqlite3, pytest (`asyncio_mode=auto`, `pythonpath=src`), flake8 (max-line-length 100), disposable PostgreSQL (pgvector) for S5/S6 postgres parts.

**Spec:** docs/superpowers/specs/2026-09-06-codegraph-context-path-filter-defects-design.md

## Global Constraints

- No schema-v2 migrations, no publication-protocol changes (intent hard constraint).
- Sanitization preserved: error responses carry a whitelisted field/parameter name only, never exception text; existing sanitize tests must stay green.
- The `@_safe` / `_code_safe` fail-soft tool contract is unchanged.
- Every writer of the SQL repository state leaves the (SQL row, metadata envelope) pair consistent.
- Each slice PR: patch version bump in `pyproject.toml`, README/`docs/README.ru.md` updates when behavior/usage changes, wiki page updates via MCP tools (parent session), `uv run pytest -q` + `uv run flake8 src tests` green.
- Merging each slice PR is proposal-first (HUMAN CHECKPOINT) per the intent's autonomy zones.
- Branch/worktree per slice, cut from refreshed `master` after the previous PR merges:

```bash
base="master"
branch="dev-codegraph-context-path-filter-defects-s<N>"
root="$(git rev-parse --show-toplevel)"
project="$(basename "$root")"
parent="$(dirname "$root")"
git fetch origin "$base"
git worktree add -b "$branch" "$parent/$project-$branch" "origin/$base"
```

- After each PR is created: `git worktree remove "$parent/$project-$branch" && git worktree prune`.
- PostgreSQL parts (Task 6, Task 7) run against a disposable database per `CLAUDE.md`:

```bash
docker run -d --name iwiki-pgtest -e POSTGRES_PASSWORD=pgtest -e POSTGRES_DB=iwiki_test -p 127.0.0.1:55432:5432 pgvector/pgvector:pg16
docker exec iwiki-pgtest psql -U postgres -d iwiki_test -c "CREATE EXTENSION IF NOT EXISTS vector;"
IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55432/iwiki_test" uv run pytest -q tests/postgres
docker rm -f iwiki-pgtest
```

---

### Task 1: Merge the chain-docs branch

**Files:**
- No source changes; branch `dev-codegraph-context-path-filter-defects` (intent, spec, this plan).

**Interfaces:**
- Produces: chain artifacts on `master`, so every slice worktree carries them.

- [ ] **Step 1: Push and open the PR**

```bash
git push -u origin dev-codegraph-context-path-filter-defects
gh pr create --base master --head dev-codegraph-context-path-filter-defects \
  --title "docs: IDD chain for codegraph-context-path-filter-defects" \
  --body "Intent, design spec (R1-R8), and implementation plan for the six-slice code-graph read-path fix. Task page: iwiki-mcp/reference/tasks/codegraph-context-path-filter-defects."
```

- [ ] **Step 2: HUMAN CHECKPOINT — merge**

Ask the user to merge (or approve merging) the PR. Wait.

### Task 2: S1 — Transition envelope (spec R1)

**Files:**
- Modify: `src/iwiki_mcp/codegraph/indexer.py` (helper + `exact_ready_metadata` generalization + dirty writers at lines ~1120-1165, ~1271, ~1289, ~1582, ~1934)
- Modify: `src/iwiki_mcp/codegraph/runtime.py:571-633` (`_read_status` state-aware matching), `runtime.py:896-956` (`_recover_stale_metadata`)
- Test: `tests/codegraph/test_indexer_runtime.py`

**Interfaces:**
- Produces: `publish_transition_envelope(store, domain, state, previous_metadata)` — writes the SQL state and republishes `code-<domain>.metadata.json` with inherited counts/diagnostics, declared `state`, a `storage_stamp` captured after the SQL write, and a recomputed `metadata_digest`, all under the already-held `mutation_lock` + `code_graph_write_lock`.
- Produces: `valid_envelope(metadata, sql_row, storage_stamp)` — generalization of `exact_ready_metadata` (`indexer.py:392`): same exact key-set discipline plus a `state` field that must agree with the SQL row; `exact_ready_metadata(m)` becomes `valid_envelope(m, state="ready")`.
- Consumes: existing `store.set_repository_state`, `store.storage_stamp()` (`store.py:965-995`), the atomic staged-replace metadata publish (`store.py:2024-2072`).

- [ ] **Step 1: Write the failing truthful-dirty test**

In `tests/codegraph/test_indexer_runtime.py` (follow the module's existing fixture pattern for building a ready snapshot, e.g. the setup used by `test_query_guard_materializes_changed_source_as_dirty_without_rows`):

```python
def test_dirty_transition_preserves_counts_and_matches_metadata(ready_graph_env):
    """A second runtime marking dirty must not zero counts or reconstruct."""
    runtime_a, runtime_b = ready_graph_env.two_runtimes()
    before = runtime_a.status()
    assert before["state"] == "ready" and before["counts"]["files"] > 0
    ready_graph_env.change_indexed_source()           # legitimate dirty cause
    runtime_b.query_guard()                            # writes SQL dirty
    after = runtime_a.status()
    assert after["state"] == "dirty"
    assert after["counts"] == before["counts"]         # counts survive
    assert "metadata_reconstructed" not in after["warnings"]
```

Add a stability companion:

```python
def test_status_idempotent_without_repository_change(ready_graph_env):
    runtime = ready_graph_env.runtime()
    first = runtime.status()
    second = runtime.status()
    assert first == second
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/codegraph/test_indexer_runtime.py -k "dirty_transition_preserves or status_idempotent" -v`
Expected: FAIL — `counts` zeroed and `metadata_reconstructed` present after the dirty flip.

- [ ] **Step 3: Implement the helper and route every dirty writer through it**

In `indexer.py`: add `publish_transition_envelope`; call it instead of the bare `self.store.set_repository_state(self.domain, "dirty")` at both `mark_dirty_if_stale` sites (`indexer.py:1271`, `indexer.py:1289`), in `_mark_selector_snapshot_dirty` (~`:1165`, replacing the minimal `_transition_metadata` record), in the selector-verify failure path (~`:1582`), and in the `SelectorError` path (~`:1934`). Generalize `exact_ready_metadata` to `valid_envelope` (keep `exact_ready_metadata` as the `state="ready"` wrapper so publication call sites are untouched). In `runtime.py:571-588` replace the hardcoded `persisted.get("state") == "ready"` clause with state agreement against the SQL row and accept a valid dirty envelope as `metadata_matches`; keep counts from the envelope for `ready` and `dirty` (`runtime.py:626-633`). Make `_recover_stale_metadata` publish through the helper so its output stays matchable.

- [ ] **Step 4: Run the slice tests, then the guard rails**

Run: `uv run pytest tests/codegraph/test_indexer_runtime.py tests/codegraph/test_recovery_concurrency.py tests/codegraph/test_query.py -q`
Expected: PASS, including the preserved tamper-evidence test `test_external_sql_row_update_breaks_sealed_storage_stamp` (an out-of-band SQL write still bypasses the helper and breaks the stamp).

- [ ] **Step 5: Full verification**

Run: `uv run pytest -q && uv run flake8 src tests`
Expected: both green.

- [ ] **Step 6: Version bump, docs, commit**

Patch-bump `pyproject.toml`. Update the wiki pages `concept/code-graph-runtime` (state machine: transition envelope) and `concept/code-graph-storage` (envelope invariant) via MCP tools — parent session. Author the GWT scenario `codegraph-truthful-dirty-status` (given ready snapshot + concurrent dirty mark, when `wiki_code_status`, then dirty state with intact counts) with `implements` (helper symbol) + `verifies` (test file) bindings — parent session. Commit:

```bash
git add -A && git commit -m "fix(codegraph): publish a transition envelope on every state write"
```

- [ ] **Step 7: PR + HUMAN CHECKPOINT — merge**

Push, `gh pr create --base master`, remove the worktree, ask the user to merge. Wait.

### Task 3: S2 — Exclude-aware input fingerprint (spec R2)

**Files:**
- Modify: `src/iwiki_mcp/codegraph/fingerprint.py:140-201` (`git_dirty_marker`, `compose` of `inputs`)
- Modify: `src/iwiki_mcp/codegraph/linking.py:1334-1446` (selector digest scope)
- Test: `tests/codegraph/test_discovery_fingerprint.py`, `tests/codegraph/test_linking.py`

**Interfaces:**
- Consumes: `_ignore_spec` merge logic from `discovery.py:324-342` (reuse, do not duplicate: extract or import the merged `GitIgnoreSpec` builder).
- Produces: `git_dirty_marker(project_root, ignore_spec)` — same return type, but porcelain lines whose paths match the ignore spec are dropped before hashing; selector digest computed only over pages carrying `code.*` selectors.

- [ ] **Step 1: Write the failing tests**

```python
def test_excluded_path_edit_does_not_change_dirty_marker(tmp_repo):
    spec = tmp_repo.ignore_spec(exclude=[".worktrees/"])
    before = git_dirty_marker(tmp_repo.root, spec)
    (tmp_repo.root / ".worktrees" / "noise.txt").write_text("x")
    assert git_dirty_marker(tmp_repo.root, spec) == before

def test_indexed_source_edit_changes_dirty_marker(tmp_repo):
    spec = tmp_repo.ignore_spec(exclude=[".worktrees/"])
    before = git_dirty_marker(tmp_repo.root, spec)
    (tmp_repo.root / "pkg" / "mod.py").write_text("changed = True\n")
    assert git_dirty_marker(tmp_repo.root, spec) != before
```

In `test_linking.py`: a selector-free wiki page write leaves the selector digest unchanged; adding/altering a `code.*` selector changes it.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/codegraph/test_discovery_fingerprint.py -k dirty_marker -v`
Expected: FAIL — excluded edit changes the marker today.

- [ ] **Step 3: Implement**

Add `filter_porcelain_lines(lines, ignore_spec)` in `fingerprint.py` and call it inside `git_dirty_marker`: parse the path column of each `git status --porcelain=v1 --untracked-files=normal` line (including the rename `a -> b` form — match both sides) and drop lines whose paths the ignore spec matches. Narrow the selector digest input to sorted `(page_id, selector_map)` pairs of pages that declare `code.*` selectors.

- [ ] **Step 4: Latency bound (closes spec finding F-001)**

Add a timed regression test — the exclude filtering must stay cheap:

```python
def test_dirty_marker_filtering_latency_bound(tmp_repo):
    spec = tmp_repo.ignore_spec(exclude=[".worktrees/"])
    lines = [f"?? .worktrees/w{i}/file{i}.py" for i in range(10_000)]
    start = time.monotonic()
    filter_porcelain_lines(lines, spec)
    assert time.monotonic() - start < 0.5
```

Run: `uv run pytest tests/codegraph/test_discovery_fingerprint.py -k latency -v` → PASS.

- [ ] **Step 5: Full verification, docs, PR**

`uv run pytest -q && uv run flake8 src tests` → green. Patch-bump. Update wiki `concept/code-graph-discovery-fingerprints` (marker scope) — parent session. GWT scenario `codegraph-excluded-edit-stays-ready` — parent session. Commit `fix(codegraph): scope the dirty marker and selector digest to indexed inputs`, PR, remove worktree, HUMAN CHECKPOINT — merge.

### Task 4: S3 — Context parity with search (spec R3)

**Files:**
- Modify: `src/iwiki_mcp/codegraph/runtime.py:1397-1489` (context guard ordering, selector-failure handling)
- Test: `tests/codegraph/test_indexer_runtime.py`, `tests/codegraph/test_linking.py:1429-1520`

**Interfaces:**
- Consumes: `query_guard()` (`runtime.py:1075-1183`) — called with no `remaining_seconds`, exactly as `search` does at `runtime.py:1213`.
- Produces: context responses where a selector failure yields nodes/relations plus warning `wiki_selector_unavailable` and suppressed `wiki_pages`.

- [ ] **Step 1: Write the failing tests**

```python
def test_context_auto_rebuilds_dirty_graph_like_search(ready_graph_env):
    runtime = ready_graph_env.runtime()
    ready_graph_env.change_indexed_source()
    runtime.query_guard()                       # graph now dirty
    response = runtime.context(module_seed_request(ready_graph_env))
    assert response["state"] == "ready"
    assert response["nodes"], "bounded auto-rebuild must serve context too"

def test_selector_failure_returns_graph_with_warning(ready_graph_env):
    runtime = ready_graph_env.runtime()
    ready_graph_env.remove_wiki_domain()
    response = runtime.context(module_seed_request(ready_graph_env))
    assert response["nodes"]
    assert "wiki_selector_unavailable" in response["warnings"]
    assert "wiki_pages" not in response
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/codegraph/test_indexer_runtime.py -k context_auto_rebuilds -v` and `-k selector_failure` in `test_linking.py`.
Expected: FAIL — today: `stale` with empty nodes.

- [ ] **Step 3: Implement**

Reorder: run `query_guard()` (full budget) before acquiring the wiki-selector lease; drop the `_deadline` threading (`runtime.py:1408-1409`, `:1487-1489`). Replace the blanket `except Exception → stale` at `runtime.py:1441-1445` with: selector/wiki failures return the graph answer plus `wiki_selector_unavailable`, `wiki_pages` suppressed. Update `test_runtime_missing_wiki_domain_returns_complete_nonready_context` (`test_linking.py:1471`) to the new contract; genuine store failures keep their typed codes.

- [ ] **Step 4: Full verification, docs, PR**

`uv run pytest -q && uv run flake8 src tests` → green. Patch-bump. Wiki `concept/code-graph-context` (parity + warning semantics) — parent session. GWT scenario `codegraph-context-recovers-like-search` — parent session. Commit `fix(codegraph): give context the same rebuild budget and fail-soft wiki capture as search`, PR, remove worktree, HUMAN CHECKPOINT — merge.

### Task 5: S4 — Diagnosable invalid_config (spec R4)

**Files:**
- Modify: `src/iwiki_mcp/codegraph/config.py` (raise sites at `:62-110`, `:201-203` — attach `field`), `src/iwiki_mcp/codegraph/context.py:81-113` (attach `parameter`), `src/iwiki_mcp/codegraph/runtime.py:193-198,245-250,330-333,1300` and `src/iwiki_mcp/server.py:862-867` (thread the name through `sanitized_error`), non-ready answers normalized to carry `error` + `code` + `hint`.
- Test: `tests/codegraph/test_config.py`, `tests/codegraph/test_context.py`, `tests/codegraph/test_server_tools.py`

**Interfaces:**
- Produces: `CodeGraphConfigError.field: str | None`, `CodeGraphContextError.parameter: str | None`; error dicts `{"error": "code graph configuration is invalid", "code": "invalid_config", "field": "<name>", "hint": …}` where `<name>` comes only from the whitelist of known config fields / request parameters.

- [ ] **Step 1: Write the failing tests**

```python
def test_invalid_depth_names_the_parameter():
    error = validate_context_error(depth=1.0)
    assert error == {
        "error": "code graph configuration is invalid",
        "code": "invalid_config",
        "field": "depth",
        "hint": ANY_STRING,
    }

def test_unknown_config_key_names_the_field(tmp_path):
    error = load_config_error(tmp_path, extra_key="reed_mode")
    assert error["field"] == "reed_mode"
    assert "reed_mode" not in error["error"]     # name only in the field slot
```

Extend the existing sanitize tests (`test_server_tools.py:469,495`) to assert exception text still never leaks.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/codegraph/test_context.py tests/codegraph/test_config.py -k "names_the" -v`
Expected: FAIL — no `field` key today.

- [ ] **Step 3: Implement**

Attach the identifier at each raise site; `_configuration_error` stores the field name (`str | None`) instead of `True`; `sanitized_error` and `_invalid_config()`/`_invalid_code_config()` emit the `field` key only when the stored name is in the whitelist (config field names ∪ context parameter names). Normalize every non-ready query answer (`runtime.py` search/context non-ready branches) to always include `error`, `code`, and `hint` beside empty `results`/`nodes`.

- [ ] **Step 4: Full verification, docs, PR**

`uv run pytest -q && uv run flake8 src tests` → green. Patch-bump. Wiki `concept/code-graph-runtime` (fail-soft diagnostics) + README error-shape note if documented — parent session. Commit `fix(codegraph): name the offending field in invalid_config errors`, PR, remove worktree, HUMAN CHECKPOINT — merge.

### Task 6: S5 — Path-prefix normalization (spec R5)

**Files:**
- Modify: `src/iwiki_mcp/codegraph/query.py:186-196` (use the normalized value), `src/iwiki_mcp/postgres/codegraph.py:1065-1070` (same normalization before `starts_with`)
- Test: `tests/codegraph/test_query.py`, `tests/postgres/test_code_graph_reader.py`

**Interfaces:**
- Consumes: `module_key` (`models.py:68-74`) — validates and normalizes (drops `.` parts, collapses `//`).
- Produces: `ValidatedSearchRequest.path` always in `module_key(path.strip())` form; matching stays case-sensitive (documented contract).

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("raw", ["./services/a", "services//a", " services/a ", "services/a/"])
def test_path_prefix_variants_match_canonical(schema_v2_search_connection, raw):
    canonical = search(connection, query="crm", kinds=["module"], path="services/a")
    variant = search(connection, query="crm", kinds=["module"], path=raw)
    assert variant.results == canonical.results and canonical.results
```

Add canonical (non-alias) module and file branch coverage with `path=`, and mirror the parametrized test in `tests/postgres/test_code_graph_reader.py` against `starts_with`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/codegraph/test_query.py -k path_prefix_variants -v`
Expected: FAIL — variants return empty today.

- [ ] **Step 3: Implement**

In `query.py:186-196`: normalize through `module_key` on the stripped value (`module_key` rejects an empty result), and re-append the separator when the caller's raw value ended with one — a trailing slash is the only way to scope a prefix to exactly one directory, so `services/a/` selects that directory while `services/a` is the wider prefix that also admits `services/ab`. State that distinction in the docstring. A non-string path must still raise the typed query error. Apply the same normalization in the PostgreSQL reader before binding the `starts_with` parameter.

- [ ] **Step 4: Verification incl. PostgreSQL**

Run: `uv run pytest -q && uv run flake8 src tests` and the disposable-postgres block from Global Constraints.
Expected: all green, `tests/postgres` running (not skipping) against the live database.

- [ ] **Step 5: Docs, PR**

Patch-bump. Wiki `concept/code-graph-search` (normalization + case-sensitivity contract), README search-tool note if present — parent session. GWT scenario `codegraph-path-prefix-normalized` — parent session. Commit `fix(codegraph): normalize the search path prefix on both backends`, PR, remove worktree, HUMAN CHECKPOINT — merge.

### Task 7: S6 — read_mode routing (spec R6)

**Files:**
- Modify: `src/iwiki_mcp/codegraph/application.py:248-293` (reader selection), `src/iwiki_mcp/server.py:1810-1878` (search/context/status dispatch for local bindings), `src/iwiki_mcp/codegraph/mcp_adapter.py:271-300` (activate `McpCodeGraphReader`)
- Modify: `README.md`, `docs/README.ru.md` if present, `docs/iwiki-mcp-modes.md` (read_mode contract)
- Test: `tests/codegraph/test_application.py`, `tests/codegraph/test_mcp_adapter.py`, `tests/codegraph/test_server_tools.py`, `tests/postgres/test_code_graph_reader.py`

**Interfaces:**
- Consumes: `Config.read_mode` (`config.py:132`, default `"sqlite"`), `McpCodeGraphReader` (`mcp_adapter.py:271`), the PostgreSQL reader from `postgres/codegraph.py`, credentials `IWIKI_CODE_GRAPH_MCP_URL` / `IWIKI_CODE_GRAPH_MCP_TOKEN`, and `PostgresBinding.connection_dsn()` — the same DSN source `publish_mode = "postgres"` already validates against.
- Produces: `application.code_reader(bind, config)` — returns the runtime (sqlite), the direct PostgreSQL reader, or the MCP transit reader per `read_mode`; one mode, no fallback; missing DSN/credentials → `invalid_config` with `field: "read_mode"`. Hosted `PostgresBinding` dispatch unchanged. In `postgres`/`mcp` read modes no local auto-rebuild: freshness comes from the published snapshot (`stale_snapshot` semantics).

- [ ] **Step 1: Write the failing tests**

```python
def test_read_mode_sqlite_uses_local_runtime(local_env):
    reader = code_reader(local_env.binding, local_env.config(read_mode="sqlite"))
    assert isinstance(reader, CodeGraphRuntime)

def test_read_mode_mcp_without_credentials_names_read_mode(local_env, monkeypatch):
    monkeypatch.delenv("IWIKI_CODE_GRAPH_MCP_URL", raising=False)
    error = code_reader_error(local_env.binding, local_env.config(read_mode="mcp"))
    assert error["code"] == "invalid_config" and error["field"] == "read_mode"

def test_read_mode_mcp_routes_search_to_remote(local_env, fake_mcp_server):
    reader = code_reader(local_env.binding, local_env.config(read_mode="mcp"))
    result = reader.search("main", kinds=("module",))
    assert fake_mcp_server.received_search_call
```

Byte-identity guard: with `read_mode="sqlite"` the `wiki_code_search`/`wiki_code_context`/`wiki_code_status` responses on the existing server-tools fixtures are unchanged (reuse the fixtures of `test_server_tools.py`).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/codegraph/test_application.py -k read_mode -v`
Expected: FAIL — `code_reader` does not exist / routing ignores `read_mode`.

- [ ] **Step 3: Implement**

Add `code_reader` to `application.py`; switch `server.py` local-binding dispatch for the three read tools through it. `postgres` mode builds the direct reader from `PostgresBinding.connection_dsn()` and therefore requires that binding, which keeps `code_reader` total over the three modes rather than serving production dispatch (the server routes every `PostgresBinding` down the hosted path first); `mcp` mode builds `McpCodeGraphReader` from the transit credentials; both return the typed error when their prerequisite is absent; neither attempts a local rebuild (skip `query_guard` auto-rebuild; surface the published snapshot's staleness as-is).

- [ ] **Step 4: Verification incl. PostgreSQL**

Run: `uv run pytest -q && uv run flake8 src tests` and the disposable-postgres block.
Expected: green; `test_mcp_adapter.py` now covers the production construction path.

- [ ] **Step 5: Docs, PR**

Patch-bump. Update `README.md` (+ `docs/README.ru.md` if present) and `docs/iwiki-mcp-modes.md`: `read_mode` now routes reads, one mode, no fallback, error names the key. Wiki `concept/code-graph-configuration` + `concept/using-the-server` — parent session. GWT scenario `codegraph-read-mode-routing` — parent session. Commit `feat(codegraph): route code-graph reads by read_mode`, PR, remove worktree, HUMAN CHECKPOINT — merge.

### Task 8: Result reconciliation and closure

**Files:**
- No new source changes; runs in the parent session in the main checkout.

**Interfaces:**
- Consumes: all six merged PRs, the intent's Desired Outcomes, this plan.

- [ ] **Step 1: Acceptance run against a real snapshot**

On refreshed `master`: build a snapshot (`wiki_code_index` via the local server or the CLI), then demonstrate each Desired Outcome observably: `wiki_code_search(path=…)` prefix subset; `wiki_code_context` IMPORTS traversal for a module seed; two consecutive `wiki_code_status` calls identical; excluded-path edit + ordinary wiki write keep `ready`; `invalid_config` names its field; `read_mode` routing per mode. Record commands and outputs on the task page (parent session).

- [ ] **Step 2: Full suite including live PostgreSQL**

Run: `uv run pytest -q && uv run flake8 src tests` plus the disposable-postgres block.
Expected: green.

- [ ] **Step 3: /check-chain result**

Run `/check-chain result docs/superpowers/plans/2026-09-06-codegraph-context-path-filter-defects-plan.md` (non-empty diff base `--since=<pre-S1 master ref>`). On `OK`: task page lifecycle `done` per the ledger's fail-closed close rules.
