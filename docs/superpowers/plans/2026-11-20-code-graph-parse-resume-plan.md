---
topic: code-graph-parse-resume
stage: plan
status: approved
chain:
  intent: docs/superpowers/intents/2026-11-20-code-graph-parse-resume-intent.md
  spec: docs/superpowers/specs/2026-11-20-code-graph-parse-resume-design.md
review:
  plan_hash: 9732726d43539949
  last_run: 2026-11-20
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
  plan_hash: 9732726d43539949
  last_run: 2026-11-21
  reviewed: true
  docs_checked: true
---
# File-level code graph recovery implementation plan

**Goal.** Restart a stopped local build, reuse verified parse results, and produce the same graph as a clean build of the final inputs. A previously verified snapshot must remain recoverable after a parsing crash.

**Architecture.** Keep parse records private to the checkout and domain. Integrate their read/write lifecycle into the existing writer-locked build. Keep full resolution, publication, and the stage 2 upload pin on their current paths. Use Python, the current language adapters, canonical JSON, POSIX directory operations and pytest.

**Authority.** Read the [approved specification](../specs/2026-11-20-code-graph-parse-resume-design.md) and [intent](../intents/2026-11-20-code-graph-parse-resume-intent.md). Spec body hash `83d405e7e56b91d7`. The baseline release is 0.7.322 at `c9fdad7`. These steps describe future work. Implementation and deployment require the execution decision after plan review.

## Global constraints

Support configured Python, TypeScript, JavaScript and Bash. Preserve readiness checks, process-local job IDs, stdout/stderr contracts and authorization. Each encoded record is at most 16 MiB. Oversized results and adapters without an explicit codec use the ordinary parse path. No hosted schema change or new CLI flag. Never read parse records from the hosted service. Run executable examples only on a disposable checkout or database unless executing the authorized rollout.

Steps 1 and 2 can develop independently against the record envelope below. Step 3 joins them. Step 4 owns reader recovery and shares indexer.py with step 3, so integrate those edits sequentially. Step 5 verifies all public routes. Step 6 measures and releases the resulting build. Use subagent-driven development for an approved in-session execution or executing-plans for a separate session. Run each named regression red against the missing or incorrect behavior, implement it, rerun narrowly, and commit the verified change.

## 1. Preserve everything the resolver consumes (R4, R5)

- [ ] Create `src/iwiki_mcp/codegraph/parse_codec.py` and `tests/codegraph/test_parse_codec.py`. Use the record types in `models.py` and the actual parsed variants produced by `languages/python.py`, `typescript.py`, `javascript.py` and `bash.py` under the same languages directory. Capture successful and warning-bearing results, including the Python base variant on parser errors. Include an unknown adapter using a supported extension.
- [ ] Produce a closed encode/decode pair for known types. Store file, symbols, references, warnings and adapter-private evidence. Validate fields, exact scalar types, nullable values, array shapes and variant before constructing records. Reconstitute ordered tuples, including duplicate warnings. Reject unexpected variants and inconsistent embedded file identity. No dynamic type imports or executable serialization.
- [ ] Expose per-file parse context from adapters that opt in. Obtain the effective Python module name from the adapter already bound to this discovery set; include an explicit absent value. Use normalized path/extension for the other built-ins. Bind format, checkout/domain, path, content hash, size, language, parser/grammar/adapter and normalization/Unicode versions, plus parse-affecting options. Changes limited to Git revision, Wiki selectors or unrelated files must leave compatible records reusable. TS boost executes during fresh resolution.
- [ ] Round-trip each fixture and compare its complete records and the resolved relations/warnings to a fresh parse. Add/remove `pkg/__init__.py` around byte-identical `pkg/mod.py` and assert an identity miss. Include the cross-file Python class mutation that depends on `_adapter_evidence`. Require one module-map derivation per Python binding, independent of hit count.

```bash
uv run pytest -q tests/codegraph/test_parse_codec.py tests/codegraph/test_python_adapter.py tests/codegraph/test_mixed_language_indexing.py
```

Pass when every supported fixture round-trips, parser/normalizer/context drift changes identity, and undeclared adapters bypass the codec.

## 2. Make one file durable and private (R3, R4, R6)

