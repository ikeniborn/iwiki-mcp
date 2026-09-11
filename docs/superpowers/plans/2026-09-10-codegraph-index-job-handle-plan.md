---
topic: codegraph-index-job-handle
stage: plan
chain:
  intent: docs/superpowers/intents/2026-09-10-codegraph-index-job-handle-intent.md
  spec: docs/superpowers/specs/2026-09-10-codegraph-index-job-handle-design.md
result_check:
  verdict: OK
  plan_hash: 626785116a8d4a75
  last_run: 2026-09-11
review:
  plan_hash: 626785116a8d4a75
  last_run: 2026-09-10
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
  findings:
    - id: F-001
      phase: coverage
      severity: WARNING
      section: Task 1
      section_hash: ab76e33ea0ba5c26
      fragment: "### Task 1: The job becomes a value in the worker registry"
      text: "No task named the spec requirement it implements, so the R1-R8 coverage held
        only in the author's head and could not be checked mechanically."
      fix: "Every task now opens with a `**Spec:**` line naming its requirements; R1-R8 are
        each claimed by exactly one task."
      verdict: fixed
      verdict_at: 2026-09-10
    - id: F-002
      phase: verifiability
      severity: WARNING
      section: Task 8
      section_hash: null
      fragment: "Update `concept/code-graph-runtime`'s \"Full-build lifecycle\" section"
      text: "The wiki update step had no observable expected result, so it could be called
        done without evidence."
      fix: "Added an expected result: a new page revision and the section re-read showing
        the three cancellation points and the job descriptor."
      verdict: fixed
      verdict_at: 2026-09-10
    - id: F-003
      phase: dependencies
      severity: INFO
      section: Task 6
      section_hash: null
      fragment: "Add the flag to `_BuildJob.__init__` as `explicit: bool = False`"
      text: "Task 6 extends a class introduced in Task 1 rather than Task 1 defining the
        field up front."
      fix: "Accepted: the flag only has meaning once the idle predicate exists, and Task 6
        states the edit explicitly."
      verdict: accepted
      verdict_at: 2026-09-10
---
# Code graph index job handle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `wiki_code_index` hands back a job descriptor instead of cancelling a build the caller could not wait for, and `wiki_code_status` reports that job's progress and outcome.

**Architecture:** The process already owns exactly one background build worker. This plan turns the worker's job into a value with an identity, splits the caller's wait from the build's own deadline, and stops the wait from cancelling. Progress comes from a phase marker the build sets where it already stamps phase timestamps. No new module, no new tool, no persistent store.

**Tech Stack:** Python 3.11, `threading` worker registry, FastMCP tool surface, pytest, flake8 (max-line-length 100).

**Spec:** `docs/superpowers/specs/2026-09-10-codegraph-index-job-handle-design.md`

## Global Constraints

- No new MCP tool: the registered surface stays at 35 tools (`tests/codegraph/test_server_tools.py` asserts the set).
- No persistent job store: job state lives in process memory and existing snapshot metadata only.
- `iwiki-mcp code publish` keeps its synchronous behavior and its exit codes 0 / 1 / 2.
- Publication atomicity is untouched: replace → provisional `rebuilding` → verification #1 → `ready` → verification #2, under the writer lock.
- Exactly one build worker per process; `is_active` / `active_count` stay driven by `thread.is_alive()`.
- flake8 clean on every touched file; line length ≤ 100.
- Rebuild speed on this repository stays ≤ 100 s end to end and ≤ 5 s in the publication phase.

---

### Task 1: The job becomes a value in the worker registry

**Spec:** R1

