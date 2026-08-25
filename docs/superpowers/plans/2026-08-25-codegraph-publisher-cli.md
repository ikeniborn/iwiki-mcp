---
review:
  plan_hash: e474835e4cb2a6cf
  last_run: 2026-08-25
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    dependencies: { status: passed }
    verifiability: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-08-24-codegraph-publisher-cli-intent.md
  spec: docs/superpowers/specs/2026-08-25-codegraph-publisher-cli-design.md
result_check:
  verdict: OK
  source: plan
  plan_hash: e474835e4cb2a6cf
  last_run: 2026-08-25
  reviewed: true
  docs_checked: true
---
# Codegraph Publisher CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one scheduled-operation CLI that builds a code graph from an explicit local checkout and safely publishes it to the configured SQLite, direct PostgreSQL, or MCP HTTP target.

**Architecture:** Move local runtime and snapshot-publication orchestration from `server.py` into a focused `codegraph.application` service. The service separates an immutable local source context from the configured target binding, while `server.py` preserves existing MCP payloads and `admin.py` adds only command parsing, output formatting, redaction, and exit mapping.

**Tech Stack:** Python 3.10+, argparse, dataclasses/protocols, SQLite, psycopg/PostgreSQL, MCP streamable HTTP, Tree-sitter adapters, pytest/pytest-asyncio, Starlette TestClient.

---

## Plan summary

Expected outputs:

1. `iwiki-mcp code publish --project <checkout> [--json]` runs without starting stdio MCP.
2. `codegraph.application` owns source-context composition, target validation, indexing, export, batching, abort, and finalize.
3. PostgreSQL-bound builds use `<project>/.iwiki/code-<domain>.sqlite3` only after local Git exclusion succeeds.
4. SQLite, direct PostgreSQL, local hosted MCP HTTP, and remote MCP transport paths have synthetic Wiki/code fixtures and failure-preservation evidence.
5. Existing code-graph MCP tools, snapshot/schema protocol, admin commands, Markdown Wiki operations, and complete regression suite remain compatible.
6. English/Russian operator docs, architecture docs, iwiki pages, systemd examples, and generic CI examples describe the delivered behavior.

Requirement coverage:

| Spec requirement | Plan tasks | Result evidence |
| --- | --- | --- |
| R-001 command/dispatch | 4 | parser and `server.main()` routing tests |
| R-002 shared service | 1, 2, 3 | application tests plus server delegation diff |
| R-003 source/target split | 1, 6 | source-context tests and PostgreSQL synthetic cache |
| R-004 cache exclusion | 1, 6 | normal/worktree/fail-closed tests |
| R-005 exact target/no fallback | 2, 5, 6, 7 | mode table and zero-fallback assertions |
| R-006 SQLite atomic path | 2, 5 | synthetic repeated publish and failure preservation |
| R-007 existing PostgreSQL publisher | 2, 6 | real `PostgresCodeGraphStore` publication |
| R-008 existing MCP API/limits/grants | 2, 7 | hosted JSON-RPC publication and bounded batches |
| R-009 output/exits | 4 | text/JSON/exit matrix |
| R-010 redaction | 4, 6, 7 | sentinel-secret capture tests |
| R-011 failure atomicity/abort | 2, 5, 6, 7 | prior-revision and abort assertions |
| R-012 compatibility | 3, 9 | legacy payload tests and full regression |
| R-013 operations docs | 8 | doc contract tests and iwiki lint |

Synthetic test matrix:

| Route | Source | Target | Evidence |
| --- | --- | --- | --- |
| SQLite | temporary Git checkout with Python source | temporary Git Wiki base | CLI ready JSON, repeated revision, local status/search |
| PostgreSQL | temporary Git checkout with Python source | disposable pgvector `*_test` database with synthetic Markdown Wiki | project-local cache, direct finalize, PostgreSQL status/search |
| MCP local HTTP | temporary Git checkout with Python source | in-process hosted streamable-HTTP server backed by disposable PostgreSQL | real JSON-RPC begin/batch/finalize/status, server bounds/grants |
| MCP remote adapter | same canonical snapshot fixture | redacted remote-session fixture | URL/token handling, binding, protocol, failure redaction |

No implementation-time human decision remains inside the approved spec. A schema,
protocol, new public flag/exit, new target, or inability to preserve active snapshots is
a HUMAN CHECKPOINT and returns to the approved design instead of being decided in code.

### Task 1: Introduce the explicit local source context

**Closes:** R-002, R-003, R-004; establishes the source half of AC-02, AC-03, and AC-04.

**Files:**
- Create: `src/iwiki_mcp/codegraph/application.py`
- Modify: `src/iwiki_mcp/codegraph/runtime.py:12-18,292-375`
- Modify: `src/iwiki_mcp/codegraph/context.py:22-27`
- Test: `tests/codegraph/test_application.py`

- [ ] **Step 1: Write failing source-context tests**

Create `tests/codegraph/test_application.py` with constructors that contain no real
credentials and these focused assertions:

```python
from pathlib import Path

import pytest

from iwiki_mcp.codegraph import application
from iwiki_mcp.storage import GitBinding, PostgresBinding


def _postgres_binding(project: Path) -> PostgresBinding:
    return PostgresBinding(
        host="127.0.0.1",
        port=5432,
        database="synthetic_test",
        user="fixture",
        password="fixture-password",
        sslmode="disable",
        iwiki_id="wiki-a",
        read=("docs",),
        write=("docs",),
        primary="docs",
        project_dir=str(project),
        embed_model="fixture-model",
        embed_dimensions=3,
        rerank_model="",
    )


def test_git_source_context_keeps_the_wiki_cache_and_selector(tmp_path):
    project = tmp_path / "project"
    wiki = tmp_path / "wiki"
    project.mkdir()
    wiki.mkdir()
    binding = GitBinding(
        base=str(wiki), read=("docs",), write=("docs",),
        primary="docs", project_dir=str(project),
    )

    source = application.source_context(binding)

    assert source.base == str(wiki)
    assert source.project_dir == str(project)
    assert source.primary == "docs"
    assert source.wiki_base == str(wiki)


def test_postgres_source_context_uses_project_cache_and_local_exclude(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    calls = []
    monkeypatch.setattr(
        application.wiki_base,
        "ensure_graph_store_excluded",
        lambda value: calls.append(value) or True,
    )

    source = application.source_context(_postgres_binding(project))

    assert source.base == str(project)
    assert source.wiki_base is None
    assert calls == [str(project)]


def test_postgres_source_context_fails_before_cache_when_exclusion_fails(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        application.wiki_base,
        "ensure_graph_store_excluded",
        lambda _value: False,
    )

    with pytest.raises(application.CodeGraphApplicationError) as failure:
        application.source_context(_postgres_binding(project))

    assert failure.value.code == "invalid_config"
    assert not (project / ".iwiki").exists()
```

