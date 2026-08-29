# Single-container deployment

This is the supported production path for hosted iwiki MCP with Telegram access. One
hardened application container runs hosted MCP, nginx, and the Telegram bot. PostgreSQL
and the literal HTTPS CONNECT proxy are external, operator-managed services.

## Route and ownership boundaries

The container has three supervised children:

1. `iwiki-mcp serve --transport streamable-http` on `127.0.0.1:8765`.
2. nginx on the operator-selected LAN/Traefik listener, forwarding to loopback MCP.
3. `iwiki-telegram-bot`, using the external HTTPS proxy for Telegram only.

The bot establishes TLS to the proxy, sends `CONNECT api.telegram.org:443`, then
establishes Telegram TLS inside that tunnel. Long polling, replies, callbacks, file
metadata, and file bytes all use the proxy. There is no direct Telegram fallback.

Accepted proxy settings are exactly these shapes:

```text
IWIKI_BOT_TELEGRAM_PROXY_URL=https://proxy.example:8443
IWIKI_BOT_TELEGRAM_PROXY_URL=https://user:password@proxy.example:9443
```

The value must begin with literal lowercase `https://` and contain a valid host and
explicit valid port. `http://`, `socks*`, paths other than optional `/`, query strings,
fragments, missing or invalid hosts, and missing, non-numeric, or out-of-range ports are
rejected. Errors and logs never include the URL or credentials.

Inference, remote iwiki, and PostgreSQL remain direct. Compose defines no standard
proxy environment variables, inference and remote-iwiki HTTPX clients use
`trust_env=False`, and psycopg connects directly to PostgreSQL.

## Operator files and templates

Keep production runtime configuration outside the checkout:

```text
/opt/iwiki-mcp/server.toml       hosted MCP and external PostgreSQL endpoint
/opt/iwiki-mcp/nginx.conf        LAN/Traefik listener and loopback upstream
/opt/iwiki-mcp/runtime.env       owner-only runtime secrets and bot settings
```

For a new target directory, create every file with final ownership and mode before
editing. These commands initialize empty files; do not rerun them over populated files.
Back up existing files or install reviewed source copies with the same metadata.

```bash
sudo install -d -o root -g root -m 0755 /opt/iwiki-mcp
sudo install -o root -g root -m 0644 /dev/null /opt/iwiki-mcp/server.toml
sudo install -o root -g root -m 0644 deploy/nginx.conf.example /opt/iwiki-mcp/nginx.conf
sudo install -o root -g root -m 0600 /dev/null /opt/iwiki-mcp/runtime.env
```

Use this complete `server.toml` template, replacing every angle-bracket placeholder:

```toml
[storage]
type = "postgres"
host = "<postgres-host>"
port = <postgres-port>
database = "<postgres-database>"
user = "<least-privilege-runtime-role>"
sslmode = "verify-full"

[server]
host = "127.0.0.1"
port = 8765
allowed_origins = ["https://<allowed-origin-host>"]
pool_min_size = 2
pool_max_size = 10
statement_timeout_ms = 30000
lock_timeout_ms = 5000
```

A same-host PostgreSQL container must publish a host port such as
`127.0.0.1:55432`; configure that host and port rather than a bridge-only service name.
A remote database supplies its DNS name and custom port and should retain
`sslmode = "verify-full"` with a trusted CA and matching hostname.

Use this complete `runtime.env` template. Replace every placeholder before Compose
reads it; never commit the populated file.

