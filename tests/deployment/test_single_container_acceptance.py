from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import uuid

import pytest


REPOSITORY = Path(__file__).parents[2]


def _request(client, token, payload, session_id=None):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if session_id is not None:
        headers["Mcp-Session-Id"] = session_id
    return client.post("/mcp", headers=headers, json=payload)


def _initialize(client, token):
    return _request(
        client,
        token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "acceptance", "version": "1"},
            },
        },
    )


def _tool(client, token, session_id, name, arguments):
    response = _request(
        client,
        token,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        session_id,
    )
    assert response.status_code == 200
    return json.loads(response.json()["result"]["content"][0]["text"])


def test_supervisor_and_health_sources_define_one_restartable_process_tree():
    supervisor = (REPOSITORY / "deploy/supervisord.conf").read_text(encoding="utf-8")
    health = (REPOSITORY / "deploy/healthcheck.py").read_text(encoding="utf-8")

    assert supervisor.count("[program:") == 3
    for child in ("iwiki-mcp", "nginx", "telegram-bot"):
        section = supervisor.split(f"[program:{child}]", 1)[1].split("[", 1)[0]
        assert "autorestart=unexpected" in section
        assert "stopasgroup=true" in section
        assert "killasgroup=true" in section
    assert "nodaemon=true" in supervisor
    assert "REQUIRED_CHILDREN = frozenset" in health
    assert '"iwiki-mcp", "nginx", "telegram-bot"' in health
    assert 'HEARTBEAT_PATH = "/run/iwiki-telegram-bot.heartbeat"' in health
    assert "telegram_heartbeat_stale" in health


def test_application_compose_has_no_database_service_or_proxy_fallback():
    compose = (REPOSITORY / "compose.yaml").read_text(encoding="utf-8").lower()
    assert compose.count("services:") == 1
    assert "postgres:" not in compose
    assert "gost:" not in compose
    assert "stunnel:" not in compose
    assert "http_proxy" not in compose
    assert "https_proxy" not in compose
    assert "all_proxy" not in compose
    assert "no_proxy" not in compose


def test_health_boundary_and_repository_artifacts_do_not_persist_dynamic_markers(
    tmp_path, capsys
):
    markers = {
        name: f"{name}-{secrets.token_urlsafe(24)}"
        for name in (
            "credential",
            "proxy-origin",
            "update",
            "reply",
            "filename",
            "audio",
            "transcription",
            "preview",
            "provider-error",
        )
    }
    spec = importlib.util.spec_from_file_location(
        "acceptance_healthcheck", REPOSITORY / "deploy/healthcheck.py"
    )
    assert spec is not None and spec.loader is not None
    healthcheck = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(healthcheck)

    def fail_with_marker(**_kwargs):
        raise RuntimeError(markers["provider-error"])

    assert healthcheck.children_running(
        healthcheck.REQUIRED_CHILDREN, run=fail_with_marker
    ) is False
    captured = capsys.readouterr()
    assert all(marker not in captured.out + captured.err for marker in markers.values())
    assert list(tmp_path.rglob("*")) == []
    for relative in (
        "compose.yaml",
        "Dockerfile",
        "deploy/supervisord.conf",
        "deploy/nginx.conf.example",
        "tests/deployment/fixtures/runtime.env",
        "tests/deployment/fixtures/server.toml",
    ):
        content = (REPOSITORY / relative).read_text(encoding="utf-8")
        assert all(marker not in content for marker in markers.values())


