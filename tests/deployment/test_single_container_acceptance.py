from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import secrets
import subprocess
import time

import pytest


REPOSITORY = Path(__file__).parents[2]


@pytest.mark.parametrize(
    ("env_name", "values", "message"),
    (
        (
            "IWIKI_TEST_POSTGRES_DSN",
            {"host": "127.0.0.1", "port": "5544"},
            "IWIKI_TEST_POSTGRES_DSN must use a non-loopback host",
        ),
        (
            "IWIKI_TEST_POSTGRES_LOOPBACK_DSN",
            {"host": "172.18.0.2", "port": "5432"},
            "IWIKI_TEST_POSTGRES_LOOPBACK_DSN must use a loopback host",
        ),
        (
            "IWIKI_TEST_POSTGRES_LOOPBACK_DSN",
            {"host": "127.0.0.1", "port": "5432"},
            "IWIKI_TEST_POSTGRES_LOOPBACK_DSN must use a custom port",
        ),
    ),
)
def test_postgres_topology_contract_rejects_invalid_present_endpoint(
    env_name, values, message
):
    from tests.deployment.conftest import require_postgres_topology

    with pytest.raises(pytest.fail.Exception, match=message):
        require_postgres_topology(env_name, values)


def test_postgres_topology_contract_rejects_identical_normalized_endpoints():
    from tests.deployment.conftest import require_distinct_postgres_endpoints

    with pytest.raises(
        pytest.fail.Exception,
        match="IWIKI_TEST_POSTGRES_DSN and IWIKI_TEST_POSTGRES_LOOPBACK_DSN "
        "must resolve to different endpoints",
    ):
        require_distinct_postgres_endpoints(
            {"host": "LOCALHOST.", "port": "5544"},
            {"host": "localhost", "port": 5544},
        )


def test_dynamic_prebuild_private_files_are_excluded_from_disposable_image(
    tmp_path, docker_command, compose_command
):
    from tests.deployment.conftest import build_disposable_privacy_proof

    marker = f"prebuild-private-{secrets.token_urlsafe(24)}"
    excluded = {
        ".env",
        "runtime.env",
        "server.toml",
        "nginx.conf",
        "acceptance.key",
        "acceptance.pem",
    }
    for name in excluded:
        (tmp_path / name).write_text(marker, encoding="utf-8")
    assert all(
        marker in (tmp_path / name).read_text(encoding="utf-8")
        for name in excluded
    )

    proof = build_disposable_privacy_proof(
        tmp_path, docker_command, compose_command, marker
    )

    assert proof["public_file"] == b"public acceptance artifact\n"
    assert excluded.isdisjoint(proof["filesystem_paths"])
    assert marker not in proof["build_output"]
    assert marker not in proof["history"]
    assert marker.encode() not in proof["filesystem_bytes"]
    assert proof["mounts"] == []
    assert proof["container_removed"] is True
    assert proof["image_removed"] is True
    assert proof["context_cleaned"] is True
    assert list(tmp_path.iterdir()) == []


