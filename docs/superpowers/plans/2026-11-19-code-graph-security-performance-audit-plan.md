---
topic: code-graph-security-performance-audit
stage: plan
chain:
  intent: docs/superpowers/intents/2026-11-19-code-graph-security-performance-audit-intent.md
  spec: docs/superpowers/specs/2026-11-19-code-graph-security-performance-audit-design.md
review:
  plan_hash: fe3276914f52b890
  last_run: 2026-11-19
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings: []
result_check:
  verdict: OK
  source: plan
  plan_hash: fe3276914f52b890
  last_run: 2026-11-19
  reviewed: true
  docs_checked: true
---
# Code graph job progress implementation plan

**Goal.** Let operators observe work completed by an authorized local job during parsing and publication, including outages at the selected snapshot reader.

**Architecture.** BuildControl records scalar progress. The process registry owns job identity, lifecycle and bounded terminal history. MCP reads the descriptor. The CLI renders updates for its exact invocation.

**Stack.** Python, threading, MCP stdio and Streamable HTTP, pytest, SQLite and PostgreSQL integration fixtures.

**Sources.** Read the [approved intent](../intents/2026-11-19-code-graph-security-performance-audit-intent.md) and [approved specification](../specs/2026-11-19-code-graph-security-performance-audit-design.md). The approved body hashes are 30f4ac48d9c08b4d and 02c772ce9afdd589 respectively.

## Execution and constraints

Use the existing topic branch. Recommend parent execution with superpowers:executing-plans because the tasks share job lifecycle code and require ordered integration. Obtain the user's checked-plan approval and execution choice before implementation. For each task, run the named failing case before changing behavior, then run the named regression set. Record actual outcomes separately from this plan.

Preserve deadline, cancellation, cache exclusion, publication reconciliation and CLI exit semantics. Keep all counters process-local. Expose only the fields allowed by the specification. Retain the existing language and storage matrix. Use disposable local fixtures for failure and integration checks. A need for durable job storage, a grant change or deployment reopens the relevant approval boundary.

## Task 1. Separate job ownership from cache occupancy

**Closes R1 scope.** Work in src/iwiki_mcp/codegraph/runtime.py and tests/codegraph/test_indexer_runtime.py. The current worker_domain_key identifies a base and primary. is_active and is_indexing also protect a shared destination during recovery and reads.

- [ ] Create two real temporary checkouts with the same base and primary. Hold the first build at a barrier. Request an identical build from the second checkout. Assert a busy result with no job descriptor. Release the first worker and repeat the foreign exact-handle lookup against terminal history.
- [ ] Retain the existing destination key for cache occupancy. Add a canonical checkout identity to the live job and its terminal record. Require both identities for joining, current/terminal selection, exact lookup and the per-owner history retention policy. Keep the global single-worker limit, worker liveness checks and shared cache lock intact.
- [ ] Test the four combinations of equal/different checkout and destination. Resolve equivalent path spellings to the same canonical checkout. Verify that normal joins within one owner still return one ID.

Representative assertions for the shared-destination fixture:

```python
assert foreign_start["code"] == "busy"
assert "job" not in foreign_start
assert same_owner_start["job"]["id"] == first_start["job"]["id"]
```

```bash
uv run pytest -q tests/codegraph/test_indexer_runtime.py
```

**Expected evidence.** The second checkout cannot join or inspect the first job. The destination stays busy for both checkouts during the write. Existing eviction, cancellation and stale-metadata recovery checks pass.

## Task 2. Capture a coherent build descriptor

**Closes R2 for local work.** Extend BuildControl and CodeGraphIndexer in src/iwiki_mcp/codegraph/indexer.py. Update _BuildJob, _JobSnapshot, attach_job and _rebuilding_job_answer in src/iwiki_mcp/codegraph/runtime.py. Use the owner scope from Task 1.

- [ ] In tests/codegraph/test_indexer_runtime.py, pause before discovery, after one accepted parse, and just before worker completion. Check both the index response and the exact job status. Check the ready-cache shortcut and a parser failure after one accepted file.
- [ ] Initialize queued progress with zero counts and Unix seconds. Set files_total when the parse list is known or a verified cache hit schedules zero files. Pass the control into both indexer parse paths. Increment files_parsed only after the adapter output passes its context checks.
- [ ] Capture progress and phase history under a short lock. Have describe() supply all public progress fields in one observation. Replace secondary reads of control.phase after describe(). Publish the frozen last phase and counters with terminal state. Keep the current terminal history bound and its scalar storage.
- [ ] Use the same snapshot path for a joined call, a detached index reply, a poll and a retained terminal reply. Test that polling leaves updated_at unchanged and preserves the established top-level phase conventions.