**Files:**
- Modify: `src/iwiki_mcp/codegraph/runtime.py:84-98` (`_BuildJob`)
- Modify: `src/iwiki_mcp/codegraph/runtime.py:99-165` (`_BuildWorkerRegistry`)
- Test: `tests/codegraph/test_indexer_runtime.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `_BuildJob(domain_key, *, force, languages)` with attributes `job_id: str`, `started_at: float`, `finished_at: float | None`, `state: str` (`"running" | "ready" | "failed" | "cancelled"`), `force: bool`, `languages: tuple[str, ...] | None`, `control`, `result`, `thread`; `_BuildJob.describe() -> dict[str, object]`; `_BuildWorkerRegistry.start(domain_key, target, *, force, languages) -> _BuildJob | None`; `_BuildWorkerRegistry.current(domain_key) -> _BuildJob | None`; `_BuildWorkerRegistry.finish(job, state)`.

- [ ] **Step 1: Write the failing test**

```python
def test_registry_keeps_the_last_terminal_job(monkeypatch):
    from iwiki_mcp.codegraph import runtime as runtime_module

    registry = runtime_module._BuildWorkerRegistry()
    key = ("/tmp/base", "docs")

    def target(control, result):
        result["state"] = "ready"

    job = registry.start(key, target, force=True, languages=None)
    job.thread.join(timeout=5)
    registry.finish(job, "ready")

    remembered = registry.current(key)
    assert remembered is job
    assert remembered.state == "ready"
    assert remembered.finished_at is not None
    assert registry.is_active(key) is False
    assert registry.active_count == 0
    described = remembered.describe()
    assert described["id"] == job.job_id
    assert described["state"] == "ready"
    assert len(described["id"]) == 16
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/codegraph/test_indexer_runtime.py::test_registry_keeps_the_last_terminal_job -v`
Expected: FAIL with `TypeError: start() got an unexpected keyword argument 'force'`

- [ ] **Step 3: Write minimal implementation**

```python
class _BuildJob:
    def __init__(
        self,
        domain_key: tuple[str, str],
        *,
        force: bool,
        languages: list[str] | None,
    ) -> None:
        self.domain_key = domain_key
        self.job_id = secrets.token_hex(8)
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.state = "running"
        self.force = force
        self.languages = None if languages is None else tuple(languages)
        self.control = BuildControl()
        self.result: dict[str, object] = {}
        self.thread: threading.Thread | None = None

    def matches(self, *, force: bool, languages: list[str] | None) -> bool:
        """Report whether a new request asks for the work this job is doing."""
        requested = None if languages is None else tuple(languages)
        return self.force == force and self.languages == requested

    def describe(self) -> dict[str, object]:
        """Return the caller-visible descriptor for this job."""
        described: dict[str, object] = {
            "id": self.job_id,
            "state": self.state,
            "started_at": self.started_at,
        }
        if self.finished_at is not None:
            described["finished_at"] = self.finished_at
        return described
```

In `_BuildWorkerRegistry`, replace `start`'s job construction and keep the slot after
completion:

```python
    def start(self, domain_key, target, *, force=False, languages=None):
        with self._lock:
            if (
                self._job is not None
                and self._job.thread is not None
                and self._job.thread.is_alive()
            ):
                return None
            job = _BuildJob(domain_key, force=force, languages=languages)
            # ... unchanged thread creation and start ...
            return job

    def current(self, domain_key):
        """Return the live or last terminal job for this domain, if any."""
        with self._lock:
            job = self._job
            if job is None or job.domain_key != domain_key:
                return None
            return job

    def finish(self, job, state: str) -> None:
        """Record a terminal state without dropping the job from the slot."""
        with self._lock:
            if job.state == "running":
                job.state = state
                job.finished_at = time.time()
```

`release()` keeps its signature but no longer sets `self._job = None`; it becomes a no-op
kept for call-site compatibility until Task 3 removes its callers.

Add `import secrets` to the module imports if it is absent.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/codegraph/test_indexer_runtime.py::test_registry_keeps_the_last_terminal_job -v`
Expected: PASS

- [ ] **Step 5: Run the neighbouring suite for regressions**

Run: `uv run pytest -q tests/codegraph/test_indexer_runtime.py`
Expected: PASS, no new failures (the three-caller busy test at line 2128 must stay green)

- [ ] **Step 6: Commit**

```bash
git add src/iwiki_mcp/codegraph/runtime.py tests/codegraph/test_indexer_runtime.py
git commit -m "feat(codegraph): give the build worker's job an identity and a terminal state"
```

---

### Task 2: The build reports the phase it is in

**Spec:** R3

**Files:**
- Modify: `src/iwiki_mcp/codegraph/indexer.py:200-231` (`BuildControl`)
- Modify: `src/iwiki_mcp/codegraph/indexer.py:1513` and every `phase = time.monotonic()` site inside `build`
- Test: `tests/codegraph/test_indexer_runtime.py`

**Interfaces:**
- Consumes: `_BuildJob.control` from Task 1.
- Produces: `BuildControl.enter_phase(name: str) -> float` (returns the phase start timestamp, so `phase = control.enter_phase("parsing")` replaces `phase = time.monotonic()`), `BuildControl.phase: str | None`, `BuildControl.phases_done: tuple[str, ...]`.

- [ ] **Step 1: Write the failing test**

