"""Real CLI coverage for direct PostgreSQL code-graph publication."""
from __future__ import annotations

import ast
from io import StringIO
import json
import os
from pathlib import Path
import re

import psycopg
import pytest

from iwiki_mcp import admin, base
from iwiki_mcp.codegraph import application
from iwiki_mcp.codegraph.query import validate_search_request
from iwiki_mcp.postgres.codegraph import PostgresCodeGraphReader
from iwiki_mcp.storage import PostgresBinding
from tests.codegraph.synthetic_wiki import create_postgres_project

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def postgres_project(
    tmp_path,
    clean_postgres,
    runtime_principal,
    monkeypatch,
):
    return create_postgres_project(
        tmp_path,
        clean_postgres,
        runtime_principal,
        monkeypatch,
    )


def _run_publish(project: Path):
    stdout = StringIO()
    stderr = StringIO()
    code = admin.run(
        ["code", "publish", "--project", str(project), "--json"],
        stdout=stdout,
        stderr=stderr,
        environ=os.environ,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def _resolved_reader(project: Path):
    binding = base.resolve_storage_binding(str(project), environ=os.environ)
    assert isinstance(binding, PostgresBinding)
    reader = PostgresCodeGraphReader(
        binding.connection_dsn(),
        binding.iwiki_id,
        binding.primary,
        max_snapshot_age_seconds=86_400,
    )
    return binding, reader


def test_code_publish_cli_activates_direct_postgres_snapshot(postgres_project):
    project = postgres_project.project
    config_path = project / ".iwiki.toml"
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    storage = config["storage"]
    markdown_before = postgres_project.markdown_bytes

    code, stdout, stderr = _run_publish(project)
    payload = json.loads(stdout)
    binding, reader = _resolved_reader(project)
    status = reader.status()
    request = validate_search_request(
        "Service", configured_languages=("python",)
    )
    search = reader.search(request)

    assert code == 0
    assert stdout.count("\n") == 1
    assert stdout == json.dumps(payload, separators=(",", ":")) + "\n"
    assert stderr == ""
    assert payload["state"] == "ready"
    assert payload["publish_mode"] == "postgres"
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", payload["snapshot_revision"])
    assert status["state"] == "ready"
    assert status["fresh"] is True
    assert status["snapshot_revision"] == payload["snapshot_revision"]
    assert search["state"] == "ready"
    assert search["fresh"] is True
    assert binding.primary == "docs"
    assert (project / ".iwiki" / "code-docs.sqlite3").is_file()
    exclude_lines = (project / ".git" / "info" / "exclude").read_text(
        encoding="utf-8"
    ).splitlines()
    assert exclude_lines.count("/.iwiki/") == 1
    assert not (project / ".gitignore").exists()
    assert postgres_project.stored_markdown_bytes() == markdown_before
    assert any(
        result["local_name"] == "Service" for result in search["results"]
    )
    assert set(storage) == {
        "type",
        "host",
        "port",
        "database",
        "user",
        "sslmode",
        "iwiki_id",
    }
    assert set(config) == {"read", "write", "primary", "storage", "code_graph"}
    assert config["read"] == config["write"] == ["docs"]
    assert config["primary"] == "docs"
    assert config["code_graph"]["publish_mode"] == "postgres"
    assert config["code_graph"]["read_mode"] == "postgres"
    rendered_config = config_path.read_text(encoding="utf-8").lower()
    for forbidden in ("password", "dsn", "token"):
        assert forbidden not in rendered_config


class _FailingPublisher:
    """Record aborts while delegating every real PostgreSQL operation."""

    def __init__(self, delegate, stage: str, failure: psycopg.Error) -> None:
        self._delegate = delegate
        self._stage = stage
        self._failure = failure
        self.abort_calls = 0
        self._failed = False

    def __repr__(self) -> str:
        return "<redacted failing PostgreSQL publisher>"

    def begin(self, header):
        return self._delegate.begin(header)

    def publish_batch(self, session, batch):
        if self._stage == "batch" and not self._failed:
            self._failed = True
            raise self._failure
        return self._delegate.publish_batch(session, batch)

    def finalize(self, session):
        if self._stage == "finalize" and not self._failed:
            self._failed = True
            raise self._failure
        return self._delegate.finalize(session)

    def abort(self, session):
        self.abort_calls += 1
        return self._delegate.abort(session)


@pytest.mark.parametrize("failure_stage", ["batch", "finalize"])
def test_direct_postgres_failure_preserves_active_revision_and_redacts(
    postgres_project,
    monkeypatch,
    caplog,
    failure_stage,
):
    project = postgres_project.project
    first_code, first_stdout, first_stderr = _run_publish(project)
    first_payload = json.loads(first_stdout)
    _binding, reader = _resolved_reader(project)
    old_revision = reader.status()["snapshot_revision"]
    assert first_code == 0
    assert first_stderr == ""
    assert old_revision == first_payload["snapshot_revision"]

    project.joinpath("service.py").write_text(
        "class Service:\n"
        "    def run(self):\n"
        "        return replacement()\n\n\n"
        "def replacement():\n"
        "    return 2\n",
        encoding="utf-8",
    )
    sentinel_dsn = (
        "postgresql://sentinel-user:sentinel-password@db.invalid/sentinel_test"
    )
    sentinel_path = str(project.resolve())
    failure = psycopg.Error(
        f"{sentinel_dsn} password=sentinel-password path={sentinel_path}"
    )
    real_factory = application.create_postgres_publisher
    wrappers = []

    def failing_factory(*args, **kwargs):
        wrapper = _FailingPublisher(
            real_factory(*args, **kwargs), failure_stage, failure
        )
        wrappers.append(wrapper)
        return wrapper

    monkeypatch.setattr(
        application,
        "create_postgres_publisher",
        failing_factory,
    )
    real_publish_project = application.publish_project
    failures = []

    def recording_publish_project(*args, **kwargs):
        try:
            return real_publish_project(*args, **kwargs)
        except application.CodeGraphPublishError as exc:
            failures.append(exc)
            raise

    monkeypatch.setattr(
        application,
        "publish_project",
        recording_publish_project,
    )

    code, stdout, stderr = _run_publish(project)
    payload = json.loads(stdout)
    emitted = stdout + stderr + caplog.text + repr(
        [payload, wrappers, failures, postgres_project]
    )

    assert code == 1
    assert stdout == (
        '{"state":"failed","publish_mode":"postgres",'
        '"error":"internal_error","duration_ms":0}\n'
    )
    assert stderr == ""
    assert reader.status()["snapshot_revision"] == old_revision
    assert len(wrappers) == 1
    assert wrappers[0].abort_calls == 1
    assert len(failures) == 1
    assert failures[0].__cause__ is None
    assert failures[0].__context__ is None
    for sentinel in (
        sentinel_dsn,
        "sentinel-password",
        sentinel_path,
        "Traceback",
    ):
        assert sentinel not in emitted


def test_cli_and_application_have_no_direct_postgres_sql_path():
    repository = Path(__file__).resolve().parents[2]
    admin_path = repository / "src" / "iwiki_mcp" / "admin.py"
    application_path = (
        repository / "src" / "iwiki_mcp" / "codegraph" / "application.py"
    )
    admin_text = admin_path.read_text(encoding="utf-8")
    admin_tree = ast.parse(admin_text)
    # Other admin commands own SQL; scan only the code-publish entry point.
    cli_function = next(
        node
        for node in admin_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_run_code_publish"
    )
    cli_text = ast.get_source_segment(admin_text, cli_function)
    application_text = application_path.read_text(encoding="utf-8")

    assert cli_text is not None
    for text in (cli_text, application_text):
        assert "cursor.execute(" not in text
        assert "psycopg.connect(" not in text
    assert "PostgresCodeGraphStore(" in application_text