- [ ] **Step 2: Run the tests and verify the missing module failure**

Run: `uv run pytest tests/codegraph/test_application.py -v`

Expected: collection fails because `iwiki_mcp.codegraph.application` does not exist.

- [ ] **Step 3: Add a structural runtime source protocol**

In `runtime.py`, replace the concrete `Binding` annotation with the exact structural
contract used by the runtime:

```python
from typing import Mapping, Protocol


class CodeGraphSource(Protocol):
    base: str
    project_dir: str
    primary: str | None


class CodeGraphRuntime:
    def __init__(
        self,
        binding: CodeGraphSource,
        *,
        adapter_factories: Mapping[str, AdapterFactory] | None = None,
        resolver_version: str = _DEFAULT_RESOLVER_VERSION,
    ) -> None:
        self.binding = binding
```

Keep the constructor body unchanged after `self.binding = binding`. Update the comment in
`context.py` so it names `codegraph.application.code_graph_adapter_factories` as the
owner of the `py`/`ts`/`js` prefixes.

- [ ] **Step 4: Add the source context and runtime composer**

Create `application.py` with focused imports, a safe error type, the immutable context,
and the runtime composer. Move the existing distribution-version and three language
adapter-factory definitions from `server.py` without changing their parser, grammar,
extension, or adapter version values.

```python
"""One-shot local code-graph build and publication application service."""
from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Mapping

from iwiki_mcp import base as wiki_base
from iwiki_mcp.storage import GitBinding, PostgresBinding

from . import config as codegraph_config
from . import indexer as codegraph_indexer
from . import linking
from . import runtime as codegraph_runtime
from .languages import javascript, python, typescript
from .models import CodeGraphError


class CodeGraphApplicationError(CodeGraphError):
    code = "invalid_config"


@dataclass(frozen=True)
class CodeGraphSourceContext:
    base: str
    project_dir: str
    primary: str
    wiki_base: str | None


def source_context(
    binding: GitBinding | PostgresBinding,
) -> CodeGraphSourceContext:
    if binding.primary is None:
        raise CodeGraphApplicationError("primary domain is required")
    if isinstance(binding, PostgresBinding):
        if not wiki_base.ensure_graph_store_excluded(binding.project_dir):
            raise CodeGraphApplicationError(
                "local code graph cache exclusion is required"
            )
        return CodeGraphSourceContext(
            base=binding.project_dir,
            project_dir=binding.project_dir,
            primary=binding.primary,
            wiki_base=None,
        )
    return CodeGraphSourceContext(
        base=binding.base,
        project_dir=binding.project_dir,
        primary=binding.primary,
        wiki_base=binding.base,
    )
```

Add `code_graph_adapter_factories(repository_id, config)` by copying the existing three
factory bodies from `server.py` exactly. Then add:

```python
def code_runtime(source: CodeGraphSourceContext) -> codegraph_runtime.CodeGraphRuntime:
    try:
        config = codegraph_config.load_code_graph_config(source.project_dir)
    except codegraph_config.CodeGraphConfigError:
        config = None
    runtime = codegraph_runtime.CodeGraphRuntime(
        source,
        adapter_factories=code_graph_adapter_factories(source.primary, config),
    )
    if runtime._indexer is not None and source.wiki_base is not None:
        runtime._indexer.wiki_selector_resolver = linking.WikiSelectorResolver(
            source.wiki_base
        )
    return runtime
```

- [ ] **Step 5: Run focused source/runtime tests**

Run: `uv run pytest tests/codegraph/test_application.py tests/codegraph/test_indexer_runtime.py -q`

Expected: all selected tests pass; no PostgreSQL integration test or network call runs.

- [ ] **Step 6: Commit the source-context slice**

```bash
git add src/iwiki_mcp/codegraph/application.py src/iwiki_mcp/codegraph/runtime.py src/iwiki_mcp/codegraph/context.py tests/codegraph/test_application.py
git commit -m "refactor(codegraph): separate local source context"
```

### Task 2: Move target selection and publication orchestration into the service

**Closes:** R-002, R-005, R-006, R-007, R-008, R-011; implements AC-02 and the unit
portion of AC-05 through AC-11.

**Files:**
- Modify: `src/iwiki_mcp/codegraph/application.py`
- Test: `tests/codegraph/test_application.py`

- [ ] **Step 1: Write failing target and lifecycle tests**

Add a recording publisher and explicit tests for target validation, server limits, batch
failure, finalize failure, exception handling, abort precedence, and zero fallback:

```python
class RecordingPublisher:
    def __init__(self, *, batch_result=None, finalize_result=None):
        self.calls = []
        self.batch_result = batch_result or {"accepted": True}
        self.finalize_result = finalize_result or {
            "state": "ready", "snapshot_revision": "sha256:remote"
        }

    def begin(self, header):
        from iwiki_mcp.codegraph.publication import PublicationSession
        self.calls.append(("begin", header.repository_id))
        return PublicationSession(
            session_id="session-a",
            lease_expires_at="2026-08-25T00:00:00+00:00",
            base_snapshot_revision="sha256:old",
            base_markdown_token=1,
            max_batch_rows=1,
            max_batch_bytes=1_000_000,
        )

    def publish_batch(self, session, batch):
        self.calls.append(("batch", batch.kind, batch.ordinal, batch.row_count))
        return dict(self.batch_result)

    def finalize(self, session):
        self.calls.append(("finalize", session.session_id))
        return dict(self.finalize_result)

    def abort(self, session):
        self.calls.append(("abort", session.session_id))
        return {"state": "aborted"}


def test_batch_failure_aborts_once_and_never_finalizes(snapshot_fixture):
    publisher = RecordingPublisher(
        batch_result={"error": "batch_conflict", "hint": "begin a new session"}
    )

    result = application.publish_snapshot(
        snapshot_fixture.runtime,
        publisher,
        snapshot_fixture.config,
    )

    assert result["error"] == "batch_conflict"
    assert [call[0] for call in publisher.calls].count("abort") == 1
    assert all(call[0] != "finalize" for call in publisher.calls)


def test_finalize_failure_aborts_once(snapshot_fixture):
    publisher = RecordingPublisher(
        finalize_result={"error": "snapshot_conflict", "hint": "rebuild"}
    )

    result = application.publish_snapshot(
        snapshot_fixture.runtime,
        publisher,
        snapshot_fixture.config,
    )

    assert result["error"] == "snapshot_conflict"
    assert publisher.calls[-1] == ("abort", "session-a")
```

Use the existing `_exported_snapshot()` row shape from
`tests/codegraph/test_server_tools.py` for `snapshot_fixture`; include two file rows so
the advertised one-row bound proves every batch has `row_count <= 1`.

- [ ] **Step 2: Run the lifecycle tests and verify missing service functions**

Run: `uv run pytest tests/codegraph/test_application.py -k "failure or target or limits" -v`

Expected: failures name missing `publish_snapshot`, `publisher_for`, or
`index_and_publish` symbols.

- [ ] **Step 3: Add the internal outcome and target validation**

Add these types and exact mode rules to `application.py`:

```python
from dataclasses import dataclass, field
import os
import secrets
import time

from iwiki_mcp.postgres.codegraph import PostgresCodeGraphStore

from .mcp_adapter import McpSnapshotPublisher, RemoteMcpTransport
from .publication import (
    PublicationSession,
    SnapshotPublisher,
    iter_snapshot_batches,
)


@dataclass(frozen=True)
class CodeGraphPublishOutcome:
    publish_mode: str | None
    index: dict[str, object]
    publication: dict[str, object] = field(default_factory=dict)
    duration_ms: int = 0

    @property
    def ready(self) -> bool:
        if self.index.get("state") != "ready":
            return False
        return self.publish_mode == "sqlite" or self.publication.get("state") == "ready"

    @property
    def snapshot_revision(self) -> str | None:
        value = (
            self.index.get("revision")
            if self.publish_mode == "sqlite"
            else self.publication.get("snapshot_revision")
        )
        return value if isinstance(value, str) else None

    def tool_result(self) -> dict[str, object]:
        if (
            self.publish_mode in (None, "sqlite")
            or self.index.get("state") != "ready"
        ):
            return dict(self.index)
        return {**self.index, "publication": dict(self.publication)}


def validate_target(binding, publish_mode: str) -> None:
    if publish_mode == "sqlite" and isinstance(binding, PostgresBinding):
        raise CodeGraphApplicationError(
            "sqlite publication requires a Git Wiki binding"
        )
    if publish_mode == "postgres" and not isinstance(binding, PostgresBinding):
        raise CodeGraphApplicationError(
            "postgres publication requires PostgreSQL storage"
        )
    if publish_mode not in {"sqlite", "postgres", "mcp"}:
        raise CodeGraphApplicationError("unknown publish mode")
```

- [ ] **Step 4: Add existing-publisher composition**

Add one constructor shared by direct CLI and hosted server composition:

```python
def create_postgres_publisher(
    binding: PostgresBinding,
    owner_id: str,
    settings,
    *,
    lock_timeout_ms: int = 5000,
) -> PostgresCodeGraphStore:
    if binding.primary is None:
        raise CodeGraphApplicationError("primary domain is required")
    return PostgresCodeGraphStore(
        binding.connection_dsn(),
        binding.iwiki_id,
        binding.primary,
        owner_id,
        lock_timeout_ms=lock_timeout_ms,
        session_ttl_seconds=settings.publication_session_ttl_seconds,
        staging_retention_seconds=settings.staging_retention_seconds,
        staging_cleanup_limit=settings.staging_cleanup_limit,
    )


def publisher_for(binding, config, *, environ=None) -> SnapshotPublisher | None:
    validate_target(binding, config.publish_mode)
    if config.publish_mode == "sqlite":
        return None
    if config.publish_mode == "postgres":
        return create_postgres_publisher(
            binding,
            secrets.token_hex(16),
            config,
        )
    return McpSnapshotPublisher(
        RemoteMcpTransport(
            environ=os.environ if environ is None else environ,
            primary=binding.primary,
        )
    )
```

No other factory or fallback is permitted.

- [ ] **Step 5: Add bounded publish and one-shot orchestration**

Move `_effective_batch_bounds` from `server.py` unchanged and add this lifecycle:

```python
def _abort_preserving_failure(publisher, session) -> None:
    try:
        publisher.abort(session)
    except Exception:
        return


def publish_snapshot(runtime, publisher, config) -> dict[str, object]:
    exported = runtime.export_snapshot()
    if isinstance(exported, dict):
        return exported
    header, rows = exported
    session = None
    try:
        opened = publisher.begin(header)
        if isinstance(opened, dict):
            return opened
        session = opened
        max_rows, max_bytes = effective_batch_bounds(session, config)
        for batch in iter_snapshot_batches(
            rows, max_rows=max_rows, max_bytes=max_bytes
        ):
            accepted = publisher.publish_batch(session, batch)
            if "error" in accepted:
                _abort_preserving_failure(publisher, session)
                return accepted
        finalized = publisher.finalize(session)
        if "error" in finalized:
            _abort_preserving_failure(publisher, session)
        return finalized
    except Exception:
        if session is not None:
            _abort_preserving_failure(publisher, session)
        raise


def index_and_publish(
    binding,
    *,
    force: bool = False,
    languages: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> CodeGraphPublishOutcome:
    started = time.monotonic()
    runtime = code_runtime(source_context(binding))
    config = runtime.config
    mode = None if config is None else config.publish_mode
    if config is not None:
        validate_target(binding, config.publish_mode)
    indexed = runtime.index(force=force, languages=languages)
    publication: dict[str, object] = {}
    if config is not None and indexed.get("state") == "ready":
        publisher = publisher_for(binding, config, environ=environ)
        if publisher is not None:
            publication = publish_snapshot(runtime, publisher, config)
    return CodeGraphPublishOutcome(
        publish_mode=mode,
        index=dict(indexed),
        publication=publication,
        duration_ms=max(0, int((time.monotonic() - started) * 1000)),
    )
```