def test_acceptance_image_uses_unique_compose_project(
    acceptance_image, docker_command
):
    inspected = json.loads(
        subprocess.run(
            [*docker_command, "image", "inspect", acceptance_image],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout
    )[0]
    project = inspected["Config"]["Labels"]["com.docker.compose.project"]

    assert project.startswith("iwikiacceptance")
    assert acceptance_image == f"{project}-iwiki:latest"
    assert acceptance_image != "iwiki-mcp-iwiki:latest"


def test_full_stack_cleanup_attempts_every_step_before_reraising():
    from tests.deployment.conftest import run_cleanup_steps

    attempted = []

    def cleanup(name, fail=False):
        def perform():
            attempted.append(name)
            if fail:
                raise RuntimeError(f"{name} cleanup failed")

        return perform

    with pytest.raises(
        RuntimeError,
        match="telegram cleanup failed; files cleanup failed",
    ):
        run_cleanup_steps(
            (
                ("telegram", cleanup("telegram", fail=True)),
                ("inference", cleanup("inference")),
                ("container", cleanup("container")),
                ("files", cleanup("files", fail=True)),
                ("images", cleanup("images")),
            )
        )

    assert attempted == [
        "telegram",
        "inference",
        "container",
        "files",
        "images",
    ]


def _request(client, token, payload, session_id=None):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Host": "acceptance.invalid",
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
        assert "autorestart=true" in section
        assert "stopsignal=TERM" in section
        assert "stopwaitsecs=55" in section
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


def test_health_failure_output_is_sanitized_and_static_fixtures_are_clean(capsys):
    marker = f"provider-error-{secrets.token_urlsafe(24)}"
    spec = importlib.util.spec_from_file_location(
        "acceptance_healthcheck", REPOSITORY / "deploy/healthcheck.py"
    )
    assert spec is not None and spec.loader is not None
    healthcheck = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(healthcheck)

    def fail_with_marker(**_kwargs):
        raise RuntimeError(marker)

    assert healthcheck.children_running(
        healthcheck.REQUIRED_CHILDREN, run=fail_with_marker
    ) is False
    captured = capsys.readouterr()
    assert marker not in captured.out + captured.err
    for relative in (
        "compose.yaml",
        "Dockerfile",
        "deploy/supervisord.conf",
        "deploy/nginx.conf.example",
        "tests/deployment/fixtures/runtime.env",
        "tests/deployment/fixtures/server.toml",
    ):
        content = (REPOSITORY / relative).read_text(encoding="utf-8")
        assert marker not in content


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


def _eventually(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    return value


def _callback_data(payload, prefix):
    keyboard = payload.get("reply_markup", {}).get("inline_keyboard", [])
    for row in keyboard:
        for button in row:
            data = button.get("callback_data", "")
            if data.startswith(prefix):
                return data
    return None


def test_supervisor_restarts_clean_exit_but_honors_explicit_stop(
    tmp_path, docker_command, acceptance_image
):
    child = tmp_path / "clean-exit-child.sh"
    child.write_text(
        "#!/bin/sh\n"
        "count=0\n"
        "test ! -f /run/clean-exit-count || count=$(cat /run/clean-exit-count)\n"
        "count=$((count + 1))\n"
        "printf '%s\\n' \"$count\" > /run/clean-exit-count\n"
        "exit 0\n",
        encoding="utf-8",
    )
    child.chmod(0o755)
    config = tmp_path / "supervisord-clean-exit.conf"
    config.write_text(
        "[supervisord]\n"
        "nodaemon=true\n"
        "logfile=/dev/null\n"
        "pidfile=/run/supervisord.pid\n\n"
        "[unix_http_server]\n"
        "file=/run/supervisor.sock\n"
        "chmod=0600\n\n"
        "[supervisorctl]\n"
        "serverurl=unix:///run/supervisor.sock\n\n"
        "[rpcinterface:supervisor]\n"
        "supervisor.rpcinterface_factory="
        "supervisor.rpcinterface:make_main_rpcinterface\n\n"
        "[program:clean-exit]\n"
        "command=/app/clean-exit-child.sh\n"
        "autorestart=true\n"
        "startsecs=0\n"
        "startretries=1000000\n"
        "stopsignal=TERM\n"
        "stopwaitsecs=55\n"
        "stopasgroup=true\n"
        "killasgroup=true\n"
        "stdout_logfile=/dev/null\n"
        "stderr_logfile=/dev/null\n",
        encoding="utf-8",
    )
    name = f"iwiki-supervisor-clean-exit-{secrets.token_hex(8)}"
    supervisor = [
        *docker_command,
        "exec",
        name,
        "supervisorctl",
        "-c",
        "/app/supervisord-clean-exit.conf",
    ]

    try:
        subprocess.run(
            [
                *docker_command,
                "run",
                "--detach",
                "--name",
                name,
                "--read-only",
                "--user",
                "10001:10001",
                "--tmpfs",
                "/run:uid=10001,gid=10001,mode=0750",
                "--tmpfs",
                "/tmp:uid=10001,gid=10001,mode=1770",
                "--volume",
                f"{config}:/app/supervisord-clean-exit.conf:ro",
                "--volume",
                f"{child}:/app/clean-exit-child.sh:ro",
                "--entrypoint",
                "/usr/bin/supervisord",
                acceptance_image,
                "-c",
                "/app/supervisord-clean-exit.conf",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )

        def restart_count():
            result = subprocess.run(
                [*docker_command, "exec", name, "cat", "/run/clean-exit-count"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            return int(result.stdout) if result.returncode == 0 else 0

        if not _eventually(lambda: restart_count() >= 2, timeout=15):
            logs = subprocess.run(
                [*docker_command, "logs", name],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            status = subprocess.run(
                [*supervisor, "status", "clean-exit"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            pytest.fail(
                "clean-exit child did not restart: "
                f"status={status.stdout.strip()!r} "
                f"logs={(logs.stdout + logs.stderr)[-1000:]!r}"
            )
        subprocess.run(
            [*supervisor, "stop", "clean-exit"],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        assert _eventually(
            lambda: "STOPPED"
            in subprocess.run(
                [*supervisor, "status", "clean-exit"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            ).stdout,
            timeout=10,
        )
        stopped_count = restart_count()
        time.sleep(1)
        assert restart_count() == stopped_count
    finally:
        subprocess.run(
            [*docker_command, "stop", "--time", "5", name],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        subprocess.run(
            [*docker_command, "rm", name],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )


@pytest.mark.postgres_integration
def test_production_children_restart_proxy_recovers_and_term_is_bounded(full_stack):
    expected = {"iwiki-mcp", "nginx", "telegram-bot"}
    children = full_stack.supervisor_status()
    assert set(children) == expected
    assert {item["state"] for item in children.values()} == {"RUNNING"}
    assert full_stack.health_status() == "healthy"
    assert full_stack.telegram_api_addresses() == {"127.0.0.2"}

    supervisor = [
        *full_stack.docker,
        "exec",
        full_stack.name,
        "supervisorctl",
        "-c",
        "/etc/supervisor/supervisord.conf",
    ]
    previous_nginx_pid = children["nginx"]["pid"]
    subprocess.run(
        [*supervisor, "signal", "QUIT", "nginx"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert _eventually(
        lambda: (
            current
            if (current := full_stack.supervisor_status().get("nginx", {})).get(
                "state"
            )
            == "RUNNING"
            and current.get("pid") != previous_nginx_pid
            else None
        ),
        timeout=20,
    )

    subprocess.run(
        [*supervisor, "stop", "telegram-bot"],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    assert _eventually(
        lambda: full_stack.supervisor_status().get("telegram-bot", {}).get(
            "state"
        )
        == "STOPPED"
    )
    time.sleep(1)
    assert full_stack.supervisor_status()["telegram-bot"]["state"] == "STOPPED"
    subprocess.run(
        [*supervisor, "start", "telegram-bot"],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    assert _eventually(
        lambda: full_stack.supervisor_status().get("telegram-bot", {}).get(
            "state"
        )
        == "RUNNING",
        timeout=20,
    )
    assert _eventually(
        lambda: full_stack.health_probe().returncode == 0, timeout=30
    )

    for child in sorted(expected):
        previous_pid = full_stack.supervisor_status()[child]["pid"]
        subprocess.run(
            [
                *full_stack.docker,
                "exec",
                full_stack.name,
                "/bin/sh",
                "-c",
                f"kill -KILL -{previous_pid}",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )

        def restarted():
            current = full_stack.supervisor_status().get(child, {})
            return (
                current
                if current.get("state") == "RUNNING"
                and current.get("pid") != previous_pid
                else None
            )

        assert _eventually(restarted, timeout=20)
        assert _eventually(
            lambda: full_stack.health_probe().returncode == 0, timeout=30
        ), {
            "child": child,
            "probe": full_stack.health_probe().stdout,
            "children": full_stack.supervisor_status(),
        }

    container_id = full_stack.container_id()
    full_stack.telegram.pause_polling()
    assert full_stack.telegram.wait_until_polling()
    time.sleep(3)
    request_count = full_stack.telegram.request_count
    stale = full_stack.health_probe()
    assert stale.returncode == 1
    assert stale.stdout == "telegram_heartbeat_stale\n", {
        "children": full_stack.supervisor_status(),
        "errors": full_stack.telegram.errors,
    }
    assert full_stack.telegram.request_count == request_count
    assert full_stack.container_id() == container_id
    full_stack.telegram.resume_polling()
    assert _eventually(
        lambda: full_stack.telegram.request_count > request_count, timeout=20
    )

    full_stack.telegram.disable()
    assert full_stack.telegram.wait_until_disabled_quiescent()
    request_count = full_stack.telegram.request_count
    time.sleep(0.25)
    assert full_stack.telegram.request_count == request_count
    time.sleep(3)
    logs = subprocess.run(
        [*full_stack.docker, "logs", full_stack.name],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert "telegram poll retry" in logs.stdout + logs.stderr
    generation = full_stack.telegram.enable()
    assert full_stack.telegram.wait_for_connect_generation(
        generation, timeout=20
    )
    assert full_stack.telegram.wait_for_get_updates_generation(
        generation, timeout=20
    )
    assert full_stack.telegram.served_get_updates_generation == generation
    assert full_stack.telegram.request_count > request_count
    assert _eventually(
        lambda: full_stack.health_status() == "healthy", timeout=20
    )
    assert full_stack.container_id() == container_id

    identities = full_stack.host_child_identities()
    assert set(identities) == expected
    assert all(identity["group_members"] for identity in identities.values())
    full_stack.telegram.pause_polling()
    assert full_stack.telegram.wait_until_polling()
    started = time.monotonic()
    subprocess.run(
        [
            *full_stack.docker,
            "stop",
            "--signal",
            "TERM",
            "-t",
            "60",
            full_stack.name,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=65,
    )
    assert time.monotonic() - started < 55
    assert full_stack.container_exit_code() == 0
    assert full_stack.captured_identities_gone(identities)
    top = subprocess.run(
        [*full_stack.docker, "top", full_stack.name],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert top.returncode != 0


@pytest.mark.postgres_integration
def test_hosted_mcp_matrix_write_replay_and_conflicts(full_stack):
    import httpx
    import psycopg

    token = full_stack.markers["iwiki_token"]
    with httpx.Client(base_url="http://127.0.0.1:8766", timeout=20) as client:
        initialized = _initialize(client, token)
        assert initialized.status_code == 200
        session_id = initialized.headers["mcp-session-id"]
        assert _request(
            client,
            token,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session_id,
        ).status_code == 202
        status = _tool(client, token, session_id, "wiki_status", {})
        assert status["storage"] == "postgres"
        assert status["domains"] == ["docs"]

        markdown = "# Acceptance\n\n## Body\nfirst version\n"
        created = _tool(
            client,
            token,
            session_id,
            "wiki_write_page",
            {
                "domain": "docs",
                "slug": "write-once",
                "markdown": markdown,
                "source": "acceptance-test",
            },
        )
        assert created.get("revision") == 1, created
        replay = _tool(
            client,
            token,
            session_id,
            "wiki_write_page",
            {
                "domain": "docs",
                "slug": "write-once",
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
            {"domain": "docs", "slug": "concept/write-once"},
        )
        section = _tool(
            client,
            token,
            session_id,
            "wiki_read_page",
            {
                "domain": "docs",
                "slug": "concept/write-once",
                "heading": "Body",
            },
        )
        revision_conflict = _tool(
            client,
            token,
            session_id,
            "wiki_update_page",
            {
                "domain": "docs",
                "slug": "concept/write-once",
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
                "slug": "concept/write-once",
                "heading": "Body",
                "new_body": "second version",
                "expected_revision": page["revision"],
                "expected_section_hash": "0" * 16,
            },
        )
        assert hash_conflict["error"] == "section_conflict"

    sent = len(full_stack.telegram.sent_payloads)
    full_stack.enqueue_message(text="/domains")
    domains = full_stack.wait_for_sent(
        sent, lambda payload: _callback_data(payload, "domain:") is not None
    )
    assert _callback_data(domains, "domain:") == "domain:docs"
    sent = len(full_stack.telegram.sent_payloads)
    full_stack.enqueue_callback("domain:docs")
    assert full_stack.wait_for_sent(
        sent,
        lambda payload: payload.get("text") == "Selected domain: docs",
    )

    sent = len(full_stack.telegram.sent_payloads)
    full_stack.enqueue_message(
        text="/create confirmed: deterministic request"
    )
    preview = full_stack.wait_for_sent(
        sent, lambda payload: _callback_data(payload, "confirm:") is not None
    )
    confirmation = _callback_data(preview, "confirm:")
    assert confirmation
    sent = len(full_stack.telegram.sent_payloads)
    full_stack.enqueue_callback(confirmation)
    assert full_stack.wait_for_sent(
        sent,
        lambda payload: payload.get("text") == "Page change saved.",
    )
    sent = len(full_stack.telegram.sent_payloads)
    full_stack.enqueue_callback(confirmation)
    assert full_stack.wait_for_sent(
        sent,
        lambda payload: payload.get("text") == "Confirmation is invalid.",
    )

    with psycopg.connect(full_stack.endpoint.dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM iwiki.pages p JOIN iwiki.domains d "
                "ON d.iwiki_id = p.iwiki_id AND d.domain_id = p.domain_id "
                "WHERE p.iwiki_id = 'wiki-a' AND d.slug = 'docs' "
                "AND p.slug = 'concept/write-once'"
            )
            assert cursor.fetchone()[0] == 1
            cursor.execute(
                "SELECT count(*) FROM iwiki.pages p JOIN iwiki.domains d "
                "ON d.iwiki_id = p.iwiki_id AND d.domain_id = p.domain_id "
                "WHERE p.iwiki_id = 'wiki-a' AND d.slug = 'docs' "
                "AND p.slug = 'concept/confirmed'"
            )
            assert cursor.fetchone()[0] == 1
    compose = (REPOSITORY / "compose.yaml").read_text(encoding="utf-8").lower()
    assert "postgres:" not in compose


@pytest.mark.postgres_integration
def test_hosted_mcp_rejects_unprovisioned_runtime_principal(
    postgres_endpoint, hosted_startup_probe
):
    import psycopg
    from psycopg import sql

    from iwiki_mcp.postgres.auth import AuthStore
    from iwiki_mcp.postgres.migrations import MigrationSettings, run_migrations

    run_migrations(
        MigrationSettings(
            dsn=postgres_endpoint.dsn,
            embed_model="acceptance-embedding",
            embed_dimensions=3,
            statement_timeout_ms=30_000,
            lock_timeout_ms=5_000,
        )
    )
    auth = AuthStore(postgres_endpoint.dsn)
    auth.create_wiki("wiki-a", "wiki-a")
    auth.create_domain("wiki-a", "docs")
    role, password = postgres_endpoint.create_role("unprovisioned")
    with psycopg.connect(postgres_endpoint.dsn) as connection:
        with connection.cursor() as cursor:
            identifier = sql.Identifier(role)
            cursor.execute(
                sql.SQL("GRANT USAGE ON SCHEMA iwiki TO {}").format(identifier)
            )
            cursor.execute(
                sql.SQL("GRANT SELECT ON iwiki.schema_migrations TO {}").format(
                    identifier
                )
            )
        connection.commit()

    result = hosted_startup_probe(role, password)

    assert result["exit_code"] != 0
    assert "hosted principal" in result["logs"]
    assert password not in result["logs"]


@pytest.mark.postgres_integration
def test_hosted_mcp_rejects_incompatible_schema(
    postgres_endpoint, hosted_startup_probe
):
    from iwiki_mcp.postgres import migrations

    migrations.run_migrations(
        migrations.MigrationSettings(
            dsn=postgres_endpoint.dsn,
            embed_model="acceptance-embedding",
            embed_dimensions=3,
            statement_timeout_ms=30_000,
            lock_timeout_ms=5_000,
        ),
        migrations=migrations.MIGRATIONS[:4],
    )
    role = postgres_endpoint.values["user"]
    password = postgres_endpoint.values.get("password", "")

    result = hosted_startup_probe(role, password)

    assert result["exit_code"] != 0
    assert "schema version 7 is required" in result["logs"]
    assert password not in result["logs"]


@pytest.mark.postgres_integration
def test_full_stack_failure_boundaries_do_not_persist_private_markers(full_stack):
    import httpx

    assert full_stack.inference.base_url.startswith("https://")
    mounts = json.loads(
        subprocess.run(
            [
                *full_stack.docker,
                "inspect",
                full_stack.name,
                "--format",
                "{{json .Mounts}}",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout
    )
    assert any(
        mount["Source"] == str(full_stack.combined_ca)
        and mount["Destination"] == "/etc/ssl/certs/ca-certificates.crt"
        and mount["RW"] is False
        for mount in mounts
    )
    assert any(
        mount["Source"] == str(full_stack.combined_ca)
        and mount["Destination"] == full_stack.httpx_ca_path
        and mount["RW"] is False
        for mount in mounts
    )

    token = full_stack.markers["iwiki_token"]
    with httpx.Client(base_url="http://127.0.0.1:8766", timeout=30) as client:
        initialized = _initialize(client, token)
        assert initialized.status_code == 200
        session_id = initialized.headers["mcp-session-id"]
        assert _request(
            client,
            token,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session_id,
        ).status_code == 202
        seeded = _tool(
            client,
            token,
            session_id,
            "wiki_write_page",
            {
                "domain": "docs",
                "slug": "privacy-context",
                "markdown": "# Context\n\n## Body\nacceptance context\n",
                "source": "acceptance-test",
            },
        )
        assert seeded.get("revision") == 1, seeded

        oversized = full_stack.markers["update"].encode() + b"x" * (
            16 * 1024 * 1024 + 1
        )
        response = client.post(
            "/mcp",
            headers={"Host": "acceptance.invalid"},
            content=oversized,
        )
        assert response.status_code == 413

        invalid = client.post(
            "/mcp",
            headers={
                "Host": "acceptance.invalid",
                "Authorization": f"Bearer {full_stack.markers['provider_error']}",
            },
            json={"jsonrpc": "2.0", "id": 9, "method": "initialize"},
        )
        assert invalid.status_code == 401

    sent = len(full_stack.telegram.sent_payloads)
    full_stack.enqueue_message(text="/domains")
    domains = full_stack.wait_for_sent(
        sent, lambda payload: _callback_data(payload, "domain:") is not None
    )
    assert _callback_data(domains, "domain:") == "domain:docs"
    sent = len(full_stack.telegram.sent_payloads)
    full_stack.enqueue_callback("domain:docs")
    assert full_stack.wait_for_sent(
        sent, lambda payload: payload.get("text") == "Selected domain: docs"
    )

    sent = len(full_stack.telegram.sent_payloads)
    full_stack.enqueue_message(text=full_stack.markers["update"])
    assert full_stack.wait_for_sent(
        sent,
        lambda payload: payload.get("text") == full_stack.markers["reply"],
    )
    assert any(
        full_stack.markers["update"].encode() in request["body"]
        for request in full_stack.inference.requests
        if request["method"] == "POST"
        and request["path"] == "/v1/chat/completions"
    )

    sent = len(full_stack.telegram.sent_payloads)
    full_stack.enqueue_message(voice=True)
    assert full_stack.wait_for_sent(
        sent,
        lambda payload: payload.get("text") == full_stack.markers["reply"],
    )
    transcription_requests = [
        request["body"]
        for request in full_stack.inference.requests
        if request["method"] == "POST"
        and request["path"] == "/v1/audio/transcriptions"
    ]
    assert any(
        full_stack.markers["filename"].encode() in body
        and full_stack.markers["audio"].encode() in body
        for body in transcription_requests
    )
    assert any(
        full_stack.markers["transcription"].encode() in body
        for request in full_stack.inference.requests
        if request["method"] == "POST"
        and request["path"] != "/v1/audio/transcriptions"
        for body in (request["body"],)
    )
    required_inference_paths = {
        "/v1/models",
        "/v1/embeddings",
        "/v1/chat/completions",
        "/v1/audio/transcriptions",
    }
    exercised = {
        request["path"]
        for request in full_stack.inference.requests
        if request["path"] in required_inference_paths
    }
    assert exercised == required_inference_paths
    assert all(
        request["authorization"]
        == f"Bearer {full_stack.markers['llm_key']}"
        for request in full_stack.inference.requests
        if request["path"] in required_inference_paths
    )

    sent = len(full_stack.telegram.sent_payloads)
    full_stack.enqueue_message(
        text=f"/create privacy-preview: {full_stack.markers['update']}"
    )
    preview = full_stack.wait_for_sent(
        sent, lambda payload: _callback_data(payload, "confirm:") is not None
    )
    confirmation = _callback_data(preview, "confirm:")
    assert confirmation
    assert full_stack.markers["preview"] in preview["text"]

    full_stack.inference.server.fail_paths.add("/v1/chat/completions")
    sent = len(full_stack.telegram.sent_payloads)
    full_stack.enqueue_message(text=f"failure {full_stack.markers['update']}")
    assert full_stack.wait_for_sent(
        sent,
        lambda payload: payload.get("text")
        == "Inference service is unavailable.",
    )
    full_stack.inference.server.fail_paths.remove("/v1/chat/completions")

    full_stack.inference.server.fail_paths.add("/v1/audio/transcriptions")
    sent = len(full_stack.telegram.sent_payloads)
    full_stack.enqueue_message(voice=True)
    assert full_stack.wait_for_sent(
        sent,
        lambda payload: payload.get("text")
        == "Voice transcription is unavailable.",
    )
    full_stack.inference.server.fail_paths.remove("/v1/audio/transcriptions")

    supervisor = [
        *full_stack.docker,
        "exec",
        full_stack.name,
        "supervisorctl",
        "-c",
        "/etc/supervisor/supervisord.conf",
    ]
    subprocess.run(
        [*supervisor, "stop", "iwiki-mcp"],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    retry_message = "telegram bot remote session retry"
    retry_count = full_stack.stable_log_count(retry_message)
    sent = len(full_stack.telegram.sent_payloads)
    attempted_while_down = full_stack.enqueue_message(text="/domains")
    assert full_stack.telegram.wait_until_update_consumed(
        attempted_while_down["update_id"], timeout=10
    )
    assert _eventually(
        lambda: full_stack.stable_log_count(retry_message) > retry_count,
        timeout=20,
    )
    subprocess.run(
        [*supervisor, "start", "iwiki-mcp"],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    domains = full_stack.wait_for_sent(
        sent,
        lambda payload: _callback_data(payload, "domain:") is not None,
        timeout=30,
    )
    assert _callback_data(domains, "domain:") == "domain:docs"
    assert all(
        payload.get("text") != "Wiki service is unavailable."
        for payload in full_stack.telegram.sent_payloads[sent:]
    )

    subprocess.run(
        [*supervisor, "stop", "iwiki-mcp"],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    subprocess.run(
        [*supervisor, "start", "iwiki-mcp"],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    assert _eventually(
        lambda: full_stack.supervisor_status().get("iwiki-mcp", {}).get(
            "state"
        )
        == "RUNNING",
        timeout=20,
    )

    def restarted_mcp_accepts_fresh_session():
        try:
            with httpx.Client(
                base_url="http://127.0.0.1:8766", timeout=5
            ) as client:
                return _initialize(client, token).status_code == 200
        except httpx.HTTPError:
            return False

    assert _eventually(restarted_mcp_accepts_fresh_session, timeout=20)
    retry_count = full_stack.stable_log_count(retry_message)
    sent = len(full_stack.telegram.sent_payloads)
    stale_session_update = full_stack.enqueue_message(text="/domains")
    assert full_stack.telegram.wait_until_update_consumed(
        stale_session_update["update_id"], timeout=10
    )
    assert _eventually(
        lambda: full_stack.stable_log_count(retry_message) > retry_count,
        timeout=20,
    )
    domains = full_stack.wait_for_sent(
        sent,
        lambda payload: _callback_data(payload, "domain:") is not None,
        timeout=30,
    )
    assert _callback_data(domains, "domain:") == "domain:docs"
    assert all(
        payload.get("text") != "Wiki service is unavailable."
        for payload in full_stack.telegram.sent_payloads[sent:]
    )

    full_stack.telegram.disable()
    assert full_stack.telegram.wait_until_disabled_quiescent()
    request_count = full_stack.telegram.request_count
    generation = full_stack.telegram.enable()
    assert full_stack.telegram.wait_for_get_updates_generation(
        generation, timeout=20
    )
    assert full_stack.telegram.request_count > request_count

    logs = subprocess.run(
        [*full_stack.docker, "logs", full_stack.name],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    combined_logs = logs.stdout + logs.stderr
    runtime_scan = subprocess.run(
        [
            *full_stack.docker,
            "exec",
            full_stack.name,
            "/app/.venv/bin/python",
            "-c",
            (
                "import pathlib,sys; markers=[bytes.fromhex(x) for x in sys.argv[1:]]; "
                "paths=[p for root in ('/run','/tmp') for p in pathlib.Path(root).rglob('*') "
                "if p.is_file()]+[pathlib.Path(p) for p in "
                "('/etc/iwiki/server.toml','/etc/nginx/nginx.conf',"
                "'/etc/ssl/certs/ca-certificates.crt')]; "
                "bad=[str(p) for p in paths if any(m in p.read_bytes() "
                "for m in markers)]; print('\\n'.join(bad))"
            ),
            *[value.encode().hex() for value in full_stack.markers.values()],
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert runtime_scan.stdout == "\n"

    credentials = {
        "telegram_token",
        "iwiki_token",
        "llm_key",
        "proxy_user",
        "proxy_password",
        "proxy_origin",
        "database_password",
    }
    content_markers = set(full_stack.markers) - credentials
    runtime_env = full_stack.runtime_env.read_text(encoding="utf-8")
    for name in credentials:
        assert full_stack.markers[name] in runtime_env
    for name in content_markers:
        assert full_stack.markers[name] not in runtime_env
    for path in (
        full_stack.server_config,
        full_stack.nginx_config,
        full_stack.ca_cert,
        full_stack.combined_ca,
    ):
        content = path.read_bytes()
        assert all(value.encode() not in content for value in full_stack.markers.values())

    history = subprocess.run(
        [*full_stack.docker, "history", "--no-trunc", full_stack.image],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    ).stdout
    for marker in full_stack.markers.values():
        assert marker not in combined_logs
        assert marker not in history

    original_id = full_stack.container_id()
    subprocess.run(
        [*full_stack.docker, "restart", "-t", "60", full_stack.name],
        capture_output=True,
        text=True,
        check=True,
        timeout=65,
    )
    assert _eventually(lambda: full_stack.health_probe().returncode == 0, timeout=45)
    assert full_stack.container_id() == original_id
    sent = len(full_stack.telegram.sent_payloads)
    full_stack.enqueue_callback(confirmation)
    assert full_stack.wait_for_sent(
        sent, lambda payload: payload.get("text") == "Confirmation is invalid."
    )
    sent = len(full_stack.telegram.sent_payloads)
    full_stack.enqueue_message(text=full_stack.markers["update"])
    assert full_stack.wait_for_sent(
        sent, lambda payload: payload.get("text") == "Select a domain first."
    )

    with httpx.Client(base_url="http://127.0.0.1:8766", timeout=30) as client:
        assert _initialize(client, token).status_code == 200
    sent = len(full_stack.telegram.sent_payloads)
    full_stack.enqueue_message(text="/domains")
    domains = full_stack.wait_for_sent(
        sent, lambda payload: _callback_data(payload, "domain:") is not None
    )
    assert _callback_data(domains, "domain:") == "domain:docs"
    sent = len(full_stack.telegram.sent_payloads)
    full_stack.enqueue_callback("domain:docs")
    assert full_stack.wait_for_sent(
        sent, lambda payload: payload.get("text") == "Selected domain: docs"
    )
    sent = len(full_stack.telegram.sent_payloads)
    full_stack.enqueue_message(text=full_stack.markers["update"])
    assert full_stack.wait_for_sent(
        sent,
        lambda payload: payload.get("text") == full_stack.markers["reply"],
    )
    sent = len(full_stack.telegram.sent_payloads)
    full_stack.enqueue_message(voice=True)
    assert full_stack.wait_for_sent(
        sent,
        lambda payload: payload.get("text") == full_stack.markers["reply"],
    )
    full_stack.telegram.disable()
    assert full_stack.telegram.wait_until_disabled_quiescent()
    generation = full_stack.telegram.enable()
    assert full_stack.telegram.wait_for_connect_generation(generation, timeout=20)
    assert full_stack.telegram.wait_for_get_updates_generation(
        generation, timeout=20
    )

    snapshot = full_stack.private_marker_snapshot()
    assert snapshot["runtime_bad_paths"] == []
    for marker in full_stack.markers.values():
        assert marker not in snapshot["logs"]
        assert marker not in snapshot["mounts"]
        assert marker not in snapshot["history"]
        assert all(
            marker.encode() not in content
            for content in snapshot["mounted_files"].values()
        )
