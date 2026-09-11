import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from iwiki_mcp.codegraph import application
from iwiki_mcp.codegraph import runtime as codegraph_runtime
from iwiki_mcp.codegraph.config import CodeGraphConfig
from iwiki_mcp.codegraph.mcp_adapter import (
    ENDPOINT_ENV,
    McpCodeGraphReader,
    TOKEN_ENV,
)
from iwiki_mcp.codegraph.publication import PublicationSession, SnapshotHeader
from iwiki_mcp.codegraph.query import CodeGraphQueryError
from iwiki_mcp.codegraph.runtime import (
    _BUILD_WORKERS,
    CodeGraphRuntime,
    sanitized_error,
)
from iwiki_mcp.codegraph.store import CodeGraphStore
from iwiki_mcp.storage import GitBinding, PostgresBinding
from iwiki_mcp.specifications import UnavailableSpecificationGraphResolver


_LOCAL_REVISION = "sha256:" + "c" * 64
_REMOTE_REVISION = "sha256:" + "d" * 64
_BASE_REVISION = "sha256:" + "e" * 64
_PAYLOAD_REVISION = "sha256:" + "f" * 64


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


def _git_binding(project: Path) -> GitBinding:
    return GitBinding(
        base=str(project / "wiki"),
        read=("docs",),
        write=("docs",),
        primary="docs",
        project_dir=str(project),
    )


def _exported_snapshot():
    rows = {
        "repositories": [{"repository_id": "project", "state": "ready"}],
        "files": [
            {"file_id": "py:file:0", "repository_id": "project"},
            {"file_id": "py:file:1", "repository_id": "project"},
        ],
        "symbols": [],
        "relations": [],
    }
    header = SnapshotHeader(
        protocol_version=1,
        schema_version=2,
        repository_id="project",
        source_fingerprint="source",
        parser_fingerprint="parser",
        normalizer_version="normalizer-1",
        unicode_data_version="15.1",
        languages=("python",),
        expected_counts={kind: len(value) for kind, value in rows.items()},
        graph_payload_revision=_PAYLOAD_REVISION,
    )
    return header, rows


def _indexed(built, publish):
    """Answer the way the real runtime does: a ready build publishes itself.

    `CodeGraphRuntime.index` hands its build worker the publication callback
    and returns the result under `publication`, so a fake runtime that swallowed
    the callback would let `index_and_publish` pass its "publication" test
    without anything ever publishing.
    """
    if publish is None or built.get("state") != "ready":
        return built
    published = publish()
    return {**built, "publication": published} if published else built


class SnapshotRuntime:
    def __init__(self, exported=None):
        self.exported = _exported_snapshot() if exported is None else exported

    def export_snapshot(self):
        return self.exported


class RecordingPublisher:
    def __init__(
        self,
        *,
        begin_result=None,
        batch_result=None,
        finalize_result=None,
        begin_exception=None,
        batch_exception=None,
        finalize_exception=None,
        abort_exception=None,
        abort_result=None,
    ):
        self.calls = []
        self.abort_result = (
            {"state": "aborted"} if abort_result is None else abort_result
        )
        self.begin_result = begin_result
        self.batch_result = (
            {"accepted": True} if batch_result is None else batch_result
        )
        self.finalize_result = (
            {
                "state": "ready",
                "snapshot_revision": _REMOTE_REVISION,
            }
            if finalize_result is None
            else finalize_result
        )
        self.begin_exception = begin_exception
        self.batch_exception = batch_exception
        self.finalize_exception = finalize_exception
        self.abort_exception = abort_exception

    def begin(self, header):
        self.calls.append(("begin", header.repository_id))
        if self.begin_exception is not None:
            raise self.begin_exception
        if self.begin_result is not None:
            return self.begin_result
        return PublicationSession(
            session_id="session-a",
            lease_expires_at="2026-08-25T00:00:00Z",
            base_snapshot_revision=_BASE_REVISION,
            base_markdown_token=0,
            max_batch_rows=1,
            max_batch_bytes=1_000_000,
        )

    def publish_batch(self, session, batch):
        self.calls.append(
            ("batch", batch.kind, batch.ordinal, batch.row_count)
        )
        if self.batch_exception is not None:
            raise self.batch_exception
        return dict(self.batch_result)

    def finalize(self, session):
        self.calls.append(("finalize", session.session_id))
        if self.finalize_exception is not None:
            raise self.finalize_exception
        return dict(self.finalize_result)

    def abort(self, session):
        self.calls.append(("abort", session.session_id))
        if self.abort_exception is not None:
            raise self.abort_exception
        return dict(self.abort_result)


@pytest.fixture
def snapshot_fixture():
    return SimpleNamespace(
        runtime=SnapshotRuntime(),
        config=CodeGraphConfig(publish_mode="mcp"),
    )


def test_git_source_context_keeps_the_wiki_cache_and_selector(tmp_path):
    project = tmp_path / "project"
    wiki = tmp_path / "wiki"
    project.mkdir()
    wiki.mkdir()
    binding = GitBinding(
        base=str(wiki),
        read=("docs",),
        write=("docs",),
        primary="docs",
        project_dir=str(project),
    )

    source = application.source_context(binding)

    assert source.base == str(wiki)
    assert source.project_dir == str(project)
    assert source.primary == "docs"
    assert source.wiki_base == str(wiki)


def test_specification_resolver_selects_ready_local_sqlite_reader(monkeypatch):
    runtime = SimpleNamespace(
        config=SimpleNamespace(publish_mode="sqlite", max_file_bytes=1024),
        paths=SimpleNamespace(database=Path("graph.sqlite"), lock=Path("graph.lock")),
        _store=object(),
        _context_root=Path("/tmp/project"),
        binding=SimpleNamespace(primary="docs", base="/tmp/wiki"),
        status=lambda: {"state": "ready", "revision": _LOCAL_REVISION},
    )
    sentinel = object()
    monkeypatch.setattr(application, "SqliteCodeGraphReader", lambda **_kwargs: sentinel)

    assert application.specification_graph_resolver(runtime) is sentinel