- [ ] **Step 6: Run all application tests**

Run: `uv run pytest tests/codegraph/test_application.py -q`

Expected: all tests pass; failures abort once, advertised limits control batching, and
invalid mode/binding pairs create no publisher calls.

- [ ] **Step 7: Commit shared publication orchestration**

```bash
git add src/iwiki_mcp/codegraph/application.py tests/codegraph/test_application.py
git commit -m "feat(codegraph): add shared publication service"
```

### Task 3: Delegate the existing MCP indexing path without contract drift

**Closes:** R-002 and R-012; proves AC-02 and AC-12.

**Files:**
- Modify: `src/iwiki_mcp/server.py:20-180,890-1110,1140-1170`
- Modify: `tests/codegraph/test_server_tools.py:630-830,1030-1075`
- Modify: `tests/test_server_startup.py`

- [ ] **Step 1: Add delegation regressions before removing server orchestration**

Replace private-factory assertions with application-bound behavior and retain exact
public payload assertions:

```python
def test_wiki_code_index_delegates_and_preserves_tool_payload(
    seed_binding, monkeypatch
):
    from iwiki_mcp.codegraph.application import CodeGraphPublishOutcome

    calls = []
    monkeypatch.setattr(server.base, "resolve_binding", lambda: seed_binding)
    monkeypatch.setattr(
        server._codegraph_application,
        "index_and_publish",
        lambda binding, **values: calls.append((binding, values)) or
        CodeGraphPublishOutcome(
            publish_mode="mcp",
            index={"state": "ready", "revision": "sha256:local"},
            publication={
                "state": "ready",
                "snapshot_revision": "sha256:remote",
            },
        ),
    )

    assert server.wiki_code_index(force=True, languages=["python"]) == {
        "state": "ready",
        "revision": "sha256:local",
        "publication": {
            "state": "ready",
            "snapshot_revision": "sha256:remote",
        },
    }
    assert calls == [
        (seed_binding, {"force": True, "languages": ["python"]})
    ]
```

Keep the existing tests that a PostgreSQL binding returns `source_unavailable`, SQLite
returns no nested publication, invalid languages fail before binding, and MCP tool input
schema contains only `force` and `languages`.

- [ ] **Step 2: Run focused server tests and verify delegation is absent**

Run: `uv run pytest tests/codegraph/test_server_tools.py -k "index or publish_mode or batch_bounds" -v`

Expected: the new delegation test fails because `server._codegraph_application` is not
imported and `wiki_code_index` still owns orchestration.

- [ ] **Step 3: Replace local server composition with application delegation**

Import `codegraph.application` in the eager startup closure. Remove
`_code_graph_adapter_factories`, `_code_runtime`, `_code_publisher_factories`,
`_code_publisher`, `_effective_batch_bounds`, and `_publish_local_snapshot` after their
tests move to `test_application.py`. Preserve hosted publication wrappers.

Use the shared PostgreSQL constructor in the hosted path:

```python
def _postgres_code_store(binding: base.PostgresBinding, owner_id: str):
    return _codegraph_application.create_postgres_publisher(
        binding,
        owner_id,
        _hosted_code_graph_settings(),
        lock_timeout_ms=_CODE_PUBLICATION_LOCK_TIMEOUT_MS,
    )
```

Keep `wiki_code_status` unchanged. Replace only the Git/local body of
`wiki_code_index`:

```python
@_safe
@_code_safe
def wiki_code_index(
    force: bool = False,
    languages: list[str] | None = None,
) -> dict:
    if languages is not None and (
        not languages
        or any(
            language not in _codegraph_config.KNOWN_LANGUAGES
            for language in languages
        )
    ):
        return _invalid_code_config()
    bind = _resolved_binding()
    if _is_postgres(bind):
        return dict(_CODE_SOURCE_UNAVAILABLE)
    if bind.primary is None:
        return _missing_code_primary()
    return _codegraph_application.index_and_publish(
        bind,
        force=force,
        languages=languages,
    ).tool_result()
```

- [ ] **Step 4: Verify existing tool and startup contracts**

Run: `uv run pytest tests/codegraph/test_server_tools.py tests/test_server_startup.py tests/test_mcp_smoke.py -q`

Expected: all tests pass; MCP tool count and schemas are unchanged, SQLite tool payloads
remain flat, external publication remains nested, and hosted PostgreSQL indexing remains
`source_unavailable`.

- [ ] **Step 5: Commit server delegation**

```bash
git add src/iwiki_mcp/server.py tests/codegraph/test_server_tools.py tests/test_server_startup.py
git commit -m "refactor(codegraph): share MCP publication flow"
```

### Task 4: Add the public CLI, stable streams, exits, and redaction

**Closes:** R-001, R-009, R-010; implements AC-01, AC-09, and AC-10.

**Files:**
- Modify: `src/iwiki_mcp/admin.py:1-170,610-670`
- Modify: `src/iwiki_mcp/server.py:3849-3860`
- Create: `tests/test_code_publish_cli.py`
- Modify: `tests/postgres/test_admin.py:60-150`

- [ ] **Step 1: Write the complete parser/output/error matrix first**

Create `tests/test_code_publish_cli.py` with an outcome helper and parameterized tests:

```python
from io import StringIO
import json

import pytest

from iwiki_mcp import admin
from iwiki_mcp.codegraph.application import CodeGraphPublishOutcome


def _run(argv, monkeypatch, value):
    stdout = StringIO()
    stderr = StringIO()
    if isinstance(value, BaseException):
        def publish(*_args, **_kwargs):
            raise value
    else:
        def publish(*_args, **_kwargs):
            return value
    monkeypatch.setattr(admin._codegraph_application, "publish_project", publish)
    code = admin.run(argv, stdout=stdout, stderr=stderr, environ={})
    return code, stdout.getvalue(), stderr.getvalue()


def test_text_success_is_one_concise_stdout_line(monkeypatch):
    outcome = CodeGraphPublishOutcome(
        publish_mode="mcp",
        index={
            "state": "ready",
            "counts": {"files": 1, "symbols": 2, "relations": 3},
        },
        publication={
            "state": "ready",
            "snapshot_revision": "sha256:remote",
        },
        duration_ms=17,
    )

    code, stdout, stderr = _run(
        ["code", "publish", "--project", "/repo"], monkeypatch, outcome
    )

    assert code == 0
    assert stdout == (
        "code graph ready mode=mcp revision=sha256:remote "
        "files=1 symbols=2 relations=3 duration_ms=17\n"
    )
    assert stderr == ""


@pytest.mark.parametrize(
    ("failure", "exit_code", "stable_code"),
    [
        (
            admin._codegraph_application.CodeGraphApplicationError(
                "sentinel-secret"
            ),
            2,
            "invalid_config",
        ),
        (RuntimeError("sentinel-secret"), 1, "internal_error"),
    ],
)
def test_json_failures_are_one_redacted_object(
    monkeypatch, failure, exit_code, stable_code
):
    code, stdout, stderr = _run(
        ["code", "publish", "--project", "/repo", "--json"],
        monkeypatch,
        failure,
    )

    assert code == exit_code
    assert json.loads(stdout)["error"] == stable_code
    assert stdout.count("\n") == 1
    assert "sentinel-secret" not in stdout + stderr


def test_json_usage_failure_never_echoes_unknown_argument_value():
    stdout = StringIO()
    stderr = StringIO()

    code = admin.run(
        ["code", "publish", "--json", "--token", "sentinel-secret"],
        stdout=stdout,
        stderr=stderr,
        environ={},
    )

    assert code == 2
    assert json.loads(stdout)["error"] == "invalid_usage"
    assert "sentinel-secret" not in stdout.getvalue() + stderr.getvalue()
```

Also test missing `--project`, every unapproved option, text index/publication failure,
JSON `publish_mode: null` before configuration resolution, no traceback, and
`server.main()` routing without `mcp.run()`.

- [ ] **Step 2: Run the new CLI tests and verify parser/runner failures**

Run: `uv run pytest tests/test_code_publish_cli.py -v`

Expected: failures show missing `code` parser and `publish_project` service entry point.

- [ ] **Step 3: Add `publish_project` checkout resolution**

In `application.py`, add a strict root check before binding resolution:

```python
from pathlib import Path
import subprocess


def checkout_root(value: str) -> Path:
    candidate = Path(value).absolute()
    try:
        result = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CodeGraphApplicationError(
            "project must be a Git checkout root"
        ) from exc
    root = Path(result.stdout.strip()).absolute()
    if root != candidate or candidate.is_symlink():
        raise CodeGraphApplicationError(
            "project must be a Git checkout root"
        )
    return root


def publish_project(
    project_dir: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> CodeGraphPublishOutcome:
    root = checkout_root(project_dir)
    binding = wiki_base.resolve_storage_binding(str(root))
    return index_and_publish(binding, environ=environ)
```

- [ ] **Step 4: Add the isolated code parser and stream formatter**

Add `code` to `_ADMIN_COMMANDS`. Create a `_CodeUsageError` and use a parser subclass
only for `code publish`, preserving existing admin parser `SystemExit` behavior.

```python
class _CodeUsageError(Exception):
    pass


class _CodeArgumentParser(_StrictArgumentParser):
    def error(self, _message: str) -> None:
        raise _CodeUsageError()


def _add_code_command(commands) -> None:
    code = commands.add_parser("code")
    code_commands = code.add_subparsers(
        dest="code_command",
        required=True,
        parser_class=_CodeArgumentParser,
    )
    publish = code_commands.add_parser("publish")
    publish.add_argument("--project", required=True)
    publish.add_argument("--json", action="store_true")
```

Catch `_CodeUsageError` around parsing. When `argv[:2] == ["code", "publish"]` and
`--json` is present, emit only this compact stdout object; otherwise emit one stable
stderr line:

```python
{"state": "failed", "publish_mode": None, "error": "invalid_usage", "duration_ms": 0}
```

Implement `_run_code_publish(args, env, out, err)` with explicit exception classes:
`BaseError`, `CodeGraphConfigError`, `CodeGraphApplicationError`, and
`CodeGraphAdapterError` map to exit 2; `psycopg.Error` and all unexpected exceptions map
to exit 1. Never print exception text. Map non-ready outcomes to `index_failed` or
`publication_failed`. Use a dedicated compact JSON writer with
`separators=(",", ":")`; do not change existing administration JSON formatting.

- [ ] **Step 5: Route code before PostgreSQL administration service creation**

Immediately after parsing and before `_service(...)`, dispatch:

```python
if args.command == "code" and args.code_command == "publish":
    return _run_code_publish(args, env, out, err)
```

Exempt `code publish` from the existing rule that reserves top-level `--project` for
stdio. `server.main()` needs no new branch beyond adding `code` to
`is_admin_command()`; its existing admin routing then avoids `mcp.run()`.

- [ ] **Step 6: Run CLI and admin compatibility tests**

Run: `uv run pytest tests/test_code_publish_cli.py tests/postgres/test_admin.py tests/test_server_startup.py -q`

Expected: all tests pass or PostgreSQL-marked admin tests skip only when the explicit
test database is absent; existing admin commands and stdio `--project` remain unchanged.

- [ ] **Step 7: Commit the public CLI slice**

```bash
git add src/iwiki_mcp/admin.py src/iwiki_mcp/server.py src/iwiki_mcp/codegraph/application.py tests/test_code_publish_cli.py tests/postgres/test_admin.py tests/test_server_startup.py
git commit -m "feat(cli): add code graph publish command"
```

