"""Real CLI coverage through the hosted streamable-HTTP MCP route."""
from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
import re

import pytest
from starlette.testclient import TestClient

from iwiki_mcp import admin, server
from iwiki_mcp.codegraph import application
from iwiki_mcp.postgres.config import HostedCodeGraphConfig
from iwiki_mcp.postgres.store import PostgresStore
from tests.codegraph.synthetic_wiki import (
    _embed,
    _postgres_config,
    create_sqlite_project,
)
from tests.postgres.test_code_graph_contract import _McpRoute, _open_session


pytestmark = pytest.mark.postgres_integration

_SERVER_MAX_BATCH_ROWS = 1


class InProcessMcpTransport:
    """Record calls while forwarding them to the real hosted JSON-RPC route."""

    def __init__(self, route, primary, *, reject_batch_at=None):
        self.route = route
        self.primary = primary
        self.reject_batch_at = reject_batch_at
        self.calls = []
        self._batch_count = 0

    def __repr__(self):
        return "<redacted in-process MCP transport>"

    def call(self, name, arguments):
        recorded = dict(arguments)
        self.calls.append((name, recorded))
        if name != "wiki_bind" and not any(
            call[0] == "wiki_bind" for call in self.calls[:-1]
        ):
            bind_arguments = {"primary": self.primary}
            bound = self.route.call("wiki_bind", bind_arguments)
            self.calls.insert(-1, ("wiki_bind", bind_arguments))
            if "error" in bound:
                return bound
        if name == "wiki_code_publish_batch":
            self._batch_count += 1
            if self._batch_count == self.reject_batch_at:
                return {
                    "error": "remote_mcp_failed",
                    "reason": "http_status",
                    "status": 503,
                    "hint": (
                        "the remote wiki refused the code graph call; see status"
                    ),
                }
        return self.route.call(name, recorded)


class HostedMcpCli:
    """One synthetic checkout and its writable hosted MCP session."""

    def __init__(
        self,
        *,
        project,
        client,
        route,
        transport,
        hosted,
        environment,
    ):
        self.project = project
        self.client = client
        self.route = route
        self.transport = transport
        self.hosted = hosted
        self.environment = environment

    def __repr__(self):
        return "<redacted hosted MCP CLI fixture>"

    def run(self):
        stdout = StringIO()
        stderr = StringIO()
        exit_code = admin.run(
            [
                "code",
                "publish",
                "--project",
                str(self.project),
                "--json",
            ],
            stdout=stdout,
            stderr=stderr,
            environ=self.environment,
        )
        return exit_code, stdout.getvalue(), stderr.getvalue()


def _configure_mcp_project(project: Path) -> None:
    config_path = project / ".iwiki.toml"
    config = config_path.read_text(encoding="utf-8")
    config = config.replace(
        'publish_mode = "sqlite"', 'publish_mode = "mcp"'
    ).replace('read_mode = "sqlite"', 'read_mode = "mcp"')
    config = config.replace(
        "max_full_rebuild_seconds = 30",
        "max_batch_rows = 5000\nmax_full_rebuild_seconds = 30",
    )
    config_path.write_text(config, encoding="utf-8")


def _seed_hosted_markdown(clean_postgres) -> None:
    store = PostgresStore(
        clean_postgres,
        "wiki-a",
        _postgres_config(),
        embedder=_embed,
    )
    created = store.write_page(
        "docs",
        "architecture",
        "---\n"
        "type: concept\n"
        "title: Architecture\n"
        "description: Synthetic hosted MCP publication fixture.\n"
        "tags: [fixture]\n"
        "status: stable\n"
        "---\n"
        "# Architecture\n\n"
        "## Service\n\n"
        "Service.run coordinates helper work.\n",
    )
    assert created["page"] == "docs/architecture.md"


@pytest.fixture
def hosted_mcp_cli(
    tmp_path,
    clean_postgres,
    hosted_runtime,
    monkeypatch,
):
    _seed_hosted_markdown(clean_postgres)
    project = create_sqlite_project(tmp_path)
    _configure_mcp_project(project)
    monkeypatch.setattr(
        server,
        "_HOSTED_CODE_GRAPH",
        HostedCodeGraphConfig(
            max_batch_rows=_SERVER_MAX_BATCH_ROWS,
            max_batch_bytes=1_000_000,
        ),
    )
    environment = {
        "IWIKI_CODE_GRAPH_MCP_URL": "http://127.0.0.1:8765/mcp",
        "IWIKI_CODE_GRAPH_MCP_TOKEN": "in-process-only-token",
    }

    with TestClient(
        hosted_runtime.runtime.app,
        base_url="http://127.0.0.1:8765",
    ) as client:
        session_id = _open_session(client, hosted_runtime.token)
        route = _McpRoute(client, hosted_runtime.token, session_id)
        transport = InProcessMcpTransport(route, "docs")
        monkeypatch.setattr(
            application,
            "RemoteMcpTransport",
            lambda *, environ, primary: transport,
        )
        yield HostedMcpCli(
            project=project,
            client=client,
            route=route,
            transport=transport,
            hosted=hosted_runtime,
            environment=environment,
        )


