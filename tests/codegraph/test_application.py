from pathlib import Path
from types import SimpleNamespace

import pytest

from iwiki_mcp.codegraph import application
from iwiki_mcp.codegraph.config import CodeGraphConfig
from iwiki_mcp.codegraph.publication import PublicationSession, SnapshotHeader
from iwiki_mcp.storage import GitBinding, PostgresBinding


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
    ):
        self.calls = []
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
        return {"state": "aborted"}


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

        def index(self, *, force=False, languages=None):
            calls.append(("index", force, languages))
            return {"state": "failed", "revision": None}

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


def test_failed_sqlite_index_does_not_export_or_select_publisher(
    tmp_path, monkeypatch
):
    """Recovery preservation is covered by test_cancellation_before_publication."""
    calls = []

    class Runtime:
        config = CodeGraphConfig(publish_mode="sqlite")

        def index(self, *, force=False, languages=None):
            calls.append(("index", force, languages))
            return {"state": "failed", "code": "rebuild_failed"}

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

        def index(self, *, force=False, languages=None):
            return {"state": "ready", "revision": _LOCAL_REVISION}

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

        def index(self, *, force=False, languages=None):
            calls.append(("index", force, languages))
            return {"state": "ready", "revision": _LOCAL_REVISION}

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
