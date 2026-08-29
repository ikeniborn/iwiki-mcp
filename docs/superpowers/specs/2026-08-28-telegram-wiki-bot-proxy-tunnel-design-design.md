---
review:
  spec_hash: fbe128aba23a9225
  last_run: 2026-08-28
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-08-28-telegram-wiki-bot-proxy-tunnel-design-intent.md
---
# Telegram Wiki Bot HTTPS Proxy and Single-Container Deployment Design

**Date:** 2026-08-28
**Status:** approved
**Intent:** `docs/superpowers/intents/2026-08-28-telegram-wiki-bot-proxy-tunnel-design-intent.md`

## 1. Purpose and Scope

This design packages the hosted iwiki MCP server, Telegram wiki bot, and the
existing LAN/Traefik nginx ingress in one runtime container. The bot sends only
Telegram Bot API traffic through an existing operator-managed HTTPS forward
proxy. The project does not ship, start, or manage GOST, stunnel, another local
proxy process, or PostgreSQL.

The HTTPS proxy is a deployment dependency reachable from the application
host. It already has network access to `api.telegram.org`. The bot connects to
the proxy over TLS, sends an HTTP `CONNECT api.telegram.org:443` request, and
then establishes Telegram TLS inside that tunnel. The proxy URL therefore
describes the protocol used between the bot and proxy; it is not an HTTP proxy
merely selected for an HTTPS destination.

The existing PostgreSQL configuration remains authoritative. PostgreSQL may be
a separately managed container on the same physical server with a port
published on the host, or a service on another host. The runtime container may
read and write through its existing least-privilege role, but it does not own
database creation, bootstrap, migrations, or database lifecycle.

Non-goals are deploying the external proxy, creating PostgreSQL, changing MCP
authentication or tenant isolation, adding a direct Telegram fallback, routing
iwiki or inference through the Telegram proxy, persisting Telegram content, or
changing write-confirmation semantics.

## 2. Decisions and Alternatives

### R1. Explicit HTTPS Proxy Transport

The Telegram transport uses `urllib3.ProxyManager` with one explicitly
configured `https://` proxy. `urllib3` documents HTTPS-to-HTTPS proxying as
TLS-in-TLS: TLS to the proxy, HTTP CONNECT, and TLS to the destination. The
implementation runs the blocking pool request in an AnyIO worker thread so the
existing async bot composition remains intact. The bot performs one long poll
at a time, so one occupied worker during that request is bounded and expected.

Reference:
<https://urllib3.readthedocs.io/en/stable/advanced-usage.html#http-and-https-proxies>

Rejected alternatives:

| Alternative | Reason not selected |
|---|---|
| Current HTTPX transport | HTTPX documents HTTPS proxy connections as unsupported; using it would not prove the literal `https://` contract. |
| `curl_cffi.AsyncSession` | libcurl supports HTTPS proxies, but this adds a large binary wheel and a second HTTP client maintenance surface when the sequential bot transport can use established pure-Python urllib3 through one worker thread. It remains a fallback only if cancellation or long-poll tests disprove the selected design. |
| Local stunnel plus an HTTP/SOCKS proxy | Adds a process and second proxy configuration, while the bot-facing effective endpoint becomes local HTTP or SOCKS. It is unnecessary because the external proxy already implements HTTPS CONNECT. |
| Global `HTTP_PROXY` or `HTTPS_PROXY` | Would risk capturing iwiki and inference traffic and does not create a Telegram-only boundary. |

HTTPX reference:
<https://www.python-httpx.org/troubleshooting/#the-https-proxy-gotcha>

Acceptance criterion: an integration test observes TLS to a test proxy,
CONNECT for `api.telegram.org:443`, and tunneled HTTPS requests for long
polling, replies, metadata, and file bytes while the direct Telegram route is
unavailable.

### R2. One Application Container

The repository supplies one Docker image and one service in `compose.yaml`.
That container runs exactly these required child processes under
`supervisord`:

1. `iwiki-mcp serve --transport streamable-http`, listening on
   `127.0.0.1:8765`.
2. nginx, exposing the configured LAN/Traefik listener and forwarding to the
   loopback MCP server.
3. `iwiki-telegram-bot`, using the external HTTPS proxy only for Telegram.

`supervisord` is PID 1, runs in the foreground, forwards TERM, stops child
process groups, and restarts children that exit unexpectedly. It writes child
stdout and stderr to container streams without a separate log file. The
container does not run systemd or s6-overlay and does not contain a proxy
daemon.

