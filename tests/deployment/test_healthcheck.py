from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


HEALTHCHECK_PATH = Path(__file__).parents[2] / "deploy" / "healthcheck.py"
SPEC = importlib.util.spec_from_file_location("deployment_healthcheck", HEALTHCHECK_PATH)
assert SPEC is not None
assert SPEC.loader is not None
healthcheck = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(healthcheck)


HEALTHY_STATUS = "\n".join(
    (
        "iwiki-mcp RUNNING pid 10, uptime 0:00:10",
        "nginx RUNNING pid 11, uptime 0:00:10",
        "telegram-bot RUNNING pid 12, uptime 0:00:10",
    )
)
ENVIRONMENT = {"IWIKI_INGRESS_HEALTH_HOST": "192.168.68.123"}


class FakeResponse:
    def __init__(self, status):
        self.status = status
        self.closed = False

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, host, port, timeout=None, *, status=401, error=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.status = status
        self.error = error
        self.requests = []
        self.responses = []
        self.closed = False

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))
        if self.error is not None:
            raise self.error

    def getresponse(self):
        response = FakeResponse(self.status)
        self.responses.append(response)
        return response

    def close(self):
        self.closed = True


class FakePath:
    def __init__(self, content="100.0", error=None):
        self.content = content
        self.error = error
        self.encodings = []

    def read_text(self, *, encoding):
        self.encodings.append(encoding)
        if self.error is not None:
            raise self.error
        return self.content


def completed_status(stdout=HEALTHY_STATUS, stderr=""):
    return SimpleNamespace(returncode=0, stdout=stdout, stderr=stderr)


def run_main(
    capsys,
    *,
    process_result=None,
    http_statuses=(401, 405),
    heartbeat_content="100.0",
    heartbeat_error=None,
    clock=lambda: 100.0,
    environment=None,
):
    process_calls = []
    http_connections = []
    paths = []
    statuses = iter(http_statuses)

    def run(*args, **kwargs):
        process_calls.append((args, kwargs))
        return process_result or completed_status()

    def connection_factory(host, port, timeout):
        connection = FakeConnection(host, port, timeout, status=next(statuses))
        http_connections.append(connection)
        return connection

    def path_factory(value):
        paths.append(value)
        return FakePath(heartbeat_content, heartbeat_error)

    exit_code = healthcheck.main(
        run=run,
        connection_factory=connection_factory,
        path_factory=path_factory,
        clock=clock,
        environ=environment or ENVIRONMENT,
    )
    captured = capsys.readouterr()
    return exit_code, captured, process_calls, http_connections, paths


def test_required_children_are_exact():
    assert healthcheck.REQUIRED_CHILDREN == frozenset(
        {"iwiki-mcp", "nginx", "telegram-bot"}
    )


def test_all_local_checks_healthy(capsys):
    exit_code, captured, process_calls, connections, paths = run_main(capsys)

    assert exit_code == 0
    assert captured.out == ""
    assert captured.err == ""
    assert process_calls == [
        (
            (),
            {
                "args": [
                    "supervisorctl",
                    "-c",
                    "/etc/supervisor/supervisord.conf",
                    "status",
                ],
                "capture_output": True,
                "text": True,
                "check": False,
                "timeout": 2.0,
            },
        )
    ]
    assert [(item.host, item.port) for item in connections] == [
        ("127.0.0.1", 8765),
        ("192.168.68.123", 8766),
    ]
    assert [item.timeout for item in connections] == [2.0, 2.0]
    assert [item.requests for item in connections] == [
        [(('GET', '/mcp'), {})],
        [(('GET', '/mcp'), {})],
    ]
    assert all(item.closed for item in connections)
    assert all(item.responses[0].closed for item in connections)
    assert paths == ["/run/iwiki-telegram-bot.heartbeat"]


@pytest.mark.parametrize("missing_child", sorted(("iwiki-mcp", "nginx", "telegram-bot")))
def test_each_missing_child_fails_without_later_checks(capsys, missing_child):
    marker = "missing-child-sensitive-marker"
    status = "\n".join(
        line for line in HEALTHY_STATUS.splitlines() if not line.startswith(missing_child)
    )
    result = completed_status(status, marker)

    exit_code, captured, _, connections, paths = run_main(
        capsys, process_result=result, http_statuses=()
    )

    assert exit_code == 1
    assert captured.out == "child_not_running\n"
    assert captured.err == ""
    assert marker not in captured.out + captured.err
    assert connections == []
    assert paths == []


@pytest.mark.parametrize("child", sorted(("iwiki-mcp", "nginx", "telegram-bot")))
def test_each_non_running_child_fails_without_raw_status(capsys, child):
    marker = "non-running-sensitive-marker"
    status = HEALTHY_STATUS.replace(
        f"{child} RUNNING", f"{child} FATAL {marker}"
    )

    exit_code, captured, _, connections, paths = run_main(
        capsys, process_result=completed_status(status), http_statuses=()
    )

    assert exit_code == 1
    assert captured.out == "child_not_running\n"
    assert captured.err == ""
    assert marker not in captured.out + captured.err
    assert connections == []
    assert paths == []