def _failure_payload(stdout, stderr):
    payload = json.loads(stdout)
    assert stdout.count("\n") == 1
    assert stderr == ""
    assert payload["state"] == "failed"
    assert payload["publish_mode"] == "mcp"
    assert payload["error"] == "publication_failed"
    return payload


def test_code_publish_cli_activates_snapshot_through_hosted_mcp(
    hosted_mcp_cli,
):
    exit_code, stdout, stderr = hosted_mcp_cli.run()
    payload = json.loads(stdout)
    status = hosted_mcp_cli.transport.call("wiki_code_status", {})
    search = hosted_mcp_cli.transport.call(
        "wiki_code_search", {"query": "Service"}
    )

    assert exit_code == 0
    assert stdout.count("\n") == 1
    assert stdout == json.dumps(payload, separators=(",", ":")) + "\n"
    assert stderr == ""
    assert payload["state"] == "ready"
    assert payload["publish_mode"] == "mcp"
    assert re.fullmatch(
        r"sha256:[0-9a-f]{64}", payload["snapshot_revision"]
    )
    assert status["state"] == "ready"
    assert status["snapshot_revision"] == payload["snapshot_revision"]
    assert search["state"] == "ready"
    assert any(
        result["local_name"] == "Service" for result in search["results"]
    )
    assert hosted_mcp_cli.transport.calls[0][0] == "wiki_bind"
    batch_calls = [
        arguments
        for name, arguments in hosted_mcp_cli.transport.calls
        if name == "wiki_code_publish_batch"
    ]
    assert batch_calls
    assert all(
        len(arguments["rows"]) <= _SERVER_MAX_BATCH_ROWS
        for arguments in batch_calls
    )


def test_read_only_hosted_grant_denies_publication_without_begin_or_abort(
    hosted_mcp_cli,
    monkeypatch,
):
    first_code, first_stdout, first_stderr = hosted_mcp_cli.run()
    old_revision = hosted_mcp_cli.transport.call(
        "wiki_code_status", {}
    )["snapshot_revision"]
    assert first_code == 0
    assert first_stderr == ""
    assert json.loads(first_stdout)["snapshot_revision"] == old_revision

    read_only = hosted_mcp_cli.hosted.auth.create_token(
        "wiki-a",
        "read-only-publisher",
        read_domains=["docs"],
        write_domains=[],
    )["token"]
    session_id = _open_session(hosted_mcp_cli.client, read_only)
    route = _McpRoute(hosted_mcp_cli.client, read_only, session_id)
    transport = InProcessMcpTransport(route, "docs")
    monkeypatch.setattr(
        application,
        "RemoteMcpTransport",
        lambda *, environ, primary: transport,
    )

    exit_code, stdout, stderr = hosted_mcp_cli.run()
    payload = _failure_payload(stdout, stderr)
    current = hosted_mcp_cli.transport.call("wiki_code_status", {})

    assert exit_code == 1
    assert current["snapshot_revision"] == old_revision
    assert [name for name, _args in transport.calls] == ["wiki_bind"]
    assert read_only not in stdout + stderr + repr(payload) + repr(transport)


def test_later_batch_failure_preserves_revision_and_aborts_once(
    hosted_mcp_cli,
    monkeypatch,
):
    first_code, first_stdout, first_stderr = hosted_mcp_cli.run()
    old_revision = hosted_mcp_cli.transport.call(
        "wiki_code_status", {}
    )["snapshot_revision"]
    assert first_code == 0
    assert first_stderr == ""
    assert json.loads(first_stdout)["snapshot_revision"] == old_revision

    hosted_mcp_cli.project.joinpath("service.py").write_text(
        "class Service:\n"
        "    def run(self):\n"
        "        return replacement()\n\n\n"
        "def replacement():\n"
        "    return 2\n",
        encoding="utf-8",
    )
    transport = InProcessMcpTransport(
        hosted_mcp_cli.route,
        "docs",
        reject_batch_at=2,
    )
    monkeypatch.setattr(
        application,
        "RemoteMcpTransport",
        lambda *, environ, primary: transport,
    )

    exit_code, stdout, stderr = hosted_mcp_cli.run()
    _failure_payload(stdout, stderr)
    current = hosted_mcp_cli.transport.call("wiki_code_status", {})
    call_names = [name for name, _args in transport.calls]

    assert exit_code == 1
    assert current["snapshot_revision"] == old_revision
    assert call_names[0] == "wiki_bind"
    assert call_names.count("wiki_code_publish_begin") == 1
    assert call_names.count("wiki_code_publish_batch") == 2
    assert call_names.count("wiki_code_publish_abort") == 1
    assert call_names.index("wiki_code_publish_begin") < call_names.index(
        "wiki_code_publish_abort"
    )
    assert "wiki_code_publish_finalize" not in call_names