### Task 5: Prove the synthetic SQLite Wiki route end to end

**Closes:** R-005, R-006, R-011; implements AC-05, AC-06, and SQLite AC-11.

**Files:**
- Create: `tests/codegraph/synthetic_wiki.py`
- Create: `tests/codegraph/test_publish_cli_sqlite.py`
- Modify: `tests/codegraph/test_application.py`

- [ ] **Step 1: Add a reusable synthetic checkout/Wiki fixture**

Create `tests/codegraph/synthetic_wiki.py` with a helper that initializes a Git repo,
writes one Markdown page and two connected Python symbols, and writes a complete
`.iwiki.toml`. All paths come from `tmp_path`; no environment secret is written.

```python
def create_sqlite_project(tmp_path):
    project = tmp_path / "project"
    wiki = tmp_path / "wiki"
    domain = wiki / "docs"
    project.mkdir()
    domain.mkdir(parents=True)
    (domain / "architecture.md").write_text(
        "---\ntype: concept\ntitle: Architecture\nstatus: stable\n---\n"
        "## Service\n\n`Service.run` is the entry point.\n",
        encoding="utf-8",
    )
    (project / "service.py").write_text(
        "class Service:\n"
        "    def run(self):\n"
        "        return helper()\n\n"
        "def helper():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    (project / ".iwiki.toml").write_text(
        f"base = {json.dumps(str(wiki))}\n"
        "read = [\"docs\"]\nwrite = [\"docs\"]\nprimary = \"docs\"\n"
        "[code_graph]\nenabled = true\nlanguages = [\"python\"]\n"
        "publish_mode = \"sqlite\"\nread_mode = \"sqlite\"\n"
        "max_full_rebuild_seconds = 30\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    return project, wiki
```

- [ ] **Step 2: Write the failing real CLI success/repeat/query test**

Call `admin.run` twice with `--json`, parse one object each time, resolve the binding,
build the application runtime, and assert:

```python
assert first["state"] == second["state"] == "ready"
assert first["publish_mode"] == "sqlite"
assert first["snapshot_revision"] == second["snapshot_revision"]
assert first["counts"]["files"] == 1
assert runtime.status()["fresh"] is True
assert runtime.search("Service")["results"]
```

Assert stderr is empty, the Wiki Markdown remains byte-identical, and no external
publisher factory call occurs.

- [ ] **Step 3: Run the synthetic SQLite test**

Run: `uv run pytest tests/codegraph/test_publish_cli_sqlite.py -v`

Expected before fixes: FAIL at the first incorrect CLI/application boundary. Expected
after minimal fixture corrections: PASS with a ready, queryable local graph.

- [ ] **Step 4: Add a prior-revision failure-preservation test**

In `test_application.py`, seed an outcome with an active SQLite revision, make the next
runtime `index()` return `{"state": "failed", "code": "rebuild_failed"}`, and assert the
service does not call export/publisher. Pair it with the existing real runtime recovery
test that reads the old active revision after a controlled build failure; do not create a
second SQLite activation implementation.

- [ ] **Step 5: Run SQLite route and recovery suites**

Run: `uv run pytest tests/codegraph/test_publish_cli_sqlite.py tests/codegraph/test_application.py tests/codegraph/test_recovery_concurrency.py -q`

Expected: all pass; repeated publish is stable and controlled failure retains the old
ready revision.

- [ ] **Step 6: Commit synthetic SQLite coverage**

```bash
git add tests/codegraph/synthetic_wiki.py tests/codegraph/test_publish_cli_sqlite.py tests/codegraph/test_application.py
git commit -m "test(codegraph): cover synthetic SQLite publication"
```

### Task 6: Prove direct PostgreSQL publication from a synthetic checkout

**Closes:** R-003, R-004, R-005, R-007, R-010, R-011; implements AC-03, AC-04, AC-07,
PostgreSQL AC-10, and PostgreSQL AC-11.

**Files:**
- Create: `tests/postgres/test_code_publish_cli.py`
- Modify: `tests/codegraph/synthetic_wiki.py`

- [ ] **Step 1: Write a real disposable-database project fixture**

Use `clean_postgres`, run existing migrations, create `wiki-a/docs`, and write synthetic
Markdown through `PostgresStore`. Convert `clean_postgres` with
`psycopg.conninfo.conninfo_to_dict`; write only host/port/database/user/sslmode and
`iwiki_id` to `.iwiki.toml`. Set the password through `monkeypatch.setenv` only.

The project config must select `publish_mode = "postgres"` and
`read_mode = "postgres"`. Initialize Git before the command so
`/.iwiki/` can be added to the local `info/exclude`.

- [ ] **Step 2: Write the failing direct publication test**

Run the real CLI, then construct `PostgresCodeGraphReader` from the resolved binding and
assert:

```python
assert exit_code == 0
assert payload["state"] == "ready"
assert payload["publish_mode"] == "postgres"
assert payload["snapshot_revision"].startswith("sha256:")
assert reader.status()["snapshot_revision"] == payload["snapshot_revision"]
assert (project / ".iwiki" / "code-docs.sqlite3").is_file()
assert "/.iwiki/" in git_exclude.read_text(encoding="utf-8").splitlines()
assert not (project / ".gitignore").exists()
```

Use `validate_search_request("Service", configured_languages=("python",))` and assert
the PostgreSQL reader returns the synthetic symbol. Assert the target Markdown remains
present and the target status reports fresh/ready.

- [ ] **Step 3: Run the PostgreSQL test and verify the explicit environment gate**

Run: `test -n "${IWIKI_TEST_POSTGRES_DSN:?set IWIKI_TEST_POSTGRES_DSN to a disposable pgvector *_test database}"`

Run: `uv run pytest tests/postgres/test_code_publish_cli.py -v -m postgres_integration`

Expected: no skip and the direct CLI publication test passes against only the validated
`*_test` database.

- [ ] **Step 4: Add direct-target failure and secret-redaction cases**

Seed an old ready revision. Force a batch or finalize failure through a recording wrapper
around the real publisher and assert the reader still reports the old revision and one
abort. Supply sentinel DSN/password/absolute path text in a raised `psycopg.Error` test
double and assert none reaches stdout, stderr, log capture, JSON, or repr.