@pytest.mark.asyncio
async def test_recreated_bot_service_has_no_confirmation_or_selection_state(tmp_path):
    from iwiki_mcp.telegram_bot.access import AccessPolicy
    from iwiki_mcp.telegram_bot.conversation import ConversationService
    from iwiki_mcp.telegram_bot.inference import InferenceError

    preview_marker = f"preview-{secrets.token_urlsafe(24)}"

    class Remote:
        async def list_domains(self):
            return ["docs"]

        async def search(self, _domain, _query):
            return []

        async def write_page(self, *_args):
            raise AssertionError("recreated service must not replay a write")

    class Inference:
        async def draft_markdown(self, _request, _context):
            return preview_marker

        async def answer(self, _question, _context):
            raise InferenceError("provider failure")

    def service():
        return ConversationService(
            AccessPolicy(frozenset({1001})),
            Remote(),
            Inference(),
            confirmation_ttl_seconds=300,
            temporary_directory=tmp_path,
        )

    original = service()
    await original.select_domain(1001, "docs")
    preview = await original.propose_create(1001, "page", "request")

    restarted = service()
    assert (await restarted.confirm_write(1001, preview.token)).text == (
        "Confirmation is invalid."
    )
    assert (await restarted.answer_question(1001, "question")).text == (
        "Select a domain first."
    )
    assert list(tmp_path.iterdir()) == []