```python
assert live["job"]["progress"]["files_parsed"] == 1
assert live["job"]["progress"]["files_total"] == 2
assert terminal["job"]["progress"]["files_parsed"] == 1  # second parse failed
```

```bash
uv run pytest -q tests/codegraph/test_indexer_runtime.py tests/codegraph/test_runtime.py
```

**Expected evidence.** Unknown totals are omitted. A cache hit reports zero parsed and zero scheduled. No mixed live/terminal descriptor appears while a worker finishes. Polling performs no source read or graph mutation through the progress API.

## Task 3. Count confirmed transfer work

**Closes R2 for publication.** Connect the existing build control to the post-build publication callback in src/iwiki_mcp/codegraph/runtime.py and src/iwiki_mcp/codegraph/application.py. Update direct callback fixtures with the same calling convention. Keep publish_snapshot usable by direct callers with no observer.

- [ ] Extend tests/codegraph/test_application.py with a controlled publisher. Hold export, begin, batch and finalize in turn. Verify phase names, rejected acknowledgements, duplicate acknowledgements and equal ordinals across different row kinds.
- [ ] Enter export before export_snapshot, publish_begin before begin, transfer before sending batches and finalize before finalize. Count accepted canonical payload bytes with batch.byte_count. Use the accepted kind and ordinal within this session to avoid double counting. With the existing ordered iterator a per-kind last accepted ordinal suffices. Test this ordering assumption against iter_snapshot_batches.
- [ ] Keep the reconciliation branch for a lost finalize reply. Snapshot counters before terminal publication failure. Keep SQLite-only jobs at zero transferred bytes and batches. Retain no rows, wire payloads or session credentials in terminal records.

```python
assert progress["batches_acknowledged"] == 2  # files/0 and symbols/0
assert progress["bytes_acknowledged"] == files_batch.byte_count + symbols_batch.byte_count
```

```bash
uv run pytest -q tests/codegraph/test_application.py tests/codegraph/test_indexer_runtime.py
```

**Expected evidence.** The live worker reports acknowledged work while finalize is blocked. Failure preserves counts and the failed terminal state. Recovery of a completed finalize still returns ready.

## Task 4. Serve local progress through the registered MCP tool

**Closes R1 and the MCP portion of R4.** Use the scoped lookup from Task 1 and descriptors from Tasks 2–3. Change src/iwiki_mcp/server.py and src/iwiki_mcp/codegraph/runtime.py. Inspect normalization in codegraph/application.py and the closed reasons in codegraph/mcp_adapter.py when classifying combined responses.

- [ ] In tests/codegraph/test_server_tools.py add job_only to the registered input schema with default false. Through the MCP dispatcher request a known job with job_only=true while the configured remote reader is held at a barrier in another call. Count reader construction, status reads, indexing and publication by the job-only request. Require zero for all four.
- [ ] Resolve the binding and enforce applicable access and primary checks before job lookup. Require job_id in job-only mode. For a known local handle return job and any publication_failed warning. For an unknown, foreign or evicted handle return the same job_unknown warning. An omitted ID in job-only mode returns invalid_config with field job_id. The hosted branch must preserve binding policy and provenance without consulting a local worker or constructing a PostgreSQL graph reader.
- [ ] Keep ordinary combined reads on the selected adapter. Attach the local descriptor to a verified stale or missing snapshot, or a classified connect/timeout error with verified local scope. Classify by the structured code and reason, accounting for normalized messages. Require session provenance before attaching a job to hosted availability diagnostics. Verify the primary when the response names it. Preserve existing healthy combined responses. A transport-level outage may have no remote response. Treat authorization refusals, binding fallback or substitution, unsafe paths and unknown failures as ineligible. Preserve original error fields and warnings.
- [ ] Update only affected error-shape and schema assertions. Retain coverage for healthy snapshots, argument omission, old clients, foreign identities, owner changes and hosted binding failures. Exercise the real hosted tool with tests/postgres/test_code_graph_http.py and a disposable database.

```python
assert local_poll["job"]["id"] == handle
assert {"state", "fresh"}.isdisjoint(local_poll)
assert denied.get("job") is None
```

```bash
uv run pytest -q tests/codegraph/test_server_tools.py tests/codegraph/test_mcp_adapter.py tests/test_http_unit.py
uv run pytest -q -rs tests/postgres/test_code_graph_http.py
```