@pytest.mark.parametrize(
    "publish_mode,status,reason",
    [
        ("mcp", {"state": "ready", "revision": _REMOTE_REVISION}, "source_unavailable"),
        ("sqlite", {"state": "missing", "code": "not_configured"}, "not_configured"),
    ],
)
def test_specification_resolver_fails_soft_without_local_ready_snapshot(
    publish_mode, status, reason
):
    runtime = SimpleNamespace(
        config=SimpleNamespace(publish_mode=publish_mode),
        paths=None,
        _store=None,
        status=lambda: status,
    )

    resolver = application.specification_graph_resolver(runtime)

    assert isinstance(resolver, UnavailableSpecificationGraphResolver)
    assert resolver.status()["reason"] == reason


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


@pytest.mark.parametrize(
    "binding_factory,publish_mode",
    [
        (_git_binding, "sqlite"),
        (_git_binding, "mcp"),
        (_postgres_binding, "postgres"),
        (_postgres_binding, "mcp"),
    ],
)
def test_target_validation_accepts_only_supported_binding_pairs(
    tmp_path, binding_factory, publish_mode
):
    application.validate_target(binding_factory(tmp_path), publish_mode)


@pytest.mark.parametrize(
    "binding_factory,publish_mode,message",
    [
        (
            _postgres_binding,
            "sqlite",
            "sqlite publication requires a Git Wiki binding",
        ),
        (
            _git_binding,
            "postgres",
            "postgres publication requires PostgreSQL storage",
        ),
        (_git_binding, "unknown", "unknown publish mode"),
    ],
)
def test_target_validation_rejects_invalid_pairs(
    tmp_path, binding_factory, publish_mode, message
):
    with pytest.raises(application.CodeGraphApplicationError, match=message):
        application.validate_target(binding_factory(tmp_path), publish_mode)


def test_invalid_target_fails_before_index_or_publisher_selection(
    tmp_path, monkeypatch
):
    calls = []

    class Runtime:
        config = SimpleNamespace(publish_mode="postgres")

        def index(self, **_kwargs):
            calls.append("index")
            return {"state": "ready"}

    monkeypatch.setattr(
        application,
        "code_runtime",
        lambda _source, *, environ=None: Runtime(),
    )
    monkeypatch.setattr(
        application,
        "publisher_for",
        lambda *_args, **_kwargs: calls.append("publisher"),
    )

    with pytest.raises(application.CodeGraphApplicationError):
        application.index_and_publish(_git_binding(tmp_path))

    assert calls == []


def test_sqlite_target_has_no_publisher(tmp_path):
    config = CodeGraphConfig(publish_mode="sqlite")

    assert application.publisher_for(_git_binding(tmp_path), config) is None