Docker documents a process manager such as supervisord as an available pattern
when multiple processes must run in one container:
<https://docs.docker.com/engine/containers/multi-service_container/>.

The image runs required services as an unprivileged runtime user. Ports 8765
and 8766 do not require privileged binding. The root filesystem is read-only;
`/run` and `/tmp` are tmpfs mounts. `stop_grace_period` exceeds the Telegram
request read timeout so normal shutdown is bounded.

Acceptance criterion: the built container has one service definition, all
three children reach RUNNING, TERM stops them as groups, an unexpected child
exit triggers its configured restart, and no GOST, stunnel, or PostgreSQL
process or image is present.

### R3. LAN and Traefik nginx Ingress

Host networking preserves the deployed topology. The hosted MCP process stays
on `127.0.0.1:8765`. nginx listens on the operator-configured LAN address and
port; the production example is `192.168.68.123:8766`. Traefik or another
authorized LAN client reaches that nginx listener.

The final host-specific nginx configuration is mounted read-only from
`/opt/iwiki-mcp/nginx.conf`. The repository supplies
`deploy/nginx.conf.example`; operators copy it to the final path and change
only host-specific values. This avoids binding all interfaces merely for
portability and avoids rebuilding the image for another LAN address.

The nginx contract is:

- one `location /` forwards every path to `http://127.0.0.1:8765`;
- `Authorization` is passed explicitly from the client request;
- HTTP/1.1 is used upstream;
- proxy buffering is disabled for Streamable HTTP behavior;
- request bodies up to 16 MiB are accepted;
- the access log is disabled;
- the error log contains operational diagnostics only and never request
  bodies or authorization values;
- nginx is not configured as an outbound or CONNECT proxy.

Acceptance criterion: container integration tests prove authorized MCP
traffic crosses nginx, missing or invalid authorization remains rejected by
the MCP server, a body at the configured boundary is accepted, an oversized
body is rejected, streaming is not buffered, and no access-log entry is
created.

### R4. PostgreSQL Connectivity Without Ownership

The container loads the existing `[storage]` configuration and
`IWIKI_DB_PASSWORD`. It retains the current runtime principal checks, schema
version validation, statement timeouts, transaction behavior, tenant scope,
revision compare-and-swap, and code-graph storage behavior.

With `network_mode: host`, a PostgreSQL container on the same physical server
must publish its port on a host address. A loopback publication such as
`127.0.0.1:55432` is addressed with `storage.host = "127.0.0.1"` and
`storage.port = 55432`. A database that is available only by a Docker bridge
service name is intentionally not reachable from this host-network container.
The separately managed database deployment owns that port publication.

A remote PostgreSQL endpoint uses its configured DNS name or IP, custom port,
database, user, and explicit `sslmode`. Remote production connections retain
the documented `verify-full` recommendation, trusted CA, and matching database
hostname. No PostgreSQL connection uses the Telegram proxy.

The runtime container does not create a database, bootstrap PostgreSQL, or run
operator migrations. Startup fails closed when the database is unreachable,
the schema is incompatible, or the runtime role is invalid. After the database
recovers, supervisord restarts the failed MCP child; the bot retries its local
MCP startup dependency and reconnects.

Acceptance criterion: integration tests exercise real read/write operations
through a same-host published port and a configurable remote/custom-port
fixture, reject an invalid runtime role or incompatible schema, and prove the
application Compose file contains no PostgreSQL service.

## 3. Configuration and Transport Boundaries

### R5. Proxy URL Contract

`IWIKI_BOT_TELEGRAM_PROXY_URL` is required by the containerized bot deployment.
Its value must literally begin with lowercase `https://`, contain a hostname,
and contain an explicit port. URL-encoded user information is supported for
proxy Basic authentication. A path other than an optional `/`, query,
fragment, missing port, HTTP scheme, or SOCKS scheme is rejected before any
network request.

Examples of accepted shapes are:

```text
https://proxy.example:8443
https://user:password@proxy.example:8443
```

The parser removes user information from the internal proxy origin. It builds
`https://host:port` for `ProxyManager` and provides credentials through a
separate `Proxy-Authorization` header. The manager uses certificate and
hostname verification through the system trust store. There is no
`verify=False` option and no silent downgrade. Support for a private CA is out
of scope until an operator requirement demonstrates it.

`BotConfig` marks the Telegram token, iwiki token, LLM key, and proxy URL as
`repr=False`. Configuration validation reports only stable field names and
error codes, never values.

