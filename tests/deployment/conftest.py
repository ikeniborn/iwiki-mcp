from __future__ import annotations

import base64
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import ssl
import subprocess
import tarfile
import threading
import time
import uuid

import pytest


REPOSITORY = Path(__file__).parents[2]


def _free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def run_checked(args, *, timeout=120, **kwargs):
    return subprocess.run(
        args,
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
        **kwargs,
    )


@pytest.fixture(scope="session")
def compose_command():
    candidates = (["docker", "compose"], ["docker-compose"])
    failures = []
    for candidate in candidates:
        if shutil.which(candidate[0]) is None:
            failures.append(f"{candidate[0]} missing")
            continue
        try:
            result = run_checked([*candidate, "version"], timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            failures.append(f"{' '.join(candidate)}: {type(exc).__name__}")
            continue
        if result.returncode == 0:
            return candidate
    pytest.skip("functional Docker Compose unavailable: " + "; ".join(failures))


@pytest.fixture(scope="session")
def rendered_compose(compose_command):
    try:
        result = run_checked(
            [
                *compose_command,
                "--env-file",
                "tests/deployment/fixtures/render.env",
                "config",
                "--format",
                "json",
            ],
            timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"Compose rendering failed with exit code {exc.returncode}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        pytest.fail("Compose command did not return valid JSON")


@pytest.fixture(scope="session")
def docker_command():
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI unavailable")
    try:
        run_checked(["docker", "info"], timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"Docker daemon unavailable: {type(exc).__name__}")
    return ["docker"]


@pytest.fixture(scope="session")
def acceptance_image(docker_command, compose_command):
    tag = f"iwiki-mcp:acceptance-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        run_checked(
            [
                *compose_command,
                "--env-file",
                "tests/deployment/fixtures/render.env",
                "build",
                "iwiki",
            ],
            timeout=300,
        )
        run_checked(
            [*docker_command, "tag", "iwiki-mcp-iwiki:latest", tag],
            timeout=20,
        )
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"acceptance image build failed with exit code {exc.returncode}")
    yield tag
    subprocess.run(
        [*docker_command, "image", "rm", "-f", tag],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


# Reuse the repository's disposable PostgreSQL fixtures without adding a
# database service to the application Compose project.
from tests.postgres.conftest import (  # noqa: E402,F401
    clean_postgres,
    hosted_runtime,
    postgres_dsn,
    store_factory,
)
from tests.telegram_bot.test_https_proxy_integration import (  # noqa: E402
    _certificate_authority,
    _HttpsConnectProxy,
    _issue_server_certificate,
)


class _InferenceHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        pass

    def _send(self, status, payload):
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.server.requests.append(
            {
                "method": "GET",
                "path": self.path,
                "body": b"",
                "authorization": self.headers.get("Authorization"),
            }
        )
        if self.path == "/v1/models":
            self._send(200, {"data": [{"id": self.server.chat_model}]})
        else:
            self._send(404, {"error": "not_found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.requests.append(
            {
                "method": "POST",
                "path": self.path,
                "body": body,
                "authorization": self.headers.get("Authorization"),
            }
        )
        if self.path in self.server.fail_paths:
            self._send(500, {"error": self.server.provider_error})
        elif self.path == "/v1/embeddings":
            payload = json.loads(body)
            values = payload.get("input", [])
            self._send(
                200,
                {
                    "data": [
                        {"index": index, "embedding": [1.0, 0.0, 0.0]}
                        for index, _value in enumerate(values)
                    ]
                },
            )
        elif self.path == "/v1/chat/completions":
            content = (
                self.server.preview
                if b"Produce Markdown" in body
                else self.server.reply
            )
            self._send(
                200,
                {"choices": [{"message": {"content": content}}]},
            )
        elif self.path == "/v1/audio/transcriptions":
            self._send(200, {"text": self.server.transcription})
        else:
            self._send(404, {"error": "not_found"})


class FakeInferenceServer:
    def __init__(self, markers, certificate, hostname):
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", _free_port()), _InferenceHandler
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(*map(str, certificate))
        self.server.socket = context.wrap_socket(
            self.server.socket, server_side=True
        )
        self.hostname = hostname
        self.server.chat_model = "acceptance-chat"
        self.server.reply = markers["reply"]
        self.server.preview = f"## Body\n{markers['preview']}"
        self.server.transcription = markers["transcription"]
        self.server.provider_error = markers["provider_error"]
        self.server.requests = []
        self.server.fail_paths = set()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self):
        return f"https://{self.hostname}:{self.server.server_port}/v1"

    @property
    def requests(self):
        return self.server.requests

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()


def _write_combined_ca_bundle(image, docker, ca_cert, destination):
    system_ca = subprocess.run(
        [
            *docker,
            "run",
            "--rm",
            "--entrypoint",
            "/bin/cat",
            image,
            "/etc/ssl/certs/ca-certificates.crt",
        ],
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout
    destination.write_bytes(system_ca.rstrip() + b"\n" + ca_cert.read_bytes())
    destination.chmod(0o644)


def _image_httpx_ca_path(image, docker):
    return subprocess.run(
        [
            *docker,
            "run",
            "--rm",
            "--entrypoint",
            "/app/.venv/bin/python",
            image,
            "-c",
            "import certifi; print(certifi.where())",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()


def build_disposable_privacy_proof(directory, docker, compose, marker):
    tag = f"iwiki-privacy-proof:{uuid.uuid4().hex[:12]}"
    container = f"iwiki-privacy-proof-{uuid.uuid4().hex[:12]}"
    (directory / ".dockerignore").write_text(
        (REPOSITORY / ".dockerignore").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (directory / "Dockerfile").write_text(
        "FROM scratch\nCOPY . /proof/\nCMD [\"/proof/public.txt\"]\n",
        encoding="utf-8",
    )
    (directory / "public.txt").write_text(
        "public acceptance artifact\n", encoding="utf-8"
    )
    (directory / "compose.yaml").write_text(
        "services:\n"
        "  proof:\n"
        "    build:\n"
        "      context: .\n"
        f"    image: {tag}\n",
        encoding="utf-8",
    )
    proof = {}
    try:
        build = subprocess.run(
            [
                *compose,
                "--project-name",
                f"iwikiprivacy{uuid.uuid4().hex[:8]}",
                "-f",
                str(directory / "compose.yaml"),
                "build",
                "--no-cache",
                "proof",
            ],
            cwd=directory,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        subprocess.run(
            [*docker, "create", "--name", container, tag],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        exported = subprocess.run(
            [*docker, "export", container],
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout
        filesystem = bytearray()
        public_file = None
        paths = set()
        with tarfile.open(fileobj=io.BytesIO(exported), mode="r:") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                paths.add(Path(member.name).name)
                extracted = archive.extractfile(member)
                assert extracted is not None
                content = extracted.read()
                filesystem.extend(content)
                if member.name.rstrip("/") == "proof/public.txt":
                    public_file = content
        history = subprocess.run(
            [*docker, "history", "--no-trunc", tag],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        mounts = json.loads(
            subprocess.run(
                [
                    *docker,
                    "inspect",
                    container,
                    "--format",
                    "{{json .Mounts}}",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            ).stdout
        )
        proof.update(
            {
                "public_file": public_file,
                "filesystem_paths": paths,
                "filesystem_bytes": bytes(filesystem),
                "build_output": build.stdout + build.stderr,
                "history": history.stdout + history.stderr,
                "mounts": mounts,
            }
        )
        assert marker in "".join(
            path.read_text(encoding="utf-8")
            for path in directory.iterdir()
            if path.name in {
                ".env",
                "runtime.env",
                "server.toml",
                "nginx.conf",
                "acceptance.key",
                "acceptance.pem",
            }
        )
    finally:
        subprocess.run(
            [*docker, "rm", "-f", container],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        subprocess.run(
            [*docker, "image", "rm", "-f", tag],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        for path in directory.iterdir():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        proof["context_cleaned"] = not any(directory.iterdir())
    proof["container_removed"] = subprocess.run(
        [*docker, "inspect", container],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    ).returncode != 0
    proof["image_removed"] = subprocess.run(
        [*docker, "image", "inspect", tag],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    ).returncode != 0
    return proof


@dataclass
class DisposablePostgresEndpoint:
    env_name: str
    dsn: str
    values: dict
    roles: list[str]

    def create_role(self, prefix):
        import psycopg
        from psycopg import sql

        role = f"iwiki_acceptance_{prefix}_{secrets.token_hex(5)}"
        password = secrets.token_urlsafe(24)
        try:
            with psycopg.connect(self.dsn, autocommit=True) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        sql.SQL("CREATE ROLE {} LOGIN NOBYPASSRLS PASSWORD {}").format(
                            sql.Identifier(role), sql.Literal(password)
                        )
                    )
        except psycopg.errors.InsufficientPrivilege:
            pytest.skip(
                f"{self.env_name} cannot create isolated runtime principals"
            )
        self.roles.append(role)
        return role, password

    def drop_roles(self):
        import psycopg
        from psycopg import sql

        with psycopg.connect(self.dsn, autocommit=True) as connection:
            with connection.cursor() as cursor:
                for role in reversed(self.roles):
                    cursor.execute(
                        sql.SQL("DROP OWNED BY {} CASCADE").format(
                            sql.Identifier(role)
                        )
                    )
                    cursor.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        self.roles.clear()


def postgres_topology_skip_reason(env_name, values):
    host = values.get("host", "")
    if not host or host.startswith("/"):
        return f"{env_name} has no container-reachable TCP host"
    port = int(values.get("port", 5432))
    loopback = host in {"localhost", "::1"} or host.startswith("127.")
    if env_name == "IWIKI_TEST_POSTGRES_LOOPBACK_DSN":
        if not loopback:
            return f"{env_name} is not loopback-hosted"
        if port == 5432:
            return f"{env_name} does not use a custom port"
    elif loopback and port == 5432:
        return f"{env_name} must use a non-loopback host or a custom port"
    return None


@pytest.fixture(
    params=("IWIKI_TEST_POSTGRES_DSN", "IWIKI_TEST_POSTGRES_LOOPBACK_DSN")
)
def postgres_endpoint(request):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    from tests.postgres.conftest import validated_test_dsn

    env_name = request.param
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        pytest.skip(f"{env_name} is not set")
    try:
        dsn = validated_test_dsn(raw)
        values = conninfo_to_dict(dsn)
    except (ValueError, psycopg.ProgrammingError) as exc:
        pytest.fail(f"{env_name} is not a disposable test DSN: {type(exc).__name__}")
    topology_reason = postgres_topology_skip_reason(env_name, values)
    if topology_reason:
        pytest.skip(topology_reason)
    try:
        with psycopg.connect(dsn, connect_timeout=10, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_extension "
                    "WHERE extname = 'vector')"
                )
                if not cursor.fetchone()[0]:
                    pytest.skip(f"{env_name} does not provide pgvector")
                cursor.execute("DROP SCHEMA IF EXISTS iwiki CASCADE")
                cursor.execute("DROP SCHEMA IF EXISTS iwiki_test_probe CASCADE")
    except psycopg.Error as exc:
        pytest.fail(f"{env_name} is unavailable: {type(exc).__name__}")
    endpoint = DisposablePostgresEndpoint(env_name, dsn, values, [])
    yield endpoint
    endpoint.drop_roles()
    with psycopg.connect(dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS iwiki CASCADE")
            cursor.execute("DROP SCHEMA IF EXISTS iwiki_test_probe CASCADE")


def _wait_until(predicate, *, timeout, interval=0.1):
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return value


class FullStackHarness:
    def __init__(self, endpoint, image, docker, directory):
        from psycopg.conninfo import make_conninfo

        from iwiki_mcp.postgres.auth import AuthStore
        from iwiki_mcp.postgres.migrations import MigrationSettings, run_migrations
        from iwiki_mcp.postgres.store import provision_runtime_grant

        self.endpoint = endpoint
        self.image = image
        self.docker = docker
        self.directory = directory
        self.name = f"iwiki-full-acceptance-{uuid.uuid4().hex[:10]}"
        self.markers = {
            name: f"{name}-{secrets.token_urlsafe(18)}"
            for name in (
                "telegram_token",
                "iwiki_token",
                "llm_key",
                "proxy_user",
                "proxy_password",
                "update",
                "reply",
                "filename",
                "audio",
                "transcription",
                "preview",
                "provider_error",
            )
        }
        self.markers["proxy_origin"] = (
            f"proxy-{secrets.token_hex(10)}.acceptance.invalid"
        )
        run_migrations(
            MigrationSettings(
                dsn=endpoint.dsn,
                embed_model="acceptance-embedding",
                embed_dimensions=3,
                statement_timeout_ms=30_000,
                lock_timeout_ms=5_000,
            )
        )
        auth = AuthStore(endpoint.dsn)
        auth.create_wiki("wiki-a", "wiki-a")
        auth.create_domain("wiki-a", "docs")
        self.markers["iwiki_token"] = auth.create_token(
            "wiki-a",
            "acceptance",
            read_domains=["docs"],
            write_domains=["docs"],
        )["token"]
        role, password = endpoint.create_role("hosted")
        provision_runtime_grant(
            endpoint.dsn,
            principal=role,
            iwiki_id="wiki-a",
            read_domains=["docs"],
            write_domains=["docs"],
            runtime="hosted",
        )
        self.markers["database_password"] = password
        runtime_values = {
            **endpoint.values,
            "user": role,
            "password": password,
        }
        self.runtime_dsn = make_conninfo(**runtime_values)
        self.inference_hostname = "inference.acceptance.invalid"
        self.ca_cert, proxy_cert, telegram_cert = _certificate_authority(
            directory,
            proxy_subject=self.markers["proxy_origin"],
            proxy_san=f"DNS:{self.markers['proxy_origin']}",
        )
        inference_certificate = _issue_server_certificate(
            directory,
            self.ca_cert,
            directory / "ca.key",
            "inference",
            self.inference_hostname,
            f"DNS:{self.inference_hostname}",
        )
        self.inference = FakeInferenceServer(
            self.markers, inference_certificate, self.inference_hostname
        )
        self.combined_ca = directory / "ca-certificates.crt"
        _write_combined_ca_bundle(
            self.image,
            self.docker,
            self.ca_cert,
            self.combined_ca,
        )
        self.httpx_ca_path = _image_httpx_ca_path(self.image, self.docker)
        proxy_credentials = (
            f"{self.markers['proxy_user']}:{self.markers['proxy_password']}"
        )
        proxy_authorization = "Basic " + base64.b64encode(
            proxy_credentials.encode()
        ).decode()
        self.telegram = _HttpsConnectProxy(
            proxy_cert,
            telegram_cert,
            token=self.markers["telegram_token"],
            updates=[],
            audio=self.markers["audio"].encode(),
            voice_path=f"voice/{self.markers['filename']}.ogg",
            proxy_authorization=proxy_authorization,
            poll_delay=0.05,
        )
        self.proxy_url = (
            "https://"
            f"{self.markers['proxy_user']}:{self.markers['proxy_password']}"
            f"@{self.markers['proxy_origin']}:{self.telegram.port}"
        )
        self.server_config = directory / "server.toml"
        self.nginx_config = directory / "nginx.conf"
        self.runtime_env = directory / "runtime.env"
        self._write_files(runtime_values)
        self.started = False
        self.telegram_stopped = False
        self.next_update_id = 100

    def _write_files(self, values):
        self.server_config.write_text(
            "[storage]\n"
            'type = "postgres"\n'
            f"host = {json.dumps(values['host'])}\n"
            f"port = {int(values.get('port', 5432))}\n"
            f"database = {json.dumps(values['dbname'])}\n"
            f"user = {json.dumps(values['user'])}\n"
            f"sslmode = {json.dumps(values.get('sslmode', 'prefer'))}\n"
            "\n[server]\n"
            'host = "127.0.0.1"\n'
            "port = 8765\n"
            'allowed_origins = ["https://acceptance.invalid"]\n'
            "pool_min_size = 1\n"
            "pool_max_size = 2\n"
            "statement_timeout_ms = 30000\n"
            "lock_timeout_ms = 5000\n",
            encoding="utf-8",
        )
        self.nginx_config.write_text(
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
        listen 127.0.0.1:8766;
        location / {
            proxy_pass http://127.0.0.1:8765;
            proxy_http_version 1.1;
            proxy_set_header Authorization $http_authorization;
            proxy_set_header Host $host;
            proxy_buffering off;
            proxy_request_buffering off;
            proxy_read_timeout 30s;
        }
    }
}
""",
            encoding="utf-8",
        )
        values = {
            "IWIKI_DB_PASSWORD": self.markers["database_password"],
            "IWIKI_LLM_BASE_URL": self.inference.base_url,
            "IWIKI_LLM_KEY": self.markers["llm_key"],
            "IWIKI_EMBED_MODEL": "acceptance-embedding",
            "IWIKI_EMBED_DIMENSIONS": "3",
            "IWIKI_RERANK_MODEL": "",
            "IWIKI_INGRESS_HEALTH_HOST": "127.0.0.1",
            "IWIKI_INGRESS_HEALTH_PORT": "8766",
            "IWIKI_BOT_HEARTBEAT_MAX_AGE_SECONDS": "2",
            "IWIKI_BOT_TELEGRAM_TOKEN": self.markers["telegram_token"],
            "IWIKI_BOT_IWIKI_URL": "http://127.0.0.1:8765/mcp",
            "IWIKI_BOT_IWIKI_TOKEN": self.markers["iwiki_token"],
            "IWIKI_BOT_ALLOWED_TELEGRAM_IDS": "1001",
            "IWIKI_BOT_LLM_BASE_URL": self.inference.base_url,
            "IWIKI_BOT_LLM_KEY": self.markers["llm_key"],
            "IWIKI_BOT_LLM_MODEL": "acceptance-chat",
            "IWIKI_BOT_TRANSCRIPTION_MODEL": "acceptance-transcription",
            "IWIKI_BOT_CONFIRMATION_TTL_SECONDS": "30",
            "IWIKI_BOT_TELEGRAM_PROXY_URL": self.proxy_url,
        }
        self.runtime_env.write_text(
            "".join(f"{name}={value}\n" for name, value in values.items()),
            encoding="utf-8",
        )
        self.runtime_env.chmod(0o600)

    def start(self):
        for port in (8765, 8766):
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError:
                    pytest.skip(f"required full-stack loopback port {port} is unavailable")
        self.inference.start()
        self.started = True
        subprocess.run(
            [
                *self.docker,
                "run",
                "-d",
                "--name",
                self.name,
                "--network",
                "host",
                "--add-host",
                f"{self.markers['proxy_origin']}:127.0.0.1",
                "--add-host",
                f"{self.inference_hostname}:127.0.0.1",
                "--restart",
                "unless-stopped",
                "--read-only",
                "--user",
                "10001:10001",
                "--tmpfs",
                "/run:uid=10001,gid=10001,mode=0750",
                "--tmpfs",
                "/tmp:uid=10001,gid=10001,mode=1770",
                "--security-opt",
                "no-new-privileges:true",
                "--cap-drop",
                "ALL",
                "--env-file",
                str(self.runtime_env),
                "-e",
                "IWIKI_SERVER_CONFIG=/etc/iwiki/server.toml",
                "-v",
                f"{self.server_config}:/etc/iwiki/server.toml:ro",
                "-v",
                f"{self.nginx_config}:/etc/nginx/nginx.conf:ro",
                "-v",
                f"{self.combined_ca}:/etc/ssl/certs/ca-certificates.crt:ro",
                "-v",
                f"{self.combined_ca}:{self.httpx_ca_path}:ro",
                "--health-cmd",
                "/app/.venv/bin/python /app/deploy/healthcheck.py",
                "--health-interval",
                "1s",
                "--health-timeout",
                "5s",
                "--health-retries",
                "3",
                "--health-start-period",
                "1s",
                self.image,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert _wait_until(
            lambda: self.health_status() == "healthy", timeout=45
        ), self.safe_diagnostics()
        return self

    def supervisor_status(self):
        result = subprocess.run(
            [
                *self.docker,
                "exec",
                self.name,
                "supervisorctl",
                "-c",
                "/etc/supervisor/supervisord.conf",
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
            if len(fields) >= 4:
                rows[fields[0]] = {
                    "state": fields[1],
                    "pid": int(fields[3].rstrip(",")) if fields[1] == "RUNNING" else None,
                }
        return rows

    def health_status(self):
        result = subprocess.run(
            [
                *self.docker,
                "inspect",
                self.name,
                "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{end}}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        return result.stdout.strip()

    def health_probe(self):
        return subprocess.run(
            [
                *self.docker,
                "exec",
                self.name,
                "/app/.venv/bin/python",
                "/app/deploy/healthcheck.py",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    def container_id(self):
        return run_checked(
            [*self.docker, "inspect", self.name, "--format", "{{.Id}}"],
            timeout=10,
        ).stdout.strip()

    def enqueue_message(self, *, text=None, voice=False):
        message = {
            "from": {"id": 1001},
            "chat": {"id": 9},
        }
        if text is not None:
            message["text"] = text
        if voice:
            message["voice"] = {"file_id": "acceptance-voice"}
        update = {"update_id": self.next_update_id, "message": message}
        self.next_update_id += 1
        self.telegram.enqueue_updates(update)
        return update

    def enqueue_callback(self, data):
        update = {
            "update_id": self.next_update_id,
            "callback_query": {
                "id": f"callback-{self.next_update_id}",
                "from": {"id": 1001},
                "message": {"chat": {"id": 9}},
                "data": data,
            },
        }
        self.next_update_id += 1
        self.telegram.enqueue_updates(update)
        return update

    def wait_for_sent(self, start, predicate=lambda _payload: True, timeout=15):
        def observed():
            for payload in self.telegram.sent_payloads[start:]:
                if predicate(payload):
                    return payload
            return None

        return _wait_until(observed, timeout=timeout)

    def safe_diagnostics(self):
        logs = subprocess.run(
            [*self.docker, "logs", self.name],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        sanitized = logs.stdout + logs.stderr
        for marker in self.markers.values():
            sanitized = sanitized.replace(marker, "<redacted>")
        return {
            "health": self.health_status(),
            "children": self.supervisor_status(),
            "logs": sanitized[-2000:],
        }

    def stop(self):
        if not self.telegram_stopped:
            self.telegram.stop()
            self.telegram_stopped = True
        if self.started:
            self.inference.stop()
            self.started = False
        subprocess.run(
            [*self.docker, "rm", "-f", self.name],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        for path in self.directory.iterdir():
            if path.is_file():
                path.unlink()


@pytest.fixture
def full_stack(request, postgres_endpoint, tmp_path, docker_command):
    image = request.getfixturevalue("acceptance_image")
    harness = FullStackHarness(postgres_endpoint, image, docker_command, tmp_path)
    try:
        yield harness.start()
    finally:
        harness.stop()


@pytest.fixture
def hosted_startup_probe(
    request, postgres_endpoint, tmp_path, docker_command
):
    image = request.getfixturevalue("acceptance_image")
    markers = {
        name: f"{name}-{secrets.token_urlsafe(18)}"
        for name in ("reply", "preview", "transcription", "provider_error")
    }
    inference_hostname = "inference-startup.acceptance.invalid"
    ca_cert, _proxy_certificate, _telegram_certificate = (
        _certificate_authority(tmp_path)
    )
    inference_certificate = _issue_server_certificate(
        tmp_path,
        ca_cert,
        tmp_path / "ca.key",
        "inference",
        inference_hostname,
        f"DNS:{inference_hostname}",
    )
    combined_ca = tmp_path / "ca-certificates.crt"
    _write_combined_ca_bundle(image, docker_command, ca_cert, combined_ca)
    httpx_ca_path = _image_httpx_ca_path(image, docker_command)
    inference = FakeInferenceServer(
        markers, inference_certificate, inference_hostname
    )
    inference.start()
    created = []

    def probe(role, password):
        values = {**postgres_endpoint.values, "user": role}
        directory = tmp_path / f"startup-{len(created)}"
        directory.mkdir()
        config = directory / "server.toml"
        config.write_text(
            "[storage]\n"
            'type = "postgres"\n'
            f"host = {json.dumps(values['host'])}\n"
            f"port = {int(values.get('port', 5432))}\n"
            f"database = {json.dumps(values['dbname'])}\n"
            f"user = {json.dumps(values['user'])}\n"
            f"sslmode = {json.dumps(values.get('sslmode', 'prefer'))}\n"
            "\n[server]\n"
            'host = "127.0.0.1"\n'
            "port = 8765\n"
            'allowed_origins = ["https://acceptance.invalid"]\n'
            "pool_min_size = 1\n"
            "pool_max_size = 2\n"
            "statement_timeout_ms = 30000\n"
            "lock_timeout_ms = 5000\n",
            encoding="utf-8",
        )
        name = f"iwiki-startup-acceptance-{uuid.uuid4().hex[:10]}"
        created.append(name)
        subprocess.run(
            [
                *docker_command,
                "run",
                "-d",
                "--name",
                name,
                "--network",
                "host",
                "--add-host",
                f"{inference_hostname}:127.0.0.1",
                "--read-only",
                "--user",
                "10001:10001",
                "--tmpfs",
                "/run:uid=10001,gid=10001,mode=0750",
                "--tmpfs",
                "/tmp:uid=10001,gid=10001,mode=1770",
                "--security-opt",
                "no-new-privileges:true",
                "--cap-drop",
                "ALL",
                "-e",
                f"IWIKI_DB_PASSWORD={password}",
                "-e",
                f"IWIKI_LLM_BASE_URL={inference.base_url}",
                "-e",
                "IWIKI_LLM_KEY=startup-probe-key",
                "-e",
                "IWIKI_EMBED_MODEL=acceptance-embedding",
                "-e",
                "IWIKI_EMBED_DIMENSIONS=3",
                "-e",
                "IWIKI_RERANK_MODEL=",
                "-v",
                f"{config}:/etc/iwiki/server.toml:ro",
                "-v",
                f"{combined_ca}:/etc/ssl/certs/ca-certificates.crt:ro",
                "-v",
                f"{combined_ca}:{httpx_ca_path}:ro",
                "--entrypoint",
                "/app/.venv/bin/iwiki-mcp",
                image,
                "serve",
                "--config",
                "/etc/iwiki/server.toml",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )

        def stopped():
            result = subprocess.run(
                [
                    *docker_command,
                    "inspect",
                    name,
                    "--format",
                    "{{.State.Running}} {{.State.ExitCode}}",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            return result.stdout.strip() if result.stdout.startswith("false ") else None

        state = _wait_until(stopped, timeout=20)
        logs = subprocess.run(
            [*docker_command, "logs", name],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if not state:
            pytest.fail("hosted MCP startup probe did not terminate")
        return {
            "exit_code": int(state.split()[1]),
            "logs": logs.stdout + logs.stderr,
        }

    try:
        yield probe
    finally:
        for name in created:
            subprocess.run(
                [*docker_command, "rm", "-f", name],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        inference.stop()
        for path in tmp_path.iterdir():
            if path.is_file():
                path.unlink()
