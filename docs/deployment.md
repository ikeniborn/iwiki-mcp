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

## Operator inputs

Keep runtime configuration outside the checkout:

```text
/opt/iwiki-mcp/server.toml       hosted MCP and external PostgreSQL endpoint
/opt/iwiki-mcp/nginx.conf        LAN/Traefik listener and loopback upstream
/opt/iwiki-mcp/runtime.env       owner-only runtime secrets and bot settings
```

`server.toml` must retain the MCP loopback listener on `127.0.0.1:8765`. For a
separately managed PostgreSQL container on the same host, publish a host port such as
`127.0.0.1:55432` and configure `storage.host = "127.0.0.1"` and
`storage.port = 55432`. A bridge-only service name is not reachable from the
host-network application container. For remote PostgreSQL, configure its DNS name or
IP and custom port and use `sslmode = "verify-full"` with a trusted CA and matching
hostname.

This Compose project creates no PostgreSQL service, database, or schema migration.
Provision a compatible database and least-privilege runtime role before starting the
container. PostgreSQL remains the durable service throughout deployment and rollback.

Copy the nginx template, then change only the host-specific `listen` value. Keep
`proxy_pass http://127.0.0.1:8765`, explicit `Authorization` forwarding, disabled
request and response buffering, `client_max_body_size 16m`, and `access_log off`
unchanged.

```bash
sudo install -d -m 0750 /opt/iwiki-mcp
sudo install -m 0644 deploy/nginx.conf.example /opt/iwiki-mcp/nginx.conf
sudoedit /opt/iwiki-mcp/server.toml
sudoedit /opt/iwiki-mcp/nginx.conf
sudoedit /opt/iwiki-mcp/runtime.env
sudo chmod 0600 /opt/iwiki-mcp/runtime.env
```

`runtime.env` supplies the PostgreSQL password, server inference settings, bot
settings, the dedicated iwiki and inference credentials, the Telegram proxy URL, and
health values. At minimum, set `IWIKI_INGRESS_HEALTH_HOST` to the nginx listener,
optionally set `IWIKI_INGRESS_HEALTH_PORT` (default `8766`), and size
`IWIKI_BOT_HEARTBEAT_MAX_AGE_SECONDS` above the expected 30-second Telegram long-poll
window (default `120`). Never place secrets in `server.toml`, `nginx.conf`, Compose,
image layers, or command lines.

## Reversible migration from `iwiki-mcp-proxy-1`

Keep the old nginx proxy stopped but recoverable until all acceptance checks pass. Do
not modify PostgreSQL during this migration.

1. Copy and validate `server.toml`, `nginx.conf`, and `runtime.env`. Confirm the MCP
   loopback, PostgreSQL endpoint, nginx upstream, allowed origins, proxy URL, and health
   listener agree. Protect `runtime.env` with mode `0600` before any Compose command.
2. Build the application image and render Compose. The rendered service list must
   contain only `iwiki`; inspect the rendered configuration for the expected read-only
   mounts, host networking, tmpfs mounts, and absence of standard proxy variables.
3. Copy nginx and runtime configuration to temporary validation files. Select a free,
   non-conflicting LAN address/port in the validation nginx `listen` directive and set
   the validation health host/port to that listener. Keep both validation files
   owner-only when they contain secrets.
4. Start the combined container under a validation Compose project using those
   temporary files. Wait for Docker health to become `healthy`; inspect child state and
   secret-free logs. The old `iwiki-mcp-proxy-1` must still be running during this step.
5. Stop the validation project. Only after its healthy result, stop
   `iwiki-mcp-proxy-1`, start the combined container with the production files, then
   switch the Traefik/LAN target to the production nginx listener.
6. Run acceptance through the production listener: valid `Authorization` reaches MCP;
   missing or invalid authorization remains rejected; a body at the 16 MiB nginx limit
   reaches upstream while an oversized body receives `413`; and Streamable HTTP data is
   delivered without nginx buffering. Confirm Telegram polling heartbeat is fresh and
   the external proxy observed Telegram CONNECT traffic.