```dotenv
IWIKI_DB_PASSWORD=<postgres-runtime-password>
IWIKI_LLM_BASE_URL=https://<server-inference-host>/v1
IWIKI_LLM_KEY=<server-inference-key>
IWIKI_EMBED_MODEL=<exact-embedding-model-id>
IWIKI_EMBED_DIMENSIONS=<exact-embedding-dimensions>
IWIKI_RERANK_MODEL=<exact-rerank-model-id-or-empty>

IWIKI_BOT_TELEGRAM_TOKEN=<telegram-bot-token>
IWIKI_BOT_IWIKI_URL=http://127.0.0.1:8765/mcp
IWIKI_BOT_IWIKI_TOKEN=<least-privilege-iwiki-token>
IWIKI_BOT_ALLOWED_TELEGRAM_IDS=<comma-separated-telegram-ids>
IWIKI_BOT_LLM_BASE_URL=https://<bot-inference-host>/v1
IWIKI_BOT_LLM_KEY=<bot-inference-only-key>
IWIKI_BOT_LLM_MODEL=<chat-model-id>
IWIKI_BOT_TRANSCRIPTION_MODEL=<transcription-model-id>
IWIKI_BOT_CONFIRMATION_TTL_SECONDS=<positive-seconds>
IWIKI_BOT_TELEGRAM_PROXY_URL=https://<proxy-host>:<proxy-port>

IWIKI_INGRESS_HEALTH_HOST=<lan-listener-host>
IWIKI_INGRESS_HEALTH_PORT=<lan-listener-port>
IWIKI_BOT_HEARTBEAT_MAX_AGE_SECONDS=<heartbeat-window-seconds>
```

Copy `deploy/nginx.conf.example` unchanged except for its host-specific `listen`
address. Keep loopback upstream `127.0.0.1:8765`, explicit `Authorization` forwarding,
disabled request/response buffering, `client_max_body_size 16m`, and `access_log off`.

```bash
sudoedit /opt/iwiki-mcp/server.toml
sudoedit /opt/iwiki-mcp/nginx.conf
sudoedit /opt/iwiki-mcp/runtime.env
sudo awk '/<[^>]+>/{found=1} END{exit found}' /opt/iwiki-mcp/server.toml /opt/iwiki-mcp/runtime.env
```

Expected metadata is `root:root 0755` for `/opt/iwiki-mcp`, `root:root 0644` for
`server.toml` and `nginx.conf`, and `root:root 0600` for `runtime.env`. Container UID
`10001` reads the two non-secret configuration files through read-only bind mounts.
Compose reads the owner-only environment file on the host; it is not mounted.

## Out-of-band schema migration and principal provisioning

Application Compose and runtime create no PostgreSQL service, database, or schema
objects and run no migrations. Runtime calls `require_schema_version`, validates its
least-privilege principal, and refuses startup on a mismatch.

Create a separate admin configuration by copying `server.toml`, then replace only
`storage.user` with the administration-only schema-owner/migrator role. Give a dedicated
operator group read access as `root:<admin-group> 0640`; the operator running the CLI
must already belong to that group. The file contains no password, is never mounted into
the application container, and must never name the runtime role.

```bash
ADMIN_GROUP='replace-with-admin-operator-group'
sudo install -o root -g "$ADMIN_GROUP" -m 0640 /opt/iwiki-mcp/server.toml /opt/iwiki-mcp/admin-server.toml
sudoedit /opt/iwiki-mcp/admin-server.toml
```

`base list` has a bounded read-only requested action, but the CLI intentionally
initializes or advances the schema before that read; there is no generic `migrate`
subcommand. Dry-run import/export and the schema compatibility command are separate
non-migrating paths. The password is read without echo, never enters argv/history, and
disappears when the subshell exits. Model identity must exactly match the intended
database-wide metadata.

```bash
(
set -e
read -r -s -p 'PostgreSQL schema-owner password: ' IWIKI_DB_PASSWORD
printf '\n'
export IWIKI_DB_PASSWORD
export IWIKI_EMBED_MODEL='replace-with-exact-embedding-model-id'
export IWIKI_EMBED_DIMENSIONS='replace-with-exact-embedding-dimensions'
export IWIKI_RERANK_MODEL='replace-with-exact-rerank-model-id-or-empty'
iwiki-mcp base list --config /opt/iwiki-mcp/admin-server.toml --json
)
```

