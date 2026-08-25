"""Public code graph publish CLI contract."""
from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
import subprocess

import psycopg
import pytest

from iwiki_mcp import admin, base, server
from iwiki_mcp.codegraph import application
from iwiki_mcp.codegraph.application import CodeGraphPublishOutcome
from iwiki_mcp.codegraph.config import CodeGraphConfig, CodeGraphConfigError
from iwiki_mcp.codegraph.mcp_adapter import CodeGraphAdapterError


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
            **({"revision": "sha256:local"} if mode == "sqlite" else {}),
        },
        publication={
            "state": publication_state,
            "snapshot_revision": "sha256:remote",
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
        lambda _value: binding,
    )
    monkeypatch.setattr(
        application.codegraph_config,
        "load_code_graph_config",
        lambda _value: CodeGraphConfig(publish_mode=mode),
    )

    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(application, "index_and_publish", fail)
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
        "code graph ready mode=mcp revision=sha256:remote "
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
        '"snapshot_revision":"sha256:remote",'
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
        lambda value: calls.append(("binding", value)) or binding,
    )
    monkeypatch.setattr(
        application,
        "index_and_publish",
        lambda value, *, environ=None: (
            calls.append(("publish", value, environ)) or outcome
        ),
    )

    result = application.publish_project(str(checkout), environ=environ)

    assert result is outcome
    assert calls == [
        ("binding", str(checkout.absolute())),
        ("publish", binding, environ),
    ]


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
