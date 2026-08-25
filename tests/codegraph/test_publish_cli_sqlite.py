"""Real CLI coverage for the local SQLite publication route."""
from __future__ import annotations

from io import StringIO
import json

from iwiki_mcp import admin, base
from iwiki_mcp.codegraph import application

from .synthetic_wiki import create_sqlite_project


def _run_publish(project, *, environ=None):
    stdout = StringIO()
    stderr = StringIO()

    code = admin.run(
        ["code", "publish", "--project", str(project), "--json"],
        stdout=stdout,
        stderr=stderr,
        environ={} if environ is None else environ,
    )

    return code, stdout.getvalue(), stderr.getvalue()


def test_code_publish_cli_reuses_local_sqlite_snapshot(tmp_path, monkeypatch):
    project = create_sqlite_project(tmp_path)
    monkeypatch.setenv("IWIKI_CODE_GRAPH_ENABLED", "false")
    monkeypatch.setenv("IWIKI_CODE_GRAPH_MAX_FILE_BYTES", "process-sentinel")
    wiki_markdown = (tmp_path / "wiki" / "docs" / "architecture.md")
    before = wiki_markdown.read_bytes()
    monkeypatch.setattr(
        application,
        "McpSnapshotPublisher",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("external publisher factory must not be called")
        ),
    )

    first_code, first_stdout, first_stderr = _run_publish(project)
    second_code, second_stdout, second_stderr = _run_publish(project)
    first = json.loads(first_stdout)
    second = json.loads(second_stdout)

    assert first_code == second_code == 0
    assert first_stdout.count("\n") == second_stdout.count("\n") == 1
    assert first["state"] == second["state"] == "ready"
    assert first["publish_mode"] == second["publish_mode"] == "sqlite"
    assert first["snapshot_revision"] == second["snapshot_revision"]
    assert first["counts"]["files"] == second["counts"]["files"] == 1
    assert first_stderr == second_stderr == ""
    assert wiki_markdown.read_bytes() == before

    binding = base.resolve_storage_binding(str(project), environ={})
    runtime = application.code_runtime(
        application.source_context(binding),
        environ={},
    )
    status = runtime.status()
    results = runtime.search("Service")

    assert status["fresh"] is True
    assert results["results"]


def test_code_publish_cli_honors_explicit_override_over_process_environment(
    tmp_path, monkeypatch
):
    project = create_sqlite_project(tmp_path)
    monkeypatch.setenv("IWIKI_CODE_GRAPH_ENABLED", "true")

    code, stdout, stderr = _run_publish(
        project,
        environ={"IWIKI_CODE_GRAPH_ENABLED": "false"},
    )
    result = json.loads(stdout)

    assert code == 1
    assert result["state"] == "failed"
    assert result["publish_mode"] == "sqlite"
    assert result["error"] == "index_failed"
    assert stderr == ""