Add a source scan assertion limited to new CLI/application files:

```python
for path in (Path("src/iwiki_mcp/admin.py"), Path("src/iwiki_mcp/codegraph/application.py")):
    text = path.read_text(encoding="utf-8")
    assert "cursor.execute(" not in text
    assert "psycopg.connect(" not in text
```

This proves the CLI/service uses `PostgresCodeGraphStore` rather than a second raw-SQL
path.

- [ ] **Step 5: Run complete direct PostgreSQL evidence**

Run: `uv run pytest tests/postgres/test_code_publish_cli.py tests/postgres/test_code_graph_contract.py -q -m postgres_integration`

Expected: all selected PostgreSQL/direct contract tests pass with no skips.

- [ ] **Step 6: Commit direct PostgreSQL coverage**

```bash
git add tests/postgres/test_code_publish_cli.py tests/codegraph/synthetic_wiki.py
git commit -m "test(codegraph): cover direct PostgreSQL publication"
```

### Task 7: Prove local hosted MCP HTTP and remote-adapter publication

**Closes:** R-005, R-008, R-010, R-011; implements AC-08, MCP AC-10, and MCP AC-11.

**Files:**
- Create: `tests/postgres/test_code_publish_cli_mcp.py`
- Modify: `tests/codegraph/test_mcp_adapter.py`
- Modify: `tests/codegraph/test_application.py`

- [ ] **Step 1: Add an in-process transport that still calls real hosted JSON-RPC**

Reuse the `_request`/`_open_session` pattern from
`tests/postgres/test_code_graph_contract.py`. The test transport records arguments and
forwards every `wiki_bind`/publication/status call through `TestClient` to the hosted
server:

```python
class InProcessMcpTransport:
    def __init__(self, route, primary):
        self.route = route
        self.primary = primary
        self.calls = []

    def call(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name != "wiki_bind" and not any(
            call[0] == "wiki_bind" for call in self.calls[:-1]
        ):
            bound = self.route.call("wiki_bind", {"primary": self.primary})
            if "error" in bound:
                return bound
            self.calls.insert(-1, ("wiki_bind", {"primary": self.primary}))
        return self.route.call(name, arguments)
```

Patch only `application.RemoteMcpTransport` construction to return this transport. The
publisher remains the production `McpSnapshotPublisher`, and the hosted endpoint remains
the production streamable-HTTP tool surface.

- [ ] **Step 2: Write the synthetic local-HTTP CLI test**

Create a Git-bound synthetic project with `publish_mode = "mcp"` and
`read_mode = "mcp"`. Add synthetic Markdown to `hosted_runtime`'s `wiki-a/docs`, set a
small server `max_batch_rows`, run the CLI, and assert:

```python
assert result["state"] == "ready"
assert result["publish_mode"] == "mcp"
assert route.call("wiki_code_status", {})["snapshot_revision"] == result[
    "snapshot_revision"
]
assert any(name == "wiki_bind" for name, _args in transport.calls)
batch_calls = [args for name, args in transport.calls if name == "wiki_code_publish_batch"]
assert batch_calls
assert all(len(args["rows"]) <= server_limit for args in batch_calls)
```

Search through the real hosted `wiki_code_search` endpoint and assert the synthetic
symbol is returned.

- [ ] **Step 3: Run hosted MCP evidence with the explicit database gate**

Run: `uv run pytest tests/postgres/test_code_publish_cli_mcp.py -v -m postgres_integration`

Expected: no skip; production publisher and hosted tool surface complete a ready
snapshot using server-advertised limits.

- [ ] **Step 4: Add grant denial, failure preservation, and remote redaction tests**

Run once with the writable token to seed the active revision. Run again through a token
without write grant or a transport that rejects a later batch. Assert exit 1, one
redacted JSON object, old status revision unchanged, and best-effort abort after a begun
session. In `test_mcp_adapter.py`, retain remote URL/token sentinel tests and add a
malformed/HTTP failure case that passes through the CLI formatter without exposing
endpoint, token, host, response body, or exception repr.

- [ ] **Step 5: Run MCP adapter, hosted contract, and CLI suites**

Run: `uv run pytest tests/codegraph/test_mcp_adapter.py tests/codegraph/test_application.py -q`

Run: `uv run pytest tests/postgres/test_code_publish_cli_mcp.py tests/postgres/test_code_graph_contract.py -q -m postgres_integration`

Expected: both commands pass; the first is offline, the second uses only the validated
disposable database.

- [ ] **Step 6: Commit hosted and remote MCP coverage**

```bash
git add tests/postgres/test_code_publish_cli_mcp.py tests/codegraph/test_mcp_adapter.py tests/codegraph/test_application.py
git commit -m "test(codegraph): cover MCP publisher CLI"
```

### Task 8: Document scheduled operation and agent verification

**Closes:** R-013 and documentation portions of R-001, R-005, R-009, R-010; implements
AC-13.

**Files:**
- Modify: `README.md:476-590`
- Modify: `docs/README.ru.md:481-598`
- Modify: `docs/architecture.md:118-155`
- Modify: `tests/test_package.py`
- Update through iwiki MCP: `concept/code-graph-publication`
- Update through iwiki MCP: `concept/using-the-server`

- [ ] **Step 1: Write failing documentation contract tests**

Add a test that loads all three repository documents and requires the exact command,
three modes, output flags/exits, project-local PostgreSQL cache, no fallback, and
environment-only secrets. Require README and Russian README to contain `[Unit]`,
`[Service]`, `[Timer]`, `EnvironmentFile`, `OnCalendar`, and a generic CI command, while
asserting no `.service`, `.timer`, or provider workflow file is added by this task.

```python
required = (
    "iwiki-mcp code publish --project",
    "--json",
    "publish_mode",
    "sqlite",
    "postgres",
    "mcp",
    "IWIKI_DB_PASSWORD",
    "IWIKI_CODE_GRAPH_MCP_URL",
    "IWIKI_CODE_GRAPH_MCP_TOKEN",
    "<project>/.iwiki/code-<domain>.sqlite3",
)
for text in (english, russian, architecture):
    assert all(value in text for value in required)
assert not list(Path(".").glob("**/*.service"))
assert not list(Path(".").glob("**/*.timer"))
```

