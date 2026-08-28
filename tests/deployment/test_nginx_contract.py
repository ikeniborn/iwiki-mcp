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
        self.rfile.read(length)
        authorization = self._authorization()
        if authorization != self.server.valid_authorization:
            body = b"upstream rejected authorization"
            self.send_response(401)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
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
        self.server.first_chunk_sent.set()
        self.server.finish_stream.wait(10)
        self.wfile.write(b"second\n")
        self.wfile.flush()


@pytest.fixture
def running_nginx(tmp_path, docker_command, acceptance_image):
    upstream_port = _unused_port()
    upstream = ThreadingHTTPServer(
        ("127.0.0.1", upstream_port), _UpstreamHandler
    )
    ingress_port = _unused_port()
    upstream.authorizations = []
    upstream.valid_authorization = "Bearer ingress-marker"
    upstream.first_chunk_sent = threading.Event()
    upstream.finish_stream = threading.Event()
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()

    config = tmp_path / "nginx.conf"
    config.write_text(
        """worker_processes 1;
pid /run/nginx.pid;
error_log /dev/stderr warn;
events { worker_connections 128; }
http {
    access_log off;
    client_body_temp_path /tmp/client_temp;
    proxy_temp_path /tmp/proxy_temp;
    fastcgi_temp_path /tmp/fastcgi_temp;
    uwsgi_temp_path /tmp/uwsgi_temp;
    scgi_temp_path /tmp/scgi_temp;
    client_max_body_size 16m;
    server {
        listen 127.0.0.1:%d;
        location / {
            proxy_pass http://127.0.0.1:%d;
            proxy_http_version 1.1;
            proxy_set_header Authorization $http_authorization;
            proxy_set_header Host $host;
            proxy_buffering off;
            proxy_request_buffering off;
            proxy_read_timeout 30s;
        }
    }
}
"""
        % (ingress_port, upstream_port),
        encoding="utf-8",
    )
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
        upstream.finish_stream.set()
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
    _name, port, _upstream = running_nginx

    exact_status, _ = _post(port, b"x" * (16 * 1024 * 1024), "Bearer ingress-marker")
    over_status, _ = _post(
        port, b"x" * (16 * 1024 * 1024 + 1), "Bearer ingress-marker"
    )

    assert exact_status != 413
    assert over_status == 413


def test_streaming_arrives_before_upstream_completion_and_access_log_is_off(
    running_nginx, docker_command
):
    name, port, upstream = running_nginx
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", "/stream")
        response = connection.getresponse()
        first = response.read(len(b"first\n"))
        assert response.status == 200
        assert upstream.first_chunk_sent.is_set()
        assert not upstream.finish_stream.is_set()
        assert first == b"first\n"
        upstream.finish_stream.set()
        assert response.read() == b"second\n"
    finally:
        upstream.finish_stream.set()
        connection.close()

    logs = subprocess.run(
        [*docker_command, "logs", name],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert "ingress-marker" not in logs.stdout + logs.stderr


def test_production_nginx_source_disables_buffering_and_access_logs():
    source = (Path(__file__).parents[2] / "deploy/nginx.conf.example").read_text(
        encoding="utf-8"
    )
    assert "access_log off;" in source
    assert "proxy_buffering off;" in source
    assert "proxy_request_buffering off;" in source
    assert "proxy_set_header Authorization $http_authorization;" in source
    assert "client_max_body_size 16m;" in source