With the same secret-safe boundary, create the base and domains only after the migration
trigger succeeds. See [PostgreSQL provisioning and least
privilege](../README.md#postgresql-provisioning-and-least-privilege) for background; do
not follow its later token step until this runbook has registered and inspected the exact
runtime principal.

```bash
(
set -e
read -r -s -p 'PostgreSQL schema-owner password: ' IWIKI_DB_PASSWORD
printf '\n'
export IWIKI_DB_PASSWORD
export IWIKI_EMBED_MODEL='replace-with-exact-embedding-model-id'
export IWIKI_EMBED_DIMENSIONS='replace-with-exact-embedding-dimensions'
export IWIKI_RERANK_MODEL='replace-with-exact-rerank-model-id-or-empty'
iwiki-mcp base create --config /opt/iwiki-mcp/admin-server.toml --iwiki replace-with-iwiki-id
iwiki-mcp domain create --config /opt/iwiki-mcp/admin-server.toml --iwiki replace-with-iwiki-id --domain replace-with-domain
)
```

Next, ensure the PostgreSQL runtime login named `replace-with-runtime-role` exists. This
is an out-of-band database/platform operation: iwiki does not create the login or accept
its password. Keep its password and runtime configuration separate from the schema-owner
configuration. The schema-owner configuration and credential are never mounted into the
application container.

Only after that login, base, and domains exist, register the exact runtime role, inspect
it, and issue tokens in this order. Both example tokens request only the domain already
covered by the principal grant; the bootstrap token's `--can-create-domain` does not
remove the `--hosted-principal` requirement.

```bash
(
set -e
read -r -s -p 'PostgreSQL schema-owner password: ' IWIKI_DB_PASSWORD
printf '\n'
export IWIKI_DB_PASSWORD
export IWIKI_EMBED_MODEL='replace-with-exact-embedding-model-id'
export IWIKI_EMBED_DIMENSIONS='replace-with-exact-embedding-dimensions'
export IWIKI_RERANK_MODEL='replace-with-exact-rerank-model-id-or-empty'
iwiki-mcp principal grant --config /opt/iwiki-mcp/admin-server.toml --principal replace-with-runtime-role --iwiki replace-with-iwiki-id --read-domain replace-with-domain --write-domain replace-with-domain --runtime hosted --json
iwiki-mcp principal inspect --config /opt/iwiki-mcp/admin-server.toml --principal replace-with-runtime-role --json
iwiki-mcp token create --config /opt/iwiki-mcp/admin-server.toml --iwiki replace-with-iwiki-id --owner replace-with-deploy-owner --hosted-principal replace-with-runtime-role --read-domain replace-with-domain --write-domain replace-with-domain
iwiki-mcp token create --config /opt/iwiki-mcp/admin-server.toml --iwiki replace-with-iwiki-id --owner replace-with-bootstrap-owner --hosted-principal replace-with-runtime-role --read-domain replace-with-domain --write-domain replace-with-domain --can-create-domain
)
```

## Acceptance helper

Hosted MCP uses JSON responses, so it cannot produce a truthful multi-event streaming
probe. The helper below uses real MCP initialize semantics for Authorization and body
limits, and a separate two-event stdlib canary for nginx buffering. It never prints the
token or response content. Save it as `/tmp/iwiki-acceptance.py` and remove it after the
change.

```bash
sudo install -o root -g root -m 0644 /dev/null /tmp/iwiki-acceptance.py
sudoedit /tmp/iwiki-acceptance.py
```

```python
import argparse
import getpass
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import time


MIB = 1024 * 1024


def initialize_body(total_bytes=None):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "operator-acceptance", "version": "1"},
        },
    }
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if total_bytes is None:
        return encoded
    padding = total_bytes - len(encoded)
    if padding < 0:
        raise RuntimeError("target body is too small")
    payload["params"]["clientInfo"]["name"] += "x" * padding
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(encoded) != total_bytes:
        raise RuntimeError("body size construction failed")
    return encoded


def request(method, host, port, body, host_header, origin, token=None, session=None):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Host": host_header,
        "Origin": origin,
    }
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if session is not None:
        headers["Mcp-Session-Id"] = session
    connection = http.client.HTTPConnection(host, port, timeout=90)
    try:
        connection.request(method, "/mcp", body=body, headers=headers)
        response = connection.getresponse()
        status = response.status
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        response_body = response.read()
        return status, response_headers, response_body
    finally:
        connection.close()


def check_mcp(args):
    token = getpass.getpass("MCP bearer token: ")
    small = initialize_body()
    missing, _, _ = request(
        "POST", args.host, args.port, small, args.host_header, args.origin
    )
    invalid, _, _ = request(
        "POST", args.host, args.port, small, args.host_header, args.origin,
        "invalid-acceptance-token"
    )
    exact, headers, body = request(
        "POST", args.host, args.port, initialize_body(16 * MIB),
        args.host_header, args.origin, token
    )
    oversized, _, _ = request(
        "POST", args.host, args.port, initialize_body(16 * MIB + 1),
        args.host_header, args.origin, token
    )
    if (missing, invalid, exact, oversized) != (401, 401, 200, 413):
        raise RuntimeError(
            "unexpected status tuple: " + repr((missing, invalid, exact, oversized))
        )
    parsed = json.loads(body)
    if parsed.get("result", {}).get("serverInfo", {}).get("name") != "iwiki":
        raise RuntimeError("initialize response is not iwiki")
    if not headers.get("mcp-session-id"):
        raise RuntimeError("initialize response has no MCP session")
    print("mcp_acceptance_ok statuses=401,401,200,413 exact_bytes=16777216")


def wait_mcp(args):
    deadline = time.monotonic() + args.timeout
    body = initialize_body()
    while time.monotonic() < deadline:
        try:
            status, _, _ = request(
                "POST", args.host, args.port, body, args.host_header, args.origin
            )
            if status == 401:
                print("mcp_backend_ready status=401")
                return
        except OSError:
            pass
        time.sleep(1)
    raise RuntimeError("MCP backend did not become ready")


class StreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        pass

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
        time.sleep(4)
        self.wfile.write(b"second\n")
        self.wfile.flush()


def stream_server(args):
    HTTPServer((args.host, args.port), StreamHandler).handle_request()


def stream_client(args):
    deadline = time.monotonic() + 15
    while True:
        connection = http.client.HTTPConnection(args.host, args.port, timeout=10)
        started = time.monotonic()
        try:
            connection.request("GET", "/stream", headers={"Host": args.host_header})
            response = connection.getresponse()
            first = response.read(len(b"first\n"))
            first_at = time.monotonic()
            rest = response.read()
            completed_at = time.monotonic()
            break
        except OSError:
            connection.close()
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)
    connection.close()
    if response.status != 200 or first != b"first\n" or rest != b"second\n":
        raise RuntimeError("unexpected stream response")
    if first_at - started >= 3 or completed_at - first_at < 3:
        raise RuntimeError("first event was buffered until completion")
    print("nginx_streaming_ok first_event_before_completion=true")


parser = argparse.ArgumentParser()
commands = parser.add_subparsers(dest="command", required=True)
for name in ("mcp", "wait-mcp"):
    command = commands.add_parser(name)
    command.add_argument("--host", required=True)
    command.add_argument("--port", type=int, required=True)
    command.add_argument("--host-header", required=True)
    command.add_argument("--origin", required=True)
commands.choices["wait-mcp"].add_argument("--timeout", type=int, default=60)
server = commands.add_parser("stream-server")
server.add_argument("--host", default="127.0.0.1")
server.add_argument("--port", type=int, default=8765)
client = commands.add_parser("stream-client")
client.add_argument("--host", required=True)
client.add_argument("--port", type=int, required=True)
client.add_argument("--host-header", required=True)
arguments = parser.parse_args()
{
    "mcp": check_mcp,
    "wait-mcp": wait_mcp,
    "stream-server": stream_server,
    "stream-client": stream_client,
}[arguments.command](arguments)
```

## Isolated pre-cutover validation

The fixed production contract uses host networking and MCP port
`127.0.0.1:8765`. Changing only nginx's listener does not remove that collision with
the current MCP owner. A full combined-container precheck therefore runs on a separate
host or VM with its own network namespace, using the same architecture and image.

The isolated environment must use all of these non-production boundaries:

- a dedicated validation Telegram bot token and validation-only allowlist, never the
  production bot token;
- an isolated PostgreSQL database/schema already migrated to the exact version, never
  the production database;
- a least-privilege validation iwiki token and scope with no production write scope or
  production content;
- the same external HTTPS CONNECT proxy only when operator policy permits the
  validation credential;
- a non-production LAN listener, Host, and allowed Origin.

Do not log secrets, raw Telegram content, MCP responses, or proxy credentials. Confirm
Telegram CONNECT through the proxy operator's credential-free destination/count
telemetry; this project cannot define the external proxy's command.

On that isolated host, render without emitting resolved environment values, build, and
start the validation project:

```bash
sudo docker compose -p iwiki-mcp-validation config --quiet
sudo docker compose -p iwiki-mcp-validation config --services
sudo docker compose -p iwiki-mcp-validation build iwiki
sudo docker compose -p iwiki-mcp-validation up -d
VALIDATION_CONTAINER="$(sudo docker compose -p iwiki-mcp-validation ps -q iwiki)"
test -n "$VALIDATION_CONTAINER"
timeout 120 sh -c 'until [ "$(sudo docker inspect --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}" "$1")" = healthy ]; do sleep 2; done' sh "$VALIDATION_CONTAINER"
sudo docker inspect --format 'health={{.State.Health.Status}}' "$VALIDATION_CONTAINER"
sudo docker exec "$VALIDATION_CONTAINER" supervisorctl -c /etc/supervisor/supervisord.conf status
```

`config --services` must print only `iwiki`; health must print `healthy`; and all three
supervisor children must be `RUNNING`. Health includes the Telegram heartbeat. If
unhealthy, inspect only stable health output:

```bash
sudo docker inspect --format '{{range .State.Health.Log}}{{.ExitCode}} {{.Output}}{{println}}{{end}}' "$VALIDATION_CONTAINER"
```

Run real MCP acceptance through the non-production listener:

```bash
VALIDATION_INGRESS_HOST='replace-with-validation-listener-host'
VALIDATION_INGRESS_PORT='replace-with-validation-listener-port'
VALIDATION_HOST_HEADER='replace-with-validation-allowed-host'
VALIDATION_ORIGIN='https://replace-with-validation-origin'
python3 /tmp/iwiki-acceptance.py mcp --host "$VALIDATION_INGRESS_HOST" --port "$VALIDATION_INGRESS_PORT" --host-header "$VALIDATION_HOST_HEADER" --origin "$VALIDATION_ORIGIN"
```

The helper must report `401,401,200,413`: missing and invalid Authorization are
rejected, a valid exact 16 MiB initialize reaches MCP and returns 200, and the larger
request is rejected by nginx with 413.

Because hosted MCP deliberately uses single JSON responses, verify nginx streaming with
the canary after stopping the validation Compose project. The canary binds the now-free
validation-host port 8765; nginx uses the same validation config and image.

```bash
VALIDATION_IMAGE="$(sudo docker compose -p iwiki-mcp-validation images -q iwiki)"
sudo docker compose -p iwiki-mcp-validation down
python3 /tmp/iwiki-acceptance.py stream-server --host 127.0.0.1 --port 8765 &
STREAM_SERVER_PID=$!
sudo docker run --detach --rm --name iwiki-nginx-stream-check --network host --read-only --user 10001:10001 --tmpfs /run:uid=10001,gid=10001,mode=0750 --tmpfs /tmp:uid=10001,gid=10001,mode=1770 --volume /opt/iwiki-mcp/nginx.conf:/etc/nginx/nginx.conf:ro --entrypoint /usr/sbin/nginx "$VALIDATION_IMAGE" -g 'daemon off;'
python3 /tmp/iwiki-acceptance.py stream-client --host "$VALIDATION_INGRESS_HOST" --port "$VALIDATION_INGRESS_PORT" --host-header "$VALIDATION_HOST_HEADER"
sudo docker stop iwiki-nginx-stream-check
wait "$STREAM_SERVER_PID"
```

Success is `first_event_before_completion=true`. If no isolated host or VM exists, a
zero-downtime full-container precheck is impossible with the current fixed host-network
port. Schedule maintenance downtime and rely on rollback; do not claim concurrent
healthy validation.

## Production inventory and cutover

Before any production change, identify the process currently owning host port 8765 and
record its service manager plus exact stop/start commands in the change record. These
commands show identity and state without command arguments or environment values:

```bash
sudo ss -H -ltnp '( sport = :8765 )'
sudo docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
```

For a systemd-owned MCP process, set and verify the actual unit:

```bash
OLD_MCP_UNIT='replace-with-systemd-unit'
sudo systemctl show "$OLD_MCP_UNIT" --property=Id --property=ActiveState --property=SubState --property=MainPID
```

For a Docker-owned MCP process, set and verify the actual container:

```bash
OLD_MCP_CONTAINER='replace-with-container-name'
sudo docker inspect --format 'name={{.Name}} image={{.Config.Image}} pid={{.State.Pid}} status={{.State.Status}}' "$OLD_MCP_CONTAINER"
```

Render and build before maintenance. Never run unfiltered `compose config` because it
resolves the secret environment file.

```bash
sudo docker compose -p iwiki-mcp config --quiet
sudo docker compose -p iwiki-mcp config --services
sudo docker compose -p iwiki-mcp build iwiki
```

`config --services` must print only `iwiki`. During cutover, stop the old nginx proxy,
then stop the recorded MCP owner using exactly one applicable alternative.

Systemd-owned old MCP:

```bash
OLD_NGINX_CONTAINER='iwiki-mcp-proxy-1'
sudo docker stop "$OLD_NGINX_CONTAINER"
sudo systemctl stop "$OLD_MCP_UNIT"
```

Docker-owned old MCP:

```bash
OLD_NGINX_CONTAINER='iwiki-mcp-proxy-1'
sudo docker stop "$OLD_NGINX_CONTAINER"
sudo docker stop "$OLD_MCP_CONTAINER"
```

Confirm no listener owns 8765, start production, and wait deterministically for health:

```bash
sudo ss -H -ltn '( sport = :8765 )' | awk 'END { exit(NR != 0) }'
sudo docker compose -p iwiki-mcp up -d
COMBINED_CONTAINER="$(sudo docker compose -p iwiki-mcp ps -q iwiki)"
test -n "$COMBINED_CONTAINER"
timeout 120 sh -c 'until [ "$(sudo docker inspect --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}" "$1")" = healthy ]; do sleep 2; done' sh "$COMBINED_CONTAINER"
sudo docker inspect --format 'health={{.State.Health.Status}}' "$COMBINED_CONTAINER"
sudo docker exec "$COMBINED_CONTAINER" supervisorctl -c /etc/supervisor/supervisord.conf status
```

Only after health is `healthy` and all three children are `RUNNING`, switch Traefik/LAN
to the production nginx listener. Then run MCP acceptance with production listener,
Host, and Origin values; `getpass` keeps the token out of argv and history.

```bash
PRODUCTION_INGRESS_HOST='replace-with-production-listener-host'
PRODUCTION_INGRESS_PORT='replace-with-production-listener-port'
PRODUCTION_HOST_HEADER='replace-with-production-allowed-host'
PRODUCTION_ORIGIN='https://replace-with-production-origin'
python3 /tmp/iwiki-acceptance.py mcp --host "$PRODUCTION_INGRESS_HOST" --port "$PRODUCTION_INGRESS_PORT" --host-header "$PRODUCTION_HOST_HEADER" --origin "$PRODUCTION_ORIGIN"
```

Acceptance requires `401,401,200,413`, fresh health, three RUNNING children, and
credential-free proxy telemetry showing Telegram CONNECT destination/count activity.
Streaming was separately proven against the same image and nginx directives on the
isolated host.

## Rollback before old-component removal

The stopped old MCP owner and `iwiki-mcp-proxy-1` remain restartable through the agreed
acceptance/rollback window. On any failed cutover or acceptance check:

1. Stop the combined container.
2. Restart the recorded old MCP owner with its exact service manager.
3. Wait for old loopback MCP to return the expected unauthenticated 401.
4. Restart the old nginx proxy.
5. Restore the previous Traefik/LAN target and run MCP acceptance through it.

Systemd-owned old MCP:

```bash
sudo docker compose -p iwiki-mcp down
sudo systemctl start "$OLD_MCP_UNIT"
python3 /tmp/iwiki-acceptance.py wait-mcp --host 127.0.0.1 --port 8765 --host-header "$PRODUCTION_HOST_HEADER" --origin "$PRODUCTION_ORIGIN" --timeout 60
sudo docker start "$OLD_NGINX_CONTAINER"
```

Docker-owned old MCP:

```bash
sudo docker compose -p iwiki-mcp down
sudo docker start "$OLD_MCP_CONTAINER"
python3 /tmp/iwiki-acceptance.py wait-mcp --host 127.0.0.1 --port 8765 --host-header "$PRODUCTION_HOST_HEADER" --origin "$PRODUCTION_ORIGIN" --timeout 60
sudo docker start "$OLD_NGINX_CONTAINER"
```

After restoring the old Traefik/LAN target, rerun `mcp` mode. Rollback succeeds only
when backend readiness reports 401, old ingress returns `401,401,200,413`, and normal
authorized MCP traffic is restored. Rollback never changes PostgreSQL.

## Removal after the rollback window

Remove old components only after production acceptance and the agreed rollback window.
Before removing Docker components, print only name, image, and Compose identity; never
use raw `docker inspect`, which includes environment values.

```bash
sudo docker inspect --format 'name={{.Name}} image={{.Config.Image}} project={{index .Config.Labels "com.docker.compose.project"}} service={{index .Config.Labels "com.docker.compose.service"}}' "$OLD_NGINX_CONTAINER"
sudo docker rm "$OLD_NGINX_CONTAINER"
```

For a Docker-owned old MCP, inspect and remove its recorded container separately:

```bash
sudo docker inspect --format 'name={{.Name}} image={{.Config.Image}} project={{index .Config.Labels "com.docker.compose.project"}} service={{index .Config.Labels "com.docker.compose.service"}}' "$OLD_MCP_CONTAINER"
sudo docker rm "$OLD_MCP_CONTAINER"
```

For systemd, retain or remove the recorded unit through the operator's service-management
change process; this runbook does not invent a unit name. Remove the temporary helper
and retain the privileged admin config only under the operator's protected credential
policy.

```bash
sudo rm /tmp/iwiki-acceptance.py
```

## Health, recovery, and privacy

Compose uses `restart: unless-stopped`, `stop_grace_period: 60s`, a read-only root
filesystem, and tmpfs mounts for `/run` and `/tmp`. Supervisor restarts unexpected exits
of hosted MCP, nginx, or the Telegram bot. Health verifies all three children, loopback
MCP, nginx ingress, and a Telegram heartbeat newer than the configured window; it makes
no extra Telegram request.

A proxy outage makes the heartbeat stale and the container unhealthy while the bot
keeps retrying through the same proxy. It never reroutes Telegram directly. Telegram
updates, user identifiers, message content, prompts, answers, transcriptions, voice
files, selected domains, and pending confirmation previews live only in memory or tmpfs
and do not survive restart. Confirmation tokens remain user-bound, expiring, and
single-use; page updates retain revision and section-hash compare-and-swap.

Container logs expose stable operational fields and health codes only. They must not
contain secrets, proxy URLs, credentials, request/response bodies, Telegram content,
filenames, audio, or transcriptions. PostgreSQL remains the only durable application
state and is outside container lifecycle.