7. Observe normal traffic through an agreed acceptance window. Remove
   `iwiki-mcp-proxy-1` only after the operator accepts every check. Then delete the
   temporary validation files.

Build and render from the repository checkout:

```bash
sudo docker compose build iwiki
sudo docker compose config --quiet
sudo docker compose config --services
```

Prepare validation copies, edit their listener/health values, then render and start the
validation project:

```bash
sudo install -m 0644 /opt/iwiki-mcp/nginx.conf /opt/iwiki-mcp/nginx.validation.conf
sudo install -m 0600 /opt/iwiki-mcp/runtime.env /opt/iwiki-mcp/runtime.validation.env
sudoedit /opt/iwiki-mcp/nginx.validation.conf
sudoedit /opt/iwiki-mcp/runtime.validation.env
sudo env IWIKI_NGINX_CONFIG_FILE=/opt/iwiki-mcp/nginx.validation.conf IWIKI_RUNTIME_ENV_FILE=/opt/iwiki-mcp/runtime.validation.env docker compose -p iwiki-mcp-validation config --quiet
sudo env IWIKI_NGINX_CONFIG_FILE=/opt/iwiki-mcp/nginx.validation.conf IWIKI_RUNTIME_ENV_FILE=/opt/iwiki-mcp/runtime.validation.env docker compose -p iwiki-mcp-validation up -d
sudo docker compose -p iwiki-mcp-validation ps
sudo docker compose -p iwiki-mcp-validation logs --no-color
```

After validation is healthy, perform the production cutover in this order:

```bash
sudo env IWIKI_NGINX_CONFIG_FILE=/opt/iwiki-mcp/nginx.validation.conf IWIKI_RUNTIME_ENV_FILE=/opt/iwiki-mcp/runtime.validation.env docker compose -p iwiki-mcp-validation down
sudo docker stop iwiki-mcp-proxy-1
sudo docker compose -p iwiki-mcp up -d
sudo docker compose -p iwiki-mcp ps
```

After accepted verification and the agreed observation window, removal is irreversible
unless the old container can be recreated from its original deployment definition.
Confirm its exact identity before running:

```bash
sudo docker inspect iwiki-mcp-proxy-1
sudo docker rm iwiki-mcp-proxy-1
sudo rm /opt/iwiki-mcp/nginx.validation.conf /opt/iwiki-mcp/runtime.validation.env
```

## Rollback before old-proxy removal

Rollback changes only ingress/application processes. It never changes PostgreSQL:

1. Stop the combined container with its 60-second graceful-stop allowance.
2. Restart `iwiki-mcp-proxy-1`.
3. Restore the previous Traefik/LAN target and verify the old MCP path.
4. Keep the combined configuration and logs for diagnosis; do not alter the external
   PostgreSQL service.

```bash
sudo docker compose -p iwiki-mcp down
sudo docker start iwiki-mcp-proxy-1
```

## Health, recovery, and privacy

Compose uses `restart: unless-stopped`, `stop_grace_period: 60s`, a read-only root
filesystem, and tmpfs mounts for `/run` and `/tmp`. Supervisor restarts any unexpected
exit of hosted MCP, nginx, or the Telegram bot. The local healthcheck verifies all
three children, loopback MCP, nginx ingress, and a Telegram heartbeat newer than the
configured liveness window; it makes no extra Telegram request.

A proxy outage makes the heartbeat stale and the container unhealthy while the bot
keeps retrying through the same proxy. It never reroutes Telegram directly. Recovery
resumes polling through the proxy without persisting Telegram state.

Telegram updates, user identifiers, message content, prompts, answers, transcriptions,
voice files, selected domains, and pending confirmation previews live only in process
memory or tmpfs and do not survive restart. Confirmation tokens remain bound to one
Telegram user, expire, and are single-use. Page updates retain revision and
section-hash compare-and-swap; conflicts require a fresh preview.

Container logs expose stable operational fields and health error codes only. They must
not contain secrets, proxy URLs, credentials, request or response bodies, Telegram
content, filenames, audio, or transcriptions. PostgreSQL remains the only durable
application state and is outside container lifecycle.