```python
def test_build_control_reports_phases_including_zero_millisecond_ones():
    from iwiki_mcp.codegraph.indexer import BuildControl

    control = BuildControl()
    assert control.phase is None
    assert control.phases_done == ()

    control.enter_phase("discovery")
    assert control.phase == "discovery"

    control.enter_phase("normalization")
    control.enter_phase("resolution")
    assert control.phase == "resolution"
    assert control.phases_done == ("discovery", "normalization")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/codegraph/test_indexer_runtime.py::test_build_control_reports_phases_including_zero_millisecond_ones -v`
Expected: FAIL with `AttributeError: 'BuildControl' object has no attribute 'phase'`

- [ ] **Step 3: Write minimal implementation**

```python
class BuildControl:
    """Linearize caller cancellation against atomic publication entry."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self.publication_entered = threading.Event()
        self._publication_gate = threading.Lock()
        self._phase: str | None = None
        self._phases_done: list[str] = []

    @property
    def phase(self) -> str | None:
        return self._phase

    @property
    def phases_done(self) -> tuple[str, ...]:
        return tuple(self._phases_done)

    def enter_phase(self, name: str) -> float:
        """Mark the phase the build is entering and return its start time.

        The phase marker exists because `phase_timings_ms` cannot answer this:
        `_elapsed_ms` rounds to whole milliseconds over a map pre-seeded with
        zeros, so a fast phase is indistinguishable from one never run.
        """
        if self._phase is not None:
            self._phases_done.append(self._phase)
        self._phase = name
        return time.monotonic()
```

Then, inside `CodeGraphIndexer.build`, replace each `phase = time.monotonic()` with the
matching marker call, keeping the existing timing assignment untouched:

```python
            phase = _enter(control, "discovery")
            discovered = discover_sources(...)
            timings["discovery"] = _elapsed_ms(phase)
```

with a module-level helper so a `None` control keeps working:

```python
def _enter(control: BuildControl | None, name: str) -> float:
    """Stamp the phase start, recording it on the control when there is one."""
    if control is None:
        return time.monotonic()
    return control.enter_phase(name)
```

Apply `_enter` at the sites that own a phase name in `build`: `discovery`, `fingerprint`,
`parsing`, `normalization`, `resolution`, `persistence`, `validation`,
`canonical_verification_1`, `final_verification`, `publication`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/codegraph/test_indexer_runtime.py::test_build_control_reports_phases_including_zero_millisecond_ones -v`
Expected: PASS

- [ ] **Step 5: Prove the marker survives a real build**

```python
def test_real_build_records_publication_as_its_last_phase(seed_runtime):
    from iwiki_mcp.codegraph.indexer import BuildControl

    control = BuildControl()
    built = seed_runtime.runtime._indexer.build(force=True, control=control)

    assert built["state"] == "ready"
    assert control.phase == "publication"
    assert "discovery" in control.phases_done
    assert "normalization" in control.phases_done
```

Run: `uv run pytest tests/codegraph/test_indexer_runtime.py -k phase -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/iwiki_mcp/codegraph/indexer.py tests/codegraph/test_indexer_runtime.py
git commit -m "feat(codegraph): record the build phase on the control object"
```

---

### Task 3: The caller's wait stops cancelling the build

**Spec:** R2, R4, R8

**Files:**
- Modify: `src/iwiki_mcp/codegraph/runtime.py:1037-1100` (`_index_with_deadline`)
- Modify: `src/iwiki_mcp/codegraph/runtime.py:1102-1127` (`index`)
- Modify: `src/iwiki_mcp/codegraph/runtime.py:1205-1233` (`query_guard`'s call site)
- Test: `tests/codegraph/test_indexer_runtime.py`

**Interfaces:**
- Consumes: `_BuildWorkerRegistry.start(..., force=, languages=)`, `current`, `finish`, `_BuildJob.describe` (Task 1); `BuildControl.phase` / `phases_done` (Task 2).
- Produces: `CodeGraphRuntime.index(*, force=False, languages=None, wait_seconds=None) -> dict`; `CodeGraphRuntime._index_with_deadline(*, force, languages, build_deadline, wait_deadline, restore_prior_on_abort, cancel_on_wait) -> dict`; module constant `_INDEX_GRACE_SECONDS = 0.5`; `_rebuilding_job_answer(job) -> dict`.

- [ ] **Step 1: Write the failing test**

```python
def test_wait_expiry_returns_the_job_and_the_build_still_reaches_ready(
    seed_runtime, monkeypatch
):
    runtime = seed_runtime
    store = runtime.runtime._indexer.store
    real_publish = store.publish_metadata

    def slow_publish(*args, **kwargs):
        time.sleep(1.05)
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(store, "publish_metadata", slow_publish)

    answer = runtime.index(force=True, wait_seconds=0)

    assert answer["state"] == "rebuilding"
    assert answer["job"]["state"] == "running"
    assert len(answer["job"]["id"]) == 16
    assert "error" not in answer

    runtime.runtime.join_workers(timeout=10)
    assert runtime.status()["state"] == "ready"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/codegraph/test_indexer_runtime.py::test_wait_expiry_returns_the_job_and_the_build_still_reaches_ready -v`
Expected: FAIL with `TypeError: index() got an unexpected keyword argument 'wait_seconds'`

- [ ] **Step 3: Write minimal implementation**

```python
_INDEX_GRACE_SECONDS = 0.5