- [ ] Create `src/iwiki_mcp/codegraph/parse_checkpoint.py` and `tests/codegraph/test_parse_checkpoint.py`. Build on the directory-descriptor conventions in `location.py` and the checked file operations in `checkpoint.py`. Keep parse filenames and cleanup independent of upload manifests. Proposed internal contract: open one scoped directory under the caller's writer lock; load by path digest and expected identity; save a validated encoded result; prune owned obsolete paths; clear owned entries after verified success.
- [ ] Store one versioned envelope with identity, payload and digest using `canonical.py`. Verify ownership, regular file type, permissions, link count and length on the opened descriptor. Bound reads to the ceiling before decoding. Treat damaged owned content, old format and an identity mismatch as misses. Reject unsafe directory components or unsafe entries before access. Count the complete encoded envelope for the size limit. Test sizes just below, exactly at and one byte above 16 MiB. Oversized valid parse output remains available in memory for this build.
- [ ] Write via an owner-only temporary file in the same directory. Flush and fsync the file, replace the record, then fsync the directory. Sync newly created parent entries. Kill a child before and after replacement and directory sync. On restart, require either a completely verified record or a miss. On a failed write or fsync stop with a sanitized storage error, preserve previously durable records and release the lock.
- [ ] Test file/directory symlinks, multiply linked or foreign-owned files where supported, two checkout scopes with the same domain, two domains, unknown filenames, abandoned temporary files and a neighboring upload pin. Prune only records owned by this scope. Assert a rejected or interrupted operation leaves neighboring contents byte-identical.

```bash
uv run pytest -q tests/codegraph/test_parse_checkpoint.py tests/codegraph/test_resume_checkpoint.py
```

Pass when a new process opens each valid committed record, corruption cannot produce a hit and paths outside the selected directory are untouched.

## 3. Resume under the existing graph writer (R1–R6)

- [ ] Extend `CodeGraphIndexer.build` and `_parse` in `indexer.py`. The optional checkpoint is absent for `build_rows`; verify that the pure path touches no recovery files. Preserve the existing ready/no-op branch. Only a verified ready result may trigger leftover cleanup on that branch. Prune removed paths on a required build after fresh discovery and under the existing writer lock.
- [ ] Bind adapters once, retain discovery order and apply its current limits and exclusions. Use the same captured bytes for a miss and clean parse. At each file boundary check cancellation and the existing deadline, including for hits. Use the existing six-field parser-record validation on both paths. A fresh eligible result becomes progress only after durable save. Count a validated hit or a deliberately uncached result once. A durability failure cannot acknowledge that file.

```text
record = lookup(expected identity) only when recovery is supported and force is false
on miss: parse the captured file; validate its result; persist if eligible
on hit: validate the restored result against the same captured file
append the accepted result in discovery order; advance progress once
```

- [ ] Add tests where deadlines expire on a run consisting entirely of hits, and where changing language selection or discovery exclusions removes previously cached paths. Apply force to the entire invocation; serve no saved hits and replace individual records only on commit. After interruption, a normal build needing reconstruction may use its completed records. Leave project-wide SymbolIndex creation, resolution, Wiki linking, input verification and atomic activation downstream of the complete current file list. Drop deleted and renamed-away files from all inputs.
- [ ] Clean only after final local verification, including selector checks, while still holding the writer lock. A remote transfer failure cannot reinstate parse records. For post-activation cleanup I/O failure, keep the successful result, emit a bounded maintenance warning, and retry only on a subsequent verified no-op or superseding build. Exercise both live-writer contention and lock release on process death. Preserve existing timeout and job join behavior.

```bash
uv run pytest -q tests/codegraph/test_indexer_runtime.py tests/codegraph/test_mixed_language_indexing.py tests/codegraph/test_resume_application.py
```

Add hit/miss/force/no-op assertions to these suites. Pass when parser calls, accepted counts, cleanup timing, pin precedence and output revision follow the specification.

## 4. Let the ordinary reader recover the previous revision (R1, R3, R5)

- [ ] In `tests/codegraph/test_indexer_runtime.py`, seed a verified graph, kill an explicit forced build while parsing unchanged source, then issue public local status/search with automatic rebuilding off. Read the previous ready revision before any new build. Repeat with changed source and changed Wiki selectors. Require the existing dirty/stale contract. Start a live writer and verify that readers still observe rebuilding.
- [ ] Use a distinguishable parsing-phase transition or equivalent durable proof so a process death after parsing cannot be mistaken for a safe parsing interruption. Extend the building transition in `indexer.py` and the abandoned-writer recovery in `runtime.py`. Preserve the previous verified readiness evidence before entering parsing. After writer death, reacquire the existing writer lock, reread the transition and revalidate scope, generation, revision, canonical store identity, source, selectors and integrity under the existing lock order. When valid, restore a readable, verified prior envelope. Preserve safety for older envelopes lacking proof and for provisional or replaced-but-unverified output. A retry may reconstruct from source.
- [ ] Inject interruption before replacement, after provisional replacement and before final verification. Require ordinary readers to reject unverified output. Assert status/search/context never open the parse cache. Test previous-ready, dirty and initially missing cases. The revision digest alone is insufficient evidence of readiness. Extend tests/codegraph/test_recovery_concurrency.py with an occupied Wiki selector lock and an abandoned parsing transition. Verify bounded return or retry, preservation of the live writer response, and no blocking Wiki lock acquisition while holding the graph writer lock. Revalidate the generation if locks must be released and reacquired.