def test_image_history_contains_no_dynamic_privacy_marker(
    docker_command, acceptance_image
):
    marker = f"image-history-{secrets.token_urlsafe(24)}"
    history = subprocess.run(
        [*docker_command, "history", "--no-trunc", acceptance_image],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    assert marker not in history.stdout + history.stderr


def test_supervisor_restarts_each_child_and_terms_process_tree_within_60_seconds(
    tmp_path, docker_command, acceptance_image
):
    config = tmp_path / "supervisord.conf"
    programs = []
    for child in ("iwiki-mcp", "nginx", "telegram-bot"):
        programs.append(
            f"""[program:{child}]
command=/bin/sh -c "while true; do sleep 1; done"
autorestart=unexpected
startsecs=0
stopasgroup=true
killasgroup=true
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0
"""
        )
    config.write_text(
        """[supervisord]
nodaemon=true
logfile=/dev/null
pidfile=/run/supervisord.pid
[unix_http_server]
file=/run/supervisor.sock
chmod=0600
[supervisorctl]
serverurl=unix:///run/supervisor.sock
[rpcinterface:supervisor]
supervisor.rpcinterface_factory=supervisor.rpcinterface:make_main_rpcinterface
"""
        + "\n".join(programs),
        encoding="utf-8",
    )
    name = f"iwiki-supervisor-acceptance-{uuid.uuid4().hex[:10]}"

    def status():
        result = subprocess.run(
            [
                *docker_command,
                "exec",
                name,
                "supervisorctl",
                "-c",
                "/tmp/supervisord.conf",
                "status",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        rows = {}
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 4 and fields[1] == "RUNNING":
                rows[fields[0]] = int(fields[3].rstrip(","))
        return rows

    try:
        subprocess.run(
            [
                *docker_command,
                "run",
                "-d",
                "--name",
                name,
                "--tmpfs",
                "/run:uid=10001,gid=10001,mode=0750",
                "--tmpfs",
                "/tmp:uid=10001,gid=10001,mode=1770",
                "-v",
                f"{config}:/tmp/supervisord.conf:ro",
                "--entrypoint",
                "/usr/bin/supervisord",
                acceptance_image,
                "-c",
                "/tmp/supervisord.conf",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        deadline = time.monotonic() + 15
        children = {}
        while time.monotonic() < deadline:
            children = status()
            if set(children) == {"iwiki-mcp", "nginx", "telegram-bot"}:
                break
            time.sleep(0.1)
        assert set(children) == {"iwiki-mcp", "nginx", "telegram-bot"}

        for child in sorted(children):
            previous_pid = status()[child]
            subprocess.run(
                [
                    *docker_command,
                    "exec",
                    name,
                    "/bin/sh",
                    "-c",
                    f"kill -KILL {previous_pid}",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                current = status().get(child)
                if current is not None and current != previous_pid:
                    break
                time.sleep(0.1)
            assert current is not None and current != previous_pid

        started = time.monotonic()
        subprocess.run(
            [*docker_command, "stop", "--signal", "TERM", "-t", "60", name],
            capture_output=True,
            text=True,
            check=True,
            timeout=65,
        )
        assert time.monotonic() - started <= 60
        running = subprocess.run(
            [
                *docker_command,
                "inspect",
                name,
                "--format",
                "{{.State.Running}}",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        assert running.stdout.strip() == "false"
    finally:
        subprocess.run(
            [*docker_command, "rm", "-f", name],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )


@pytest.mark.postgres_integration
def test_external_postgres_hosted_write_replay_and_conflicts(
    hosted_runtime, store_factory, clean_postgres, monkeypatch
):
    import psycopg
    from iwiki_mcp import server
    from starlette.testclient import TestClient

    store = store_factory()
    monkeypatch.setattr(server, "_postgres_store_for_binding", lambda _binding: store)
    runtime = hosted_runtime.runtime
    token = hosted_runtime.token
    markdown = "# Acceptance\n\n## Body\nfirst version\n"

    with TestClient(runtime.app, base_url="http://127.0.0.1:8765") as client:
        initialized = _initialize(client, token)
        assert initialized.status_code == 200
        session_id = initialized.headers["mcp-session-id"]
        assert _request(
            client,
            token,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session_id,
        ).status_code == 202

        created = _tool(
            client,
            token,
            session_id,
            "wiki_write_page",
            {
                "domain": "docs",
                "slug": "acceptance/write-once",
                "markdown": markdown,
                "source": "acceptance-test",
            },
        )
        assert created["revision"] == 1
        replay = _tool(
            client,
            token,
            session_id,
            "wiki_write_page",
            {
                "domain": "docs",
                "slug": "acceptance/write-once",
                "markdown": markdown,
                "source": "acceptance-test",
            },
        )
        assert replay["error"] == "page_exists"
        page = _tool(
            client,
            token,
            session_id,
            "wiki_read_page",
            {"domain": "docs", "slug": "acceptance/write-once"},
        )
        section = _tool(
            client,
            token,
            session_id,
            "wiki_read_page",
            {
                "domain": "docs",
                "slug": "acceptance/write-once",
                "heading": "Body",
            },
        )
        assert page["revision"] == 1
        assert isinstance(section["section_hash"], str)

        revision_conflict = _tool(
            client,
            token,
            session_id,
            "wiki_update_page",
            {
                "domain": "docs",
                "slug": "acceptance/write-once",
                "heading": "Body",
                "new_body": "second version",
                "expected_revision": 0,
                "expected_section_hash": section["section_hash"],
            },
        )
        assert revision_conflict["error"] == "conflict"
        hash_conflict = _tool(
            client,
            token,
            session_id,
            "wiki_update_page",
            {
                "domain": "docs",
                "slug": "acceptance/write-once",
                "heading": "Body",
                "new_body": "second version",
                "expected_revision": page["revision"],
                "expected_section_hash": "0" * 16,
            },
        )
        assert hash_conflict["error"] == "section_conflict"

    with psycopg.connect(clean_postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM iwiki.pages p JOIN iwiki.domains d "
                "ON d.iwiki_id = p.iwiki_id AND d.domain_id = p.domain_id "
                "WHERE p.iwiki_id = 'wiki-a' AND d.slug = 'docs' "
                "AND p.slug = 'acceptance/write-once'"
            )
            assert cursor.fetchone()[0] == 1


@pytest.mark.postgres_integration
def test_disposable_postgres_custom_loopback_endpoint_is_external_to_compose():
    raw_dsn = os.environ.get("IWIKI_TEST_POSTGRES_LOOPBACK_DSN", "").strip()
    if not raw_dsn:
        pytest.skip("IWIKI_TEST_POSTGRES_LOOPBACK_DSN is not set")
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    values = conninfo_to_dict(raw_dsn)
    if values.get("host") not in {"127.0.0.1", "localhost", "::1"}:
        pytest.skip("IWIKI_TEST_POSTGRES_LOOPBACK_DSN is not loopback-hosted")
    if int(values.get("port", 5432)) == 5432:
        pytest.skip("IWIKI_TEST_POSTGRES_LOOPBACK_DSN does not use a custom port")
    with psycopg.connect(raw_dsn, connect_timeout=10) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            assert cursor.fetchone() == (1,)
    assert "postgres:" not in (REPOSITORY / "compose.yaml").read_text(
        encoding="utf-8"
    ).lower()
