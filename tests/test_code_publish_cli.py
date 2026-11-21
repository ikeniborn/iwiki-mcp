"""Public code graph publish CLI contract."""
from __future__ import annotations

from io import StringIO
import json
import os
import select
import socket
from pathlib import Path
import subprocess
import sys
import threading
import traceback

import psycopg
import pytest

from iwiki_mcp import admin, base, server
from iwiki_mcp.codegraph import application, indexer
from iwiki_mcp.codegraph.application import CodeGraphPublishOutcome
from iwiki_mcp.codegraph.config import CodeGraphConfig, CodeGraphConfigError
from iwiki_mcp.codegraph.mcp_adapter import CodeGraphAdapterError
from tests.codegraph.synthetic_wiki import create_sqlite_project


_LOCAL_REVISION = "sha256:" + "c" * 64
_REMOTE_REVISION = "sha256:" + "d" * 64


def _outcome(
    *,
    mode: str = "mcp",
    index_state: str = "ready",
    publication_state: str = "ready",
) -> CodeGraphPublishOutcome:
    return CodeGraphPublishOutcome(
        publish_mode=mode,
        index={
            "state": index_state,
            "counts": {"files": 1, "symbols": 2, "relations": 3},
            **({"revision": _LOCAL_REVISION} if mode == "sqlite" else {}),
        },
        publication={
            "state": publication_state,
            "snapshot_revision": _REMOTE_REVISION,
        },
        duration_ms=17,
    )


def _run(argv, monkeypatch, value):
    stdout = StringIO()
    stderr = StringIO()

    if isinstance(value, BaseException):
        def publish(*_args, **_kwargs):
            raise value
    else:
        def publish(*_args, **_kwargs):
            return value

    monkeypatch.setattr(application, "publish_project", publish)
    monkeypatch.setattr(
        admin,
        "_service",
        lambda *_args, **_kwargs: pytest.fail("PostgreSQL admin service created"),
    )
    code = admin.run(
        argv,
        stdout=stdout,
        stderr=stderr,
        environ={"SENTINEL_ENV": "sentinel-secret"},
    )
    return code, stdout.getvalue(), stderr.getvalue()


def _run_post_resolution_failure(
    argv,
    monkeypatch,
    *,
    mode,
    failure,
):
    stdout = StringIO()
    stderr = StringIO()
    binding = object()
    monkeypatch.setattr(
        application,
        "checkout_root",
        lambda _value: Path("/safe/project"),
    )
    monkeypatch.setattr(
        application.wiki_base,
        "resolve_storage_binding",
        lambda _value, *, environ=None: binding,
    )
    monkeypatch.setattr(application, "source_context", lambda _binding: object())
    monkeypatch.setattr(application, "validate_target", lambda *_args: None)
    # The publisher is selected before the build, so this bare `object()`
    # binding would not survive a real selection. The subject here is what the
    # *index* step's failure becomes, so keep the selection inert.
    monkeypatch.setattr(
        application, "publisher_for", lambda *_args, **_kwargs: object()
    )

    class Runtime:
        config = CodeGraphConfig(publish_mode=mode)

        def index(
            self, *, force=False, languages=None, wait_seconds=None,
            publish=None, _observe_job=None,
        ):
            raise failure

    monkeypatch.setattr(
        application,
        "code_runtime",
        lambda _source, *, environ=None: Runtime(),
    )
    code = admin.run(
        argv,
        stdout=stdout,
        stderr=stderr,
        environ={},
    )
    return code, stdout.getvalue(), stderr.getvalue()


def test_text_success_is_one_concise_stdout_line(monkeypatch):
    code, stdout, stderr = _run(
        ["code", "publish", "--project", "/repo"],
        monkeypatch,
        _outcome(),
    )

    assert code == 0
    assert stdout == (
        f"code graph ready mode=mcp revision={_REMOTE_REVISION} "
        "files=1 symbols=2 relations=3 duration_ms=17\n"
    )
    assert stderr == ""


def test_json_success_is_exactly_one_compact_object(monkeypatch):
    code, stdout, stderr = _run(
        ["code", "publish", "--project", "/repo", "--json"],
        monkeypatch,
        _outcome(),
    )

    assert code == 0
    assert stdout == (
        '{"state":"ready","publish_mode":"mcp",'
        f'"snapshot_revision":"{_REMOTE_REVISION}",'
        '"counts":{"files":1,"symbols":2,"relations":3},'
        '"duration_ms":17}\n'
    )
    assert stderr == ""
    assert json.loads(stdout)["state"] == "ready"