```bash
uv run pytest -q tests/codegraph/test_indexer_runtime.py tests/codegraph/test_runtime.py tests/codegraph/test_recovery_concurrency.py
```

Pass when the unchanged-input reader succeeds before a rebuild, changed inputs do not become fresh, and publication-state regressions remain green.

## 5. Prove recovery through operator entry points (R1–R7)

- [ ] Create `tests/codegraph/test_parse_resume_process.py`. Use the disposable Git/SQLite project from `tests/codegraph/synthetic_wiki.py`. Launch the real CLI in separate processes. Instrument adapter calls and terminate the first worker immediately after the fourth durable file acknowledgement. Verify the expected crash code. Between runs change an accepted file, remove one, rename one, add another, and change a cross-file import target.
- [ ] Compare the restart with a fresh build of the same final checkout, commit, configuration and Wiki. Isolate caches; use the same logical repository identity. Assert zero parse calls for compatible durable hits, one for each miss, absence of old paths and no stale relation targets. Compare normalized tables, warnings and canonical payload revision; omit clocks and diagnostics timings. Run a mixed-language fixture for Python, TS, JS and Bash. Include the byte-identical Python package-marker case and mutation evidence from step 1.
- [ ] Exercise a separate real stdio MCP process and poll the original job with job_only on the same binding. After process death the former job ID cannot report completion; start a new job and check its own counters and revision. Verify the CLI's final JSON and stderr progress. Use the hosted PostgreSQL fixtures for a stage 2 pin with a changed checkout: pin replay must perform zero parsing and must not republish a later revision.

```bash
uv run pytest -q tests/codegraph/test_parse_resume_process.py tests/codegraph/test_server_tools.py tests/test_code_publish_cli.py
uv run pytest -q -rs tests/postgres/test_code_resume_process.py
```

The PostgreSQL command needs a disposable pgvector database and must execute all four process-loss cases without skips.

## 6. Measure cost, reconcile the result and release (R7)

- [ ] Extend the existing `eval/code_graph/runner.py` measurement path or its fixture driver to capture adapter calls by path, accepted file count, parsing and total wall time, and peak scratch bytes including temporary files. The existing measure_publication calls build(force=True); retain it for the scale check. Drive restart measurements through an ordinary new CLI process. Confirm that the benchmark adapter actually opts into checkpointing before reporting savings. Use isolated caches, the same filesystem and identical checked-out inputs for release 0.7.322 and the candidate. Run three or more interleaved repetitions each of cold, interrupted/restarted and independent clean builds. Record median values, workload, storage and revisions. Investigate a cold median slowdown above 10% before release. Equality and zero redundant parses remain mandatory even when resolution dominates wall time.
- [ ] Update EN/RU graph and publication docs, release metadata, and the Wiki GWT scenarios with implementation and test bindings. Check job-vs-snapshot semantics, `force`, cleanup, and the stage 2 pin in client instructions and the framework runbook. Update affected instructions and run their contract checks. The hosting service has no checkout. Demonstrate parse recovery on the publishing machine.
- [ ] Run focused tests during development, flake8, compileall and the full suite with disposable pgvector, the dedicated loopback PostgreSQL fixture, and Docker available. The full suite includes the 20,000-file test. If run separately, deselect it from the other shard and total both exit statuses. Compare its timing with 0.7.322 using matching generator, instrumentation and settings; run the scale case once on final candidate inputs. Attach checks, state fingerprints, metrics and any skip reasons to the task ledger. Before branch finishing, review the aggregate diff and run the required result check against the plan. Record the verified implementation, test and disposable rehearsal evidence, and explicitly mark production acceptance pending. Keep the task open through the release.

```bash
uv run flake8 src tests
uv run python -m compileall -q src/iwiki_mcp
uv run pytest -q -rs
git diff --check
```

- [ ] Complete the disposable recovery and rollback rehearsal before the pre-merge result check. At authorized release, merge the verified change via PR, pin the tested tree on framework, retain the running image and follow the runbook appropriate to the actual schema diff. Verify installed package version, service health, watchdog, authenticated bound MCP and active snapshot. Compare the retained active revision before/after rollout without concurrent publication. Record the publisher restart comparison, deployed revision and release acceptance in the ledger. Close the implementation task and its intent outcomes only after live release evidence is available. If rehearsal or acceptance fails, use the verified rollback path and preserve the failure evidence.

Pass when the final tests and measurements support R1–R7 and the deployed revision passes its health and compatibility checks. Recording a proposed plan does not authorize a release.