**Expected evidence.** The local poll completes before releasing the blocked reader. Combined status retains its snapshot diagnostics and independently reports authorized local work. All protected cases suppress job metadata.

## Task 5. Render CLI progress without blocking the worker

**Closes R3.** Route a private optional progress observer from src/iwiki_mcp/admin.py through publish_project and index_and_publish in src/iwiki_mcp/codegraph/application.py to the exact job returned by registry.start in src/iwiki_mcp/codegraph/runtime.py. Preserve default call behavior for callers without an observer.

- [ ] Add a pipe-based test in tests/test_code_publish_cli.py. Start the actual CLI dispatch with an event-controlled build. Read a flushed line while the worker is held, release it, then parse the final stdout. Cover text and JSON, success, publication failure and configuration failure before worker creation.
- [ ] Attach the observer to the exact started or joined job. Account for a worker that finishes before subscription. Capture startup, phase transitions and the terminal descriptor with bounded in-memory records; coalesce same-phase counter updates. Retain phase transitions in order for a draining sink, including fast transitions between two reads. Bound a slow subscriber to phase-boundary records and the latest counters. Keep stream writes on the consumer side. Keep the worker independent of stderr speed and the caller's wait deadline.
- [ ] Emit startup, phase boundaries and termination promptly on a draining sink. In the rate-limit test, inject a fake monotonic clock. Use a monotonic clock to emit changed counters at most once per second inside a phase. Flush each emitted line. Suppress only progress output failures. Detach on every caller exit, including early return, without manufacturing a terminal event. Exercise a held sink with a releasable barrier and check that worker completion proceeds independently.
- [ ] Update stderr assertions for runs that start a job in tests/test_code_publish_cli.py, tests/codegraph/test_publish_cli_sqlite.py, tests/postgres/test_code_publish_cli.py and tests/postgres/test_code_publish_cli_mcp.py. Preserve pre-start failure diagnostics, exact final JSON, exit codes, redaction checks and text result assertions. Check CLI output with a non-TTY stream.

```python
assert first_line and not worker_released.is_set()
assert json.loads(stdout)["state"] == "ready"
assert exit_code == 0
```

```bash
uv run pytest -q tests/test_code_publish_cli.py tests/codegraph/test_publish_cli_sqlite.py
uv run pytest -q -rs tests/postgres/test_code_publish_cli.py tests/postgres/test_code_publish_cli_mcp.py
```

**Expected evidence.** One final JSON object reaches stdout. A fresh job produces intermediate stderr before release. Same-phase output obeys the clock bound. Runtime/configuration failures retain exit codes 1/2. Broken sinks, joined jobs and early returns do not change publication outcomes.

## Task 6. Verify and document the public contract

**Closes R4 and reconciles R1–R3.** Run after Tasks 1–5 on the same final code fingerprint.

- [ ] Update docs/code-graph.md, docs/code-graph-publishing.md and docs/tools-reference.md with their .ru.md counterparts. Include a copyable local job poll, example progress, a stale snapshot case and the CLI stream contract. Identify the originating-process lifetime in both languages. Synchronize the package version and lockfile under the repository patch-version policy.
- [ ] Update the bound Wiki through MCP. Search existing scenarios and inspect applicable scenario context. Preserve the existing wait and detached-publication scenarios. Add GWT coverage linked to the actual implementation and executable tests for isolated polling, progress counts and CLI output. Apply the configured graph publication and binding-resolution procedure after source changes. Record graph_unavailable when resolution is unavailable; record test evidence independently.
- [ ] Follow CLAUDE.md and docs/postgres-ci.md to supply a disposable pgvector test database. Use a container-reachable test host for deployment fixtures when needed. Check that the relevant PostgreSQL cases execute. Record environmental skips explicitly.
- [ ] Run syntax validation and the full suite once on the final stable fingerprint after focused checks pass. Record commands, exit codes, Git revision, uncommitted diff fingerprint if applicable, relevant skips and observable outcomes. Run Wiki lint and reconcile against this plan with check-chain result before branch finishing.

```bash
uv run python -m compileall -q src/iwiki_mcp
git diff --check
uv run pytest -q -rs
```

**Expected evidence.** A held-reader demonstration returns job progress, an accepted-batch demonstration reports exact bytes, and a pipe demonstration shows progress before the final result. Link the three observations and the regression results to the topic. A skipped integration case cannot establish its result.