def _rebuilding_job_answer(job) -> dict[str, object]:
    """Answer a caller whose wait ended while its build kept running."""
    descriptor = job.describe()
    descriptor["phase"] = job.control.phase
    descriptor["phases_done"] = list(job.control.phases_done)
    return {
        "state": "rebuilding",
        "fresh": False,
        "job": descriptor,
        "hint": "poll wiki_code_status for this job",
    }
```

`_index_with_deadline` takes two deadlines and an explicit cancellation policy:

```python
    def _index_with_deadline(
        self,
        *,
        force: bool,
        languages: list[str] | None,
        build_deadline: float,
        wait_deadline: float,
        restore_prior_on_abort: bool,
        cancel_on_wait: bool,
    ) -> dict[str, object]:
        assert self._indexer is not None
        if time.monotonic() >= build_deadline:
            return self._busy_response()

        def run_build(control, result):
            ...  # unchanged body, with `deadline=build_deadline`

        try:
            job = _BUILD_WORKERS.start(
                self._worker_domain_key,
                run_build,
                force=force,
                languages=languages,
            )
        except Exception:
            return _rebuild_failed()
        if job is None or job.thread is None:
            return self._busy_response()
        job.thread.join(max(0.0, wait_deadline - time.monotonic()))
        if job.thread.is_alive():
            if cancel_on_wait:
                job.control.cancel()
                LOGGER.info("code_graph_build code=busy")
                return self._busy_response()
            LOGGER.info("code_graph_build code=rebuilding job=%s", job.job_id)
            return _rebuilding_job_answer(job)
        _BUILD_WORKERS.finish(
            job, "ready" if job.result.get("state") == "ready" else "failed"
        )
        return job.result or _rebuild_failed()
```

`index` splits the two clocks and enforces the grace:

```python
    def index(
        self,
        *,
        force: bool = False,
        languages: list[str] | None = None,
        wait_seconds: float | None = None,
    ) -> dict[str, object]:
        if languages is not None and (
            not languages
            or any(language not in KNOWN_LANGUAGES for language in languages)
        ):
            return _invalid_config()
        unavailable = self._unavailable()
        if unavailable is not None:
            return unavailable
        assert self._indexer is not None and self.config is not None
        full_rebuild_seconds = (
            self.config.max_full_rebuild_seconds
            or self.config.max_rebuild_seconds
        )
        if wait_seconds is not None and (
            wait_seconds < 0 or wait_seconds > full_rebuild_seconds
        ):
            raise CodeGraphQueryError(
                "wait_seconds must be between 0 and "
                f"{full_rebuild_seconds}"
            )
        started = time.monotonic()
        build_deadline = started + full_rebuild_seconds
        wait_budget = (
            full_rebuild_seconds
            if wait_seconds is None
            else max(float(wait_seconds), _INDEX_GRACE_SECONDS)
        )
        return self._index_with_deadline(
            force=force,
            languages=languages,
            build_deadline=build_deadline,
            wait_deadline=started + wait_budget,
            restore_prior_on_abort=False,
            cancel_on_wait=False,
        )
```

`query_guard`'s call site keeps today's behavior by passing both deadlines equal and
`cancel_on_wait=True`:

```python
            deadline = time.monotonic() + min(
                budget, config.max_rebuild_seconds
            )
            rebuilt = self._index_with_deadline(
                force=False,
                languages=None,
                build_deadline=deadline,
                wait_deadline=deadline,
                restore_prior_on_abort=True,
                cancel_on_wait=True,
            )
```

Import `CodeGraphQueryError` in `runtime.py` if it is not already imported there.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/codegraph/test_indexer_runtime.py::test_wait_expiry_returns_the_job_and_the_build_still_reaches_ready -v`
Expected: PASS