@pytest.mark.parametrize(
    "argv",
    [
        ["code", "publish"],
        ["code", "publish", "--json"],
        ["code", "publish", "--project"],
    ],
)
def test_missing_or_incomplete_project_is_usage_failure(argv):
    stdout = StringIO()
    stderr = StringIO()

    code = admin.run(argv, stdout=stdout, stderr=stderr, environ={})

    assert code == 2
    if "--json" in argv:
        assert stdout.getvalue() == (
            '{"state":"failed","publish_mode":null,'
            '"error":"invalid_usage","duration_ms":0}\n'
        )
        assert stderr.getvalue() == ""
    else:
        assert stdout.getvalue() == ""
        assert stderr.getvalue() == (
            "iwiki-mcp: invalid code publish usage (code=invalid_usage)\n"
        )


@pytest.mark.parametrize(
    "option",
    [
        "--target",
        "--force",
        "--languages",
        "--url",
        "--token",
        "--dsn",
        "--user",
        "--password",
        "--fallback",
        "--config",
    ],
)
def test_every_unapproved_option_is_rejected_without_echo(option):
    sentinel = "sentinel-secret-value"
    stdout = StringIO()
    stderr = StringIO()

    code = admin.run(
        [
            "code", "publish", "--project", "/repo", "--json",
            option, sentinel,
        ],
        stdout=stdout,
        stderr=stderr,
        environ={},
    )

    assert code == 2
    assert stdout.getvalue() == (
        '{"state":"failed","publish_mode":null,'
        '"error":"invalid_usage","duration_ms":0}\n'
    )
    assert stderr.getvalue() == ""
    assert option not in stdout.getvalue() + stderr.getvalue()
    assert sentinel not in stdout.getvalue() + stderr.getvalue()


def test_text_usage_is_one_stable_line_without_argparse_output():
    stdout = StringIO()
    stderr = StringIO()

    code = admin.run(
        ["code", "publish", "--project", "/repo", "--unknown"],
        stdout=stdout,
        stderr=stderr,
        environ={},
    )

    assert code == 2
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == (
        "iwiki-mcp: invalid code publish usage (code=invalid_usage)\n"
    )


@pytest.mark.parametrize(
    "failure",
    [
        base.BaseError("sentinel-secret"),
        CodeGraphConfigError("sentinel-secret"),
        application.CodeGraphApplicationError("sentinel-secret"),
    ],
)
@pytest.mark.parametrize("json_output", [False, True])
def test_expected_configuration_failures_are_redacted(
    monkeypatch, failure, json_output
):
    argv = ["code", "publish", "--project", "/secret/project"]
    if json_output:
        argv.append("--json")
    code, stdout, stderr = _run(
        argv,
        monkeypatch,
        failure,
    )

    assert code == 2
    if json_output:
        assert stdout == (
            '{"state":"failed","publish_mode":null,'
            '"error":"invalid_config","duration_ms":0}\n'
        )
        assert stderr == ""
    else:
        assert stdout == ""
        assert stderr == (
            "iwiki-mcp: code graph configuration failed "
            "(code=invalid_config)\n"
        )
    assert "sentinel-secret" not in stdout + stderr
    assert "/secret/project" not in stdout + stderr


def test_real_invalid_code_graph_config_is_exit_two_json(
    monkeypatch, tmp_path
):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(
        ["git", "init", str(checkout)],
        check=True,
        capture_output=True,
        text=True,
    )
    wiki = tmp_path / "wiki"
    (wiki / "docs").mkdir(parents=True)
    (wiki / "docs" / "page.md").write_text("# Page\n", encoding="utf-8")
    (checkout / ".iwiki.toml").write_text(
        f'base = "{wiki}"\n'
        'read = ["docs"]\n'
        'write = ["docs"]\n'
        'primary = "docs"\n'
        "[code_graph]\n"
        'publish_mode = "sentinel-invalid-mode"\n',
        encoding="utf-8",
    )

    stdout = StringIO()
    stderr = StringIO()
    code = admin.run(
        ["code", "publish", "--project", str(checkout), "--json"],
        stdout=stdout,
        stderr=stderr,
        environ={},
    )

    assert code == 2
    assert stdout.getvalue() == (
        '{"state":"failed","publish_mode":null,'
        '"error":"invalid_config","duration_ms":0}\n'
    )
    assert stderr.getvalue() == ""
    assert "sentinel-invalid-mode" not in stdout.getvalue()
    assert str(checkout) not in stdout.getvalue()


