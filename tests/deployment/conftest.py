from __future__ import annotations

import base64
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import ipaddress
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


def derive_nginx_config(source, *, listen, upstream):
    substitutions = (
        ("listen 192.168.68.123:8766;", f"listen {listen};"),
        ("proxy_pass http://127.0.0.1:8765;", f"proxy_pass http://{upstream};"),
    )
    derived = source
    for original, replacement in substitutions:
        if derived.count(original) != 1:
            raise AssertionError(
                f"production nginx config must contain exactly one {original!r}"
            )
        derived = derived.replace(original, replacement)
    return derived


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
    project = f"iwikiacceptance{os.getpid()}{uuid.uuid4().hex[:8]}"
    tag = f"{project}-iwiki:latest"
    try:
        try:
            run_checked(
                [
                    *compose_command,
                    "--project-name",
                    project,
                    "--env-file",
                    "tests/deployment/fixtures/render.env",
                    "build",
                    "iwiki",
                ],
                timeout=300,
            )
        except subprocess.CalledProcessError as exc:
            pytest.fail(
                f"acceptance image build failed with exit code {exc.returncode}"
            )
        yield tag
    finally:
        def remove_compose_resources():
            subprocess.run(
                [
                    *compose_command,
                    "--project-name",
                    project,
                    "--env-file",
                    "tests/deployment/fixtures/render.env",
                    "down",
                    "--rmi",
                    "all",
                    "--remove-orphans",
                ],
                cwd=REPOSITORY,
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
            )

        def remove_image_reference():
            inspected = subprocess.run(
                [*docker_command, "image", "inspect", tag],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            if inspected.returncode == 0:
                subprocess.run(
                    [*docker_command, "image", "rm", "-f", tag],
                    cwd=REPOSITORY,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                )

        run_cleanup_steps(
            (
                ("compose resources", remove_compose_resources),
                ("image reference", remove_image_reference),
            )
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


def _postgres_endpoint_identity(values):
    host = values.get("host", "")
    normalized_host = host.strip("[]").rstrip(".").lower()
    try:
        loopback = ipaddress.ip_address(normalized_host).is_loopback
    except ValueError:
        loopback = normalized_host == "localhost"
    return ("loopback" if loopback else normalized_host, int(values.get("port", 5432)))


def require_postgres_topology(env_name, values):
    host = values.get("host", "")
    if not host or host.startswith("/"):
        pytest.fail(f"{env_name} has no container-reachable TCP host")
    normalized_host, port = _postgres_endpoint_identity(values)
    loopback = normalized_host == "loopback"
    if env_name == "IWIKI_TEST_POSTGRES_LOOPBACK_DSN":
        if not loopback:
            pytest.fail(f"{env_name} must use a loopback host")
        if port == 5432:
            pytest.fail(f"{env_name} must use a custom port")
    elif loopback:
        pytest.fail(f"{env_name} must use a non-loopback host")


def require_distinct_postgres_endpoints(generic, loopback):
    if _postgres_endpoint_identity(generic) == _postgres_endpoint_identity(loopback):
        pytest.fail(
            "IWIKI_TEST_POSTGRES_DSN and IWIKI_TEST_POSTGRES_LOOPBACK_DSN "
            "must resolve to different endpoints"
        )


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
    peer_name = (
        "IWIKI_TEST_POSTGRES_LOOPBACK_DSN"
        if env_name == "IWIKI_TEST_POSTGRES_DSN"
        else "IWIKI_TEST_POSTGRES_DSN"
    )
    peer_raw = os.environ.get(peer_name, "").strip()
    if peer_raw:
        try:
            peer_values = conninfo_to_dict(validated_test_dsn(peer_raw))
        except (ValueError, psycopg.ProgrammingError) as exc:
            pytest.fail(
                f"{peer_name} is not a disposable test DSN: {type(exc).__name__}"
            )
        generic, loopback = (
            (values, peer_values)
            if env_name == "IWIKI_TEST_POSTGRES_DSN"
            else (peer_values, values)
        )
        require_distinct_postgres_endpoints(generic, loopback)
    require_postgres_topology(env_name, values)
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


def _generate_ogg_voice(image, docker):
    generated = subprocess.run(
        [
            *docker,
            "run",
            "--rm",
            "--entrypoint",
            "/usr/bin/ffmpeg",
            image,
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=16000:cl=mono",
            "-t",
            "0.1",
            "-c:a",
            "libopus",
            "-f",
            "ogg",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
        timeout=30,
    )
    if not generated.stdout.startswith(b"OggS"):
        raise RuntimeError("ffmpeg did not produce OGG/Opus acceptance audio")
    return generated.stdout


def run_cleanup_steps(steps):
    failures = []
    for name, cleanup in steps:
        try:
            cleanup()
        except Exception as exc:
            failures.append(str(exc) or f"{name} cleanup failed")
    if failures:
        raise RuntimeError("; ".join(failures))


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
            audio=_generate_ogg_voice(self.image, self.docker),
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
        nginx_source = (REPOSITORY / "deploy/nginx.conf.example").read_text(
            encoding="utf-8"
        )
        self.nginx_config.write_text(
            derive_nginx_config(
                nginx_source,
                listen="127.0.0.1:8766",
                upstream="127.0.0.1:8765",
            ),
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
                "--add-host",
                "api.telegram.org:127.0.0.2",
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

    def telegram_api_addresses(self):
        result = run_checked(
            [
                *self.docker,
                "exec",
                self.name,
                "/app/.venv/bin/python",
                "-c",
                (
                    "import json,socket; "
                    "print(json.dumps(sorted({row[4][0] for row in "
                    "socket.getaddrinfo('api.telegram.org', 443)})))"
                ),
            ],
            timeout=10,
        )
        return set(json.loads(result.stdout))

    @staticmethod
    def _process_start_time(pid):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        return int(stat.rpartition(")")[2].split()[19])

    def host_child_identities(self):
        children = self.supervisor_status()
        result = run_checked(
            [*self.docker, "top", self.name, "-eo", "pid,pgid,args"],
            timeout=10,
        )
        rows = []
        for line in result.stdout.splitlines()[1:]:
            fields = line.split(maxsplit=2)
            if len(fields) < 2:
                continue
            host_pid, host_pgid = map(int, fields[:2])
            status = Path(f"/proc/{host_pid}/status").read_text(encoding="utf-8")
            nspid_line = next(
                item for item in status.splitlines() if item.startswith("NSpid:")
            )
            namespace_pid = int(nspid_line.split()[-1])
            rows.append((host_pid, host_pgid, namespace_pid))
        identities = {}
        for child, status in children.items():
            namespace_pid = status["pid"]
            host_pid, host_pgid, _namespace_pid = next(
                row for row in rows if row[2] == namespace_pid
            )
            members = tuple(
                (pid, self._process_start_time(pid))
                for pid, pgid, _nested_pid in rows
                if pgid == host_pgid
            )
            identities[child] = {
                "pid": host_pid,
                "pid_start": self._process_start_time(host_pid),
                "pgid": host_pgid,
                "group_members": members,
            }
        return identities

    def container_exit_code(self):
        return int(
            run_checked(
                [
                    *self.docker,
                    "inspect",
                    self.name,
                    "--format",
                    "{{.State.ExitCode}}",
                ],
                timeout=10,
            ).stdout.strip()
        )

    def captured_identities_gone(self, identities):
        captured = {
            member
            for identity in identities.values()
            for member in identity["group_members"]
        }
        captured.update(
            (identity["pid"], identity["pid_start"])
            for identity in identities.values()
        )
        return all(
            self._process_start_time(pid) != start_time
            for pid, start_time in captured
        )

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

    def stable_log_count(self, message):
        result = subprocess.run(
            [*self.docker, "logs", self.name],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        return (result.stdout + result.stderr).count(message)

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

    def private_marker_snapshot(self):
        logs = subprocess.run(
            [*self.docker, "logs", self.name],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        mounts = run_checked(
            [
                *self.docker,
                "inspect",
                self.name,
                "--format",
                "{{json .Mounts}}",
            ],
            timeout=10,
        ).stdout
        mounted_paths = (
            "/etc/iwiki/server.toml",
            "/etc/nginx/nginx.conf",
            "/etc/ssl/certs/ca-certificates.crt",
            self.httpx_ca_path,
        )
        runtime_scan = run_checked(
            [
                *self.docker,
                "exec",
                self.name,
                "/app/.venv/bin/python",
                "-c",
                (
                    "import pathlib,sys; "
                    "markers=[bytes.fromhex(x) for x in sys.argv[1].split(',')]; "
                    "mounted=[pathlib.Path(x) for x in sys.argv[2:]]; "
                    "paths=[p for root in ('/run','/tmp') "
                    "for p in pathlib.Path(root).rglob('*') if p.is_file()]+mounted; "
                    "print('\\n'.join(str(p) for p in paths "
                    "if any(m in p.read_bytes() for m in markers)))"
                ),
                ",".join(value.encode().hex() for value in self.markers.values()),
                *mounted_paths,
            ],
            timeout=10,
        )
        mounted_files = {}
        for path in mounted_paths:
            mounted_files[path] = subprocess.run(
                [*self.docker, "exec", self.name, "/bin/cat", path],
                capture_output=True,
                check=True,
                timeout=10,
            ).stdout
        history = run_checked(
            [*self.docker, "history", "--no-trunc", self.image],
            timeout=20,
        ).stdout
        return {
            "logs": logs.stdout + logs.stderr,
            "mounts": mounts,
            "runtime_bad_paths": [
                line for line in runtime_scan.stdout.splitlines() if line
            ],
            "mounted_files": mounted_files,
            "history": history,
        }

    def stop(self):
        def stop_telegram():
            if not self.telegram_stopped:
                self.telegram.stop()
                self.telegram_stopped = True

        def stop_inference():
            if self.started:
                self.inference.stop()
                self.started = False

        def remove_container():
            removed = subprocess.run(
                [*self.docker, "rm", "-f", self.name],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if removed.returncode and "No such container" not in removed.stderr:
                raise RuntimeError(
                    f"container cleanup failed: {removed.stderr.strip()}"
                )

        steps = [
            ("telegram", stop_telegram),
            ("inference", stop_inference),
            ("container", remove_container),
        ]
        steps.extend(
            (
                f"file {path.name}",
                lambda path=path: path.unlink(missing_ok=True),
            )
            for path in tuple(self.directory.iterdir())
            if path.is_file()
        )
        run_cleanup_steps(steps)


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
