from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import subprocess
import threading
import time
import uuid

import pytest


def _unused_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class _UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        pass

    def _authorization(self):
        value = self.headers.get("Authorization")
        self.server.authorizations.append(value)
        return value

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        authorization = self._authorization()
        self.server.requests.append(
            {
                "path": self.path,
                "authorization": authorization,
                "marker": self.headers.get("X-Acceptance-Marker"),
                "body": body,
            }
        )
        if authorization != self.server.valid_authorization:
            body = b"upstream rejected authorization"
            self.send_response(401)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.server.accepted_body_lengths.append(len(body))
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path != "/stream":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b"first\n")
        self.wfile.flush()
        self.server.stream_started.set()
        self.server.release_stream.wait()
        self.wfile.write(b"second\n")
        self.wfile.flush()
        self.server.stream_completed.set()


@pytest.fixture
def running_nginx(tmp_path, docker_command, acceptance_image):
    upstream_port = _unused_port()
    upstream = ThreadingHTTPServer(
        ("127.0.0.1", upstream_port), _UpstreamHandler
    )
    ingress_port = _unused_port()
    upstream.authorizations = []
    upstream.requests = []
    upstream.accepted_body_lengths = []
    upstream.valid_authorization = "Bearer ingress-marker"
    upstream.stream_started = threading.Event()
    upstream.release_stream = threading.Event()
    upstream.stream_completed = threading.Event()
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()

    config = tmp_path / "nginx.conf"
    from tests.deployment.conftest import derive_nginx_config

    source = (Path(__file__).parents[2] / "deploy/nginx.conf.example").read_text(
        encoding="utf-8"
    )
    config.write_text(
        derive_nginx_config(
            source,
            listen=f"127.0.0.1:{ingress_port}",
            upstream=f"127.0.0.1:{upstream_port}",
        ),
        encoding="utf-8",
    )
    upstream.nginx_config = config
    name = f"iwiki-nginx-acceptance-{uuid.uuid4().hex[:10]}"
    try:
        subprocess.run(
            [
                *docker_command,
                "run",
                "-d",
                "--name",
                name,
                "--network",
                "host",
                "--tmpfs",
                "/run:uid=10001,gid=10001,mode=0750",
                "--tmpfs",
                "/tmp:uid=10001,gid=10001,mode=1770",
                "-v",
                f"{config}:/etc/nginx/nginx.conf:ro",
                "--entrypoint",
                "/usr/sbin/nginx",
                acceptance_image,
                "-g",
                "daemon off;",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", ingress_port), 0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            logs = subprocess.run(
                [*docker_command, "logs", name],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            pytest.fail(f"nginx test container did not listen: {logs.stderr[-300:]}")
        yield name, ingress_port, upstream
    finally:
        upstream.release_stream.set()
        subprocess.run(
            [*docker_command, "rm", "-f", name],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=5)


def _post(port, body, authorization=None):
    headers = {"Content-Type": "application/octet-stream"}
    if authorization is not None:
        headers["Authorization"] = authorization
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("POST", "/mcp", body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def test_authorization_is_preserved_and_rejected_only_by_upstream(running_nginx):
    _name, port, upstream = running_nginx

    valid = _post(port, b"{}", "Bearer ingress-marker")
    missing = _post(port, b"{}")
    invalid = _post(port, b"{}", "Bearer invalid-marker")

    assert valid == (204, b"")
    assert missing == (401, b"upstream rejected authorization")
    assert invalid == (401, b"upstream rejected authorization")
    assert upstream.authorizations == [
        "Bearer ingress-marker",
        None,
        "Bearer invalid-marker",
    ]


def test_body_limit_accepts_exactly_16_mib_and_rejects_larger(running_nginx):
    _name, port, upstream = running_nginx
    before = len(upstream.requests)

    exact_status, _ = _post(port, b"x" * (16 * 1024 * 1024), "Bearer ingress-marker")
    after_exact = len(upstream.requests)
    over_status, _ = _post(
        port, b"x" * (16 * 1024 * 1024 + 1), "Bearer ingress-marker"
    )

    assert exact_status == 204
    assert upstream.accepted_body_lengths == [16 * 1024 * 1024]
    assert after_exact == before + 1
    assert over_status == 413
    assert len(upstream.requests) == after_exact


def test_running_nginx_uses_exact_derived_production_config(running_nginx):
    from tests.deployment.conftest import derive_nginx_config

    _name, port, upstream = running_nginx
    source = (Path(__file__).parents[2] / "deploy/nginx.conf.example").read_text(
        encoding="utf-8"
    )
    assert upstream.nginx_config.read_text(encoding="utf-8") == (
        derive_nginx_config(
            source,
            listen=f"127.0.0.1:{port}",
            upstream=f"127.0.0.1:{upstream.server_port}",
        )
    )


def test_streaming_arrives_before_upstream_completion(running_nginx):
    name, port, upstream = running_nginx
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", "/stream")
        response = connection.getresponse()
        first = response.read(len(b"first\n"))
        assert response.status == 200
        assert upstream.stream_started.wait(1)
        assert not upstream.stream_completed.is_set()
        assert first == b"first\n"
        upstream.release_stream.set()
        assert response.read() == b"second\n"
        assert upstream.stream_completed.wait(1)
    finally:
        upstream.release_stream.set()
        connection.close()


def test_access_log_omits_marker_sent_through_request(
    running_nginx, docker_command
):
    name, port, upstream = running_nginx
    marker = f"nginx-access-{uuid.uuid4().hex}"
    upstream.valid_authorization = f"Bearer {marker}"
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        body = marker.encode("ascii")
        connection.request(
            "POST",
            f"/mcp/{marker}",
            body=body,
            headers={
                "Authorization": f"Bearer {marker}",
                "X-Acceptance-Marker": marker,
                "Content-Type": "application/octet-stream",
            },
        )
        response = connection.getresponse()
        assert response.status == 204
        response.read()
    finally:
        connection.close()

    assert upstream.requests[-1] == {
        "path": f"/mcp/{marker}",
        "authorization": f"Bearer {marker}",
        "marker": marker,
        "body": marker.encode("ascii"),
    }
    logs = subprocess.run(
        [*docker_command, "logs", name],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert marker not in logs.stdout + logs.stderr


@pytest.mark.postgres_integration
def test_actual_mcp_auth_rejection_matches_direct_upstream(full_stack):
    import httpx

    from tests.deployment.conftest import derive_nginx_config

    source = (Path(__file__).parents[2] / "deploy/nginx.conf.example").read_text(
        encoding="utf-8"
    )
    assert full_stack.nginx_config.read_text(encoding="utf-8") == (
        derive_nginx_config(
            source,
            listen="127.0.0.1:8766",
            upstream="127.0.0.1:8765",
        )
    )

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "auth-proof", "version": "1"},
        },
    }
    cases = (
        {"Host": "acceptance.invalid"},
        {
            "Host": "acceptance.invalid",
            "Authorization": f"Bearer invalid-{uuid.uuid4().hex}",
        },
    )
    with httpx.Client(timeout=10) as client:
        for headers in cases:
            direct = client.post(
                "http://127.0.0.1:8765/mcp", headers=headers, json=payload
            )
            ingress = client.post(
                "http://127.0.0.1:8766/mcp", headers=headers, json=payload
            )
            assert ingress.status_code == direct.status_code == 401
            assert ingress.content == direct.content
            assert ingress.headers.get("www-authenticate") == direct.headers.get(
                "www-authenticate"
            )


def test_production_nginx_source_disables_buffering_and_access_logs():
    source = (Path(__file__).parents[2] / "deploy/nginx.conf.example").read_text(
        encoding="utf-8"
    )
    assert "access_log off;" in source
    assert "proxy_buffering off;" in source
    assert "proxy_request_buffering off;" in source
    assert "proxy_set_header Authorization $http_authorization;" in source
    assert "client_max_body_size 16m;" in source
