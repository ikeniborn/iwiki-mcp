from __future__ import annotations

import http.client
import math
import os
from pathlib import Path
import subprocess
import time


REQUIRED_CHILDREN = frozenset({"iwiki-mcp", "nginx", "telegram-bot"})
SUPERVISOR_CONFIG = "/etc/supervisor/supervisord.conf"
HEARTBEAT_PATH = "/run/iwiki-telegram-bot.heartbeat"
PROBE_TIMEOUT_SECONDS = 2.0


def children_running(
    required_children,
    run=subprocess.run,
    timeout=PROBE_TIMEOUT_SECONDS,
):
    try:
        result = run(
            args=["supervisorctl", "-c", SUPERVISOR_CONFIG, "status"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        if result.returncode != 0:
            return False

        states = {}
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 2:
                states[fields[0]] = fields[1]
        return all(states.get(child) == "RUNNING" for child in required_children)
    except Exception:
        return False


def local_http_ok(
    host,
    port,
    path,
    connection_factory=http.client.HTTPConnection,
    timeout=PROBE_TIMEOUT_SECONDS,
):
    connection = None
    response = None
    healthy = False
    try:
        connection = connection_factory(host, port, timeout=timeout)
        connection.request("GET", path)
        response = connection.getresponse()
        healthy = response.status in {401, 405}
    except Exception:
        healthy = False
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                healthy = False
        if connection is not None:
            try:
                connection.close()
            except Exception:
                healthy = False
    return healthy


def check_heartbeat(path, maximum_age, clock):
    try:
        observed = float(path.read_text(encoding="ascii").strip())
        age = clock() - observed
        return 0 <= age <= maximum_age
    except Exception:
        return False


def fail(code):
    print(code)
    return 1


def main(
    *,
    run=subprocess.run,
    connection_factory=http.client.HTTPConnection,
    path_factory=Path,
    clock=time.monotonic,
    environ=None,
    probe_timeout=PROBE_TIMEOUT_SECONDS,
):
    if environ is None:
        environ = os.environ

    if not children_running(REQUIRED_CHILDREN, run, probe_timeout):
        return fail("child_not_running")
    if not local_http_ok(
        "127.0.0.1",
        8765,
        "/mcp",
        connection_factory,
        probe_timeout,
    ):
        return fail("mcp_unavailable")

    try:
        ingress_host = environ["IWIKI_INGRESS_HEALTH_HOST"]
        ingress_port = int(environ.get("IWIKI_INGRESS_HEALTH_PORT", "8766"))
        if not 1 <= ingress_port <= 65535:
            raise ValueError("invalid ingress health port")
    except (KeyError, TypeError, ValueError):
        return fail("nginx_unavailable")
    if not local_http_ok(
        ingress_host,
        ingress_port,
        "/mcp",
        connection_factory,
        probe_timeout,
    ):
        return fail("nginx_unavailable")

    try:
        maximum_age = float(
            environ.get("IWIKI_BOT_HEARTBEAT_MAX_AGE_SECONDS", "120")
        )
        if not math.isfinite(maximum_age) or maximum_age <= 0:
            raise ValueError("invalid heartbeat maximum age")
    except (TypeError, ValueError):
        return fail("telegram_heartbeat_stale")
    if not check_heartbeat(path_factory(HEARTBEAT_PATH), maximum_age, clock):
        return fail("telegram_heartbeat_stale")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