Acceptance criterion: configuration tests cover accepted forms and reject
every invalid form without exposing a supplied username, password, hostname,
token, or URL in exception text or object representations.

### R6. Telegram-Only Routing

One Telegram proxy client owns every request to the Bot API and file endpoint:

- `getUpdates` long polling;
- `sendMessage` and `answerCallbackQuery`;
- `getFile` metadata;
- `https://api.telegram.org/file/...` voice bytes.

The client accepts only the fixed `api.telegram.org` API and file bases built
from the configured bot token. It does not accept user-supplied origins and
does not follow redirects to another host. The existing injected transport
test boundary is retained through a small request adapter rather than exposing
urllib3 objects to conversation logic.

Inference creates its HTTPX client with `trust_env=False`. The remote iwiki MCP
client supplies an HTTPX factory with `trust_env=False`. The Compose service
does not define standard proxy environment variables. PostgreSQL continues to
use psycopg directly. These choices prevent the Telegram proxy setting from
affecting other traffic even if the host environment defines proxy variables.

Acceptance criterion: focused tests observe the explicit proxy on all four
Telegram request classes, observe no proxy on inference or remote iwiki, and
prove an external proxy failure never causes a direct Telegram connection.

### R7. Timeouts, Retry, and Ambiguous Delivery

The Telegram Bot API long-poll argument remains 30 seconds. Proxy connection
timeout is bounded separately from a read timeout longer than the long-poll
window. The bot updates a monotonic backoff after a sanitized polling failure,
caps the delay, adds bounded jitter, and resets the delay after a successful
poll.

urllib3 automatic retries are disabled for Telegram POST operations. In
particular, an ambiguous `sendMessage` result is not retried automatically,
because Telegram may already have accepted the first request. Polling retries
reuse the current update offset. Existing confirmation tokens remain
single-use and user-bound, so a repeated callback cannot repeat a successful
write.

Startup retries the inference probe and local iwiki readiness with bounded
backoff instead of exiting in a hot loop while dependencies start. A fatal
configuration error exits immediately and is not treated as transient.

Acceptance criterion: deterministic clock tests verify capped backoff and
reset, a proxy outage produces no direct connection, recovery resumes polling,
and a simulated ambiguous send produces no automatic second send.

## 4. Privacy, Logging, and Health

### R8. Secret-Safe Failure Boundary

Low-level urllib3 and HTTPX exceptions may contain request or proxy URLs. They
are caught at the transport boundary and converted to stable application error
codes without exception chaining. The long-running bot entry point catches
expected dependency errors and logs only operation, outcome, retry delay, and
elapsed time. It never logs exception representations, request JSON, response
bodies, update objects, message text, transcriptions, filenames, tokens, or
proxy URLs.

Secrets are supplied at runtime and are absent from the image, repository,
nginx configuration, supervisord command lines, and healthcheck output.
Supervisor does not write a persistent logfile. nginx access logging remains
off. Test marker values guard the complete container log stream.

Acceptance criterion: unit and container tests inject unique marker values for
every secret and content class, force representative failures, and assert no
marker appears in stdout, stderr, configuration errors, supervisor output, or
nginx output.

### R9. Transient Telegram Content

Updates, messages, replies, and transcriptions remain in process memory. Voice
bytes may use the existing `NamedTemporaryFile` only inside `/tmp`, which is a
tmpfs mount with no volume. The context manager deletes the file immediately
after transcription. No Telegram content is written to PostgreSQL by proxy
support, image layers, container logs, health state, or a mounted filesystem.

The health heartbeat contains only a timestamp written under `/run`. It is
updated after a successful long poll and is discarded when the container
stops. Confirmation previews remain in memory; restart invalidates them safely.

Acceptance criterion: tests observe voice cleanup on success and failure,
inspect mounted paths and logs for content markers, and prove restart retains
no update, audio, transcription, reply, or confirmation preview.

### R10. Container Health and Recovery

One Docker healthcheck performs only local checks:

- all required supervisor children are RUNNING;
- the loopback MCP listener responds at `127.0.0.1:8765` with an expected
  protocol or authorization status;
- the nginx listener responds through the configured ingress address;
- the Telegram heartbeat is newer than the configured liveness window.

The healthcheck does not issue an extra Telegram request and never prints
environment values. A stale heartbeat marks the container unhealthy while the
bot keeps retrying the external proxy. Docker health status provides operator
visibility; bot recovery does not require a container restart. The Compose
restart policy remains `unless-stopped` for process/container exit recovery.