- [ ] **Step 2: Run doc tests and verify missing command guidance**

Run: `uv run pytest tests/test_package.py -k "publisher or docs" -v`

Expected: the new contract test fails because current docs describe only manual
`wiki_code_index` publication.

- [ ] **Step 3: Add English/Russian operator guidance**

Document one command for all three modes, the mode/binding matrix, JSON/text outputs,
exit 0/1/2, secret redaction, local versus remote HTTP equivalence, and verification via
`wiki_code_status` before agents trust code results.

Include copy-ready systemd service/timer text with an external protected environment
file and no embedded secret:

```ini
[Unit]
Description=Publish iwiki code graph snapshot

[Service]
Type=oneshot
WorkingDirectory=/srv/project
EnvironmentFile=/etc/iwiki/codegraph-publisher.env
ExecStart=/usr/local/bin/iwiki-mcp code publish --project /srv/project --json
```

```ini
[Unit]
Description=Schedule iwiki code graph publication

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
```

The generic CI example exports provider-supplied secret variables and executes the same
command; it is documentation text, not a committed workflow artifact.

- [ ] **Step 4: Update architecture and iwiki agent/operator pages**

In `docs/architecture.md`, record the shared application service, source/target split,
and cache locations. Through bound iwiki section mutation tools, update
`concept/code-graph-publication` and `concept/using-the-server` in English. Agent guidance
must say:

1. run or schedule `code publish` on a machine holding the checkout;
2. select exactly one configured mode and never improvise fallback;
3. verify `wiki_code_status.fresh == true` before code-graph search/context;
4. use separate Markdown search when only Wiki semantics are needed;
5. treat unified Wiki/code search as a separate capability until implemented.

Read each PostgreSQL Wiki page immediately before mutation and use revision/section hash
CAS. Run `wiki_lint`; require broken, stale, and missing-source lists to be empty.

- [ ] **Step 5: Run documentation checks**

Run: `uv run pytest tests/test_package.py tests/test_resources.py tests/test_resources_frontmatter.py -q`

Expected: all pass; repository docs describe delivered behavior and no deployment
artifact exists.

- [ ] **Step 6: Commit documentation**

```bash
git add README.md docs/README.ru.md docs/architecture.md tests/test_package.py
git commit -m "docs(codegraph): document scheduled publication"
```

### Task 9: Reconcile versions and run complete verification

**Closes:** R-012 and the complete Done-when clause; produces final result evidence for
AC-01 through AC-13.

**Files:**
- Modify: `pyproject.toml:3`
- Modify: `src/iwiki_mcp/__init__.py:2`
- Modify: `tests/test_package.py:20-27`
- Regenerate: `uv.lock`
- Verify: all changed implementation, tests, docs, iwiki pages, and branch state

- [ ] **Step 1: Align all version surfaces for the implementation change**

Bump patch version from the plan's `0.7.179` to `0.7.180` in `pyproject.toml` and
`src/iwiki_mcp/__init__.py`, update the explicit package-version regression assertion,
then regenerate `uv.lock` with `uv lock`. Assert all four surfaces report `0.7.180`.

- [ ] **Step 2: Run focused offline behavior suites**

Run: `uv run pytest tests/test_code_publish_cli.py tests/codegraph/test_application.py tests/codegraph/test_publish_cli_sqlite.py tests/codegraph/test_server_tools.py tests/codegraph/test_mcp_adapter.py -q`

Expected: zero failures; SQLite CLI, shared orchestration, MCP adapter, legacy tool
payloads, outputs, exits, and redaction all pass.

- [ ] **Step 3: Run all synthetic PostgreSQL and hosted MCP routes**

Run: `test -n "${IWIKI_TEST_POSTGRES_DSN:?set IWIKI_TEST_POSTGRES_DSN to a disposable pgvector *_test database}"`

Run: `uv run pytest tests/postgres/test_code_publish_cli.py tests/postgres/test_code_publish_cli_mcp.py tests/postgres/test_code_graph_contract.py -q -m postgres_integration`

Expected: zero failures and zero skips. Evidence includes real direct PostgreSQL and real
hosted JSON-RPC snapshots, queryability, server limits/grants, and old-revision
preservation.

- [ ] **Step 4: Run full regression without accidental external access**

Run: `env -u IWIKI_TEST_POSTGRES_DSN uv run pytest -q`

Expected: zero failures; PostgreSQL integration tests are skipped by their explicit gate
and no network/database attempt occurs.

- [ ] **Step 5: Run static and package smoke checks**

Run: `uv run flake8 src tests`

Expected: zero lint errors.

Run: `uv run iwiki-mcp code publish --help`

Expected: exit 0; help shows required `--project` and optional `--json` only.

Run: `uv run iwiki-mcp --help`

Expected: exit 0; existing stdio help remains available.

- [ ] **Step 6: Audit diff against every requirement and inspect secrets**

Run `git diff origin/master...HEAD --check`, inspect every changed path, map it to
R-001..R-013, and scan staged/untracked content for credential-like values. Confirm no
schema migration, protocol version, new target, deployment artifact, or unified-search
implementation entered the diff.

- [ ] **Step 7: Commit version and verification surfaces**

```bash
git add pyproject.toml src/iwiki_mcp/__init__.py uv.lock tests/test_package.py
git commit -m "chore(release): bump codegraph publisher version"
```

- [ ] **Step 8: Run the result gate and PR workflow**

Run `$check-chain result docs/superpowers/plans/2026-08-25-codegraph-publisher-cli.md`.
Fix every confirmed finding, rerun affected tests and the result gate, then update the
task page with final evidence and clean wiki lint. After `OK`, use
`superpowers:finishing-a-development-branch` and `git-workflow` to push
`dev-codegraph-publisher-cli` and open a PR against `master`; never merge or push to
`master` directly.

Expected final evidence: clean worktree, result `OK`, lifecycle ready for closeout, CI
commands recorded, and an open PR targeting `master`.