def test_postgres_target_uses_exact_store_settings(tmp_path, monkeypatch):
    captured = {}

    class Store:
        def __init__(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    config = CodeGraphConfig(
        publish_mode="postgres",
        publication_session_ttl_seconds=37,
        staging_retention_seconds=91,
        superseded_retention_seconds=53,
        superseded_cleanup_limit=3,
        staging_cleanup_limit=7,
    )
    binding = _postgres_binding(tmp_path)
    monkeypatch.setattr(application, "PostgresCodeGraphStore", Store)
    monkeypatch.setattr(
        PostgresBinding,
        "connection_dsn",
        lambda _binding: "postgresql://fixture",
    )

    publisher = application.create_postgres_publisher(
        binding,
        "owner-a",
        config,
        lock_timeout_ms=123,
    )

    assert isinstance(publisher, Store)
    assert captured == {
        "args": ("postgresql://fixture", "wiki-a", "docs", "owner-a"),
        "kwargs": {
            "lock_timeout_ms": 123,
            "session_ttl_seconds": 37,
            "staging_retention_seconds": 91,
            "staging_cleanup_limit": 7,
            "superseded_retention_seconds": 53,
            "superseded_cleanup_limit": 3,
            "require_database_principal": True,
        },
    }


def test_postgres_target_requires_primary_before_store_creation(
    tmp_path, monkeypatch
):
    binding = _postgres_binding(tmp_path)
    binding = PostgresBinding(
        **{**binding.__dict__, "primary": None}
    )
    monkeypatch.setattr(
        application,
        "PostgresCodeGraphStore",
        lambda *_args, **_kwargs: pytest.fail("store must not be created"),
    )

    with pytest.raises(
        application.CodeGraphApplicationError,
        match="primary domain is required",
    ):
        application.create_postgres_publisher(
            binding, "owner-a", CodeGraphConfig(publish_mode="postgres")
        )


@pytest.mark.parametrize("binding_factory", [_git_binding, _postgres_binding])
def test_mcp_target_uses_only_remote_transport(
    tmp_path, binding_factory, monkeypatch
):
    calls = []

    class Transport:
        def __init__(self, *, environ, primary):
            calls.append(("transport", environ, primary))

    class Publisher:
        def __init__(self, transport):
            calls.append(("publisher", transport))

    environment = {"ENDPOINT": "fixture"}
    monkeypatch.setattr(application, "RemoteMcpTransport", Transport)
    monkeypatch.setattr(application, "McpSnapshotPublisher", Publisher)

    publisher = application.publisher_for(
        binding_factory(tmp_path),
        CodeGraphConfig(publish_mode="mcp"),
        environ=environment,
    )

    assert isinstance(publisher, Publisher)
    assert calls[0] == ("transport", environment, "docs")
    assert calls[1][0] == "publisher"


@pytest.mark.parametrize(
    "reported_rows,reported_bytes,expected_rows,expected_bytes",
    [
        (1000, 1_000_000, 1000, 1_000_000),
        (None, None, 5000, 5_000_000),
        (0, 1_000_000, 5000, 1_000_000),
        (-1, 1_000_000, 5000, 1_000_000),
        (5001, 1_000_000, 5000, 1_000_000),
        (1000, 0, 1000, 5_000_000),
        (1000, 5_000_001, 1000, 5_000_000),
        (True, 1_000_000, 5000, 1_000_000),
    ],
)
def test_server_limits_are_validated_before_batching(
    reported_rows,
    reported_bytes,
    expected_rows,
    expected_bytes,
):
    session = PublicationSession(
        session_id="session-a",
        lease_expires_at="2026-08-25T00:00:00Z",
        base_snapshot_revision=None,
        base_markdown_token=0,
        max_batch_rows=reported_rows,
        max_batch_bytes=reported_bytes,
    )
    config = CodeGraphConfig(
        max_batch_rows=5000,
        max_batch_bytes=5_000_000,
    )

    assert application.effective_batch_bounds(session, config) == (
        expected_rows,
        expected_bytes,
    )


def test_advertised_server_limits_control_every_batch(snapshot_fixture):
    publisher = RecordingPublisher()

    result = application.publish_snapshot(
        snapshot_fixture.runtime, publisher, snapshot_fixture.config
    )

    assert result["state"] == "ready"
    batch_calls = [call for call in publisher.calls if call[0] == "batch"]
    assert len([call for call in batch_calls if call[1] == "files"]) == 2
    assert all(call[3] <= 1 for call in batch_calls)


def test_export_failure_returns_without_opening_session(snapshot_fixture):
    publisher = RecordingPublisher()
    snapshot_fixture.runtime.exported = {
        "error": "store_failed",
        "hint": "inspect local snapshot",
    }

    result = application.publish_snapshot(
        snapshot_fixture.runtime, publisher, snapshot_fixture.config
    )

    assert result == {
        "error": "store_failed",
        "hint": "inspect local snapshot",
    }
    assert publisher.calls == []


def test_begin_failure_returns_without_abort(snapshot_fixture):
    publisher = RecordingPublisher(
        begin_result={"error": "busy", "hint": "retry later"}
    )

    result = application.publish_snapshot(
        snapshot_fixture.runtime, publisher, snapshot_fixture.config
    )

    assert result == {"error": "busy", "hint": "retry later"}
    assert publisher.calls == [("begin", "project")]


def test_begin_exception_reraises_without_abort(snapshot_fixture):
    original = RuntimeError("begin exploded")
    publisher = RecordingPublisher(begin_exception=original)

    with pytest.raises(RuntimeError) as failure:
        application.publish_snapshot(
            snapshot_fixture.runtime, publisher, snapshot_fixture.config
        )

    assert failure.value is original
    assert publisher.calls == [("begin", "project")]


def test_malformed_begin_dict_returns_without_abort(snapshot_fixture):
    publisher = RecordingPublisher(begin_result={})

    result = application.publish_snapshot(
        snapshot_fixture.runtime, publisher, snapshot_fixture.config
    )

    assert result == {}
    assert publisher.calls == [("begin", "project")]


def test_batch_failure_aborts_once_and_never_finalizes(snapshot_fixture):
    publisher = RecordingPublisher(
        batch_result={"error": "batch_conflict", "hint": "begin a new session"}
    )
    result = application.publish_snapshot(
        snapshot_fixture.runtime, publisher, snapshot_fixture.config
    )
    assert result["error"] == "batch_conflict"
    assert [call[0] for call in publisher.calls].count("abort") == 1
    assert all(call[0] != "finalize" for call in publisher.calls)


def test_safe_adapter_failure_preserves_mcp_mode_and_aborts_once(
    tmp_path, monkeypatch
):
    remote_failure = {
        "error": "remote_mcp_failed",
        "reason": "http_status",
        "status": 503,
        "hint": "the remote wiki refused the code graph call; see status",
    }
    publisher = RecordingPublisher(batch_result=remote_failure)

    class Runtime(SnapshotRuntime):
        config = CodeGraphConfig(publish_mode="mcp")

        def index(
            self, *, force=False, languages=None, wait_seconds=None,
            publish=None,
        ):
            return _indexed(
                {"state": "ready", "revision": _LOCAL_REVISION}, publish
            )

    runtime = Runtime()
    monkeypatch.setattr(
        application,
        "code_runtime",
        lambda _source, *, environ=None: runtime,
    )
    monkeypatch.setattr(
        application,
        "publisher_for",
        lambda *_args, **_kwargs: publisher,
    )

    outcome = application.index_and_publish(_git_binding(tmp_path))

    assert outcome.publish_mode == "mcp"
    assert outcome.publication == remote_failure
    assert not outcome.ready
    assert [call[0] for call in publisher.calls].count("abort") == 1
    assert all(call[0] != "finalize" for call in publisher.calls)


def test_batch_rejection_aborts_once_and_never_finalizes(snapshot_fixture):
    rejected = {"accepted": False}
    publisher = RecordingPublisher(
        batch_result=rejected,
        abort_exception=RuntimeError("abort exploded"),
    )

    result = application.publish_snapshot(
        snapshot_fixture.runtime, publisher, snapshot_fixture.config
    )

    assert result == rejected
    assert [call[0] for call in publisher.calls].count("abort") == 1
    assert all(call[0] != "finalize" for call in publisher.calls)


@pytest.mark.parametrize(
    "malformed",
    [{}, {"state": "accepted"}],
    ids=["empty", "missing-accepted"],
)
def test_malformed_batch_result_aborts_once_and_never_finalizes(
    snapshot_fixture, malformed
):
    publisher = RecordingPublisher(batch_result=malformed)

    result = application.publish_snapshot(
        snapshot_fixture.runtime, publisher, snapshot_fixture.config
    )

    assert result == malformed
    assert [call[0] for call in publisher.calls].count("abort") == 1
    assert all(call[0] != "finalize" for call in publisher.calls)


def test_finalize_failure_aborts_once(snapshot_fixture):
    publisher = RecordingPublisher(
        finalize_result={"error": "snapshot_conflict", "hint": "rebuild"}
    )
    result = application.publish_snapshot(
        snapshot_fixture.runtime, publisher, snapshot_fixture.config
    )
    assert result["error"] == "snapshot_conflict"
    assert publisher.calls[-1] == ("abort", "session-a")
    assert [call[0] for call in publisher.calls].count("abort") == 1


def test_non_ready_finalize_result_aborts_once(snapshot_fixture):
    staging = {
        "state": "staging",
        "snapshot_revision": _REMOTE_REVISION,
    }
    publisher = RecordingPublisher(finalize_result=staging)

    result = application.publish_snapshot(
        snapshot_fixture.runtime, publisher, snapshot_fixture.config
    )

    assert result == staging
    assert publisher.calls[-1] == ("abort", "session-a")
    assert [call[0] for call in publisher.calls].count("abort") == 1


@pytest.mark.parametrize(
    "malformed",
    [
        {"state": "ready"},
        {"state": "ready", "snapshot_revision": ""},
        {"state": "ready", "snapshot_revision": 7},
    ],
    ids=["missing", "empty", "non-string"],
)
def test_finalize_ready_without_valid_revision_aborts_once(
    snapshot_fixture, malformed
):
    publisher = RecordingPublisher(
        finalize_result=malformed,
        abort_exception=RuntimeError("abort exploded"),
    )

    result = application.publish_snapshot(
        snapshot_fixture.runtime, publisher, snapshot_fixture.config
    )

    assert result == {"state": "failed", "error": "publication_failed"}
    assert publisher.calls[-1] == ("abort", "session-a")
    assert [call[0] for call in publisher.calls].count("abort") == 1


@pytest.mark.parametrize(
    "revision",
    [
        "sha256:remote",
        "sha256:" + "A" * 64,
        "sha256:" + "a" * 63,
        "https://private.invalid/revision",
        "token-secret\nsha256:" + "a" * 64,
    ],
)
def test_ready_finalize_rejects_noncanonical_revision_and_aborts_once(
    snapshot_fixture, revision
):
    publisher = RecordingPublisher(
        finalize_result={
            "state": "ready",
            "snapshot_revision": revision,
        }
    )

    result = application.publish_snapshot(
        snapshot_fixture.runtime, publisher, snapshot_fixture.config
    )

    assert result == {"state": "failed", "error": "publication_failed"}
    assert publisher.calls[-1] == ("abort", "session-a")
    assert [call[0] for call in publisher.calls].count("abort") == 1
    assert revision not in repr(result)


def test_finalize_the_client_could_not_await_reports_the_activation(
    snapshot_fixture,
):
    """A lost finalize answer is not a failed publication when the target finished it."""
    completed = {
        "state": "ready",
        "snapshot_revision": _REMOTE_REVISION,
        "counts": {"relations": 3},
    }
    publisher = RecordingPublisher(
        finalize_result={
            "error": "remote_mcp_failed",
            "reason": "timeout",
            "hint": "the remote code graph call timed out; retry it",
        },
        abort_result=completed,
    )

    result = application.publish_snapshot(
        snapshot_fixture.runtime, publisher, snapshot_fixture.config
    )

    assert result == completed
    assert [call[0] for call in publisher.calls].count("abort") == 1


def test_abort_reporting_a_noncanonical_revision_keeps_the_failure(
    snapshot_fixture,
):
    rejected = {"error": "remote_mcp_failed", "reason": "timeout", "hint": "retry"}
    publisher = RecordingPublisher(
        finalize_result=rejected,
        abort_result={"state": "ready", "snapshot_revision": "sha256:remote"},
    )

    result = application.publish_snapshot(
        snapshot_fixture.runtime, publisher, snapshot_fixture.config
    )

    assert result == rejected


def test_exact_batch_and_finalize_success_never_aborts(snapshot_fixture):
    publisher = RecordingPublisher(
        batch_result={"accepted": True},
        finalize_result={
            "state": "ready",
            "snapshot_revision": _REMOTE_REVISION,
        },
    )

    result = application.publish_snapshot(
        snapshot_fixture.runtime, publisher, snapshot_fixture.config
    )

    assert result == {
        "state": "ready",
        "snapshot_revision": _REMOTE_REVISION,
    }
    assert publisher.calls[-1] == ("finalize", "session-a")
    assert all(call[0] != "abort" for call in publisher.calls)


def test_code_runtime_lets_runtime_own_the_single_config_load(
    tmp_path, monkeypatch
):
    source = application.CodeGraphSourceContext(
        base=str(tmp_path),
        project_dir=str(tmp_path),
        primary="docs",
        wiki_base=None,
    )
    calls = []

    environment = {"IWIKI_CODE_GRAPH_ENABLED": "true"}

    class Runtime:
        def __init__(self, actual_source, *, adapter_factories, environ=None):
            calls.append((actual_source, adapter_factories, environ))
            self.config = CodeGraphConfig(publish_mode="mcp")
            self._indexer = None

    monkeypatch.setattr(application.codegraph_runtime, "CodeGraphRuntime", Runtime)
    monkeypatch.setattr(
        application.codegraph_config,
        "load_code_graph_config",
        lambda _project: pytest.fail("application loaded config separately"),
    )

    runtime = application.code_runtime(source, environ=environment)

    assert runtime.config.publish_mode == "mcp"
    assert len(calls) == 1
    assert calls[0][2] is environment


@pytest.mark.parametrize("failure_stage", ["batch", "finalize"])
def test_publication_exception_aborts_once_and_reraises_original(
    snapshot_fixture, failure_stage
):
    original = RuntimeError(f"{failure_stage} exploded")
    publisher = RecordingPublisher(
        batch_exception=original if failure_stage == "batch" else None,
        finalize_exception=original if failure_stage == "finalize" else None,
    )

    with pytest.raises(RuntimeError) as failure:
        application.publish_snapshot(
            snapshot_fixture.runtime, publisher, snapshot_fixture.config
        )

    assert failure.value is original
    assert [call[0] for call in publisher.calls].count("abort") == 1


def test_abort_failure_never_replaces_returned_batch_failure(snapshot_fixture):
    publisher = RecordingPublisher(
        batch_result={"error": "batch_conflict", "hint": "restart"},
        abort_exception=RuntimeError("abort exploded"),
    )

    result = application.publish_snapshot(
        snapshot_fixture.runtime, publisher, snapshot_fixture.config
    )

    assert result == {"error": "batch_conflict", "hint": "restart"}
    assert [call[0] for call in publisher.calls].count("abort") == 1


def test_abort_failure_never_replaces_raised_exception(snapshot_fixture):
    original = RuntimeError("batch exploded")
    publisher = RecordingPublisher(
        batch_exception=original,
        abort_exception=RuntimeError("abort exploded"),
    )

    with pytest.raises(RuntimeError) as failure:
        application.publish_snapshot(
            snapshot_fixture.runtime, publisher, snapshot_fixture.config
        )

    assert failure.value is original
    assert [call[0] for call in publisher.calls].count("abort") == 1


@pytest.mark.parametrize(
    "mode,index,publication,ready,revision,tool_result",
    [
        (
            None,
            {"state": "missing", "revision": None},
            {},
            False,
            None,
            {"state": "missing", "revision": None},
        ),
        (
            "sqlite",
            {"state": "ready", "revision": _LOCAL_REVISION},
            {},
            True,
            _LOCAL_REVISION,
            {"state": "ready", "revision": _LOCAL_REVISION},
        ),
        (
            "mcp",
            {"state": "ready", "revision": _LOCAL_REVISION},
            {"state": "ready", "snapshot_revision": _REMOTE_REVISION},
            True,
            _REMOTE_REVISION,
            {
                "state": "ready",
                "revision": _LOCAL_REVISION,
                "publication": {
                    "state": "ready",
                    "snapshot_revision": _REMOTE_REVISION,
                },
            },
        ),
        (
            "postgres",
            {"state": "ready", "revision": _LOCAL_REVISION},
            {"error": "snapshot_conflict"},
            False,
            None,
            {
                "state": "ready",
                "revision": _LOCAL_REVISION,
                "publication": {"error": "snapshot_conflict"},
            },
        ),
    ],
)
def test_outcome_ready_revision_and_tool_result_semantics(
    mode, index, publication, ready, revision, tool_result
):
    outcome = application.CodeGraphPublishOutcome(
        publish_mode=mode,
        index=index,
        publication=publication,
        duration_ms=7,
    )

    assert outcome.ready is ready
    assert outcome.snapshot_revision == revision
    assert outcome.tool_result() == tool_result


def test_non_ready_index_never_selects_or_publishes_target(
    tmp_path, monkeypatch
):
    calls = []

    class Runtime:
        config = CodeGraphConfig(publish_mode="mcp")

        def index(
            self, *, force=False, languages=None, wait_seconds=None,
            publish=None,
        ):
            calls.append(("index", force, languages))
            return _indexed(
                {"state": "failed", "revision": None}, publish
            )

    monkeypatch.setattr(
        application,
        "code_runtime",
        lambda _source, *, environ=None: Runtime(),
    )
    monkeypatch.setattr(
        application,
        "publisher_for",
        lambda *_args, **_kwargs: pytest.fail("publisher must not be selected"),
    )

    outcome = application.index_and_publish(
        _git_binding(tmp_path), force=True, languages=["python"]
    )

    assert calls == [("index", True, ["python"])]
    assert outcome.publish_mode == "mcp"
    assert outcome.publication == {}
    assert not outcome.ready


def test_index_and_publish_threads_wait_seconds_to_the_runtime(
    tmp_path, monkeypatch
):
    calls = []

    class Runtime:
        config = CodeGraphConfig(publish_mode="sqlite")

        def index(
            self, *, force=False, languages=None, wait_seconds=None,
            publish=None,
        ):
            calls.append(("index", force, languages, wait_seconds))
            return _indexed(
                {"state": "ready", "revision": _LOCAL_REVISION}, publish
            )

    monkeypatch.setattr(
        application,
        "code_runtime",
        lambda _source, *, environ=None: Runtime(),
    )

    outcome = application.index_and_publish(
        _git_binding(tmp_path), wait_seconds=3.5
    )

    assert calls == [("index", False, None, 3.5)]
    assert outcome.index == {"state": "ready", "revision": _LOCAL_REVISION}


def test_out_of_range_wait_seconds_propagates_for_the_tool_layer_to_sanitize(
    tmp_path, monkeypatch
):
    # Ruling from review: `index_and_publish` must not hand-build a second
    # error dialect for `CodeGraphQueryError` -- it stays uncaught here (like
    # `CodeGraphConfigError` and friends already are) and falls through to
    # the generic `except Exception: if not redact_failures: raise`, so a
    # direct caller with `redact_failures=False` sees the raised exception.
    # The tool layer (`wiki_code_index`'s `_code_safe` decorator) is what
    # turns it into a sanitized answer -- see the server-level coverage in
    # test_server_tools.py.
    class Runtime:
        config = CodeGraphConfig(publish_mode="sqlite")

        def index(
            self, *, force=False, languages=None, wait_seconds=None,
            publish=None,
        ):
            raise CodeGraphQueryError(
                "wait_seconds must be between 0 and 10",
                parameter="wait_seconds",
            )

    monkeypatch.setattr(
        application,
        "code_runtime",
        lambda _source, *, environ=None: Runtime(),
    )

    with pytest.raises(CodeGraphQueryError):
        application.index_and_publish(_git_binding(tmp_path), wait_seconds=-1)


def test_failed_sqlite_index_does_not_export_or_select_publisher(
    tmp_path, monkeypatch
):
    """Recovery preservation is covered by test_cancellation_before_publication."""
    calls = []

    class Runtime:
        config = CodeGraphConfig(publish_mode="sqlite")

        def index(
            self, *, force=False, languages=None, wait_seconds=None,
            publish=None,
        ):
            calls.append(("index", force, languages))
            return _indexed(
                {"state": "failed", "code": "rebuild_failed"}, publish
            )

        def export_snapshot(self):
            pytest.fail("failed SQLite rebuild must not export a snapshot")

    monkeypatch.setattr(
        application,
        "code_runtime",
        lambda _source, *, environ=None: Runtime(),
    )
    monkeypatch.setattr(
        application,
        "publisher_for",
        lambda *_args, **_kwargs: pytest.fail(
            "failed SQLite rebuild must not create a publisher"
        ),
    )

    outcome = application.index_and_publish(_git_binding(tmp_path))

    assert calls == [("index", False, None)]
    assert outcome.index == {"state": "failed", "code": "rebuild_failed"}
    assert outcome.publication == {}
    assert not outcome.ready
    assert outcome.snapshot_revision is None


def test_sqlite_index_uses_only_atomic_runtime_path(tmp_path, monkeypatch):
    class Runtime:
        config = CodeGraphConfig(publish_mode="sqlite")

        def index(
            self, *, force=False, languages=None, wait_seconds=None,
            publish=None,
        ):
            return _indexed(
                {"state": "ready", "revision": _LOCAL_REVISION}, publish
            )

        def export_snapshot(self):
            raise AssertionError("SQLite must not export a snapshot")

    monkeypatch.setattr(
        application,
        "code_runtime",
        lambda _source, *, environ=None: Runtime(),
    )

    outcome = application.index_and_publish(_git_binding(tmp_path))

    assert outcome.ready
    assert outcome.snapshot_revision == _LOCAL_REVISION
    assert outcome.publication == {}
    assert outcome.tool_result() == {
        "state": "ready",
        "revision": _LOCAL_REVISION,
    }


def test_ready_external_index_publishes_through_selected_target(
    tmp_path, monkeypatch
):
    calls = []
    publisher = RecordingPublisher()

    class Runtime(SnapshotRuntime):
        config = CodeGraphConfig(publish_mode="mcp")

        def index(
            self, *, force=False, languages=None, wait_seconds=None,
            publish=None,
        ):
            calls.append(("index", force, languages))
            return _indexed(
                {"state": "ready", "revision": _LOCAL_REVISION}, publish
            )

    runtime = Runtime()
    monkeypatch.setattr(
        application,
        "code_runtime",
        lambda _source, *, environ=None: (
            calls.append(("runtime", environ)) or runtime
        ),
    )
    monkeypatch.setattr(
        application,
        "publisher_for",
        lambda binding, config, *, environ=None: (
            calls.append(("publisher", binding.storage, config.publish_mode, environ))
            or publisher
        ),
    )
    environment = {"IWIKI_CODE_GRAPH_MCP_URL": "https://example.invalid"}

    outcome = application.index_and_publish(
        _git_binding(tmp_path),
        force=True,
        languages=["python"],
        environ=environment,
    )

    assert calls == [
        ("runtime", environment),
        ("index", True, ["python"]),
        ("publisher", "git", "mcp", environment),
    ]
    assert outcome.ready
    assert outcome.snapshot_revision == _REMOTE_REVISION
    assert outcome.duration_ms >= 0


def test_detached_build_publishes_after_the_caller_took_its_handle(
    seed_runtime, monkeypatch
):
    """R3/F1: a build that outlives `wait_seconds` must still publish.

    The publication used to be the caller's, and ran only when `index`
    returned `ready`. A build that detached returned `rebuilding` instead, so
    under `publish_mode = "mcp"` nothing ever published: the job reached
    `ready`, the caller believed it, and the hosted snapshot stayed the old
    one with no error anywhere. The assertion is against the publisher, not
    the tool answer, because the tool answer is exactly what was not wrong.
    """
    harness = seed_runtime.with_config(
        publish_mode="mcp", max_full_rebuild_seconds=30
    )
    publisher = RecordingPublisher()
    monkeypatch.setattr(
        application, "publisher_for", lambda *_args, **_kwargs: publisher
    )
    real_publish_metadata = CodeGraphStore.publish_metadata

    def slow_publish_metadata(self, *args, **kwargs):
        time.sleep(0.4)
        return real_publish_metadata(self, *args, **kwargs)

    monkeypatch.setattr(
        CodeGraphStore, "publish_metadata", slow_publish_metadata
    )
    domain_key = codegraph_runtime.worker_domain_key(
        application.source_context(harness.binding)
    )

    outcome = application.index_and_publish(
        harness.binding, force=True, wait_seconds=0
    )

    assert outcome.index["state"] == "rebuilding"
    assert outcome.publication == {}
    job_id = outcome.index["job"]["id"]

    _BUILD_WORKERS.join(timeout=30)

    assert [call[0] for call in publisher.calls].count("begin") == 1
    assert [call[0] for call in publisher.calls].count("finalize") == 1
    assert any(call[0] == "batch" for call in publisher.calls)
    assert _BUILD_WORKERS.terminal_by_id(domain_key, job_id).state == "ready"


def test_detached_publication_failure_ends_the_job_as_failed(
    seed_runtime, monkeypatch
):
    """R3/F1: a build that indexed but could not publish is not `ready`.

    The local snapshot is fine; the published one is still the old revision,
    so a caller polling the handle must not be told the build succeeded.
    """
    harness = seed_runtime.with_config(
        publish_mode="mcp", max_full_rebuild_seconds=30
    )
    publisher = RecordingPublisher(
        finalize_result={"error": "snapshot_conflict"}
    )
    monkeypatch.setattr(
        application, "publisher_for", lambda *_args, **_kwargs: publisher
    )
    real_publish_metadata = CodeGraphStore.publish_metadata

    def slow_publish_metadata(self, *args, **kwargs):
        time.sleep(0.4)
        return real_publish_metadata(self, *args, **kwargs)

    monkeypatch.setattr(
        CodeGraphStore, "publish_metadata", slow_publish_metadata
    )
    domain_key = codegraph_runtime.worker_domain_key(
        application.source_context(harness.binding)
    )

    outcome = application.index_and_publish(
        harness.binding, force=True, wait_seconds=0
    )
    job_id = outcome.index["job"]["id"]
    _BUILD_WORKERS.join(timeout=30)

    assert [call[0] for call in publisher.calls].count("finalize") == 1
    assert _BUILD_WORKERS.terminal_by_id(domain_key, job_id).state == "failed"


def test_synchronous_build_still_reports_its_publication(
    seed_runtime, monkeypatch
):
    """The waiting caller reads the publication out of the build's answer.

    Same single path as the detached case -- the build publishes and reports
    what happened -- so the outcome a caller that waited sees is unchanged:
    the publication result, and the revision it activated.
    """
    harness = seed_runtime.with_config(publish_mode="mcp")
    publisher = RecordingPublisher()
    monkeypatch.setattr(
        application, "publisher_for", lambda *_args, **_kwargs: publisher
    )
    domain_key = codegraph_runtime.worker_domain_key(
        application.source_context(harness.binding)
    )

    outcome = application.index_and_publish(harness.binding, force=True)

    assert outcome.index["state"] == "ready"
    assert "publication" not in outcome.index
    assert outcome.publication["state"] == "ready"
    assert outcome.ready
    assert outcome.snapshot_revision == _REMOTE_REVISION
    assert _BUILD_WORKERS.terminal_by_id(
        domain_key, outcome.index["job"]["id"]
    ).state == "ready"


# -- read_mode routing (R6) ------------------------------------------------

_INVALID_READ_MODE = {
    "error": "code graph configuration is invalid",
    "code": "invalid_config",
    "field": "read_mode",
    "hint": "inspect code_graph project configuration",
}


def _read_mode_project(
    tmp_path: Path, read_mode: str | None = None, *, enabled: bool = True
) -> GitBinding:
    """Write one real project config and bind it to a Git wiki base."""
    project = tmp_path / "project"
    wiki = tmp_path / "wiki"
    project.mkdir(exist_ok=True)
    (wiki / "docs").mkdir(parents=True, exist_ok=True)
    lines = [
        f"base = {json.dumps(str(wiki))}",
        'read = ["docs"]',
        'write = ["docs"]',
        'primary = "docs"',
        "",
        "[code_graph]",
        f"enabled = {str(enabled).lower()}",
        'languages = ["python"]',
        'auto_rebuild = "off"',
    ]
    if read_mode is not None:
        lines.append(f"read_mode = {json.dumps(read_mode)}")
    project.joinpath(".iwiki.toml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return GitBinding(
        base=str(wiki),
        read=("docs",),
        write=("docs",),
        primary="docs",
        project_dir=str(project),
    )


@pytest.mark.parametrize("read_mode", [None, "sqlite"])
def test_sqlite_read_mode_keeps_the_local_runtime(tmp_path, read_mode):
    binding = _read_mode_project(tmp_path, read_mode)

    reader = application.code_reader(binding)

    assert isinstance(reader, CodeGraphRuntime)
    assert reader.config.read_mode == "sqlite"


@pytest.mark.parametrize("read_mode", ["postgres", "mcp"])
def test_disabled_code_graph_never_leaves_the_local_runtime(
    tmp_path, read_mode, monkeypatch
):
    monkeypatch.setenv(ENDPOINT_ENV, "https://wiki.example/mcp")
    monkeypatch.setenv(TOKEN_ENV, "fixture-bearer-not-a-real-token")
    binding = _read_mode_project(tmp_path, read_mode, enabled=False)

    reader = application.code_reader(binding)

    assert isinstance(reader, CodeGraphRuntime)
    assert reader.status()["code"] == "not_configured"


def test_postgres_read_mode_on_a_git_binding_names_read_mode(tmp_path):
    """`read_mode = "postgres"` needs PostgreSQL storage; no DSN is consulted."""
    binding = _read_mode_project(tmp_path, "postgres")

    with pytest.raises(application.CodeGraphReadModeError) as failure:
        application.code_reader(binding)

    assert sanitized_error(failure.value) == _INVALID_READ_MODE


@pytest.mark.parametrize("present", [(), (ENDPOINT_ENV,), (TOKEN_ENV,)])
def test_mcp_read_mode_without_credentials_names_read_mode(
    tmp_path, monkeypatch, present
):
    for name in (ENDPOINT_ENV, TOKEN_ENV):
        monkeypatch.delenv(name, raising=False)
    for name in present:
        monkeypatch.setenv(name, "fixture-value-not-a-real-secret")
    binding = _read_mode_project(tmp_path, "mcp")

    with pytest.raises(application.CodeGraphReadModeError) as failure:
        application.code_reader(binding)

    assert sanitized_error(failure.value) == _INVALID_READ_MODE
    assert "fixture-value-not-a-real-secret" not in str(failure.value)


def test_mcp_read_mode_selects_the_remote_transit_reader(tmp_path, monkeypatch):
    monkeypatch.setenv(ENDPOINT_ENV, "https://wiki.example/mcp")
    monkeypatch.setenv(TOKEN_ENV, "fixture-bearer-not-a-real-token")
    binding = _read_mode_project(tmp_path, "mcp")

    reader = application.code_reader(binding)

    assert not isinstance(reader, CodeGraphRuntime)
    assert isinstance(reader._reader, McpCodeGraphReader)
    assert reader._reader._transport._primary == "docs"


def test_postgres_read_mode_uses_exact_direct_reader_settings(
    tmp_path, monkeypatch
):
    captured = {}

    class Reader:
        def __init__(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    project = tmp_path / "project"
    project.mkdir()
    project.joinpath(".iwiki.toml").write_text(
        "[code_graph]\nread_mode = \"postgres\"\n"
        "max_snapshot_age_seconds = 41\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        application.wiki_base,
        "ensure_graph_store_excluded",
        lambda _value: True,
    )
    monkeypatch.setattr(application, "PostgresCodeGraphReader", Reader)
    monkeypatch.setattr(
        PostgresBinding,
        "connection_dsn",
        lambda _binding: "postgresql://fixture",
    )

    reader = application.code_reader(_postgres_binding(project))

    assert isinstance(reader._reader, Reader)
    assert captured == {
        "args": ("postgresql://fixture", "wiki-a", "docs"),
        "kwargs": {"max_snapshot_age_seconds": 41},
    }


def test_published_snapshot_reader_exposes_reads_only(tmp_path, monkeypatch):
    """A non-sqlite reader carries no build or rebuild-guard entry point."""
    monkeypatch.setenv(ENDPOINT_ENV, "https://wiki.example/mcp")
    monkeypatch.setenv(TOKEN_ENV, "fixture-bearer-not-a-real-token")
    binding = _read_mode_project(tmp_path, "mcp")

    reader = application.code_reader(binding)

    assert isinstance(reader, application.PublishedSnapshotReader)
    assert not hasattr(reader, "query_guard")
    assert not hasattr(reader, "index")
    assert not hasattr(reader, "export_snapshot")


# -- remote reads are scoped by the snapshot, not by the local config -------


def _two_language_config() -> CodeGraphConfig:
    return CodeGraphConfig(languages=("python", "typescript"))


class _RecordingTransport:
    """Capture the exact remote payload without opening a session."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool: str, payload: dict) -> dict:
        self.calls.append((tool, payload))
        return {"state": "ready", "fresh": True, "results": []}


def test_mcp_read_omits_languages_when_the_caller_named_none():
    """A local `languages` config is not a filter the caller asked for."""
    transport = _RecordingTransport()
    reader = application.PublishedSnapshotReader(
        McpCodeGraphReader(transport), _two_language_config()
    )

    reader.search("needle")

    assert "languages" not in transport.calls[0][1]


def test_mcp_read_forwards_the_languages_the_caller_named():
    transport = _RecordingTransport()
    reader = application.PublishedSnapshotReader(
        McpCodeGraphReader(transport), _two_language_config()
    )

    reader.search("needle", languages=["typescript"])

    assert transport.calls[0][1]["languages"] == ["typescript"]


class _RecordingPostgresReader:
    """Stand in for `PostgresCodeGraphReader.search`'s two accepted shapes."""

    def __init__(self, snapshot_languages: tuple[str, ...]) -> None:
        self._snapshot_languages = snapshot_languages
        self.request = None

    def search(self, request):
        self.request = (
            request(self._snapshot_languages) if callable(request) else request
        )
        return {"state": "ready", "fresh": True, "results": []}


def test_postgres_read_scopes_languages_to_the_published_snapshot():
    """A python-only snapshot must not receive the project's second language.

    Sending it makes the remote's snapshot-scoped validator refuse every
    search with `unsupported_language`, even though the caller filtered
    nothing.
    """
    wrapped = _RecordingPostgresReader(("python",))
    reader = application.PublishedSnapshotReader(
        wrapped, _two_language_config(), snapshot_scoped_languages=True
    )

    reader.search("needle")

    assert wrapped.request.languages == ("python",)


def test_postgres_read_forwards_the_languages_the_caller_named():
    from iwiki_mcp.codegraph.query import CodeGraphLanguageUnavailableError

    wrapped = _RecordingPostgresReader(("python",))
    reader = application.PublishedSnapshotReader(
        wrapped, _two_language_config(), snapshot_scoped_languages=True
    )

    reader.search("needle", languages=["python"])
    assert wrapped.request.languages == ("python",)

    with pytest.raises(CodeGraphLanguageUnavailableError):
        reader.search("needle", languages=["typescript"])


# -- R4's non-ready contract holds on the local read_mode routes -----------


class _NonReadyReader:
    """Answer every read with one fixed non-ready payload."""

    def __init__(self, answer: dict) -> None:
        self._answer = answer

    def status(self):
        return dict(self._answer)

    def search(self, request):
        request = request(("python",)) if callable(request) else request
        return {**self._answer, "results": []}

    def context(self, request):
        return {
            **self._answer,
            "seeds": list(request.seeds),
            "nodes": [],
            "relations": [],
            "files": [],
            "wiki_pages": [],
            "warnings": [],
        }


_NON_READY_ANSWERS = {
    "missing_snapshot": {
        "domain": "docs", "state": "missing", "fresh": False,
        "error": "missing_snapshot",
    },
    "stale_snapshot": {
        "domain": "docs", "state": "ready", "fresh": False,
        "error": "stale_snapshot",
    },
    "remote_mcp_failed": {
        "domain": "docs", "state": "missing", "fresh": False,
        "error": "remote_mcp_failed", "reason": "timeout",
        "hint": "the remote code graph call timed out; retry it",
    },
}


@pytest.mark.parametrize("token", sorted(_NON_READY_ANSWERS))
@pytest.mark.parametrize("call", ["status", "search", "context"])
def test_published_snapshot_reader_normalizes_non_ready_answers(token, call):
    """Spec R4: `error` is the message, `code` the machine token, plus a hint.

    The wrapped snapshot readers put the machine token in `error` and
    carry no `code`. S6 made those answers reachable from a local server
    through one config key, so without normalization the same tool
    returns two incompatible non-ready shapes on one machine.
    """
    reader = application.PublishedSnapshotReader(
        _NonReadyReader(_NON_READY_ANSWERS[token]), _two_language_config()
    )
    arguments = {
        "status": (),
        "search": ("needle",),
        "context": (["py:file:" + "0" * 64],),
    }

    answer = getattr(reader, call)(*arguments[call])

    assert answer["code"] == token
    assert answer["error"] != token and answer["error"]
    assert answer["hint"]
    assert answer["fresh"] is False
    assert answer.get("results", []) == []
    assert answer.get("nodes", []) == []


def test_published_snapshot_reader_preserves_a_compliant_answer():
    """A remote answer that already names its code is left exactly alone."""
    compliant = {
        "domain": "docs", "state": "missing", "fresh": False,
        "error": "code graph is stale", "code": "stale",
        "hint": "run wiki_code_index",
    }
    reader = application.PublishedSnapshotReader(
        _NonReadyReader(compliant), _two_language_config()
    )

    assert reader.status() == compliant


def test_published_snapshot_reader_passes_a_ready_answer_through():
    ready = {"domain": "docs", "state": "ready", "fresh": True}
    reader = application.PublishedSnapshotReader(
        _NonReadyReader(ready), _two_language_config()
    )

    assert reader.status() == ready
    assert reader.search("needle") == {**ready, "results": []}