Acceptance criterion: health tests cover healthy startup, every child stopped,
MCP and nginx failures, stale heartbeat, proxy recovery, and secret-free output.

## 5. Delivery Artifacts and Verification

### R11. Repository Artifacts

Implementation adds or updates only these delivery surfaces:

- root `Dockerfile` and `.dockerignore`;
- root `compose.yaml` with exactly one application service;
- `deploy/supervisord.conf`;
- `deploy/nginx.conf.example`;
- `deploy/healthcheck.py`;
- Telegram configuration and transport modules;
- focused tests under `tests/telegram_bot/` and container/deployment tests;
- `README.md`, `docs/README.ru.md`, `docs/telegram-bot.md`, and deployment or
  architecture documentation needed to explain the new supported path;
- `pyproject.toml` and `uv.lock` for the direct urllib3 dependency and project
  version.

The Compose service uses host networking, `restart: unless-stopped`, a
read-only root filesystem, tmpfs for `/run` and `/tmp`, the read-only final
nginx config, and runtime configuration mounts/environment. It declares no
PostgreSQL, GOST, stunnel, or second application service.

Acceptance criterion: `docker compose config` resolves to one service, the
image builds reproducibly from pinned bases, the image smoke test starts the
three supervised processes, and filesystem/mount inspection matches this
contract.

### R12. End-to-End Acceptance

The production-like acceptance environment denies direct access from the
application host to Telegram but permits the configured HTTPS proxy. It uses
an operator-supplied external PostgreSQL endpoint and inference endpoint. The
test then proves:

1. The single container starts and its healthcheck becomes healthy.
2. An authorized MCP client reaches the loopback server through nginx with its
   Authorization header intact.
3. The bot receives a text update through long polling and sends a reply.
4. The bot retrieves voice metadata and bytes through the same proxy and sends
   the transcription workflow reply.
5. A user-bound confirmation performs one safe wiki write in external
   PostgreSQL with existing revision and section-hash conflict protection.
6. Proxy interruption stops Telegram operations without enabling a direct
   route; restoring it resumes polling.
7. iwiki, inference, and PostgreSQL traffic retain their direct routes.
8. Container logs and mounted files contain no injected secret or Telegram
   content markers.

## 6. Acceptance (from intent)

### Desired Outcomes (verbatim)

- An operator starts one container that runs the hosted iwiki MCP server, Telegram bot, LAN/Traefik reverse proxy, and Telegram HTTPS proxy integration without deploying PostgreSQL from this project.
- The hosted iwiki MCP server connects to the separately managed PostgreSQL service and remains available to the Telegram bot and authorized MCP clients.
- The single container replaces the current separate `iwiki-mcp-proxy-1` deployment while preserving its LAN/Traefik ingress behavior.
- The bot starts without direct network access to Telegram.
- The bot receives Telegram updates through long polling over the configured proxy.
- The bot sends Telegram replies over the configured proxy.
- The bot downloads Telegram voice files over the configured proxy.

### Done when (verbatim)

- Done when: the checked design defines the literal HTTPS proxy URL contract with custom ports; specifies one supervised runtime container containing the hosted MCP server, Telegram bot, LAN/Traefik nginx ingress, and any required local tunnel process without PostgreSQL; preserves the current `127.0.0.1:8765` to LAN/Traefik reverse-proxy contract; compares viable HTTPS tunnel options; recommends one with evidence; specifies process, proxy, and container failure recovery; and defines acceptance checks for hosted MCP ingress plus Telegram startup, long polling, replies, and voice-file downloads without direct Telegram access.

## 7. Requirement Traceability

| Intent requirement | Design coverage |
|---|---|
| One runtime container | R2, R3, R11, R12 |
| External PostgreSQL with unchanged semantics | R4, R12 |
| Literal HTTPS proxy URL and custom port | R1, R5 |
| Long polling, replies, and voice downloads through proxy | R6, R7, R12 |
| No Telegram direct fallback | R6, R7, R12 |
| Secrets absent from logs | R5, R8, R10, R12 |
| Telegram content not persisted | R9, R12 |
| iwiki and inference direct | R6, R12 |
| Existing confirmation safety | R7, R9, R12 |
| Preserved LAN/Traefik nginx ingress | R2, R3, R10, R12 |
| Failure and recovery behavior | R7, R10, R12 |