- [ ] **Step 5: Write the no-op grace test**

```python
def test_current_graph_answers_with_its_report_inside_the_grace(seed_runtime):
    runtime = seed_runtime
    assert runtime.index(force=True)["state"] == "ready"

    started = time.monotonic()
    answer = runtime.index(wait_seconds=0)
    elapsed = time.monotonic() - started

    assert answer["state"] == "ready"
    assert answer["no_op"] is True
    assert "job" not in answer or answer["job"]["state"] == "ready"
    assert elapsed < 1.0
```

Run: `uv run pytest tests/codegraph/test_indexer_runtime.py -k grace -v`
Expected: PASS

- [ ] **Step 6: Prove the query path did not change**

Run: `uv run pytest -q tests/codegraph/test_indexer_runtime.py tests/codegraph/test_recovery_concurrency.py`
Expected: PASS, with the busy assertions at `test_indexer_runtime.py:2128` and
`test_recovery_concurrency.py:177` unmodified

- [ ] **Step 7: Commit**

```bash
git add src/iwiki_mcp/codegraph/runtime.py tests/codegraph/test_indexer_runtime.py
git commit -m "feat(codegraph): return the running job instead of cancelling on wait expiry"
```

---

### Task 4: Joining a live job is narrow

**Spec:** R6