def test_process_failure_uses_only_stable_code(capsys):
    marker = "process-exception-sensitive-marker"

    def failing_run(*args, **kwargs):
        raise OSError(marker)

    exit_code = healthcheck.main(
        run=failing_run,
        connection_factory=lambda *args: pytest.fail("HTTP check must not run"),
        path_factory=lambda value: pytest.fail("heartbeat check must not run"),
        clock=lambda: 100.0,
        environ=ENVIRONMENT,
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == "child_not_running\n"
    assert captured.err == ""
    assert marker not in captured.out + captured.err


def test_process_timeout_uses_stable_code_without_later_checks(capsys):
    marker = "process-timeout-sensitive-marker"
    calls = []

    def timing_out_run(*args, **kwargs):
        calls.append((args, kwargs))
        raise subprocess.TimeoutExpired([marker], kwargs["timeout"])

    exit_code = healthcheck.main(
        run=timing_out_run,
        connection_factory=lambda *args: pytest.fail("HTTP check must not run"),
        path_factory=lambda value: pytest.fail("heartbeat check must not run"),
        clock=lambda: 100.0,
        environ=ENVIRONMENT,
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == "child_not_running\n"
    assert captured.err == ""
    assert calls[0][1]["timeout"] == 2.0
    assert marker not in captured.out + captured.err


@pytest.mark.parametrize("boundary", ["process", "http", "file", "clock"])
def test_injected_boundary_exceptions_never_escape_or_print(capsys, boundary):
    marker = f"{boundary}-unexpected-sensitive-marker"

    def fail():
        raise RuntimeError(marker)

    if boundary == "process":
        result = healthcheck.children_running(
            healthcheck.REQUIRED_CHILDREN, lambda **kwargs: fail()
        )
    elif boundary == "http":
        result = healthcheck.local_http_ok(
            "127.0.0.1", 8765, "/mcp", lambda host, port, timeout: fail()
        )
    elif boundary == "file":
        result = healthcheck.check_heartbeat(
            FakePath(error=RuntimeError(marker)), 120.0, lambda: 100.0
        )
    else:
        result = healthcheck.check_heartbeat(FakePath(), 120.0, fail)

    captured = capsys.readouterr()
    assert result is False
    assert captured.out == ""
    assert captured.err == ""
    assert marker not in captured.out + captured.err


@pytest.mark.parametrize("healthy_status", [401, 405])
def test_http_check_accepts_only_expected_unauthenticated_statuses(healthy_status):
    connections = []

    def factory(host, port, timeout):
        connection = FakeConnection(host, port, timeout, status=healthy_status)
        connections.append(connection)
        return connection

    assert healthcheck.local_http_ok("127.0.0.1", 8765, "/mcp", factory)
    assert connections[0].timeout == 2.0
    assert connections[0].requests == [(('GET', '/mcp'), {})]
    assert connections[0].responses[0].closed is True
    assert connections[0].closed is True


@pytest.mark.parametrize("unhealthy_status", [200, 204, 400, 403, 404, 500])
def test_http_check_rejects_other_statuses(unhealthy_status):
    assert not healthcheck.local_http_ok(
        "127.0.0.1",
        8765,
        "/mcp",
        lambda host, port, timeout: FakeConnection(
            host, port, timeout, status=unhealthy_status
        ),
    )


def test_http_check_sends_no_credentials_and_hides_connection_failure(capsys):
    marker = "http-exception-sensitive-marker"
    connection = FakeConnection("127.0.0.1", 8765, error=OSError(marker))

    assert not healthcheck.local_http_ok(
        "127.0.0.1", 8765, "/mcp", lambda host, port, timeout: connection
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert marker not in captured.out + captured.err
    assert connection.requests == [(('GET', '/mcp'), {})]
    assert connection.closed is True


def test_http_timeout_is_bounded_and_hides_failure(capsys):
    marker = "http-timeout-sensitive-marker"
    connections = []

    def factory(host, port, timeout):
        connection = FakeConnection(
            host, port, timeout, error=TimeoutError(marker)
        )
        connections.append(connection)
        return connection

    assert not healthcheck.local_http_ok("127.0.0.1", 8765, "/mcp", factory)
    captured = capsys.readouterr()

    assert len(connections) == 1
    assert connections[0].timeout == 2.0
    assert connections[0].closed is True
    assert captured.out == ""
    assert captured.err == ""
    assert marker not in captured.out + captured.err


def test_mcp_http_timeout_uses_stable_code_without_later_checks(capsys):
    marker = "mcp-timeout-sensitive-marker"
    connections = []

    def factory(host, port, timeout):
        connection = FakeConnection(
            host, port, timeout, error=TimeoutError(marker)
        )
        connections.append(connection)
        return connection

    exit_code = healthcheck.main(
        run=lambda **kwargs: completed_status(),
        connection_factory=factory,
        path_factory=lambda value: pytest.fail("heartbeat check must not run"),
        clock=lambda: 100.0,
        environ=ENVIRONMENT,
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == "mcp_unavailable\n"
    assert captured.err == ""
    assert len(connections) == 1
    assert connections[0].timeout == 2.0
    assert connections[0].closed is True
    assert marker not in captured.out + captured.err


def test_mcp_failure_precedes_nginx_and_heartbeat(capsys):
    exit_code, captured, _, connections, paths = run_main(
        capsys, http_statuses=(500,)
    )

    assert exit_code == 1
    assert captured.out == "mcp_unavailable\n"
    assert captured.err == ""
    assert len(connections) == 1
    assert paths == []


def test_nginx_failure_precedes_heartbeat(capsys):
    exit_code, captured, _, connections, paths = run_main(
        capsys, http_statuses=(401, 500)
    )

    assert exit_code == 1
    assert captured.out == "nginx_unavailable\n"
    assert captured.err == ""
    assert len(connections) == 2
    assert paths == []


@pytest.mark.parametrize(
    ("content", "error", "now"),
    [
        ("ignored", FileNotFoundError("missing-sensitive-marker"), 100.0),
        ("malformed-sensitive-marker", None, 100.0),
        ("101.0", None, 100.0),
        ("-21.0", None, 100.0),
    ],
    ids=("missing", "malformed", "future", "stale"),
)
def test_heartbeat_failures_use_only_stable_code(capsys, content, error, now):
    exit_code, captured, _, _, paths = run_main(
        capsys,
        heartbeat_content=content,
        heartbeat_error=error,
        clock=lambda: now,
    )

    assert exit_code == 1
    assert captured.out == "telegram_heartbeat_stale\n"
    assert captured.err == ""
    assert "sensitive-marker" not in captured.out + captured.err
    assert paths == ["/run/iwiki-telegram-bot.heartbeat"]


@pytest.mark.parametrize(
    ("observed", "now", "expected"),
    [(100.0, 100.0, True), (-20.0, 100.0, True), (-20.001, 100.0, False), (101.0, 100.0, False)],
)
def test_heartbeat_age_is_inclusive_and_never_future(observed, now, expected):
    path = FakePath(str(observed))

    assert healthcheck.check_heartbeat(path, 120.0, lambda: now) is expected
    assert path.encodings == ["ascii"]


def test_environment_overrides_ingress_port_and_heartbeat_age(capsys):
    environment = {
        "IWIKI_INGRESS_HEALTH_HOST": "192.168.68.123",
        "IWIKI_INGRESS_HEALTH_PORT": "9876",
        "IWIKI_BOT_HEARTBEAT_MAX_AGE_SECONDS": "30",
    }

    exit_code, captured, _, connections, _ = run_main(
        capsys,
        heartbeat_content="70.0",
        clock=lambda: 100.0,
        environment=environment,
    )

    assert exit_code == 0
    assert captured.out == ""
    assert captured.err == ""
    assert connections[1].port == 9876


@pytest.mark.parametrize(
    "maximum_age",
    ["nan", "inf", "+inf", "-inf", "0", "-0.0", "-1", "age-sensitive-marker"],
)
def test_invalid_heartbeat_maximum_age_uses_stable_code(capsys, maximum_age):
    environment = {
        "IWIKI_INGRESS_HEALTH_HOST": "192.168.68.123",
        "IWIKI_BOT_HEARTBEAT_MAX_AGE_SECONDS": maximum_age,
    }

    exit_code, captured, _, connections, paths = run_main(
        capsys,
        heartbeat_content="-1000000.0",
        clock=lambda: 100.0,
        environment=environment,
    )

    assert exit_code == 1
    assert captured.out == "telegram_heartbeat_stale\n"
    assert captured.err == ""
    assert "sensitive-marker" not in captured.out + captured.err
    assert len(connections) == 2
    assert paths == []


@pytest.mark.parametrize(
    "port", ["0", "-1", "65536", "port-sensitive-marker"]
)
def test_invalid_ingress_health_port_uses_stable_code(capsys, port):
    environment = {
        "IWIKI_INGRESS_HEALTH_HOST": "192.168.68.123",
        "IWIKI_INGRESS_HEALTH_PORT": port,
    }

    exit_code, captured, _, connections, paths = run_main(
        capsys,
        http_statuses=(401,),
        environment=environment,
    )

    assert exit_code == 1
    assert captured.out == "nginx_unavailable\n"
    assert captured.err == ""
    assert "sensitive-marker" not in captured.out + captured.err
    assert len(connections) == 1
    assert paths == []