@pytest.mark.parametrize(
    ("mode", "failure", "exit_code", "stable_code"),
    [
        (
            "mcp",
            CodeGraphAdapterError("sentinel-adapter-token"),
            2,
            "invalid_config",
        ),
        (
            "sqlite",
            application.CodeGraphApplicationError("sentinel-application-path"),
            2,
            "invalid_config",
        ),
        (
            "postgres",
            psycopg.Error("sentinel-postgres-dsn"),
            1,
            "internal_error",
        ),
        (
            "postgres",
            RuntimeError("sentinel-publication-url"),
            1,
            "internal_error",
        ),
    ],
)
@pytest.mark.parametrize("json_output", [False, True])
def test_post_resolution_failures_preserve_safe_selected_mode(
    monkeypatch,
    mode,
    failure,
    exit_code,
    stable_code,
    json_output,
):
    argv = ["code", "publish", "--project", "/ignored"]
    if json_output:
        argv.append("--json")

    code, stdout, stderr = _run_post_resolution_failure(
        argv,
        monkeypatch,
        mode=mode,
        failure=failure,
    )

    assert code == exit_code
    if json_output:
        assert stdout == (
            '{"state":"failed",'
            f'"publish_mode":"{mode}",'
            f'"error":"{stable_code}","duration_ms":0}}\n'
        )
        assert stderr == ""
    else:
        assert stdout == ""
        assert stderr.count("\n") == 1
        assert f"(code={stable_code})" in stderr
    emitted = stdout + stderr
    assert "sentinel" not in emitted
    assert "Traceback" not in emitted


@pytest.mark.parametrize("json_output", [False, True])
def test_pre_resolution_runtime_failure_is_redacted_internal_error(
    monkeypatch, json_output
):
    argv = ["code", "publish", "--project", "/repo"]
    if json_output:
        argv.append("--json")
    code, stdout, stderr = _run(
        argv,
        monkeypatch,
        RuntimeError("sentinel-runtime-error"),
    )

    assert code == 1
    if json_output:
        assert stdout == (
            '{"state":"failed","publish_mode":null,'
            '"error":"internal_error","duration_ms":0}\n'
        )
        assert stderr == ""
    else:
        assert stdout == ""
        assert stderr == (
            "iwiki-mcp: code graph publication failed "
            "(code=internal_error)\n"
        )
    assert "sentinel" not in stdout + stderr
    assert "Traceback" not in stdout + stderr


@pytest.mark.parametrize(
    ("outcome", "stable_code", "line"),
    [
        (
            _outcome(index_state="failed"),
            "index_failed",
            "iwiki-mcp: code graph indexing failed (code=index_failed)\n",
        ),
        (
            _outcome(publication_state="failed"),
            "publication_failed",
            (
                "iwiki-mcp: code graph publication failed "
                "(code=publication_failed)\n"
            ),
        ),
    ],
)
def test_non_ready_text_outcomes_are_stable_runtime_failures(
    monkeypatch, outcome, stable_code, line
):
    code, stdout, stderr = _run(
        ["code", "publish", "--project", "/repo"],
        monkeypatch,
        outcome,
    )

    assert code == 1
    assert stdout == ""
    assert stderr == line
    assert stable_code in stderr


def test_json_publication_failure_keeps_selected_mode(monkeypatch):
    code, stdout, stderr = _run(
        ["code", "publish", "--project", "/repo", "--json"],
        monkeypatch,
        _outcome(publication_state="failed"),
    )

    assert code == 1
    assert stdout == (
        '{"state":"failed","publish_mode":"mcp",'
        '"error":"publication_failed","duration_ms":17}\n'
    )
    assert stderr == ""


def test_json_index_failure_keeps_selected_mode(monkeypatch):
    code, stdout, stderr = _run(
        ["code", "publish", "--project", "/repo", "--json"],
        monkeypatch,
        _outcome(index_state="failed"),
    )

    assert code == 1
    assert stdout == (
        '{"state":"failed","publish_mode":"mcp",'
        '"error":"index_failed","duration_ms":17}\n'
    )
    assert stderr == ""