**Files:**
- Modify: `src/iwiki_mcp/codegraph/runtime.py:99-125` (`_BuildWorkerRegistry.start`)
- Modify: `src/iwiki_mcp/codegraph/runtime.py:1037-1100` (`_index_with_deadline`'s `job is None` branch)
- Test: `tests/codegraph/test_indexer_runtime.py`

**Interfaces:**
- Consumes: `_BuildJob.matches(force=, languages=)` (Task 1), `_rebuilding_job_answer` (Task 3).
- Produces: `_BuildWorkerRegistry.start` returns the live job when the request matches it, `None` when a different job holds the worker.

- [ ] **Step 1: Write the failing test**

```python
def test_matching_second_call_joins_the_live_job(seed_runtime, monkeypatch):
    runtime = seed_runtime
    store = runtime.runtime._indexer.store
    real_publish = store.publish_metadata

    def slow_publish(*args, **kwargs):
        time.sleep(1.05)
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(store, "publish_metadata", slow_publish)

    first = runtime.index(force=True, wait_seconds=0)
    second = runtime.index(force=True, wait_seconds=0)
    mismatched = runtime.index(force=True, languages=["python"], wait_seconds=0)

    assert first["job"]["id"] == second["job"]["id"]
    assert mismatched["code"] == "busy"
    assert sum(
        thread.name == "iwiki-code-graph-build"
        for thread in threading.enumerate()
    ) == 1

    runtime.runtime.join_workers(timeout=10)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/codegraph/test_indexer_runtime.py::test_matching_second_call_joins_the_live_job -v`
Expected: FAIL — the second call answers `busy` because `start` refuses any live worker

- [ ] **Step 3: Write minimal implementation**

```python
    def start(self, domain_key, target, *, force=False, languages=None):
        with self._lock:
            live = (
                self._job
                if self._job is not None
                and self._job.thread is not None
                and self._job.thread.is_alive()
                else None
            )
            if live is not None:
                if live.domain_key == domain_key and live.matches(
                    force=force, languages=languages
                ):
                    return live
                return None
            job = _BuildJob(domain_key, force=force, languages=languages)
            # ... unchanged thread creation and start ...
            return job
```

In `_index_with_deadline`, a joined job must not be started twice — the returned job may
already be running, and `job.thread.join(...)` on it is exactly the wait a joining caller
wants, so no further change is needed there.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/codegraph/test_indexer_runtime.py::test_matching_second_call_joins_the_live_job -v`
Expected: PASS

- [ ] **Step 5: Prove the cross-domain guard still answers busy**

Run: `uv run pytest -q tests/codegraph/test_indexer_runtime.py -k "busy or concurrent"`
Expected: PASS with `test_indexer_runtime.py:2128` unmodified — its runtimes use different
domain keys, so `start` still returns `None` for them

- [ ] **Step 6: Commit**

```bash
git add src/iwiki_mcp/codegraph/runtime.py tests/codegraph/test_indexer_runtime.py
git commit -m "feat(codegraph): join a live build only when domain and parameters match"
```

---

### Task 5: The tool surface carries the job

**Spec:** R5

**Files:**
- Modify: `src/iwiki_mcp/codegraph/runtime.py:808-816` (`status`)
- Modify: `src/iwiki_mcp/codegraph/application.py:633-665` (`index_and_publish`)
- Modify: `src/iwiki_mcp/server.py:1767-1787` (`wiki_code_index`)
- Modify: `README.md`, `docs/README.ru.md`
- Test: `tests/codegraph/test_server_tools.py`, `tests/codegraph/test_application.py`

**Interfaces:**
- Consumes: `_BuildWorkerRegistry.current` and `_BuildJob.describe` (Task 1), `index(wait_seconds=...)` (Task 3).
- Produces: `wiki_code_index(force: bool = False, languages: list[str] | None = None, wait_seconds: float | None = None) -> dict`; `application.index_and_publish(binding, *, force, languages, environ, redact_failures, wait_seconds=None)`; `runtime.status()` answers gain an optional `job` key.

- [ ] **Step 1: Write the failing test**

Extend the existing schema assertion in
`tests/codegraph/test_server_tools.py::test_fastmcp_registry_has_exact_code_tools`, which
already builds its map as `tools = {tool.name: tool for tool in await server.mcp.list_tools()}`:

```python
    assert set(tools["wiki_code_index"].inputSchema["properties"]) == {
        "force", "languages", "wait_seconds",
    }
    assert tools["wiki_code_index"].inputSchema["properties"][
        "wait_seconds"
    ]["default"] is None
```

and, for the runtime half:

```python
def test_status_carries_the_terminal_job(seed_runtime):
    runtime = seed_runtime
    built = runtime.index(force=True)

    assert built["state"] == "ready"
    status = runtime.status()
    assert status["job"]["state"] == "ready"
    assert status["job"]["id"] == built["job"]["id"]
    assert "finished_at" in status["job"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/codegraph/test_server_tools.py -k wait_seconds -v`
Expected: FAIL — the schema holds only `{force, languages}`

- [ ] **Step 3: Write minimal implementation**

In `runtime.status`, attach the job after the answer is otherwise complete:

```python
    def _with_job(self, status: dict[str, object]) -> dict[str, object]:
        """Attach this process's job descriptor when the registry holds one."""
        job = _BUILD_WORKERS.current(self._worker_domain_key)
        if job is None:
            return status
        descriptor = job.describe()
        if job.state == "running":
            descriptor["phase"] = job.control.phase
            descriptor["phases_done"] = list(job.control.phases_done)
        return {**status, "job": descriptor}
```

and wrap every `return status` path of `status()` in `self._with_job(...)`.

In `index_and_publish`, thread the parameter through and skip publication for an
unfinished build:

```python
def index_and_publish(
    binding,
    *,
    force: bool = False,
    languages: list[str] | None = None,
    environ=None,
    redact_failures: bool = False,
    wait_seconds: float | None = None,
):
    ...
        indexed = runtime.index(
            force=force, languages=languages, wait_seconds=wait_seconds
        )
```

The existing `if config is not None and indexed.get("state") == "ready":` guard already
keeps a `rebuilding` answer from publishing.

In `server.wiki_code_index`, accept and validate the parameter:

```python
def wiki_code_index(
    force: bool = False,
    languages: list[str] | None = None,
    wait_seconds: float | None = None,
) -> dict:
    ...
    return _codegraph_application.index_and_publish(
        bind, force=force, languages=languages, wait_seconds=wait_seconds
    ).tool_result()
```

`CodeGraphQueryError` raised by `runtime.index` is already mapped to a typed answer by
`@_code_safe`; confirm that in Step 5 rather than adding a second mapping.

Document the parameter in `README.md` (the `wiki_code_index` row of the tool table and the
rebuild-budget section) and mirror it in `docs/README.ru.md`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/codegraph/test_server_tools.py tests/codegraph/test_application.py -q`
Expected: PASS

- [ ] **Step 5: Pin the parameter error**

```python
def test_out_of_range_wait_seconds_is_refused(seed_runtime):
    runtime = seed_runtime
    with pytest.raises(CodeGraphQueryError):
        runtime.index(wait_seconds=-1)
    with pytest.raises(CodeGraphQueryError):
        runtime.index(wait_seconds=10_000)
```

Run: `uv run pytest tests/codegraph/test_indexer_runtime.py -k wait_seconds -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/iwiki_mcp/codegraph/runtime.py src/iwiki_mcp/codegraph/application.py \
  src/iwiki_mcp/server.py README.md docs/README.ru.md \
  tests/codegraph/test_server_tools.py tests/codegraph/test_application.py \
  tests/codegraph/test_indexer_runtime.py
git commit -m "feat(codegraph): expose the index job through the tool surface"
```

---

### Task 6: An explicit job keeps the server awake

**Spec:** R7

**Files:**
- Modify: `src/iwiki_mcp/engine/idle.py:9-45` (`IdleTracker`)
- Modify: `src/iwiki_mcp/server.py:125-170` (`IdleFastMCP`)
- Modify: `src/iwiki_mcp/codegraph/runtime.py` (expose an explicit-job predicate)
- Test: `tests/engine/test_idle.py`

**Interfaces:**
- Consumes: `_BuildWorkerRegistry.current` (Task 1).
- Produces: `IdleTracker(has_background_work: Callable[[], bool] | None = None)`; `codegraph_runtime.explicit_job_active() -> bool`.

- [ ] **Step 1: Write the failing test**

```python
async def test_idle_waits_while_background_work_is_declared():
    from iwiki_mcp.engine.idle import IdleTracker

    working = {"value": True}
    tracker = IdleTracker(has_background_work=lambda: working["value"])

    with anyio.move_on_after(0.3) as scope:
        await tracker.wait_until_idle(0)
    assert scope.cancel_called is True

    working["value"] = False
    with anyio.move_on_after(1.0) as scope:
        await tracker.wait_until_idle(0)
    assert scope.cancel_called is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/engine/test_idle.py -k background -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'has_background_work'`

- [ ] **Step 3: Write minimal implementation**

```python
class IdleTracker:
    """Track incoming MCP activity, running tool calls, and declared work."""

    def __init__(self, has_background_work=None) -> None:
        self._active_calls = 0
        self._last_activity = time.monotonic()
        self._changed = anyio.Event()
        self._has_background_work = has_background_work

    async def wait_until_idle(self, timeout_seconds: int) -> None:
        """Return only after a quiet period with no call and no declared work."""
        while True:
            if self._active_calls:
                await self._changed.wait()
                continue
            if self._has_background_work is not None and self._has_background_work():
                await anyio.sleep(1)
                continue
            remaining = self._last_activity + timeout_seconds - time.monotonic()
            if remaining <= 0:
                return
            changed = self._changed
            with anyio.move_on_after(remaining):
                await changed.wait()
```

In `runtime.py`:

```python
def explicit_job_active() -> bool:
    """Report whether an explicit wiki_code_index job is still running."""
    job = _BUILD_WORKERS.explicit_job()
    return job is not None
```

with `_BuildWorkerRegistry.explicit_job()` returning the live job only when it was started
by `index` — Task 3 sets `job.explicit = True` there and `query_guard` leaves it `False`.
Add the flag to `_BuildJob.__init__` as `explicit: bool = False` and pass it from
`_index_with_deadline` as `explicit=not cancel_on_wait`.

In `server.py`:

```python
        tracker = IdleTracker(
            has_background_work=_codegraph_runtime.explicit_job_active
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/engine/test_idle.py -q`
Expected: PASS

- [ ] **Step 5: Prove an auto-rebuild does not hold the server**

```python
def test_query_time_rebuild_is_not_idle_activity(seed_runtime, monkeypatch):
    runtime = seed_runtime
    store = runtime.runtime._indexer.store
    real_publish = store.publish_metadata

    def slow_publish(*args, **kwargs):
        time.sleep(1.05)
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(store, "publish_metadata", slow_publish)
    runtime.query_guard()

    from iwiki_mcp.codegraph.runtime import explicit_job_active

    assert explicit_job_active() is False
    runtime.runtime.join_workers(timeout=10)
```

Run: `uv run pytest tests/codegraph/test_indexer_runtime.py -k idle -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/iwiki_mcp/engine/idle.py src/iwiki_mcp/server.py \
  src/iwiki_mcp/codegraph/runtime.py tests/engine/test_idle.py \
  tests/codegraph/test_indexer_runtime.py
git commit -m "feat(server): keep the stdio server awake while an explicit index job runs"
```

---

### Task 7: Whole-suite verification, docs, and release

**Spec:** the Testing section, and the Global Constraints of this plan

**Files:**
- Modify: `pyproject.toml`, `src/iwiki_mcp/__init__.py`, `tests/test_package.py`, `uv.lock`
- Modify: `src/iwiki_mcp/resources.py` (authoring rules mention of the rebuild answer)
- Test: the whole suite

**Interfaces:**
- Consumes: every earlier task.
- Produces: a release-ready branch.

- [ ] **Step 1: Run the code-graph and server suites**

Run: `uv run pytest -q tests/codegraph tests/engine tests/test_server_write.py tests/test_server_read.py`
Expected: PASS

- [ ] **Step 2: Run the PostgreSQL suite against a disposable database**

```bash
docker run -d --name iwiki-pgjob -e POSTGRES_PASSWORD=pgtest -e POSTGRES_DB=iwiki_test -p 127.0.0.1:55439:5432 pgvector/pgvector:pg16
sleep 8
docker exec iwiki-pgjob psql -U postgres -d iwiki_test -c "CREATE EXTENSION IF NOT EXISTS vector;"
IWIKI_TEST_POSTGRES_DSN="postgresql://postgres:pgtest@127.0.0.1:55439/iwiki_test" uv run pytest -q tests/postgres
docker rm -f iwiki-pgjob
```

Expected: PASS (544 tests at the time this plan was written)

- [ ] **Step 3: Run the full default suite**

Run: `uv run pytest -q`
Expected: PASS — 3494 passed / 238 skipped at the time this plan was written, plus the new cases

- [ ] **Step 4: Lint every touched file**

Run: `uv run flake8 src/iwiki_mcp/codegraph/runtime.py src/iwiki_mcp/codegraph/indexer.py src/iwiki_mcp/codegraph/application.py src/iwiki_mcp/server.py src/iwiki_mcp/engine/idle.py tests/codegraph/test_indexer_runtime.py tests/codegraph/test_server_tools.py tests/engine/test_idle.py`
Expected: exit 0, no output

- [ ] **Step 5: Measure the health metric on this repository**

Run the tool against the checkout and read its own numbers:

```bash
IWIKI_LLM_BASE_URL=http://example.invalid/v1 IWIKI_LLM_KEY=test uv run python -c "
from iwiki_mcp import base as wiki_base
from iwiki_mcp.codegraph import application
binding = wiki_base.resolve_storage_binding('.')
runtime = application.code_runtime(application.source_context(binding))
built = runtime.index(force=True)
print(built['duration_ms'], built['phase_timings_ms']['publication'])
"
```

Expected: `duration_ms` ≤ 100000 and the publication phase ≤ 5000, matching the intent's Health Metric

- [ ] **Step 6: Bump the version**

```bash
sed -i 's/0\.7\.256/0.7.257/' pyproject.toml src/iwiki_mcp/__init__.py tests/test_package.py
uv lock --quiet
uv run pytest -q tests/test_package.py
```

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/iwiki_mcp/__init__.py tests/test_package.py uv.lock src/iwiki_mcp/resources.py
git commit -m "chore(release): bump version to 0.7.257"
```

---

### Task 8: Result reconciliation and wiki closure

**Spec:** the Known limits section (they are what the wiki must record) plus the chain's own result gate

**Files:**
- Modify: wiki pages through the MCP tools (no repository files)

**Interfaces:**
- Consumes: the finished branch from Task 7.
- Produces: a validated result gate and an updated task ledger.

- [ ] **Step 1: Run the result gate**

Run: `/check-chain result docs/superpowers/plans/2026-09-10-codegraph-index-job-handle-plan.md`
Expected: verdict OK with every plan task matched to the diff

- [ ] **Step 2: Update the concept page**

Update `concept/code-graph-runtime`'s "Full-build lifecycle" section through
`wiki_update_page`: the caller's wait no longer cancels, the three surviving cancellation
points, and the `job` descriptor on both tools.

Expected: the write returns a new `revision`, and re-reading that section shows the three
cancellation points and the `job` descriptor named in it.

- [ ] **Step 3: Author the scenario**

Add a Given-When-Then scenario to `specification/code-graph-read-path-specifications`
covering "a wait that expires returns the job and the build still reaches ready", binding
`iwiki_mcp.codegraph.runtime.CodeGraphRuntime.index` as `implements` and
`tests/codegraph/test_indexer_runtime.py` as `verifies`.

- [ ] **Step 4: Rebuild and resolve**

Run `wiki_code_index` on the local server, then `wiki_spec_resolve` for the new scenario.
Expected: `state: ready` and `resolved` evidence for the symbol binding.

- [ ] **Step 5: Open the pull request**

```bash
git push -u origin dev-codegraph-index-job-handle
gh pr create --base master --head dev-codegraph-index-job-handle \
  --title "feat(codegraph): return an index job handle instead of cancelling on wait expiry" \
  --body "<measurements, verification, known limits>"
```

Expected: the PR URL. Merging it is the human checkpoint named in the intent.