def test_failure_output_never_contains_connection_secrets(
    monkeypatch, caplog
):
    secret_text = (
        "token=sentinel-token password=sentinel-password "
        "postgresql://sentinel-user:sentinel-password@db.invalid/wiki "
        "https://api.invalid/private /absolute/secret/cache"
    )
    code, stdout, stderr = _run(
        ["code", "publish", "--project", "/absolute/secret/cache", "--json"],
        monkeypatch,
        RuntimeError(secret_text),
    )

    assert code == 1
    emitted = stdout + stderr + caplog.text + repr(json.loads(stdout))
    for sentinel in (
        "sentinel-token",
        "sentinel-password",
        "postgresql://",
        "https://api.invalid/private",
        "/absolute/secret/cache",
        "Traceback",
    ):
        assert sentinel not in emitted


def test_publish_project_resolves_root_and_delegates(monkeypatch, tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(
        ["git", "init", str(checkout)],
        check=True,
        capture_output=True,
        text=True,
    )
    binding = object()
    outcome = _outcome()
    calls = []
    environ = {"EXAMPLE": "value"}
    monkeypatch.setattr(
        application.wiki_base,
        "resolve_storage_binding",
        lambda value, *, environ=None: (
            calls.append(("binding", value, environ)) or binding
        ),
    )
    monkeypatch.setattr(
        application,
        "index_and_publish",
        lambda value, *, environ=None, redact_failures=False: (
            calls.append(("publish", value, environ, redact_failures)) or outcome
        ),
    )

    result = application.publish_project(str(checkout), environ=environ)

    assert result is outcome
    assert calls == [
        ("binding", str(checkout.absolute()), environ),
        ("publish", binding, environ, True),
    ]


def test_publish_project_redacted_failure_has_no_raw_exception_chain(
    monkeypatch, tmp_path
):
    secret = "sentinel-token://user:password@private.invalid/absolute/path"
    binding = object()
    monkeypatch.setattr(
        application,
        "checkout_root",
        lambda _value: tmp_path,
    )
    monkeypatch.setattr(
        application.wiki_base,
        "resolve_storage_binding",
        lambda _value, *, environ=None: binding,
    )
    monkeypatch.setattr(application, "source_context", lambda _binding: object())

    class Runtime:
        config = CodeGraphConfig(publish_mode="mcp")

        def index(
            self, *, force=False, languages=None, wait_seconds=None,
            publish=None,
        ):
            raise RuntimeError(secret)

    monkeypatch.setattr(
        application,
        "code_runtime",
        lambda _source, *, environ=None: Runtime(),
    )

    with pytest.raises(application.CodeGraphPublishError) as caught:
        application.publish_project(str(tmp_path), environ={})

    failure = caught.value
    rendered = "".join(traceback.format_exception(failure))
    inspected = rendered + repr(failure) + str(failure)
    assert secret not in inspected
    assert failure.__cause__ is None
    assert failure.__context__ is None
    assert failure.publish_mode == "mcp"
    assert failure.category == "internal"


def test_checkout_root_rejects_non_root_and_symlink(tmp_path):
    checkout = tmp_path / "checkout"
    child = checkout / "child"
    child.mkdir(parents=True)
    subprocess.run(
        ["git", "init", str(checkout)],
        check=True,
        capture_output=True,
        text=True,
    )
    link = tmp_path / "linked-checkout"
    link.symlink_to(checkout, target_is_directory=True)

    for invalid in (child, link, tmp_path / "not-a-checkout"):
        with pytest.raises(
            application.CodeGraphApplicationError,
            match="^project must be a Git checkout root$",
        ):
            application.checkout_root(str(invalid))


def test_checkout_root_redacts_git_execution_failure(monkeypatch):
    monkeypatch.setattr(
        application.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("secret path")),
    )

    with pytest.raises(
        application.CodeGraphApplicationError,
        match="^project must be a Git checkout root$",
    ):
        application.checkout_root("/secret/path")


def test_server_main_routes_code_publish_without_starting_stdio(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server.sys,
        "argv",
        ["iwiki-mcp", "code", "publish", "--project", "/repo"],
    )
    monkeypatch.setattr(
        admin,
        "run",
        lambda argv: calls.append(argv) or 7,
    )
    monkeypatch.setattr(
        server.mcp,
        "run",
        lambda: pytest.fail("mcp.run called"),
    )

    with pytest.raises(SystemExit) as caught:
        server.main()

    assert caught.value.code == 7
    assert calls == [["code", "publish", "--project", "/repo"]]


def test_code_command_is_recognized_as_admin_route():
    assert admin.is_admin_command(
        ["code", "publish", "--project", str(Path("/repo"))]
    )


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("publication_fails", [False, True])
def test_publish_flushes_progress_to_pipe_before_worker_finishes(
    tmp_path, monkeypatch, json_output, publication_fails,
):
    project = create_sqlite_project(tmp_path)
    if publication_fails:
        config = project / ".iwiki.toml"
        config.write_text(config.read_text().replace(
            'publish_mode = "sqlite"', 'publish_mode = "mcp"'
        ))

        class RejectedPublisher:
            def begin(self, header):
                raise RuntimeError("private-token-and-endpoint")

        monkeypatch.setattr(application, "publisher_for", lambda *a, **kw: RejectedPublisher())

    entered, release = threading.Event(), threading.Event()
    discover = indexer.discover_sources

    def hold_discovery(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return discover(*args, **kwargs)

    monkeypatch.setattr(indexer, "discover_sources", hold_discovery)
    argv = ["code", "publish", "--project", str(project)]
    if json_output:
        argv.append("--json")
    stdout = StringIO()
    read_fd, write_fd = os.pipe()
    codes = []
    with os.fdopen(read_fd, "rb", buffering=0) as reader, os.fdopen(write_fd, "w") as stderr:
        cli = threading.Thread(
            target=lambda: codes.append(admin.run(argv, stdout=stdout, stderr=stderr, environ={}))
        )
        cli.start()
        try:
            assert entered.wait(5)
            assert select.select([reader], [], [], 1)[0], "no flushed progress while worker held"
            startup = reader.readline().decode()
            assert "state=running phase=queued" in startup
            assert stdout.getvalue() == ""
        finally:
            release.set()
            cli.join(10)
        assert not cli.is_alive()
        stderr.close()
        lines = (startup + reader.read().decode()).splitlines()

    progress = [line for line in lines if line.startswith("code graph progress ")]
    phases = [line.split("phase=")[1].split()[0] for line in progress]
    assert phases[:3] == ["queued", "discovery", "fingerprint"]
    assert "files_total=" not in progress[0]
    assert "files_parsed=1" in progress[-1]
    assert "updated_at=" in progress[-1]
    state = "failed" if publication_fails else "ready"
    assert f"state={state}" in progress[-1]
    assert codes == [1 if publication_fails else 0]
    if json_output:
        assert stdout.getvalue().count("\n") == 1
        payload = json.loads(stdout.getvalue())
        assert payload["state"] == state
        if publication_fails:
            assert payload["error"] == "publication_failed"
    elif publication_fails:
        assert stdout.getvalue() == ""
        assert lines[-1] == "iwiki-mcp: code graph publication failed (code=publication_failed)"
    else:
        assert stdout.getvalue().startswith("code graph ready mode=sqlite ")
    assert "private-token-and-endpoint" not in "".join(lines)
    assert str(project) not in "".join(lines)


def test_progress_lines_rate_limit_changed_counters_within_phase():
    stream = StringIO()
    now = 0.0
    progress = admin._CodePublishProgress(stream, clock=lambda: now)

    def record(phase, files, state="running"):
        progress.emit({
            "id": "a" * 16, "state": state, "private": "secret",
            "progress": {
                "phase": phase, "files_parsed": files, "files_total": 50,
                "batches_acknowledged": 0, "bytes_acknowledged": 0,
                "updated_at": 123 + files, "filename": "private-source",
            },
        })

    for files in range(50):
        now = files / 20
        record("parsing", files)
    now = 9.0
    record("parsing", 49)
    now = 11.0
    record("parsing", 49)
    record("resolution", 49)
    record("resolution", 49, state="ready")
    lines = stream.getvalue().splitlines()
    assert [int(line.split("files_parsed=")[1].split()[0]) for line in lines] == [
        0, 20, 40, 49, 49, 49,
    ]
    assert all("secret" not in line and "private-source" not in line for line in lines)
    assert "state=ready phase=resolution" in lines[-1]


def test_closed_progress_sink_preserves_final_result(tmp_path):
    project = create_sqlite_project(tmp_path)
    stream = StringIO()
    stream.close()
    stdout = StringIO()
    code = admin.run(
        ["code", "publish", "--project", str(project), "--json"],
        stderr=stream, stdout=stdout, environ={},
    )
    assert code == 0
    assert json.loads(stdout.getvalue())["state"] == "ready"


def test_blocked_progress_sink_leaves_worker_and_command_free_to_finish(tmp_path, monkeypatch):
    project = create_sqlite_project(tmp_path)
    writing, release_sink = threading.Event(), threading.Event()
    building = threading.Event()
    workers = []
    build = indexer.CodeGraphIndexer.build

    def build_after_output_stalls(*args, **kwargs):
        workers.append(threading.current_thread())
        building.set()
        assert writing.wait(5)
        return build(*args, **kwargs)

    monkeypatch.setattr(indexer.CodeGraphIndexer, "build", build_after_output_stalls)

    consumers = []

    class HeldStream(StringIO):
        def write(self, data):
            consumers.append(threading.current_thread())
            writing.set()
            assert release_sink.wait(10)
            return super().write(data)

    codes, sink, stdout = [], HeldStream(), StringIO()
    cli = threading.Thread(target=lambda: codes.append(admin.run(
        ["code", "publish", "--project", str(project), "--json"],
        stderr=sink, stdout=stdout, environ={},
    )))
    cli.start()
    try:
        assert writing.wait(5)
        assert building.wait(5)
        workers[0].join(3)
        assert not workers[0].is_alive()
        cli.join(1.5)
        assert not cli.is_alive(), "final stdout is blocked by the progress sink"
        assert codes == [0]
        assert json.loads(stdout.getvalue())["state"] == "ready"
    finally:
        release_sink.set()
        cli.join(10)
    consumers[0].join(1)
    assert not consumers[0].is_alive()
    assert len(sink.getvalue().splitlines()) == 1


def test_early_join_observes_same_job_and_detaches_before_termination(tmp_path, monkeypatch):
    project = create_sqlite_project(tmp_path)
    binding = base.resolve_storage_binding(str(project), environ={})
    runtime = application.code_runtime(application.source_context(binding), environ={})
    builds, release = [], threading.Event()
    build = indexer.CodeGraphIndexer.build

    def hold_build(*args, **kwargs):
        builds.append(threading.current_thread())
        assert release.wait(10)
        return build(*args, **kwargs)

    monkeypatch.setattr(indexer.CodeGraphIndexer, "build", hold_build)
    first_stream, second_stream = StringIO(), StringIO()
    try:
        first = runtime.index(
            wait_seconds=0, _observe_job=admin._CodePublishProgress(first_stream).observe,
        )
        second = runtime.index(
            wait_seconds=0, _observe_job=admin._CodePublishProgress(second_stream).observe,
        )
        assert first["state"] == second["state"] == "rebuilding"
        handle = first["job"]["id"]
        assert second["job"]["id"] == handle
        expected = (first_stream.getvalue(), second_stream.getvalue())
        assert all(f"job={handle} state=running phase=queued" in output for output in expected)
    finally:
        release.set()
        runtime.join_workers(timeout=5)
    assert len(builds) == 1
    assert expected == (first_stream.getvalue(), second_stream.getvalue())


def test_fast_worker_replays_phase_boundaries_after_completion(tmp_path, monkeypatch):
    project = create_sqlite_project(tmp_path)
    observe = admin._CodePublishProgress.observe

    def observe_finished_job(output, job):
        job.thread.join(5)
        assert not job.thread.is_alive()
        return observe(output, job)

    monkeypatch.setattr(admin._CodePublishProgress, "observe", observe_finished_job)
    stderr = StringIO()
    code = admin.run(
        ["code", "publish", "--project", str(project), "--json"],
        stderr=stderr, stdout=StringIO(), environ={},
    )
    assert code == 0
    lines = stderr.getvalue().splitlines()
    assert "state=running phase=queued" in lines[0]
    phases = [line.split("phase=")[1].split()[0] for line in lines]
    assert phases[:4] == ["queued", "discovery", "fingerprint", "parsing"]
    assert phases[-3:] == ["final_verification", "metadata", "metadata"]
    assert "state=ready" in lines[-1]


@pytest.mark.parametrize("publication_fails", [False, True])
@pytest.mark.parametrize("sink", ["pipe", "socket"])
@pytest.mark.parametrize("json_output", [False, True])
def test_cli_process_exits_with_saturated_stderr(tmp_path, publication_fails, sink, json_output):
    project = create_sqlite_project(tmp_path)
    env = {key: value for key, value in os.environ.items() if not key.startswith("IWIKI_")}
    # Reserve an unlistened local port for a deterministic connection refusal.
    with socket.socket() as unavailable:
        if publication_fails:
            unavailable.bind(("127.0.0.1", 0))
            env.update({
                "IWIKI_CODE_GRAPH_MCP_URL": f"http://127.0.0.1:{unavailable.getsockname()[1]}/mcp",
                "IWIKI_CODE_GRAPH_MCP_TOKEN": "test-token",
            })
            config = project / ".iwiki.toml"
            config.write_text(config.read_text().replace(
                'publish_mode = "sqlite"', 'publish_mode = "mcp"'
            ))
        if sink == "socket":
            read_end, write_end = socket.socketpair()
            read_fd, write_fd = read_end.detach(), write_end.detach()
        else:
            read_fd, write_fd = os.pipe()
        try:
            os.set_blocking(write_fd, False)
            with pytest.raises(BlockingIOError):
                while True:
                    os.write(write_fd, b"x" * 4096)
            os.set_blocking(write_fd, True)
            # Leave the read end open and unread until the child exits.
            command = Path(sys.executable).with_name("iwiki-mcp")
            argv = [command, "code", "publish", "--project", str(project)]
            if json_output:
                argv.append("--json")
            result = subprocess.run(
                argv,
                stdout=subprocess.PIPE, stderr=write_fd, env=env,
                cwd=tmp_path, timeout=7, text=True,
            )
        finally:
            os.close(write_fd)
            os.close(read_fd)
    assert result.returncode == (1 if publication_fails else 0)
    if json_output:
        assert result.stdout.count("\n") == 1
        payload = json.loads(result.stdout)
        assert payload["state"] == ("failed" if publication_fails else "ready")
        if publication_fails:
            assert payload["error"] == "publication_failed"
    elif publication_fails:
        assert result.stdout == ""
    else:
        assert result.stdout.startswith("code graph ready mode=sqlite ")


def test_text_configuration_error_with_closed_stderr_retains_exit_two(tmp_path):
    stderr = StringIO()
    stderr.close()
    stdout = StringIO()
    code = admin.run(
        ["code", "publish", "--project", str(tmp_path)], stdout=stdout, stderr=stderr, environ={},
    )
    assert code == 2
    assert stdout.getvalue() == ""


def test_text_usage_error_with_disconnected_stderr_pipe_retains_exit_two(tmp_path):
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    try:
        result = subprocess.run(
            [Path(sys.executable).with_name("iwiki-mcp"), "code", "publish"],
            stdout=subprocess.PIPE, stderr=write_fd, cwd=tmp_path, timeout=7, text=True,
        )
    finally:
        os.close(write_fd)
    assert result.returncode == 2
    assert result.stdout == ""


@pytest.mark.parametrize("flag,state", [("--resume", "ready"), ("--forget-resume", "forgotten")])
def test_resume_cli_dispatch(flag, state, monkeypatch, tmp_path):
    def resume(project, **kwargs):
        assert project == str(tmp_path)
        return _outcome()

    monkeypatch.setattr(application, "resume_project", resume, raising=False)
    monkeypatch.setattr(application, "forget_resume_project",
                        lambda *a, **kw: {"state": "forgotten", "outcome_unknown": True},
                        raising=False)
    out = StringIO()
    code = admin.run(["code", "publish", "--project", str(tmp_path), flag, "--json"],
                     stdout=out, stderr=StringIO(), environ={})
    assert code == 0
    assert json.loads(out.getvalue())["state"] == state


@pytest.mark.parametrize("json_mode", [True, False])
def test_checkpoint_storage_failure_exposes_safe_reason(monkeypatch, json_mode):
    outcome = _outcome(index_state="failed")
    outcome.index.update(code="parse_checkpoint_storage_failed", hint="secret-sentinel")
    args = ["code", "publish", "--project", "/repo"]
    code, stdout, stderr = _run(args + (["--json"] if json_mode else []), monkeypatch, outcome)
    assert code == 1
    assert "parse_checkpoint_storage_failed" in stderr
    assert "secret-sentinel" not in stdout + stderr
    if json_mode:
        assert json.loads(stdout)["reason"] == "parse_checkpoint_storage_failed"
        assert json.loads(stdout)["error"] == "index_failed"
